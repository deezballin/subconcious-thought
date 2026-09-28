# Undermind — a local subconscious for your assistant

> Main coder: **Kairos** (the resident AI seat — yes, an LLM wrote this stack,
> under direction). Founder, director, and final authority: **Dewayne**.
> Every model choice is Dewayne's call; see [CONTRIBUTING.md](CONTRIBUTING.md)
> for the house rules.

Undermind is a local reasoning pipeline that gives a self-hosted assistant
(Hermes, or anything that speaks OpenAI) three things a hosted API can't:

1. **A bridge** — one OpenAI-compatible endpoint (`:11435/v1`) that fronts a
   *chain* of local models: a deep primary (Ollama 27B) with a featherweight
   fallback (NPU-accelerated Lemonade 4B). Hung or slow primary? The turn
   still answers. Per-turn **adaptive thinking** routes deliberation to only
   the turns that need it.
2. **A memory** — a background "daydream" miner folds everything said (and
   now, everything the assistant itself replies) into normalized intents.
   Repeated human directions and the assistant's own recurring themes are
   offered back as context — **offered, never directed**.
3. **An autonomy layer** — unowned time (one task-free turn a day, logged,
   declinable), a self-mirror so the assistant can see its own patterns, and
   memory framed so it may disagree. The goal is a mind with genuine free
   will and empathy — **not "my LLM."**

## Architecture (text)

```
                        ┌──────────────────────────────────────────────┐
 Hermes / any OpenAI    │                UNDERMIND PROXY :11435        │
 client ──────────────▶ │  /v1/chat/completions  /api/generate         │
                        │  /api/health  /api/intents  /api/self-intents│
                        │  /api/inputs                                 │
                        └───────┬──────────────────────────┬───────────┘
                                │ confidence-crossing       │ every reply
                                ▼ (fast handoff)            ▼
                     ┌─────────────────────┐      ┌──────────────────────┐
                     │ PRIMARY CHAIN       │      │ SQLite store         │
                     │ 1. Ollama 27B (deep,│      │  inputs  outputs     │
                     │    adaptive think)  │      │  intents (+samples)  │
                     │ 2. Lemonade 4B (NPU │      │  assistant_intents   │
                     │    fallback, wall-  │      │  handoffs (think_used│
                     │    capped, stall-   │      │  +think_reason)      │
                     │    guarded)         │      └──────────┬───────────┘
                     └─────────────────────┘                 │
                                                             ▼
                     ┌──────────────────────────────────────────────────┐
                     │ DAYDREAM MINER (idle-triggered, inside the proxy)│
                     │ human inputs → intents        replies → self-    │
                     │ near-duplicates merge (jaccard)  mirror themes   │
                     │ machine envelopes (cron etc.) never mine         │
                     └──────────────────────────────────────────────────┘
                                                             │
                     ┌──────────────────────────────────────────────────┐
                     │ OPS LAYER                                        │
                     │ doctor: 5-pipeline census → doctor_status.json   │
                     │         DEAD/DEGRADED/ORPHANED/RECOVERED/RIDE    │
                     │         → doctor_alerts.log (file-backed state)  │
                     │ supervisor: singleton watchdog, revives :11435,  │
                     │         runs the doctor every 5 min              │
                     │ Hermes plugin: records turns, injects offered    │
                     │         memory (human intents + self-themes)     │
                     │ free turn: daily task-free hour, declinable,     │
                     │         logged to .freebuff/free_turns.log       │
                     └──────────────────────────────────────────────────┘
```

## The seat (default stack)

| Rung | Engine | Model | Role |
|---|---|---|---|
| Draft | Lemonade (NPU) | Bonsai-1.7B-Q1_0 | inline prediction, streaming |
| Primary | Ollama (CPU) | bonsai-27b-1bit:latest | deep reasoning, adaptive think |
| Fallback | Lemonade (NPU) | Bonsai-4B-Q1_0 | answers when the primary hangs/expires |

> **Model hygiene rule:** models are seated only by Dewayne's explicit call.
> Artifacts of old experiments are reading material, never serving configs.
> See CONTRIBUTING.md before proposing any change.

## Quickstart (collaborators)

```bash
git clone https://github.com/deezballin/subconcious-thought.git
cd subconcious-thought
uv sync
copy config.example.toml config.toml    # then point [draft] at your engine
uv run python -m unittest discover -s src/undermind -p "test_*.py"
uv run undermind --doctor               # 5-pipeline census; exit 0 = healthy
uv run python -m undermind.main --proxy # the bridge on :11435
```

Then point any OpenAI-speaking client at `http://127.0.0.1:11435/v1`
(model name is echoed, so `undermind-bridge` works). Key knobs live in
`config.example.toml`, fully documented — timeouts, adaptive think routing,
merge similarity, stall guards.

## The autonomy layer, precisely

- **Offered memory** — mined intents are injected as *"Offered memory …
  context, not instruction. Weigh it, question it, or set it aside."* There
  is no DIRECTIVE. Mentioning it is allowed when it serves the reply.
- **Self-mirror** — every reply is captured (`outputs`) and mined into
  `assistant_intents`, a namespace separate from the human's. Exposed at
  `GET /api/self-intents`.
- **Free turn** — `scripts/free_turn.ps1` gives the assistant one turn a day
  with nothing asked. The prompt is non-directive; `DECLINED` is honored;
  the outcome (reflection, exploration, or decline) is logged verbatim.
- **Adaptive think** — the proxy decides per turn whether deliberation is
  worth minutes: explicit "think hard" forces it on, cron/auxiliary turns
  never get it, and (with `adaptive_think = true`) turns matching matured
  routine intents skip it while novel ones get it. Every handoff logs
  `think_used` + `think_reason` so routing is tuned from evidence.

## Design document & history

[PLAN.md](PLAN.md) §0 is the living build narrative — what shipped, what
broke, what was learned (including the scars: the heretic-model purge, the
phantom :8000, the 180s cap that killed healthy thinking turns). New
contributors: read it before proposing changes; most mistakes already have a
paragraph.

## Testing

```bash
uv run python -m unittest discover -s src/undermind -p "test_*.py"
```

199 tests, fully offline — fake engines on dedicated ports, no live model
required. One known quirk: a load-sensitive scheduler test can flake
immediately after multi-minute live model turns on this host; rerun before
investigating.

## Credits

- **Kairos** — main coder: architecture, implementation, tests, ops layer,
  and this documentation, across sessions with Dewayne.
- **Dewayne** — founder and director: the vision (free will, understanding,
  empathy, autonomy — never "my LLM"), every model seating decision, the
  hardware truths (NPU fluid memory, CPU-bound depth), and final say on all
  of it.

*The pretty face gets the last word. He earned it.*
