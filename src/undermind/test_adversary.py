"""
Tests for the Adversary: watch-only critic over completed deep turns.

Covers the verdict parser contract, the compact brief, trigger semantics
(shadow-only, deep-only, draft-length floor), fail-open behavior, store
round-trips, and the /api/adversary-notes endpoint. All offline: the critic
client is a fake; no engine is ever contacted.

Windows notes (both learned the hard way):
- SQLite stores hold open file handles; register store.close() as test
  cleanup or TemporaryDirectory deletion fails with WinError 32.
- When stopping a ThreadingHTTPServer in cleanups, register join() FIRST
  and shutdown() SECOND: cleanups run LIFO, so shutdown stops the serving
  loop before join waits on the thread. The reverse order deadlocks.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind.adversary import Adversary, build_brief, extract_verdict
from undermind.config import Config
from undermind.store import UndermindStore


class _Cfg:
    """Minimal stand-in for config.adversary in parser/brief tests."""

    def __init__(self, **kw):
        self.user_chars = 500
        self.max_draft_chars = 4000
        self.min_draft_chars = 200
        self.timeout_s = 5.0
        self.health_cache_s = 60.0
        for k, v in kw.items():
            setattr(self, k, v)


class TestExtractVerdict(unittest.TestCase):
    def test_bare_ok(self):
        self.assertEqual(extract_verdict('{"verdict": "OK"}'), {"verdict": "OK"})

    def test_ok_embedded_in_prose(self):
        raw = 'The draft seems fine. {"verdict": "OK"} Hope that helps.'
        self.assertEqual(extract_verdict(raw), {"verdict": "OK"})

    def test_revise_full(self):
        raw = (
            '{"verdict": "REVISE", "category": "Restated_Question",'
            ' "issue": "The draft promises instead of answering."}'
        )
        self.assertEqual(
            extract_verdict(raw),
            {
                "verdict": "REVISE",
                "category": "restated_question",
                "issue": "The draft promises instead of answering.",
            },
        )

    def test_empty_is_skip(self):
        self.assertIsNone(extract_verdict(""))

    def test_garbage_is_skip(self):
        self.assertIsNone(extract_verdict("I cannot comply with that request."))

    def test_unknown_category_is_skip(self):
        raw = '{"verdict": "REVISE", "category": "vibes", "issue": "x"}'
        self.assertIsNone(extract_verdict(raw))

    def test_revise_without_issue_is_skip(self):
        raw = '{"verdict": "REVISE", "category": "contradiction"}'
        self.assertIsNone(extract_verdict(raw))

    def test_issue_truncated_to_300(self):
        raw = json.dumps(
            {"verdict": "REVISE", "category": "missed_ask", "issue": "x" * 400}
        )
        out = extract_verdict(raw)
        self.assertEqual(len(out["issue"]), 300)

    def test_non_json_dict_is_skip(self):
        self.assertIsNone(extract_verdict("[1, 2, 3]"))


class TestBuildBrief(unittest.TestCase):
    def test_contains_ask_and_draft(self):
        cfg = _Cfg()
        brief = build_brief("what changed?", "Lots changed, notably X.", cfg)
        self.assertIn("what changed?", brief)
        self.assertIn("Lots changed, notably X.", brief)

    def test_ask_clamped(self):
        cfg = _Cfg(user_chars=10)
        brief = build_brief("a" * 100, "draft", cfg)
        self.assertIn("a" * 10, brief)
        self.assertNotIn("a" * 11, brief)

    def test_draft_tail_marked(self):
        cfg = _Cfg(max_draft_chars=50)
        brief = build_brief("ask", "d" * 100, cfg)
        self.assertTrue(brief.rstrip().endswith("..."))
        self.assertNotIn("d" * 51, brief)


class _FakeClient:
    """Stands in for the critic provider; records calls, returns scripted text."""

    def __init__(self, behavior="ok"):
        self.calls = []
        self.behavior = behavior

    def execute(self, branch, context=None):
        self.calls.append({"branch": branch, "context": context})
        if self.behavior == "ok":
            return '{"verdict": "OK"}'
        if self.behavior == "revise":
            return (
                '{"verdict": "REVISE", "category": "unfounded_claim",'
                ' "issue": "Asserts a version number with no source."}'
            )
        if self.behavior == "garbage":
            return "no json here"
        if self.behavior == "raise":
            raise RuntimeError("engine wedged")
        return ""


def _make_adversary(case, tmpdir, mode="shadow", client=None):
    """Build (store, cfg, Adversary) with the store closed on cleanup."""
    store = UndermindStore(os.path.join(tmpdir, "test.db"))
    case.addCleanup(store.close)
    cfg = Config()
    cfg.adversary.mode = mode
    cfg.adversary.timeout_s = 2.0
    adversary = Adversary(store=store, config=cfg, client=client)
    return store, cfg, adversary


class TestAdversaryTriggers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = self._tmp.name

    def test_off_mode_never_calls(self):
        client = _FakeClient()
        store, cfg, adv = _make_adversary(self, self.tmpdir, mode="off", client=client)
        adv.review("ask", "d" * 300, 1)
        self.assertEqual(client.calls, [])
        self.assertEqual(store.critique_stats()["recent"], 0)

    def test_shadow_records_ok_verdict(self):
        client = _FakeClient("ok")
        store, cfg, adv = _make_adversary(self, self.tmpdir, mode="shadow", client=client)
        adv.review("ask", "d" * 300, 7)
        self.assertEqual(len(client.calls), 1)
        stats = store.critique_stats()
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(stats["recent"], 1)

    def test_short_draft_not_critiqued(self):
        client = _FakeClient("ok")
        store, cfg, adv = _make_adversary(self, self.tmpdir, mode="shadow", client=client)
        adv.review("ask", "short", 1)
        self.assertEqual(client.calls, [])
        self.assertEqual(store.critique_stats()["recent"], 0)

    def test_garbage_verdict_records_skip(self):
        client = _FakeClient("garbage")
        store, cfg, adv = _make_adversary(self, self.tmpdir, mode="shadow", client=client)
        adv.review("ask", "d" * 300, 2)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(store.critique_stats()["skip"], 1)

    def test_raising_client_records_skip(self):
        client = _FakeClient("raise")
        store, cfg, adv = _make_adversary(self, self.tmpdir, mode="shadow", client=client)
        adv.review("ask", "d" * 300, 3)  # must not raise
        self.assertEqual(store.critique_stats()["skip"], 1)

    def test_review_async_respects_mode(self):
        client = _FakeClient("ok")
        store, cfg, adv = _make_adversary(self, self.tmpdir, mode="off", client=client)
        adv.review_async("ask", "d" * 300, 1)  # returns immediately, no work
        self.assertEqual(client.calls, [])

    def test_review_async_shadow_records(self):
        client = _FakeClient("ok")
        store, cfg, adv = _make_adversary(self, self.tmpdir, mode="shadow", client=client)
        adv.review_async("ask", "d" * 300, 8)
        deadline = 50  # generous: a daemon thread + one fake call
        for _ in range(deadline):
            if store.critique_stats()["recent"]:
                break
            import time

            time.sleep(0.1)
        self.assertEqual(store.critique_stats()["ok"], 1)


class TestAdversaryStoreRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = self._tmp.name

    def _store(self):
        store = UndermindStore(os.path.join(self.tmpdir, f"t{len(self._tmp.__dict__)}.db"))
        self.addCleanup(store.close)
        return store

    def test_revise_row_carries_category_issue_draft(self):
        store = self._store()
        store.record_critique(
            handoff_id=11,
            mode="shadow",
            verdict="REVISE",
            category="restated_question",
            issue="It asks back.",
            draft_text="d" * 250,
            critic_latency_ms=12.5,
        )
        rows = store.recent_issues(3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "restated_question")
        self.assertEqual(rows[0]["issue"], "It asks back.")
        stats = store.critique_stats()
        self.assertEqual(stats["revise"], 1)
        self.assertEqual(stats["ok"], 0)

    def test_ok_rows_not_in_recent_issues(self):
        store = self._store()
        store.record_critique(handoff_id=1, mode="shadow", verdict="OK")
        self.assertEqual(store.recent_issues(3), [])
        self.assertEqual(store.critique_stats()["ok"], 1)

    def test_stats_window_only_counts_recent(self):
        store = self._store()
        for i in range(5):
            store.record_critique(handoff_id=i, mode="shadow", verdict="OK")
        store.record_critique(
            handoff_id=99, mode="shadow", verdict="REVISE",
            category="missed_ask", issue="wrong thing",
        )
        stats = store.critique_stats(window=3)
        self.assertEqual(stats["recent"], 3)
        self.assertEqual(stats["revise"], 1)
        self.assertEqual(stats["ok"], 2)


class TestNotesEndpoint(unittest.TestCase):
    """The /api/adversary-notes route served by a real hermetic server."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmpdir = cls._tmp.name
        from undermind.proxy import ProxyServer

        config = Config()
        config.store.db_path = os.path.join(cls.tmpdir, "notes.db")
        config.proxy.port = 11453
        cls.server = ProxyServer(host="127.0.0.1", port=11453, config=config)
        # Seed one REVISE row so the endpoint has something to surface.
        cls.server.store.record_critique(
            handoff_id=5,
            mode="shadow",
            verdict="REVISE",
            category="unfounded_claim",
            issue="Names a file that was never created.",
        )
        handler = cls.server._handler_factory()
        cls.http = ThreadingHTTPServer(("127.0.0.1", 11453), handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        # LIFO cleanups: shutdown stops the serving loop, join reaps the
        # thread, the store closes last so Windows can delete the temp dir.
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(cls.server.store.close)
        cls.addClassCleanup(cls.thread.join)
        cls.addClassCleanup(cls.http.server_close)
        cls.addClassCleanup(cls.http.shutdown)

    @classmethod
    def tearDownClass(cls):
        pass  # cleanup handled via classCleanups

    def test_notes_endpoint_shape(self):
        with urllib.request.urlopen(
            "http://127.0.0.1:11453/api/adversary-notes", timeout=10
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        notes = data["notes"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["category"], "unfounded_claim")
        self.assertIn("never created", notes[0]["issue"])

    def test_health_includes_adversary_block(self):
        with urllib.request.urlopen(
            "http://127.0.0.1:11453/api/health", timeout=10
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertIn("adversary", data)
        self.assertEqual(data["adversary"]["mode"], "off")
        self.assertEqual(data["adversary"]["stats"]["revise"], 1)


if __name__ == "__main__":
    unittest.main()
