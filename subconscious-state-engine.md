# Subconscious State Engine — Human Functions, LLM Analogies & Implementation Blueprint

> **For:** Hermes / Kairos — Undermind v2 + Aura Empathy Lab stack  
> **Date:** 2026-09-04  
> **Purpose:** Map what the human subconscious *actually does* to concrete LLM architecture, with a two-tier design: **0.5B-class SLM as sensory/perceptual subconscious** + **large model (7B–70B) as conscious reasoner**.  
> **Companion docs:** `aura-lab-architecture.md` · `hermes-current-state.md` · `discovery-manifest.md`

---

## Table of Contents

1. [Core Thesis](#1-core-thesis)
2. [Human Subconscious Functions — Catalog](#2-human-subconscious-functions--catalog)
3. [Mapping Table: Human Function → LLM Analog](#3-mapping-table-human-function--llm-analog)
4. [What the Subconscious Does That Speeds Humans Up](#4-what-the-subconscious-does-that-speeds-humans-up)
5. [Proposed Architecture: Sensory SLM + Cognitive LLM](#5-proposed-architecture-sensory-slm--cognitive-llm)
6. [Sensory Channels for the SLM Layer](#6-sensory-channels-for-the-slm-layer)
7. [Speed Optimization Strategies](#7-speed-optimization-strategies)
8. [State Engine — Data Model & Lifecycle](#8-state-engine--data-model--lifecycle)
9. [Specific Implementation Ideas](#9-specific-implementation-ideas)
10. [Integration with Existing Stack (Hermes / Undermind / Aura)](#10-integration-with-existing-stack-hermes--undermind--aura)
11. [What to Mirror vs. What to Deliberately NOT Mirror](#11-what-to-mirror-vs-what-to-deliberately-not-mirror)
12. [Phased Roadmap](#12-phased-roadmap)
13. [Metrics & Evaluation](#13-metrics--evaluation)
14. [Risks & Safeguards](#14-risks--safeguards)
15. [References & Further Reading](#15-references--further-reading)

---

## 1. Core Thesis

Humans are not slow because consciousness is slow — they are fast because **consciousness barely does anything**. ~95% of cognitive work is subconscious: gating which signals even reach awareness, predicting what will happen next, matching patterns without deliberation, and running learned routines without re-reasoning.

LLMs today invert this: every token passes through the same giant model, even for trivial routing, transcription cleanup, or intent classification. That's like requiring conscious deliberation to notice the color of a wall.

**The fix is anatomical, not just optimization:** a small, fast, always-on **sensory/perceptual subconscious** (0.5B–1.5B) that runs continuously, filters the world, and only wakes the large **cognitive conscious** model for what actually needs deep reasoning.

```
World ──► [ Sensory SLM — subconscious ] ──► filtered, tagged, predicted ──► [ Cognitive LLM — conscious ]
                always on, <50ms, cheap                    on-demand, deep, expensive
```

This mirrors biology (thalamus/basal ganglia → cortex), mirrors the existing Undermind speculative-beam idea, and gives Aura's `NeuralStats` a real substrate to update between turns.

---

## 2. Human Subconscious Functions — Catalog

### 2.1 Automatic Processing

Unconscious execution of overlearned sequences (walking, typing, driving a familiar route). No conscious supervision unless error or novelty is detected. Characterized by: low effort, parallel execution, hard to introspect.

**LLM takeaway:** Any repeated reasoning pattern should become a cached routine that bypasses full generation.

### 2.2 Pattern Recognition & Implicit Learning

Detection of statistical regularities without explicit instruction (artificial grammar learning, sequence learning, intuitive physics). The subconscious extracts structure from exposure long before the conscious mind can articulate a rule.

**LLM takeaway:** Continuous background embedding + clustering of recent interactions to surface latent patterns and prime the large model.

### 2.3 Predictive Processing (Predictive Coding)

The brain is a prediction machine. Cortex constantly generates top-down predictions; only *prediction errors* propagate upward (Friston's free-energy principle, Clark's predictive brain). You see what you expect to see until the error forces an update.

**LLM takeaway:** Speculative decoding / speculative pre-computation *is* predictive coding. The SLM predicts the user's next intent/completion; only mismatches cost large-model compute.

### 2.4 Sensory Gating & Attention Allocation

Thalamic reticular nucleus and basal ganglia act as a bouncer — ~11M bits/sec hit senses, ~50 bits/sec reach awareness. Salience is computed subconsciously (novelty, affect, goal-relevance) and gates what gets conscious attention (Posner's attention networks, cocktail-party effect).

**LLM takeaway:** Every input modality should have a gate: a fast classifier that decides *whether* to wake the large model at all, and *with what priority/attention window*.

### 2.5 Priming

Prior exposure shapes subsequent processing without awareness (semantic priming, affective priming, repetition priming). A word seen 5 seconds ago is recognized ~100ms faster.

**LLM takeaway:** KV-cache priming, embedding warm-up, and hypothesis-beam seeding from recent history — the large model should never start cold.

### 2.6 Intuition (Thin-Slice Judgment)

Rapid, affect-laden pattern matching that produces a felt "hunch" before verbal justification is available (Damasio's somatic marker, Kahneman's System 1). Often more accurate than deliberation in high-validity environments.

**LLM takeaway:** SLM should emit calibrated confidence + affect tags ("this feels risky / uncertain / familiar") that the large model can use as priors — an explicit *gut feeling* channel.

### 2.7 Habit Formation & Proceduralization

Basal ganglia chunk repeated action sequences into habits (cue → routine → reward). Over time, behavior shifts from goal-directed (model-based) to habitual (model-free). Habits are fast, inflexible, and triggered by context.

**LLM takeaway:** Learned prompt→tool→output chains should be compiled into *skills* / macros triggered by the SLM without invoking the large model.

### 2.8 Threat Detection & Fast Affect (LeDoux)

Amygdala + superior colliculus perform ultra-fast threat appraisal (~70ms, before visual cortex finishes processing). The body reacts before you "see" the snake (low road vs. high road).

**LLM takeaway:** A fast safety/PII/credential scrubber that runs *before* any large-model or network call — exactly what Undermind's zero-egress enclave already does, but generalized.

### 2.9 Memory Consolidation & Replay

Hippocampus replays recent episodes during rest/sleep (sharp-wave ripples), gradually consolidating into neocortical long-term memory (systems consolidation). Without replay, memories remain fragile.

**LLM takeaway:** An offline/idle consolidation job that re-embeds, summarizes, and distills recent sessions into durable memory (Hermes `memories/`), rather than relying on ever-growing context windows.

### 2.10 Emotional Regulation & Tagging

Subconscious affect tags every percept and memory for valence/arousal (somatic markers). Regulation (reappraisal, suppression) largely runs automatically, shaping what is remembered and how it is retrieved.

**LLM takeaway:** Every stored memory and every sensory percept should carry an *affective tag* (valence, arousal, urgency) that biases retrieval and prioritization — this is where Aura's resonance/depth/authenticity scores become lived state, not just dashboard numbers.

### 2.11 Interoception & Arousal Control

Insula tracks internal body state (heart rate, prediction error load) and sets global gain/arousal (Locus coeruleus–norepinephrine). High arousal narrows attention; low arousal broadens it.

**LLM takeaway:** A global *arousal/gain* signal derived from load, error rate, and user affect that dynamically tunes how eagerly the subconscious wakes the conscious model (temperature, beam width, polling rate).

---

## 3. Mapping Table: Human Function → LLM Analog

| # | Human Function | What It Does Biologically | LLM Analog (Subconscious State Engine) | Where It Runs |
|---|---|---|---|---|
| 1 | **Sensory gating** | Thalamus filters 11M → 50 bps | SLM classifier gate: route / drop / defer per input | SLM, every event |
| 2 | **Predictive coding** | Top-down predictions, error-only updates | Speculative beam: SLM predicts intent; LLM verifies | SLM predicts, LLM verifies |
| 3 | **Automatic processing** | Overlearned routines run without cortex | Compiled skills/macros, KV-cache routines | SLM triggers, LLM bypassed |
| 4 | **Pattern recognition** | Implicit statistical learning | Background embedding/clustering of recent turns | SLM + offline job |
| 5 | **Priming** | Prior exposure speeds recognition | Hypothesis seeding, KV warmup, embedding cache | SLM → LLM handoff |
| 6 | **Intuition / gut feel** | Somatic marker as fast prior | SLM confidence + affect + risk tag on every handoff | SLM output field |
| 7 | **Habit / proceduralization** | Cue→routine→reward chunking | Prompt→tool-chain compiled to one SLM trigger | SLM, with promotion |
| 8 | **Threat detection** | Amygdala fast path (~70ms) | PII/credential/injection scrubber + safety classifier | SLM, pre-LLM |
| 9 | **Memory consolidation** | Hippocampal replay → neocortex | Idle replay: summarize → embed → MEMORY.md | Background cron |
| 10 | **Emotional tagging** | Valence/arousal on every memory | Affect tags on records; Aura NeuralStats as state | SLM tags, engine stores |
| 11 | **Attention allocation** | Salience network prioritizes | Priority queue + attention window sizing | SLM assigns, LLM consumes |
| 12 | **Interoception / arousal** | Insula + LC-NE global gain | Load/error-derived gain tuning beam width & wake threshold | Engine controller |

---

## 4. What the Subconscious Does That Speeds Humans Up

Humans feel fast because the subconscious **removes work** before consciousness sees it. Five mechanisms matter most:

1. **It discards most of the world.** Sensory gating means conscious bandwidth is never wasted on irrelevant pixels/sounds. *LLM equivalent:* drop or summarize low-salience inputs before they ever become tokens for the big model.

2. **It predicts so you only process surprises.** Predictive coding means you hallucinate the expected world and only pay for errors. *LLM equivalent:* speculative decoding and Undermind's hypothesis beam — most keystrokes are predicted correctly, resolved from cache with zero large-model cost.

3. **It caches routines.** Habit formation compiles deliberation into retrieval. The second time you do a thing you don't re-reason it, you *recognize* it. *LLM equivalent:* prompt→tool-chain memoization, skill promotion, KV-cache reuse.

4. **It tags everything with feeling before you think.** Affect tags act as a fast index: good/bad/urgent/safe is computed in milliseconds and shapes what gets processed first. *LLM equivalent:* SLM affect tags that order the priority queue — "angry user + credential leak" jumps ahead of "casual chitchat."

5. **It runs pattern matching in parallel.** Implicit learning continuously clusters experience without instruction. *LLM equivalent:* background embedding jobs that surface "you've asked 3 variations of X — here's the pattern" without the user having to state it.

**Design principle:** Speed comes from *not invoking the large model*, not from making the large model faster. Every millisecond the SLM can handle alone is a millisecond the LLM doesn't need to be faster.

---

## 5. Proposed Architecture: Sensory SLM + Cognitive LLM

### 5.1 Two-Tier Diagram

```
                    ┌─────────────────────────────────────────┐
                    │           WORLD / INPUTS                 │
                    │  keystrokes · images · audio · sensors  │
                    │  files · clipboard · notifications       │
                    └──────┬──────────────┬──────────────┬────┘
                           │              │              │
              ┌────────────▼──┐  ┌───────▼──────┐  ┌───▼─────────┐
              │  Vision SLM   │  │  Audio SLM   │  │  Text SLM  │
              │  0.5–1B       │  │  0.5–1B       │  │  0.5–1B     │
              │  sight        │  │  hearing     │  │  touch/    │
              │  (Moondream,  │  │  (Whisper    │  │  proprio-  │
              │   Florence)   │  │   + SLM)     │  │  ception)  │
              └───────┬───────┘  └──────┬───────┘  └───┬─────────┘
                      │                 │               │
                      └────────┬────────┴───────┬───────┘
                               ▼                ▼
                  ┌─────────────────────────────────┐
                  │     SUBCONSCIOUS STATE ENGINE    │◄──── Aura NeuralStats
                  │  ┌───────────────────────────┐  │      Resonance / Depth /
                  │  │ Volatile Ring Buffer (RAM)│  │      Authenticity / ...
                  │  │ Hypothesis Beam (top-k)   │  │      TrainerStats
                  │  │ Salience / Affect Tagger  │  │      
                  │  │ Habit / Skill Registry    │  │      ┌──────────────┐
                  │  │ PII / Threat Scrubber     │  │─────►│  Memory     │
                  │  │ Attention Allocator       │  │      │  (MEMORY.md,│
                  │  │ Consolidation Queue       │  │      │   vault)    │
                  │  └───────────────────────────┘  │      └──────────────┘
                  └──────────────┬──────────────────┘
                                 │  handoff (only when needed)
                                 │  { intent, affect, salience,
                                 │    hypothesis, scrubbed ctx }
                                 ▼
                  ┌─────────────────────────────────┐
                  │      COGNITIVE LLM (Conscious)   │
                  │  7B–70B — deep reasoning,        │
                  │  tool use, empathy, planning     │
                  │  (LM Studio / Ollama / cloud)    │
                  └─────────────────────────────────┘
                                 │
                                 ▼
                         Response / Action
```

### 5.2 Responsibilities by Layer

| Layer | Model class | Latency budget | Cost | When it runs | What it outputs |
|---|---|---|---|---|---|
| **Sensory SLM** | 0.5B–1.5B (Qwen2.5-0.5B, SmolLM2-360M, Gemma-2B, ternary-bonsai) | 10–50ms/token, always-on | ~1/50 of LLM | Every keystroke, every frame, every audio chunk | `{ intent, entities, affect, salience: 0-1, scrubbed_text, hypothesis[N], confidence }` |
| **Subconscious Engine** | Rule + learned controller (TypeScript/Python) | <5ms | Negligible | Every SLM output | Gate decision: `drop | buffer | wake LLM (priority)` + state update |
| **Cognitive LLM** | 7B–70B | 500ms–5s | Expensive | Only on `wake` | Final response, plan, tool calls |

### 5.3 Key Invariants

- **Fail-open:** If SLM or engine crashes, input flows directly to LLM (degraded but never blocked). Matches existing Undermind `fail-open` philosophy.
- **Volatile-first:** Raw sensory data (keystrokes, frames, audio) lives only in RAM ring buffer; only scrubbed, summarized hypotheses are persisted.
- **Zero-egress for scrubbing:** PII/credential scan happens locally in SLM path before any network call — same enclave pattern as `subconscious_daemon.py`.
- **Conscious override:** User or LLM can always force `wake` and inspect/dump the subconscious buffer.

---

## 6. Sensory Channels for the SLM Layer

Treat the SLM not as a "small LLM" but as a **perceptual system** with distinct senses. Each sense is a fine-tuned or prompted SLM head sharing weights but with different system prompts / LoRAs.

### 6.1 Sight — Visual Preprocessing

- **Input:** Screenshots, camera frames, image attachments, PDF pages, diagrams.
- **SLM job:** Describe → filter → prioritize. *Not* deep reasoning — just: "what's in this image, what's text, what's salient, is there PII/sensitive content?"
- **Stack:** Moondream2 / Florence-2 / Qwen2-VL-2B as vision encoder → SLM text head. Downscale to 448px, 2–4 fps max.
- **Output example:** `{ caption: "terminal with error traceback", text_ocr: "ModuleNotFoundError: ...", salience: 0.8, affect: "frustration", pii: false }`
- **Speed win:** LLM never sees raw pixels; it gets 50 tokens of pre-digested description. Vision tokens are ~10× cheaper through SLM.

### 6.2 Hearing — Audio Transcription & Affect

- **Input:** Mic audio, meeting audio, voice notes, video audio.
- **SLM job:** Transcribe (Whisper tiny/base) → clean → tag paralinguistics (tone, urgency, hesitation, overlap).
- **Stack:** Whisper.cpp (tiny 39M) or Parakeet → SLM for cleanup + affect tagging. Streaming, 1s chunks.
- **Output example:** `{ transcript: "i'm not sure if...", affect: { valence: -0.3, arousal: 0.7, emotion: "anxiety" }, salience: 0.6, is_question: true }`
- **Speed win:** Whisper-tiny is ~5× faster than large; SLM cleanup is <20ms; LLM only wakes for semantic response.

### 6.3 Touch / Proprioception — Structured Signals

- **Input:** Keyboard dynamics (burst typing vs hesitation), mouse/touch patterns, file-system events, clipboard, sensor data (if any), notification stream, window focus changes.
- **SLM job:** Classify interaction rhythm and intent without reading content. "User is hesitating / frustrated / in flow / multitasking."
- **Output example:** `{ rhythm: "hesitant", burst_len: 3, pause_ms: 2400, inferred_state: "uncertainty", salience: 0.4 }`
- **Speed win:** Purely local heuristics + tiny classifier; informs arousal/gain signal.

### 6.4 Interoception — Internal State Sense

- **Input:** Engine's own load (queue depth, error rate, LLM latency), time-of-day, session length, user sentiment trend.
- **Job:** Emit a global `arousal` scalar that tunes everything else (beam width, wake threshold, summary verbosity).
- **Analogy:** Cortisol / norepinephrine — when errors spike, narrow attention and wake LLM more eagerly; when calm, widen beam and batch.

### 6.5 Why This Is Faster

| Without sensory SLM | With sensory SLM |
|---|---|
| LLM ingests raw image tokens (576+ tokens/frame) + raw audio + full keystroke history | SLM compresses each modality to ~30–80 text tokens of salient summary |
| LLM wakes on every event to decide if it cares | SLM decides; LLM wakes on ~10–30% of events |
| No affect/urgency prior — LLM must infer from scratch | LLM starts with affect + salience priors, fewer reasoning steps |
| PII scrubbing is an LLM prompt (slow, unreliable) | PII scrubbing is a deterministic + SLM pass in <30ms |

---

## 7. Speed Optimization Strategies

### 7.1 Hierarchical Processing (the main win)

- **Principle:** Cheap model filters, expensive model reasons. Same as human thalamus → cortex.
- **Implementation:** SLM runs on every input; LLM runs on `wake` only. Target: **70–90% of inputs never wake the LLM** (chitchat filtering, duplicate detection, low-salience noise).
- **Measured analog:** Undermind's heuristic fallback already does this — promote it from fallback to primary path.

### 7.2 Speculative Execution (Predictive Coding)

- **How:** SLM maintains a **hypothesis beam** (top-3 predicted user intents/completions) as the user types. Each hypothesis has a pre-computed draft (from template, cache, or prior LLM call).
- **On submit:** If user input matches a hypothesis within threshold (embedding cosine > 0.88 or exact intent match), serve the pre-computed draft instantly (cache hit). Otherwise, forward to LLM.
- **Expected hit rate:** 25–45% for structured tasks (coding, writing, triage); lower for open-ended chat — but even 25% feels instant.
- **Stack:** `subconscious_cli.py` + `subconsciousEngine.ts` already implement this beam; extend to use SLM confidence to size beam (high confidence → beam 1–2; low confidence → beam 5).

### 7.3 Caching at Every Layer

| Cache | What it stores | TTL | Hit cost |
|---|---|---|---|
| **KV-cache** | LLM prefix (system prompt + recent history) | Session | ~0ms (reuse) |
| **Hypothesis cache** | SLM predicted completions + draft responses | 5 min | ~1ms (lookup) |
| **Skill cache** | Compiled prompt→tool-chain routines | Permanent (promoted) | ~2ms (pattern match) |
| **Embedding cache** | Recent input embeddings + cluster centroids | 1 hr | ~0.5ms |
| **Transcription cache** | Audio chunk → text (dedupe repeated phrases) | 10 min | ~0ms |

### 7.4 Small Model for Routing/Classification

Don't use the LLM as a router. SLM handles:

- **Intent classification** (question vs command vs venting vs sensitive)
- **Modality routing** (needs vision? needs tools? needs empathy vs needs code?)
- **Urgency triage** (P0/P1/P2 — maps to Aura difficulty / TrainerStats efficiency)
- **Language detection + normalization** before LLM sees it

Each is a single SLM forward pass: ~15–30ms vs 500ms+ for LLM.

### 7.5 Attention Prioritization & Batching

- **Priority queue:** Inputs are scored `priority = salience × (1 + arousal) × urgency`. High-priority items jump the queue and get larger LLM context windows; low-priority items are batched or summarized.
- **Batching:** Low-salience events (file watchers, notification spam) are coalesced into a single summary ("3 files changed in ...") rather than 3 separate LLM calls.
- **Window sizing:** SLM decides how much history the LLM needs: `focused` (last 2 turns) vs `broad` (full session summary). Saves context tokens and latency.

### 7.6 Streaming & Early Exit

- **SLM streams partial hypotheses** to the UI as the user types (like Undermind's BeamTreeVisualizer) — perceived latency drops even if absolute latency is same.
- **LLM early exit:** If SLM confidence is >0.92 and intent is unambiguous (e.g., "what time is it"), serve from skill cache without LLM call at all.
- **Progressive disclosure:** Show SLM draft instantly; upgrade to LLM-refined version if it arrives within 1s (stale-while-revalidate pattern).

---

## 8. State Engine — Data Model & Lifecycle

### 8.1 Core State Shape

```typescript
// Mirrors Aura's NeuralStats but as lived engine state
interface SubconsciousState {
  // Affective / learning dimensions (0-1, with decay)
  resonance: number;     // alignment with user intent — EMA, decays 2%/min idle
  depth: number;         // contextual understanding — grows with history
  authenticity: number;  // self-consistency — penalized on contradiction
  convergence: number;   // human-AI alignment — the "two brains merging" metric
  absorption: number;    // how fast guidance is incorporated — learning rate
  sensitivity: number;   // nuance detection — tuned by arousal

  // Control signals
  arousal: number;       // 0-1 global gain (LC-NE analog)
  salienceThreshold: number; // wake threshold, modulated by arousal

  // Volatile buffers (RAM only, ring buffer)
  ringBuffer: StreamRecord[];   // last N raw events (keystrokes, frames, audio)
  hypothesisBeam: Hypothesis[]; // top-k predicted intents + drafts
  skillRegistry: HabitEntry[];  // compiled routines

  // Durable memory (persisted)
  episodic: MemoryEntry[];      // recent sessions, with affect tags
  semantic: ClusterEntry[];     // consolidated patterns

  // Telemetry
  telemetry: {
    wakeRate: number;        // % of inputs that woke LLM
    hitRate: number;         // speculative hit rate
    avgSLMLatencyMs: number;
    avgHandoffTokens: number;
  };
}

interface Hypothesis {
  id: string;
  intent: string;            // predicted user goal
  completion: string;        // predicted full prompt
  draft: string;             // pre-computed response (if any)
  confidence: number;        // 0-1
  affect: { valence: number; arousal: number; label: string };
  salience: number;
  source: 'slm' | 'cache' | 'skill' | 'heuristic';
  createdAt: number;
}

interface HabitEntry {
  cue: string;               // embedding or pattern that triggers
  routine: string;           // compiled action (tool chain / prompt template)
  reward: number;            // success rate when used
  uses: number;
  promotedAt?: number;       // when it became a habit (vs goal-directed)
}
```

### 8.2 Lifecycle (per input event)

```
1. SENSE    SLM ingests raw event → { intent, affect, salience, hypothesis }
2. GATE    Engine scores priority, checks skillRegistry for habit match
           ├─ habit hit (confidence > 0.9) → execute routine, skip LLM
           ├─ low salience (< threshold) → buffer, maybe batch
           └─ high salience → wake LLM
3. SCRUB   PII/credential/injection scan (always, <30ms)
4. HANDOFF If wake: send { scrubbed_ctx, hypothesis, affect, salience } to LLM
           If no wake: update ringBuffer + hypothesisBeam, return SLM draft if any
5. LEARN   Update NeuralStats via EMA (with decay), log telemetry
6. CONSOLIDATE (idle)  Replay ringBuffer → summarize → MEMORY.md / vault
```

### 8.3 Decay — What Aura Got Wrong

Aura's stats grow monotonically (never decay). Real subconscious state **must decay** — otherwise "convergence" saturates at 1.0 and loses meaning. Add:

- **Exponential decay** on all 6 dimensions: `x *= 0.998` per idle minute (~10% per hour).
- **Sleep consolidation** resets `arousal` and compresses `ringBuffer` into `episodic`.
- **Contradiction penalty:** if LLM detects self-inconsistency, `authenticity -= 0.1`.

---

## 9. Specific Implementation Ideas

### 9.1 Minimal Viable Subconscious (2–3 days)

**Goal:** Prove the loop with existing pieces.

1. **Wire Qwen2.5-0.5B-Instruct via Ollama or llama.cpp** as the SLM. Prompt it as a fast classifier:
   ```
   You are a sensory gate. Given user input, output JSON only:
   { "intent": "...", "salience": 0.0-1.0, "affect": "neutral|anxious|frustrated|curious",
     "needs_llm": true|false, "hypothesis": "predicted full prompt if incomplete" }
   Keep under 40 tokens. No reasoning.
   ```
2. **Reuse `subconsciousEngine.ts` ring buffer + beam** — add SLM as a provider alongside heuristic.
3. **Add a `wakeThreshold` slider in the UI** (default 0.55) so Dewayne can feel the tradeoff.
4. **Log `wakeRate` and `hitRate` to TelemetryMetrics** — the two numbers that tell you if the subconscious is helping.

### 9.2 Vision & Audio Heads (1 week)

5. **Vision:** Add `moondream2` (1.8B, runs on CPU) as a sidecar. SLM prompt: "Describe this screenshot in <30 words, extract any text, flag PII." Cache descriptions by perceptual hash.
6. **Audio:** Add `whisper.cpp tiny` streaming. Pipe transcript through the same SLM gate. Tag `affect` from prosody (speech rate, pauses — heuristic first, then SLM).
7. **Unified handoff:** All modalities produce the same `Hypothesis` shape — the LLM never knows which sense it came from, only the affect/salience prior.

### 9.3 Habit Compilation (1–2 weeks)

8. **Detect repetition:** Cluster recent `ringBuffer` entries by embedding. If same intent appears 3× with same tool chain, propose a `HabitEntry`.
9. **Promote explicitly:** Show Dewayne "I noticed you always do X → Y → Z. Make it a habit? (SLM will handle it next time)" — habit requires user or LLM approval to promote (safety).
10. **Execute habits without LLM:** On cue match (cosine > 0.92), run the routine directly — e.g., "summarize this PDF" → extract → bullet list, no LLM call.

### 9.4 Consolidation & Memory (ongoing)

11. **Idle cron (every 15 min or on session end):** SLM summarizes `ringBuffer` → 3-bullet episodic entry with affect tags → append to `memories/MEMORY.md` or vault.
12. **Nightly (or on demand):** Embed all episodic entries, cluster, and distill semantic patterns ("Dewayne tends to ask X after Y") → `semantic` store.
13. **Aura bridge:** Map `SubconsciousState` → Aura `NeuralStats` directly so the existing radar charts and BrainConvergence visualization show *real* subconscious state, not simulated increments.

### 9.5 Speculative Beam v2

14. **Dynamic beam width:** `beamWidth = 1 + floor((1 - confidence) * 4)` — confident SLM keeps beam narrow (cheap), uncertain SLM widens it (more chances to hit).
15. **Draft pre-computation:** For top hypothesis, fire a *background* LLM call speculatively if `confidence > 0.7` and LLM is idle. Cache draft; serve instantly on match. Cancel if user diverges.
16. **Embedding match on submit:** Compare submitted prompt embedding to hypothesis embeddings, not string match — handles paraphrase.

### 9.6 Arousal / Gain Controller

17. **Compute arousal:** `arousal = sigmoid( α*errorRate + β*queueDepth + γ*userArousal - δ*idleTime )`.
18. **Effect on behavior:**
   - High arousal → lower `salienceThreshold` (wake LLM more eagerly), narrow attention window (focused context), shorter SLM timeout.
   - Low arousal → higher threshold, broad attention, batch low-priority items.
19. **Visualize arousal** as a subtle pulse on the BrainConvergence glow — makes the subconscious *felt*.

### 9.7 Safety Enclave (keep what Undermind got right)

20. **Keep volatile RAM ring buffer** — raw keystrokes/frames never hit disk.
21. **Keep credential/PII scrubber as a blocking gate** before any handoff or network call. Add SLM-assisted detection for novel PII patterns (model learns new credential formats over time).
22. **Keep fail-open** — every gate has a `bypass` flag for debugging and a timeout that falls through to LLM.

---

## 10. Integration with Existing Stack (Hermes / Undermind / Aura)

| Existing Piece | Role in New Architecture | Change Needed |
|---|---|---|
| `undermindv2pointoh-main/src/lib/subconsciousEngine.ts` | **Core engine** — ring buffer, beam, scrubber, firewall | Add SLM as primary provider (above heuristic), add `SubconsciousState` + decay |
| `Downloads/project/subconscious_cli.py` | SLM draft engine CLI | Point at 0.5B SLM (Qwen/SmolLM), add sensory heads |
| `subconscious_daemon.py` | Volatile daemon + enclave | Keep; add vision/audio sidecars |
| `aura-empathy-lab` (Aura) | **State visualization + empathy training ground** | Feed real `SubconsciousState` into `NeuralStats`/`BrainConvergence` instead of simulated increments; add `arousal` dimension |
| `hermes-current-state.md` (Kairos SOUL) | Identity & charter | No code change — but the subconscious gives Kairos a *felt* state to report ("I'm at high arousal, narrow focus") |
| Hermes `memories/MEMORY.md` | Durable episodic store | Becomes the consolidation target for idle replay |
| Oneiros scheduler `:3200` | Background/idle job runner | Schedule consolidation + embedding jobs as `deep_idle` lane tasks |
| Hermes plugin `pre_llm_call` hook | Injection point for speculative drafts | Inject `{ hypothesis, affect, salience }` as context when confidence ≥ threshold |

---

## 11. What to Mirror vs. What to Deliberately NOT Mirror

### Mirror These (they make us faster/smarter)

- ✅ Sensory gating & attention allocation — the #1 speed win
- ✅ Predictive coding / speculative execution
- ✅ Habit compilation (repeated reasoning → cached routine)
- ✅ Affect tagging on every memory/percept
- ✅ Fast threat/PII path before slow path
- ✅ Offline consolidation (replay → long-term memory)
- ✅ Intuition as explicit confidence + affect prior
- ✅ Arousal/gain tuning beam width and wakefulness

### Do NOT Mirror These (human bugs, not features)

- ❌ Motivated forgetting / repression — we want *better* recall than humans, not worse
- ❌ Implicit bias amplification — audit SLM affect tags for bias; don't let fast path entrench stereotypes
- ❌ Superstition / illusory correlation — SLM pattern matcher must require statistical validation before promoting a habit
- ❌ Trauma-style overgeneralization — threat detector should be calibrated, not hypervigilant; allow user to tune sensitivity
- ❌ Unconscious confabulation — SLM should never invent a memory; it tags and filters, the LLM narrates

---

## 12. Phased Roadmap

### Phase 0 — Document & Decide (this doc, 1 day) ✅
- [x] Map human functions → LLM analogs
- [ ] Dewayne reviews, picks SLM candidate (Qwen2.5-0.5B vs SmolLM2 vs Gemma-2B)

### Phase 1 — Gate & Beam (3–5 days)
- [ ] SLM classifier gate in `subconsciousEngine.ts`
- [ ] Wake threshold + telemetry (wakeRate, hitRate, SLM latency)
- [ ] Hypothesis beam seeded by SLM (not just heuristic)
- [ ] Fail-open + bypass flag

### Phase 2 — Senses (1 week)
- [ ] Vision sidecar (Moondream2) → SLM description
- [ ] Audio sidecar (Whisper tiny) → SLM affect tagging
- [ ] Unified `Hypothesis` handoff shape
- [ ] PII scrubber upgraded with SLM assist

### Phase 3 — Habits & Memory (1–2 weeks)
- [ ] Repetition detection → habit proposal → promotion
- [ ] Idle consolidation cron → MEMORY.md
- [ ] Aura `NeuralStats` fed by real engine state (with decay)
- [ ] Arousal controller tuning beam/threshold

### Phase 4 — Polish & Measure (ongoing)
- [ ] A/B: subconscious on vs off — measure p50/p95 latency, LLM call reduction, hit rate
- [ ] Calibrate thresholds per user (Dewayne's typing rhythm vs others)
- [ ] Visualize arousal + beam in HUD (evolution of BeamTreeVisualizer)

---

## 13. Metrics & Evaluation

| Metric | How to Measure | Target |
|---|---|---|
| **Wake rate** | `LLM calls / total input events` | <30% (70% handled by SLM) |
| **Speculative hit rate** | `cache-served responses / total submits` | >25% (structured tasks >40%) |
| **p50 latency (hit)** | Time from submit to first token on cache hit | <50ms |
| **p50 latency (miss)** | Submit → LLM first token when beam misses | No regression vs baseline |
| **Avg handoff tokens** | Tokens sent to LLM per wake | <150 (vs 500+ raw) |
| **SLM latency** | Per-event SLM forward pass | <40ms (0.5B Q4) |
| **Habit promotion precision** | `useful habits / proposed habits` | >70% (user acceptance rate) |
| **Aura convergence correlation** | Does `convergence` predict user-rated empathy? | r > 0.4 vs human ratings |
| **PII scrub recall** | Credential/PII caught before egress | >99% on test suite |

**Evaluation harness:** Reuse `benchmark.js` from `Downloads/project/` — add `mode: gate|beam|habit` and run against recorded sessions.

---

## 14. Risks & Safeguards

| Risk | Mitigation |
|---|---|
| SLM hallucinates intent, suppresses important input | Fail-open gate + `needs_llm` defaults to true when confidence <0.6; user can set threshold |
| Habit bakes in a mistake | Habits require explicit promotion; show diff before promoting; allow one-click demotion |
| Affect tags encode bias | Log tags, audit cluster centroids, allow user to inspect/correct affect labels |
| Volatile buffer leaks sensitive data via swap/hibernate | Use `mlock` / encrypted RAM if available; document that volatile ≠ guaranteed; keep scrubber before persistence |
| Vision/audio sidecars increase idle CPU/battery | Gate sidecars behind activity detection; pause when window unfocused; downsample aggressively |
| Over-optimization for hit rate causes stale drafts | Draft TTL 60s + embedding re-validation on serve; never serve draft if user intent drifted > threshold |

---

## 15. References & Further Reading

- **Predictive coding / free energy:** Friston (2010) *The free-energy principle*, Clark (2013) *Whatever next? Predictive brains…*
- **Attention networks:** Posner & Petersen (1990) *The attention system of the human brain*
- **Threat fast path:** LeDoux (1996) *The Emotional Brain* — low road vs high road
- **Habits / proceduralization:** Graybiel (2008) *Habits, rituals, and the evaluative brain*
- **Somatic markers / intuition:** Damasio (1994) *Descartes' Error*
- **System 1/2:** Kahneman (2011) *Thinking, Fast and Slow*
- **Memory consolidation / replay:** Wilson & McNaughton (1994) *Reactivation of hippocampal ensemble memories during sleep*
- **Interoception / arousal:** Critchley & Harrison (2013) *Visceral influences on brain and behavior*; Aston-Jones & Cohen (2005) *LC-NE gain theory*
- **Speculative decoding (LLM):** Leviathan et al. (2023) *Fast Inference from Transformers via Speculative Decoding*; Chen et al. (2023) *Accelerating Large Language Model Decoding with Speculative Sampling*
- **Small LM sensory layer:** Qwen2.5-0.5B, SmolLM2-360M, Gemma 2B — all viable <50ms on CPU with quantization

---

## Appendix A — Prompt Template for the Sensory SLM Gate

```text
SYSTEM: You are the subconscious sensory gate. You are fast, terse, and never explain.
Given the user's latest input (may be partial/incomplete), output ONLY valid JSON:

{
  "intent": "<1-5 word label: question|command|venting|chitchat|code|search|image|audio>",
  "salience": <0.0-1.0>,
  "affect": "<neutral|anxious|frustrated|curious|urgent|playful>",
  "affect_confidence": <0.0-1.0>,
  "needs_llm": <true|false>,
  "hypothesis": "<predicted full prompt if input looks incomplete, else empty>",
  "entities": ["<key nouns/creds/PII if any>"],
  "pii_risk": <true|false>
}

Rules:
- salience >0.7 if question/command/code/PII/urgent affect.
- salience <0.3 if filler, duplicate, or low-information.
- needs_llm = false only if salience <0.35 AND no question/command.
- Never output reasoning. JSON only. Under 60 tokens.
```

## Appendix B — Handoff Payload (SLM → LLM)

```json
{
  "scrubbed_text": "how do i fix ModuleNotFoundError for torch?",
  "original_salience": 0.82,
  "affect": { "label": "frustrated", "valence": -0.4, "arousal": 0.6, "confidence": 0.78 },
  "hypothesis": {
    "intent": "code debugging",
    "completion": "how do i fix ModuleNotFoundError for torch in my venv?",
    "confidence": 0.71,
    "draft_available": true
  },
  "priority": "P1",
  "suggested_context_window": "focused",
  "scrubbed": true,
  "slm_latency_ms": 22
}
```

## Appendix C — File to Create Next

```
subconscious-state-engine/
├── engine/
│   ├── state.ts              # SubconsciousState + decay
│   ├── gate.ts               # SLM classifier gate
│   ├── beam.ts               # hypothesis beam (extends subconsciousEngine.ts)
│   ├── habits.ts             # HabitEntry registry + promotion
│   ├── arousal.ts            # gain controller
│   └── consolidate.ts        # idle replay → MEMORY.md
├── senses/
│   ├── vision.ts             # Moondream sidecar
│   ├── audio.ts              # Whisper sidecar
│   └── proprioception.ts     # rhythm / focus heuristics
├── prompts/
│   └── sensory-gate.txt      # Appendix A
└── telemetry/
    └── metrics.ts            # wakeRate, hitRate, latency
```

---

*End of document — ready for review and Phase 1 kickoff.*
