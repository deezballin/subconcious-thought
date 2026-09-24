# Undermind — Local Subconscious Predictor

> Built by Dewayne — read [MISSION.md](MISSION.md) for who built this and why.

A universal local pipeline that watches what you type, predicts your sentence
continuation with a fast local draft model (Krios, 9B Qwen on **Lemonade
Server** for NPU acceleration), and the instant token confidence crosses
**95%**, bypasses the local loop and executes the entire text branch on your
configured primary LLM pipeline. When typing goes fully idle, a background
**daydreaming loop** mines your history for repeating intents and appends
training-ready data to `data/training_export.jsonl`.

## Architecture

```
keystrokes → Listener (thread-safe buffer, idle clock)
   └─▶ Pipeline tick (20 ms)
         ├─ debounce → DraftPredictor streams a Krios continuation
         │    (Lemonade POST /v1/chat/completions, stream=true, logprobs=true)
         │    └─ ConfidenceTracker: exp(logprob) per token, ns timestamps
         │          └─ crossing ≥ 0.95 (edge-triggered, once per stream)
         │                └─ UniversalHandoff(text branch) → primary provider
         │                      └─ result → console + SQLite `handoffs`
         └─ idle ≥ idle_threshold_s → DaydreamWorker (daemon thread)
              ├─ flush current buffer → `inputs` table
              ├─ normalize → intent_id → upsert `intents` / `intent_samples`
              └─ Exporter → append data/training_export.jsonl
```

## Setup

```bash
cd C:\Users\dewayne\Downloads\undermind
uv sync
copy config.example.toml config.toml   # then edit [draft].model
```

Find your Krios model id as Lemonade reports it:

```bash
curl http://localhost:13305/v1/models
```

Set `[draft].model` in `config.toml` to that exact id. On startup Undermind
validates the model and prints available ids if it is missing.

## Run

```bash
uv run undermind                        # full keystroke pipeline
uv run undermind --config other.toml    # custom config
uv run undermind --daydream-once        # one daydream cycle, then exit
uv run undermind --status               # pipeline + DB status
uv run python -m undermind.proxy        # Ollama-compatible proxy on :11435
```

Point any Ollama-speaking wrapper at `http://127.0.0.1:11435` instead of
`:11434`; the proxy predicts, hands off at the confidence crossing, executes
on your primary pipeline, and caches the result.

## Universal primary pipelines

The primary LLM that receives handed-off branches is pluggable via
`[primary].kind` in `config.toml`:

| kind            | What it does                                                        |
|-----------------|---------------------------------------------------------------------|
| `openai_compat` | Any OpenAI-compatible server (Lemonade, Ollama OpenAI, LM Studio, vLLM, hosted APIs) |
| `ollama`        | Ollama-native `/api/generate`                                        |
| `webhook`       | POST `{branch, context}` JSON to any HTTP endpoint you control       |
| `none`          | Log the branch without executing (testing)                           |

## Daydreaming loop & training export

When no keystrokes and no in-flight prediction/handoff have occurred for
`[daydream].idle_threshold_s` (default 5 s), the worker:

1. flushes the live buffer into the `inputs` table,
2. folds unprocessed inputs into `intents` — different phrasings sharing the
   same direction collapse to one normalized intent ID (lowercase → strip
   punctuation → drop stopwords → light stemming → sorted signature →
   SHA-256[:16]),
3. appends any intent whose count grew to `data/training_export.jsonl`
   (deduped via the `exports` ledger).

Each JSONL line:

```json
{"intent_id": "a1b2c3d4e5f60718", "signature": "bug fix login", "count": 3,
 "first_seen": "2026-09-23T10:00:00Z", "last_seen": "2026-09-23T10:12:00Z",
 "export_run_ms": 1789000000000,
 "samples": [{"input_id": 1, "ts_ms": 1789000000000, "text": "fix the login bug"}]}
```

## SQLite schema (`data/undermind.db`)

| Table            | Contents                                                       |
|------------------|----------------------------------------------------------------|
| `inputs`         | every flushed typed input (+ normalized signature, intent_id)   |
| `intents`        | one row per normalized intent: signature, count, export state   |
| `intent_samples` | links inputs to intents as example occurrences                  |
| `handoffs`       | every confidence-crossing execution: branch, confidence, latency|
| `exports`        | JSONL export ledger                                             |

## Configuration

See `config.example.toml` — every key is documented there. Key values:

- `[confidence].threshold = 0.95` — the handoff trigger
- `[confidence].mode = "latest"` or `"ewma"` — per-token vs smoothed confidence
- `[draft].debounce_s = 0.35` — typing pause before a prediction stream starts
- `[daydream].idle_threshold_s = 5.0` — complete idle time before daydreaming
- `[daydream].min_intent_count = 2` — occurrences required before export

## Testing

```bash
uv run python -m unittest discover -s src/undermind -p "test_*.py" -v
```

The suite is fully offline: provider integration tests skip automatically
when no model backend is reachable, and proxy integration tests use
in-process fakes.

## Project layout

| File / folder          | Purpose                                                    |
|------------------------|------------------------------------------------------------|
| `PLAN.md`              | Design document — the pipeline described end to end        |
| `config.example.toml`  | Documented default configuration                           |
| `src/undermind/config.py`     | TOML-backed typed configuration                     |
| `src/undermind/providers/`    | Pluggable backends (OpenAI-compat / Ollama / webhook)|
| `src/undermind/confidence.py` | Token-confidence tracking + 95% crossing            |
| `src/undermind/intents.py`    | Lexical normalization → intent IDs                  |
| `src/undermind/store.py`     | SQLite persistence layer                            |
| `src/undermind/exporter.py`   | JSONL training export with dedupe                   |
| `src/undermind/daydream.py`   | Idle-triggered background worker                    |
| `src/undermind/listener.py`   | Keystroke capture, thread-safe buffer               |
| `src/undermind/predictor.py`  | Streamed draft prediction (DraftPredictor)          |
| `src/undermind/handoff.py`    | UniversalHandoff to the primary pipeline            |
| `src/undermind/main.py`       | Pipeline orchestrator + CLI                         |
| `src/undermind/proxy.py`      | Ollama-compatible HTTP proxy                        |

Note: keystroke capture (`listener.py`) requires OS accessibility permissions
on macOS and works out of the box on Windows/Linux.
