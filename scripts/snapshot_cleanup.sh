#!/usr/bin/env bash
#
# snapshot_cleanup.sh — make the VM pristine right before the ISO snapshot
#
# Usage on the VM:
#
#   sudo bash /opt/undermind/scripts/snapshot_cleanup.sh
#
# Implements the "Do not" list from docs/VM_CHECKLIST.md section 9:
#   - stops the undermind units (they stay ENABLED for the image)
#   - wipes data/ so first boot in the sandbox is pristine
#   - removes __pycache__ and undermind logs / tmp artifacts
#   - apt clean + journal vacuum to shrink the image
#
# Keeps: .venv (services need it), config.toml, MISSION.md, the repo itself.
# Idempotent: safe to re-run. Exits non-zero if anything forbidden survives.

set -euo pipefail

DEST="${UNDERMIND_HOME:-/opt/undermind}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()  { printf '  \033[1;32mok\033[0m  %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo bash $0"; exit 1; }

# ---------------------------------------------------------------------------
# Stop services (enabled units will restart on next boot — that is Gate 9)
# ---------------------------------------------------------------------------
log "Stopping undermind services"
systemctl stop undermind-demo.service 2>/dev/null || true
systemctl stop undermind-proxy.service 2>/dev/null || true
ok "units stopped (still enabled for the image)"

# ---------------------------------------------------------------------------
# Demo state
# ---------------------------------------------------------------------------
log "Wiping demo state"
rm -rf "${DEST}/data"
if [ -d "${DEST}/data" ]; then echo "ERROR: could not remove ${DEST}/data"; exit 1; fi
ok "data/ removed"

# ---------------------------------------------------------------------------
# Caches, logs, tmp artifacts
# ---------------------------------------------------------------------------
log "Removing caches, logs and tmp artifacts"
find "${DEST}" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
rm -f /var/log/undermind-proxy.log /var/log/undermind-demo.log
rm -f /tmp/undermind-*.json /tmp/undermind-*.log /tmp/undermind-report.json
journalctl --vacuum-time=1d >/dev/null 2>&1 || true
apt-get clean >/dev/null 2>&1 || true
ok "caches and logs cleared"

# --status must not be allowed to recreate data/ as a side effect; if it did,
# wipe again so the image truly ships without data/.
( cd "${DEST}" && sudo -u undermind "${DEST}/.venv/bin/undermind" --status >/dev/null 2>&1 ) || true
rm -rf "${DEST}/data"

# ---------------------------------------------------------------------------
# Verify the section 9 "do not" list
# ---------------------------------------------------------------------------
log "Verifying pristine image"
FAIL=0

check_absent() {
    # check_absent <glob>
    local matches
    matches=$(compgen -G "$1" 2>/dev/null || true)
    if [ -n "${matches}" ]; then
        echo "  FAIL  still present: $1"
        FAIL=1
    fi
}

check_absent "${DEST}/data"
check_absent "${DEST}/**/__pycache__"
check_absent "/var/log/undermind-*.log"
check_absent "/tmp/undermind-*"

[ -f "${DEST}/config.toml" ] && ok "config.toml present"      || { echo "  FAIL  config.toml missing"; FAIL=1; }
[ -f "${DEST}/MISSION.md"  ] && ok "MISSION.md present"       || { echo "  FAIL  MISSION.md missing"; FAIL=1; }
[ -x "${DEST}/.venv/bin/undermind" ] && ok ".venv intact"     || { echo "  FAIL  .venv broken"; FAIL=1; }
systemctl is-enabled undermind-proxy.service >/dev/null 2>&1 \
    && systemctl is-enabled undermind-demo.service >/dev/null 2>&1 \
    && ok "both units still enabled"                           || { echo "  FAIL  units not enabled"; FAIL=1; }

if [ "${FAIL}" -eq 0 ]; then
    log "PRISTINE — take the VirtualBox snapshot now (VBoxManage snapshot <vm> take pre-iso)"
    echo "
Reminder (section 9): after checkpointing, confirm the ISO stays under the
1.5 GB target and that UNDERMIND_REPORT_URL is set to a throwaway endpoint —
never an API key."
else
    log "NOT pristine — fix the FAIL lines above before snapshotting"
    exit 1
fi
