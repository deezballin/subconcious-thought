"""
Unit tests for the Undermind proxy layer.

Integration tests run a real HTTP server on dedicated ports but use an
in-process fake draft client, so they never require a live model backend.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind.config import load_config
from undermind.proxy import PredictionCache, ProxyServer, PROXY_HOST
from undermind.providers.base import ProviderError


class FakeDraftClient:
    """Deterministic draft stream: confident when the prompt mentions 'capital'."""

    def __init__(self, crossing_logprob=-0.001, plain_logprob=-0.9):
        self.crossing_logprob = crossing_logprob
        self.plain_logprob = plain_logprob

    def stream_tokens(self, messages, max_tokens, temperature, on_token):
        prompt = messages[-1]["content"]
        if "capital" in prompt.lower():
            tokens = [(" Paris", self.crossing_logprob), (" is", self.crossing_logprob)]
        else:
            tokens = [(" some", self.plain_logprob), (" words", self.plain_logprob)]
        for text, logprob in tokens:
            on_token(text, logprob)
        return "".join(text for text, _ in tokens)

    def list_models(self):
        return ["FakeDraft-9B"]


class FakePrimary:
    """Echoes the branch so tests can assert exactly what was executed."""

    def execute(self, branch, context=None):
        return f"ECHO::{branch}"


class TestPredictionCache(unittest.TestCase):
    def test_set_get(self):
        cache = PredictionCache(ttl=10)
        cache.set("hello", "world")
        self.assertEqual(cache.get("hello"), "world")

    def test_miss(self):
        cache = PredictionCache(ttl=10)
        self.assertIsNone(cache.get("missing"))

    def test_ttl_expiry(self):
        cache = PredictionCache(ttl=0.1)
        cache.set("hello", "world")
        self.assertEqual(cache.get("hello"), "world")
        time.sleep(0.15)
        self.assertIsNone(cache.get("hello"))

    def test_clear(self):
        cache = PredictionCache(ttl=10)
        cache.set("hello", "world")
        cache.clear()
        self.assertIsNone(cache.get("hello"))


def _make_server(port: int, tmpdir: str) -> ProxyServer:
    # Hermetic: build defaults directly so a repo-root config.toml (e.g. the
    # live Hermes bridge seating) never leaks into the test suite.
    from undermind.config import Config

    config = Config()
    config.store.db_path = os.path.join(tmpdir, f"proxy-{port}.db")
    config.confidence.min_tokens = 1
    config.confidence.min_chars = 0
    server = ProxyServer(host=PROXY_HOST, port=port, config=config)
    server.draft_client = FakeDraftClient()
    server.primary = FakePrimary()
    handler = server._handler_factory()
    server._server = ThreadingHTTPServer((server.host, port), handler)
    return server


class TestProxyIntegration(unittest.TestCase):
    """Real HTTP server with fake providers on dedicated ports."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = _make_server(11440, cls.tmp.name)
        cls.thread = threading.Thread(
            target=cls.server._server.serve_forever, daemon=True
        )
        cls.thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server._server.shutdown()
        cls.server._server.server_close()
        cls.server.store.close()
        cls.tmp.cleanup()

    def _post(self, path: str, payload: dict, timeout: int = 30) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:11440{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode())

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(
            f"http://127.0.0.1:11440{path}", timeout=20
        ) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode())

    def test_tags_lists_models(self):
        data = self._get("/api/tags")
        self.assertIn("models", data)
        self.assertIn("FakeDraft-9B", [m["name"] for m in data["models"]])

    def test_generate_crosses_and_executes_branch(self):
        data = self._post(
            "/api/generate",
            {"model": "FakePrimary", "prompt": "The capital of France is", "stream": False},
        )
        self.assertIn("response", data)
        # " Paris" is the first token above threshold, so the stream stops
        # there and the executed branch is prompt + that single token.
        self.assertEqual(data["response"], "ECHO::The capital of France is Paris")
        self.assertTrue(data["undermind"]["confidence_crossed"])
        self.assertGreaterEqual(data["undermind"]["confidence"], 0.95)

    def test_generate_without_crossing_uses_raw_prompt(self):
        data = self._post(
            "/api/generate",
            {"model": "FakePrimary", "prompt": "hello world", "stream": False},
        )
        self.assertEqual(data["response"], "ECHO::hello world")
        self.assertFalse(data["undermind"]["confidence_crossed"])

    def test_cache_hit(self):
        payload = {"model": "FakePrimary", "prompt": "cache me", "stream": False}
        first = self._post("/api/generate", payload)
        second = self._post("/api/generate", payload)
        self.assertEqual(first["response"], second["response"])
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])

    def test_chat_endpoint(self):
        data = self._post(
            "/api/chat",
            {
                "model": "FakePrimary",
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "The capital of Italy is"},
                ],
                "stream": False,
            },
        )
        self.assertEqual(data["message"]["role"], "assistant")
        self.assertEqual(data["message"]["content"], "ECHO::The capital of Italy is Paris")

    def test_handoffs_recorded(self):
        before = self.server.store.count_handoffs()
        self._post("/api/generate", {"model": "m", "prompt": "record me", "stream": False})
        self.assertEqual(self.server.store.count_handoffs(), before + 1)

    def test_unknown_endpoint_404(self):
        request = urllib.request.Request(
            "http://127.0.0.1:11440/api/bogus",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=20)
        self.assertEqual(ctx.exception.code, 404)


class TestProxyPrimaryFailure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = _make_server(11441, cls.tmp.name)

        class BrokenPrimary:
            def execute(self, branch, context=None):
                raise ProviderError("primary down")

        cls.server.primary = BrokenPrimary()
        handler = cls.server._handler_factory()
        cls.server._server = ThreadingHTTPServer((cls.server.host, 11441), handler)
        cls.thread = threading.Thread(
            target=cls.server._server.serve_forever, daemon=True
        )
        cls.thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server._server.shutdown()
        cls.server._server.server_close()
        cls.server.store.close()
        cls.tmp.cleanup()

    def test_primary_failure_maps_to_502(self):
        request = urllib.request.Request(
            "http://127.0.0.1:11441/api/generate",
            data=json.dumps({"model": "m", "prompt": "hello"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 502)


class TestProxyOpenAIEndpoints(unittest.TestCase):
    """OpenAI-compatible routes: /v1/models + /v1/chat/completions (JSON/SSE)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = _make_server(11442, cls.tmp.name)
        cls.thread = threading.Thread(
            target=cls.server._server.serve_forever, daemon=True
        )
        cls.thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server._server.shutdown()
        cls.server._server.server_close()
        cls.server.store.close()
        cls.tmp.cleanup()

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:11442{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode())

    def test_v1_models_lists_openai_shape(self):
        with urllib.request.urlopen(
            "http://127.0.0.1:11442/v1/models", timeout=20
        ) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode())
        self.assertEqual(data["object"], "list")
        first = data["data"][0]
        for field in ("id", "object", "created", "owned_by"):
            self.assertIn(field, first)
        self.assertEqual(first["object"], "model")
        self.assertIn("FakeDraft-9B", [m["id"] for m in data["data"]])

    def test_v1_chat_completion_json(self):
        data = self._post(
            "/v1/chat/completions",
            {
                "model": "whatever",
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "The capital of Spain is"},
                ],
            },
        )
        self.assertEqual(data["object"], "chat.completion")
        choice = data["choices"][0]
        self.assertEqual(choice["message"]["role"], "assistant")
        # FakeDraftClient streams " Paris" for any "capital" prompt.
        self.assertEqual(choice["message"]["content"], "ECHO::The capital of Spain is Paris")
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertIn("undermind", data)
        self.assertTrue(data["undermind"]["confidence_crossed"])
        self.assertGreaterEqual(data["usage"]["total_tokens"], 2)

    def test_v1_chat_completion_echoes_model_and_cached(self):
        payload = {
            "model": "my-model",
            "messages": [{"role": "user", "content": "hello v1"}],
        }
        first = self._post("/v1/chat/completions", payload)
        second = self._post("/v1/chat/completions", payload)
        self.assertEqual(first["model"], "my-model")
        self.assertEqual(first["choices"][0]["message"]["content"], "ECHO::hello v1")
        self.assertFalse(first["undermind"]["cached"])
        self.assertTrue(second["undermind"]["cached"])

    def test_v1_chat_completion_stream(self):
        request = urllib.request.Request(
            "http://127.0.0.1:11442/v1/chat/completions",
            data=json.dumps(
                {
                    "model": "my-model",
                    "messages": [{"role": "user", "content": "stream this please"}],
                    "stream": True,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Content-Type"), "text/event-stream")
            body = response.read().decode()
        events = [
            line[len("data: ") :]
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events[-1], "[DONE]")
        chunks = [json.loads(e) for e in events[:-1]]
        self.assertTrue(all(c["object"] == "chat.completion.chunk" for c in chunks))
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        content = "".join(
            c["choices"][0]["delta"].get("content", "") for c in chunks[1:-1]
        )
        self.assertEqual(content, "ECHO::stream this please")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(content.startswith("ECHO::stream"))

    def test_v1_bogus_path_404(self):
        request = urllib.request.Request(
            "http://127.0.0.1:11442/v1/bogus",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=20)
        self.assertEqual(ctx.exception.code, 404)


from collections import deque

from undermind.providers.fallback import FallbackProvider
from undermind.proxy import UndermindProxy


class _Rung:
    def __init__(self, tag, model):
        self.tag = tag
        self.model = model

    def execute(self, branch, context=None):
        return f"ok:{self.tag}"


class _DeadRung:
    model = "dead-rung"

    def execute(self, branch, context=None):
        raise ProviderError("rung down")


class TestServedAttribution(unittest.TestCase):
    """Serving must be credited to the chain rung that actually answered."""

    ATTR_PORT = 11444

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._running = []

    def tearDown(self):
        for httpd, server in self._running:
            httpd.shutdown()
            httpd.server_close()
            try:
                server.store.close()
            except Exception:
                pass
        self.tmp.cleanup()

    def _running_server(self, primary) -> ProxyServer:
        from undermind.config import Config

        config = Config()
        config.store.db_path = os.path.join(self.tmp.name, "attr.db")
        config.confidence.min_tokens = 1
        config.confidence.min_chars = 0
        server = ProxyServer(host=PROXY_HOST, port=self.ATTR_PORT, config=config)
        server.draft_client = FakeDraftClient()
        server.primary = primary  # captured by the handler factory below
        httpd = ThreadingHTTPServer(
            (PROXY_HOST, self.ATTR_PORT), server._handler_factory()
        )
        server._server = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self._running.append((httpd, server))
        return server

    def _generate(self, prompt="hello world") -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.ATTR_PORT}/api/generate",
            data=json.dumps(
                {"model": "undermind-bridge", "prompt": prompt, "stream": False}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    def test_plain_primary_reports_config_engine(self):
        server = self._running_server(FakePrimary())
        self._generate()
        self.assertEqual(
            server.recent_serving[0]["model"], server.config.primary.model
        )

    def test_chain_primary_win_attributed(self):
        chain = FallbackProvider(
            [_Rung("p0", "bonsai-27b-1bit:latest"), _Rung("p1", "Bonsai-4B-Q1_0")]
        )
        server = self._running_server(chain)
        self._generate()
        self.assertEqual(chain.last_served, 0)
        self.assertEqual(
            server.recent_serving[0]["model"], "bonsai-27b-1bit:latest"
        )

    def test_chain_fallback_rung_attributed(self):
        chain = FallbackProvider(
            [_DeadRung(), _DeadRung(), _Rung("p2", "Bonsai-4B-Q1_0")]
        )
        server = self._running_server(chain)
        self._generate()
        self.assertEqual(chain.last_served, 2)
        self.assertEqual(server.recent_serving[0]["model"], "Bonsai-4B-Q1_0")

    def test_serving_summary_flags_riding_fallback(self):
        from types import SimpleNamespace

        from undermind.config import Config

        history = deque(maxlen=30)
        shim = SimpleNamespace(recent_serving=history, config=Config())
        primary = shim.config.primary.model
        fb = "Bonsai-4B-Q1_0"
        for model, lat in ((fb, 100.0), (fb, 200.0), (fb, 300.0), (primary, 400.0)):
            history.append(
                {"ts": time.time(), "provider": "x", "model": model, "latency_ms": lat}
            )
        summary = UndermindProxy._serving_summary(shim)
        self.assertEqual(summary["recent_count"], 4)
        self.assertTrue(summary["riding_fallback"])
        self.assertEqual(summary["median_latency_ms"], 250.0)

        history.clear()
        for model in (primary, primary, primary, fb):
            history.append(
                {"ts": time.time(), "provider": "x", "model": model, "latency_ms": 50.0}
            )
        self.assertFalse(UndermindProxy._serving_summary(shim)["riding_fallback"])


class TestThinkRouting(unittest.TestCase):
    """_decide_think: per-turn think routing, first hit wins.

    The decision lives on the request handler (next to _execute_branch);
    tests drive it through a lightweight shim carrying config + store.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from undermind.config import Config

        self.config = Config()
        self.config.store.db_path = os.path.join(self.tmp.name, "think.db")
        server = ProxyServer(host=PROXY_HOST, port=11447, config=self.config)
        self.store = server.store
        self._handler_cls = server._handler_factory()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _decide(self, prompt):
        from types import SimpleNamespace

        shim = SimpleNamespace(config=self.config, store=self.store)
        return self._handler_cls._decide_think(shim, prompt)

    def _seed_routine(self, text, count):
        from undermind.intents import signature

        iid = f"routine-{abs(hash(text)) % 99999}"
        self.store._conn.execute(
            "INSERT OR REPLACE INTO intents (intent_id, signature, first_seen_ms,"
            " last_seen_ms, count) VALUES (?, ?, 1, 1, ?)",
            (iid, signature(text), count),
        )
        self.store._conn.commit()

    def test_explicit_request_wins(self):
        think, reason = self._decide("please think hard about this")
        self.assertTrue(think)
        self.assertEqual(reason, "explicit_request")

    def test_cron_envelope_never_thinks(self):
        think, reason = self._decide(
            "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: x]"
        )
        self.assertFalse(think)
        self.assertEqual(reason, "auxiliary_turn")

    def test_config_default_when_adaptive_off(self):
        self.config.primary.think = True
        self.config.primary.adaptive_think = False
        think, reason = self._decide("a plain question")
        self.assertTrue(think)
        self.assertEqual(reason, "config_default")

    def test_adaptive_routine_match_stays_off(self):
        self.config.primary.think = False
        self.config.primary.adaptive_think = True
        self.config.primary.routine_threshold = 3
        self._seed_routine("fix the login bug", 6)
        think, reason = self._decide("can you fix the login bug")
        self.assertFalse(think)
        self.assertEqual(reason, "routine_match")

    def test_adaptive_novel_thinks(self):
        self.config.primary.think = False
        self.config.primary.adaptive_think = True
        self._seed_routine("fix the login bug", 6)
        think, reason = self._decide(
            "design a schema for tracking bird migrations"
        )
        self.assertTrue(think)
        self.assertEqual(reason, "adaptive_novel")

    def test_adaptive_below_threshold_not_routine(self):
        self.config.primary.think = False
        self.config.primary.adaptive_think = True
        self.config.primary.routine_threshold = 3
        self._seed_routine("fix the login bug", 2)  # seen but not matured
        think, reason = self._decide("can you fix the login bug")
        self.assertTrue(think)
        self.assertEqual(reason, "adaptive_novel")


if __name__ == "__main__":
    unittest.main()
