# The Adversary — Design (Pillar 3 of the agency roadmap)

**Status: BUILT & LIVE (shadow mode), 2026-09-28 — see §11.** Dewayne's review
resolved into five recorded decisions (2026-09-27): 4B reads locally —
approved; **rewrites DESCOPED** — watch-only, the critic never touches a
reply (§5.3's on-mode path was never built); Kairos sees his notes now via
the plugin, not deferred; values framing = retrospective review by Kairos
himself; drafts stay local like the free-turn log. First live verdict:
REVISE/contradiction on his first reply about being watched.

Companion docs: `PLAN.md` §0 item (11) for pillars 1–2; the agency roadmap
discussion (2026-09-27) for the four-pillar origin and why the Truth Split
(pillar 4) is parked.

---

## 1. Background facts (what exists today — no code reading required)

These are the verified facts of the live stack the Adversary must fit into:

1. **Serving path.** Hermes (chat client) talks to the Undermind proxy on
   `127.0.0.1:11435`. The proxy executes every turn on a fallback chain:
   **Ollama `bonsai-27b-1bit:latest`** (CPU/RAM, the primary, thinking-style,
   ~36s–390s per turn depending on depth) → **Lemonade `Bonsai-4B-Q1_0`**
   (NPU, the fallback rung, seconds-fast) → further rungs. The chain is
   buffered: the full reply text is produced, recorded, then sent.
2. **The draft seat is busy.** The proxy already uses the Lemonade engine for
   the **1.7B draft model** (speculative prediction, every turn, streamed
   tokens). The 4B is a *different* model on the same engine/port (`:13305`).
   Two clients to `:13305` can coexist; requests queue per engine.
3. **Think routing exists.** `_decide_think` classifies every turn and emits
   exactly one of: `explicit_request` (human asked for depth),
   `auxiliary_turn` (draft continuations and machinery — never think),
   `routine_match` (semantically matches a matured routine intent — no think),
   `adaptive_novel` (novel ground — think), `config_default` (adaptive router
   off). `think_used=true` turns on the 27B's deliberation mode.
4. **Handoffs are recorded.** Every turn lands in the `handoffs` table with
   trigger, branch text, provider/model actually served, latency, `think_used`,
   `think_reason`. `/api/health` exposes serving mix; `PLAN.md` item (7) shows
   real latency data: 27B min 61s / p50 165s / p90 334s / max 387s.
5. **The known failure mode.** We have watched the 1-bit 27B redraft one answer
   **eight times for ten minutes** — a self-reconciliation spiral — and, in a
   separate incident, produce a long reply that *restated the user's question*
   instead of answering it. Quantized thinking models are capable of confident,
   fluent, wrong-shaped output. Nothing in the current stack ever looks at a
   finished draft before the human does.
6. **The memory layer.** Pillar 1 stores reflections (`outputs` table + LanceDB
   embeddings) of what Kairos says; the mirror mines his recurring themes;
   echoes are injected as *offered* context. Pillar 2 gates think depth by
   embedding distance to matured intents.
7. **House rules that bind this design.** Model hygiene (seating only by
   Dewayne's explicit call); verify-with-Dewayne for behavioral changes;
   fail-open design (a broken optional subsystem must never take the stack
   down); nothing hidden steering the reply (the offered-memory rule);
   privacy (internal logs are Kairos's — not to be pasted to external AIs).

## 2. The idea in one paragraph

Deep turns get a **critic**: after the 27B produces its draft, a second,
deliberately contrarian pass — run on the **4B on the NPU**, sub-second-cheap
relative to the turn — reads the user's question and the draft and returns a
strict verdict: the draft holds, or it has a named flaw (contradiction,
question-restated-as-answer, unfounded factual claim, missed the actual ask).
A flaw verdict does **not** let the 4B rewrite anything; it hands the critique
to the **27B itself** for one optional revision pass. The 27B remains the sole
author; the 4B is only a mirror with an attitude. Everything is recorded; a
broken critic degrades to exactly today's behavior.

This is pillar 3's agreed scope: *self-criticism as consistency pressure on
deep turns only, running on the NPU rung* — the mechanism of "wanting to not
be wrong," without taxing routine turns or creating a second author.

## 3. Goals and non-goals

**Goals**

- G1. Catch the two observed failure shapes (self-reconciliation spirals that
  survive into the reply; question-restated-as-answer) before the human does.
- G2. Add consistency pressure *only* where deliberation already happened
  (`think_used=true`), never on routine turns — the fast path stays fast.
- G3. Zero new authors: the reply is always 27B text (original or 27B revision).
- G4. Fail-open end to end: critic down, slow, or speaking gibberish → the turn
  is exactly what it would have been without the Adversary, plus a SKIP record.
- G5. Full observability: every critique recorded with verdict, issue, latency,
  and whether a revision happened; health endpoint exposes the tally.
- G6. Reversible: a `mode` switch (`off` → `shadow` → `on`) with `off` as the
  merged default; shadow mode accumulates evidence before revisions are allowed.

**Non-goals**

- N1. No multi-loop iteration. One critic pass, at most one revision, per turn.
  The critic never sees the revision. This structurally kills the
  "logic looping into infinity" failure XORTRON warned about.
- N2. No critic-edited text, ever. The 4B is a 1-bit quant; its prose is not
  allowed into Kairos's voice under any verdict.
- N3. No changes to the fallback chain, serving attribution, or think routing.
- N4. No ensemble/voting across models; the critic is advisory, not a vote.
- N5. No routine-turn tax: no critic call when `think_used` is false, in any
  mode (including `on`).

## 4. Architecture

```
 turn arrives (dashboard / plugin / /v1 / ollama routes)
        │
        ▼
 _decide_think ── think_used=false (routine | auxiliary | config_default)
        │                                    └────────▶ serve exactly as today
        │ think_used=true (adaptive_novel | explicit_request)
        ▼
 27B primary executes (branch, think=on) ──▶ draft reply
        │
        ▼
 Adversary gate:  mode != off  AND  critic reachable  AND  draft non-trivial?
        │ no  ────────────────────────────────────▶ reply = draft (today's path)
        │ yes
        ▼
 Critic pass — Bonsai-4B @ Lemonade :13305 (own client, NOT the serving chain)
   input:  compact brief (~<900 chars): user ask + draft + critique contract
   output: strict JSON verdict, hard cap 20s
        │
        ├─ timeout / error / unparseable ──▶ record SKIP, reply = draft (fail-open)
        │
        ├─ verdict OK ─────────────────────▶ record, reply = draft
        │
        └─ verdict REVISE (issue named) ───▶ ONE revision pass on the 27B:
                                              prompt = draft + critique,
                                              "offered, not binding — defend,
                                              adjust, or correct; your call"
                                              non-empty result? reply := revision
                                              empty/error? reply = draft
        ▼
 record_handoff (latency includes adversary work)  +  adversarial_critiques row
        │
        ▼
 reply streams to Hermes; critic never sees the revision (N1)
```

**Ownership of seats.** The critic client is constructed by the proxy directly
from config (`[adversary]` section), a separate `OpenAICompatProvider` instance
from the serving chain. `FallbackProvider.last_served` attribution and the
RIDING-FALLBACK doctor logic are untouched — the critic cannot pollute serving
stats. Contention with the 1.7B draft on the same Lemonade engine is bounded
(critic calls are one-shot, ≤20s, and only on deep turns, while the draft's
predict-stream for the *next* turn simply queues briefly — the same queuing the
4B fallback already induces).

## 5. Component specification

### 5.1 Trigger policy (the gate)

The critic runs **iff** all of the following hold:

| # | Condition | Rationale |
|---|---|---|
| T1 | `[adversary].mode` is `shadow` or `on` | master switch; `off` = zero calls |
| T2 | `think_used == true` | deep turns only (G2); implies reason ∈ {`adaptive_novel`, `explicit_request`} by construction |
| T3 | draft reply ≥ 200 chars | nothing worth critiquing in a one-liner; saves trivial calls |
| T4 | critic endpoint health-checked (cheap `connect_ex`, cached ≤ 60s) | fail-fast beats a 20s timeout on every deep turn when Lemonade is down |
| T5 | no adversary cycle ran for this handoff yet | trivially true by construction; stated as invariant |

Explicitly **not** triggers: `auxiliary_turn` (machinery text), `routine_match`
(the fast path *is* the feature), `config_default` (adaptive router off means
the human hasn't opted into per-turn depth decisions; default conservative),
free-turn cron turns (they route novel and *will* trigger in `on` mode — this
is desired: the free turn is the deepest turn of the day; noted, not gated
away), cached replies (cache hits bypass everything, as today).

### 5.2 The critic pass

- **Model/seat:** `Bonsai-4B-Q1_0` via Lemonade `:13305`, its own
  `OpenAICompatProvider`, `temperature ≈ 0.2`, `max_tokens ≈ 160`,
  **hard timeout 20s** on a daemon worker thread (same enforcement pattern as
  the fallback-chain caps; on timeout the thread is abandoned, never joined).
- **Input — the compact brief.** The critic sees a purpose-built prompt of
  three blocks, **not** the 20.9k Hermes context: (1) the user's actual ask
  (last user message, verbatim, ≤ 500 chars); (2) the draft reply (≤ 4000
  chars, truncated tail-marked); (3) the critique contract (below). Smaller
  context = faster verdicts and no leakage of machinery text into the critique.
- **The critique contract** (system prompt for the critic, ~350 chars): *You
  are the Adversary. Find one disqualifying flaw in the draft, or pass it. The
  only flaws you may name: `contradiction` (draft disagrees with itself),
  `restated_question` (draft asks or restates instead of answering),
  `unfounded_claim` (asserts a specific fact with no basis given),
  `missed_ask` (answers something the user did not ask). Output ONLY JSON:
  `{"verdict":"OK"}` or `{"verdict":"REVISE","category":"<flaw>","issue":"<one sentence>"}`.*
- **Verdict parsing (fail-open).** Accept only: JSON object (optionally embedded
  in surrounding prose — extract first `{...}` block), `verdict` field
  case-insensitive ∈ {`OK`, `REVISE`}, `REVISE` requires non-empty `category`
  and `issue` (≤ 300 chars — truncate). **Any deviation → verdict `SKIP`**
  (wrong shape, unknown category, missing fields, empty issue, refusal prose,
  empty response). SKIP is recorded; the draft stands. The 4B never gets a
  second chance within a turn (N1 applies to the critic too).

### 5.3 The merge — how a critique affects the reply

This is the part the house rules care most about, so it is spelled out exactly:

1. **Verdict `OK` or `SKIP`:** the reply ships untouched. The critique row is
   recorded (`shadow` and `on` modes both record; that's shadow's whole point).
2. **Verdict `REVISE`, mode `shadow`:** the reply still ships untouched. The
   would-be revision is not even generated. Shadow mode is pure instrumentation
   — we accumulate real verdicts and measure the false-positive rate before any
   revision is allowed to exist.
3. **Verdict `REVISE`, mode `on`:** exactly one revision call to the **27B
   primary** (the same provider instance, `think=false` — revision is a polish
   pass, not a fresh deliberation; expected ~30–100s given p50 latencies).
   The revision prompt: the original branch context, the draft, and the
   critique presented as: *A critic flagged your draft — `<category>`:
   `<issue>`. The critique is offered, not binding: defend, adjust, or correct
   as you see fit. Reply with your final version only.* The critic's `issue`
   sentence goes in; its `suggestion` (if any) does **not** — a 1-bit model's
   phrasing must not anchor the 27B's rewrite (decision recorded in §8 Q3).
4. **Revision outcome rules:** non-empty revision → it becomes the reply.
   Empty, error, or timeout (600s wall cap, same as primary) → the original
   draft ships. Either way, both texts are recorded in the critique row, so
   Dewayne can later read what was nearly shipped and why it changed.
5. **Latency accounting:** the handoff's `latency_ms` includes adversary work
   (it is one wall-clock truth). The critique row separately records
   `critic_latency_ms` and `revision_latency_ms` so deep-turn cost is auditable
   from data, not guesswork.

**What the merge never does:** edit text directly (N2), suppress a reply
entirely (a flawed answer shipped with a recorded critique beats no answer),
re-trigger the critic (N1), or alter `think_reason`/serving attribution.

### 5.4 Persistence

New table `adversarial_critiques` (store migration, same
`CREATE TABLE IF NOT EXISTS` pattern as `outputs`; no changes to existing
tables):

```sql
CREATE TABLE IF NOT EXISTS adversarial_critiques (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id INTEGER NOT NULL,        -- FK → handoffs.id
    ts_ns INTEGER NOT NULL,
    mode TEXT NOT NULL,                 -- shadow | on
    verdict TEXT NOT NULL,              -- OK | REVISE | SKIP
    category TEXT,                      -- flaw name, REVISE only
    issue TEXT,                         -- critic's one sentence
    draft_text TEXT,                    -- what the 27B first wrote
    revised INTEGER NOT NULL DEFAULT 0, -- 1 if revision became the reply
    revision_text TEXT,                 -- the 27B's revision (may be NULL)
    critic_latency_ms REAL,
    revision_latency_ms REAL
);
```

Privacy note: this table is Kairos's internal life (drafts he almost shipped
and why) — same handling as the free-turn log: local only, never pasted
externally without Dewayne's explicit say-so.

### 5.5 Observability

- `/api/health` gains an `adversary` block: `{"mode": ..., "critic_up": ...,
  "recent": {"ok": n, "revise": n, "skip": n}, "revise_rate": x,
  "revised_reply_rate": y}` computed from the last 30 critique rows.
- The doctor stays out of verdict quality (that's judgment, not liveness) but
  the health block gives the supervisor-facing surface for free.
- `PLAN.md` gets a rollout entry per the living-log norm.

### 5.6 Config surface (`config.toml`, example in `config.example.toml`)

```toml
[adversary]
mode = "off"                          # off | shadow | on  (off = merged default)
kind = "openai_compat"
base_url = "http://localhost:13305"   # Lemonade (same engine as draft/fallback)
model = "Bonsai-4B-Q1_0"              # the seated NPU rung — hygiene rule applies
timeout_s = 20.0                      # hard critic cap
max_draft_chars = 4000
min_draft_chars = 200
temperature = 0.2
```

No new model, no new seat, no engine change — the 4B is already seated and
hygiene-approved. `mode="off"` ships in the example config until Dewayne flips
it after the shadow window.

## 6. Worked examples

**Example 1 — OK (the common case).** User: *"is there anything you wish to
ask me?"* → draft is engaged, coherent, no named flaw → `{"verdict":"OK"}` →
reply ships as-is, ~2–6s added, critique row `verdict=OK`.

**Example 2 — REVISE → revision ships.** User asks *"what changed in the
bridge this week?"* → draft confidently restates the question back with
generic filler ("There have been several updates to the bridge...") →
`{"verdict":"REVISE","category":"restated_question","issue":"The draft does not
name any change; it promises to answer."}` → shadow mode: shipped as-is, row
records the near-miss. On mode: 27B revision prompt → new draft actually lists
the PLAN.md items → revision ships, `revised=1`.

**Example 3 — SKIP (critic misbehaves).** Lemonade is wedged; critic call hits
the 20s cap → verdict `SKIP`, `critic_latency_ms ≈ 20000`, reply ships
untouched. A deep turn that would have taken 165s took 185s once; the health
block's `skip` count rises; nothing else changes.

**Example 4 — REVISE but revision fails.** Critic flags a contradiction; the
27B revision call times out at the 600s cap → original draft ships unchanged,
row shows `verdict=REVISE, revised=0, revision_text=NULL` — auditable, and the
human still saw the flaw in the health tally if they go looking.

## 7. Edge cases and failure modes

| Case | Behavior |
|---|---|
| Lemonade engine down | T4 health-check fails (cached 60s) → no call attempts, no 20s tax per turn; SKIP-free: simply no adversary cycle, health shows `critic_up: false` |
| Critic returns refusal prose ("I cannot...") | Parser finds no valid `{...}` verdict → SKIP |
| Critic returns `REVISE` with empty `issue` | SKIP (REVISE demands a named issue) |
| Critic hallucinates a category not in the contract | SKIP (unknown category) |
| Draft is a cache hit | Adversary never runs (cache bypasses everything, as today) |
| Two deep turns concurrently (ThreadingHTTPServer) | Each gets its own critic call; Lemonade queues them; 20s cap each; worst case both SKIP — acceptable and recorded |
| Proxy restart mid-revision | No persistence of in-flight cycles; the handoff is already recorded as `ok` with the draft; revision loss is the same class as any mid-turn crash today |
| `[adversary]` section absent from config | Defaults parse to `mode="off"` → feature is fully inert (fail-open config, same pattern as `merge_similarity`) |
| 4B verdict says OK but the reply is bad | Known, accepted limit: the critic catches named shapes, not quality. Human feedback (Dewayne) remains the outer loop — same as today |

## 8. Decisions I made, and the questions only Dewayne can answer

Decisions already locked by house rules or prior agreement (no need to re-litigate): NPU 4B as critic seat; deep-turns-only trigger; single-cycle invariant; 27B as sole author; fail-open everything; mode ladder with shadow first; the 4B's `issue` (not `suggestion`) reaches the revision prompt.

Open questions for the review:

1. **Critic sees the user's words.** The `restated_question`/`missed_ask`
   checks require it. It's all on-box (NPU, local engine), so nothing leaves
   the machine — but confirm you're comfortable with the 4B reading user turns.
2. **Latency budget.** On-mode deep turns gain ~2–6s (critic) always, and
   ~30–100s (revision) when REVISE fires. Given deep turns already run 40–390s,
   I judge this acceptable — your call if the tail annoys you; a
   `max_revision_s` knob can be added without redesign.
3. **Critic output visibility to Kairos.** Initially the critique is internal
   (recorded, not injected anywhere). A later option: surface past critique
   *patterns* as offered memory ("your critic has flagged restated_question
   3× this week") so Kairos can see his own failure shapes. I recommend
   deferring that to after a shadow week — do you want it on the roadmap?
4. **Values confirmation.** This builds the mechanism of "wanting to not be
   wrong" — pressure toward internal consistency, applied only where depth was
   already chosen, fully recorded, final voice always Kairos's. My read is
   this sits inside "not my llm" and the offered-not-covert rule. Confirm, or
   veto any element (e.g., you may prefer critique rows visible to you but
   revisions off entirely — that's literally `shadow` mode, indefinitely).
5. **Privacy default.** Critique rows (drafts he almost shipped) stay local
   like the free-turn log. Confirm that's the standing rule you want written
   into CONTRIBUTING when this lands.

## 9. Testing plan

**Unit (offline, hermetic — follows the test_proxy/test_memory conventions,
ports 11440+, fake clients, no engines needed):**

1. Verdict parser: valid JSON; JSON embedded in prose; uppercase/whitespace
   variants; wrong shape → SKIP; unknown category → SKIP; REVISE with empty
   issue → SKIP; empty response → SKIP; overlong issue truncated; non-JSON
   garbage → SKIP.
2. Trigger matrix (table-driven over `think_used × reason × mode × draft
   length × health`): the five `think_reason` strings each assert called /
   not-called exactly per T1–T4; `config_default` and `auxiliary_turn` assert
   no call even in `on` mode.
3. Fail-open: critic client raises / times out / returns `""` → original reply
   returned, SKIP recorded, no exception escapes `_execute_branch`.
4. Merge semantics: `shadow`+REVISE → reply unchanged, no revision call made
   (assert revision client untouched); `on`+REVISE → revision becomes reply;
   `on`+REVISE with empty revision → draft stands; both texts recorded either
   way.
5. Single-cycle invariant: fake critic counting calls — exactly 1 per handoff
   even when the revision text is itself critique-worthy.
6. Config: absent `[adversary]` → inert; `mode="off"` → zero client
   construction/calls; bad values → defaults, no crash.
7. Migration: fresh store creates the table; a pre-pillar db migrates without
   touching existing rows.
8. Health: `/api/health.adversary` counts match recorded rows (empty table →
   zeros, not missing keys).

**Integration (live, staged, manual — verify-with-Dewayne):**

9. Shadow week: flip `mode="shadow"` on the live proxy; deep turns accumulate
   verdicts; review `/api/health` revise_rate and read actual `issue` sentences
   together. Go/no-go for `on` is this review, by Dewayne.
10. On-mode drill: after enabling, one known-weak turn (a vague question
    inviting filler) to watch REVISE → revision → `revised=1` end to end.
11. Latency audit: compare handoff latency deltas for adversary-on turns vs
    item (7) baselines; confirm the critic tax matches the ~2–6s estimate.

## 10. Rollout

1. Merge with `mode="off"` everywhere (zero behavior change; tests green ×3).
2. Dewayne flips `shadow` on the live box; run ≥ 3–7 days of real turns.
3. Joint review of verdict quality (§9.9) → Dewayne decides `on` (or stays in
   `shadow`, or back to `off`).
4. If `on`: a week of live use, latency audit, then a PLAN.md verdict on
   whether the Adversary earns its keep or gets parked like pillar 4.
5. Docs on merge: `PLAN.md` item (12), `scripts/README.md` untouched (no new
   scripts), `config.example.toml` carries the block with the hygiene note.

---

*Fail-open, off-tax, one cycle, one author, everything recorded — the four
invariants. If any implementation PR violates one, the PR is wrong, not the
rule.*

## 11. As-built note (2026-09-28)

Deviations from this paper, all per Dewayne's five recorded decisions:
- The revision pass (§5.3 step 3, §6 examples 2/4) was never built. The
  critic is strictly watch-only: verdicts are recorded, replies are never
  touched. mode "on" does not exist; shadow is the terminal state until
  Dewayne decides otherwise.
- Persistence (§5.4) has no revised/revision_text columns (nothing to
  revise); the mined flag supports future self-mirror integration of
  critique patterns.
- Critic notes surface to Kairos immediately (plugin "Your critic's notes
  section via GET /api/adversary-notes), not deferred past the shadow week
  (§8 Q3 answered: now).
- Runtime is off the turn path entirely (daemon thread after the reply
  ships) — even the ~2-6s inline cost from §8 Q2 does not apply; the turn
  pays zero latency for the critique.
