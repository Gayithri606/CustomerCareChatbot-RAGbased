"""Pydantic AI Agent for the customer-care chatbot.

Single-file wiring of the Agent instance and its output validator. All
per-request state (vector_store, policy, session_id, retrieved_chunk_ids)
flows through ChatDeps / RunContext — no module-level globals hold
request-scoped data (working rule 7).

Design decisions captured here (Phase 9):

Decision A2 — Grounding failure: one ModelRetry, then UnexpectedModelBehavior.
    The output validator raises ModelRetry when the LLM sets
    enough_context=True but emits no citations that exist in the retrieved
    set. This is a structural slip (forgot to cite, not a semantic miss).
    Agent(retries=1) gives the LLM exactly one retry with a targeted message.
    If the retry also fails, Pydantic AI raises UnexpectedModelBehavior;
    the Phase 10 route handler converts it to a safe canned response.
    When enough_context=False the validator does NOT retry (the LLM made
    an honest judgment) — it applies the canned soft off-ramp directly.

Decision B3 — Keyword escalation detection: deferred to Phase 10.
    Deterministic "human"/"agent"/"representative" keyword triggers (Q-D)
    are pre-LLM checks that short-circuit before agent.run(). They belong
    in the route handler's pre-processing pipeline alongside evaluate_input()
    and relevance_gate(), not in this file.

Decision C1 — Module-level singleton Agent.
    The Agent holds no I/O resources (connections, pools). get_settings() is
    lru_cached, so reading at import time is cheap and deterministic. Tests
    override via `agent.override(model=TestModel())`. No FastAPI lifespan
    dependency needed.

Decision D1 — UsageLimits and model_settings: what lives where.
    model_settings are baked into Agent() so every agent.run() call inherits
    temperature and max_tokens without Phase 10 repeating them. UsageLimits
    is not an Agent.__init__ parameter in Pydantic AI — it is per-run. We
    export `default_usage_limits` alongside the agent so Phase 10 imports
    one object per concern rather than re-reading settings in the route.
    Note: Pydantic AI's UsageLimits covers token budgets (request / response
    / total). ChatbotSettings.max_tool_iterations has no direct UsageLimits
    field; the response token budget provides an indirect bound on runaway
    tool loops, and the route handler's request_timeout_seconds is the hard
    wall (Phase 10).
"""

from __future__ import annotations

import logging

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from chatbot.deps import ChatDeps
from chatbot.guardrails.output_guards import apply_output_guards
from chatbot.models import ChatAnswer
from chatbot.prompts import SYSTEM_PROMPT
from chatbot.tools import escalate_to_human, retrieve_knowledge
from config.settings import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings snapshot
# ---------------------------------------------------------------------------
# get_settings() is lru_cached; reading at import time is cheap and
# idempotent. Snapshot only chatbot settings to keep the dependency surface
# minimal — retrieval and guardrail knobs travel via ChatDeps.policy.

_cs = get_settings().chatbot


# ---------------------------------------------------------------------------
# Agent singleton (Decision C1)
# ---------------------------------------------------------------------------

agent: Agent[ChatDeps, ChatAnswer] = Agent(
    # "<provider>:<name>" convention. Default "openai:gpt-4o".
    # Overridable via CHATBOT_MODEL env var.
    model=_cs.model,
    deps_type=ChatDeps,
    output_type=ChatAnswer,
    system_prompt=SYSTEM_PROMPT,
    tools=[retrieve_knowledge, escalate_to_human],
    # Baked in so every agent.run() call inherits them without Phase 10
    # having to pass them explicitly. Decision D1.
    model_settings=ModelSettings(
        temperature=_cs.temperature,
        max_tokens=_cs.max_output_tokens,
    ),
    # retries=1: allows the output validator to raise ModelRetry exactly
    # once (Decision A2). If the retry attempt also fails validation,
    # Pydantic AI raises UnexpectedModelBehavior — caught and converted to
    # a safe canned response by the Phase 10 route handler.
    retries=1,
)


# ---------------------------------------------------------------------------
# Default per-run usage limits (Decision D1)
# ---------------------------------------------------------------------------
# Exported so Phase 10 can pass `usage_limits=default_usage_limits` to
# agent.run() without re-reading settings in the route. Phase 10 may layer
# per-request overrides on top (e.g., tighter limits for throttled users).

default_usage_limits = UsageLimits(
    # Cap the LLM's output token budget per turn. Kept in sync with the
    # model_settings.max_tokens baked into the Agent above.
    response_tokens_limit=_cs.max_output_tokens,
    # request_tokens_limit and total_tokens_limit are left unset here;
    # Phase 10 can add per-session token accounting if needed.
)


# ---------------------------------------------------------------------------
# Output validator (Decision A2)
# ---------------------------------------------------------------------------

@agent.output_validator
async def _validate_and_guard_output(
    ctx: RunContext[ChatDeps],
    answer: ChatAnswer,
) -> ChatAnswer:
    """Apply Phase 6 output guards; raise ModelRetry on grounding failure.

    Runs after every LLM generation attempt, including retries. Delegates
    the full guard pipeline to apply_output_guards (output_guards.py):

        Stage 1  Citation integrity — drop citations whose chunk_id is not
                 in ctx.deps.retrieved_chunk_ids. Logs each drop; does not
                 reject the whole answer on a single bad citation.
        Stage 2  Grounding check — enough_context=True requires ≥ 1
                 surviving citation. Fires ModelRetry when it fails (below).
        Stage 3  Soft off-ramp — suppress the answer body and replace with
                 _ENOUGH_CONTEXT_OFFRAMP when enough_context=False or when
                 grounding failure persists after retries are exhausted.
        Stage 4  PII scrub — regex replacement on answer.answer, always run
                 when policy.scrub_pii_in_output is True (trace hygiene).
        Stage 5  Profanity scrub — token-level masking, same treatment.

    Grounding failure vs. enough_context=False — Decision A2:
        "grounding" fires when enough_context=True but zero valid citations
        remain after stage 1. This is a fixable structural slip (LLM had
        usable context but forgot to populate citations). We raise ModelRetry
        with a targeted message so the LLM can correct it on its one allowed
        retry. We do NOT retry on enough_context=False: the LLM made an
        honest judgment; retrying would waste a call and likely produce the
        same result. The off-ramp is applied immediately in that case.

    Args:
        ctx: RunContext carrying ChatDeps. Uses ctx.deps.retrieved_chunk_ids
             (written by retrieve_knowledge in Phase 7) and ctx.deps.policy
             (immutable GuardrailPolicy built at startup). ctx.deps.session_id
             is used for structured log context only.
        answer: raw ChatAnswer from this LLM generation attempt.

    Returns:
        ChatAnswer with:
          - citations filtered to this turn's retrieved set (stage 1 applied).
          - answer.answer set to result.final_body: the scrubbed LLM body
            when grounded; the canned off-ramp when body_suppressed=True.

    Raises:
        ModelRetry: on grounding failure (stage 2) only. Pydantic AI appends
            the message to the conversation and re-runs the LLM. If the retry
            also fails this validator, Pydantic AI raises
            UnexpectedModelBehavior — caught by the Phase 10 route handler
            which returns a safe canned response and logs the turn to Langfuse.
    """
    result = apply_output_guards(
        answer=answer,
        retrieved_chunk_ids=ctx.deps.retrieved_chunk_ids,
        policy=ctx.deps.policy,
    )

    if result.flagged_stages:
        logger.info(
            "agent.output_validator session_id=%s flagged=%s "
            "body_suppressed=%s dropped_citations=%s",
            ctx.deps.session_id,
            result.flagged_stages,
            result.body_suppressed,
            result.dropped_citations,
        )

    # Decision A2: structural grounding failure → one targeted ModelRetry.
    # Only fires when enough_context=True + zero valid citations after
    # stage 1 filtering. Distinct from enough_context_false: the LLM
    # judged it had enough context but failed to back it up — correctable.
    if "grounding" in result.flagged_stages:
        raise ModelRetry(
            "Your response set enough_context=True but cited no chunks from "
            "the retrieved context (all cited chunk_ids were absent or invalid). "
            "Re-read the context returned by retrieve_knowledge and populate "
            "'citations' with valid chunk_id values from it. "
            "If the context does not support a grounded answer, set "
            "enough_context=False and leave citations empty."
        )

    # Merge all guard transforms into the returned ChatAnswer.
    # result.answer has: filtered citations (stage 1) + scrubbed answer body
    # (stages 4, 5 applied to answer.answer in apply_output_guards).
    # result.final_body is the correct user-facing string: the scrubbed body
    # when grounded, or the canned off-ramp when body_suppressed=True
    # (enough_context=False or ungrounded — both handled in stage 3).
    return result.answer.model_copy(update={"answer": result.final_body})