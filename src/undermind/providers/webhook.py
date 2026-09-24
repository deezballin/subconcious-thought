"""
Webhook provider: POST the handed-off branch as JSON to any HTTP endpoint.

This is the universal escape hatch for primary pipelines that are neither
OpenAI-compatible nor Ollama-native (custom agents, n8n flows, home servers).
"""

from __future__ import annotations

from typing import Any

import requests

from undermind.providers.base import ProviderError


class WebhookProvider:
    """Executes a branch by POSTing a JSON envelope to a user-defined URL."""

    def __init__(self, url: str, api_key: str = "", timeout_s: float = 120.0) -> None:
        if not url:
            raise ProviderError(
                "WebhookProvider requires primary.webhook_url in the config"
            )
        self.url = url
        self.api_key = api_key
        self.timeout_s = timeout_s

    def execute(self, branch: str, context: str | None = None) -> str:
        """POST {branch, context, timestamp} and return the reply text.

        The response is parsed as JSON and read from, in order: ``response``,
        ``output``, ``result``, ``text``, ``content``; otherwise the raw body.
        """
        envelope: dict[str, Any] = {
            "branch": branch,
            "context": context or "",
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = requests.post(
                self.url,
                json=envelope,
                headers=headers,
                timeout=self.timeout_s,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(f"Webhook {self.url} failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            return response.text.strip()

        if isinstance(data, dict):
            for key in ("response", "output", "result", "text", "content"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return response.text.strip()
