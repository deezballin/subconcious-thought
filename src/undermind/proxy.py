"""
Proxy layer for Undermind.

Wraps the configured primary pipeline behind an Ollama-compatible HTTP API on
localhost:11435, so existing wrappers (Hermes, OpenClaw, anything speaking the
Ollama protocol) work unchanged.

For every incoming prompt the proxy:
1. Returns the cached response instantly when the prompt hash matches.
2. On a miss, streams a draft continuation with confidence tracking; the
   instant token confidence crosses the threshold, the full text branch
   (prompt + continuation) is executed on the primary pipeline.
3. If confidence never crosses, the primary executes the raw prompt.
4. The result is cached (TTL) and returned in Ollama response format.

Handoffs are recorded to the shared SQLite store.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from undermind.config import Config, load_config
from undermind.confidence import ConfidenceTracker
from undermind.handoff import UniversalHandoff
from undermind.predictor import DraftPredictor
from undermind.providers import build_draft_client, build_primary_provider
from undermind.providers.base import DraftClient, PrimaryProvider, ProviderError
from undermind.store import UndermindStore

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 11435
CACHE_TTL = 30
MODELS_CACHE_TTL_S = 30.0


class PredictionCache:
    """Thread-safe TTL cache for executed responses."""

    def __init__(self, ttl: float = CACHE_TTL) -> None:
        self.ttl = ttl
        self._store: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def set(self, key: str, response: str) -> None:
        with self._lock:
            self._store[key] = (response, time.monotonic())

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            response, timestamp = entry
            if time.monotonic() - timestamp > self.ttl:
                del self._store[key]
                return None
            return response

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class _StopStream(Exception):
    """Internal: unwinds the token stream after the threshold crossing."""


class UndermindProxy(BaseHTTPRequestHandler):
    """Ollama-compatible HTTP handler backed by the universal pipeline."""

    cache: PredictionCache = None  # type: ignore[assignment]
    draft_client: DraftClient = None  # type: ignore[assignment]
    primary: PrimaryProvider = None  # type: ignore[assignment]
    handoff: UniversalHandoff = None  # type: ignore[assignment]
    store: UndermindStore = None  # type: ignore[assignment]
    config: Config = None  # type: ignore[assignment]
    predictor: DraftPredictor = None  # type: ignore[assignment]
    _models_cache: tuple[float, list[str]] = (0.0, [])

    # ------------------------------------------------------------------
    # routing
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/tags":
            self._handle_tags()
        elif path == "/api/ps":
            self._handle_ps()
        else:
            self._not_found()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/generate":
            self._handle_generate()
        elif path == "/api/chat":
            self._handle_chat()
        else:
            self._not_found()

    def log_message(self, format, *args) -> None:
        pass

    # ------------------------------------------------------------------
    # info endpoints
    # ------------------------------------------------------------------

    def _handle_tags(self) -> None:
        if self.config.primary.kind == "ollama":
            if self._passthrough_get("/api/tags"):
                return
        models = self._available_models()
        self._send_json(
            {"models": [{"name": m, "model": m} for m in models]}
        )

    def _handle_ps(self) -> None:
        if self.config.primary.kind == "ollama":
            if self._passthrough_get("/api/ps"):
                return
        self._send_json(
            {"models": [{"name": self.config.primary.model, "model": self.config.primary.model}]}
        )

    def _available_models(self) -> list[str]:
        now = time.monotonic()
        ts, cached = self._models_cache
        if now - ts < MODELS_CACHE_TTL_S and cached:
            return cached
        try:
            models = self.draft_client.list_models()
        except Exception:
            models = [self.config.draft.model, self.config.primary.model]
        self.__class__._models_cache = (now, models)
        return models

    def _passthrough_get(self, path: str) -> bool:
        """Forward a GET to the Ollama-native primary; True when served."""
        if self.config.primary.kind != "ollama":
            return False
        try:
            with urlopen(
                f"{self.config.primary.base_url}{path}", timeout=5
            ) as resp:
                self._send_raw(resp.status, resp.read())
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # completion endpoints
    # ------------------------------------------------------------------

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    @staticmethod
    def _hash_prompt(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    @staticmethod
    def _last_user_content(messages: list) -> str:
        for message in reversed(messages or []):
            if message.get("role") == "user":
                return str(message.get("content", ""))
        texts = [str(m.get("content", "")) for m in messages or []]
        return "\n".join(texts)

    def _handle_generate(self) -> None:
        body = self._read_body()
        prompt = str(body.get("prompt", ""))
        model = str(body.get("model", self.config.primary.model))
        self._serve_completion(prompt, model, chat_format=False)

    def _handle_chat(self) -> None:
        body = self._read_body()
        prompt = self._last_user_content(body.get("messages", []))
        model = str(body.get("model", self.config.primary.model))
        self._serve_completion(prompt, model, chat_format=True)

    def _serve_completion(self, prompt: str, model: str, chat_format: bool) -> None:
        key = self._hash_prompt(prompt)
        cached = self.cache.get(key)
        if cached is not None:
            self._send_completion(cached, model, chat_format, cached=True, meta=None)
            return

        try:
            outcome = self._execute_branch(prompt)
        except ProviderError as exc:
            self._error(exc, status=502)
            return
        except Exception as exc:
            self._error(exc, status=500)
            return

        self.cache.set(key, outcome["response"])
        self._send_completion(
            outcome["response"], model, chat_format, cached=False, meta=outcome
        )

    def _send_completion(
        self,
        text: str,
        model: str,
        chat_format: bool,
        cached: bool,
        meta: Optional[dict],
    ) -> None:
        undermind = {"cached": cached}
        if meta:
            undermind.update(
                {
                    "confidence_crossed": meta["crossed"],
                    "confidence": meta["confidence"],
                    "latency_ms": meta["latency_ms"],
                }
            )
        if chat_format:
            self._send_json(
                {
                    "model": model,
                    "message": {"role": "assistant", "content": text},
                    "cached": cached,
                    "undermind": undermind,
                }
            )
        else:
            self._send_json(
                {"model": model, "response": text, "cached": cached, "undermind": undermind}
            )

    # ------------------------------------------------------------------
    # confidence-crossing branch execution
    # ------------------------------------------------------------------

    def _execute_branch(self, prompt: str) -> dict:
        """Predict, cross the threshold, execute on the primary, record it."""
        start_ns = time.perf_counter_ns()
        crossed = False
        confidence: Optional[float] = None
        branch = prompt

        if prompt.strip():
            tracker = self._predict_with_confidence(prompt)
            if tracker.crossed and tracker.crossing is not None:
                crossed = True
                confidence = tracker.crossing.confidence
                branch = prompt + tracker.text

        result = self.primary.execute(
            branch, context=self.config.primary.system_prompt or None
        )
        latency_ms = (time.perf_counter_ns() - start_ns) / 1e6
        self.store.record_handoff(
            trigger="proxy_confidence_cross" if crossed else "proxy_direct",
            branch=branch,
            provider=self.config.primary.kind,
            model=self.config.primary.model,
            status="ok",
            confidence=confidence,
            latency_ms=latency_ms,
        )
        return {
            "response": result,
            "crossed": crossed,
            "confidence": confidence,
            "latency_ms": latency_ms,
        }

    def _predict_with_confidence(self, prompt: str) -> ConfidenceTracker:
        """Stream a draft continuation, stopping at the threshold crossing."""
        conf_kwargs = self.predictor.confidence_kwargs
        tracker = ConfidenceTracker(**conf_kwargs)

        def on_token(token_text: str, logprob: float) -> None:
            crossing = tracker.add_token(token_text, logprob)
            if crossing is not None:
                raise _StopStream()

        messages = self._build_messages(prompt)
        try:
            self.draft_client.stream_tokens(
                messages,
                self.config.draft.max_tokens,
                self.config.draft.temperature,
                on_token,
            )
        except _StopStream:
            pass
        return tracker

    @staticmethod
    def _build_messages(buffer_text: str) -> list[dict[str, str]]:
        from undermind.providers.base import build_draft_messages

        return build_draft_messages(buffer_text)

    # ------------------------------------------------------------------
    # responses
    # ------------------------------------------------------------------

    def _send_raw(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: dict) -> None:
        self._send_raw(200, json.dumps(data).encode("utf-8"))

    def _error(self, exc: Exception, status: int = 500) -> None:
        body = json.dumps({"error": str(exc)}).encode("utf-8")
        self._send_raw(status, body)

    def _not_found(self) -> None:
        self._send_raw(404, b'{"error": "not found"}')


class ProxyServer:
    """HTTP server sharing one pipeline across handler threads."""

    def __init__(
        self,
        host: str = PROXY_HOST,
        port: int = PROXY_PORT,
        model: Optional[str] = None,
        config: Optional[Config] = None,
    ) -> None:
        self.config = config or load_config()
        if model:
            self.config.primary.model = model
        self.host = host
        self.port = port

        self.store = UndermindStore(self.config.store.db_path)
        self.cache = PredictionCache(ttl=self.config.proxy.cache_ttl_s)
        self.draft_client = build_draft_client(self.config)
        self.primary = build_primary_provider(self.config)
        self.handoff = UniversalHandoff(
            provider=self.primary,
            provider_name=self.config.primary.kind,
            model=self.config.primary.model,
            system_prompt=self.config.primary.system_prompt,
        )
        self.predictor = DraftPredictor(
            draft_client=self.draft_client,
            confidence_kwargs={
                "threshold": self.config.confidence.threshold,
                "mode": self.config.confidence.mode,
                "ewma_alpha": self.config.confidence.ewma_alpha,
                "min_tokens": self.config.confidence.min_tokens,
                "min_chars": self.config.confidence.min_chars,
            },
        )
        self._server: Optional[ThreadingHTTPServer] = None

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler_factory())
        print(f"Undermind proxy running at http://{self.host}:{self.port}")
        print(f"Draft: {self.config.draft.model} @ {self.config.draft.base_url}")
        print(f"Primary: {self.config.primary.kind} ({self.config.primary.model})")
        print("Press Ctrl+C to stop.")
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            print("\nProxy stopped.")
        finally:
            self.store.close()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    def _handler_factory(self):
        _cache = self.cache
        _draft = self.draft_client
        _primary = self.primary
        _handoff = self.handoff
        _store = self.store
        _config = self.config
        _predictor = self.predictor

        class ProxyHandler(UndermindProxy):
            cache = _cache
            draft_client = _draft
            primary = _primary
            handoff = _handoff
            store = _store
            config = _config
            predictor = _predictor

        return ProxyHandler


def main() -> None:
    print("=" * 60)
    print("UNDERMIND PROXY")
    print(f"Listening on http://{PROXY_HOST}:{PROXY_PORT}")
    print("=" * 60)
    server = ProxyServer()
    server.start()


if __name__ == "__main__":
    main()
