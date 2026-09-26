"""
Unit tests for Undermind core modules (listener, config, providers).

Live-model integration tests skip automatically when no backend is
reachable, so the suite runs fully offline.
"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind.config import load_config
from undermind.listener import Listener
from undermind.providers import (
    NoopProvider,
    build_draft_client,
    build_primary_provider,
)
from undermind.providers.base import ProviderError


class TestListener(unittest.TestCase):
    def test_initial_buffer(self):
        listener = Listener()
        self.assertEqual(listener.get_buffer(), "")
        self.assertFalse(listener.running)

    def test_on_press_appends_characters(self):
        listener = Listener()

        class FakeKey:
            def __init__(self, char):
                self.char = char

        listener.on_press(FakeKey("h"))
        listener.on_press(FakeKey("i"))
        self.assertEqual(listener.get_buffer(), "hi")

    def test_space_and_backspace(self):
        listener = Listener()
        listener.on_press(type("K", (), {"char": None})())
        # space arrives as a Key without .char
        listener.on_press(__import__("pynput").keyboard.Key.space)
        self.assertEqual(listener.get_buffer(), " ")
        listener.on_press(__import__("pynput").keyboard.Key.backspace)
        self.assertEqual(listener.get_buffer(), "")

    def test_enter_sets_submit_flag(self):
        import pynput

        listener = Listener()
        listener.on_press(pynput.keyboard.Key.enter)
        self.assertTrue(listener.consume_submit())
        self.assertFalse(listener.consume_submit())

    def test_take_buffer_clears(self):
        listener = Listener()
        listener.on_press(type("K", (), {"char": "a"})())
        text = listener.take_buffer()
        self.assertEqual(text, "a")
        self.assertEqual(listener.get_buffer(), "")

    def test_max_buffer_trim(self):
        listener = Listener(max_buffer=10)
        for index in range(15):
            listener.on_press(type("K", (), {"char": chr(ord("a") + index)})())
        self.assertEqual(len(listener.get_buffer()), 10)
        self.assertEqual(listener.get_buffer(), "fghijklmno")

    def test_idle_seconds(self):
        listener = Listener()
        listener.on_press(type("K", (), {"char": "a"})())
        self.assertLess(listener.idle_seconds(), 1.0)

    def test_start_stop_thread(self):
        listener = Listener()
        thread = threading.Thread(target=listener.start, daemon=True)
        thread.start()
        time.sleep(0.2)
        listener.stop()
        self.assertFalse(listener.running)


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        # Load explicitly from a nonexistent path so the suite is hermetic and
        # does not depend on a (real) config.toml sitting in the repo root.
        config = load_config(os.path.join(os.sep, "nonexistent", "config.toml"))
        self.assertEqual(config.confidence.threshold, 0.95)
        self.assertEqual(config.confidence.mode, "latest")
        self.assertEqual(config.draft.base_url, "http://localhost:13305")
        self.assertEqual(config.primary.kind, "openai_compat")
        self.assertEqual(config.daydream.idle_threshold_s, 5.0)
        self.assertEqual(config.daydream.export_path, "data/training_export.jsonl")
        self.assertEqual(config.store.db_path, "data/undermind.db")

    def test_toml_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "[confidence]\n"
                    "threshold = 0.9\n"
                    "mode = 'ewma'\n"
                    "[draft]\n"
                    'model = "Krios-9B"\n'
                    '[primary]\n'
                    'kind = "webhook"\n'
                    'webhook_url = "http://example.com/hook"\n'
                )
            config = load_config(path)
            self.assertEqual(config.confidence.threshold, 0.9)
            self.assertEqual(config.confidence.mode, "ewma")
            self.assertEqual(config.draft.model, "Krios-9B")
            self.assertEqual(config.primary.kind, "webhook")
            self.assertEqual(config.primary.webhook_url, "http://example.com/hook")
            self.assertEqual(config.source_path, path)

    def test_missing_file_falls_back_to_defaults(self):
        config = load_config("/nonexistent/path/config.toml")
        self.assertEqual(config.confidence.threshold, 0.95)

    def test_unknown_section_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("[bogus]\nkey = 1\n")
            with self.assertRaises(ValueError):
                load_config(path)

    def test_unknown_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("[draft]\nbogus_key = 1\n")
            with self.assertRaises(ValueError):
                load_config(path)


class TestProviderFactory(unittest.TestCase):
    def test_build_draft_openai_compat(self):
        config = load_config(None)
        client = build_draft_client(config)
        self.assertEqual(client.base_url, "http://localhost:13305")

    def test_build_draft_ollama(self):
        config = load_config(None)
        config.draft.kind = "ollama"
        config.draft.base_url = "http://localhost:11434"
        client = build_draft_client(config)
        self.assertEqual(client.base_url, "http://localhost:11434")

    def test_build_draft_unknown_raises(self):
        config = load_config(None)
        config.draft.kind = "bogus"
        with self.assertRaises(ProviderError):
            build_draft_client(config)

    def test_build_primary_variants(self):
        config = load_config(None)
        config.primary.webhook_url = "http://example.com/hook"
        for kind, expected in (
            ("openai_compat", "OpenAICompatProvider"),
            ("ollama", "OllamaNativeProvider"),
            ("webhook", "WebhookProvider"),
            ("none", "NoopProvider"),
        ):
            config.primary.kind = kind
            provider = build_primary_provider(config)
            self.assertEqual(type(provider).__name__, expected)

    def test_noop_provider(self):
        provider = NoopProvider()
        result = provider.execute("branch text")
        self.assertIn("branch received", result)

    def test_webhook_requires_url(self):
        config = load_config(None)
        config.primary.kind = "webhook"
        config.primary.webhook_url = ""
        with self.assertRaises(ProviderError):
            build_primary_provider(config)


class TestLiveDraftClient(unittest.TestCase):
    """Integration against a real backend; skipped when unreachable."""

    def setUp(self):
        self.config = load_config(None)
        self.client = build_draft_client(self.config)
        try:
            models = self.client.list_models()
        except ProviderError:
            self.skipTest(f"No backend at {self.config.draft.base_url}")
        if not models:
            self.skipTest("Backend reports no models")

    def test_list_models(self):
        self.assertIsInstance(self.client.list_models(), list)

    def test_stream_tokens(self):
        if self.config.draft.model not in self.client.list_models():
            self.skipTest(f"{self.config.draft.model} not loaded")
        from undermind.providers.base import build_draft_messages

        tokens = []
        self.client.stream_tokens(
            build_draft_messages("The capital of France is"),
            16,
            0.0,
            lambda text, logprob: tokens.append((text, logprob)),
        )
        self.assertGreater(len(tokens), 0)


if __name__ == "__main__":
    unittest.main()
