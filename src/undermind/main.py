"""
Main orchestrator: wires listener → predictor → handoff → daydream.

The Pipeline runs a 20 ms tick. Each tick:
1. feeds the current buffer to the predictor (debounced streamed prediction),
2. hands a confident branch to the primary pipeline when the 95% crossing
   fires,
3. keeps the idle watchdog armed for the background daydreaming worker.

CLI:
    uv run undermind                     start the full pipeline
    uv run undermind --config PATH       use a specific config file
    uv run undermind --daydream-once     run one daydream cycle and exit
    uv run undermind --status            print pipeline/DB status
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from undermind import __version__
from undermind.config import Config, load_config
from undermind.confidence import ConfidenceTracker, Crossing
from undermind.daydream import DaydreamResult, DaydreamWorker
from undermind.exporter import Exporter
from undermind.handoff import UniversalHandoff
from undermind.listener import Listener
from undermind.predictor import DraftPredictor
from undermind.providers import build_draft_client, build_primary_provider
from undermind.store import UndermindStore


def validate_draft_model(config: Config, draft_client) -> bool:
    """Check that the configured draft model exists; print help if not."""
    try:
        available = draft_client.list_models()
    except Exception as exc:
        print(f"[warn] Cannot reach draft backend at {config.draft.base_url}: {exc}")
        return False
    if config.draft.model in available:
        return True
    print(
        f"[warn] Draft model '{config.draft.model}' not found on {config.draft.base_url}."
    )
    matches = [m for m in available if "qwen" in m.lower()]
    hints = matches if matches else available
    for model_id in hints[:10]:
        print(f"       available: {model_id}")
    if not hints:
        print("       (no models reported by the backend)")
    print("       Set [draft].model in config.toml to one of these ids.")
    return False


class Pipeline:
    """Full Undermind pipeline: typing prediction, handoff, daydreaming."""

    def __init__(
        self,
        config: Config,
        store: UndermindStore | None = None,
        quiet: bool = False,
    ) -> None:
        self.config = config
        self.quiet = quiet
        self.store = store or UndermindStore(config.store.db_path)
        self._owns_store = store is None

        self.draft_client = build_draft_client(config)
        self.predictor = DraftPredictor(
            draft_client=self.draft_client,
            debounce_s=config.draft.debounce_s,
            temperature=config.draft.temperature,
            max_tokens=config.draft.max_tokens,
            min_buffer_chars=config.draft.min_buffer_chars,
            confidence_kwargs={
                "threshold": config.confidence.threshold,
                "mode": config.confidence.mode,
                "ewma_alpha": config.confidence.ewma_alpha,
                "min_tokens": config.confidence.min_tokens,
                "min_chars": config.confidence.min_chars,
            },
            on_confident=self._on_confident,
            on_suggestion=self._on_suggestion,
            on_error=self._on_error,
        )
        self.handoff = UniversalHandoff(
            provider=build_primary_provider(config),
            provider_name=config.primary.kind,
            model=config.primary.model,
            system_prompt=config.primary.system_prompt,
            retries=config.primary.retries,
        )
        self.exporter = Exporter(self.store, config.daydream.export_path)
        self.daydream = DaydreamWorker(
            store=self.store,
            exporter=self.exporter,
            idle_threshold_s=config.daydream.idle_threshold_s,
            min_intent_count=config.daydream.min_intent_count,
            max_samples_per_intent=config.daydream.max_samples_per_intent,
            poll_interval_s=config.daydream.poll_interval_s,
            buffer_supplier=self.listener_buffer,
            on_cycle=self._on_daydream_cycle,
        )
        self.listener = Listener()
        self._running = False
        self._tick_thread: threading.Thread | None = None
        self.handoff_count = 0
        self.suggestion_count = 0

    # ------------------------------------------------------------------
    # callbacks
    # ------------------------------------------------------------------

    def _on_confident(self, branch: str, crossing: Crossing, tracker: ConfidenceTracker) -> None:
        """Threshold crossed: bypass the local loop, execute on the primary."""
        self.daydream.set_busy(True)
        conf = crossing.confidence
        self._print(
            f"\r[HANDOFF {conf*100:.1f}% @ {crossing.token_index + 1} tokens, "
            f"{tracker.ms_since_first_token() or 0:.0f}ms] branch: {branch[:72]}"
        )
        self.daydream.force_idle()
        try:
            result = self.handoff.send_and_record(
                branch,
                self.store,
                trigger=f"confidence_{self.config.confidence.threshold:.2f}",
                confidence=conf,
            )
            self.handoff_count += 1
            self._print(f"[PRIMARY] {result[:200]}")
        except Exception as exc:
            self._print(f"[HANDOFF ERROR] {exc}")

    def _on_suggestion(self, branch: str, tracker: ConfidenceTracker) -> None:
        """Stream finished without crossing: show it as a suggestion."""
        self.suggestion_count += 1
        self._print(
            f"\r[suggest {tracker.current_confidence*100:.0f}%] "
            f"{tracker.text[:56]}"
        )

    def _on_error(self, message: str) -> None:
        self._print(f"[draft error] {message}")

    def _on_daydream_cycle(self, result: DaydreamResult) -> None:
        self._print(
            f"[daydream] inputs={result.inputs_processed} "
            f"intents={result.intents_updated} "
            f"exported={result.records_exported}"
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start listener, daydream worker, and the pipeline tick."""
        self._running = True
        self.listener.start()
        self.daydream.start()
        self._tick_thread = threading.Thread(
            target=self._tick_loop, name="undermind-pipeline", daemon=True
        )
        self._tick_thread.start()
        self._print(
            f"Undermind {__version__} started — "
            f"draft={self.config.draft.model} @ {self.config.draft.base_url}, "
            f"primary={self.config.primary.kind}, "
            f"confidence threshold={self.config.confidence.threshold:.0%}. "
            "Type anywhere; Ctrl+C here to stop."
        )

    def stop(self) -> None:
        """Stop all threads and flush the live buffer into the store."""
        self._running = False
        if self._tick_thread is not None:
            self._tick_thread.join(timeout=2)
            self._tick_thread = None
        self.predictor.cancel()
        self.daydream.stop()
        self.listener.stop()
        text = self.listener.take_buffer()
        if text.strip():
            self.store.record_input(text)
        if self._owns_store:
            self.store.close()
        self._print("Undermind stopped.")

    # ------------------------------------------------------------------
    # tick loop
    # ------------------------------------------------------------------

    def _tick_loop(self) -> None:
        while self._running:
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(0.02)

    def tick(self) -> None:
        """One pipeline tick: keystroke feed, prediction poll, busy tracking."""
        buffer_text = self.listener.get_buffer()
        if buffer_text != getattr(self, "_last_buffer", None):
            self.daydream.notify_activity()
        self.predictor.notify_keystroke(buffer_text)
        if self.predictor.is_busy():
            self.daydream.set_busy(True)
        elif buffer_text == getattr(self, "_last_buffer", None):
            self.daydream.set_busy(False)
        self._last_buffer = buffer_text
        if self.listener.consume_submit():
            text = self.listener.take_buffer()
            if text.strip():
                self.store.record_input(text)
                self._print(f"[submitted] {text[:72]}")
        self.predictor.poll()

    def listener_buffer(self) -> str | None:
        """Buffer supplier for the daydream flush (called only when idle)."""
        if self.listener.idle_seconds() < self.config.daydream.idle_threshold_s:
            return None
        return self.listener.take_buffer()

    # ------------------------------------------------------------------
    # misc
    # ------------------------------------------------------------------

    def _print(self, message: str) -> None:
        if not self.quiet:
            print(message, flush=True)

    def status(self) -> dict:
        return {
            "version": __version__,
            "draft_model": self.config.draft.model,
            "draft_base_url": self.config.draft.base_url,
            "primary_kind": self.config.primary.kind,
            "confidence_threshold": self.config.confidence.threshold,
            "handoffs": self.store.count_handoffs(),
            "inputs": self.store.count_inputs(),
            "intents": len(self.store.list_intents(min_count=1)),
            "daydream_cycles": self.daydream.cycles_run,
            "predictions_fired": self.handoff_count,
            "suggestions": self.suggestion_count,
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="undermind",
        description="Undermind: local inline prediction + fast handoff + daydreaming.",
    )
    parser.add_argument("--config", default=None, help="Path to config.toml")
    parser.add_argument(
        "--daydream-once",
        action="store_true",
        help="Run one daydream cycle (flush, intents, export) and exit",
    )
    parser.add_argument(
        "--status", action="store_true", help="Print pipeline status and exit"
    )
    parser.add_argument(
        "--version", action="version", version=f"undermind {__version__}"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = load_config(args.config)

    if args.daydream_once:
        store = UndermindStore(config.store.db_path)
        exporter = Exporter(store, config.daydream.export_path)
        worker = DaydreamWorker(
            store=store,
            exporter=exporter,
            idle_threshold_s=config.daydream.idle_threshold_s,
            min_intent_count=config.daydream.min_intent_count,
            max_samples_per_intent=config.daydream.max_samples_per_intent,
        )
        worker.force_idle()
        result = worker.run_cycle()
        if result is None:
            print("Daydream: nothing new to process.")
        else:
            print(
                f"Daydream: inputs={result.inputs_processed} "
                f"intents={result.intents_updated} "
                f"exported={result.records_exported} -> "
                f"{config.daydream.export_path}"
            )
        store.close()
        return 0

    pipeline = Pipeline(config)
    if args.status:
        for key, value in pipeline.status().items():
            print(f"{key}: {value}")
        pipeline.store.close()
        return 0

    try:
        validate_draft_model(config, pipeline.draft_client)
        pipeline.start()
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        pipeline.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
