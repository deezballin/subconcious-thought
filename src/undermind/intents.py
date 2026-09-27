"""
Lexical intent normalization.

Groups different phrasings that share the same direction into a single
normalized intent ID. The pipeline is fully deterministic and offline:

    lowercase -> strip punctuation -> drop stopwords -> light suffix stemming
    -> sort + dedupe content words -> joined signature -> sha256[:16] intent ID

Example: "can you fix the login bug?" and "please fix login bugs" both map to
the signature "bug fix login" and share one intent ID.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable, Optional

STOPWORDS = frozenset(
    """
    a an the and or but if then else for to of in on at by with from as is are
    was were be been being am i me my mine you your yours he him his she her
    hers it its its' we us our ours they them their theirs this that these
    those do does did done doing have has had having can could should would
    will shall may might must about above after again all any because before
    below between both during each few more most no nor not only other our
    out over own same some such too under until up very what when where which
    while who whom why how so than there here just now please thy gonna wanna
    someone somebody something anything everything nothing thing things hey
    okay thanks thank really actually literally basically kinda sorta
    """.split()
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_MIN_STEM_LEN = 3
_MIN_WORD_LEN = 2

# Machine-authored prompt templates should never become "directions the human
# keeps asking". Matched case-insensitively against the RAW text; each entry
# is a conservative marker that only appears in machine-generated envelopes.
_SYSTEM_MARKERS = (
    "[important: you are running as a scheduled cron job",
    "[context from the interrupted assistant response",
    "you are kairos (inside hermes). refresh a continui",
    "<undermind private=",
    "<subconscious private=",
)


def is_system_prompt(text: str) -> bool:
    """True when the text looks machine-authored rather than human.

    Conservative: only known template markers match, so unusual-but-human
    phrasings are never silently dropped from mining.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _SYSTEM_MARKERS)


def normalize_text(text: str) -> str:
    """Unicode-normalize and lowercase the raw input."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower().strip()


def tokenize(text: str) -> list[str]:
    """Split normalized text into alphanumeric word tokens."""
    cleaned = _PUNCT_RE.sub(" ", normalize_text(text))
    words = _WHITESPACE_RE.sub(" ", cleaned).split(" ")
    return [w for w in words if w]


_SUFFIXES = ("ing", "edly", "ed", "es", "ly", "s")


def stem(word: str) -> str:
    """Light deterministic suffix stem, safe for short words.

    Strips the first matching suffix as long as the remaining stem keeps at
    least ``_MIN_STEM_LEN`` characters; otherwise the word is unchanged.
    """
    if word == "status":
        return "status"
    for suffix in _SUFFIXES:
        if word.endswith(suffix):
            candidate = word[: -len(suffix)]
            if len(candidate) >= _MIN_STEM_LEN:
                return candidate
            return word
    return word


def content_words(text: str) -> list[str]:
    """Stopword-free, stemmed, sorted content words of the input."""
    stems = {
        stem(word)
        for word in tokenize(text)
        if len(word) >= _MIN_WORD_LEN and word not in STOPWORDS
    }
    return sorted(stems)


def signature(text: str) -> str:
    """The canonical space-joined signature for a piece of input text."""
    return " ".join(content_words(text))


def jaccard_similarity(sig_a: str, sig_b: str) -> float:
    """Word-set Jaccard similarity between two intent signatures (0..1).

    Signatures are already stopword-free stems joined by spaces, so plain
    set arithmetic is the right measure. Identical sets -> 1.0; disjoint
    -> 0.0. An empty signature on either side yields 0.0 (no evidence).
    """
    words_a = set(sig_a.split())
    words_b = set(sig_b.split())
    if not words_a or not words_b:
        return 0.0
    union = words_a | words_b
    return len(words_a & words_b) / len(union)


def find_similar_intent(
    signature: str,
    existing: Iterable[tuple[str, str]],
    threshold: float = 0.6,
) -> Optional[str]:
    """Return the intent_id of the most similar existing intent, or None.

    ``existing`` is an iterable of (intent_id, signature) pairs — typically
    all rows of the intents table. When several clear the threshold, the
    highest similarity wins; ties break toward the earliest row listed.
    A threshold <= 0 disables the search (always None).
    """
    if threshold <= 0:
        return None
    best_id: Optional[str] = None
    best_score = threshold
    for other_id, other_signature in existing:
        score = jaccard_similarity(signature, other_signature)
        if score > best_score:
            best_score = score
            best_id = other_id
    return best_id


def intent_id(text: str) -> str:
    """Stable 16-hex-char intent ID derived from the text signature."""
    return hashlib.sha256(signature(text).encode("utf-8")).hexdigest()[:16]


def is_repeat(text: str, existing_ids: set[str]) -> bool:
    """True when this text's intent ID has been seen before."""
    return intent_id(text) in existing_ids
