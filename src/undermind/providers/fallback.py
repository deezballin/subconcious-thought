"""
Fallback primary provider: tries providers in order, returns the first success.

Used to seat a featherweight second engine behind the main primary so a hung,
unloaded, or unreachable big model degrades to a smaller one instead of
erroring the turn. Fail-open by construction: every failure is swallowed and
the next provider gets the branch; only an exhausted chain raises.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

from undermind.providers.base import PrimaryProvider, ProviderError

logger = logging.getLogger(__name__)


class FallbackProvider:
    """Execute on the first provider in the chain that answers.

    After each ``execute``, ``last_served`` (index of the provider that
    answered, or None) and ``last_latency_ms`` describe what actually
    happened, so callers can attribute the response and notice when the
    chain is riding a fallback rung instead of the primary.
    """

    def __init__(
        self,
        providers: List[PrimaryProvider],
        per_provider_timeout_s: Optional[float] = None,
    ) -> None:
        if not providers:
            raise ProviderError("FallbackProvider needs at least one provider")
        self.providers = providers
        self.per_provider_timeout_s = per_provider_timeout_s
        self.last_served: Optional[int] = None
        self.last_latency_ms: Optional[float] = None

    def execute(self, branch: str, context: str | None = None) -> str:
        last_exc: Optional[Exception] = None
        self.last_served = None
        self.last_latency_ms = None
        for index, provider in enumerate(self.providers):
            start_ns = time.perf_counter_ns()
            try:
                result = provider.execute(branch, context=context)
                self.last_served = index
                self.last_latency_ms = (time.perf_counter_ns() - start_ns) / 1e6
                return result
            except Exception as exc:  # fail-open: any failure moves down the chain
                last_exc = exc
                logger.warning(
                    "[fallback] provider %d/%d failed: %s",
                    index + 1,
                    len(self.providers),
                    exc,
                )
                if self.per_provider_timeout_s:
                    time.sleep(0)
        raise ProviderError(f"All fallback providers failed: {last_exc}") from last_exc

    @property
    def chain_summary(self) -> str:
        return " -> ".join(type(p).__name__ for p in self.providers)


def build_primary_chain(config) -> Tuple[PrimaryProvider, str]:
    """Build the primary chain from config, honoring optional [primary].fallback_* keys.

    Returns (provider, human-readable chain description). When no fallback is
    configured, this is exactly build_primary_provider(config).
    """
    from undermind.providers import build_primary_provider

    primary = build_primary_provider(config)
    fallback_kind = str(getattr(config.primary, "fallback_kind", "") or "").strip()
    if not fallback_kind:
        return primary, f"{config.primary.kind}:{config.primary.model}"

    fallback_config = _derive_fallback_config(config, fallback_kind)
    if hasattr(config, "primary"):
        from dataclasses import replace

        wrapper = replace(config, primary=fallback_config)
    else:  # bare PrimaryConfig passed in — wrap minimally
        from undermind.config import Config

        wrapper = Config()
        wrapper.primary = fallback_config
    fallback = build_primary_provider(wrapper)
    return (
        FallbackProvider([primary, fallback]),
        f"{config.primary.kind}:{config.primary.model} -> {fallback_kind}:{fallback_config.model}",
    )


def _derive_fallback_config(config, fallback_kind: str):
    """Clone [primary] with the fallback's kind/base_url/model/api_key applied."""
    from dataclasses import replace

    fb = getattr(config, "primary", config)
    return replace(
        fb,
        kind=fallback_kind,
        base_url=str(getattr(fb, "fallback_base_url", "") or fb.base_url),
        model=str(getattr(fb, "fallback_model", "") or fb.model),
        api_key=str(getattr(fb, "fallback_api_key", "") or ""),
    )


__all__ = ["FallbackProvider", "build_primary_chain"]
