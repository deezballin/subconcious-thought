"""
The Adversary: a watch-only critic over completed deep turns.

Per Dewayne's decisions (2026-09-27): the critic seat is the NPU 4B already
served by Lemonade (no new model); it reviews turns AFTER they ship and
records a verdict — OK, REVISE (named category + one-sentence issue), or
SKIP. It never rewrites, blocks, or gates a reply: shadow mode only. The
bridge plugin surfaces recent REVISE issues to Kairos as offered memory so
his own failure shapes are his to weigh.

Fail-open everywhere: a missing engine, a timeout, or a garbled verdict
produces a SKIP row (or no row at all) and never disturbs the turn. With
mode="off" (the shipped default) the class is fully inert.

Design doc: docs/ADVERSARY_DESIGN.md (the rewrite path there was descoped
by decision — this build is watch-only).
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Optional

CRITIC_SYSTEM_PROMPT = (
    "You are the Adversary, a strict internal critic. Review the draft "
    "reply against the user's actual ask. If the draft has exactly one of "
    "these flaws, name it: contradiction (disagrees with itself), "
    "restated_question (asks or restates instead of answering), "
    "unfounded_claim (asserts a specific fact with no basis), missed_ask "
    "(answers something the user did not ask). Otherwise pass it. Output "
    "ONLY JSON, one of:\n"
    '{"verdict": "OK"}\n'
    '{"verdict": "REVISE", "category": "<flaw>", "issue": "<one sentence>"}'
)

CATEGORIES = frozenset(
    {"contradiction", "restated_question", "unfounded_claim", "missed_ask"}
)

_JSON_BLOCK = re.compile(r"\{[^{}]*\}", re.DOTALL)


def extract_verdict(raw: str) -> Optional[dict]:
    """Parse a critic reply into a verdict dict, or None (meaning SKIP).

    Accepts a JSON object either bare or embedded in surrounding prose.
    Anything else — empty, non-JSON, wrong shape, unknown category, REVISE
    without a named issue — is None. The critic gets no second chance
    within a turn (single-cycle invariant).
    """
    if not raw or not raw.strip():
        return None
    match = _JSON_BLOCK.search(raw)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict == "OK":
        return {"verdict": "OK"}
    if verdict != "REVISE":
        return None
    category = str(data.get("category", "")).strip().lower()
    issue = str(data.get("issue", "")).strip()
    if category not in CATEGORIES or not issue:
        return None
    return {"verdict": "REVISE", "category": category, "issue": issue[:300]}


def build_brief(user_ask: str, draft: str, cfg) -> str:
    """The compact critic brief: the ask and the draft, tightly bounded."""
    ask = (user_ask or "").strip()[: cfg.user_chars]
    body = (draft or "").strip()
    if len(body) > cfg.max_draft_chars:
        body = body[: cfg.max_draft_chars] + "..."
    return (
        f"User's actual ask:\n{ask}\n\n"
        f"Draft reply to review:\n{body}"
    )


class Adversary:
    """Watch-only critic client. One instance, owned by the proxy server.

    The critic runs on its own daemon thread AFTER the reply has shipped,
    so the turn path pays zero latency for the critique. All failures are
    silent and fail-open; the worst case is a missing or SKIP row.
    """

    def __init__(self, store, config, client=None) -> None:
        self.store = store
        self.config = config
        self._client = client  # injected (tests); built lazily otherwise
        self._health: tuple[float, bool] = (0.0, False)

    # -- mode / seat ----------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True only in shadow mode. (\"on\" is not offered in this build.)"""
        return self.config.adversary.mode == "shadow"

    def _make_client(self):
        """Build the critic client from config (override in tests)."""
        if self._client is not None:
            return self._client
        from undermind.providers.openai_compat import OpenAICompatProvider

        cfg = self.config.adversary
        return OpenAICompatProvider(
            base_url=cfg.base_url,
            model=cfg.model,
            api_key=cfg.api_key,
            timeout_s=cfg.timeout_s,
            stall_timeout_s=0.0,
        )

    def _endpoint_up(self) -> bool:
        """Cached TCP probe of the critic seat; a down seat means no call,
        no 20s tax per turn (fail-fast beats fail-slow here)."""
        now = time.monotonic()
        checked_at, up = self._health
        if now - checked_at < self.config.adversary.health_cache_s:
            return up
        from urllib.parse import urlparse

        parsed = urlparse(self.config.adversary.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        try:
            with socket.create_connection((host, port), timeout=1.0):
                up = True
        except OSError:
            up = False
        self._health = (now, up)
        return up

    # -- entry points ----------------------------------------------------

    def review_async(self, user_ask: str, draft: str, handoff_id: Optional[int]) -> None:
        """Fire the critique on a daemon thread; returns immediately."""
        if not self.enabled:
            return
        worker = threading.Thread(
            target=self.review,
            args=(user_ask, draft, handoff_id),
            daemon=True,
            name="undermind-adversary",
        )
        worker.start()

    def review(self, user_ask: str, draft: str, handoff_id: Optional[int]) -> None:
        """Critique one completed deep turn. Watch-only; never raises."""
        try:
            self._review(user_ask, draft, handoff_id)
        except Exception:
            pass  # fail-open: the critique must never disturb anything

    def _review(self, user_ask: str, draft: str, handoff_id: Optional[int]) -> None:
        cfg = self.config.adversary
        if not self.enabled:
            return
        if len((draft or "").strip()) < cfg.min_draft_chars:
            return
        if not self._endpoint_up():
            return

        outcome = self._run_critic(user_ask, draft)
        if outcome is None:
            self.store.record_critique(
                handoff_id=handoff_id,
                mode=cfg.mode,
                verdict="SKIP",
            )
            return
        self.store.record_critique(
            handoff_id=handoff_id,
            mode=cfg.mode,
            verdict=outcome["verdict"],
            category=outcome.get("category"),
            issue=outcome.get("issue"),
            draft_text=draft,
            critic_latency_ms=outcome.get("latency_ms"),
        )

    def _run_critic(self, user_ask: str, draft: str) -> Optional[dict]:
        """One critic call on a daemon thread with a hard wall cap."""
        try:
            client = self._make_client()
        except Exception:
            return None

        brief = build_brief(user_ask, draft, self.config.adversary)
        result: dict = {}

        def _work() -> None:
            start = time.perf_counter_ns()
            try:
                raw = client.execute(brief, context=CRITIC_SYSTEM_PROMPT)
            except Exception:
                return
            result["latency_ms"] = (time.perf_counter_ns() - start) / 1e6
            result["verdict"] = extract_verdict(raw)

        cfg = self.config.adversary
        worker = threading.Thread(
            target=_work, daemon=True, name="undermind-adversary-call"
        )
        worker.start()
        worker.join(timeout=cfg.timeout_s + 1.0)
        if "verdict" not in result or result["verdict"] is None:
            return None
        verdict: dict = result["verdict"]
        verdict["latency_ms"] = result.get("latency_ms")
        return verdict
