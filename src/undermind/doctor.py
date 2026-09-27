"""
Stack doctor: health census for the five local pipelines.

    uv run undermind --doctor          human-readable report
    uv run undermind --doctor --json   machine-readable report

Pipelines checked:

* Ollama        (primary engine, :11434)      — target model must be present
* Lemonade      (NPU draft/fallback, :13305)  — draft model must be present
* Undermind     (proxy, :11435)               — /api/health, incl. daydream
* OmniRoute     (:20128)                      — 401 counts as up (auth-gated)
* Hermes gateway(:9119 dashboard, daemon census) — headless daemon counts as
  up; a stopped web dashboard is noted in the detail, not an outage

"Orphaned" reports *extra* instances: a second proxy listening on :11435's
sibling, or Hermes gateway processes beyond the expected singleton. Fail-open
throughout — a check that cannot even run reports "degraded", never raises.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen

from undermind import __version__
from undermind.config import Config

OMNIROUTE_URL = "http://127.0.0.1:20128"
HERMES_DASHBOARD_PORT = 9119

# Files the doctor maintains for machines and the supervisor (repo-relative
# unless made absolute elsewhere). The status file is the current census;
# the alert log appends one line per newly-dead pipeline (suppressed per
# pipeline for SUPPRESS_S so a steady state does not spam).
STATUS_FILE = "data/doctor_status.json"
ALERT_LOG = "data/doctor_alerts.log"
# Transition memory for the alert log. File-backed (not in-memory) because
# the supervisor runs the doctor as a fresh process each tick; state must
# survive across ticks for DEGRADED->RECOVERED style transitions to work.
_ALERT_STATE_FILE = "data/doctor_alert_state.json"

# A median serving latency above this on the primary means the bridge is
# effectively limping; surfaced in the Undermind check's detail. Tuned from
# 30 handoffs (2026-09-27): the 27B's MINIMUM observed turn is ~61s and p90 is
# ~334s, so 60s flagged constantly; 300s sits above p90 and means "limping".
PRIMARY_SLOW_MS = 300_000.0

_PS_HERMES_CENSUS = (
    "Get-CimInstance Win32_Process | "
    "Where-Object { $_.CommandLine -match 'hermes_cli.main' } | "
    "ForEach-Object { '{0},{1}' -f $_.ProcessId, "
    "[int]((New-TimeSpan $_.CreationDate (Get-Date)).TotalSeconds) }"
)

# Gateway-family processes younger than this are transients (one-shot
# `hermes -z` runs, plugin subprocesses), not durable orphans.
_LONG_LIVED_S = 120


# ----------------------------------------------------------------------
# injectable probes (tests monkeypatch these)
# ----------------------------------------------------------------------

def _http_json(url: str, timeout: float = 3.0) -> tuple[int, dict]:
    """GET url and return (status, parsed JSON). Raises on transport errors."""
    with urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _http_status(url: str, timeout: float = 3.0) -> int:
    """GET url and return the HTTP status (4xx/5xx included)."""
    try:
        with urlopen(url, timeout=timeout) as resp:
            return resp.status
    except URLError as exc:
        code = getattr(exc, "code", None)
        if code is not None:
            return int(code)
        raise


def _hermes_gateway_pids() -> list[tuple[int, int]]:
    """(pid, age_seconds) of processes running `hermes_cli.main`.

    Empty on non-Windows or when the census cannot run (fail-open).
    """
    if not sys.platform.startswith("win"):
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PS_HERMES_CENSUS],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if "," not in line:
            continue
        pid_text, _, age_text = line.partition(",")
        if pid_text.isdigit() and age_text.isdigit():
            rows.append((int(pid_text), int(age_text)))
    return sorted(rows)


# ----------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------

def check_ollama(config: Config) -> dict:
    url = f"{config.primary.base_url}/api/tags"
    try:
        status, data = _http_json(url)
    except Exception as exc:
        return {"name": "Ollama", "status": "dead", "detail": f"{url}: {exc}"}
    if status != 200:
        return {"name": "Ollama", "status": "degraded", "detail": f"HTTP {status}"}
    names = [m.get("name", "") for m in data.get("models", [])]
    target = config.primary.model
    if target not in names:
        return {
            "name": "Ollama",
            "status": "degraded",
            "detail": f"up, but primary model '{target}' not loaded/available",
        }
    return {
        "name": "Ollama",
        "status": "up",
        "detail": f"primary '{target}' available ({len(names)} models)",
    }


def check_lemonade(config: Config) -> dict:
    url = f"{config.draft.base_url}/v1/models"
    try:
        status, data = _http_json(url)
    except Exception as exc:
        return {"name": "Lemonade", "status": "dead", "detail": f"{url}: {exc}"}
    if status != 200:
        return {"name": "Lemonade", "status": "degraded", "detail": f"HTTP {status}"}
    ids = [m.get("id", "") for m in data.get("data", [])]
    target = config.draft.model
    if target not in ids:
        return {
            "name": "Lemonade",
            "status": "degraded",
            "detail": f"up, but draft model '{target}' not served",
        }
    return {
        "name": "Lemonade",
        "status": "up",
        "detail": f"draft '{target}' served ({len(ids)} models)",
    }


def check_undermind(config: Config) -> dict:
    url = f"http://{config.proxy.host}:{config.proxy.port}/api/health"
    try:
        status, data = _http_json(url)
    except Exception as exc:
        return {"name": "Undermind", "status": "dead", "detail": f"{url}: {exc}"}
    if status != 200 or not data.get("ok"):
        return {"name": "Undermind", "status": "degraded", "detail": f"HTTP {status}"}
    daydream = data.get("daydream") or {}
    mining = "mining" if daydream.get("running") else "miner idle"
    detail = (
        f"v{data.get('version', '?')} on :{config.proxy.port}, "
        f"cache {data.get('cache_entries', 0)}, "
        f"inputs {data.get('inputs', 0)} "
        f"({data.get('unprocessed_inputs', 0)} unprocessed), "
        f"{mining}, cycles {daydream.get('cycles_run', 0)}"
    )

    # Serving mix: is the bridge riding the fallback rung, or limping?
    serving = data.get("serving") or {}
    if serving.get("recent_count"):
        models = serving.get("models") or {}
        median_ms = serving.get("median_latency_ms") or 0
        mix = ", ".join(f"{m} x{n}" for m, n in models.items())
        detail += f"; serving: {mix}, median {median_ms / 1000:.1f}s"
        if serving.get("riding_fallback"):
            return {
                "name": "Undermind",
                "status": "degraded",
                "detail": detail
                + " - RIDING FALLBACK (primary not answering most turns)",
            }
        if median_ms > PRIMARY_SLOW_MS:
            detail += f" - primary slow (median > {PRIMARY_SLOW_MS / 1000:.0f}s)"
    return {"name": "Undermind", "status": "up", "detail": detail}


def check_omniroute(config: Config) -> dict:
    url = f"{OMNIROUTE_URL}/v1/models"
    try:
        status = _http_status(url)
    except Exception as exc:
        return {"name": "OmniRoute", "status": "dead", "detail": f"{url}: {exc}"}
    if status == 401:
        return {
            "name": "OmniRoute",
            "status": "up",
            "detail": "alive (401 auth required - normal unauthenticated)",
        }
    if status == 200:
        return {"name": "OmniRoute", "status": "up", "detail": "models listed"}
    return {"name": "OmniRoute", "status": "degraded", "detail": f"HTTP {status}"}


def _hermes_dashboard_status() -> Optional[int]:
    """HTTP status of the web dashboard (:9119), or None when not serving.

    The dashboard is optional at runtime (the gateway daemons work headless),
    so this is detection only — it never flips the check's verdict.
    """
    url = f"http://127.0.0.1:{HERMES_DASHBOARD_PORT}/"
    try:
        return int(_http_status(url))
    except Exception:
        return None


def check_hermes_gateway(config: Config) -> dict:
    dash = _hermes_dashboard_status()
    if dash is not None:
        web = f"dashboard :{HERMES_DASHBOARD_PORT} HTTP {dash}"
    else:
        web = f"dashboard :{HERMES_DASHBOARD_PORT} not running"
    rows = _hermes_gateway_pids()
    if not rows:
        return {
            "name": "Hermes gateway",
            "status": "dead",
            "detail": f"{web}; no hermes_cli.main process found",
        }
    long_lived = [pid for pid, age in rows if age >= _LONG_LIVED_S]
    transient = [pid for pid, age in rows if age < _LONG_LIVED_S]
    if not long_lived:
        return {
            "name": "Hermes gateway",
            "status": "degraded",
            "detail": (
                f"{web}; only transient process(es) {transient} "
                f"(<{_LONG_LIVED_S}s), no durable daemon"
            ),
        }
    if len(long_lived) > 2:
        return {
            "name": "Hermes gateway",
            "status": "orphaned",
            "detail": (
                f"{web}; {len(long_lived)} durable gateway processes: "
                f"{long_lived}"
            ),
        }
    detail = f"{web}; daemon PID(s) {long_lived}"
    if transient:
        detail += f" (+ transient {transient})"
    if dash is None:
        detail += " (dashboard not started - run: hermes dashboard)"
    return {"name": "Hermes gateway", "status": "up", "detail": detail}


CHECKS = (check_ollama, check_lemonade, check_undermind, check_omniroute, check_hermes_gateway)


# ----------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------

def _status_dir(config: Config):
    """Directory for the status file / alert log (next to the SQLite db)."""
    return Path(config.store.db_path).expanduser().resolve().parent


def _write_status_file(config: Config, results: list, dead: list) -> None:
    """Persist the current census for machines (health panels, watchdogs)."""
    try:
        directory = _status_dir(config)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "version": __version__,
            "verdict": "dead" if dead else "up",
            "dead": dead,
            "results": results,
        }
        (directory / Path(STATUS_FILE).name).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except Exception:
        pass  # fail-open: reporting must never crash the doctor


def _write_alerts(config: Config, results: list, dead: list) -> None:
    """Append state transitions to the alert log (file-backed across ticks).

    Events: DEAD, DEGRADED, ORPHANED on entering that state, RECOVERED on
    returning to up, and RIDE when the bridge starts riding the fallback
    rung (from the Undermind check's RIDING FALLBACK detail). Only
    *transitions* fire, so a supervisor ticking every few minutes does not
    spam steady-state rows. The ``dead`` argument is kept for compatibility;
    statuses are derived from ``results`` directly.
    """
    try:
        directory = _status_dir(config)
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / Path(ALERT_LOG).name
        state_path = directory / Path(_ALERT_STATE_FILE).name
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        details = {r["name"]: r["detail"] for r in results}

        events: list[tuple[str, str, str]] = []
        for r in results:
            name = r["name"]
            status = str(r.get("status") or "up")
            prev = state.get(name)
            if status == "up":
                if prev and prev != "up":
                    events.append((name, "RECOVERED", ""))
            elif prev != status:
                kind = status.upper()  # DEAD / DEGRADED / ORPHANED
                events.append((name, kind, str(details.get(name, ""))))
            state[name] = status

        riding = any(
            "RIDING FALLBACK" in str(r.get("detail") or "") for r in results
        )
        if riding and not state.get("_riding"):
            under = next(
                (str(r.get("detail") or "") for r in results if r["name"] == "Undermind"),
                "bridge riding the fallback rung",
            )
            events.append(("Undermind", "RIDE", under))
        state["_riding"] = riding

        state_path.write_text(json.dumps(state), encoding="utf-8")
        if events:
            lines = []
            for name, kind, det in events:
                lines.append(f"{stamp} {kind} {name}: {det}" if det else f"{stamp} {kind} {name}")
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
    except Exception:
        pass  # fail-open


def run_doctor(config: Config, as_json: bool = False) -> int:
    """Run all checks; print a report; return 0 iff nothing is dead.

    Every run also refreshes the machine-readable status file and appends
    any new state transitions (DEAD / DEGRADED / ORPHANED / RECOVERED /
    fallback RIDE) to the alert log, so a supervisor or dashboard can
    surface outages without a human running this.
    """
    results = []
    for check in CHECKS:
        try:
            results.append(check(config))
        except Exception as exc:  # fail-open: a broken check is a degraded row
            results.append(
                {
                    "name": getattr(check, "__name__", "check"),
                    "status": "degraded",
                    "detail": f"check itself failed: {exc}",
                }
            )

    dead = [r["name"] for r in results if r["status"] == "dead"]
    degraded = [r["name"] for r in results if r["status"] == "degraded"]
    orphaned = [r["name"] for r in results if r["status"] == "orphaned"]

    _write_status_file(config, results, dead)
    _write_alerts(config, results, dead)

    if as_json:
        print(json.dumps({"results": results}, indent=2))
    else:
        width = max(len(r["name"]) for r in results)
        print(f"Undermind doctor v{__version__} - local pipeline census")
        print("-" * 72)
        for r in results:
            print(f"  {r['name']:<{width}}  [{r['status']:>8}]  {r['detail']}")
        print("-" * 72)

    if as_json:
        return 1 if dead else 0
    if dead:
        print(f"verdict: DEAD - {', '.join(dead)} (exit 1)")
    elif degraded or orphaned:
        bits = []
        if degraded:
            bits.append(f"degraded: {', '.join(degraded)}")
        if orphaned:
            bits.append(f"orphaned: {', '.join(orphaned)}")
        print(f"verdict: UP with issues - {'; '.join(bits)} (exit 0)")
    else:
        print("verdict: ALL UP (exit 0)")
    return 1 if dead else 0
