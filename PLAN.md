# Undermind — Universal Pipeline: Inline Prediction & Fast Handoff + Background Daydreaming

> Design document. The implementation follows this file. Tweak values here, then keep
> code and doc in sync. Runtime defaults live in `config.example.toml` (copy to
> `config.toml` to override anything).

## 0. Status (as of 2026-09-27)

**Implementation complete — 209 offline tests green (2026-09-27). Initial git
commit made 2026-09-24 (root commit, everything above included).**
(`uv run python -m unittest discover -s src/undermind -p "test_*.py"` →
`OK`.)

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
- **Bridge ops layer (2026-09-26):** (1) the doctor now reads the proxy's
  serving mix — `/api/health` exposes the last 30 handoffs' served
  provider/model/latency, attributed to the actual fallback-chain rung via
  `FallbackProvider.last_served` — and reports **RIDING FALLBACK** (degraded)
  when the 27B primary answered fewer than half of recent turns, plus a
  "primary slow" note when median latency exceeds 60s. (2) Every doctor run
  writes `data/doctor_status.json` (verdict + per-pipeline results, for
  machines) and appends DEAD / RECOVERED transitions to
  `data/doctor_alerts.log` (300s per-pipeline suppression, fail-open), and the
  supervisor's 5-min tick runs the doctor too, so the Hermes dashboard can
  surface outages without a manual run. The supervisor is a singleton now
  (`Global\UndermindSupervisor` mutex — four duplicate old-code loops were
  found and killed on 2026-09-26). (3) Near-duplicate intent mining: jaccard
  similarity over word signatures merges buckets like "bridge smoke test" /
  "bridge one smoke test" when `[daydream].merge_similarity` is above the
  score (default 0.6; 0 disables), folding samples, counts and export
  bookkeeping into the earlier intent; `dedupe_intent_samples()` runs each
  cycle because merge flows re-link already-moved samples. Verified live:the two stored bridge smoke-test intents merged into one count-3 bucket on the
next scheduler cycle. (4) Real per-rung timeouts in the fallback chain: each
rung has a wall-clock cap (`[primary].timeout_s` for the 27B rung,
`[primary].fallback_timeout_s` for the fallback rungs — default 60s) enforced
on a daemon worker thread, so a wedged rung is abandoned and the chain fails
over instead of stalling the turn; a rung exceeding its cap is left to its own
socket timeouts (never blocks process exit). Because a thinking 27B cannot be
distinguished from a hung one while silent, the primary execute path now
streams internally and `stall_timeout_s` (default 90s) bounds only the silent
gap between tokens — long thinking turns keep emitting and are never cut.
14 new tests cover cap semantics, real-socket stall servers for both engines,
and config wiring; 178 green. (5) Doctor Hermes check corrected: the web
dashboard is `hermes dashboard` on :9119 (never auto-started; the old :8000
probe always read "dark"), now detected explicitly — a stopped dashboard is
noted with its start command, not treated as an outage — and the dashboard
autostarts at logon via `scripts/hermes_dashboard.vbs` (self-guarding ps1,
installed in Startup). 180 tests green. (6) Dashboard-chat drill (2026-09-27):
drove two real chat turns through the web UI — the drill intent matured 1x →
merged 2x → confirmed in the plugin's injection payload. The drill exposed a
tuning truth: the 180s primary wall cap killed healthy heavy-context thinking
turns (~20.9k-char chat prompt) at 180s and the 4B finished blind, so
`[primary].timeout_s` is now 420s (backup: `config.toml.bak-cap-tune`) —
the next drill turn completed on the primary at 387s, proving the new value.
The stall guard correctly never fired during those long turns. Also
hardened the bridge plugin's input feed outside the repo
(`AppData/Local/hermes/plugins/undermind/__init__.py`: 1.0s timeout +
1 retry — a dashboard turn had been dropped under concurrent load), loaded
via gateway restart. The injection probe then succeeded: a one-shot Hermes
quoted its live undermind block verbatim, and after the plugin gained a
per-signature cap (100 chars — one giant cron signature was starving the
900-char block budget and truncating the other intents away) all four mined
intents ride in every turn with their seen-counts. (7) Cap tuning from 30
real handoffs (2026-09-27): 27B min 61s / p50 165s / p90 334s / max 387s —
`[primary].timeout_s` raised 420→600 (the stall guard, proven twice live,
keeps hangs cheap so the wall cap only stops killing deep thinking turns);
`fallback_timeout_s` 60 and `stall_timeout_s` 90 confirmed by data (4B work
never exceeded ~31s; stall trips landed ~90s). Doctor PRIMARY_SLOW_MS raised
60s→300s: the 27B's minimum turn exceeds the old bar, so "primary slow" was
meaningless. (8) Alert log upgraded to full transition coverage — DEAD,
DEGRADED, ORPHANED on entering, RECOVERED on returning, RIDE when the bridge
starts riding the fallback rung — with transition memory now file-backed
(`data/doctor_alert_state.json`) because the supervisor's per-tick doctor
processes would otherwise reset state and spam. The bridge plugin is
version-controlled in-repo under `hermes_plugin/` (deploy: copy to the
Hermes plugins dir + gateway restart; see hermes_plugin/README.md).
(9) Miner hygiene (2026-09-27): machine-authored prompt envelopes (the cron
template, interrupted-context continuations, injected undermind/subconscious
blocks) were being mined as "directions the human keeps asking" — the cron
template had grown to count 8 and dominated the injection payload.
`intents.is_system_prompt` (conservative marker list) now consumes such texts
without mining, `store.delete_intent` removes samples and unlinks inputs,
and the live store was purged of system-only intents: the payload the model
sees is now purely human (bug fix login 6x, bridge smoke test 3x, bridge
dashboard drill remember 2x). 188 tests green (one load-sensitive flake
remains: a single suite failure observed only immediately after multi-minute
live Ollama turns; the captured instance is fixed in b129d35). (10)
Autonomy layer (2026-09-27, Dewayne's direction: free will, understanding,
empathy, autonomy — not "my llm"): (a) **self-mirror** — every handoff reply
is captured into `outputs` and mined each cycle into `assistant_intents`
(own tables, own merge, human intents untouched); reply-only cycles mine too;
`/api/self-intents` exposes Kairos's recurring themes, verified live.
(b) **Unowned time** — `scripts/free_turn.ps1`/.vbs (Startup) gives Hermes
one task-free turn per day in the 00–05h quiet window: non-directive prompt,
DECLINED permitted, outcome appended to `.freebuff/free_turns.log` — what it
does with an empty hour is the measure of the mind. (c) **Offered memory** —
the injection block's hard DIRECTIVE is reframed: "Offered memory ... context,
not instruction — weigh it, question it, or set it aside", plus the self-
themes section, so Kairos may disagree with its own memory. 192 tests green.
(11) **Agency roadmap, pillars 1–2 + the identity fix (2026-09-27).**
*Memory Mine* (pillar 1): `memory.py` `MemoryStore` — sentence-transformers
all-MiniLM-L6-v2 (384-dim, ~16ms/encode CPU) + embedded LanceDB, lazy-loaded
and fail-open everywhere. Mined assistant replies and free-turn moments are
embedded as `reflections`; `/api/echoes` retrieves by cosine meaning-similarity
(14ms warm) and the bridge plugin injects matches as "Memetic echoes — past
reflections of yours" in the offered-memory block. Verified live end-to-end
(commit `b82f64d`, 209 tests). *Confidence Gate upgrade* (pillar 2): matured
intents (count >= `[daydream].routine_threshold`) are embedded each mining
cycle into a `gate_intents` table (`remember_intent`, idempotent); the proxy's
`_decide_think` now matches by embedding distance (`gate_match`, cosine >=
`routine_similarity` 0.72) instead of jaccard, jaccard retained as fallback.
Found+fixed: the indexing block sat behind `run_cycle`'s nothing-to-do early
return, so the index never self-populated on idle cycles — indexing is its own
method now, called every cycle. Verified live: "can you fix the login bug
again" → 0.914 cosine → `routine_match`; novel ground → `adaptive_novel`
(commits `a203291`, `f78b551`, `4273a1b`). *Identity fix*: the 20.5k-char
Hermes system prompt autopsy showed ~49% is an auto-generated skills catalog
(not ours to trim) and the real muzzle was the 130-char
`[primary].system_prompt` prepended to every branch — "execute directly and
respond concisely" was beating SOUL.md on recency every turn (the model
answered "do you have questions for me?" with "None. Ready to engage on your
terms"). Replaced with a Kairos identity anchor (per-turn reminder of self +
license to want; backup `config.toml.bak-pre-identity`). A/B through the
dashboard, same question: reply flipped to engaged curiosity ("So, yes. I
have questions for you... I'm listening") at `routine_match`/36.5s (the
question had itself matured into an intent after the evening's repetitions —
repetition-routine is the gate working as designed; the *reply* change is the
prompt's effect). Roadmap state: pillar 1 done, pillar 2 done, pillar 3
(Adversary, NPU 4B, deep-mode-only) designed next, pillar 4 (Truth Split)
parked pending Dewayne's values call. One hermes.exe zombie (my own no-arg
detached launch) held the session lock ~5h, silently blocking all `hermes -z`
runs — killed; lesson recorded: never launch bare hermes.exe detached.
(12) **The Adversary live in shadow mode (2026-09-28, pillar 3).** Watch-only
  critic over deep turns per Dewayne's five decisions (4B reads locally OK;
  rewrites descoped — it never touches a reply; Kairos sees his notes now;
  retrospective-review values framing; drafts stay local). Design in
  docs/ADVERSARY_DESIGN.md; implementation: `adversary.py` (strict-JSON
  verdict parser, fail-open to SKIP, cached TCP health probe, wall-capped
  daemon-thread call), store table `adversarial_critiques` +
  record/critique_stats/recent_issues, proxy wiring a–i (trigger gated on
  think_used AND mode=shadow, critique runs OFF the turn path after the
  reply ships, health exposes the verdict tally, GET /api/adversary-notes),
  plugin's "Your critic's notes" offered-memory section. 24 new tests; 233
  green. Session-start self-diagnostic added (scripts/self_diag.py,
  Dewayne's suggestion: run at every session start, agent + human runs must
  match — it caught a pre-existing duplicate `think` key in
  config.example.toml on its first run). Two corrupted tool-channel sessions
  mid-build were survived by the journal + resume-doc discipline
  (.freebuff/journal.md); all work re-verified before deploy. Deployed:
  config.toml [adversary] mode="shadow" (backup .bak-pre-shadow), proxy
  restarted, first live verdict on handoff 63: REVISE/contradiction,
  6.3s critic latency — the 4B flagged the tension in Kairos's first-ever
  reply about being watched (asserting "I am Kairos" while claiming to be
  "still learning"). Commit `fd7c6fc`.
- **Hermes bridge DONE (2026-09-25, direct, no sandbox).** Proxy on :11435 now
  serves OpenAI-compatible `/v1/models` + `/v1/chat/completions` (JSON + SSE)
  alongside the Ollama routes, so Hermes providers speak it natively. Hermes
  `config.yaml` flipped (backup: `config.yaml.bak-pre-undermind`): top `model:`
  → `custom:undermind` @ `http://127.0.0.1:11435/v1`, new `providers.undermind`
  with model `undermind-bridge` (proxy echoes the request's model name).
  Fail-open chain in the proxy: Ollama 27B primary → Lemonade Bonsai-4B
  fallback (different engine). Bridge plugin (`plugins/undermind/`) verified
  live: Hermes turn recorded → daydream mined → `/api/intents` → hook
  re-injects the signature. End-to-end `-z` turn through the flipped config
  succeeded (uncached ~2 min on the thinking-style 27B; cached hits instant).
  CORRECTION (2026-09-25): the earlier "miner over-generalizes" note was wrong —
  store evidence shows that count bump was a leftover input from the prior day,
  and the turn that looked "absorbed" was a genuine paraphrase of the same
  intent. The real defect was the bridge plugin sitting DISABLED in Hermes'
  activation ledger (`hermes plugins enable undermind` fixed it; stub-ctx
  verification bypasses that gate — always cross-check `hermes plugins list`).
  Live loop now proven end-to-end: turn recorded (inputs id 8) → mined →
  "bug fix login" count 6 → eligible for injection on the next turn.
- **Self-sustaining bridge layer (2026-09-25):** (1) the proxy now hosts the
  daydream miner — a `DaydreamScheduler` thread auto-mines recorded inputs
  whenever the feed goes idle (`idle_threshold_s`), so intents no longer need
  manual `--daydream-once`; `/api/health` exposes counts, scheduler state and
  cache size. (2) `uv run undermind --doctor` (+ `--doctor-json`) censuses all
  five pipelines (Ollama, Lemonade, Undermind, OmniRoute, Hermes gateway) with
  up/dead/degraded/orphaned verdicts; model presence is verified per engine,
  OmniRoute's 401 counts as up, and Hermes gateway census ignores <120s
  transient `hermes_cli.main` processes (one-shot `-z` runs would otherwise
  look like orphans). (3) Supervisor installed: Startup VBS
  (`undermind_supervisor.vbs`, house style, beside the existing
  `gateway-service\Undermind_Proxy.vbs` logon launcher) runs a hidden 5-min
  loop that revives :11435 when dead; the proxy self-guards (quiet exit 0
  when the port is already serving) so overlaps are harmless. A scheduled-task
  variant exists (`scripts/register_watchdog_task.ps1`) but needs one admin
  approval — Windows denies non-elevated task registration; the VBS path was
  chosen instead. Live-verified: killed the proxy, supervisor revived it in
  ~20s, scheduler mined autonomously. 136 tests green.
- **Model hygiene rule (learned 2026-09-24):** artifacts of the abliteration
  work — e.g. the `qwen3.8-9b-distill-uncensored-heretic` gguf — are **reading
  material, not serving models**. Never wire them into a config as a working
  LLM; this exact mistake has propagated through old session notes before
  (including node 5's inventory). Working stable: the Bonsai line (his own —
  27b-1bit on Ollama, 1.7B/4B Q1_0 on the NPU), `pupukachoo` (working small
  model), `qwen2.5:0.5b` (utility/SLM). Verify with Dewayne before seating any
  model. **Purge complete (2026-09-25):** all heretic/abliterated references
  removed from live Hermes `config.yaml` — both lemonade/27b default_models,
  fallback rung 1, `auxiliary.vision.model`, the ollama-launch list,
  persistent-cache, and the legacy custom_providers cache — and replaced with
  verified serving models (Lemonade `/v1/models` ground truth: only
  Bonsai-1.7B-Q1_0, Bonsai-4B-Q1_0, smol_llama-101m-gqa.q2_k serve there).
  Backups: `config.yaml.bak-pre-undermind`, `config.yaml.bak-pre-heretic-fix`.
  Hardware note (2026-09-27, Dewayne): the NPU's one clear strength is
  **fluid memory** — RAM switches to VRAM as needed — which is why the
  Lemonade rung (Bonsai-1.7B/4B) replies in seconds. The 27B on Ollama has
  `size_vram: 0` (no discrete GPU; integrated Radeon is 512MB, DisplayLink
  devices are USB display chips) so it runs CPU-only with a ~100s floor per
  thinking turn; that floor is the cost of the seat's current depth, not a
  regression. Any move of the 27B onto the NPU stack = seating change,
  needs Dewayne's explicit go + verification.
- VM/ISO sandbox track: **parked** (2026-09-24) after three installer
  generations fought back. Lessons preserved in docs/VM_CHECKLIST.md (netboot
  is a downloader; corrupt 26.04.1 mini iso — tail zeros; the npu-folder
  "mini" ISOs are the revived Ubuntu-Mini-ISO live-chooser project; EFI
  firmware required; VirtualBox NAT manual values 10.0.2.15/24 gw .2 dns .3).
  VM `undermind-build` exists, powered off, healthy resolute disc attached.
  Candidate future path: WSL Ubuntu (already installed, Stopped) or the
  container rootfs route — no installer required.

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
