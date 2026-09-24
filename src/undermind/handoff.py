"""
Fast handoff: send the confident text branch to the primary LLM pipeline.

The exact millisecond the confidence threshold is crossed, the local draft
loop is bypassed and UniversalHandoff routes the entire text branch to the
configured primary provider for immediate execution. Results, latency, and
errors are recorded to the SQLite handoffs table.
"""

from __future__ import annotations

import time
from typing import Optional

from undermind.providers.base import PrimaryProvider, ProviderError


class HandoffError(RuntimeError):
    """Raised when the primary pipeline fails to execute a branch."""


class UniversalHandoff:
    """Routes confident text branches to the primary LLM context pipeline."""

    def __init__(
        self,
        provider: PrimaryProvider,
        provider_name: str,
        model: str,
        system_prompt: str = "",
        retries: int = 1,
    ) -> None:
        self.provider = provider
        self.provider_name = provider_name
        self.model = model
        self.system_prompt = system_prompt
        self.retries = retries

    def send(
        self,
        branch: str,
        trigger: str = "confidence_95",
        confidence: Optional[float] = None,
    ) -> str:
        """Execute the branch on the primary pipeline and return the result.

        Raises HandoffError after exhausting retries. Callers that need the
        outcome recorded should use ``send_and_record``.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return self.provider.execute(branch, context=self.system_prompt or None)
            except ProviderError as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        raise HandoffError(f"Primary pipeline failed: {last_exc}") from last_exc

    def send_and_record(
        self,
        branch: str,
        store,
        trigger: str = "confidence_95",
        confidence: Optional[float] = None,
        started_monotonic_ns: Optional[int] = None,
    ) -> str:
        """Execute and persist the outcome to the handoffs table.

        Returns the primary result string; errors are recorded and re-raised
        as HandoffError.
        """
        start = started_monotonic_ns or time.perf_counter_ns()
        try:
            result = self.send(branch, trigger=trigger, confidence=confidence)
        except HandoffError as exc:
            store.record_handoff(
                trigger=trigger,
                branch=branch,
                provider=self.provider_name,
                model=self.model,
                status="error",
                confidence=confidence,
                latency_ms=(time.perf_counter_ns() - start) / 1e6,
                error=str(exc),
            )
            raise
        store.record_handoff(
            trigger=trigger,
            branch=branch,
            provider=self.provider_name,
            model=self.model,
            status="ok",
            confidence=confidence,
            latency_ms=(time.perf_counter_ns() - start) / 1e6,
        )
        return result


def preload_note() -> str:
    return (
        "UniversalHandoff replaces the old Ollama-only Handoff. Configure the "
        "primary pipeline in [primary] of config.toml."
    )
