---
name: Bug report
about: Something in the pipeline broke or behaved wrong
title: "bug: "
labels: bug
assignees: ""
---

**What happened**
A clear description. Include what you expected vs what occurred.

**Where it showed**
- [ ] proxy (:11435) / bridge turn
- [ ] daydream miner / intents / self-mirror
- [ ] doctor / supervisor / alerts
- [ ] Hermes plugin (injection, recording)
- [ ] free turn
- [ ] tests / CI
- [ ] other

**Evidence**
Paste the relevant lines — handoff rows (`SELECT id, provider, model, status,
latency_ms, think_used, think_reason FROM handoffs ORDER BY id DESC LIMIT 5;`),
doctor output, log tail, or test output. The stack logs a lot on purpose;
share the interesting slice.

**Environment**
- OS / build (Windows 11 host expected)
- Which models are seated (engine + model names only — no config secrets)
- Anything unusual about the machine state (live model running, reboot just happened, NPU under load...)

**Repro steps**
What you did, in order. Include whether a rerun made it go away — we have
one known load-sensitive flake (see README Testing section).

**Willing to help test the fix?**
yes / no / maybe
