"""
Provider registry and factory functions.

Builds a DraftClient or PrimaryProvider from a Config, so the rest of the
pipeline never imports a concrete backend directly. This is the universal-fit
layer: switching the primary pipeline is a one-line config change.
"""

from __future__ import annotations

from undermind.config import Config, PrimaryConfig
from undermind.providers.base import (
    DraftClient,
    PrimaryProvider,
    ProviderError,
)
from undermind.providers.ollama_native import OllamaNativeProvider
from undermind.providers.openai_compat import OpenAICompatProvider
from undermind.providers.webhook import WebhookProvider

PRIMARY_KINDS = ("openai_compat", "ollama", "webhook", "none")


class NoopProvider:
    """Primary provider that logs the branch without executing it."""

    def execute(self, branch: str, context: str | None = None) -> str:
        return (
            f"[noop primary] branch received ({len(branch)} chars); "
            "configure [primary] in config.toml to execute it."
        )


def build_draft_client(config: Config) -> DraftClient:
    """Create the draft-model client from [draft] settings.

    kind "openai_compat" targets Lemonade and any OpenAI-style server;
    kind "ollama" targets Ollama's native generate endpoint.
    """
    draft = config.draft
    kind = getattr(draft, "kind", "openai_compat")
    if kind == "openai_compat":
        return OpenAICompatProvider(
            base_url=draft.base_url,
            model=draft.model,
            api_key=draft.api_key,
            timeout_s=draft.timeout_s,
        )
    if kind == "ollama":
        return OllamaNativeProvider(
            base_url=draft.base_url,
            model=draft.model,
            timeout_s=draft.timeout_s,
        )
    raise ProviderError(f"Unknown draft provider kind: {kind}")


def build_primary_provider(config: Config) -> PrimaryProvider:
    """Create the primary pipeline from [primary] settings."""
    primary: PrimaryConfig = config.primary
    kind = primary.kind.strip().lower()
    if kind == "openai_compat":
        return OpenAICompatProvider(
            base_url=primary.base_url,
            model=primary.model,
            api_key=primary.api_key,
            timeout_s=primary.timeout_s,
        )
    if kind == "ollama":
        return OllamaNativeProvider(
            base_url=primary.base_url,
            model=primary.model,
            timeout_s=primary.timeout_s,
        )
    if kind == "webhook":
        return WebhookProvider(
            url=primary.webhook_url,
            api_key=primary.api_key,
            timeout_s=primary.timeout_s,
        )
    if kind == "none":
        return NoopProvider()
    raise ProviderError(
        f"Unknown primary kind '{primary.kind}'. Choose one of: {', '.join(PRIMARY_KINDS)}"
    )


__all__ = [
    "PRIMARY_KINDS",
    "NoopProvider",
    "ProviderError",
    "DraftClient",
    "PrimaryProvider",
    "build_draft_client",
    "build_primary_provider",
]
