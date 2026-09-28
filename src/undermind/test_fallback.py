"""Tests for the fail-open FallbackProvider chain."""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from undermind.config import Config, PrimaryConfig
from undermind.providers.base import PrimaryProvider, ProviderError
from undermind.providers.fallback import (
    FallbackProvider,
    _derive_fallback_config,
    build_primary_chain,
)
from undermind.providers.ollama_native import OllamaNativeProvider
from undermind.providers.openai_compat import OpenAICompatProvider


class _Stub(PrimaryProvider):
    def __init__(self, name: str, fail: bool = False, reply: str = "") -> None:
        self.name = name
        self.fail = fail
        self.reply = reply or f"ok-{name}"
        self.calls: list[str] = []

    def execute(self, branch: str, context: str | None = None) -> str:
        self.calls.append(branch)
        if self.fail:
            raise ProviderError(f"{self.name} is down")
        return self.reply


class FallbackProviderTests(unittest.TestCase):
    def test_first_provider_answers(self) -> None:
        chain = FallbackProvider([_Stub("a"), _Stub("b")])
        self.assertEqual(chain.execute("hi"), "ok-a")
        self.assertEqual(chain.calls if hasattr(chain, "calls") else [], [])

    def test_falls_through_on_failure(self) -> None:
        primary = _Stub("primary", fail=True)
        fallback = _Stub("fallback")
        chain = FallbackProvider([primary, fallback])
        self.assertEqual(chain.execute("hi"), "ok-fallback")
        self.assertEqual(primary.calls, ["hi"])
        self.assertEqual(fallback.calls, ["hi"])

    def test_raises_when_chain_exhausted(self) -> None:
        chain = FallbackProvider([_Stub("a", fail=True), _Stub("b", fail=True)])
        with self.assertRaises(ProviderError):
            chain.execute("hi")

    def test_empty_chain_rejected(self) -> None:
        with self.assertRaises(ProviderError):
            FallbackProvider([])

    def test_context_forwarded(self) -> None:
        stub = _Stub("a")
        chain = FallbackProvider([stub])
        chain.execute("hi", context="sys")
        self.assertEqual(stub.calls, ["hi"])

    def test_chain_summary(self) -> None:
        chain = FallbackProvider([_Stub("a"), _Stub("b")])
        self.assertEqual(chain.chain_summary, "_Stub -> _Stub")


class BuildPrimaryChainTests(unittest.TestCase):
    def _config(self, **primary_kwargs) -> Config:
        config = Config()
        config.primary = PrimaryConfig(kind="ollama", model="big:latest", **primary_kwargs)
        return config

    def test_no_fallback_returns_plain_provider(self) -> None:
        provider, desc = build_primary_chain(self._config())
        self.assertNotIsInstance(provider, FallbackProvider)
        self.assertEqual(desc, "ollama:big:latest")

    def test_fallback_config_derived(self) -> None:
        config = self._config(
            fallback_kind="openai_compat",
            fallback_base_url="http://localhost:13305",
            fallback_model="Bonsai-4B-Q1_0",
        )
        fb = _derive_fallback_config(config, "openai_compat")
        self.assertEqual(fb.kind, "openai_compat")
        self.assertEqual(fb.base_url, "http://localhost:13305")
        self.assertEqual(fb.model, "Bonsai-4B-Q1_0")
        # system prompt and other fields carry over
        self.assertEqual(fb.system_prompt, config.primary.system_prompt)

    def test_fallback_kind_without_fields_uses_primary_base(self) -> None:
        config = self._config(fallback_kind="ollama", fallback_model="small:latest")
        fb = _derive_fallback_config(config, "ollama")
        self.assertEqual(fb.base_url, config.primary.base_url)
        self.assertEqual(fb.model, "small:latest")

    def test_chain_built_with_fallback(self) -> None:
        config = self._config(
            fallback_kind="openai_compat",
            fallback_base_url="http://localhost:13305",
            fallback_model="Bonsai-4B-Q1_0",
        )
        provider, desc = build_primary_chain(config)
        self.assertIsInstance(provider, FallbackProvider)
        self.assertIn("->", desc)
        self.assertIn("Bonsai-4B-Q1_0", desc)

    def test_derive_tolerates_bare_primary_config(self) -> None:
        # Defensive: a bare PrimaryConfig (no wrapping Config) must not crash.
        fb = _derive_fallback_config(
            PrimaryConfig(kind="ollama", model="big:latest", fallback_kind="ollama"),
            "ollama",
        )
        self.assertEqual(fb.kind, "ollama")


from undermind.providers.fallback import FallbackProvider


class _Ok:
    def __init__(self, tag=""):
        self.tag = tag

    def execute(self, branch, context=None):
        return f"ok:{self.tag}"


class _Boom:
    def execute(self, branch, context=None):
        raise ProviderError("down")


class TestServedAttribution(unittest.TestCase):
    def test_last_served_primary(self):
        chain = FallbackProvider([_Ok("p1"), _Ok("p2")])
        self.assertEqual(chain.execute("x"), "ok:p1")
        self.assertEqual(chain.last_served, 0)
        self.assertIsNotNone(chain.last_latency_ms)

    def test_last_served_fallback(self):
        chain = FallbackProvider([_Boom(), _Ok("p2")])
        self.assertEqual(chain.execute("x"), "ok:p2")
        self.assertEqual(chain.last_served, 1)
        self.assertIsNotNone(chain.last_latency_ms)

    def test_last_served_none_on_total_failure(self):
        chain = FallbackProvider([_Boom(), _Boom()])
        with self.assertRaises(ProviderError):
            chain.execute("x")
        self.assertIsNone(chain.last_served)
        self.assertIsNone(chain.last_latency_ms)


class _Hung(PrimaryProvider):
    """Rung that never answers (simulates a wedged engine)."""

    def __init__(self, name: str = "hung") -> None:
        self.name = name
        self.completed = False

    def execute(self, branch: str, context: str | None = None) -> str:
        time.sleep(30)
        self.completed = True
        return f"ok-{self.name}"


class _Slow(PrimaryProvider):
    def __init__(self, name: str, delay_s: float) -> None:
        self.name = name
        self.delay_s = delay_s

    def execute(self, branch: str, context: str | None = None) -> str:
        time.sleep(self.delay_s)
        return f"ok-{self.name}"


class TestPerRungTimeouts(unittest.TestCase):
    """A hung rung must be abandoned quickly, not stall the turn."""

    def test_hung_primary_fails_over_quickly(self):
        chain = FallbackProvider(
            [_Hung("primary"), _Stub("fallback")], per_provider_timeout_s=0.5
        )
        start = time.monotonic()
        result = chain.execute("hello")
        elapsed = time.monotonic() - start
        self.assertEqual(result, "ok-fallback")
        self.assertEqual(chain.last_served, 1)
        self.assertLess(elapsed, 5.0, "failover should take well under 5s")

    def test_caps_apply_per_position(self):
        chain = FallbackProvider(
            [_Hung("p0"), _Slow("p1", 0.8), _Stub("p2")],
            per_provider_timeout_s=(0.4, 3.0, 3.0),
        )
        start = time.monotonic()
        result = chain.execute("hello")
        elapsed = time.monotonic() - start
        self.assertEqual(result, "ok-p1")
        self.assertEqual(chain.last_served, 1)
        self.assertLess(elapsed, 5.0)

    def test_none_cap_leaves_rung_uncapped(self):
        # A 0.6s rung with a None cap must finish; a 0.2s cap would kill it.
        chain = FallbackProvider(
            [_Slow("p0", 0.6), _Stub("p1")], per_provider_timeout_s=(None, 0.2)
        )
        self.assertEqual(chain.execute("hello"), "ok-p0")
        self.assertEqual(chain.last_served, 0)

    def test_tail_rungs_inherit_last_cap(self):
        chain = FallbackProvider(
            [_Hung("p0"), _Hung("p1"), _Stub("p2")],
            per_provider_timeout_s=(0.3, 0.3),
        )
        start = time.monotonic()
        self.assertEqual(chain.execute("hello"), "ok-p2")
        self.assertLess(time.monotonic() - start, 5.0)

    def test_worker_abandoned_not_joined(self):
        hung = _Hung()
        chain = FallbackProvider([hung, _Stub("fb")], per_provider_timeout_s=0.3)
        self.assertEqual(chain.execute("hello"), "ok-fb")
        # The chain returned while the worker was still stuck: it was
        # abandoned, not joined (a join would have blocked 30s here).
        self.assertFalse(hung.completed)

    def test_chain_reusable_after_timeout(self):
        chain = FallbackProvider(
            [_Hung("p0"), _Stub("fb")], per_provider_timeout_s=0.3
        )
        self.assertEqual(chain.execute("a"), "ok-fb")
        self.assertEqual(chain.execute("b"), "ok-fb")
        self.assertEqual(chain.last_served, 1)


class _StallHandler(BaseHTTPRequestHandler):
    """Shared streaming test server: a body containing STALL wedges mid-stream,
    otherwise it streams a healthy two-token reply. The wedge keeps the socket
    open and silent, exactly like a hung engine."""

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def log_message(self, *args):
        pass


class _OpenAIStallHandler(_StallHandler):
    def do_POST(self):
        body = self._read_body()
        if b'"stream": false' in body:
            # Plain non-streaming completion (stall detection disabled).
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"choices": [{"message": {"content": "hello world"}}]}
                ).encode()
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def chunk(text):
            payload = json.dumps(
                {"choices": [{"delta": {"content": text}}]}
            )
            self.wfile.write(f"data: {payload}\n\n".encode())
            self.wfile.flush()

        if b"STALL" in body:
            chunk("partial")
            threading.Event().wait()  # wedge forever, socket stays open
        chunk("hello ")
        chunk("world")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class _OllamaStallHandler(_StallHandler):
    def do_POST(self):
        body = self._read_body()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()

        def line(obj):
            self.wfile.write(json.dumps(obj).encode() + b"\n")
            self.wfile.flush()

        if b"STALL" in body:
            line({"response": "partial"})
            threading.Event().wait()
        line({"response": "hello"})
        line({"response": " world"})
        line({"done": True})


class TestStallGuard(unittest.TestCase):
    """execute() must trip on a silent stream instead of the whole turn."""

    OPENAI_PORT = 11445
    OLLAMA_PORT = 11446

    @classmethod
    def setUpClass(cls):
        cls.servers = []
        for port, handler in (
            (cls.OPENAI_PORT, _OpenAIStallHandler),
            (cls.OLLAMA_PORT, _OllamaStallHandler),
        ):
            httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            cls.servers.append(httpd)

    @classmethod
    def tearDownClass(cls):
        for httpd in cls.servers:
            httpd.shutdown()
            httpd.server_close()

    def test_openai_execute_trips_on_silent_stream(self):
        provider = OpenAICompatProvider(
            base_url=f"http://127.0.0.1:{self.OPENAI_PORT}",
            model="m",
            timeout_s=60.0,
            stall_timeout_s=1.0,
        )
        start = time.monotonic()
        with self.assertRaises(ProviderError):
            provider.execute("STALL please")
        self.assertLess(time.monotonic() - start, 15.0)

    def test_openai_execute_streams_ok_when_alive(self):
        provider = OpenAICompatProvider(
            base_url=f"http://127.0.0.1:{self.OPENAI_PORT}",
            model="m",
            timeout_s=60.0,
            stall_timeout_s=5.0,
        )
        self.assertEqual(provider.execute("ok"), "hello world")

    def test_ollama_execute_trips_on_silent_stream(self):
        provider = OllamaNativeProvider(
            base_url=f"http://127.0.0.1:{self.OLLAMA_PORT}",
            model="m",
            timeout_s=60.0,
            stall_timeout_s=1.0,
        )
        start = time.monotonic()
        with self.assertRaises(ProviderError):
            provider.execute("STALL please")
        self.assertLess(time.monotonic() - start, 15.0)

    def test_ollama_execute_streams_ok_when_alive(self):
        provider = OllamaNativeProvider(
            base_url=f"http://127.0.0.1:{self.OLLAMA_PORT}",
            model="m",
            timeout_s=60.0,
            stall_timeout_s=5.0,
        )
        self.assertEqual(provider.execute("ok"), "hello world")

    def test_think_flag_reaches_payload(self):
        provider = OllamaNativeProvider(
            base_url="http://127.0.0.1:1/",
            model="m",
            timeout_s=5.0,
            think=False,
        )
        payload = provider._payload("p", stream=True)
        self.assertIs(payload["think"], False)
        self.assertNotIn("options", payload)  # stall disabled -> no logprobs
        plain = provider._payload("p", stream=False)
        self.assertIs(plain["think"], False)

        # With the stall guard on, the streaming payload keeps logprobs.
        guarded = OllamaNativeProvider(
            base_url="http://127.0.0.1:1/",
            model="m",
            stall_timeout_s=5.0,
            think=False,
        )
        self.assertIn("options", guarded._payload("p", stream=True))

        deep = OllamaNativeProvider(base_url="http://127.0.0.1:1/", model="m")
        self.assertIs(deep._payload("p", stream=False)["think"], True)

    def test_stall_disabled_keeps_plain_request_path(self):
        provider = OpenAICompatProvider(
            base_url=f"http://127.0.0.1:{self.OPENAI_PORT}",
            model="m",
            timeout_s=60.0,
            stall_timeout_s=0.0,
        )
        self.assertEqual(provider.execute("ok"), "hello world")


class TestChainWiring(unittest.TestCase):
    """build_primary_chain must wire caps and stall guards from config."""

    def _config(self):
        config = Config()
        config.primary.kind = "ollama"
        config.primary.base_url = "http://localhost:11434"
        config.primary.model = "big:latest"
        config.primary.timeout_s = 180.0
        config.primary.fallback_kind = "openai_compat"
        config.primary.fallback_base_url = "http://localhost:13305"
        config.primary.fallback_model = "Bonsai-4B-Q1_0"
        config.primary.fallback_timeout_s = 60.0
        config.primary.stall_timeout_s = 90.0
        return config

    def test_caps_and_stall_flow_through(self):
        chain, desc = build_primary_chain(self._config())
        self.assertIsInstance(chain, FallbackProvider)
        self.assertEqual(chain.per_provider_timeout_s, (180.0, 60.0))
        self.assertEqual(chain.stall_timeout_s, 90.0)
        self.assertEqual(chain.providers[0].stall_timeout_s, 90.0)
        self.assertEqual(chain.providers[1].timeout_s, 60.0)
        self.assertEqual(chain.providers[1].stall_timeout_s, 90.0)

    def test_fallback_rung_socket_cap_capped_at_60(self):
        config = self._config()
        config.primary.timeout_s = 180.0
        derived = _derive_fallback_config(config, "openai_compat")
        self.assertEqual(derived.timeout_s, 60.0)

        config.primary.timeout_s = 30.0
        derived = _derive_fallback_config(config, "openai_compat")
        self.assertEqual(derived.timeout_s, 30.0)

    def test_stall_defaults_when_unset(self):
        config = self._config()
        config.primary.stall_timeout_s = 0.0
        derived = _derive_fallback_config(config, "openai_compat")
        self.assertEqual(derived.stall_timeout_s, 30.0)


if __name__ == "__main__":
    unittest.main()
