"""
Draft prediction engine.

DraftPredictor streams a Krios continuation for the current character buffer,
tracks per-token confidence, and fires ``on_confident`` the instant the
confidence threshold is crossed — cutting the local loop and handing the full
text branch (buffer + continuation) to the primary pipeline.

A legacy ``Predictor`` beam-search shim is kept for the proxy cache and older
callers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from undermind.confidence import ConfidenceTracker, Crossing
from undermind.providers.base import DraftClient, StreamCallback, build_draft_messages

ON_CONFIDENT = Callable[[str, Crossing, ConfidenceTracker], None]
ON_SUGGESTION = Callable[[str, ConfidenceTracker], None]
ON_ERROR = Callable[[str], None]


class _StopStream(Exception):
    """Internal: unwinds the token stream after the threshold crossing."""


@dataclass(order=True)
class Hypothesis:
    """One candidate completion with a score (legacy beam item)."""

    score: float
    text: str = field(compare=False)
    created_at: float = field(default_factory=time.time, compare=False)
    updated_at: float = field(default_factory=time.time, compare=False)

    def update(self, new_text: str, new_score: float) -> "Hypothesis":
        return Hypothesis(
            score=new_score,
            text=new_text,
            created_at=self.created_at,
            updated_at=time.time(),
        )


class DraftPredictor:
    """Debounced, cancellable streamed prediction over the keystroke buffer."""

    def __init__(
        self,
        draft_client: DraftClient,
        debounce_s: float = 0.35,
        temperature: float = 0.2,
        max_tokens: int = 64,
        min_buffer_chars: int = 4,
        confidence_kwargs: Optional[dict] = None,
        on_confident: Optional[ON_CONFIDENT] = None,
        on_suggestion: Optional[ON_SUGGESTION] = None,
        on_error: Optional[ON_ERROR] = None,
    ) -> None:
        self.draft_client = draft_client
        self.debounce_s = debounce_s
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.min_buffer_chars = min_buffer_chars
        self.confidence_kwargs = dict(confidence_kwargs or {})
        self.on_confident = on_confident
        self.on_suggestion = on_suggestion
        self.on_error = on_error

        self._lock = threading.Lock()
        self._generation = 0
        self._pending_buffer: Optional[str] = None
        self._pending_start: float = 0.0
        self._active_streams = 0
        self.last_tracker: Optional[ConfidenceTracker] = None
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # input side (called from the pipeline tick)
    # ------------------------------------------------------------------

    def notify_keystroke(self, buffer_text: str) -> None:
        """A keystroke landed: cancel any stream and (re)arm the debounce.

        An unchanged buffer keeps its original debounce timestamp, so the
        timer actually elapses when typing pauses.
        """
        with self._lock:
            if buffer_text == self._pending_buffer:
                return
            self._generation += 1
            if len(buffer_text.strip()) >= self.min_buffer_chars:
                self._pending_buffer = buffer_text
                self._pending_start = time.monotonic()
            else:
                self._pending_buffer = None
                self._pending_start = 0.0

    def poll(self) -> None:
        """Launch a stream when the debounce window has elapsed."""
        with self._lock:
            if self._pending_buffer is None:
                return
            if time.monotonic() - self._pending_start < self.debounce_s:
                return
            buffer_text = self._pending_buffer
            generation = self._generation
            self._pending_buffer = None
            self._pending_start = 0.0
            self._active_streams += 1
        thread = threading.Thread(
            target=self._stream_worker,
            args=(buffer_text, generation),
            name=f"undermind-draft-{generation}",
            daemon=True,
        )
        thread.start()

    def is_busy(self) -> bool:
        with self._lock:
            return self._active_streams > 0

    def cancel(self) -> None:
        """Cancel all in-flight streams (new generation invalidates old ones)."""
        with self._lock:
            self._generation += 1
            self._pending_buffer = None
            self._pending_start = 0.0

    # ------------------------------------------------------------------
    # stream worker
    # ------------------------------------------------------------------

    def _stream_worker(self, buffer_text: str, generation: int) -> None:
        tracker = ConfidenceTracker(**self.confidence_kwargs)
        with self._lock:
            self.last_tracker = tracker
        fired = {"crossed": False}

        def on_token(token_text: str, logprob: float) -> None:
            if self._stale(generation):
                raise _StopStream()
            crossing = tracker.add_token(token_text, logprob)
            if crossing is not None and not fired["crossed"]:
                fired["crossed"] = True
                raise _StopStream()

        try:
            messages = build_draft_messages(buffer_text)
            self.draft_client.stream_tokens(
                messages, self.max_tokens, self.temperature, on_token
            )
        except _StopStream:
            pass
        except Exception as exc:
            self.last_error = str(exc)
            if self.on_error is not None:
                self.on_error(str(exc))
        finally:
            with self._lock:
                self._active_streams -= 1

        if self._stale(generation):
            return

        branch = buffer_text + tracker.text
        if fired["crossed"] and self.on_confident is not None:
            crossing = tracker.crossing
            if crossing is not None:
                self.on_confident(branch, crossing, tracker)
        elif not fired["crossed"] and tracker.samples and self.on_suggestion is not None:
            self.on_suggestion(branch, tracker)

    def _stale(self, generation: int) -> bool:
        with self._lock:
            return generation != self._generation

    # ------------------------------------------------------------------
    # one-shot helper (proxy pre-generation / legacy shim)
    # ------------------------------------------------------------------

    def complete_once(self, buffer_text: str) -> tuple[str, float]:
        """Blocking single continuation; returns (text, mean token confidence)."""
        tracker = ConfidenceTracker(**self.confidence_kwargs)

        def collect(token_text: str, logprob: float) -> None:
            tracker.add_token(token_text, logprob)

        messages = build_draft_messages(buffer_text)
        try:
            self.draft_client.stream_tokens(
                messages, self.max_tokens, self.temperature, collect
            )
        except Exception:
            pass
        confidences = [s.confidence for s in tracker.samples]
        mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
        return tracker.text, mean_conf


class Predictor:
    """Legacy beam-search shim over the draft model (kept for the proxy)."""

    def __init__(
        self,
        beam_width: int = 5,
        temperature: float = 0.2,
        draft_client: Optional[DraftClient] = None,
        max_tokens: int = 64,
    ) -> None:
        self.beam_width = beam_width
        self.temperature = temperature
        self.draft_client = draft_client
        self.max_tokens = max_tokens
        self.hypotheses: List[Hypothesis] = []
        self._last_buffer = ""

    def _generate_completions(self, buffer: str) -> List[tuple[str, float]]:
        """Query the draft model for one completion, scored by mean confidence."""
        if not buffer.strip() or self.draft_client is None:
            return []
        try:
            tracker = ConfidenceTracker(mode="latest")

            def collect(token_text: str, logprob: float) -> None:
                tracker.add_token(token_text, logprob)

            messages = build_draft_messages(buffer)
            self.draft_client.stream_tokens(
                messages, self.max_tokens, self.temperature, collect
            )
            text = tracker.text.strip()
            if not text:
                return []
            confidences = [s.confidence for s in tracker.samples]
            mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
            return [(text, mean_conf)]
        except Exception:
            return []

    def update(self, buffer: str) -> List[Hypothesis]:
        """Refresh the beam: anchor the buffer plus draft completions."""
        self._last_buffer = buffer
        anchor = Hypothesis(score=0.0, text=buffer)
        candidates: List[Hypothesis] = [anchor]
        for text, score in self._generate_completions(buffer):
            candidates.append(Hypothesis(score=score, text=text))

        merged = self.hypotheses + candidates if self.hypotheses else candidates
        unique: dict[str, Hypothesis] = {}
        for hyp in merged:
            unique[hyp.text] = hyp
        self.hypotheses = sorted(unique.values(), key=lambda h: h.score, reverse=True)
        self.hypotheses = self.hypotheses[: self.beam_width]
        return self.hypotheses

    @property
    def top(self) -> Optional[Hypothesis]:
        return self.hypotheses[0] if self.hypotheses else None

    def is_dominant(self, gap: float = 0.5) -> bool:
        if len(self.hypotheses) < 2:
            return True
        return (self.hypotheses[0].score - self.hypotheses[1].score) >= gap

    def prune(self, threshold: float = -1e9) -> List[Hypothesis]:
        self.hypotheses = [h for h in self.hypotheses if h.score > threshold]
        return self.hypotheses

    def reset(self) -> None:
        self.hypotheses.clear()
        self._last_buffer = ""

    def get_state(self) -> dict:
        return {
            "beam_width": self.beam_width,
            "beam_size": len(self.hypotheses),
            "top_score": self.hypotheses[0].score if self.hypotheses else None,
            "top_text": self.hypotheses[0].text if self.hypotheses else "",
            "is_dominant": self.is_dominant() if self.hypotheses else False,
            "all": [(h.text, round(h.score, 3)) for h in self.hypotheses],
        }
