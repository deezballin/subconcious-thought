#!/usr/bin/env bash
#
# vm_bootstrap.sh — one-paste setup for a freshly installed VM
# (Ubuntu mini / Debian 12, plain install, no desktop)
#
# Usage on the VM (after copying the repo somewhere, e.g. ~/undermind):
#
#   sudo bash ~/undermind/scripts/vm_bootstrap.sh ~/undermind
#
# Optional environment overrides:
#   UNDERMIND_HOME=/opt/undermind        install destination
#   UNDERMIND_DEMO_MODEL=qwen2.5:0.5b    model to pull for the demo
#
# Idempotent: safe to re-run. Everything already done is skipped.
# Implements docs/VM_CHECKLIST.md sections 1-7. After it finishes, run
# Gate 9 (sudo reboot) and then follow section 9's snapshot checklist.
#
# MISSION.md ships inside the repo and therefore travels with this install —
# do not remove it from the image.

set -euo pipefail

SRC="${1:-${UNDERMIND_SRC:-}}"
DEST="${UNDERMIND_HOME:-/opt/undermind}"
DEMO_MODEL="${UNDERMIND_DEMO_MODEL:-qwen2.5:0.5b}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()  { printf '  \033[1;32mok\033[0m  %s\n' "$*"; }
skip(){ printf '  \033[2mskip\033[0m %s (already done)\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo bash $0 [path-to-repo]"; exit 1; }

if [ -z "${SRC}" ]; then
    echo "Usage: sudo bash $0 /path/to/undermind-repo"
    exit 1
fi

if [ ! -f "${SRC}/pyproject.toml" ] || [ ! -d "${SRC}/src/undermind" ]; then
    echo "ERROR: ${SRC} does not look like the undermind repo (need pyproject.toml + src/undermind)"
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Base system packages (checklist section 1)
# ---------------------------------------------------------------------------
log "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    curl ca-certificates git \
    xvfb x11-utils xdotool \
    sqlite3 >/dev/null
ok "apt packages installed"

# ---------------------------------------------------------------------------
# 2. Place the repo at /opt/undermind (checklist section 2)
# ---------------------------------------------------------------------------
log "Placing repo at ${DEST}"
if [ -f "${DEST}/pyproject.toml" ] && [ -d "${DEST}/src/undermind" ]; then
    skip "repo already at ${DEST}"
else
    mkdir -p "${DEST}"
    cp -a "${SRC}/." "${DEST}/"
    rm -rf "${DEST}/data" "${DEST}/.venv"
    find "${DEST}" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
    ok "copied (MISSION.md included: $([ -f "${DEST}/MISSION.md" ] && echo yes || echo 'MISSING!'))"
fi

# ---------------------------------------------------------------------------
# 3. venv + editable install
# ---------------------------------------------------------------------------
log "Creating venv and installing undermind"
if [ -x "${DEST}/.venv/bin/undermind" ]; then
    skip "venv already installed"
else
    python3 -m venv "${DEST}/.venv"
    "${DEST}/.venv/bin/pip" install --quiet --upgrade pip
    "${DEST}/.venv/bin/pip" install --quiet -e "${DEST}"
    "${DEST}/.venv/bin/pip" install --quiet requests pynput   # explicit for safety
    ok "$("${DEST}/.venv/bin/undermind" --version) at ${DEST}/.venv/bin/undermind"
fi

# ---------------------------------------------------------------------------
# 4. Ollama + tiny backend model (checklist section 3)
# ---------------------------------------------------------------------------
log "Ollama"
if command -v ollama >/dev/null 2>&1; then
    skip "ollama already installed"
else
    curl -fsSL https://ollama.com/install.sh | sh >/dev/null
    ok "ollama installed"
fi
systemctl enable --now ollama >/dev/null 2>&1 || true

for i in $(seq 1 60); do
    curl -sS -m 2 http://localhost:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
    [ "$i" -eq 60 ] && { echo "ERROR: Ollama did not answer within 60s"; exit 1; }
done
ok "ollama answering on :11434"

if curl -sS http://localhost:11434/api/tags 2>/dev/null | grep -q "${DEMO_MODEL}"; then
    skip "model ${DEMO_MODEL} already pulled"
else
    ollama pull "${DEMO_MODEL}" >/dev/null 2>&1
    ok "model ${DEMO_MODEL} pulled"
fi

# ---------------------------------------------------------------------------
# 5. Demo config.toml (checklist section 4 — bake only if absent)
# ---------------------------------------------------------------------------
log "Demo config"
if [ -f "${DEST}/config.toml" ]; then
    skip "config.toml already present (not overwriting)"
else
    cat > "${DEST}/config.toml" <<'TOML'
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
TOML
    ok "wrote ${DEST}/config.toml (demo preset)"
fi

# ---------------------------------------------------------------------------
# 6. Service user + systemd units (checklist section 7)
# ---------------------------------------------------------------------------
log "systemd units"
id undermind >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin undermind
cp "${DEST}/scripts/"*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable undermind-proxy.service undermind-demo.service >/dev/null 2>&1
ok "undermind-proxy + undermind-demo enabled (start at boot)"

chown -R undermind:undermind "${DEST}"
ok "${DEST} owned by undermind"

# ---------------------------------------------------------------------------
# 7. Smoke gate: offline test suite (Gate 2)
# ---------------------------------------------------------------------------
log "Smoke gate: offline test suite"
if ( cd "${DEST}" && sudo -u undermind "${DEST}/.venv/bin/python" -m unittest discover \
        -s src/undermind -p "test_*.py" 2>&1 | tail -1 ) ; then
    ok "tests finished (expect: OK (skipped=1))"
else
    echo "WARNING: test suite reported failures — inspect before snapshotting"
fi

# ---------------------------------------------------------------------------
# Done — next steps
# ---------------------------------------------------------------------------
log "Bootstrap complete"
cat <<EOF

Next steps (docs/VM_CHECKLIST.md):

  1. Gate 9 reboot test:
       sudo reboot
     ...then after boot:
       systemctl status undermind-proxy.service undermind-demo.service
       curl -s http://localhost:8080/ | head -5
       cat /tmp/undermind-report.json | python3 -m json.tool

  2. Run gates 1-8 by hand if you want the full walkthrough (section 6),
     or trust the autorun report above.

  3. Before taking the ISO snapshot:
       sudo bash ${DEST}/scripts/snapshot_cleanup.sh
     then take the VirtualBox snapshot and build the ISO.

MISSION.md travels with the image: $([ -f "${DEST}/MISSION.md" ] && echo present || echo MISSING)
EOF
