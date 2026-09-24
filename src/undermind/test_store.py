"""Tests for the SQLite store layer."""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind.store import UndermindStore


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "undermind.db")
        self.store = UndermindStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_creates_data_directory(self):
        nested = os.path.join(self.tmp.name, "a", "b", "undermind.db")
        with UndermindStore(nested):
            self.assertTrue(os.path.isdir(os.path.dirname(nested)))

    def test_tables_exist(self):
        rows = self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {row["name"] for row in rows}
        for expected in ("inputs", "intents", "intent_samples", "handoffs", "exports", "meta"):
            self.assertIn(expected, names)

    def test_record_and_fetch_unprocessed(self):
        input_id = self.store.record_input("fix the login bug")
        unprocessed = self.store.fetch_unprocessed()
        self.assertEqual(len(unprocessed), 1)
        self.assertEqual(unprocessed[0]["id"], input_id)
        self.assertEqual(unprocessed[0]["text"], "fix the login bug")
        self.assertEqual(unprocessed[0]["processed"], 0)

    def test_mark_processed(self):
        id1 = self.store.record_input("first")
        id2 = self.store.record_input("second")
        self.store.mark_processed([id1, id2])
        self.assertEqual(len(self.store.fetch_unprocessed()), 0)

    def test_upsert_intent_creates_and_bumps(self):
        count1 = self.store.upsert_intent("abc123", "bug fix login")
        count2 = self.store.upsert_intent("abc123", "bug fix login")
        self.assertEqual(count1, 1)
        self.assertEqual(count2, 2)
        row = self.store.get_intent("abc123")
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["signature"], "bug fix login")

    def test_intent_samples(self):
        input_id = self.store.record_input("fix the login bug")
        self.store.upsert_intent("abc123", "bug fix login")
        self.store.add_intent_sample("abc123", input_id)
        samples = self.store.samples_for_intent("abc123")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["text"], "fix the login bug")

    def test_pending_export_flow(self):
        iid = "abc123"
        self.store.upsert_intent(iid, "sig")
        pending = self.store.pending_export_intents(min_count=2)
        self.assertEqual(pending, [])
        self.store.upsert_intent(iid, "sig")
        pending = self.store.pending_export_intents(min_count=2)
        self.assertEqual(len(pending), 1)
        self.store.mark_exported([iid])
        self.assertEqual(self.store.pending_export_intents(min_count=2), [])
        self.store.upsert_intent(iid, "sig")
        self.assertEqual(len(self.store.pending_export_intents(min_count=2)), 1)

    def test_handoff_records(self):
        self.store.record_handoff(
            trigger="confidence_0.95",
            branch="the quick brown fox",
            provider="openai_compat",
            model="test-model",
            status="ok",
            confidence=0.97,
            latency_ms=12.5,
        )
        self.store.record_handoff(
            trigger="proxy_direct",
            branch="failing branch",
            provider="webhook",
            model="hook",
            status="error",
            error="connection refused",
        )
        self.assertEqual(self.store.count_handoffs(), 2)
        self.assertEqual(self.store.count_handoffs(status="ok"), 1)
        latest = self.store.latest_handoff()
        self.assertEqual(latest["status"], "error")
        self.assertEqual(latest["error"], "connection refused")

    def test_export_ledger(self):
        self.store.record_export("data/training_export.jsonl", 3, ["a", "b", "c"])
        row = self.store._conn.execute("SELECT * FROM exports").fetchone()
        self.assertEqual(row["records"], 3)
        self.assertEqual(row["intent_ids"], "a,b,c")

    def test_concurrent_writes(self):
        errors = []

        def writer(index: int):
            try:
                for _ in range(20):
                    self.store.record_input(f"text {index}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.store.count_inputs(), 80)


if __name__ == "__main__":
    unittest.main()
