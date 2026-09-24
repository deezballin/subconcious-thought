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
    config = load_config(None)
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
        cls.server = _make_server(11438, cls.tmp.name)
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
            f"http://127.0.0.1:11438{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode())

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(
            f"http://127.0.0.1:11438{path}", timeout=5
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
            "http://127.0.0.1:11438/api/bogus",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 404)


class TestProxyPrimaryFailure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = _make_server(11439, cls.tmp.name)

        class BrokenPrimary:
            def execute(self, branch, context=None):
                raise ProviderError("primary down")

        cls.server.primary = BrokenPrimary()
        handler = cls.server._handler_factory()
        cls.server._server = ThreadingHTTPServer((cls.server.host, 11439), handler)
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
            "http://127.0.0.1:11439/api/generate",
            data=json.dumps({"model": "m", "prompt": "hello"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 502)


if __name__ == "__main__":
    unittest.main()
