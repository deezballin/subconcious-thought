"""
Stack doctor: health census for the five local pipelines.

    uv run undermind --doctor          human-readable report
    uv run undermind --doctor --json   machine-readable report

Pipelines checked:

* Ollama        (primary engine, :11434)      — target model must be present
* Lemonade      (NPU draft/fallback, :13305)  — draft model must be present
* Undermind     (proxy, :11435)               — /api/health, incl. daydream
* OmniRoute     (:20128)                      — 401 counts as up (auth-gated)
* Hermes gateway(:8000 web, daemon census)    — headless daemon counts as up

"Orphaned" reports *extra* instances: a second proxy listening on :11435's
sibling, or Hermes gateway processes beyond the expected singleton. Fail-open
throughout — a check that cannot even run reports "degraded", never raises.
"""

from __future__ import annotations

import json
import subprocess
import sys
from urllib.error import URLError
from urllib.request import urlopen

from undermind import __version__
from undermind.config import Config

OMNIROUTE_URL = "http://127.0.0.1:20128"
HERMES_GATEWAY_PORT = 8000

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


def check_hermes_gateway(config: Config) -> dict:
    web_url = f"http://127.0.0.1:{HERMES_GATEWAY_PORT}/"
    try:
        status = _http_status(web_url)
        web = f"web UI :{HERMES_GATEWAY_PORT} HTTP {status}"
    except Exception:
        web = f"web UI :{HERMES_GATEWAY_PORT} dark"
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
    return {"name": "Hermes gateway", "status": "up", "detail": detail}


CHECKS = (check_ollama, check_lemonade, check_undermind, check_omniroute, check_hermes_gateway)


# ----------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------

def run_doctor(config: Config, as_json: bool = False) -> int:
    """Run all checks; print a report; return 0 iff nothing is dead."""
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

    if as_json:
        print(json.dumps({"results": results}, indent=2))
    else:
        width = max(len(r["name"]) for r in results)
        print(f"Undermind doctor v{__version__} - local pipeline census")
        print("-" * 72)
        for r in results:
            print(f"  {r['name']:<{width}}  [{r['status']:>8}]  {r['detail']}")
        print("-" * 72)

    dead = [r["name"] for r in results if r["status"] == "dead"]
    degraded = [r["name"] for r in results if r["status"] == "degraded"]
    orphaned = [r["name"] for r in results if r["status"] == "orphaned"]
    if as_json:
        return 1 if dead else 0
    if dead:
        print(f"verdict: DEAD — {', '.join(dead)} (exit 1)")
    elif degraded or orphaned:
        bits = []
        if degraded:
            bits.append(f"degraded: {', '.join(degraded)}")
        if orphaned:
            bits.append(f"orphaned: {', '.join(orphaned)}")
        print(f"verdict: UP with issues — {'; '.join(bits)} (exit 0)")
    else:
        print("verdict: ALL UP (exit 0)")
    return 1 if dead else 0
