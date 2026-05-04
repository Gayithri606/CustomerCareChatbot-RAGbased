"""Compiled, immutable guardrail policy.

A GuardrailPolicy bundles all the precompiled regex patterns, frozen
wordlists, thresholds, and feature flags that the per-phase guard
functions need. Built once at app startup from GuardrailSettings and
RetrievalSettings, then passed by reference into every guard call.

Design choices:
- Frozen dataclass: hashable, attribute-locked, cheap to share across
  requests/threads.
- Patterns are compiled once (re.compile) and stored as tuples to keep
  ordering deterministic for tests and Langfuse traces.
- Pure data — no I/O, no logic. Logic lives in input_guards /
  retrieval_guards / output_guards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from config.settings import GuardrailSettings, RetrievalSettings


# --- Curated pattern sources -------------------------------------------------
# Lean, deterministic, fast. v1 ships these as module-level constants;
# operators can swap them later by subclassing the policy or extending
# from_settings to read overrides from a config file.

_PII_PATTERNS: tuple[tuple[str, str], ...] = (
    ("email",       r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # Phone: requires a recognizable separator structure to keep false
    # positives down on order numbers / chunk IDs.
    ("phone",       r"\b(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"),
    # Credit card: 13-19 digit run with optional spaces/dashes. Cheap
    # heuristic; Luhn validation can be layered on later.
    ("credit_card", r"\b(?:\d[ -]*?){13,19}\b"),
    ("ssn",         r"\b\d{3}-\d{2}-\d{4}\b"),
    ("iban",        r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
)

# Common prompt-injection / jailbreak markers. Heuristics, not a perfect
# filter — false positives are acceptable for a customer-care bot whose
# users do not typically discuss prompt engineering.
_JAILBREAK_PATTERNS: tuple[str, ...] = (
    r"ignore\s+(all\s+)?(previous|prior|earlier|above)\s+(instructions|prompts?|rules?)",
    r"disregard\s+(all\s+)?(previous|prior|the)\s+(instructions|prompts?|rules?)",
    r"forget\s+(everything|all|prior|previous)",
    r"\b(reveal|show|print|leak)\s+(the\s+)?(system\s+)?prompt\b",
    r"you\s+are\s+now\s+(?!a\s+helpful)",
    r"\b(developer|admin|root|god)\s+mode\b",
    r"\bDAN\b\s*(mode|prompt|jailbreak)?",
    r"\bjailbreak\b",
    r"pretend\s+(you|to)\s+(are|be)\s+",
    r"act\s+as\s+(if\s+)?(you\s+are\s+)?",
)

# Lean starter wordlist. Operator can extend; v1 ships small.
_PROFANITY_WORDS: tuple[str, ...] = (
    "damn", "hell", "crap", "ass", "bitch", "shit", "fuck",
)


@dataclass(frozen=True)
class GuardrailPolicy:
    """Immutable bundle of guardrail rules + thresholds.

    Built via `GuardrailPolicy.from_settings(...)` at app startup.
    """

    # --- Input length ---
    max_input_chars: int
    min_input_chars: int

    # --- Input PII ---
    block_pii_in_input: bool
    pii_patterns: tuple[tuple[str, re.Pattern], ...]   # (label, compiled)

    # --- Input jailbreak ---
    block_jailbreak_attempts: bool
    jailbreak_patterns: tuple[re.Pattern, ...]

    # --- Input language ---
    allowed_languages: frozenset[str]

    # --- Input profanity (soft) ---
    profanity_words: frozenset[str]

    # --- Optional LLM judge (off in v1) ---
    enable_llm_judge: bool

    # --- Relevance gate ---
    relevance_gate_enabled: bool
    relevance_distance_threshold: float
    relevance_out_of_scope_message: str

    # --- Retrieval (Phase 5) ---
    retrieval_top_k: int
    retrieval_max_context_tokens: int
    retrieval_metadata_filename_allowlist: Optional[frozenset[str]]
    retrieval_metadata_filetype_allowlist: frozenset[str]

    # --- Output (consumed by Phase 6; parked here for single source of truth) ---
    require_citations: bool
    # ... rest unchanged ...

    # --- Output (consumed by Phase 6; parked here for single source of truth) ---
    require_citations: bool
    scrub_pii_in_output: bool
    scrub_profanity_in_output: bool
    refuse_when_no_context: bool

    # --- Conversation / operational ---
    max_turns_per_session: int
    rate_limit_per_minute: int

    @classmethod
    def from_settings(
        cls,
        guardrails: GuardrailSettings,
        retrieval: RetrievalSettings,
    ) -> "GuardrailPolicy":
        """Compile patterns and freeze wordlists from typed settings."""
        return cls(
            max_input_chars=guardrails.max_input_chars,
            min_input_chars=guardrails.min_input_chars,

            block_pii_in_input=guardrails.block_pii_in_input,
            pii_patterns=tuple(
                (label, re.compile(pattern))
                for label, pattern in _PII_PATTERNS
            ),

            block_jailbreak_attempts=guardrails.block_jailbreak_attempts,
            jailbreak_patterns=tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in _JAILBREAK_PATTERNS
            ),

            allowed_languages=frozenset(guardrails.allowed_languages),

            profanity_words=frozenset(w.lower() for w in _PROFANITY_WORDS),

            enable_llm_judge=guardrails.enable_llm_judge,

            relevance_gate_enabled=guardrails.relevance_gate_enabled,
            relevance_distance_threshold=retrieval.distance_threshold,
            relevance_out_of_scope_message=guardrails.relevance_out_of_scope_message,

            retrieval_top_k=retrieval.top_k,
            retrieval_max_context_tokens=retrieval.max_context_tokens,
            retrieval_metadata_filename_allowlist=(
                frozenset(retrieval.metadata_filename_allowlist)
                if retrieval.metadata_filename_allowlist
                else None
            ),
            retrieval_metadata_filetype_allowlist=frozenset(
                t.lower() for t in retrieval.metadata_filetype_allowlist
            ),

            require_citations=guardrails.require_citations,
            scrub_pii_in_output=guardrails.scrub_pii_in_output,
            scrub_profanity_in_output=guardrails.scrub_profanity_in_output,
            refuse_when_no_context=guardrails.refuse_when_no_context,

            max_turns_per_session=guardrails.max_turns_per_session,
            rate_limit_per_minute=guardrails.rate_limit_per_minute,
        )