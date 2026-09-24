"""
Provider protocol definitions shared by all Undermind backends.

A DraftClient streams draft-model continuations token by token together with
per-token logprobs (used for confidence tracking). A PrimaryProvider executes a
handed-off text branch on the user's chosen large-model pipeline.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator, Protocol, runtime_checkable

StreamCallback = Callable[[str, float], None]


class ProviderError(RuntimeError):
    """Raised when a provider backend fails or is unreachable."""


@runtime_checkable
class DraftClient(Protocol):
    """A backend that streams continuations with per-token logprobs."""

    def stream_tokens(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        on_token: StreamCallback,
    ) -> str:
        """Stream a continuation, calling on_token(text, logprob) per token.

        Returns the full continuation text when generation finishes.
        """
        ...

    def list_models(self) -> list[str]:
        """Return model ids available on the backend, for startup validation."""
        ...


@runtime_checkable
class PrimaryProvider(Protocol):
    """A backend that executes a handed-off text branch."""

    def execute(self, branch: str, context: str | None = None) -> str:
        """Run the branch through the primary pipeline and return the result."""
        ...


def chat_message(role: str, content: str) -> dict[str, str]:
    """Build a chat message dict in the OpenAI wire format."""
    return {"role": role, "content": content}


def build_draft_messages(
    buffer_text: str, system_prompt: str | None = None
) -> list[dict[str, str]]:
    """Assemble chat messages asking the draft model to continue the buffer.

    The continuation instruction keeps the draft model from answering the text
    as a question; it must extend the sentence exactly as typed.
    """
    system = system_prompt or (
        "You are an inline sentence-prediction engine. Continue the user's "
        "text seamlessly from where it stops. Output only the continuation "
        "with no preamble, no quotes, and no repetition of the original text."
    )
    return [
        chat_message("system", system),
        chat_message("user", buffer_text),
    ]


def _sse_iter(line_iter: Iterator[bytes]) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON payloads from an SSE byte stream, skipping keep-alives."""
    for raw_line in line_iter:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            yield json_loads(payload)
        except ValueError:
            continue


def json_loads(payload: str) -> dict[str, Any]:
    """Thin wrapper so modules below do not each import json separately."""
    import json

    return json.loads(payload)
