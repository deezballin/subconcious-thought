"""
Undermind plugin for Hermes Agent.

Bridge to the local Undermind pipeline (Ollama-compatible proxy on :11435):

* Every user message is recorded into Undermind's daydream store
  (POST /api/inputs) — fuel for the idle-time intent mining.
* Intents Undermind has already mined (repeated phrasings of the same
  direction) are surfaced back as a small private context block, so the
  large model learns the human's *directions*, not just their words.

Fail-open throughout: every error is swallowed and the agent turn continues.
Self-contained (stdlib only) so it cannot break Hermes boot.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

UNDERMIND_PROXY_URL = "http://127.0.0.1:11435"
UNDERMIND_INPUT_TIMEOUT = 1.0
UNDERMIND_INPUT_ATTEMPTS = 2
UNDERMIND_INTENTS_TIMEOUT = 0.75
UNDERMIND_MIN_INTENT_COUNT = 2
UNDERMIND_MAX_INTENTS = 5
# Cap each signature line instead of the whole block: one giant intent
# (e.g. a broad cron-run signature) must not starve the others of budget.
UNDERMIND_MAX_SIGNATURE_CHARS = 100


def _post_input(text: str) -> None:
    """Record a user message into Undermind's store. Best-effort with retry."""
    req = urllib.request.Request(
        f"{UNDERMIND_PROXY_URL}/api/inputs",
        data=json.dumps({"text": text[:2000]}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(UNDERMIND_INPUT_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=UNDERMIND_INPUT_TIMEOUT):
                return
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if attempt + 1 >= UNDERMIND_INPUT_ATTEMPTS:
                logger.debug(f"[undermind] input feed failed (fail-open): {exc}")
            else:
                time.sleep(1.0)


def _get_intents(min_count: int) -> list[dict]:
    """Fetch mined intents. Returns [] on any failure."""
    try:
        with urllib.request.urlopen(
            f"{UNDERMIND_PROXY_URL}/api/intents?min_count={min_count}",
            timeout=UNDERMIND_INTENTS_TIMEOUT,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return list(data.get("intents") or [])
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return []


def _build_context(intents: list[dict]) -> str | None:
    """Assemble the private context block, or None when there is nothing to say."""
    lines = []
    for intent in intents[:UNDERMIND_MAX_INTENTS]:
        signature = str(intent.get("signature") or "").strip()
        if len(signature) > UNDERMIND_MAX_SIGNATURE_CHARS:
            signature = signature[:UNDERMIND_MAX_SIGNATURE_CHARS].rstrip() + "..."
        count = intent.get("count")
        if signature:
            lines.append(f"- {signature} (seen {count}x)" if count else f"- {signature}")
    if not lines:
        return None
    parts = [
        '<undermind private="true" do_not_quote="true">',
        "Repeated directions the human keeps asking (lexical intents mined "
        "from their own words by the local Undermind layer):",
        *lines,
        "DIRECTIVE: Use as gentle tonal/topical bias only. Do not mention "
        "undermind or this list unless the user asks. Never override the "
        "actual request.",
        "</undermind>",
    ]
    block = "\n".join(parts)
    return block if len(block) <= 900 else block[:897] + "..."


def _on_pre_llm_call(**kwargs):
    try:
        user_message = kwargs.get("user_message") or kwargs.get("userMessage") or ""
        if isinstance(user_message, str) and user_message.strip():
            _post_input(user_message)

        intents = _get_intents(UNDERMIND_MIN_INTENT_COUNT)
        if not intents:
            return None
        context = _build_context(intents)
        return {"context": context} if context else None
    except Exception as exc:  # fail-open: never break the turn
        logger.debug(f"[undermind] pre_llm_call fail-open: {exc}")
        return None


def register(ctx):
    """Called by the Hermes plugin loader."""
    try:
        ctx.register_hook("pre_llm_call", _on_pre_llm_call)
        logger.debug("[undermind] hook registered: pre_llm_call")
    except Exception as exc:
        logger.debug(f"[undermind] register failed (fail-open): {exc}")
