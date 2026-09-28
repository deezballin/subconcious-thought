# RESUME: Adversary build (paused 2026-09-27, mid-implementation)

Read this file first. It is the complete resume plan after the session was
stopped mid-build due to edit-channel corruption. The design doc
(`docs/ADVERSARY_DESIGN.md`) and everything before it is committed and pushed;
this file plus the notes below carry everything else.

## Dewayne's five decisions (2026-09-27, recorded — do not re-ask)

1. The 4B critic reads user turns locally: **approved** (all on-box, nothing
   leaves the machine).
2. **Watch-only, no rewrites.** The rewrite/revision path from the design doc
   is DESCOPED — never build a revision pass. The critic records opinions
   only; replies are never touched, blocked, or gated.
3. Kairos sees his own critique patterns **right away** (plugin section), not
   deferred to after a shadow week.
4. Values framing settled: retrospective review by Kairos himself — the
   critic's notes become offered memory so he can improve or disagree.
5. Critique rows (drafts he almost shipped, and why) **stay local**, same
   handling as the free-turn log.

## File state at pause (verified)

| File | State |
|---|---|
| `src/undermind/config.py` | DONE + verified. `[adversary]` AdversaryConfig (mode off, openai_compat, Bonsai-4B-Q1_0 @ :13305, timeout 20s, health_cache 60s, min/max draft chars). In `_SECTION_TYPES`. 209 suite green with it. |
| `src/undermind/store.py` | DONE + verified via git diff. `adversarial_critiques` table (handoff_id, ts_ns, mode, verdict, category, issue, draft_text, critic_latency_ms, mined) + `record_critique`, `critique_stats(window)`, `recent_issues(limit)`. `count_handoffs` intact. |
| `src/undermind/adversary.py` | DONE (new file). `extract_verdict` (strict-JSON contract, fail-open → None; parser smoke 6/6), `build_brief`, `Adversary` (enabled ⇔ mode=="shadow"; `_endpoint_up` cached TCP probe; `review_async` daemon thread; `review` never raises; `_run_critic` wall-capped call, client injected for tests). |
| `src/undermind/proxy.py` | **BROKEN — partial wiring.** Has: import, meta passthrough (think_used/think_reason/adversary), return-dict keys referencing `adversary_meta`, `handoff_id =` capture, an adversary block calling `adversary.review_async` (uses `prompt` — correct). MISSING/possibly mangled: `adversary_meta` definition order, ProxyServer `self.adversary`, factory closure passthrough, health block, notes endpoint. **ACTION: `git restore src/undermind/proxy.py` and redo from fresh reads.** |
| `hermes_plugin/undermind/__init__.py` | Untouched. Needs the "critic notes" section. |
| Tests | Untouched. `test_adversary.py` does not exist yet. |
| Live stack | Unaffected: running proxy is the OLD code (Kairos unaffected; free turn fires normally). |

## Resume steps (in order)

1. `git restore src/undermind/proxy.py` — discards only the broken partial
   wiring; config/store/adversary stay as-is.
2. Re-read each proxy region immediately before editing it. Wiring list,
   ONE single-anchor edit per tool call, verify (`py_compile` + targeted
   test) after every single edit:
   a. `from undermind.adversary import Adversary` (import block).
   b. In `_execute_branch`: capture `handoff_id = self.store.record_handoff(...)`.
   c. After the `record_output` try/except, BEFORE the return dict:
      `adversary_meta = None`; `adversary = getattr(self, "adversary", None)`;
      if adversary is not None and adversary.enabled and think_used:
      `adversary.review_async(prompt, result, handoff_id)` (local `prompt`
      IS the user's ask — chat routes already extracted it) and set
      `adversary_meta = "review_started"`.
   d. Return dict gains: `"think_used"`, `"think_reason"`, `"adversary"`.
   e. `_send_completion` meta passthrough (think/adversary keys) — optional,
      include for observability.
   f. `ProxyServer.__init__` (near `recent_serving`):
      `self.adversary = Adversary(store=self.store, config=self.config)`.
   g. `_handler_factory`: `_adversary = self.adversary` + handler class
      attribute `adversary = _adversary` (same pattern as `memory` — omitting
      this is the known bug class that 500s handlers).
   h. `_handle_health`: add `"adversary": {"mode": self.config.adversary.mode,
      "stats": self.store.critique_stats(30)}`.
   i. GET `/api/adversary-notes` route → `{"notes": [{"category", "issue",
      "ts_ns"} ...from store.recent_issues(3)]}`.
3. Plugin (`hermes_plugin/undermind/__init__.py`): `_get_notes()` fetching
   `/api/adversary-notes` (short timeout, fail-open → []), third
   offered-memory section: "Your critic's notes - recent flaws your own
   critic flagged (weigh, question, or ignore):" with `- {category}: {issue}`
   lines, wired into `_build_context` + the `_on_pre_llm_call` empty-gate.
   Deploy = copy to the Hermes plugins dir + gateway restart.
4. Tests `src/undermind/test_adversary.py`: parser matrix (bare/embedded/
   empty/garbage/bad-category/no-issue/long-issue-truncated), brief
   truncation, trigger matrix (mode off → zero calls; shadow + think →
   called; shadow + routine → no call; min_draft_chars boundary; endpoint
   down → no call, no row), fail-open (client raises/timeouts → SKIP row),
   store round-trips, notes endpoint shape, health counts. Suite green ×3:
   `uv run python -m unittest discover -s src/undermind -p "test_*.py"`
   (209 green at pause; expect ~225+ after).
5. Deploy live: commit + push; backup `config.toml` then set
   `[adversary] mode = "shadow"`; restart proxy (find PID via
   `netstat -ano | findstr :11435`, `taskkill //PID n //T //F`, relaunch via
   the supervisor VBS) and verify `/api/health` shows the adversary block;
   one real deep turn → confirm a row lands in `adversarial_critiques`.
6. Docs: PLAN.md item (12); append the descope note (decision 2) to
   `docs/ADVERSARY_DESIGN.md`; commit + push.

## Discipline rules for the fresh session (why this file exists)

- ONE `str_replace` replacement per tool call. Never two.
- Fresh read of the exact region immediately before every edit.
- Verify after every single edit: `py_compile` at minimum, `git diff` when
  anything feels off. A tool "success" message is not verification.
- Whole-file `write_file` for any new file (no anchors at all).
- At the first contradictory tool output: stop, re-run one deterministic
  command (`git diff` / `git status`), and re-derive state from that before
  continuing. Never continue on momentum.

## Live-stack facts

Proxy :11435 (old code running, self-guarding) · Ollama :11434 (bonsai-27b)
· Lemonade :13305 (Bonsai-1.7B draft + Bonsai-4B critic seat) · dashboard
:9119 · Hermes gateway running as a service · test ports 11440+ · identity
prompt + semantic gate + echoes all live · last commit at pause: `b5d0c0d`.
