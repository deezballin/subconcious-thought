---
name: Seating change request
about: Propose adding, moving, or tuning a model in the serving chain
title: "seating: "
labels: seating, needs-dewayne
assignees: ""
---

> **House rule (CONTRIBUTING.md #1):** models are seated only by Dewayne's
> explicit decision. This template exists so the request carries its own
> evidence — fill it out and the review is fast.

**What do you want to seat where?**
e.g. "try X model as the fallback rung", "move the 27B onto the NPU stack",
"raise fallback_timeout_s to 90".

**Why — what problem does this solve?**
Speed? Depth? Memory pressure? Latency floor? Be specific about which
turns are hurting today.

**Evidence**
Numbers beat adjectives: handoff latencies, doctor output, p50/p90 from the
`handoffs` table, benchmark runs. If you haven't measured yet, say so —
a measurement plan is acceptable, vibes are not.

**The hygiene check**
- Is this model on the verified-stable list (PLAN.md §0)? yes / no
- If no: where did it come from, and who says it's appropriate for serving?
  (Artifacts of old experiments are reading material, never serving configs.)

**Rollback plan**
How do we get back to today's seating in under a minute if this goes wrong?
(Config backup, alternate config path, provider factory flag...)

**Dewayne's decision**
leave this section for him: APPROVED / DENIED / TRY-THEN-DECIDE, with date.
