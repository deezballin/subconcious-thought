"""
OpenAI-compatible provider backend.

Works with Lemonade Server (default, http://localhost:13305), LM Studio,
llama.cpp server, vLLM, Ollama's OpenAI endpoint, and hosted OpenAI-style APIs.
Supports streaming chat completions with per-token logprobs for confidence
tracking, plus non-streaming execution for the primary pipeline.
"""

from __future__ import annotations

import json

import requests

from undermind.providers.base import ProviderError

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


class OpenAICompatProvider:
    """Client for any OpenAI-compatible chat/completions server."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_s: float = 30.0,
        stall_timeout_s: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        # Max silent gap between streamed tokens during execute(); bounds a
        # hung engine without cutting off long thinking turns. 0 = disabled.
        self.stall_timeout_s = stall_timeout_s

    # ------------------------------------------------------------------
    # DraftClient
    # ------------------------------------------------------------------

    def stream_tokens(self, messages, max_tokens, temperature, on_token) -> str:
        """Stream a chat completion, reporting (text, logprob) per token.

        If the stream carries no logprobs (some backends omit them while
        streaming), a single non-streaming request with logprobs enabled is
        issued instead and the tokens are replayed through on_token.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "logprobs": True,
        }
        collected: list[str] = []
        saw_logprobs = False
        try:
            with requests.post(
                f"{self.base_url}{CHAT_COMPLETIONS_PATH}",
                json=payload,
                headers=_headers(self.api_key),
                stream=True,
                timeout=self.timeout_s,
            ) as response:
                if response.status_code != 200:
                    body = response.text[:300]
                    raise ProviderError(
                        f"{self.base_url} returned {response.status_code}: {body}"
                    )
                for raw_line in response.iter_lines():
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    token_text = delta.get("content") or ""
                    if not token_text:
                        continue
                    logprob = self._extract_chunk_logprob(choices[0])
                    if logprob is not None:
                        saw_logprobs = True
                    collected.append(token_text)
                    on_token(token_text, logprob if logprob is not None else 0.0)
        except requests.RequestException as exc:
            raise ProviderError(
                f"Cannot reach {self.base_url}{CHAT_COMPLETIONS_PATH}: {exc}"
            ) from exc

        if saw_logprobs:
            return "".join(collected)
        return self._replay_with_logprobs(messages, max_tokens, temperature, on_token)

    @staticmethod
    def _extract_chunk_logprob(choice: dict) -> float | None:
        """Pull the token logprob out of a streaming chunk, if present."""
        logprobs = choice.get("logprobs") or {}
        content = logprobs.get("content") or []
        if content:
            first = content[0]
            value = first.get("logprob")
            if isinstance(value, (int, float)):
                return float(value)
        alternatives = choice.get("logprobs") if isinstance(choice.get("logprobs"), dict) else None
        if alternatives and "logprob" in alternatives:
            value = alternatives["logprob"]
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def _replay_with_logprobs(self, messages, max_tokens, temperature, on_token) -> str:
        """Non-streaming fallback that guarantees per-token logprobs."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "logprobs": True,
        }
        try:
            response = requests.post(
                f"{self.base_url}{CHAT_COMPLETIONS_PATH}",
                json=payload,
                headers=_headers(self.api_key),
                timeout=self.timeout_s,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(
                f"Logprob replay against {self.base_url} failed: {exc}"
            ) from exc

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.base_url} returned no choices")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        content = ((choices[0].get("logprobs") or {}).get("content")) or []
        for entry in content:
            token_text = entry.get("token", "")
            logprob = entry.get("logprob")
            on_token(token_text, float(logprob) if isinstance(logprob, (int, float)) else 0.0)
        if not content and text:
            on_token(text, 0.0)
        return text

    def list_models(self) -> list[str]:
        """Return model ids from GET /v1/models."""
        try:
            response = requests.get(
                f"{self.base_url}{MODELS_PATH}",
                headers=_headers(self.api_key),
                timeout=5,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(f"Cannot reach {self.base_url}{MODELS_PATH}: {exc}") from exc
        return [m.get("id", "") for m in response.json().get("data", [])]

    # ------------------------------------------------------------------
    # PrimaryProvider
    # ------------------------------------------------------------------

    def execute(self, branch: str, context: str | None = None) -> str:
        """Run the branch as a chat completion and return the result text.

        Streams internally so the socket read timeout bounds the silent gap
        between tokens (stall_timeout_s) instead of the whole completion —
        a thinking model that pauses mid-reasoning is safe, a wedged engine
        trips the stall timeout quickly. With stall detection disabled this
        is a plain non-streaming request capped by timeout_s.
        """
        messages = []
        if context:
            messages.append({"role": "system", "content": context})
        messages.append({"role": "user", "content": branch})
        if not self.stall_timeout_s or self.stall_timeout_s <= 0:
            payload = {"model": self.model, "messages": messages, "stream": False}
            try:
                response = requests.post(
                    f"{self.base_url}{CHAT_COMPLETIONS_PATH}",
                    json=payload,
                    headers=_headers(self.api_key),
                    timeout=self.timeout_s,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                raise ProviderError(f"Primary execution failed: {exc}") from exc
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise ProviderError(f"{self.base_url} returned no choices")
            return (choices[0].get("message") or {}).get("content") or ""

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "logprobs": True,
        }
        collected: list[str] = []
        try:
            with requests.post(
                f"{self.base_url}{CHAT_COMPLETIONS_PATH}",
                json=payload,
                headers=_headers(self.api_key),
                stream=True,
                timeout=(10.0, self.stall_timeout_s),
            ) as response:
                if response.status_code != 200:
                    body = response.text[:300]
                    raise ProviderError(
                        f"{self.base_url} returned {response.status_code}: {body}"
                    )
                for raw_line in response.iter_lines():
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    token_text = (choices[0].get("delta") or {}).get("content") or ""
                    if token_text:
                        collected.append(token_text)
        except requests.RequestException as exc:
            raise ProviderError(f"Primary execution failed: {exc}") from exc
        return "".join(collected)
