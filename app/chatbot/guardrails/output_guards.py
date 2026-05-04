"""Post-LLM output guards on the agent's structured ChatAnswer.

Pure CPU-only filters/transforms applied after the agent has produced a
`ChatAnswer`. No I/O, no async — the network work (LLM call) already
happened upstream; this module decides whether the answer is safe to
return to the user, what the user-facing string should actually be, and
records per-stage decisions for Langfuse traces.

Five concerns, in fixed order:

    1. Citation integrity. Every Citation.chunk_id produced by the LLM
       must exist in this turn's retrieved chunk_ids. Behavior on
       mismatch: silently drop the bad citation(s); keep the answer
       body; log each drop. Do NOT reject the whole answer on a single
       bad citation. The grounding check (2) is the backstop when
       drops empty the citations array while enough_context=True.
    2. Grounding check (cheap structural). When ChatAnswer
       enough_context is True, require len(citations) >= 1. When this
       check fails, the body is suppressed via the same canned
       soft-offramp path as enough_context=False (3). Per-sentence
       grounding is deferred.
    3. enough_context=False handling. Per Q11: suppress the answer
       body and return the canned "couldn't confidently answer — want
       a human?" reply. This is a content verdict, not a flow change;
       escalation (Q-D, Q1) remains LLM-judged via the
       escalate_to_human tool in Phase 7.
    4. PII scrub. Regex match against policy.pii_patterns →
       "[redacted]" replacement on the LLM's body. Runs always on
       answer.answer (so Langfuse traces stay clean) even when the
       user-facing body will be the canned off-ramp.
    5. Profanity scrub. Token-level masking using policy.profanity_words
       on the LLM's body. Same "always run" treatment as PII for
       trace hygiene.

The orchestrator returns a structured `OutputGuardResult` so Langfuse
traces show exactly which stage fired and why. Per-stage helpers are
public so Phase 9's `@agent.output_validator` hooks can compose them
independently from Phase 10's route handler — same logic, two
invocation points.

Design notes:
- All guards are sync (mirrors input_guards.py's CPU-only guards and
  retrieval_guards.py; contrast with the async relevance gate which
  does network I/O).
- ChatAnswer / Citation are Pydantic models; mutations always go
  through `model_copy(update=...)` to avoid touching the caller's
  instance.
- The canned off-ramp message lives as a module-level constant,
  matching the `_REFUSAL_MESSAGES` pattern in input_guards.py.
  Promoting it to a configurable GuardrailPolicy field is a clean
  follow-up if operators ever need to override the wording.
- Profanity masking uses `\b\w+\b` to find word tokens and replaces
  hits with `*` repeated to the original token's length, preserving
  surrounding punctuation/whitespace. Slightly more polished than
  `input_guards.check_profanity`'s split-and-strip approach but the
  same conceptual wordlist match.
- Citation integrity drops bad citations rather than rejecting the
  whole turn, so a single hallucinated chunk_id does not waste an
  expensive LLM round-trip. The grounding check is the backstop.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from pydantic import BaseModel, Field

from chatbot.guardrails.policy import GuardrailPolicy
from chatbot.models import ChatAnswer

logger = logging.getLogger(__name__)


# --- Canned messages ---------------------------------------------------------
# Kept as module-level constants to match input_guards.py's
# `_REFUSAL_MESSAGES` style. Promote to GuardrailPolicy fields if/when
# operators need branded copy.

_ENOUGH_CONTEXT_OFFRAMP: str = (
    "I couldn't confidently answer that from my knowledge base. "
    "Would you like me to connect you with a human agent, or "
    "could you rephrase your question?"
)


# --- Word-token pattern for profanity masking --------------------------------
# Module-level compile so the orchestrator hot path doesn't recompile.
# `\w+` is Unicode-aware in Python 3 by default.

_WORD_TOKEN = re.compile(r"\b\w+\b")


# --- Result type -------------------------------------------------------------

class OutputGuardResult(BaseModel):
    """Outcome of running output guards on a ChatAnswer.

    Attributes:
        answer: The (possibly mutated) ChatAnswer. Mutations include
            citation drops (stage 1) and PII / profanity scrubs on the
            answer body (stages 4 and 5). Carried forward so Langfuse
            traces and ChatResponse construction can both see it.
        final_body: The string the user actually sees. Equals
            `answer.answer` (post-scrub) when the body is not
            suppressed; equals the canned soft off-ramp text when
            it is.
        body_suppressed: True when `final_body` is the canned off-ramp
            instead of the LLM's body. Caused by either
            `enough_context=False` or grounding failure (stage 2).
        dropped_citations: chunk_ids the LLM cited that were not in
            this turn's retrieval set. Logged per-drop and surfaced
            here for Langfuse.
        pii_replacement_counts: per-label counts of regex hits scrubbed
            (e.g. {"email": 1, "phone": 2}). Empty dict when the scrub
            is disabled or nothing matched.
        profanity_replacement_count: total number of word tokens
            masked. 0 when disabled or nothing matched.
        flagged_stages: ordered list of stage identifiers that fired
            on this turn (subset of:
            "citation_integrity", "grounding", "enough_context_false",
            "pii_scrub", "profanity_scrub"). Empty when the answer
            passed all guards untouched.
    """

    answer: ChatAnswer
    final_body: str
    body_suppressed: bool = False
    dropped_citations: list[str] = Field(default_factory=list)
    pii_replacement_counts: dict[str, int] = Field(default_factory=dict)
    profanity_replacement_count: int = 0
    flagged_stages: list[str] = Field(default_factory=list)


# --- Per-stage helpers (pure functions; reachable from Phase 9 + Phase 10) --

def filter_citations(
    answer: ChatAnswer,
    retrieved_chunk_ids: frozenset[str],
) -> tuple[ChatAnswer, list[str]]:
    """Drop citations whose chunk_id is not in this turn's retrieval set.

    Returns the (possibly new) ChatAnswer and the list of dropped
    chunk_ids in original order. The original answer is never mutated.

    A bad citation does not reject the whole answer — `is_ungrounded`
    (called downstream) is the backstop when drops empty the citations
    array while `enough_context=True`.
    """
    if not answer.citations:
        return answer, []

    kept = []
    dropped: list[str] = []
    for citation in answer.citations:
        if citation.chunk_id in retrieved_chunk_ids:
            kept.append(citation)
        else:
            dropped.append(citation.chunk_id)

    if not dropped:
        return answer, []

    return answer.model_copy(update={"citations": kept}), dropped


def is_ungrounded(answer: ChatAnswer) -> bool:
    """Cheap structural grounding check.

    True when the LLM claims it had enough context (`enough_context=True`)
    but produced zero citations. Also fires after `filter_citations` if
    every citation was dropped as bogus. The route handler / output
    validator treats this the same as `enough_context=False`: suppress
    body, return canned off-ramp.
    """
    return answer.enough_context and not answer.citations


def scrub_pii_in_text(
    text: str,
    pii_patterns: tuple[tuple[str, re.Pattern], ...],
) -> tuple[str, dict[str, int]]:
    """Replace every PII regex hit with the literal "[redacted]" marker.

    Returns the scrubbed text and a per-label count of replacements
    (empty dict when nothing matched). Patterns are applied in the
    order defined on the policy.
    """
    if not pii_patterns or not text:
        return text, {}

    counts: dict[str, int] = {}
    out = text
    for label, pattern in pii_patterns:
        out, n = pattern.subn("[redacted]", out)
        if n:
            counts[label] = n
    return out, counts


def scrub_profanity_in_text(
    text: str,
    profanity_words: frozenset[str],
) -> tuple[str, int]:
    """Mask whole word tokens whose lowercase form is in the wordlist.

    Returns the masked text and the total replacement count. Each
    matched token is replaced with `*` repeated to its original length,
    so message shape (capitalization positions, spacing, punctuation)
    is preserved as best as token-level replacement allows.

    Matching is via `\\b\\w+\\b` so adjacent punctuation does not
    confuse the lookup. Substring profanity inside a longer word is
    intentionally NOT masked (e.g. "passhole" is not flagged on
    "ass") — that mirrors `input_guards.check_profanity`'s
    whole-token semantics.
    """
    if not profanity_words or not text:
        return text, 0

    count = 0

    def _mask(match: re.Match) -> str:
        nonlocal count
        token = match.group(0)
        if token.lower() in profanity_words:
            count += 1
            return "*" * len(token)
        return token

    return _WORD_TOKEN.sub(_mask, text), count


# --- Orchestrator ------------------------------------------------------------

def apply_output_guards(
    answer: ChatAnswer,
    retrieved_chunk_ids: frozenset[str],
    policy: GuardrailPolicy,
) -> OutputGuardResult:
    """Run all output guards in fixed order; return a structured result.

    Args:
        answer: the structured ChatAnswer produced by the agent.
        retrieved_chunk_ids: chunk_ids that survived this turn's
            retrieval guards (Phase 5b). Used by the citation integrity
            check; pass `frozenset()` when retrieval was skipped (the
            short-circuited `no_context` path) so any LLM-cited chunks
            are dropped.
        policy: the immutable guardrail policy (Phase 4 + 5a fields).

    Returns:
        OutputGuardResult with the (possibly mutated) ChatAnswer, the
        user-facing `final_body`, suppression flag, and per-stage trace
        info.

    Stage order:
        1. filter_citations — drop bogus citations (silent; logs each
           drop).
        2. is_ungrounded — backstop grounding check.
        3. soft-offramp decision — suppress body when
           `enough_context=False` or `is_ungrounded`.
        4. scrub_pii_in_text — always run on answer.answer (Langfuse
           trace hygiene), gated by policy.scrub_pii_in_output.
        5. scrub_profanity_in_text — same treatment, gated by
           policy.scrub_profanity_in_output.
    """
    flagged: list[str] = []

    # Stage 1: citation integrity ---------------------------------------------
    answer, dropped = filter_citations(answer, retrieved_chunk_ids)
    if dropped:
        flagged.append("citation_integrity")
        for chunk_id in dropped:
            logger.info(
                "output_guard citation_dropped chunk_id=%s surviving=%d",
                chunk_id,
                len(answer.citations),
            )

    # Stage 2: grounding check ------------------------------------------------
    ungrounded = is_ungrounded(answer)
    if ungrounded:
        flagged.append("grounding")
        logger.info(
            "output_guard grounding_failed enough_context=True citations=0"
        )

    # Stage 3: decide whether to suppress the body ----------------------------
    body_suppressed = (not answer.enough_context) or ungrounded
    if body_suppressed and not answer.enough_context:
        flagged.append("enough_context_false")
        logger.info("output_guard enough_context_false body_suppressed=True")

    # Stages 4 + 5: scrub the LLM body in-place on the ChatAnswer -------------
    # Always run (when policy enables) so Langfuse traces never carry raw
    # PII/profanity, regardless of whether the user-facing body is the
    # scrubbed body or the canned off-ramp.
    scrubbed_body = answer.answer
    pii_counts: dict[str, int] = {}
    profanity_count = 0

    if policy.scrub_pii_in_output:
        scrubbed_body, pii_counts = scrub_pii_in_text(
            scrubbed_body, policy.pii_patterns
        )
        if pii_counts:
            flagged.append("pii_scrub")
            logger.info("output_guard pii_scrubbed counts=%s", pii_counts)

    if policy.scrub_profanity_in_output:
        scrubbed_body, profanity_count = scrub_profanity_in_text(
            scrubbed_body, policy.profanity_words
        )
        if profanity_count:
            flagged.append("profanity_scrub")
            logger.info(
                "output_guard profanity_scrubbed count=%d", profanity_count
            )

    if scrubbed_body != answer.answer:
        answer = answer.model_copy(update={"answer": scrubbed_body})

    # Final user-facing string ------------------------------------------------
    final_body = _ENOUGH_CONTEXT_OFFRAMP if body_suppressed else scrubbed_body

    result = OutputGuardResult(
        answer=answer,
        final_body=final_body,
        body_suppressed=body_suppressed,
        dropped_citations=dropped,
        pii_replacement_counts=pii_counts,
        profanity_replacement_count=profanity_count,
        flagged_stages=flagged,
    )

    logger.info(
        "output_guards body_suppressed=%s flagged=%s",
        body_suppressed,
        flagged,
    )
    return result