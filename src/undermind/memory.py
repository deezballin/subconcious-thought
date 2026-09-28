"""
Semantic memory over the self-mirror: reflections, embeddings, echoes.

The Memory Mine pillar: every mined assistant reflection (and free-turn
moment) is embedded and stored in a local LanceDB table, so past internal
life can be recalled by *meaning* rather than word overlap. Retrieved
reflections surface as offered context ([MEMETIC_ECHOES]) — never as
directives.

Design constraints:
- Fail-open everywhere: if lancedb / sentence-transformers are missing, the
  model download fails, or the table is unreadable, every method degrades to
  a no-op / empty result. The pipeline must never depend on this module.
- Off-turn: embedding is only ever done from the daydream miner and the free
  turn, never inside the request path (encode is ~16ms CPU, model load is
  seconds — neither belongs in a turn).
- Local only: vectors live in a single directory next to the SQLite store.
  Nothing leaves the machine.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

EMBED_MODEL = "all-MiniLM-L6-v2"
EMBED_DIM = 384


class MemoryStore:
    """Local semantic memory of the assistant's own reflections."""

    def __init__(self, dir_path: str | Path) -> None:
        self.dir_path = Path(dir_path)
        self._model = None
        self._table = None
        self._gate_table = None
        self._broken = False  # latch: stop retrying a dead backend

    # ------------------------------------------------------------------
    # lazy backends
    # ------------------------------------------------------------------

    def _get_model(self):
        if self._model is None and not self._broken:
            try:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(EMBED_MODEL)
            except Exception as exc:
                logger.warning("[memory] embedder unavailable, fail-open: %s", exc)
                self._broken = True
        return self._model

    def _get_table(self):
        if self._table is None and not self._broken:
            try:
                import lancedb

                db = lancedb.connect(str(self.dir_path))
                if "reflections" not in db.table_names():
                    self._table = db.create_table(
                        "reflections",
                        schema=self._schema(),
                        mode="create",
                    )
                else:
                    self._table = db.open_table("reflections")
            except Exception as exc:
                logger.warning("[memory] lancedb unavailable, fail-open: %s", exc)
                self._broken = True
        return self._table

    @staticmethod
    def _schema():
        import pyarrow as pa

        return pa.schema(
            [
                pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
                pa.field("text", pa.string()),
                pa.field("kind", pa.string()),  # reply | free_turn | note
                pa.field("ts_ms", pa.int64()),
                pa.field("ref_id", pa.int64()),  # outputs.id or free-turn row
            ]
        )

    def _embed(self, text: str) -> Optional[list[float]]:
        model = self._get_model()
        if model is None:
            return None
        try:
            vec = model.encode(text[:2000])
            return vec.tolist() if hasattr(vec, "tolist") else [float(v) for v in vec]
        except Exception as exc:
            logger.warning("[memory] encode failed, fail-open: %s", exc)
            return None

    @property
    def available(self) -> bool:
        return self._get_table() is not None and self._get_model() is not None

    # ------------------------------------------------------------------
    # write path (off-turn)
    # ------------------------------------------------------------------

    def remember(
        self, text: str, kind: str = "reply", ref_id: int = 0, ts_ms: Optional[int] = None
    ) -> bool:
        """Embed and store a reflection. Returns True when stored."""
        if not text or not text.strip():
            return False
        table = self._get_table()
        if table is None:
            return False
        vector = self._embed(text)
        if vector is None:
            return False
        try:
            table.add(
                [
                    {
                        "vector": vector,
                        "text": text[:2000],
                        "kind": kind,
                        "ts_ms": ts_ms if ts_ms is not None else int(time.time() * 1000),
                        "ref_id": ref_id,
                    }
                ]
            )
            return True
        except Exception as exc:
            logger.warning("[memory] store failed, fail-open: %s", exc)
            return False

    # ------------------------------------------------------------------
    # think gate: matured intents, indexed for the routine-vs-novel decision
    # ------------------------------------------------------------------

    def _get_gate_table(self):
        if self._gate_table is None and not self._broken:
            try:
                import lancedb
                import pyarrow as pa

                db = lancedb.connect(str(self.dir_path))
                schema = pa.schema(
                    [
                        pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
                        pa.field("text", pa.string()),
                        pa.field("ref_str", pa.string()),  # intent_id
                    ]
                )
                if "gate_intents" not in db.table_names():
                    self._gate_table = db.create_table(
                        "gate_intents", schema=schema, mode="create"
                    )
                else:
                    self._gate_table = db.open_table("gate_intents")
            except Exception as exc:
                logger.warning("[memory] gate table unavailable, fail-open: %s", exc)
                self._broken = True
        return self._gate_table

    def remember_intent(self, text: str, intent_id: str) -> bool:
        """Index a matured intent for the think gate (idempotent per id).

        The signature is re-embedded on every call (signatures are stable,
        so the vector is too); any previous row for the same intent_id is
        deleted first so the table stays 1:1 with matured intents.
        """
        if not text or not text.strip():
            return False
        table = self._get_gate_table()
        if table is None:
            return False
        vector = self._embed(text)
        if vector is None:
            return False
        try:
            table.delete(f"ref_str = '{intent_id}'")
        except Exception:
            pass  # best-effort; a stray duplicate is harmless
        try:
            table.add(
                [{"vector": vector, "text": text[:2000], "ref_str": intent_id}]
            )
            return True
        except Exception as exc:
            logger.warning("[memory] gate store failed, fail-open: %s", exc)
            return False

    def gate_match(self, query: str, min_score: float) -> Optional[dict]:
        """Best matured-intent match for ``query`` above ``min_score``.

        Returns {"score", "ref_str"} or None. This is the certainty metric
        for the think gate: high similarity to a routine intent means the
        turn is routine (no deliberation needed).
        """
        table = self._get_gate_table()
        if table is None:
            return None
        vector = self._embed(query)
        if vector is None:
            return None
        try:
            rows = table.search(vector).limit(1).to_list()
        except Exception as exc:
            logger.warning("[memory] gate search failed, fail-open: %s", exc)
            return None
        if not rows:
            return None
        score = 1.0 - float(rows[0].get("_distance", 2.0)) / 2.0
        if score < min_score:
            return None
        return {"score": round(score, 3), "ref_str": str(rows[0].get("ref_str", ""))}

    # ------------------------------------------------------------------
    # read path (echoes)
    # ------------------------------------------------------------------

    def echoes(self, query: str, limit: int = 3, min_score: float = 0.35) -> list[dict]:
        """Past reflections most similar in meaning to ``query``.

        Returns [{"text", "kind", "ts_ms", "ref_id", "score"}], best first;
        [] when the backend is unavailable or nothing clears ``min_score``.
        """
        table = self._get_table()
        if table is None:
            return []
        vector = self._embed(query)
        if vector is None:
            return []
        try:
            rows = table.search(vector).limit(limit).to_list()
        except Exception as exc:
            logger.warning("[memory] search failed, fail-open: %s", exc)
            return []
        # LanceDB default metric is squared L2 (range 0..2). Normalize to a
        # cosine-like similarity in [-1, 1]: 1 - dist/2. For the normalized
        # vectors we store, 1 - dist/2 == cosine similarity.
        out = []
        for row in rows:
            score = 1.0 - float(row.get("_distance", 2.0)) / 2.0
            if score >= min_score:
                out.append(
                    {
                        "text": str(row.get("text", "")),
                        "kind": str(row.get("kind", "")),
                        "ts_ms": int(row.get("ts_ms", 0)),
                        "ref_id": int(row.get("ref_id", 0)),
                        "score": round(score, 3),
                    }
                )
        return out

    def count(self) -> int:
        table = self._get_table()
        if table is None:
            return 0
        try:
            return table.count_rows()
        except Exception:
            return 0
