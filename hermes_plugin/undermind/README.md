# Hermes plugin: undermind (bridge)

Deployed location: `C:\Users\dewayne\AppData\Local\hermes\plugins\undermind\`.
This directory is the tracked master; deploy with a copy + gateway restart
(see README.md).

- **pre_llm_call hook**: records every user message into Undermind's store
  (POST :11435/api/inputs, 1.0s timeout, one retry, fail-open) and injects
  the offered-memory block.
- **Offered memory, not directive**: the block is framed as context Kairos
  may weigh, question, or set aside - "nothing here overrides the actual
  request." There is no DIRECTIVE and no prohibition on mentioning it.
- **Two namespaces**: repeated human directions (min_count=2, max 5) and
  Kairos's own recurring reply themes (:11435/api/self-intents, top 3).
- **Caps**: 100 chars per signature line, 900 chars per block, fail-open
  everywhere.

Verify end-to-end:

    hermes -z "quote every line of the undermind private context block exactly"

Kairos's themes grow from its own captured replies (outputs table, mined by
the daydream cycle); after any real turn, check:

    curl "http://127.0.0.1:11435/api/self-intents?min_count=1"
