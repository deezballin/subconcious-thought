"""
SQLite persistence layer for Undermind.

Stores raw inputs, normalized intents, intent samples, handoff records, and
the export ledger. Uses WAL mode and an RLock-guarded connection so the
pipeline thread, daydream worker, and proxy threads can share one store.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    normalized TEXT NOT NULL DEFAULT '',
    intent_id TEXT,
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inputs_processed ON inputs(processed);
CREATE INDEX IF NOT EXISTS idx_inputs_ts ON inputs(ts_ms);

CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    exported_at_ms INTEGER,
    export_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS intent_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    input_id INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES intents(intent_id),
    FOREIGN KEY (input_id) REFERENCES inputs(id)
);
CREATE INDEX IF NOT EXISTS idx_samples_intent ON intent_samples(intent_id);

CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ns INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    confidence REAL,
    branch TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    path TEXT NOT NULL,
    records INTEGER NOT NULL,
    intent_ids TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_ms() -> int:
    """Wall-clock milliseconds."""
    return time.time_ns() // 1_000_000


class UndermindStore:
    """Thread-safe SQLite store for all Undermind state."""

    def __init__(self, db_path: str = "data/undermind.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA_SQL)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # inputs
    # ------------------------------------------------------------------

    def record_input(
        self, text: str, normalized: str = "", intent_id: Optional[str] = None
    ) -> int:
        """Store a finished piece of typed input, unprocessed by default."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO inputs (ts_ms, text, normalized, intent_id, processed)"
                " VALUES (?, ?, ?, ?, 0)",
                (now_ms(), text, normalized, intent_id),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def fetch_unprocessed(self, limit: int = 200) -> List[sqlite3.Row]:
        """Inputs not yet folded into the intent tables."""
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM inputs WHERE processed = 0 ORDER BY id LIMIT ?",
                    (limit,),
                )
            )

    def mark_processed(self, input_ids: Sequence[int]) -> None:
        """Flag inputs as folded into intents."""
        if not input_ids:
            return
        placeholders = ",".join("?" for _ in input_ids)
        with self._lock:
            self._conn.execute(
                f"UPDATE inputs SET processed = 1 WHERE id IN ({placeholders})",
                tuple(input_ids),
            )
            self._conn.commit()

    def record_input_id_intent(
        self, input_id: int, intent_id: str, normalized: str
    ) -> None:
        """Attach the computed intent ID and normalized signature to an input."""
        with self._lock:
            self._conn.execute(
                "UPDATE inputs SET intent_id = ?, normalized = ? WHERE id = ?",
                (intent_id, normalized, input_id),
            )
            self._conn.commit()

    def count_inputs(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM inputs").fetchone()
            return int(row["n"])

    def count_unprocessed(self) -> int:
        """Inputs still awaiting daydream mining (proxy /api/health)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM inputs WHERE processed = 0"
            ).fetchone()
            return int(row["n"])

    # ------------------------------------------------------------------
    # intents
    # ------------------------------------------------------------------

    def upsert_intent(self, intent_id: str, signature: str) -> int:
        """Create or bump an intent; returns its total count."""
        ts = now_ms()
        with self._lock:
            self._conn.execute(
                "INSERT INTO intents (intent_id, signature, first_seen_ms,"
                " last_seen_ms, count) VALUES (?, ?, ?, ?, 1)"
                " ON CONFLICT(intent_id) DO UPDATE SET"
                " last_seen_ms = excluded.last_seen_ms, count = count + 1",
                (intent_id, signature, ts, ts),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT count FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            return int(row["count"])

    def add_intent_sample(self, intent_id: str, input_id: int) -> None:
        """Link an input row to its intent as an example occurrence."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO intent_samples (intent_id, input_id, ts_ms)"
                " VALUES (?, ?, ?)",
                (intent_id, input_id, now_ms()),
            )
            self._conn.commit()

    def delete_intent(self, intent_id: str) -> bool:
        """Remove an intent and its samples; returns True if a row was removed.

        Sample rows are deleted (they would be orphans); inputs that pointed
        at the intent keep their text and get intent_id = NULL instead.
        No-op (False) when the intent does not exist.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM intent_samples WHERE intent_id = ?", (intent_id,)
            )
            samples = cur.rowcount
            self._conn.execute(
                "UPDATE inputs SET intent_id = NULL WHERE intent_id = ?",
                (intent_id,),
            )
            cur = self._conn.execute(
                "DELETE FROM intents WHERE intent_id = ?", (intent_id,)
            )
            self._conn.commit()
            return bool(samples or cur.rowcount)

    def dedupe_intent_samples(self) -> int:
        """Remove duplicate (intent_id, input_id) sample rows; returns removed.

        Merge flows re-link samples that were already moved, so the
        (intent_id, input_id) pair can repeat; the miner only needs one.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM intent_samples WHERE id NOT IN ("
                "  SELECT MIN(id) FROM intent_samples"
                "  GROUP BY intent_id, input_id)"
            )
            self._conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def merge_intent(self, source_id: str, target_id: str) -> int:
        """Fold source intent into target; returns the target's new count.

        Moves intent_samples and inputs references over, sums counts,
        keeps the earlier first_seen and later last_seen, unions export
        bookkeeping, then deletes the source row. Self-merges and missing
        rows are no-ops.
        """
        with self._lock:
            source = self._conn.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (source_id,)
            ).fetchone()
            target = self._conn.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (target_id,)
            ).fetchone()
            if source is None or target is None or source_id == target_id:
                return int(target["count"]) if target is not None else 0

            self._conn.execute(
                "UPDATE intent_samples SET intent_id = ? WHERE intent_id = ?",
                (target_id, source_id),
            )
            self._conn.execute(
                "UPDATE inputs SET intent_id = ? WHERE intent_id = ?",
                (target_id, source_id),
            )

            exported_values = [
                int(v)
                for v in (target["exported_at_ms"], source["exported_at_ms"])
                if v is not None
            ]
            self._conn.execute(
                "UPDATE intents SET count = ?, first_seen_ms = ?,"
                " last_seen_ms = ?, exported_at_ms = ?, export_count = ?"
                " WHERE intent_id = ?",
                (
                    int(target["count"]) + int(source["count"]),
                    min(int(target["first_seen_ms"]), int(source["first_seen_ms"])),
                    max(int(target["last_seen_ms"]), int(source["last_seen_ms"])),
                    min(exported_values) if exported_values else None,
                    int(target["export_count"]) + int(source["export_count"]),
                    target_id,
                ),
            )
            self._conn.execute(
                "DELETE FROM intents WHERE intent_id = ?", (source_id,)
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT count FROM intents WHERE intent_id = ?", (target_id,)
            ).fetchone()
            return int(row["count"])

    def get_intent(self, intent_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()

    def list_intents(self, min_count: int = 1) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM intents WHERE count >= ? ORDER BY count DESC,"
                    " last_seen_ms DESC",
                    (min_count,),
                )
            )

    def pending_export_intents(self, min_count: int) -> List[sqlite3.Row]:
        """Intents with occurrences not yet included in an export."""
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM intents WHERE count >= ?"
                    " AND (export_count < count OR exported_at_ms IS NULL)"
                    " ORDER BY count DESC",
                    (min_count,),
                )
            )

    def mark_exported(self, intent_ids: Iterable[str]) -> None:
        """Record that current counts for these intents have been exported."""
        ids = list(intent_ids)
        if not ids:
            return
        ts = now_ms()
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self._conn.execute(
                f"UPDATE intents SET exported_at_ms = ?, export_count = count"
                f" WHERE intent_id IN ({placeholders})",
                tuple([ts, *ids]),
            )
            self._conn.commit()

    def samples_for_intent(
        self, intent_id: str, limit: int = 20
    ) -> List[sqlite3.Row]:
        """Most recent example inputs for an intent."""
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT i.id, i.ts_ms, i.text FROM intent_samples s"
                    " JOIN inputs i ON i.id = s.input_id"
                    " WHERE s.intent_id = ? ORDER BY s.id DESC LIMIT ?",
                    (intent_id, limit),
                )
            )

    # ------------------------------------------------------------------
    # handoffs
    # ------------------------------------------------------------------

    def record_handoff(
        self,
        trigger: str,
        branch: str,
        provider: str,
        model: str,
        status: str,
        confidence: Optional[float] = None,
        latency_ms: Optional[float] = None,
        error: Optional[str] = None,
    ) -> int:
        """Log one fast-handoff execution of a text branch."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO handoffs (ts_ns, trigger, confidence, branch,"
                " provider, model, status, latency_ms, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time_ns(),
                    trigger,
                    confidence,
                    branch,
                    provider,
                    model,
                    status,
                    latency_ms,
                    error,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def count_handoffs(self, status: Optional[str] = None) -> int:
        with self._lock:
            if status:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM handoffs WHERE status = ?",
                    (status,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM handoffs"
                ).fetchone()
            return int(row["n"])

    def latest_handoff(self) -> Optional[sqlite3.Row]:
        """The most recent handoff record, or None."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM handoffs ORDER BY id DESC LIMIT 1"
            ).fetchone()

    # ------------------------------------------------------------------
    # exports
    # ------------------------------------------------------------------

    def record_export(self, path: str, records: int, intent_ids: Sequence[str]) -> int:
        """Append a row to the export ledger."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO exports (ts_ms, path, records, intent_ids)"
                " VALUES (?, ?, ?, ?)",
                (now_ms(), path, records, ",".join(intent_ids)),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "UndermindStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
