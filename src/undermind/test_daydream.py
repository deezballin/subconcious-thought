"""Tests for the DaydreamWorker idle-triggered background loop."""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind.daydream import DaydreamWorker
from undermind.exporter import Exporter
from undermind.intents import intent_id as compute_intent_id
from undermind.store import UndermindStore


class TestDaydreamCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "undermind.db")
        self.export_path = os.path.join(self.tmp.name, "training_export.jsonl")
        self.store = UndermindStore(self.db_path)
        self.exporter = Exporter(self.store, self.export_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _worker(self, **kwargs) -> DaydreamWorker:
        defaults = dict(
            store=self.store,
            exporter=self.exporter,
            idle_threshold_s=5.0,
            min_intent_count=2,
            max_samples_per_intent=10,
            poll_interval_s=0.05,
        )
        defaults.update(kwargs)
        return DaydreamWorker(**defaults)

    def test_idle_gate_blocks_run(self):
        worker = self._worker()
        self.store.record_input("fix the login bug")
        self.store.record_input("please fix login bugs")
        result = worker.run_cycle()
        self.assertIsNotNone(result)
        self.assertEqual(result.inputs_processed, 2)

    def test_cycle_groups_intents_and_exports(self):
        worker = self._worker()
        self.store.record_input("fix the login bug")
        self.store.record_input("please fix login bugs")
        self.store.record_input("deploy the staging server")
        result = worker.run_cycle()
        self.assertEqual(result.inputs_processed, 3)
        self.assertEqual(result.intents_updated, 2)
        self.assertEqual(result.records_exported, 1)
        self.assertTrue(os.path.exists(self.export_path))

    def test_empty_cycle_returns_none(self):
        worker = self._worker()
        self.assertIsNone(worker.run_cycle())

    def test_buffer_supplier_flushes(self):
        worker = self._worker(buffer_supplier=lambda: "summarize the meeting notes")
        result = worker.run_cycle()
        self.assertEqual(result.inputs_processed, 1)
        self.assertEqual(self.store.count_inputs(), 1)

    def test_none_from_supplier_skips_flush(self):
        worker = self._worker(buffer_supplier=lambda: None)
        self.assertIsNone(worker.run_cycle())

    def test_background_thread_runs_on_idle(self):
        done = threading.Event()
        results = []

        def on_cycle(result):
            results.append(result)
            done.set()

        worker = self._worker(
            idle_threshold_s=0.2,
            poll_interval_s=0.05,
            buffer_supplier=lambda: "write release notes",
            on_cycle=on_cycle,
        )
        worker.start()
        try:
            self.assertTrue(done.wait(timeout=5))
            self.assertGreaterEqual(results[0].inputs_processed, 1)
        finally:
            worker.stop()

    def test_no_cycle_while_active(self):
        cycles = []

        def on_cycle(result):
            cycles.append(result)

        worker = self._worker(
            idle_threshold_s=5.0,
            on_cycle=on_cycle,
        )
        worker.start()
        try:
            for _ in range(10):
                worker.notify_activity()
                time.sleep(0.05)
            self.assertEqual(cycles, [])
        finally:
            worker.stop()

    def test_set_busy_defers_cycle(self):
        worker = self._worker(idle_threshold_s=0.2, poll_interval_s=0.05)
        worker.start()
        try:
            worker.set_busy(True)
            time.sleep(0.3)
            self.assertEqual(worker.cycles_run, 0)
        finally:
            worker.stop()

    def test_force_idle(self):
        worker = self._worker(idle_threshold_s=100.0, poll_interval_s=0.05)
        worker.force_idle()
        self.store.record_input("fix the login bug")
        self.store.record_input("repair login bugs now")
        result = worker.run_cycle()
        self.assertIsNotNone(result)


class TestNearDuplicateMerge(unittest.TestCase):
    """Near-duplicate intents fold into one bucket when merging is enabled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UndermindStore(os.path.join(self.tmp.name, "merge.db"))
        self.exporter = Exporter(self.store, os.path.join(self.tmp.name, "exp.jsonl"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _worker(self, **kwargs) -> DaydreamWorker:
        defaults = dict(
            store=self.store,
            exporter=self.exporter,
            idle_threshold_s=5.0,
            min_intent_count=1,
            max_samples_per_intent=10,
        )
        defaults.update(kwargs)
        return DaydreamWorker(**defaults)

    def test_near_duplicates_merge_when_enabled(self):
        worker = self._worker(merge_similarity=0.6)
        self.store.record_input("bridge smoke test")
        self.store.record_input("bridge one smoke test")
        result = worker.run_cycle()
        self.assertEqual(result.inputs_processed, 2)
        intents = self.store.list_intents(min_count=1)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["count"], 2)

    def test_near_duplicates_stay_separate_when_disabled(self):
        worker = self._worker(merge_similarity=0.0)
        self.store.record_input("bridge smoke test")
        self.store.record_input("bridge one smoke test")
        worker.run_cycle()
        self.assertEqual(len(self.store.list_intents(min_count=1)), 2)

    def test_merge_survivor_is_earlier_intent(self):
        # The first-seen intent absorbs the later near-duplicate.
        first_id = compute_intent_id("bridge smoke test")
        worker = self._worker(merge_similarity=0.6)
        self.store.record_input("bridge smoke test")
        self.store.record_input("bridge one smoke test")
        worker.run_cycle()
        merged = self.store.get_intent(first_id)
        self.assertIsNotNone(merged)
        self.assertEqual(merged["count"], 2)

    def test_same_batch_near_duplicates_merge(self):
        # Both inputs arrive in one cycle; candidates refresh per input.
        worker = self._worker(merge_similarity=0.6)
        self.store.record_input("fix the login bug")
        self.store.record_input("fix login bug please")
        result = worker.run_cycle()
        self.assertEqual(result.inputs_processed, 2)
        self.assertEqual(len(self.store.list_intents(min_count=1)), 1)


if __name__ == "__main__":
    unittest.main()
