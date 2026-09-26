"""
Fallback primary provider: tries providers in order, returns the first success.

Used to seat a featherweight second engine behind the main primary so a hung,
unloaded, or unreachable big model degrades to a smaller one instead of
erroring the turn. Fail-open by construction: every failure is swallowed and
the next provider gets the branch; only an exhausted chain raises.

Two timeout layers keep a hung rung from stalling the turn:

* **Stall guard** (``stall_timeout_s``): bounds the silent gap between tokens
  while a rung streams. Long thinking turns keep emitting tokens, so they are
  never cut; a wedged engine that goes quiet is abandoned quickly. Enforced by
  the provider's socket read timeout (see ollama_native/openai_compat).
* **Wall cap** (``per_provider_timeout_s``): a hard ceiling per rung, either a
  single number for every rung or a sequence aligned with the chain (primary
  rung first). The rung executes on a daemon worker thread; when the cap
  elapses the caller moves on and the worker is abandoned — it cannot block
  process exit, and its socket read timeout reaps it shortly after.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import List, Optional, Sequence, Tuple, Union

from undermind.providers.base import PrimaryProvider, ProviderError

logger = logging.getLogger(__name__)

# A rung timeout: one number for every rung, or one per rung in chain order.
TimeoutSpec = Union[float, int, Sequence[Optional[float]], None]


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
        per_provider_timeout_s: TimeoutSpec = None,
        stall_timeout_s: float = 0.0,
    ) -> None:
        if not providers:
            raise ProviderError("FallbackProvider needs at least one provider")
        self.providers = providers
        self.per_provider_timeout_s = per_provider_timeout_s
        self.stall_timeout_s = stall_timeout_s
        self.last_served: Optional[int] = None
        self.last_latency_ms: Optional[float] = None

    # ------------------------------------------------------------------
    # timeouts
    # ------------------------------------------------------------------

    def _cap_for(self, index: int) -> Optional[float]:
        """Wall-clock cap for rung ``index``; None means uncapped."""
        spec = self.per_provider_timeout_s
        if spec is None:
            return None
        if isinstance(spec, (int, float)):
            return float(spec) or None
        caps = list(spec)
        if index < len(caps):
            cap = caps[index]
            return float(cap) if cap else None
        # More rungs than caps: the last cap covers the tail of the chain.
        cap = caps[-1] if caps else None
        return float(cap) if cap else None

    def _execute_capped(
        self,
        provider: PrimaryProvider,
        branch: str,
        context: Optional[str],
        cap_s: Optional[float],
    ) -> str:
        """Run one rung under an optional wall-clock cap.

        Without a cap this is a plain call. With a cap the rung runs on a
        daemon worker thread; if the cap elapses first, this raises and the
        worker is abandoned (its own socket timeouts eventually reap it).
        """
        if not cap_s or cap_s <= 0:
            return provider.execute(branch, context=context)

        done: "queue.Queue[tuple[str, object]]" = queue.Queue()

        def _work() -> None:
            try:
                done.put(("ok", provider.execute(branch, context=context)))
            except BaseException as exc:  # never let the worker die silently
                done.put(("err", exc))

        worker = threading.Thread(
            target=_work, daemon=True, name="undermind-fallback-rung"
        )
        started = time.perf_counter()
        worker.start()
        try:
            kind, payload = done.get(timeout=cap_s)
        except queue.Empty:
            raise ProviderError(
                f"provider did not answer within {cap_s:.0f}s wall cap"
            ) from None
        if kind == "ok":
            return str(payload)
        raise payload  # the rung's own exception (typically ProviderError)

    # ------------------------------------------------------------------
    # chain
    # ------------------------------------------------------------------

    def execute(self, branch: str, context: str | None = None) -> str:
        last_exc: Optional[Exception] = None
        self.last_served = None
        self.last_latency_ms = None
        for index, provider in enumerate(self.providers):
            cap = self._cap_for(index)
            start_ns = time.perf_counter_ns()
            try:
                result = self._execute_capped(provider, branch, context, cap)
            except Exception as exc:  # fail-open: any failure moves down the chain
                last_exc = exc
                logger.warning(
                    "[fallback] provider %d/%d failed: %s",
                    index + 1,
                    len(self.providers),
                    exc,
                )
                continue
            self.last_served = index
            self.last_latency_ms = (time.perf_counter_ns() - start_ns) / 1e6
            return result
        raise ProviderError(f"All fallback providers failed: {last_exc}") from last_exc

    @property
    def chain_summary(self) -> str:
        return " -> ".join(type(p).__name__ for p in self.providers)


def build_primary_chain(config) -> Tuple[PrimaryProvider, str]:
    """Build the primary chain from config, honoring optional [primary].fallback_* keys.

    Returns (provider, human-readable chain description). When no fallback is
    configured, this is exactly build_primary_provider(config).

    Rung caps come from ``[primary].timeout_s`` (primary rung) and
    ``[primary].fallback_timeout_s`` (fallback rungs); ``stall_timeout_s``
    is handed to streaming-capable rungs so a silent engine trips the
    per-read timeout long before the wall cap.
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

    primary_cap = float(getattr(config.primary, "timeout_s", 0) or 0)
    fallback_cap = float(getattr(config.primary, "fallback_timeout_s", 0) or 0)
    stall = float(getattr(config.primary, "stall_timeout_s", 0) or 0)
    chain = FallbackProvider(
        [primary, fallback],
        per_provider_timeout_s=(primary_cap, fallback_cap),
        stall_timeout_s=stall,
    )
    return (
        chain,
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
        # The fallback rung should be quick: short socket cap + stall guard
        # so an abandoned wall-cap worker reaps promptly.
        timeout_s=min(float(fb.timeout_s or 0) or 60.0, 60.0),
        stall_timeout_s=float(getattr(fb, "stall_timeout_s", 0) or 0) or 30.0,
    )


__all__ = ["FallbackProvider", "build_primary_chain"]
