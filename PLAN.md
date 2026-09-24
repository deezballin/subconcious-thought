# Undermind — Universal Pipeline: Inline Prediction & Fast Handoff + Background Daydreaming

> Design document. The implementation follows this file. Tweak values here, then keep
> code and doc in sync. Runtime defaults live in `config.example.toml` (copy to
> `config.toml` to override anything).

## 0. Status (as of 2026-09-24)

**Implementation complete — 101 offline tests green. Initial git commit made
2026-09-24 (root commit, everything above included).**
(`uv run python -m unittest discover -s src/undermind -p "test_*.py"` →
`OK (skipped=1)`; the skip is the live-backend integration test).

Done:
- Full pipeline in `src/undermind/` per the design below (config, confidence,
  intents, store, exporter, daydream, listener, predictor, handoff, main, proxy,
  pluggable providers).
- Demo tooling in `scripts/`: `sandbox_autorun.sh`, `status_page.py`,
  `seed_daydream.py`, two systemd units, README with env vars.
- `vm_bootstrap.sh` — one-paste fresh-VM setup (checklist sections 1–7, idempotent).
- `snapshot_cleanup.sh` — pre-snapshot wipe + PRISTINE verification (section 9).
- `docs/VM_CHECKLIST.md` — master runbook: specs, demo `config.toml`, gates 1–9,
  snapshot checklist. Brain app fixed in place (`brain/`, backup
  `main.js.bak.20260923_kairos`); `brain/auto_nodes.json` rebuilt (9 entries).
- **Aristocrat added to the Hall of Names in MISSION.md** (2026-09-24), matching
  register nodes 200–204; the doc and register now agree.
- **Brain 3D flight** (2026-09-24): nodes orbit on individual tilted planes with
  perspective projection (near = big/bright, far = small/dim, depth-sorted paint,
  depth-faded links). Toggle via ✈ header button or `F`; preference persisted in
  `brain.flight`; honors `prefers-reduced-motion`; dragging a node re-syncs its
  orbit; classic 2D drift preserved behind the toggle. Verified live in a browser
  (pixel-motion check) with `node --check` green.

In flight:
- Ubuntu 26.04.1 mini (netboot) VM **BUNTU** running in **VirtualBox** 7.2.20
  (Hyper-V dropped — flaky netboot). Host has user-mode `VBoxManage`, so the VM
  can be snapshotted/inspected without elevation. Next: get guest IP
  (`hostname -I`), install openssh-server if missing, then
  `sudo bash <repo>/scripts/vm_bootstrap.sh <repo>` → Gate 9 reboot test →
  `snapshot_cleanup.sh` → `VBoxManage snapshot BUNTU take pre-iso` → ISO build.

Backlog ideas (not built):
- ISO build script / container-rootfs fallback; Brain↔Undermind bridge
  (auto memory nodes on handoff/export); Hall of Names view.

## 1. Overview

Undermind is a universal, config-driven pipeline with two core features:

1. **Inline Sentence Prediction & Fast Handoff** — while you type, Krios (the local
   9B Qwen, served by **Lemonade Server** for NPU acceleration at
   `http://localhost:13305`) streams a sentence continuation for the active
   character buffer. Every streamed token carries a logprob; per-token confidence
   (`exp(logprob)`) is tracked in real time with millisecond timestamps. The instant
   confidence crosses **95%**, the local loop is cut and the **entire text branch**
   (current buffer + predicted continuation) is handed to the configured primary
   LLM provider for immediate execution.
2. **Background Daydreaming Loop** — an asynchronous worker that triggers only when
   the typing pipeline goes completely idle. It flushes the live buffer, reads
   historical inputs from SQLite, groups different phrasings that share the same
   direction into a single normalized intent ID, logs them to the local database,
   and appends training-ready snapshots to `data/training_export.jsonl` for offline
   training or local weight adjustments.

No new third-party dependencies: `requests`, `pynput`, and the standard library
(`sqlite3`, `tomllib`, `threading`, `hashlib`, `json`).

## 2. Architecture

```
keystrokes → Listener (thread-safe buffer, idle clock)
   └─▶ Pipeline tick (20 ms, main.py)
         ├─ debounce → DraftPredictor streams a Krios continuation
         │    (Lemonade POST /v1/chat/completions, stream=true, logprobs=true)
         │    └─ ConfidenceTracker: exp(logprob) per token, ns timestamps
         │          └─ crossing ≥ 0.95 (edge-triggered, min-token guard)
         │                └─ UniversalHandoff(text branch) → primary provider
         │                      └─ result → console + SQLite `handoffs` table
         │                            (+ proxy pre-generation cache in HTTP mode)
         └─ idle ≥ idle_threshold_s → DaydreamWorker (daemon thread)
              ├─ flush current buffer → `inputs` table
              ├─ process unprocessed inputs → normalize → upsert `intents`
              └─ Exporter → append data/training_export.jsonl (+ `exports` ledger)
```

The same `DraftClient`/`PrimaryProvider` interfaces power three frontends:

- `uv run undermind` — keystroke pipeline (listener → predictor → handoff → daydream)
- `uv run python -m undermind.proxy` — Ollama-compatible HTTP proxy on :11435
- `python -m undermind.main --daydream-once` — one offline daydream cycle

## 3. Module map

| Module | Purpose |
|---|---|
| `config.py` | Dataclass config, loaded from `config.toml` over built-in defaults. |
| `providers/` | Pluggable backends: `base.py` (protocols), `openai_compat.py` (Lemonade + any OpenAI-compatible server), `ollama_native.py`, `webhook.py`, `__init__.py` (registry/factory). |
| `confidence.py` | `ConfidenceTracker` — per-token confidence, 95% edge-triggered crossing. |
| `intents.py` | Lexical normalization → deterministic intent IDs. |
| `store.py` | SQLite layer, WAL mode, thread-safe, idempotent `init_db()`. |
| `exporter.py` | `data/training_export.jsonl` writer with dedupe ledger. |
| `daydream.py` | Idle-triggered background worker. |
| `listener.py` | Keystroke capture, thread-safe buffer, idle clock. |
| `predictor.py` | `DraftPredictor` — streamed continuations + confidence crossing callback. Legacy `Predictor`/`Hypothesis` kept for the proxy cache. |
| `handoff.py` | `UniversalHandoff` — routes the branch to the primary provider, records it. |
| `main.py` | `Pipeline` orchestrator + CLI (`--config`, `--daydream-once`, `--status`). |
| `proxy.py` | HTTP proxy with pre-generation cache (Ollama-compatible endpoints). |

## 4. Key behaviors

- **Confidence**: `exp(logprob)` per streamed token. Trigger mode `latest` (default)
  uses the newest token's confidence; `ewma` smooths across tokens. Crossing is
  edge-triggered: exactly one handoff per prediction stream. Guards: `min_tokens`,
  `min_chars` prevent firing on a trivial first token. Timestamps use
  `time.perf_counter_ns()` (monotonic, latency math) and `time.time_ns()` (wall
  clock, DB records).
- **Stream lifecycle**: one in-flight streamed continuation per buffer state. A new
  keystroke cancels the stream (stop flag + response close) and restarts after the
  debounce window. Stream start, token arrival, crossing, and handoff are all
  timestamped at nanosecond resolution.
- **Handoff payload**: the full text branch = current buffer + predicted
  continuation. `openai_compat` and `ollama` providers send it as chat messages
  (`system` + `user`); `webhook` providers POST the raw JSON envelope; `none`
  providers log only.
- **Idle definition**: no keystrokes **and** no in-flight prediction or handoff for
  `idle_threshold_s`. On idle, an unflushed buffer is committed to `inputs` first,
  then the daydream cycle runs. Any activity re-arms the watchdog.
- **Intent normalization** (lexical, deterministic, offline): lowercase → strip
  punctuation → drop stopwords → light suffix stemming (`-ing`, `-ed`, `-s`, `-es`,
  `-ly` with a minimum-length guard) → sort + dedupe content words → joined
  signature → `sha256[:16]` hex intent ID. Different wordings of the same direction
  collapse to one ID; the original text is preserved in `inputs`.
- **DB schema** (SQLite, WAL):
  - `inputs(id INTEGER PK, ts_ms INTEGER, text TEXT, normalized TEXT, intent_id TEXT, processed INTEGER DEFAULT 0)`
  - `intents(intent_id TEXT PK, signature TEXT, first_seen_ms INTEGER, last_seen_ms INTEGER, count INTEGER, exported_at_ms INTEGER, export_count INTEGER)`
  - `intent_samples(id INTEGER PK, intent_id TEXT, input_id INTEGER, ts_ms INTEGER)`
  - `handoffs(id INTEGER PK, ts_ns INTEGER, trigger TEXT, confidence REAL, branch TEXT, provider TEXT, model TEXT, status TEXT, latency_ms REAL, error TEXT)`
  - `exports(id INTEGER PK, ts_ms INTEGER, path TEXT, records INTEGER, intent_ids TEXT)`

## 5. Configuration

`config.toml` (optional) overrides defaults; sections mirror `config.example.toml`:
`[draft]` base_url/model/debounce, `[confidence]` threshold/mode/guards,
`[primary]` provider type/base_url/model/timeout, `[daydream]` idle threshold and
export path, `[store]` db path, `[proxy]` host/port. The draft model must be the
exact model id reported by Lemonade `GET /v1/models`; startup validation prints
available ids when the configured one is missing.

## 6. Verification

1. `uv run python -m unittest discover -s src/undermind -p "test_*.py" -v` — all
   offline tests green without any backend running.
2. Live smoke (Lemonade running): `uv run undermind` → type a sentence; observe the
   streamed suggestion, the 95% handoff log line, and rows in `data/undermind.db`;
   go idle → daydream cycle log + appended lines in `data/training_export.jsonl`.
3. `uv run python -m undermind.proxy` serves `/api/generate`, `/api/chat`,
   `/api/tags` with crossing-triggered pre-generation caching.
