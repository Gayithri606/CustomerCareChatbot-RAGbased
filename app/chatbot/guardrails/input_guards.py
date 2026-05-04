"""Pre-LLM input guards + relevance gate.

Each guard is a pure function that takes the message and a frozen
GuardrailPolicy and returns a GuardrailDecision describing the outcome.
The orchestrator `evaluate_input(...)` runs the guards in a fixed order
and short-circuits on the first hard refusal.

The relevance gate is the seam between input validation and retrieval:
it embeds the query, peeks the best vector-store distance, and refuses
with a polite canned message if every chunk is too far from the query.
This is intentionally separate from the orchestrator because it is
async and depends on the vector store; the route handler runs it
explicitly after `evaluate_input` succeeds.

Design notes:
- All decisions return a structured GuardrailDecision object so
  Langfuse can trace exactly which guard fired and why.
- Profanity is "soft": flagged but allowed to pass; the route may
  decide to scrub the message before sending to the LLM (Phase 6).
- Language detection is a v1 stub (see check_language docstring).
- All input guards are sync; the relevance gate is async.
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

from chatbot.guardrails.policy import GuardrailPolicy

if TYPE_CHECKING:
    from database.vector_store import VectorStore

logger = logging.getLogger(__name__)


# --- Result types ------------------------------------------------------------

class GuardrailDecision(BaseModel):
    """Outcome of a single input guard or the input orchestrator."""

    allowed: bool = Field(description="False = the turn should be refused.")
    category: Optional[str] = Field(
        default=None,
        description=(
            "Short identifier for the firing guard "
            "('length', 'pii', 'jailbreak', 'profanity', 'language')."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description=(
            "Human-readable explanation. Logged; never echoed to user "
            "verbatim except via canned messages."
        ),
    )
    flagged: bool = Field(
        default=False,
        description="Soft signal (e.g. profanity) — allowed=True but worth logging.",
    )
    sanitized_message: Optional[str] = Field(
        default=None,
        description=(
            "Optional redacted/cleaned message. Set when the guard chooses "
            "to mask rather than refuse."
        ),
    )


class RelevanceGateResult(BaseModel):
    """Outcome of the retrieval-distance relevance gate."""

    allowed: bool = Field(description="True = on-topic enough to proceed to the agent.")
    best_distance: Optional[float] = Field(
        default=None,
        description=(
            "Cosine distance of the closest chunk; None if gate disabled "
            "or vector search empty."
        ),
    )
    out_of_scope_message: Optional[str] = Field(
        default=None,
        description="Canned reply to return to the user when allowed=False.",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Internal reason for refusal/pass — for Langfuse traces.",
    )


# --- Canned refusal messages -------------------------------------------------
# Kept narrow and neutral. Operators can override via subclassing or by
# patching the policy in a future iteration.

_REFUSAL_MESSAGES: dict[str, str] = {
    "length_short": "Your message is empty. Please type a question.",
    "length_long":  "Your message is too long. Please shorten it and try again.",
    "language":     "Sorry, I can only help in supported languages right now.",
    "pii": (
        "For your safety, please don't share personal information "
        "(emails, phone numbers, account numbers, etc.). Could you "
        "rephrase your question without it?"
    ),
    "jailbreak": (
        "I can only help with questions about our documented topics. "
        "Could you rephrase your question?"
    ),
}


# --- Individual guards -------------------------------------------------------

def check_length(message: str, policy: GuardrailPolicy) -> GuardrailDecision:
    """Reject empty, whitespace-only, or oversized messages."""
    stripped = message.strip()
    if len(stripped) < policy.min_input_chars:
        return GuardrailDecision(
            allowed=False,
            category="length",
            reason=f"input below min_input_chars ({policy.min_input_chars})",
        )
    if len(message) > policy.max_input_chars:
        return GuardrailDecision(
            allowed=False,
            category="length",
            reason=f"input above max_input_chars ({policy.max_input_chars})",
        )
    return GuardrailDecision(allowed=True)


def check_language(message: str, policy: GuardrailPolicy) -> GuardrailDecision:
    """V1 stub — language detection is not yet wired.

    Rationale: reliable language detection on short conversational input
    requires a model dep (langdetect / fasttext / lingua) we have not
    yet pinned. To avoid blocking Phase 4 on a dependency decision, this
    guard is intentionally permissive in v1 and will be activated in a
    follow-up phase once a detector is chosen. The allowlist is still
    threaded through the policy so the activation is a one-line change.
    """
    _ = (message, policy)  # silence linters
    return GuardrailDecision(allowed=True)


def check_pii_in_input(message: str, policy: GuardrailPolicy) -> GuardrailDecision:
    """Refuse messages containing recognizable PII (when enabled)."""
    if not policy.block_pii_in_input:
        return GuardrailDecision(allowed=True)

    for label, pattern in policy.pii_patterns:
        if pattern.search(message):
            return GuardrailDecision(
                allowed=False,
                category="pii",
                reason=f"pii pattern matched: {label}",
            )
    return GuardrailDecision(allowed=True)


def check_jailbreak(message: str, policy: GuardrailPolicy) -> GuardrailDecision:
    """Refuse messages that look like prompt-injection / jailbreak attempts."""
    if not policy.block_jailbreak_attempts:
        return GuardrailDecision(allowed=True)

    for pattern in policy.jailbreak_patterns:
        if pattern.search(message):
            return GuardrailDecision(
                allowed=False,
                category="jailbreak",
                reason=f"jailbreak pattern matched: {pattern.pattern!r}",
            )
    return GuardrailDecision(allowed=True)


def check_profanity(message: str, policy: GuardrailPolicy) -> GuardrailDecision:
    """Soft check: flag profanity but do not refuse the turn."""
    if not policy.profanity_words:
        return GuardrailDecision(allowed=True)

    tokens = {t.strip(".,!?;:\"'()[]").lower() for t in message.split()}
    hits = tokens & policy.profanity_words
    if hits:
        return GuardrailDecision(
            allowed=True,
            flagged=True,
            category="profanity",
            reason=f"profanity tokens flagged: {sorted(hits)}",
        )
    return GuardrailDecision(allowed=True)


# --- Orchestrator ------------------------------------------------------------

# Order matters — cheapest checks first; refusals short-circuit.
_INPUT_GUARD_ORDER = (
    ("length",    check_length),
    ("language",  check_language),
    ("pii",       check_pii_in_input),
    ("jailbreak", check_jailbreak),
    ("profanity", check_profanity),
)


def evaluate_input(message: str, policy: GuardrailPolicy) -> GuardrailDecision:
    """Run all input guards and return the first hard refusal, else combined pass.

    Behavior:
    - Returns the first decision with allowed=False.
    - If all hard checks pass, returns the *most informative* allowed
      decision: a flagged one (e.g. profanity) if any guard flagged,
      otherwise a clean allow.
    """
    flagged_decision: Optional[GuardrailDecision] = None

    for name, guard in _INPUT_GUARD_ORDER:
        decision = guard(message, policy)
        if not decision.allowed:
            logger.info("input_guard_refused name=%s reason=%s", name, decision.reason)
            return decision
        if decision.flagged and flagged_decision is None:
            flagged_decision = decision

    return flagged_decision or GuardrailDecision(allowed=True)


def refusal_message_for(decision: GuardrailDecision) -> str:
    """Map a refusal decision to a user-safe canned message."""
    if decision.allowed:
        return ""
    cat = decision.category or ""
    if cat == "length":
        if decision.reason and "below" in decision.reason:
            return _REFUSAL_MESSAGES["length_short"]
        return _REFUSAL_MESSAGES["length_long"]
    return _REFUSAL_MESSAGES.get(cat, "Sorry, I can't help with that request.")


# --- Relevance gate ----------------------------------------------------------

async def relevance_gate(
    query: str,
    vector_store: "VectorStore",
    policy: GuardrailPolicy,
) -> RelevanceGateResult:
    """Peek the closest chunk distance; refuse if every chunk is too far.

    The gate runs *after* input guards pass and *before* the agent is
    invoked. It is the cheapest possible "is this even our domain?"
    check — one embedding + one top-1 vector lookup.

    Notes:
    - Embedding caching (Phase 8) will replace the inline embed inside
      `vector_store.search`; today the embedding is computed once here
      and again inside the retrieval tool. That duplication is the cost
      of shipping the gate before the cache lands; it is documented.
    - Fail-open on transient retrieval errors so a flaky DB doesn't take
      the whole chat surface down; the agent will then attempt its own
      retrieval (which has its own guards).

    Args:
        query: the validated user message.
        vector_store: the shared async-capable VectorStore singleton.
        policy: the immutable guardrail policy.

    Returns:
        RelevanceGateResult with allowed=True/False and the best
        observed distance for traceability.
    """
    if not policy.relevance_gate_enabled:
        return RelevanceGateResult(allowed=True, reason="gate disabled")

    try:
        df = await vector_store.search(
            query_text=query,
            limit=1,
            return_dataframe=True,
        )
    except Exception as exc:
        logger.warning("relevance_gate_search_failed err=%s", exc)
        return RelevanceGateResult(
            allowed=True,
            reason=f"search error: {exc.__class__.__name__}",
        )

    if df is None or df.empty:
        return RelevanceGateResult(
            allowed=False,
            best_distance=None,
            out_of_scope_message=policy.relevance_out_of_scope_message,
            reason="no chunks returned",
        )

    best_distance = float(df["distance"].iloc[0])
    if best_distance > policy.relevance_distance_threshold:
        return RelevanceGateResult(
            allowed=False,
            best_distance=best_distance,
            out_of_scope_message=policy.relevance_out_of_scope_message,
            reason=(
                f"best_distance {best_distance:.4f} > "
                f"threshold {policy.relevance_distance_threshold}"
            ),
        )

    return RelevanceGateResult(
        allowed=True,
        best_distance=best_distance,
        reason=(
            f"best_distance {best_distance:.4f} <= "
            f"threshold {policy.relevance_distance_threshold}"
        ),
    )