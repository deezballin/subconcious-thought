"""Tests for the ConfidenceTracker (95% crossing detection)."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind.confidence import ConfidenceTracker


class TestConfidenceMath(unittest.TestCase):
    def test_logprob_conversion(self):
        self.assertAlmostEqual(
            ConfidenceTracker.confidence_from_logprob(-0.0513), 0.95, places=3
        )
        self.assertAlmostEqual(
            ConfidenceTracker.confidence_from_logprob(0.0), 1.0
        )
        self.assertAlmostEqual(
            ConfidenceTracker.confidence_from_logprob(-2.3), 0.1, places=2
        )

    def test_negative_logprob_clamped(self):
        self.assertEqual(ConfidenceTracker.confidence_from_logprob(0.5), 1.0)


class TestCrossing(unittest.TestCase):
    def test_crossing_detection(self):
        tracker = ConfidenceTracker(threshold=0.95, min_tokens=1, min_chars=0)
        crossing = None
        # exp(-0.06) = 0.942, below threshold
        crossing = tracker.add_token("Hello", -0.06)
        self.assertIsNone(crossing)
        # exp(-0.01) = 0.990, crosses
        crossing = tracker.add_token(" world", -0.01)
        self.assertIsNotNone(crossing)
        self.assertGreaterEqual(crossing.confidence, 0.95)
        self.assertTrue(tracker.crossed)

    def test_edge_triggered_once(self):
        tracker = ConfidenceTracker(threshold=0.95, min_tokens=1, min_chars=0)
        first = tracker.add_token("a", -0.5)
        second = tracker.add_token("b", -0.001)
        third = tracker.add_token("c", -0.001)
        self.assertIsNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third)

    def test_no_crossing_below_threshold(self):
        tracker = ConfidenceTracker(threshold=0.95, min_tokens=1, min_chars=0)
        for _ in range(5):
            result = tracker.add_token("x", -0.5)
            self.assertIsNone(result)
        self.assertFalse(tracker.crossed)
        self.assertLess(tracker.current_confidence, 0.95)

    def test_min_tokens_guard(self):
        tracker = ConfidenceTracker(threshold=0.95, min_tokens=3, min_chars=0)
        tracker.add_token("a", -0.001)
        tracker.add_token("b", -0.001)
        crossing = tracker.add_token("c", -0.001)
        self.assertIsNotNone(crossing)
        self.assertEqual(crossing.token_index, 2)

    def test_min_chars_guard(self):
        tracker = ConfidenceTracker(threshold=0.95, min_tokens=1, min_chars=50)
        crossing = tracker.add_token("short", -0.001)
        self.assertIsNone(crossing)
        crossing = tracker.add_token("x" * 60, -0.001)
        self.assertIsNotNone(crossing)

    def test_timestamps_are_nanosecond(self):
        tracker = ConfidenceTracker(threshold=0.95, min_tokens=1, min_chars=0)
        crossing = tracker.add_token("hi", -0.001)
        self.assertGreater(crossing.monotonic_ns, 0)
        self.assertGreater(crossing.wall_ns, 0)
        self.assertLessEqual(
            abs(crossing.wall_ns - time.time_ns()), 60_000_000_000
        )

    def test_crossing_ms_measured(self):
        tracker = ConfidenceTracker(threshold=0.95, min_tokens=1, min_chars=0)
        tracker.add_token("one ", -0.4)
        tracker.add_token("two", -0.001)
        state = tracker.to_dict()
        self.assertTrue(state["crossed"])
        self.assertGreaterEqual(state["ms_from_first_token_to_crossing"], 0.0)


class TestModes(unittest.TestCase):
    def test_latest_mode_uses_newest_token(self):
        tracker = ConfidenceTracker(mode="latest", min_tokens=1, min_chars=0)
        tracker.add_token("a", -0.001)
        self.assertGreater(tracker.current_confidence, 0.99)
        tracker.add_token("b", -3.0)
        self.assertLess(tracker.current_confidence, 0.05)

    def test_ewma_mode_smooths(self):
        tracker = ConfidenceTracker(
            mode="ewma", ewma_alpha=0.5, min_tokens=1, min_chars=0
        )
        tracker.add_token("a", -0.001)
        high = tracker.current_confidence
        tracker.add_token("b", -3.0)
        smoothed = tracker.current_confidence
        self.assertGreater(smoothed, 0.05)
        self.assertLess(smoothed, high)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            ConfidenceTracker(mode="bogus")

    def test_invalid_threshold_raises(self):
        with self.assertRaises(ValueError):
            ConfidenceTracker(threshold=1.5)


class TestReset(unittest.TestCase):
    def test_reset_clears_crossing(self):
        tracker = ConfidenceTracker(min_tokens=1, min_chars=0)
        tracker.add_token("a", -0.001)
        self.assertTrue(tracker.crossed)
        tracker.reset()
        self.assertFalse(tracker.crossed)
        self.assertEqual(tracker.text, "")
        self.assertEqual(len(tracker.samples), 0)
        # A fresh stream can cross again after reset.
        crossing = tracker.add_token("b", -0.001)
        self.assertIsNotNone(crossing)
        self.assertTrue(tracker.crossed)


if __name__ == "__main__":
    unittest.main()
