"""Tests for DraftPredictor and Pipeline wiring with a fake draft provider."""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind.config import load_config
from undermind.confidence import ConfidenceTracker
from undermind.handoff import UniversalHandoff
from undermind.predictor import DraftPredictor, Predictor, Hypothesis
from undermind.providers.base import ProviderError, build_draft_messages
from undermind.providers import NoopProvider


class FakeDraftClient:
    """Emits a scripted token/logprob sequence for one buffer state."""

    def __init__(self, script=None, fail=False, token_delay_s=0.0):
        self.script = script or [("The", -0.1), (" quick", -0.02), (" fox", -0.005)]
        self.fail = fail
        self.token_delay_s = token_delay_s
        self.calls = []
        self.cancelled = False

    def stream_tokens(self, messages, max_tokens, temperature, on_token):
        self.calls.append(messages)
        if self.fail:
            raise ProviderError("backend offline")
        for text, logprob in self.script:
            if self.token_delay_s:
                time.sleep(self.token_delay_s)
            on_token(text, logprob)
        return "".join(text for text, _ in self.script)

    def list_models(self):
        return ["FakeDraft-9B"]


class TestBuildMessages(unittest.TestCase):
    def test_messages_shape(self):
        messages = build_draft_messages("hello wor")
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"], "hello wor")


class TestDraftPredictorCrossing(unittest.TestCase):
    def _make(self, client, **kwargs) -> DraftPredictor:
        defaults = dict(
            draft_client=client,
            debounce_s=0.0,
            confidence_kwargs={"threshold": 0.95, "min_tokens": 1, "min_chars": 0},
        )
        defaults.update(kwargs)
        return DraftPredictor(**defaults)

    def test_crossing_fires_confident_callback(self):
        client = FakeDraftClient()
        fired = []

        def on_confident(branch, crossing, tracker):
            fired.append((branch, crossing.confidence))

        predictor = self._make(client, on_confident=on_confident)
        predictor.notify_keystroke("the quick brown")
        predictor.poll()
        deadline = time.monotonic() + 5
        while not fired and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(fired), 1)
        branch, confidence = fired[0]
        # "The" (~0.90) stays below threshold; " quick" (~0.98) crosses, so
        # the stream stops there and the branch carries only those tokens.
        self.assertEqual(branch, "the quick brownThe quick")
        self.assertGreaterEqual(confidence, 0.95)

    def test_no_crossing_fires_suggestion(self):
        client = FakeDraftClient(script=[("some", -0.9), (" words", -0.9)])
        suggestions = []
        predictor = self._make(client, on_suggestion=lambda b, t: suggestions.append(b))
        predictor.notify_keystroke("hello world")
        predictor.poll()
        deadline = time.monotonic() + 5
        while not suggestions and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0], "hello worldsome words")

    def test_new_keystroke_cancels_inflight(self):
        client = FakeDraftClient(token_delay_s=0.05)
        fired = []
        predictor = self._make(client, on_confident=lambda b, c, t: fired.append(b))
        predictor.notify_keystroke("first buffer state")
        predictor.poll()
        time.sleep(0.02)
        predictor.notify_keystroke("first buffer state now longer")
        deadline = time.monotonic() + 5
        while predictor.is_busy() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        self.assertEqual(fired, [])

    def test_provider_error_reaches_error_callback(self):
        client = FakeDraftClient(fail=True)
        errors = []
        predictor = self._make(client, on_error=errors.append)
        predictor.notify_keystroke("hello world")
        predictor.poll()
        deadline = time.monotonic() + 5
        while predictor.is_busy() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(errors), 1)
        self.assertIn("backend offline", errors[0])

    def test_short_buffer_not_predicted(self):
        client = FakeDraftClient()
        predictor = self._make(client)
        predictor.notify_keystroke("ok")
        predictor.poll()
        time.sleep(0.1)
        self.assertEqual(client.calls, [])

    def test_complete_once_returns_text_and_confidence(self):
        client = FakeDraftClient()
        predictor = self._make(client)
        text, confidence = predictor.complete_once("the quick brown")
        self.assertEqual(text, "The quick fox")
        self.assertGreater(confidence, 0.9)


class TestUniversalHandoff(unittest.TestCase):
    def test_send_and_record_ok(self):
        store_tmp = tempfile.TemporaryDirectory()
        from undermind.store import UndermindStore

        store = UndermindStore(os.path.join(store_tmp.name, "db.sqlite"))
        try:
            handoff = UniversalHandoff(NoopProvider(), "none", "no-model")
            result = handoff.send_and_record(
                "branch text", store, trigger="confidence_0.95", confidence=0.96
            )
            self.assertIn("branch received", result)
            self.assertEqual(store.count_handoffs(), 1)
            row = store.latest_handoff()
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["confidence"], 0.96)
        finally:
            store.close()
            store_tmp.cleanup()

    def test_send_and_record_error(self):
        from undermind.store import UndermindStore

        class FailingProvider:
            def execute(self, branch, context=None):
                raise ProviderError("boom")

        store_tmp = tempfile.TemporaryDirectory()
        store = UndermindStore(os.path.join(store_tmp.name, "db.sqlite"))
        try:
            handoff = UniversalHandoff(FailingProvider(), "fail", "m")
            with self.assertRaises(Exception):
                handoff.send_and_record("branch", store)
            row = store.latest_handoff()
            self.assertEqual(row["status"], "error")
            self.assertIn("boom", row["error"])
        finally:
            store.close()
            store_tmp.cleanup()

    def test_retries_then_raises(self):
        attempts = []

        class FlakyProvider:
            def execute(self, branch, context=None):
                attempts.append(1)
                raise ProviderError("down")

        handoff = UniversalHandoff(FlakyProvider(), "flaky", "m", retries=2)
        with self.assertRaises(Exception):
            handoff.send("branch")
        self.assertEqual(len(attempts), 3)


class TestPipelineWiring(unittest.TestCase):
    def test_pipeline_with_fake_provider(self):
        tmp = tempfile.TemporaryDirectory()
        config = load_config(None)
        config.store.db_path = os.path.join(tmp.name, "undermind.db")
        config.daydream.export_path = os.path.join(tmp.name, "training_export.jsonl")
        config.daydream.idle_threshold_s = 0.1
        config.draft.debounce_s = 0.0

        from undermind.main import Pipeline
        from undermind.store import UndermindStore

        store = UndermindStore(config.store.db_path)
        pipeline = Pipeline(config, store=store, quiet=True)
        pipeline.draft_client = FakeDraftClient()
        pipeline.predictor.draft_client = pipeline.draft_client
        pipeline.handoff.provider = NoopProvider()
        pipeline.handoff.provider_name = "none"
        pipeline.handoff.model = "noop"

        fired = []
        real_callback = pipeline._on_confident

        def tracking_callback(branch, crossing, tracker):
            fired.append(branch)
            real_callback(branch, crossing, tracker)

        pipeline.predictor.on_confident = tracking_callback
        pipeline.predictor.confidence_kwargs = {
            "threshold": 0.95,
            "min_tokens": 1,
            "min_chars": 0,
        }

        pipeline.listener.buffer.extend(list("the quick brown"))
        pipeline.tick()
        deadline = time.monotonic() + 5
        while not fired and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0], "the quick brownThe quick")

        deadline = time.monotonic() + 5
        while store.count_handoffs() < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(store.count_handoffs(), 1)
        row = store.latest_handoff()
        self.assertEqual(row["trigger"], "confidence_0.95")
        self.assertEqual(row["provider"], "none")
        self.assertEqual(row["status"], "ok")

        pipeline.stop()
        store.close()
        tmp.cleanup()

    def test_status_shape(self):
        tmp = tempfile.TemporaryDirectory()
        config = load_config(None)
        config.store.db_path = os.path.join(tmp.name, "undermind.db")
        config.daydream.export_path = os.path.join(tmp.name, "training_export.jsonl")

        from undermind.main import Pipeline
        from undermind.store import UndermindStore

        store = UndermindStore(config.store.db_path)
        pipeline = Pipeline(config, store=store, quiet=True)
        status = pipeline.status()
        for key in (
            "version",
            "draft_model",
            "primary_kind",
            "confidence_threshold",
            "handoffs",
            "inputs",
            "intents",
        ):
            self.assertIn(key, status)
        pipeline.stop()
        store.close()
        tmp.cleanup()


class TestLegacyPredictor(unittest.TestCase):
    def test_hypothesis_ordering(self):
        h1 = Hypothesis(score=1.0, text="a")
        h2 = Hypothesis(score=2.0, text="b")
        self.assertGreater(h2, h1)

    def test_beam_update_with_fake_client(self):
        client = FakeDraftClient()
        predictor = Predictor(draft_client=client, beam_width=3)
        beam = predictor.update("buffer text")
        texts = {h.text for h in beam}
        self.assertIn("buffer text", texts)
        self.assertIn("The quick fox", texts)
        self.assertEqual(beam[0].text, "The quick fox")
        self.assertGreater(beam[0].score, 0.9)
        self.assertTrue(predictor.is_dominant())
        self.assertIsNotNone(predictor.top)
        predictor.reset()
        self.assertEqual(len(predictor.hypotheses), 0)

    def test_beam_without_client(self):
        predictor = Predictor(draft_client=None)
        beam = predictor.update("buffer text")
        self.assertEqual(len(beam), 1)
        self.assertEqual(beam[0].score, 0.0)


if __name__ == "__main__":
    unittest.main()
