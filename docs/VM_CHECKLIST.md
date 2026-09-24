# VM Checklist — Local Build & Pre-Snapshot Verification for the Live ISO

Build everything here on your local VM where time is free. The 30-minute
sandbox window only spends time booting and watching. Work top to bottom;
**do not snapshot until every gate in Section 6 passes.**

---

## 0. VM specs and OS choice

| Requirement | Value | Why |
|---|---|---|
| OS | **Debian 12** or **Ubuntu 24.04** | Ships Python 3.11+ (Undermind needs `tomllib`, 3.11+) |
| RAM | 4 GB min, 8 GB comfortable | Draft model + primary model + tests |
| Disk | 15 GB free | Model (~400 MB) + toolchain + ISO build space |
| CPU | 2+ cores | Tiny model runs fine on CPU for a demo |
| OS to avoid | Alpine/musl distros | pynput + Ollama friction; not worth it for a demo |

Keep the sandbox ISO the **same OS family** you build on, so nothing behaves
differently after boot.

> **Netboot note (learned 2026-09-24):** the `ubuntu-XX.XX-mini-iso` is a
> **debian-installer netboot image** — it contains no system, only an installer
> that downloads regular Ubuntu from the archive mirror. Consequences: the VM
> must stay online for the entire install (a dropped connection means starting
> over), the install takes a while because it *is* the download, and the result
> is a standard minimal Ubuntu — ideal for squashing into the live ISO. The
> installer is single-threaded: **1–2 vCPUs is plenty**; over-provisioning vCPUs
> on a busy host causes guest `soft lockup` watchdog spam (seen on 4 vCPUs).
> VirtualBox tuning that helped: `--paravirtprovider kvm`, 4 GB RAM, NAT with a
> `host:2222 -> guest:22` port-forward for SSH (no Guest Additions needed).

## 1. Base system packages

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  curl ca-certificates git \
  xvfb x11-utils xdotool \
  sqlite3
```

| Package | Role |
|---|---|
| `python3` (3.11+) | Runtime |
| `curl` / `ca-certificates` | Ollama installer + health checks + phone-home |
| `xvfb` + `x11-utils` | Virtual display for the keystroke demo |
| `xdotool` | Injects typed text into the live listener loop |
| `sqlite3` | Inspecting/seeding the DB during verification |

Ollama installs via its own script (Section 3) — no apt package needed.

## 2. Undermind install

Place the repo at a fixed path so service files and config reference one
location forever:

```bash
sudo mkdir -p /opt/undermind
sudo chown $USER /opt/undermind
# copy or clone the project into /opt/undermind, then:
cd /opt/undermind
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install requests pynput   # already in pyproject; explicit for safety
```

Verify the CLI resolves:

```bash
/opt/undermind/.venv/bin/undermind --version
# expected: undermind 0.2.0
```

## 3. Ollama + tiny backend model

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
ollama pull qwen2.5:0.5b
```

Verify:

```bash
ollama list                      # qwen2.5:0.5b present
curl -s http://localhost:11434/api/tags | grep -o qwen2.5:0.5b
```

The tiny model runs the demo in seconds on CPU. Krios 9B stays on the local
Lemonade machine — the sandbox proves the pipeline, not the NPU.

## 4. Undermind config (bake this exact file)

`/opt/undermind/config.toml`:

```toml
[draft]
kind = "ollama"
base_url = "http://localhost:11434"
model = "qwen2.5:0.5b"
temperature = 0.2
max_tokens = 32
debounce_s = 0.25
timeout_s = 30.0
min_buffer_chars = 4

[confidence]
threshold = 0.95
mode = "latest"
min_tokens = 1
min_chars = 0

[primary]
kind = "ollama"
base_url = "http://localhost:11434"
model = "qwen2.5:0.5b"
system_prompt = "You are the primary reasoning engine of a local assistant pipeline. Execute the handed-off text branch directly and respond concisely."
timeout_s = 120.0
retries = 1

[daydream]
idle_threshold_s = 5.0
min_intent_count = 2
export_path = "data/training_export.jsonl"
poll_interval_s = 0.5
max_samples_per_intent = 20

[store]
db_path = "data/undermind.db"

[proxy]
host = "0.0.0.0"
port = 11435
cache_ttl_s = 30.0
```

Two deliberate choices: `min_tokens = 1` / `min_chars = 0` make the 95%
crossing fire easily with the tiny model (keep the stricter defaults for real
use), and `host = "0.0.0.0"` lets the sandbox's port-forward reach the proxy.

## 5. Keyboard-demo tooling (only if you want live typing in the sandbox)

```bash
# Sanity check on the local VM before it goes into the ISO:
xvfb-run -a bash -c '
  Xvfb :99 -screen 0 1280x800x24 &
  sleep 1
  export DISPLAY=:99
  # open a terminal/emulator here if you want visible typing, then:
  xdotool type --delay 60 "The capital of France is"
'
```

If `xdotool type` injects without errors, the demo path works headless.
The proxy and daydream demos (Section 6, gates 4–6) need none of this.

## 6. Verification gates — all must pass before snapshot

Run from `/opt/undermind` with the venv active.

**Gate 1 — Python version**

```bash
python3 --version   # must be >= 3.11
```

**Gate 2 — Offline test suite (101 tests, ~5 s)**

```bash
.venv/bin/python -m unittest discover -s src/undermind -p "test_*.py"
# expected: OK (skipped=1)   ← the skip is the live-backend integration test
```

**Gate 3 — Proxy confidence crossing (Feature 1)**

```bash
ollama serve &      # if not running as a service
.venv/bin/python -m undermind.proxy &
sleep 3
curl -s http://localhost:11435/api/generate \
  -d '{"model":"qwen2.5:0.5b","prompt":"The capital of France is","stream":false}'
```

Expected JSON contains `"confidence_crossed": true` and a
`"confidence"` ≥ 0.95 inside the `"undermind"` block.

**Gate 4 — No-crossing fallback**

```bash
curl -s http://localhost:11435/api/generate \
  -d '{"model":"qwen2.5:0.5b","prompt":"purple elephant disco","stream":false}'
```

Expected: `"confidence_crossed": false` — the primary executed the raw prompt.

**Gate 5 — Daydream intents + export (Feature 2)**

```bash
# Seed three paraphrases of one intent directly into the DB:
.venv/bin/python - <<'EOF'
from undermind.store import UndermindStore
store = UndermindStore("data/undermind.db")
for text in ("fix the login bug", "please fix login bugs", "can you fix the login bug"):
    store.record_input(text)
store.close()
EOF
.venv/bin/undermind --daydream-once
```

Expected output: `inputs=3 intents=1 exported=1` and one line appended to
`data/training_export.jsonl` with `"count": 3` and a single `intent_id`.

**Gate 6 — Proxy model listing**

```bash
curl -s http://localhost:11435/api/tags
# expected: {"models":[{"name":"qwen2.5:0.5b",...}]}
```

**Gate 7 — Live keystroke loop under Xvfb (Feature 1 through the real listener)**

```bash
xvfb-run -a bash -c '
  cd /opt/undermind
  export DISPLAY=:99
  .venv/bin/undermind &
  sleep 6
  xdotool type --delay 80 "The capital of France is"
  sleep 8
'
sqlite3 data/undermind.db "SELECT trigger, status, round(confidence,3) FROM handoffs ORDER BY id DESC LIMIT 1;"
# expected row: confidence_0.95 | ok | 0.9xx
```

**Gate 8 — Reset for a clean image**

After all gates pass, wipe demo state so the sandbox starts pristine:

```bash
rm -rf data/
.venv/bin/undermind --status   # everything zeroed
```

## 7. Bake in the boot autorun

The sandbox-side script does the work after startup. Companion files live in
`scripts/` (written in the follow-up step):

| File | Role |
|---|---|
| `scripts/vm_bootstrap.sh` | One-paste setup of a fresh VM: automates sections 1–7 (packages → repo → venv → Ollama → config → units → smoke test) |
| `scripts/snapshot_cleanup.sh` | Pre-snapshot wipe: stops units, removes `data/`, logs, caches; verifies the section 9 "do not" list and reports PRISTINE |
| `scripts/sandbox_autorun.sh` | Starts backend → proxy → runs both demos → phone-home → serves status page |
| `scripts/status_page.py` | Tiny HTTP status page on :8080 for browser watching |
| `scripts/undermind-proxy.service` | systemd unit for the proxy |
| `scripts/undermind-demo.service` | systemd unit that runs the autorun at boot |

**Automated alternative for sections 1–7.** Instead of running this checklist
by hand, copy the repo onto the VM and run one command:

```bash
sudo bash ~/undermind/scripts/vm_bootstrap.sh ~/undermind
```

It is idempotent (safe to re-run), writes the demo `config.toml` only if
absent, and finishes by printing the Gate 9 reboot instructions.

Install them on the VM now and verify the boot path **before** snapshotting:

```bash
sudo cp scripts/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable undermind-proxy.service undermind-demo.service
sudo reboot
# after reboot:
systemctl status undermind-proxy.service undermind-demo.service
curl -s http://localhost:8080/   # status page serving
```

Gate 9 — reboot test: both units `active`, status page responds, and the
phone-home webhook received the boot report. A failed boot inside the sandbox
is a wasted window; a failed boot on the VM is a 60-second fix.

## 8. ISO build + upload

- Build the ISO with your live-boot tool on the VM, using this exact system
- Target **< 1.5 GB** (squashfs-compressed); the 400 MB model and ~1 GB
  toolchain compress well
- Upload the ISO to the sandbox as a pre-staged artifact **before** starting
  the 30-minute window if the platform allows it
- If the sandbox turns out to be container-based, ship the same system as a
  rootfs tarball / OCI image with the same systemd-free autorun entrypoint

## 9. Snapshot checklist

Do:
- [ ] All gates 1–8 pass
- [ ] Reboot test (Gate 9) passes
- [ ] `sudo bash /opt/undermind/scripts/snapshot_cleanup.sh` ran last and reported PRISTINE
- [ ] `data/` wiped — pristine first boot
- [ ] `config.toml` present with tiny-model settings
- [ ] Phone-home URL baked into the autorun env (no secrets — a request-bin
      or your own endpoint, never an API key)
- [ ] ISO size under target
- [ ] ISO uploaded/attached before the clock starts

Do not:
- [ ] No `data/` or `__pycache__/` in the image
- [ ] No API keys, tokens, or personal paths in any baked file
- [ ] No Krios/Lemonade endpoints — the sandbox uses only local Ollama

---

## Appendix — one-paste verification block

```bash
cd /opt/undermind && . .venv/bin/activate && \
python3 --version && \
python -m unittest discover -s src/undermind -p "test_*.py" 2>&1 | tail -2 && \
curl -s http://localhost:11435/api/generate -d '{"model":"qwen2.5:0.5b","prompt":"The capital of France is","stream":false}' | python -m json.tool | grep -E "confidence_crossed|confidence" && \
undermind --daydream-once && \
tail -1 data/training_export.jsonl | python -m json.tool | head -8
```

If every command in this block returns the expected values, snapshot the ISO.
