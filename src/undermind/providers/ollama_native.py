"""
Ollama-native provider backend.

Uses Ollama's /api/generate endpoint (v0.12.11+) which reports per-token
logprobs in streaming chunks when the ``logprobs`` option is enabled. Useful
when the draft model is hosted by Ollama instead of Lemonade.
"""

from __future__ import annotations

import json

import requests

from undermind.providers.base import ProviderError

GENERATE_PATH = "/api/generate"
TAGS_PATH = "/api/tags"


class OllamaNativeProvider:
    """Client for Ollama's native generate API with logprob reporting."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = 30.0,
        stall_timeout_s: float = 0.0,
        think: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        # Max silent gap between streamed tokens during execute(); bounds a
        # hung engine without cutting off long thinking turns. 0 = disabled.
        self.stall_timeout_s = stall_timeout_s
        # Keep the model's hidden reasoning enabled for execute(); false
        # replies directly (faster, slightly shallower).
        self.think = think

    def stream_tokens(self, messages, max_tokens, temperature, on_token) -> str:
        """Stream tokens with logprobs from /api/generate.

        Chat-format messages are flattened into a single prompt so no chat
        template is required on the Ollama side.
        """
        parts = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            if role == "system":
                parts.append(content)
            else:
                parts.append(f"{content}")
        prompt = "".join(parts)

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
                "logprobs": 1,
            },
        }
        collected: list[str] = []
        try:
            with requests.post(
                f"{self.base_url}{GENERATE_PATH}",
                json=payload,
                stream=True,
                timeout=self.timeout_s,
            ) as response:
                if response.status_code != 200:
                    body = response.text[:300]
                    raise ProviderError(
                        f"{self.base_url} returned {response.status_code}: {body}"
                    )
                for raw_line in response.iter_lines():
                    if not raw_line:
                        continue
                    try:
                        chunk = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    token_text = chunk.get("response", "")
                    if not token_text:
                        continue
                    logprob = self._extract_chunk_logprob(chunk)
                    collected.append(token_text)
                    on_token(token_text, logprob if logprob is not None else 0.0)
        except requests.RequestException as exc:
            raise ProviderError(
                f"Cannot reach {self.base_url}{GENERATE_PATH}: {exc}"
            ) from exc
        return "".join(collected)

    @staticmethod
    def _extract_chunk_logprob(chunk: dict) -> float | None:
        """Read the logprob from an Ollama streaming chunk, if reported."""
        logprobs = chunk.get("logprobs")
        if isinstance(logprobs, list) and logprobs:
            first = logprobs[0]
            value = first.get("logprob") if isinstance(first, dict) else None
            if isinstance(value, (int, float)):
                return float(value)
        value = chunk.get("logprob")
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def list_models(self) -> list[str]:
        """Return model names from GET /api/tags."""
        try:
            response = requests.get(f"{self.base_url}{TAGS_PATH}", timeout=5)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(f"Cannot reach {self.base_url}{TAGS_PATH}: {exc}") from exc
        return [m.get("name", "") for m in response.json().get("models", [])]

    def _payload(self, prompt: str, stream: bool) -> dict:
        """Generate payload for primary execution (think flag included)."""
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": stream,
            "think": self.think,
        }
        if stream and self.stall_timeout_s and self.stall_timeout_s > 0:
            payload["options"] = {"logprobs": 1}
        return payload

    def execute(self, branch: str, context: str | None = None) -> str:
        """Generate for primary-pipeline execution.

        Streams internally so the socket read timeout bounds the silent gap
        between tokens (stall_timeout_s) instead of the whole completion —
        a thinking model that pauses mid-reasoning is safe, a wedged engine
        trips the stall timeout quickly. With stall detection disabled this
        is a plain non-streaming request capped by timeout_s.
        """
        prompt = f"{context}\n\n{branch}" if context else branch
        if not self.stall_timeout_s or self.stall_timeout_s <= 0:
            payload = self._payload(prompt, stream=False)
            try:
                response = requests.post(
                    f"{self.base_url}{GENERATE_PATH}",
                    json=payload,
                    timeout=self.timeout_s,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                raise ProviderError(f"Primary execution failed: {exc}") from exc
            return response.json().get("response", "")

        payload = self._payload(prompt, stream=True)
        collected: list[str] = []
        try:
            with requests.post(
                f"{self.base_url}{GENERATE_PATH}",
                json=payload,
                stream=True,
                timeout=(10.0, self.stall_timeout_s),
            ) as response:
                if response.status_code != 200:
                    body = response.text[:300]
                    raise ProviderError(
                        f"{self.base_url} returned {response.status_code}: {body}"
                    )
                for raw_line in response.iter_lines():
                    if not raw_line:
                        continue
                    try:
                        chunk = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    token_text = chunk.get("response", "")
                    if token_text:
                        collected.append(token_text)
        except requests.RequestException as exc:
            raise ProviderError(f"Primary execution failed: {exc}") from exc
        return "".join(collected)
