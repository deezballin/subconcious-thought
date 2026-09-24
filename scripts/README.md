# scripts/ — sandbox demo tooling

Everything the live ISO needs to demo itself after boot, so the 30-minute
sandbox window is spent watching instead of installing.

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
