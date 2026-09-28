"""Undermind self-diagnostic - run at session start, or by the human.

Verifies the working environment before any work resumes: file inventory,
syntax, config parse, test suite, git state, file hashes, journal presence.
Read-only throughout.

Run:  uv run python scripts/self_diag.py
Exit: 0 = all checks pass, 1 = at least one FAILED.

Why this exists: after two sessions of tool-channel corruption (fabricated
outputs, contradictory reads, malformed agent calls), every work session
starts by proving the channel and the baseline are sane. The token line is
a canary: if the agent's run of this script shows anything but the exact
token, or disagrees with the human's own run in their terminal, the channel
is corrupt - stop and report to Dewayne. Record every run's full output in
.freebuff/journal.md.
"""

from __future__ import annotations

import hashlib
import io
import py_compile
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "SELF-DIAG-TOKEN-9Q4F"

EXPECTED_FILES = [
    "src/undermind/config.py",
    "src/undermind/store.py",
    "src/undermind/proxy.py",
    "src/undermind/adversary.py",
    "src/undermind/test_adversary.py",
    "hermes_plugin/undermind/__init__.py",
    "config.example.toml",
    "config.toml",
    "docs/ADVERSARY_DESIGN.md",
    "docs/RESUME_ADVERSARY.md",
    ".freebuff/journal.md",
    "PLAN.md",
]

HASHED_FILES = [
    "src/undermind/config.py",
    "src/undermind/store.py",
    "src/undermind/proxy.py",
    "src/undermind/adversary.py",
]

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def main() -> int:
    print(f"token {TOKEN}")
    sys.path.insert(0, str(ROOT / "src"))

    # 1. File inventory (catches phantom paths and vanished files).
    for rel in EXPECTED_FILES:
        check(f"exists {rel}", (ROOT / rel).is_file())

    # 2. Syntax (catches garbled edits).
    for rel in [
        "src/undermind/config.py",
        "src/undermind/store.py",
        "src/undermind/proxy.py",
        "src/undermind/adversary.py",
        "src/undermind/test_adversary.py",
        "hermes_plugin/undermind/__init__.py",
    ]:
        try:
            py_compile.compile(str(ROOT / rel), doraise=True)
            check(f"compiles {rel}", True)
        except py_compile.PyCompileError as exc:
            check(f"compiles {rel}", False, str(exc)[:120])

    # 3. Config parse (settles the example-toml question every run).
    try:
        from undermind.config import load_config

        cfg = load_config(str(ROOT / "config.example.toml"))
        check("config.example.toml parses", True, f"adversary.mode={cfg.adversary.mode}")
    except Exception as exc:
        check("config.example.toml parses", False, str(exc)[:120])

    # 4. Test suite (the ground truth of code health).
    try:
        loader = unittest.TestLoader()
        suite = loader.discover(str(ROOT / "src" / "undermind"), pattern="test_*.py")
        runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0)
        result = runner.run(suite)
        check(
            "unit tests",
            result.wasSuccessful(),
            f"{result.testsRun} run, {len(result.failures)} failures,"
            f" {len(result.errors)} errors",
        )
    except Exception as exc:
        check("unit tests", False, str(exc)[:120])

    # 5. Git state (printed for journal comparison, not asserted).
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        ).stdout.strip().splitlines()
        check("git reachable", True, f"HEAD={head}, {len(status)} changed/untracked")
        for line in status:
            print(f"         {line}")
    except Exception as exc:
        check("git reachable", False, str(exc)[:120])

    # 6. Hashes (record in the journal; later runs compare for silent edits).
    for rel in HASHED_FILES:
        digest = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()[:12]
        print(f"       sha256[:12] {rel} = {digest}")

    failed = [name for ok, name in results if not ok]
    print(f"SELF-DIAG: {len(results) - len(failed)}/{len(results)} PASS")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("Channel and baseline sane. Record this output in .freebuff/journal.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
