#!/usr/bin/env bash
#
# sandbox_autorun.sh — Undermind live-ISO boot demo
#
# Runs at boot (undermind-demo.service). Sequence:
#   1. Wait for Ollama, start undermind.proxy, wait for health
#   2. Start the live status page on :8080
#   3. Demo 1: proxy confidence crossing   -> phone home
#   4. Demo 2: daydream intents + JSONL export -> phone home
#   5. Demo 3 (optional, UNDERMIND_DEMO_KEYBOARD=1): live keystrokes via xdotool
#   6. Post the final report and keep the status page serving
#
# Every step's result is timestamped into a JSON report and POSTed to
# UNDERMIND_REPORT_URL when set. Console output mirrors everything.

set -u

# ---------------------------------------------------------------------------
# Configuration (overridable via environment)
# ---------------------------------------------------------------------------

UNDERMIND_HOME="${UNDERMIND_HOME:-/opt/undermind}"
VENV_BIN="${UNDERMIND_HOME}/.venv/bin"
UNDERMIND="${VENV_BIN}/undermind"
PROXY_HOST_PORT="${UNDERMIND_PROXY_HOST:-127.0.0.1:11435}"
STATUS_PORT="${UNDERMIND_STATUS_PORT:-8080}"
REPORT_URL="${UNDERMIND_REPORT_URL:-}"
DEMO_MODEL="${UNDERMIND_DEMO_MODEL:-qwen2.5:0.5b}"
DEMO_KEYBOARD="${UNDERMIND_DEMO_KEYBOARD:-0}"
LOG_FILE="${UNDERMIND_LOG_FILE:-/var/log/undermind-demo.log}"

PROXY_URL="http://${PROXY_HOST_PORT}"
BOOT_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START_MONO="$(date +%s%N 2>/dev/null || python3 -c 'import time; print(time.monotonic_ns())')"

mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null || LOG_FILE="/tmp/undermind-demo.log"
exec >> "${LOG_FILE}" 2>&1

# ---------------------------------------------------------------------------
# Report state
# ---------------------------------------------------------------------------

declare -a STEPS=()

step_result() {
    # step_result <name> <status> <detail>
    local name="$1" status="$2" detail="$3"
    local ts elapsed
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    local now
    now="$(date +%s%N 2>/dev/null || python3 -c 'import time; print(time.monotonic_ns())')"
    elapsed=$(( (now - START_MONO) / 1000000000 ))
    STEPS+=("{\"step\": \"${name}\", \"status\": \"${status}\", \"detail\": \"${detail}\", \"ts\": \"${ts}\", \"elapsed_s\": ${elapsed}}")
    echo "[${ts}] +${elapsed}s ${status^^} ${name}: ${detail}"
}

report() {
    # Build and POST the report; also leave a copy on disk.
    local phase="$1"
    local report_json steps joined
    joined="$(printf '%s,' "${STEPS[@]:-}")"
    joined="${joined%,}"
    report_json="{\"phase\": \"${phase}\", \"boot_ts\": \"${BOOT_TS}\", \"host\": \"$(hostname 2>/dev/null || echo sandbox)\", \"steps\": [${joined}]}"
    echo "${report_json}" > /tmp/undermind-report.json
    if [ -n "${REPORT_URL}" ]; then
        curl -sS -m 10 -X POST -H "Content-Type: application/json" \
            -d "${report_json}" "${REPORT_URL}" >/dev/null 2>&1 \
            && echo "[report] posted to ${REPORT_URL}" \
            || echo "[report] FAILED to reach ${REPORT_URL} (saved to /tmp/undermind-report.json)"
    else
        echo "[report] UNDERMIND_REPORT_URL not set; saved to /tmp/undermind-report.json"
    fi
}

wait_for_url() {
    # wait_for_url <url> <label> <max_seconds>
    local url="$1" label="$2" max="$3" i=0
    while [ "${i}" -lt "$(( max * 2 ))" ]; do
        if curl -sS -m 2 -o /dev/null "${url}" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
        i=$(( i + 1 ))
    done
    echo "TIMEOUT waiting for ${label} at ${url}" >&2
    return 1
}

# ---------------------------------------------------------------------------
# Step 1: backends up
# ---------------------------------------------------------------------------

echo "=== Undermind sandbox demo starting (${BOOT_TS}) ==="

if wait_for_url "http://localhost:11434/api/tags" "Ollama" 60; then
    step_result "ollama_ready" "ok" "Ollama answered /api/tags"
else
    step_result "ollama_ready" "fail" "Ollama did not answer within 60s"
    report "aborted"
    exit 1
fi

if ! curl -sS http://localhost:11434/api/tags 2>/dev/null | grep -q "${DEMO_MODEL}"; then
    step_result "ollama_model" "fail" "model ${DEMO_MODEL} missing from /api/tags"
    report "aborted"
    exit 1
fi
step_result "ollama_model" "ok" "${DEMO_MODEL} present"

# ---------------------------------------------------------------------------
# Step 2: status page (start early so the browser has something immediately)
# ---------------------------------------------------------------------------

python3 "$(dirname "$0")/status_page.py" --port "${STATUS_PORT}" \
    --log "${LOG_FILE}" --report /tmp/undermind-report.json &
STATUS_PID=$!
if wait_for_url "http://localhost:${STATUS_PORT}/" "status page" 10; then
    step_result "status_page" "ok" "serving on :${STATUS_PORT}"
else
    step_result "status_page" "fail" "did not bind :${STATUS_PORT}"
fi

# ---------------------------------------------------------------------------
# Step 3: proxy up
# ---------------------------------------------------------------------------

if [ -x "${VENV_BIN}/python" ]; then
    ( cd "${UNDERMIND_HOME}" && "${VENV_BIN}/python" -m undermind.proxy ) &
else
    ( cd "${UNDERMIND_HOME}" && python3 -m undermind.proxy ) &
fi
PROXY_PID=$!

if wait_for_url "${PROXY_URL}/api/tags" "undermind.proxy" 45; then
    step_result "proxy_ready" "ok" "answering at ${PROXY_URL}"
else
    step_result "proxy_ready" "fail" "no response from ${PROXY_URL}/api/tags"
    report "aborted"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 4: Demo 1 — confidence crossing through the proxy
# ---------------------------------------------------------------------------

CROSS_BODY="$(curl -sS -m 120 "${PROXY_URL}/api/generate" \
    -d "{\"model\":\"${DEMO_MODEL}\",\"prompt\":\"The capital of France is\",\"stream\":false}" 2>/dev/null || true)"

if echo "${CROSS_BODY}" | grep -q '"confidence_crossed": true'; then
    CROSS_CONF="$(echo "${CROSS_BODY}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("undermind",{}).get("confidence"))' 2>/dev/null || echo unknown)"
    step_result "demo1_crossing" "ok" "crossed at confidence ${CROSS_CONF}; response: $(echo "${CROSS_BODY}" | head -c 120)"
else
    step_result "demo1_crossing" "fail" "no confidence_crossed=true in: $(echo "${CROSS_BODY}" | head -c 200)"
fi

FALLBACK_BODY="$(curl -sS -m 120 "${PROXY_URL}/api/generate" \
    -d "{\"model\":\"${DEMO_MODEL}\",\"prompt\":\"purple elephant discoroutine\",\"stream\":false}" 2>/dev/null || true)"
if echo "${FALLBACK_BODY}" | grep -q '"confidence_crossed": false'; then
    step_result "demo1_fallback" "ok" "raw-prompt path executed"
else
    step_result "demo1_fallback" "warn" "fallback check inconclusive: $(echo "${FALLBACK_BODY}" | head -c 120)"
fi

# ---------------------------------------------------------------------------
# Step 5: Demo 2 — daydream intents + training export
# ---------------------------------------------------------------------------

SEED_SQL="$(dirname "$0")/seed_daydream.py"
if "${VENV_BIN}/python" "${SEED_SQL}" >/dev/null 2>&1 || python3 "${SEED_SQL}" >/dev/null 2>&1; then
    step_result "demo2_seed" "ok" "three paraphrased inputs recorded"
else
    step_result "demo2_seed" "fail" "seed script failed"
fi

DAYDREAM_OUT="$( cd "${UNDERMIND_HOME}" && "${UNDERMIND}" --daydream-once 2>&1 || true )"
if echo "${DAYDREAM_OUT}" | grep -q "intents=1"; then
    step_result "demo2_daydream" "ok" "$(echo "${DAYDREAM_OUT}" | tail -1)"
else
    step_result "demo2_daydream" "fail" "$(echo "${DAYDREAM_OUT}" | tail -1)"
fi

EXPORT_LINE="$(tail -1 "${UNDERMIND_HOME}/data/training_export.jsonl" 2>/dev/null || true)"
if echo "${EXPORT_LINE}" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); sys.exit(0 if d.get("count")==3 else 1)' 2>/dev/null; then
    step_result "demo2_export" "ok" "JSONL line valid with count=3"
else
    step_result "demo2_export" "fail" "export line missing or wrong: ${EXPORT_LINE:0:120}"
fi

# ---------------------------------------------------------------------------
# Step 6: optional keyboard demo
# ---------------------------------------------------------------------------

if [ "${DEMO_KEYBOARD}" = "1" ]; then
    (
        cd "${UNDERMIND_HOME}"
        export DISPLAY="${DISPLAY:-:99}"
        "${UNDERMIND}" >> "${LOG_FILE}" 2>&1 &
        UM_PID=$!
        sleep 6
        xdotool type --delay 80 "The capital of France is"
        sleep 10
        kill "${UM_PID}" 2>/dev/null
    )
    LAST_HANDOFF="$(sqlite3 "${UNDERMIND_HOME}/data/undermind.db" \
        "SELECT status || ':' || round(coalesce(confidence,0),3) FROM handoffs ORDER BY id DESC LIMIT 1;" 2>/dev/null || true)"
    if echo "${LAST_HANDOFF}" | grep -q "^ok:"; then
        step_result "demo3_keyboard" "ok" "handoff recorded: ${LAST_HANDOFF}"
    else
        step_result "demo3_keyboard" "fail" "no ok handoff found (got: ${LAST_HANDOFF:-none})"
    fi
else
    step_result "demo3_keyboard" "skip" "UNDERMIND_DEMO_KEYBOARD!=1"
fi

# ---------------------------------------------------------------------------
# Step 7: offline test suite
# ---------------------------------------------------------------------------

if ( cd "${UNDERMIND_HOME}" && "${VENV_BIN:-}" python -m unittest discover -s src/undermind -p "test_*.py" >/tmp/undermind-unittest.log 2>&1 ); then
    step_result "test_suite" "ok" "$(tail -1 /tmp/undermind-unittest.log)"
else
    step_result "test_suite" "fail" "$(tail -1 /tmp/undermind-unittest.log)"
fi

# ---------------------------------------------------------------------------
# Final report; keep status page alive
# ---------------------------------------------------------------------------

report "complete"

# Hand the foreground to the status page so the unit stays "running".
wait "${STATUS_PID}" 2>/dev/null || while true; do sleep 60; done
