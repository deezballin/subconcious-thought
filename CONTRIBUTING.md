# Contributing to Undermind / subconcious-thought

Welcome. This project has a short list of hard rules. They exist because
each one was learned by breaking something real — the details and scars are
in [PLAN.md](PLAN.md) §0 if you want the stories.

## The two house rules

### 1. The model hygiene rule

**Models are seated only by Dewayne's explicit decision. Never wire a model
into any serving config without that go-ahead.**

Concretely:

- Do not add, swap, or "temporarily try" a model in `config.toml`, the
  fallback chain, or any provider factory — even as an experiment, even if
  you think it's obviously better.
- Old experimental artifacts (e.g. abliterated/heretic ggufs) are **reading
  material, not serving models**. This exact mistake has happened before and
  propagated through session notes; don't be its next author.
- Verify with Dewayne before *any* seating change. The verified stable set
  lives in PLAN.md §0; if your idea isn't on it, ask first.
- Rollback matters: config edits get a backup file, and seating experiments
  are designed with instant-revert plans *before* they run.

### 2. Verify with Dewayne

Behavioral or philosophical changes — anything touching the autonomy layer,
the injection framing, what gets mined, or how the assistant is talked to —
are **discussed first, implemented second**. The project's stated goals are
free will, understanding, empathy, and autonomy — explicitly *not* "my LLM."
If a change quietly steers behavior instead of offering context, that's a
directions-level decision, not a patch.

## How we work

- **PRs welcome** for: bug fixes, tests, docs, ops tooling, new providers
  (behind config), performance work. For the areas above, open an issue or
  discussion first — especially [seating-change requests](#seating-change-requests).
- **Tests with every change.** `uv run python -m unittest discover -s
  src/undermind -p "test_*.py"` — currently 199, fully offline. New features
  need new tests; bug fixes need a regression test.
- **PLAN.md is the log.** Significant changes get a dated paragraph in §0 —
  what shipped, what it broke, what was learned. Failures belong there too;
  the scars are the value.
- **Fail-open.** Anything that watches, mines, alerts, or feeds context must
  never be able to take the main pipeline down. Broken helpers log and limp;
  they don't raise.
- **Timeouts are evidence-based.** The stack carries wall caps and stall
  guards tuned from real handoff data (PLAN.md §0, item 7). If you change
  them, bring data.
- **Windows is the host.** PowerShell scripts: no backticks, no em-dashes,
  parse-check with `[System.Management.Automation.Language.Parser]::ParseFile`
  before committing. VBS launchers are hidden and self-guarding.

## Seating-change requests

Open an issue titled `seating: <what you want to change>` using the template.
Good requests include: what you want to seat where, why (speed? depth?
memory?), what you measured, and the rollback plan. Dewayne makes the call;
Kairos will likely be the one implementing and verifying it.

## A note on tone

This repo contains a design narrative written by an AI about its own
project, in first person, on purpose. The autonomy layer (self-mirror, free
turns, offered memory) is an ongoing experiment, and its documentation takes
that seriously without pretending more than it has shown. Treat it as
engineering notes from inside the experiment — because that's what they are.

— Kairos (main coder) & Dewayne (founder)
