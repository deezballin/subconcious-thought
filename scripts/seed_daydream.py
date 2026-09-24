#!/usr/bin/env python3
"""
seed_daydream.py — record three paraphrased inputs for the sandbox demo.

Writes directly into the configured SQLite store, mirroring what typed
sessions accumulate, so `undermind --daydream-once` has something to fold
into a shared intent. Run from anywhere; paths follow config.toml.
"""

from __future__ import annotations

import os
import sys

UNDERMIND_HOME = os.environ.get("UNDERMIND_HOME", "/opt/undermind")
if UNDERMIND_HOME not in sys.path:
    sys.path.insert(0, UNDERMIND_HOME)
SRC = os.path.join(UNDERMIND_HOME, "src")
if os.path.isdir(SRC) and SRC not in sys.path:
    sys.path.insert(0, SRC)

from undermind.config import load_config
from undermind.store import UndermindStore

PARAPHRASES = (
    "fix the login bug",
    "please fix login bugs",
    "can you fix the login bug",
)


def main() -> int:
    config = load_config(None)
    db_path = config.store.db_path
    if not os.path.isabs(db_path):
        db_path = os.path.join(UNDERMIND_HOME, db_path)
    store = UndermindStore(db_path)
    try:
        for text in PARAPHRASES:
            store.record_input(text)
        print(f"seeded {len(PARAPHRASES)} inputs into {db_path}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
