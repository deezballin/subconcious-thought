# hermes_plugin/ — version-controlled Hermes plugin

The live, installed copy of the bridge plugin lives **outside this repo** at:

    C:\Users\dewayne\AppData\Local\hermes\plugins\undermind\

Hermes loads it from there; the `undermind/__init__.py` in this directory is
the tracked master copy. After editing the repo copy, deploy with:

    cp hermes_plugin/undermind/__init__.py \
       "$APPDATA/../../Local/hermes/plugins/undermind/__init__.py"
    hermes gateway restart

(then watch `hermes logs` for `[undermind] hook registered`.)

## What the plugin does

- **pre_llm_call hook**: records every user message into Undermind's store
  (POST :11435/api/inputs, 1.0s timeout, one retry, fail-open) and injects a
  private context block listing mined intents (min_count=2, max 5) so the
  model sees repeated directions in the user's own words.
- **Per-signature cap (100 chars)**: one giant signature (e.g. the broad
  cron-run intent) must not starve the rest of the 900-char block budget —
  before this cap, only the cron intent survived truncation.
- **Block cap (900 chars) stays**: overall prompt budget is unchanged.

Verify injection end-to-end with a one-shot:

    hermes -z "quote every line of the undermind private context block exactly"

(the "do not mention undermind" directive is the plugin's own; override it in
the prompt when testing, as above).
