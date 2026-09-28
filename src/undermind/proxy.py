"""
Proxy layer for Undermind.

Wraps the configured primary pipeline behind an Ollama-compatible HTTP API on
localhost:11435, so existing wrappers (Hermes, OpenClaw, anything speaking the
Ollama protocol) work unchanged.

OpenAI-compatible routes (/v1/models, /v1/chat/completions — JSON and SSE
streaming) are served alongside, so /v1-speaking clients (Hermes providers)
point at the same port with no shim.

The proxy also hosts the daydream miner: a background scheduler mines
recorded inputs into intents whenever the feed goes idle, so the
Hermes bridge self-sustains without a separate --daydream-once process.
/api/health exposes the whole picture for `undermind doctor`.

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
import re
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from undermind import __version__
from undermind.config import Config, load_config
from undermind.confidence import ConfidenceTracker
from undermind.daydream import DaydreamWorker
from undermind.exporter import Exporter
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

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


class DaydreamScheduler:
    """Mine recorded inputs into intents whenever the input feed goes idle.

    A daemon thread inside the proxy process. Every /api/inputs POST counts
    as activity and re-arms the idle watchdog; once the feed has been quiet
    for ``idle_threshold_s`` the worker runs one mining cycle (reusing the
    exact DaydreamWorker logic — no separate process needed).
    """

    def __init__(
        self,
        worker: DaydreamWorker,
        idle_threshold_s: float,
        poll_interval_s: float = 0.5,
    ) -> None:
        self.worker = worker
        self.idle_threshold_s = idle_threshold_s
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._last_activity_ns = time.perf_counter_ns()
        self._thread: Optional[threading.Thread] = None
        self.cycles_run = 0
        self.last_result = worker.last_result

    def notify_activity(self) -> None:
        """Re-arm the idle watchdog (call after each recorded input)."""
        self._last_activity_ns = time.perf_counter_ns()

    def idle_for(self) -> float:
        return (time.perf_counter_ns() - self._last_activity_ns) / 1e9

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="undermind-proxy-daydream", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def status(self) -> dict:
        result = self.worker.last_result
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "idle_threshold_s": self.idle_threshold_s,
            "idle_for_s": round(self.idle_for(), 1),
            "cycles_run": self.cycles_run,
            "last_result": result.to_dict() if result else None,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.idle_for() >= self.idle_threshold_s:
                try:
                    result = self.worker.run_cycle()
                    if result is not None:
                        self.cycles_run += 1  # productive mining cycles only
                        self.last_result = result
                except Exception:
                    pass  # fail-open: mining must never take the proxy down
                # Sleep a full idle window before even considering another
                # cycle; fresh activity re-arms the watchdog anyway.
                self._stop.wait(max(self.idle_threshold_s, self.poll_interval_s))
            else:
                self._stop.wait(self.poll_interval_s)


def port_in_use(host: str, port: int) -> bool:
    """True when something already accepts connections on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


class _StopStream(Exception):
    """Internal: unwinds the token stream after the threshold crossing."""


# Per-turn think routing markers (module-level so tests can shim handlers).
THINK_MARKERS = (
    "think hard",
    "think deeply",
    "reason carefully",
    "think this through",
    "deliberate",
    "step by step",
)
CRON_MARKERS = (
    "[important: you are running as a scheduled cron job",
    "[context from the interrupted assistant response",
)


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
        elif path == "/api/intents":
            self._handle_intents()
        elif path == "/api/self-intents":
            self._handle_self_intents()
        elif path == "/api/echoes":
            self._handle_echoes()
        elif path == "/api/health":
            self._handle_health()
        elif path == "/v1/models":
            self._handle_v1_models()
        else:
            self._not_found()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        # Drain the request body up front: a handler that crashes before
        # reading it leaves the client mid-send while we respond, which
        # intermittently resets the connection instead of returning the
        # intended status (observed as flaky 404 tests).
        try:
            body = self._read_body()
        except Exception:
            body = {}
        if path == "/api/generate":
            self._handle_generate(body)
        elif path == "/api/chat":
            self._handle_chat(body)
        elif path == "/api/inputs":
            self._handle_inputs(body)
        elif path == "/api/echoes":
            self._handle_echoes_post(body)
        elif path == "/v1/chat/completions":
            self._handle_v1_chat(body)
        else:
            self._not_found()

    def log_message(self, format, *args) -> None:
        pass

    # ------------------------------------------------------------------
    # daydream bridge endpoints (Hermes plugin fuel)
    # ------------------------------------------------------------------

    def _handle_inputs(self, body: dict) -> None:
        """POST {"text": ...} — record a prompt into the daydream store."""
        try:
            text = str(body.get("text", "")).strip()
            if not text:
                self._error(ValueError("empty text"), status=400)
                return
            input_id = self.store.record_input(text)
            self._record_input_activity()
            self._send_json({"ok": True, "input_id": input_id})
        except Exception as exc:
            self._error(exc, status=500)

    def _record_input_activity(self) -> None:
        """Nudge the proxy's daydream scheduler: fresh input just landed."""
        scheduler = getattr(self, "daydream_scheduler", None)
        if scheduler is not None:
            scheduler.notify_activity()

    def _handle_intents(self) -> None:
        """GET /api/intents?min_count=N — the intents Undermind has mined."""
        try:
            query = parse_qs(urlparse(self.path).query)
            min_count = int(query.get("min_count", ["1"])[0])
        except Exception:
            min_count = 1
        try:
            rows = self.store.list_intents(min_count=min_count)
        except Exception as exc:
            self._error(exc, status=500)
            return
        intents = [
            {
                "intent_id": row["intent_id"],
                "signature": row["signature"],
                "count": row["count"],
                "last_seen_ms": row["last_seen_ms"],
            }
            for row in rows[:20]
        ]
        self._send_json({"intents": intents})

    def _handle_echoes(self, query: str = "", limit: int = 3) -> None:
        """Echo retrieval: POST {q, limit} or GET /api/echoes?q=..."""
        memory = getattr(self, "memory", None)
        echoes = memory.echoes(query, limit=limit) if memory and query else []
        self._send_json({"echoes": echoes})

    def _handle_echoes_post(self, body: dict) -> None:
        query = str(body.get("q") or "")[:500]
        try:
            limit = int(body.get("limit") or 3)
        except (TypeError, ValueError):
            limit = 3
        self._handle_echoes(query, limit=max(1, min(limit, 5)))

    def _handle_self_intents(self) -> None:
        """GET /api/self-intents — Kairos's own recurring themes."""
        try:
            min_count = int(self.path.split("min_count=")[-1].split("&")[0])
        except (ValueError, IndexError):
            min_count = 1
        rows = self.store.list_assistant_intents(min_count=min_count)
        intents = [
            {
                "intent_id": row["intent_id"],
                "signature": row["signature"],
                "count": row["count"],
                "last_seen_ms": row["last_seen_ms"],
            }
            for row in rows
        ]
        self._send_json({"self_intents": intents})

    def _handle_health(self) -> None:
        """GET /api/health — one-stop status for the doctor and watchdogs."""
        scheduler = getattr(self, "daydream_scheduler", None)
        self._send_json(
            {
                "ok": True,
                "version": __version__,
                "model": self.config.primary.model,
                "draft_model": self.config.draft.model,
                "cache_entries": len(self.cache),
                "handoffs": self.store.count_handoffs(),
                "inputs": self.store.count_inputs(),
                "unprocessed_inputs": self.store.count_unprocessed(),
                "intents": len(self.store.list_intents(min_count=1)),
                "outputs": self.store.count_outputs(),
                "self_intents": len(self.store.list_assistant_intents(min_count=1)),
                "reflections": self.memory.count() if self.memory else 0,
                "daydream": scheduler.status() if scheduler else None,
                "serving": self._serving_summary(),
            }
        )

    def _serving_summary(self) -> dict:
        """Which engine has been answering, over the recent window."""
        history = list(getattr(self, "recent_serving", []) or [])
        if not history:
            return {"recent_count": 0}
        from collections import Counter

        counts = Counter(h["model"] for h in history)
        latencies = sorted(h["latency_ms"] for h in history)
        mid = len(latencies) // 2
        median_ms = (
            latencies[mid]
            if len(latencies) % 2
            else (latencies[mid - 1] + latencies[mid]) / 2
        )
        primary_model = self.config.primary.model
        return {
            "recent_count": len(history),
            "models": dict(counts),
            "median_latency_ms": round(median_ms, 1),
            "riding_fallback": primary_model not in counts
            or counts[primary_model] < len(history) / 2,
        }

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

    def _handle_generate(self, body: dict) -> None:
        prompt = str(body.get("prompt", ""))
        model = str(body.get("model", self.config.primary.model))
        self._serve_completion(prompt, model, chat_format=False)

    def _handle_chat(self, body: dict) -> None:
        prompt = self._last_user_content(body.get("messages", []))
        model = str(body.get("model", self.config.primary.model))
        self._serve_completion(prompt, model, chat_format=True)

    def _serve_completion(
        self,
        prompt: str,
        model: str,
        chat_format: bool,
        openai: bool = False,
        stream: bool = False,
    ) -> None:
        key = self._hash_prompt(prompt)
        cached = self.cache.get(key)
        if cached is not None:
            self._send_completion(
                cached,
                model,
                chat_format,
                cached=True,
                meta=None,
                openai=openai,
                stream=stream,
                prompt=prompt,
            )
            return

        try:
            outcome = self._execute_branch(prompt)
        except ProviderError as exc:
            self._send_error(exc, status=502, openai=openai)
            return
        except Exception as exc:
            self._send_error(exc, status=500, openai=openai)
            return

        self.cache.set(key, outcome["response"])
        self._send_completion(
            outcome["response"],
            model,
            chat_format,
            cached=False,
            meta=outcome,
            openai=openai,
            stream=stream,
            prompt=prompt,
        )

    def _send_completion(
        self,
        text: str,
        model: str,
        chat_format: bool,
        cached: bool,
        meta: Optional[dict],
        openai: bool = False,
        stream: bool = False,
        prompt: str = "",
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
        if openai:
            if stream:
                self._send_openai_stream(text, model)
            else:
                self._send_openai_json(text, model, undermind, prompt=prompt)
            return
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
    # OpenAI-compatible routes (Hermes providers speak /v1)
    # ------------------------------------------------------------------

    def _handle_v1_models(self) -> None:
        models = self._available_models()
        created = int(time.time())
        self._send_json(
            {
                "object": "list",
                "data": [
                    {
                        "id": m,
                        "object": "model",
                        "created": created,
                        "owned_by": "undermind",
                    }
                    for m in models
                ],
            }
        )

    def _handle_v1_chat(self, body: dict) -> None:
        messages = body.get("messages") or []
        prompt = self._last_user_content(messages)
        model = str(body.get("model") or self.config.primary.model)
        stream = bool(body.get("stream", False))
        self._serve_completion(
            prompt, model, chat_format=False, openai=True, stream=stream
        )

    def _send_openai_json(
        self, text: str, model: str, undermind: dict, prompt: str = ""
    ) -> None:
        prompt_tokens = max(1, len(prompt.split())) if prompt else 0
        completion_tokens = max(1, len(text.split())) if text else 0
        self._send_json(
            {
                "id": f"chatcmpl-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
                "undermind": undermind,
            }
        )

    def _send_openai_stream(self, text: str, model: str) -> None:
        completion_id = f"chatcmpl-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}"
        created = int(time.time())

        def event(delta: dict, finish_reason: Optional[str] = None) -> bytes:
            payload = json.dumps(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": finish_reason}
                    ],
                }
            )
            return f"data: {payload}\n\n".encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(event({"role": "assistant"}))
        for piece in re.findall(r"\s*\S+", text):
            self.wfile.write(event({"content": piece}))
        self.wfile.write(event({}, finish_reason="stop"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_openai_error(self, message: str, status: int = 500) -> None:
        body = json.dumps(
            {"error": {"message": message, "type": "undermind_proxy_error"}}
        ).encode("utf-8")
        self._send_raw(status, body)

    def _send_error(
        self, exc: Exception, status: int = 500, openai: bool = False
    ) -> None:
        if openai:
            self._send_openai_error(str(exc), status)
        else:
            self._error(exc, status)

    # ------------------------------------------------------------------
    # confidence-crossing branch execution
    # ------------------------------------------------------------------

    def _decide_think(self, prompt: str) -> tuple[bool, str]:
        """Per-turn think routing for the Ollama primary.

        Priority order (first hit wins):
        1. explicit "think hard"-style request -> on
        2. cron / auxiliary envelope -> off (they were never asked to think)
        3. config default (adaptive_think off, or think on) -> config value
        4. adaptive: turn matching a matured routine intent (count >=
           routine_threshold) -> off; novel -> on (only new ground pays
           for deliberation).
        Returns (think, reason) for the handoff log.
        """
        lowered = (prompt or "").lower()
        if any(m in lowered for m in THINK_MARKERS):
            return True, "explicit_request"
        if any(m in lowered for m in CRON_MARKERS):
            return False, "auxiliary_turn"
        cfg = self.config.primary
        if not bool(getattr(cfg, "adaptive_think", False)):
            return bool(cfg.think), "config_default"
        # Adaptive: semantic match against matured intents (Memory Mine).
        # Embedding distance decides routine-vs-novel by meaning; jaccard is
        # the fallback when the memory backend is unavailable. Both fail-open
        # to the config default.
        routine_similarity = float(getattr(cfg, "routine_similarity", 0.72))
        memory = getattr(self, "memory", None)
        if memory is not None and memory.available:
            try:
                match = memory.gate_match(prompt, min_score=routine_similarity)
                if match:
                    return False, "routine_match"
                return True, "adaptive_novel"
            except Exception:
                pass  # fall through to jaccard fallback
        try:
            from undermind.intents import find_similar_intent, signature

            sig = signature(prompt)
            if not sig:
                return True, "adaptive_novel"  # nothing to match: deliberate
            matured = [
                (r["intent_id"], r["signature"])
                for r in self.store.list_intents(min_count=1)
                if r["count"] >= int(getattr(cfg, "routine_threshold", 3))
            ]
            if matured and find_similar_intent(sig, matured, 0.6) is not None:
                return False, "routine_match"
            return True, "adaptive_novel"
        except Exception:
            return bool(cfg.think), "config_default"  # fail-open to config

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

        think_used, think_reason = self._decide_think(prompt)
        try:
            result = self.primary.execute(
                branch,
                context=self.config.primary.system_prompt or None,
                think=think_used,
            )
        except TypeError:
            # Provider without per-call override (openai_compat, webhook,
            # Noop, fakes): execute without the kwarg.
            result = self.primary.execute(
                branch, context=self.config.primary.system_prompt or None
            )
        latency_ms = (time.perf_counter_ns() - start_ns) / 1e6

        # Attribute the response to the engine that actually served it.
        served_index = 0
        served_latency_ms: Optional[float] = None
        fallback = getattr(self.primary, "last_served", None)
        if fallback is not None:
            served_index = int(fallback)
            served_latency_ms = getattr(self.primary, "last_latency_ms", None)
        served_provider, served_model = self._served_engine(served_index)
        self._note_serving(
            served_provider, served_model, served_latency_ms or latency_ms
        )

        self.store.record_handoff(
            trigger="proxy_confidence_cross" if crossed else "proxy_direct",
            branch=branch,
            provider=served_provider,
            model=served_model,
            status="ok",
            confidence=confidence,
            latency_ms=latency_ms,
            think_used=think_used,
            think_reason=think_reason,
        )
        try:
            # Self-mirror: capture what Kairos actually said, for its own
            # mined themes. Fail-open; never blocks the reply.
            self.store.record_output(result)
        except Exception:
            pass
        return {
            "response": result,
            "crossed": crossed,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "served_index": served_index,
            "served_provider": served_provider,
            "served_model": served_model,
            "served_latency_ms": served_latency_ms,
        }

    _KIND_BY_CLASS = {
        "OllamaNativeProvider": "ollama",
        "OpenAICompatProvider": "openai_compat",
        "NoopProvider": "noop",
    }

    def _served_engine(self, served_index: int) -> tuple[str, str]:
        """(kind, model) of the chain rung that actually answered."""
        chain = getattr(self.primary, "providers", None)
        if chain:
            rung = chain[min(served_index, len(chain) - 1)]
            cls_name = type(rung).__name__
            kind = getattr(rung, "provider_kind", None) or self._KIND_BY_CLASS.get(
                cls_name, cls_name
            )
            model = getattr(rung, "model", "") or self.config.primary.model
            return str(kind), str(model)
        return self.config.primary.kind, self.config.primary.model

    def _note_serving(
        self, provider: str, model: str, latency_ms: float
    ) -> None:
        """Keep a short history so /api/health can expose the serving mix."""
        history = getattr(self, "recent_serving", None)
        if history is not None:
            history.append(
                {
                    "ts": time.time(),
                    "provider": provider,
                    "model": model,
                    "latency_ms": round(latency_ms, 1),
                }
            )

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
        # Memory Mine (semantic recall over the self-mirror). Optional:
        # None on any failure so the stack runs without it.
        try:
            from pathlib import Path as _Path

            from undermind.memory import MemoryStore

            self.memory = MemoryStore(
                _Path(self.config.store.db_path).parent / "memory_lance"
            )
        except Exception:
            self.memory = None
        self.draft_client = build_draft_client(self.config)
        from undermind.providers.fallback import build_primary_chain

        self.primary, self.chain_desc = build_primary_chain(self.config)
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
        self.exporter = Exporter(self.store, self.config.daydream.export_path)
        self.daydream = DaydreamWorker(
            store=self.store,
            exporter=self.exporter,
            idle_threshold_s=self.config.daydream.idle_threshold_s,
            min_intent_count=self.config.daydream.min_intent_count,
            max_samples_per_intent=self.config.daydream.max_samples_per_intent,
            merge_similarity=self.config.daydream.merge_similarity,
            memory_store=self.memory,
            routine_threshold=int(
                getattr(self.config.primary, "routine_threshold", 3)
            ),
        )
        self.daydream_scheduler = DaydreamScheduler(
            worker=self.daydream,
            idle_threshold_s=self.config.daydream.idle_threshold_s,
            poll_interval_s=self.config.daydream.poll_interval_s,
        )
        self.recent_serving: deque = deque(maxlen=30)
        self._server: Optional[ThreadingHTTPServer] = None

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler_factory())
        self.daydream_scheduler.start()
        print(f"Undermind proxy running at http://{self.host}:{self.port}")
        print(f"Draft: {self.config.draft.model} @ {self.config.draft.base_url}")
        print(f"Primary chain: {self.chain_desc}")
        print(
            "Daydream: auto-mining on idle "
            f"(threshold {self.config.daydream.idle_threshold_s:.0f}s)"
        )
        print("Press Ctrl+C to stop.")
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            print("\nProxy stopped.")
        finally:
            self.daydream_scheduler.stop()
            self.store.close()

    def run_forever(self) -> int:
        """Serve; if the port is already bound, exit 0 so watchdogs stay calm.

        A scheduled task fires every few minutes and at logon. When a healthy
        proxy is already listening, this instance exits successfully instead
        of crash-looping — that is what makes the watchdog hands-off.
        """
        if port_in_use(self.host, self.port):
            print(
                f"Port {self.port} already serving — another proxy owns it. "
                "Exiting quietly (watchdog no-op)."
            )
            self.store.close()
            return 0
        self.start()
        return 0

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        self.daydream_scheduler.stop()

    def _handler_factory(self):
        _cache = self.cache
        _draft = self.draft_client
        _primary = self.primary
        _handoff = self.handoff
        _store = self.store
        _config = self.config
        _predictor = self.predictor
        _scheduler = self.daydream_scheduler
        _recent_serving = self.recent_serving
        _memory = self.memory

        class ProxyHandler(UndermindProxy):
            cache = _cache
            draft_client = _draft
            primary = _primary
            handoff = _handoff
            store = _store
            config = _config
            predictor = _predictor
            daydream_scheduler = _scheduler
            recent_serving = _recent_serving
            memory = _memory

        return ProxyHandler


def main() -> None:
    print("=" * 60)
    print("UNDERMIND PROXY")
    print(f"Listening on http://{PROXY_HOST}:{PROXY_PORT}")
    print("=" * 60)
    server = ProxyServer()
    raise SystemExit(server.run_forever())


if __name__ == "__main__":
    main()
