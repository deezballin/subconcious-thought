"""
Training export writer.

Formats accumulated intent clusters as one JSON object per line in
data/training_export.jsonl so the logs are ready for offline training or
local weight adjustments. An intent is written only when its occurrence
count has changed since the last export (dedupe via the export ledger).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List

from undermind.store import UndermindStore

EXPORT_FILENAME = "training_export.jsonl"


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000.0)) + "Z"


class Exporter:
    """Appends intent snapshots to the JSONL training export."""

    def __init__(self, store: UndermindStore, export_path: str) -> None:
        self.store = store
        self.export_path = Path(export_path)
        self.export_path.parent.mkdir(parents=True, exist_ok=True)

    def export_pending(self, min_count: int, max_samples: int) -> List[dict]:
        """Export every intent with un-exported occurrences.

        Returns the list of records written (empty when nothing changed).
        Each line carries: intent_id, signature, count, samples, first/last
        seen timestamps, and the export run timestamp.
        """
        pending = self.store.pending_export_intents(min_count)
        if not pending:
            return []

        records: List[dict] = []
        exported_ids: List[str] = []
        run_ts = int(time.time() * 1000)

        with open(self.export_path, "a", encoding="utf-8") as fh:
            for row in pending:
                intent_id = row["intent_id"]
                samples = self.store.samples_for_intent(intent_id, max_samples)
                record = {
                    "intent_id": intent_id,
                    "signature": row["signature"],
                    "count": row["count"],
                    "first_seen_ms": row["first_seen_ms"],
                    "last_seen_ms": row["last_seen_ms"],
                    "first_seen": _iso(row["first_seen_ms"]),
                    "last_seen": _iso(row["last_seen_ms"]),
                    "export_run_ms": run_ts,
                    "samples": [
                        {
                            "input_id": s["id"],
                            "ts_ms": s["ts_ms"],
                            "text": s["text"],
                        }
                        for s in samples
                    ],
                }
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                records.append(record)
                exported_ids.append(intent_id)

        self.store.mark_exported(exported_ids)
        self.store.record_export(
            str(self.export_path), len(records), exported_ids
        )
        return records
