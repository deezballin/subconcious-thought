"""
Keystroke listener: thread-safe character buffer with an idle clock.

Captures printable characters, space, enter, and backspace at the OS level.
The pipeline polls ``get_buffer()``/``take_buffer()`` on its own thread; the
pynput callback only appends, so no generation work ever happens on the
hook thread. Enter records a submit signal; backspace edits the buffer.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from pynput import keyboard

IGNORED_SUPPLEMENTAL = {
    keyboard.Key.shift,
    keyboard.Key.shift_r,
    keyboard.Key.ctrl,
    keyboard.Key.ctrl_r,
    keyboard.Key.alt,
    keyboard.Key.alt_r,
    keyboard.Key.cmd,
    keyboard.Key.cmd_r,
    keyboard.Key.caps_lock,
}


class Listener:
    """System-wide keystroke capture feeding a bounded character buffer."""

    def __init__(self, max_buffer: int = 4000) -> None:
        self.buffer: list[str] = []
        self.running: bool = False
        self.max_buffer = max_buffer
        self.submit_requested: bool = False
        self.last_input_ts: float = 0.0
        self.key_count: int = 0
        self._lock = threading.Lock()
        self._listener: Optional[keyboard.Listener] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # callback (pynput hook thread)
    # ------------------------------------------------------------------

    def on_press(self, key) -> None:
        """Handle one keystroke. Must stay fast; no I/O here."""
        handled = False
        try:
            char = key.char
            if char is not None:
                handled = True
                with self._lock:
                    self._append(char)
        except AttributeError:
            if key == keyboard.Key.space:
                handled = True
                with self._lock:
                    self._append(" ")
            elif key == keyboard.Key.enter:
                handled = True
                with self._lock:
                    self.submit_requested = True
                    self.last_input_ts = time.time()
            elif key == keyboard.Key.backspace:
                handled = True
                with self._lock:
                    if self.buffer:
                        self.buffer.pop()
                    self.last_input_ts = time.time()
                    self.key_count += 1
            # supplemental keys (shift/ctrl/...) are ignored entirely
        if handled:
            self.key_count += 1

    def _append(self, text: str) -> None:
        self.buffer.append(text)
        if len(self.buffer) > self.max_buffer:
            del self.buffer[: len(self.buffer) - self.max_buffer]
        self.last_input_ts = time.time()
        self.key_count += 1

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start capturing keystrokes in a background thread."""
        if self.running:
            return
        self.running = True
        self._listener = keyboard.Listener(on_press=self.on_press)
        self._thread = threading.Thread(
            target=self._run, name="undermind-listener", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        with self._listener as listener:
            listener.join()

    def stop(self) -> None:
        """Stop capturing keystrokes."""
        self.running = False
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # ------------------------------------------------------------------
    # buffer access (pipeline thread)
    # ------------------------------------------------------------------

    def get_buffer(self) -> str:
        """Current buffer contents as a string."""
        with self._lock:
            return "".join(self.buffer)

    def take_buffer(self) -> str:
        """Atomically read and clear the buffer (used by the daydream flush)."""
        with self._lock:
            text = "".join(self.buffer)
            self.buffer.clear()
            return text

    def consume_submit(self) -> bool:
        """True exactly once after Enter was pressed."""
        with self._lock:
            requested = self.submit_requested
            self.submit_requested = False
            return requested

    def idle_seconds(self) -> float:
        """Seconds since the last keystroke (0.0 when nothing typed yet)."""
        with self._lock:
            if self.last_input_ts == 0.0:
                return 0.0
            return time.time() - self.last_input_ts

    def clear(self) -> None:
        """Clear the buffer without recording anything."""
        with self._lock:
            self.buffer.clear()
            self.submit_requested = False
