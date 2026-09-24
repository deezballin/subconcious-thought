"""Tests for the JSONL training exporter."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind.exporter import Exporter
from undermind.intents import intent_id, signature
from undermind.store import UndermindStore


class TestExporter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "undermind.db")
        self.export_path = os.path.join(self.tmp.name, "training_export.jsonl")
        self.store = UndermindStore(self.db_path)
        self.exporter = Exporter(self.store, self.export_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _record(self, text: str) -> int:
        input_id = self.store.record_input(text)
        iid = intent_id(text)
        self.store.upsert_intent(iid, signature(text))
        self.store.add_intent_sample(iid, input_id)
        return iid

    def test_no_export_below_min_count(self):
        self._record("fix the login bug")
        records = self.exporter.export_pending(min_count=2, max_samples=10)
        self.assertEqual(records, [])
        self.assertFalse(os.path.exists(self.export_path))

    def test_export_appends_valid_jsonl(self):
        iid = self._record("fix the login bug")
        self._record("please fix login bugs")
        records = self.exporter.export_pending(min_count=2, max_samples=10)
        self.assertEqual(len(records), 1)
        self.assertTrue(os.path.exists(self.export_path))
        with open(self.export_path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(lines), 1)
        record = lines[0]
        self.assertEqual(record["intent_id"], iid)
        self.assertEqual(record["count"], 2)
        self.assertEqual(record["signature"], signature("fix the login bug"))
        self.assertEqual(len(record["samples"]), 2)
        self.assertIn("export_run_ms", record)
        sample_texts = {s["text"] for s in record["samples"]}
        self.assertEqual(sample_texts, {"fix the login bug", "please fix login bugs"})

    def test_dedupe_until_count_changes(self):
        self._record("fix the login bug")
        self._record("please fix login bugs")
        first = self.exporter.export_pending(min_count=2, max_samples=10)
        self.assertEqual(len(first), 1)
        self.assertEqual(self.exporter.export_pending(min_count=2, max_samples=10), [])
        self._record("can someone fix the login bug")
        second = self.exporter.export_pending(min_count=2, max_samples=10)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["count"], 3)

        with open(self.export_path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual([line["count"] for line in lines], [2, 3])

    def test_ledger_row_written(self):
        self._record("fix the login bug")
        self._record("please fix login bugs")
        records = self.exporter.export_pending(min_count=2, max_samples=10)
        self.assertEqual(len(records), 1)
        row = self.store._conn.execute("SELECT * FROM exports").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["records"], 1)
        self.assertTrue(row["path"].endswith("training_export.jsonl"))

    def test_max_samples_respected(self):
        for text in (
            "fix the login bug",
            "please fix login bugs",
            "can you fix the login bug",
            "fix login bug now",
        ):
            self._record(text)
        records = self.exporter.export_pending(min_count=2, max_samples=3)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]["samples"]), 3)
    def test_unicode_export(self):
        self._record("résumé café review")
        self._record("résumé café review again")
        records = self.exporter.export_pending(min_count=2, max_samples=10)
        self.assertEqual(len(records), 1)
        with open(self.export_path, encoding="utf-8") as fh:
            line = json.loads(fh.readline())
        self.assertIn("café review", json.dumps(line, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
