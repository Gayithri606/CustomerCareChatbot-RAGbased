"""Guardrails subsystem for the customer-care chatbot.

Layered defenses driven by GuardrailSettings + RetrievalSettings:

- policy.py        — compiled patterns, wordlists, and thresholds (read-only).
- input_guards.py  — pre-LLM checks (length, language, PII, jailbreak,
                     profanity) plus the retrieval-distance relevance gate.
- retrieval_guards.py (Phase 5) — distance/top-k/token-budget/metadata gates.
- output_guards.py    (Phase 6) — citation integrity, grounding, PII scrub.

All modules consume a single immutable GuardrailPolicy built once at app
startup from the typed settings objects. See
docs/customer-care-chatbot-design-notes.md for the full strategy.
"""