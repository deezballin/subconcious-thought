"""
Undermind: universal inline prediction + fast handoff + daydreaming pipeline.

Module map:
1. config.py            - TOML-backed configuration over typed defaults
2. providers/           - pluggable backends (Lemonade/OpenAI-compat, Ollama, webhook)
3. listener.py          - keystroke capture, thread-safe buffer, idle clock
4. confidence.py        - per-token confidence + 95% edge-triggered crossing
5. predictor.py         - streamed draft continuations (DraftPredictor) + legacy beam
6. handoff.py           - UniversalHandoff to the primary LLM context pipeline
7. store.py             - SQLite persistence (inputs, intents, handoffs, exports)
8. intents.py           - lexical normalization to stable intent IDs
9. exporter.py          - data/training_export.jsonl writer with dedupe ledger
10. daydream.py         - idle-triggered background worker
11. main.py             - Pipeline orchestrator + CLI
12. proxy.py            - Ollama-compatible HTTP proxy frontend
"""

__version__ = "0.2.0"
