"""Tests for the Memory Mine (semantic reflection store).

Uses a deterministic fake embedder (hash-based vectors) so the suite stays
offline and fast; the real model's behavior is verified in the live check.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind.memory import MemoryStore


class _FakeModel:
    """Deterministic embedder: word-hash bag vector, normalized."""

    def encode(self, text):
        vec = [0.0] * 384
        for word in text.lower().split():
            vec[hash(word) % 384] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


class FakeMemoryStore(MemoryStore):
    """MemoryStore with the fake embedder injected."""

    def _get_model(self):
        return _FakeModel()


class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FakeMemoryStore(os.path.join(self.tmp.name, "mem_lance"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_remember_and_count(self):
        self.assertFalse(self.store.remember(""))
        self.assertTrue(self.store.remember("I keep offering login fixes first", kind="reply", ref_id=1))
        self.assertTrue(self.store.remember("the staging deploy felt rushed", kind="free_turn", ref_id=2))
        self.assertEqual(self.store.count(), 2)

    def test_echoes_recall_by_meaning(self):
        self.store.remember("I keep offering login fixes first")
        self.store.remember("the staging deploy felt rushed")
        # The fake embedder is a bag-of-hash vector (absolute scores are
        # meaningless); the semantic assertion is *ranking*: the text sharing
        # the query's subject must outrank the unrelated one.
        hits = self.store.echoes("another login fix suggestion", limit=2, min_score=-1.0)
        self.assertEqual(len(hits), 2)
        self.assertIn("login", hits[0]["text"])
        self.assertIn("staging", hits[1]["text"])

    def test_echoes_empty_when_nothing_stored(self):
        self.assertEqual(self.store.echoes("anything at all"), [])

    def test_min_score_filters_weak_matches(self):
        self.store.remember("the staging deploy felt rushed")
        self.assertEqual(self.store.echoes("totally unrelated quantum banana", min_score=0.99), [])


class TestGateIntents(unittest.TestCase):
    """The think gate's embedded intent index (pillar 2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FakeMemoryStore(os.path.join(self.tmp.name, "gate_lance"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_remember_intent_is_idempotent(self):
        self.assertTrue(self.store.remember_intent("fix the login bug", "aaa"))
        self.assertTrue(self.store.remember_intent("fix the login bug", "aaa"))
        # Same id re-indexed -> still exactly one row.
        self.assertEqual(self.store._get_gate_table().count_rows(), 1)

    def test_gate_match_above_and_below_bar(self):
        self.store.remember_intent("fix the login bug", "aaa")
        hit = self.store.gate_match("please fix the login bug", min_score=0.5)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["ref_str"], "aaa")
        self.assertGreater(hit["score"], 0.5)
        self.assertIsNone(self.store.gate_match("quantum banana orbit", min_score=0.9))

    def test_gate_match_empty_table(self):
        self.assertIsNone(self.store.gate_match("anything", min_score=0.0))


if __name__ == "__main__":
    unittest.main()
