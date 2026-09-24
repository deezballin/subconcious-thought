"""
Token-confidence tracking with the 95% fast-handoff trigger.

As the draft model streams a continuation, each token arrives with a logprob.
Confidence per token is exp(logprob). The ConfidenceTracker records every
sample at nanosecond resolution and reports the exact moment the configured
threshold (default 0.95) is crossed — edge-triggered, so one crossing per
prediction stream.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import List, Optional

VALID_MODES = ("latest", "ewma")


@dataclass
class TokenSample:
    """One streamed token with its confidence and arrival timestamps."""

    text: str
    logprob: float
    confidence: float
    monotonic_ns: int
    wall_ns: int

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "logprob": self.logprob,
            "confidence": self.confidence,
            "monotonic_ns": self.monotonic_ns,
            "wall_ns": self.wall_ns,
        }


@dataclass
class Crossing:
    """The moment confidence met or exceeded the threshold."""

    confidence: float
    monotonic_ns: int
    wall_ns: int
    token_index: int
    text_so_far: str

    def to_dict(self) -> dict:
        return {
            "confidence": self.confidence,
            "monotonic_ns": self.monotonic_ns,
            "wall_ns": self.wall_ns,
            "token_index": self.token_index,
            "text_so_far": self.text_so_far,
        }


@dataclass
class ConfidenceTracker:
    """Accumulates token confidences and detects the threshold crossing."""

    threshold: float = 0.95
    mode: str = "latest"
    ewma_alpha: float = 0.4
    min_tokens: int = 2
    min_chars: int = 8
    samples: List[TokenSample] = field(default_factory=list)
    crossing: Optional[Crossing] = None
    _ewma: Optional[float] = None

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {self.mode!r}")
        if not 0.0 < self.threshold <= 1.0:
            raise ValueError("threshold must be in (0.0, 1.0]")
        if not 0.0 < self.ewma_alpha <= 1.0:
            raise ValueError("ewma_alpha must be in (0.0, 1.0]")

    @staticmethod
    def confidence_from_logprob(logprob: float) -> float:
        """Convert a logprob to a probability, clamped to [0, 1]."""
        if logprob >= 0.0:
            return 1.0
        return math.exp(logprob)

    def add_token(self, text: str, logprob: float) -> Optional[Crossing]:
        """Record one streamed token.

        Returns the Crossing exactly once, on the sample that first meets the
        threshold; returns None otherwise. Missing logprobs arrive as 0.0 and
        are treated as full confidence (conservative: encourages handoff).
        """
        now_mono = time.perf_counter_ns()
        now_wall = time.time_ns()
        confidence = self.confidence_from_logprob(logprob)
        sample = TokenSample(
            text=text,
            logprob=logprob,
            confidence=confidence,
            monotonic_ns=now_mono,
            wall_ns=now_wall,
        )
        self.samples.append(sample)

        if self._ewma is None:
            self._ewma = confidence
        else:
            self._ewma = (
                self.ewma_alpha * confidence + (1.0 - self.ewma_alpha) * self._ewma
            )

        if self.crossing is not None:
            return None

        score = confidence if self.mode == "latest" else self._ewma
        if self._guards_met() and score >= self.threshold:
            self.crossing = Crossing(
                confidence=score,
                monotonic_ns=now_mono,
                wall_ns=now_wall,
                token_index=len(self.samples) - 1,
                text_so_far=self.text,
            )
            return self.crossing
        return None

    def _guards_met(self) -> bool:
        if len(self.samples) < self.min_tokens:
            return False
        return len(self.text) >= self.min_chars

    @property
    def text(self) -> str:
        """Concatenated continuation text collected so far."""
        return "".join(sample.text for sample in self.samples)

    @property
    def current_confidence(self) -> float:
        """Latest token confidence, or the EWMA depending on mode."""
        if not self.samples:
            return 0.0
        if self.mode == "latest":
            return self.samples[-1].confidence
        return self._ewma if self._ewma is not None else 0.0

    @property
    def crossed(self) -> bool:
        return self.crossing is not None

    def ms_since_first_token(self) -> Optional[float]:
        """Milliseconds elapsed since the first token arrived."""
        if not self.samples:
            return None
        first = self.samples[0].monotonic_ns
        return (self.samples[-1].monotonic_ns - first) / 1e6

    def reset(self) -> None:
        """Clear all state for a fresh prediction stream."""
        self.samples.clear()
        self.crossing = None
        self._ewma = None

    def to_dict(self) -> dict:
        """Snapshot for logging and DB records."""
        crossing_ms = None
        if self.samples and self.crossing is not None:
            crossing_ms = (
                self.crossing.monotonic_ns - self.samples[0].monotonic_ns
            ) / 1e6
        return {
            "threshold": self.threshold,
            "mode": self.mode,
            "tokens": len(self.samples),
            "chars": len(self.text),
            "current_confidence": self.current_confidence,
            "crossed": self.crossed,
            "crossing": self.crossing.to_dict() if self.crossing else None,
            "ms_from_first_token_to_crossing": crossing_ms,
        }
