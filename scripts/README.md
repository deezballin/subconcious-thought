# scripts/ — sandbox demo tooling + Windows host tooling

Everything the live ISO needs to demo itself after boot, so the 30-minute
sandbox window is spent watching instead of installing.

## Windows host tooling (live Hermes bridge)

| File | Role |
|---|---|
| `undermind_supervisor.ps1` | Mid-session crash supervisor: every 5 min, if :11435 is dead, relaunch the proxy detached (hidden); each tick also runs the stack doctor so `data/doctor_status.json` + `data/doctor_alerts.log` stay fresh. Singleton via a `Global\UndermindSupervisor` mutex (second copies log one line and exit). Logs to `.freebuff/supervisor.log` |
| `undermind_supervisor.vbs` | Hidden logon launcher for the supervisor. Install: copy to `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` (already installed) |
| `hermes_dashboard.ps1` | Ensures the Hermes web dashboard is running on :9119 (idempotent: exits when the port already serves). Logs to `.freebuff/dashboard.log` |
| `hermes_dashboard.vbs` | Hidden logon launcher for the dashboard. Install: copy to `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` (already installed) |
| `register_watchdog_task.ps1` | OPTIONAL one-click admin script: registers the `UndermindProxyWatchdog` scheduled task (every 5 min + at logon). Only needed if you prefer Task Scheduler over the Startup VBS |
| `undermind_proxy_watchdog.ps1` | Watchdog tick used by the scheduled task: `undermind --proxy` (self-guarding no-op when the port is already serving) |

Reboot persistence rides on the existing `gateway-service\Undermind_Proxy.vbs`
Startup launcher; the supervisor adds mid-session revival. The proxy itself
also hosts the daydream miner (auto-mines on input idle) and `/api/health`.
Stack census: `uv run undermind --doctor` (add `--doctor-json` for machines).
Every doctor run refreshes `data/doctor_status.json` (machine-readable
verdict + per-pipeline results) and appends DEAD / RECOVERED transitions to
`data/doctor_alerts.log`, and the supervisor tick keeps both current. The
Undermind row also reports the recent serving mix and flags **RIDING
FALLBACK** when the Lemonade rung is answering most turns instead of the 27B
primary, or "primary slow" when median latency exceeds 60s.



| File | Role |
|---|---|
| `sandbox_autorun.sh` | Boot sequence: wait for Ollama → start status page → start proxy → Demo 1 (confidence crossing via `/api/generate`) → Demo 2 (daydream intents + `training_export.jsonl`) → optional keyboard demo → test suite → phone-home JSON report → keep status page alive |
| `status_page.py` | Self-refreshing dashboard on **:8080**: report steps, recent handoffs (from SQLite), export tail, demo log tail. Stdlib only |
| `seed_daydream.py` | Records three paraphrased inputs ("fix the login bug" family) into the store so `--daydream-once` has something to merge |
| `vm_bootstrap.sh` | One-paste setup of a fresh VM (checklist sections 1–7): apt packages → repo to `/opt/undermind` → venv + editable install → Ollama + `qwen2.5:0.5b` → demo `config.toml` → service user + units → offline test-suite smoke gate. Idempotent |
| `snapshot_cleanup.sh` | Pre-snapshot wipe (checklist section 9 "do not" list): stops units (leaves them enabled), removes `data/`, `__pycache__`, undermind logs/tmp artifacts, vacuums journal + apt cache, then verifies and reports PRISTINE |
| `undermind-proxy.service` | systemd unit: `python -m undermind.proxy` on :11435 |
| `undermind-demo.service` | systemd unit: runs `sandbox_autorun.sh` at boot |

## Environment variables (set in the unit or the shell)

| Variable | Default | Purpose |
|---|---|---|
| `UNDERMIND_HOME` | `/opt/undermind` | Repo location on the image |
| `UNDERMIND_PROXY_HOST` | `127.0.0.1:11435` | Where the proxy answers |
| `UNDERMIND_STATUS_PORT` | `8080` | Status page port |
| `UNDERMIND_REPORT_URL` | *(unset)* | Webhook that receives the JSON report at each phase — point at a request-bin or your own endpoint |
| `UNDERMIND_DEMO_MODEL` | `qwen2.5:0.5b` | Model used for the demos |
| `UNDERMIND_DEMO_KEYBOARD` | `0` | `1` enables the live xdotool typing demo |
| `UNDERMIND_LOG_FILE` | `/var/log/undermind-demo.log` | Autorun log (also shown on the status page) |

No secrets belong here — the report URL is a throwaway endpoint, never an
API key.

## Install on the VM (before building the ISO)

```bash
sudo useradd -r -s /usr/sbin/nologin undermind 2>/dev/null || true
sudo chown -R undermind:undermind /opt/undermind
sudo cp scripts/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable undermind-proxy.service undermind-demo.service
```

## Gate 9 — reboot test (run BEFORE snapshotting)

```bash
sudo reboot
# after it comes back:
systemctl status undermind-proxy.service undermind-demo.service
curl -s http://localhost:8080/ | head -5          # status page serving
cat /tmp/undermind-report.json | python3 -m json.tool   # phase: complete
```

Expected: both units `active`, the report shows every step `ok`
(`demo3_keyboard` may be `skip`), and the phone-home endpoint received the
report. The status page then keeps serving — open the sandbox's forwarded
:8080 in a browser and watch the handoffs arrive.

## What the report looks like

```json
{"phase": "complete", "boot_ts": "2026-09-23T14:02:11Z", "host": "sandbox",
 "steps": [
   {"step": "ollama_ready",     "status": "ok",   "detail": "Ollama answered /api/tags", "elapsed_s": 4},
   {"step": "proxy_ready",      "status": "ok",   "detail": "answering at http://127.0.0.1:11435", "elapsed_s": 9},
   {"step": "demo1_crossing",   "status": "ok",   "detail": "crossed at confidence 0.9841", "elapsed_s": 15},
   {"step": "demo2_daydream",   "status": "ok",   "detail": "inputs=3 intents=1 exported=1", "elapsed_s": 21},
   {"step": "demo3_keyboard",   "status": "skip", "detail": "UNDERMIND_DEMO_KEYBOARD!=1", "elapsed_s": 21},
   {"step": "test_suite",       "status": "ok",   "detail": "OK (skipped=1)", "elapsed_s": 27}
 ]}
```
