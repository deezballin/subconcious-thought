"""
Tests for the stack doctor, the proxy daydream scheduler, /api/health,
and the already-running watchdog guard.
"""

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind import doctor
from undermind.config import Config
from undermind.daydream import DaydreamWorker
from undermind.exporter import Exporter
from undermind.proxy import DaydreamScheduler, ProxyServer, port_in_use
from undermind.store import UndermindStore


class TestPortInUse(unittest.TestCase):
    def test_open_port_detected(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            self.assertTrue(port_in_use("127.0.0.1", port))
        finally:
            server.close()

    def test_free_port_not_detected(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertFalse(port_in_use("127.0.0.1", port))


class TestDaydreamScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UndermindStore(os.path.join(self.tmp.name, "sched.db"))
        exporter = Exporter(self.store, os.path.join(self.tmp.name, "exp.jsonl"))
        worker = DaydreamWorker(
            store=self.store,
            exporter=exporter,
            idle_threshold_s=0.2,
            min_intent_count=1,
        )
        self.scheduler = DaydreamScheduler(
            worker, idle_threshold_s=0.2, poll_interval_s=0.05
        )

    def tearDown(self):
        self.scheduler.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_idle_miner_processes_recorded_inputs(self):
        self.store.record_input("please fix the login bug")
        self.scheduler.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.store.count_unprocessed() == 0:
                break
            time.sleep(0.05)
        self.assertEqual(self.store.count_unprocessed(), 0)
        intents = self.store.list_intents(min_count=1)
        self.assertTrue(any("login" in i["signature"] for i in intents))
        self.assertGreaterEqual(self.scheduler.cycles_run, 1)

    def test_activity_rearms_idle_window(self):
        self.scheduler.start()
        time.sleep(0.2)
        before = self.scheduler.idle_for()
        self.scheduler.notify_activity()
        self.assertLess(self.scheduler.idle_for(), before)

    def test_status_shape(self):
        for key in (
            "running",
            "idle_threshold_s",
            "idle_for_s",
            "cycles_run",
            "last_result",
        ):
            self.assertIn(key, self.scheduler.status())


class TestHealthEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        config = Config()
        config.store.db_path = os.path.join(cls.tmp.name, "health.db")
        cls.server = ProxyServer(host="127.0.0.1", port=11443, config=config)
        cls.server.daydream_scheduler.start()
        handler = cls.server._handler_factory()
        cls.server._server = ThreadingHTTPServer(("127.0.0.1", 11443), handler)
        cls.thread = threading.Thread(
            target=cls.server._server.serve_forever, daemon=True
        )
        cls.thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server._server.shutdown()
        cls.server._server.server_close()
        cls.server.daydream_scheduler.stop()
        cls.server.store.close()
        cls.tmp.cleanup()

    def test_health_reports_counts_and_scheduler(self):
        with urllib.request.urlopen(
            "http://127.0.0.1:11443/api/health", timeout=20
        ) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
        self.assertTrue(data["ok"])
        for key in (
            "version",
            "model",
            "cache_entries",
            "handoffs",
            "inputs",
            "unprocessed_inputs",
            "intents",
            "daydream",
        ):
            self.assertIn(key, data)
        self.assertTrue(data["daydream"]["running"])
        self.assertEqual(data["unprocessed_inputs"], 0)

    def test_recorded_input_rearms_scheduler(self):
        # Simulate 3s of idle so the assertion is meaningful regardless of
        # suite timing: only a real rearm pulls the clock back under 1s.
        scheduler = self.server.daydream_scheduler
        scheduler._last_activity_ns = time.perf_counter_ns() - 3_000_000_000
        self.assertGreaterEqual(scheduler.idle_for(), 3.0)
        request = urllib.request.Request(
            "http://127.0.0.1:11443/api/inputs",
            data=json.dumps({"text": "health feed probe"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        self.assertTrue(data["ok"])
        time.sleep(0.2)
        self.assertLess(scheduler.idle_for(), 1.0)


class TestDoctorChecks(unittest.TestCase):
    def setUp(self):
        self.config = Config()

    def test_undermind_check_up_shape(self):
        original = doctor._http_json
        doctor._http_json = lambda url, timeout=3.0: (
            200,
            {
                "ok": True,
                "version": "9.9.9",
                "inputs": 3,
                "unprocessed_inputs": 1,
                "intents": 2,
                "cache_entries": 4,
                "daydream": {"running": True, "cycles_run": 7},
            },
        )
        try:
            result = doctor.check_undermind(self.config)
        finally:
            doctor._http_json = original
        self.assertEqual(result["status"], "up")
        self.assertIn("mining", result["detail"])

    def test_ollama_down_reports_dead(self):
        def boom(url, timeout=3.0):
            raise ConnectionError("refused")

        original = doctor._http_json
        doctor._http_json = boom
        try:
            result = doctor.check_ollama(self.config)
        finally:
            doctor._http_json = original
        self.assertEqual(result["status"], "dead")

    def test_ollama_missing_model_reports_degraded(self):
        original = doctor._http_json
        doctor._http_json = lambda url, timeout=3.0: (
            200,
            {"models": [{"name": "other:latest"}]},
        )
        try:
            result = doctor.check_ollama(self.config)
        finally:
            doctor._http_json = original
        self.assertEqual(result["status"], "degraded")
        self.assertIn("not loaded", result["detail"])

    def test_lemonade_missing_model_reports_degraded(self):
        original = doctor._http_json
        doctor._http_json = lambda url, timeout=3.0: (
            200,
            {"data": [{"id": "not-the-draft"}]},
        )
        try:
            result = doctor.check_lemonade(self.config)
        finally:
            doctor._http_json = original
        self.assertEqual(result["status"], "degraded")

    def test_omniroute_401_counts_as_up(self):
        original = doctor._http_status
        doctor._http_status = lambda url, timeout=3.0: 401
        try:
            result = doctor.check_omniroute(self.config)
        finally:
            doctor._http_status = original
        self.assertEqual(result["status"], "up")

    def test_hermes_no_pids_reports_dead(self):
        original = doctor._hermes_gateway_pids
        doctor._hermes_gateway_pids = lambda: []
        try:
            result = doctor.check_hermes_gateway(self.config)
        finally:
            doctor._hermes_gateway_pids = original
        self.assertEqual(result["status"], "dead")

    def test_hermes_many_durable_pids_reports_orphaned(self):
        original = doctor._hermes_gateway_pids
        doctor._hermes_gateway_pids = lambda: [(1, 9999), (2, 8888), (3, 7777)]
        try:
            result = doctor.check_hermes_gateway(self.config)
        finally:
            doctor._hermes_gateway_pids = original
        self.assertEqual(result["status"], "orphaned")

    def test_hermes_single_durable_pid_reports_up(self):
        original = doctor._hermes_gateway_pids
        doctor._hermes_gateway_pids = lambda: [(4242, 9999)]
        try:
            result = doctor.check_hermes_gateway(self.config)
        finally:
            doctor._hermes_gateway_pids = original
        self.assertEqual(result["status"], "up")

    def test_hermes_transient_only_reports_degraded(self):
        original = doctor._hermes_gateway_pids
        doctor._hermes_gateway_pids = lambda: [(7976, 5)]
        try:
            result = doctor.check_hermes_gateway(self.config)
        finally:
            doctor._hermes_gateway_pids = original
        self.assertEqual(result["status"], "degraded")

    def test_hermes_durable_pair_plus_transient_reports_up(self):
        original = doctor._hermes_gateway_pids
        doctor._hermes_gateway_pids = lambda: [(10472, 99999), (11296, 99999), (7976, 3)]
        try:
            result = doctor.check_hermes_gateway(self.config)
        finally:
            doctor._hermes_gateway_pids = original
        self.assertEqual(result["status"], "up")

    def test_run_doctor_fail_open_and_exit_code(self):
        def broken(config):
            raise RuntimeError("probe exploded")

        original = doctor.CHECKS
        doctor.CHECKS = (broken,)
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                code = doctor.run_doctor(self.config)
        finally:
            doctor.CHECKS = original
        self.assertEqual(code, 0)  # degraded, not dead
        self.assertIn("degraded", buffer.getvalue())


class TestDoctorAlerting(unittest.TestCase):
    """Status file + alert log + serving-mix awareness."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config()
        self.config.store.db_path = os.path.join(self.tmp.name, "alert.db")

    def tearDown(self):
        doctor._alert_state.clear()
        self.tmp.cleanup()

    @staticmethod
    def _results(*statuses):
        return [
            {"name": f"pipe{i}", "status": s, "detail": f"{s} detail"}
            for i, s in enumerate(statuses)
        ]

    def test_status_file_written_and_verdicts(self):
        doctor._write_status_file(
            self.config, self._results("up", "up"), []
        )
        payload = json.loads(
            (Path(self.tmp.name) / "doctor_status.json").read_text()
        )
        self.assertEqual(payload["verdict"], "up")
        self.assertEqual(payload["dead"], [])

        doctor._write_status_file(
            self.config, self._results("up", "dead"), ["pipe1"]
        )
        payload = json.loads(
            (Path(self.tmp.name) / "doctor_status.json").read_text()
        )
        self.assertEqual(payload["verdict"], "dead")
        self.assertEqual(payload["dead"], ["pipe1"])

    def test_alert_log_records_dead_then_recovers(self):
        doctor._write_alerts(self.config, self._results("dead"), ["pipe0"])
        log = (Path(self.tmp.name) / "doctor_alerts.log").read_text()
        self.assertIn("DEAD pipe0", log)

        doctor._write_alerts(self.config, self._results("up"), [])
        log = (Path(self.tmp.name) / "doctor_alerts.log").read_text()
        self.assertIn("RECOVERED pipe0", log)

    def test_dead_alerts_suppressed_within_window(self):
        doctor._write_alerts(self.config, self._results("dead"), ["pipe0"])
        doctor._write_alerts(self.config, self._results("dead"), ["pipe0"])
        log = (Path(self.tmp.name) / "doctor_alerts.log").read_text()
        self.assertEqual(log.count("DEAD pipe0"), 1)

    def test_alert_writers_fail_open(self):
        # Unwritable directory must not raise.
        self.config.store.db_path = os.path.join(
            self.tmp.name, "no", "such", "dir", "x.db"
        )
        doctor._write_status_file(self.config, self._results("up"), [])
        doctor._write_alerts(self.config, self._results("up"), [])

    def test_undermind_check_riding_fallback_degraded(self):
        health = {
            "ok": True,
            "version": "9.9.9",
            "cache_entries": 0,
            "inputs": 1,
            "unprocessed_inputs": 0,
            "daydream": {"running": False, "cycles_run": 0},
            "serving": {
                "recent_count": 4,
                "models": {"Bonsai-4B-Q1_0": 3, "bonsai-27b-1bit:latest": 1},
                "median_latency_ms": 400.0,
                "riding_fallback": True,
            },
        }
        original = doctor._http_json
        doctor._http_json = lambda url, timeout=3.0: (200, health)
        try:
            result = doctor.check_undermind(self.config)
        finally:
            doctor._http_json = original
        self.assertEqual(result["status"], "degraded")
        self.assertIn("RIDING FALLBACK", result["detail"])

    def test_undermind_check_primary_slow_but_up(self):
        health = {
            "ok": True,
            "version": "9.9.9",
            "cache_entries": 0,
            "inputs": 1,
            "unprocessed_inputs": 0,
            "daydream": {"running": False, "cycles_run": 0},
            "serving": {
                "recent_count": 2,
                "models": {"bonsai-27b-1bit:latest": 2},
                "median_latency_ms": float(doctor.PRIMARY_SLOW_MS) + 1,
                "riding_fallback": False,
            },
        }
        original = doctor._http_json
        doctor._http_json = lambda url, timeout=3.0: (200, health)
        try:
            result = doctor.check_undermind(self.config)
        finally:
            doctor._http_json = original
        self.assertEqual(result["status"], "up")
        self.assertIn("primary slow", result["detail"])

    def test_run_doctor_writes_status_file(self):
        original = doctor.CHECKS
        doctor.CHECKS = (
            lambda config: {"name": "pipe0", "status": "up", "detail": "ok"},
        )
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                code = doctor.run_doctor(self.config)
        finally:
            doctor.CHECKS = original
        self.assertEqual(code, 0)
        self.assertTrue(
            (Path(self.tmp.name) / "doctor_status.json").exists()
        )


class TestWatchdogGuard(unittest.TestCase):
    def test_run_forever_noops_when_port_busy(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        tmp = tempfile.TemporaryDirectory()
        try:
            config = Config()
            config.store.db_path = os.path.join(tmp.name, "guard.db")
            server = ProxyServer(host="127.0.0.1", port=port, config=config)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = server.run_forever()
            self.assertEqual(code, 0)
            self.assertIn("already serving", buffer.getvalue())
        finally:
            blocker.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
