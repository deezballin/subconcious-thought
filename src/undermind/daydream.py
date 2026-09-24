"""
Background daydreaming loop.

A daemon worker that triggers only when the typing pipeline goes completely
idle (no keystrokes and no in-flight prediction or handoff). Each cycle it
flushes the live buffer into the inputs table, folds unprocessed inputs into
normalized intents, and appends new intent snapshots to the JSONL training
export.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from undermind.exporter import Exporter
from undermind.intents import intent_id as compute_intent_id
from undermind.intents import signature as compute_signature
from undermind.store import UndermindStore


class DaydreamResult:
    """What one daydream cycle did."""

    def __init__(
        self,
        inputs_processed: int,
        intents_updated: int,
        records_exported: int,
        intent_ids: List[str],
    ) -> None:
        self.inputs_processed = inputs_processed
        self.intents_updated = intents_updated
        self.records_exported = records_exported
        self.intent_ids = intent_ids

    def to_dict(self) -> dict:
        return {
            "inputs_processed": self.inputs_processed,
            "intents_updated": self.intents_updated,
            "records_exported": self.records_exported,
            "intent_ids": self.intent_ids,
        }


class DaydreamWorker:
    """Idle-triggered background worker for intent mining and export."""

    def __init__(
        self,
        store: UndermindStore,
        exporter: Exporter,
        idle_threshold_s: float,
        min_intent_count: int = 2,
        max_samples_per_intent: int = 20,
        poll_interval_s: float = 0.5,
        buffer_supplier: Optional[Callable[[], Optional[str]]] = None,
        on_cycle: Optional[Callable[[DaydreamResult], None]] = None,
    ) -> None:
        self.store = store
        self.exporter = exporter
        self.idle_threshold_s = idle_threshold_s
        self.min_intent_count = min_intent_count
        self.max_samples_per_intent = max_samples_per_intent
        self.poll_interval_s = poll_interval_s
        self.buffer_supplier = buffer_supplier
        self.on_cycle = on_cycle

        self._idle_event = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_activity_ns = time.perf_counter_ns()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Optional[DaydreamResult] = None
        self.cycles_run = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the worker thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="undermind-daydream", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the worker thread."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def notify_activity(self) -> None:
        """Call on every keystroke: re-arms the idle watchdog."""
        self._last_activity_ns = time.perf_counter_ns()
        self._idle_event.clear()

    def idle_for(self) -> float:
        """Seconds since the last registered activity."""
        return (time.perf_counter_ns() - self._last_activity_ns) / 1e9

    def set_busy(self, busy: bool) -> None:
        """Mark a prediction or handoff as in-flight (blocks daydreaming)."""
        if busy:
            self._last_activity_ns = time.perf_counter_ns()
            self._idle_event.clear()

    def force_idle(self) -> None:
        """Force the idle condition for the very next cycle.

        Backdates the activity clock so the watchdog sees full idle time and
        wakes immediately. Used by --daydream-once and after a handoff.
        """
        self._last_activity_ns = time.perf_counter_ns() - int(
            (self.idle_threshold_s + 1.0) * 1e9
        )
        self._idle_event.set()
        self._wake.set()

    # ------------------------------------------------------------------
    # worker loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            idle_for = (time.perf_counter_ns() - self._last_activity_ns) / 1e9
            if idle_for >= self.idle_threshold_s:
                try:
                    result = self.run_cycle()
                    if result is not None and self.on_cycle is not None:
                        self.on_cycle(result)
                except Exception:
                    pass
                wait_until = time.monotonic() + max(
                    self.idle_threshold_s, self.poll_interval_s
                )
                while not self._stop.is_set() and time.monotonic() < wait_until:
                    if self._last_activity_changed():
                        break
                    time.sleep(self.poll_interval_s)
            else:
                time.sleep(self.poll_interval_s)

    def _last_activity_changed(self) -> bool:
        """True when fresh activity reset the idle clock since we last looked."""
        idle_for = (time.perf_counter_ns() - self._last_activity_ns) / 1e9
        return idle_for < self.idle_threshold_s

    # ------------------------------------------------------------------
    # one cycle
    # ------------------------------------------------------------------

    def run_cycle(self) -> Optional[DaydreamResult]:
        """Flush the buffer, fold inputs into intents, export new snapshots."""
        flushed = self._flush_buffer()
        unprocessed = self.store.fetch_unprocessed()

        if not unprocessed:
            return None

        updated_ids: List[str] = []
        processed_ids: List[int] = []

        for row in unprocessed:
            text = row["text"]
            if not text or not text.strip():
                processed_ids.append(row["id"])
                continue
            sig = compute_signature(text)
            iid = compute_intent_id(text)
            self.store.upsert_intent(iid, sig)
            self.store.add_intent_sample(iid, row["id"])
            self.store.record_input_id_intent(row["id"], iid, sig)
            processed_ids.append(row["id"])
            if iid not in updated_ids:
                updated_ids.append(iid)

        self.store.mark_processed(processed_ids)

        records = self.exporter.export_pending(
            self.min_intent_count, self.max_samples_per_intent
        )

        result = DaydreamResult(
            inputs_processed=len(processed_ids),
            intents_updated=len(updated_ids),
            records_exported=len(records),
            intent_ids=updated_ids,
        )
        self.last_result = result
        self.cycles_run += 1
        return result

    def _flush_buffer(self) -> Optional[int]:
        """Commit the live typed buffer (if any) as a new input row."""
        if self.buffer_supplier is None:
            return None
        text = self.buffer_supplier()
        if not text or not text.strip():
            return None
        return self.store.record_input(text)
