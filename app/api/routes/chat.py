"""POST /chat — the customer-care chatbot endpoint.

This is the orchestrator for a single conversational turn. Every guard,
store, and agent built in Phases 3-9 exists as an independent module;
this route is the only place that knows the *order* they run in.

Turn pipeline (fail-cheap ordering — each stage is more expensive than
the last, so the earliest possible refusal is always the cheapest):

    1. Schema validation        ChatRequest (UUID session_id, non-empty message)
    2. Input guards             pure regex, no network, no cost
    3. Session memory load      one Redis GET
    4. Conversation cap         max_turns_per_session (Guardrail F)
    4.5 Query condensation      cheap-model rewrite of follow-ups (ADR-001)
    5. Relevance gate           one embed + one top-1 vector lookup
    6. Agent run                the only LLM call; tools + output validator
    7. Escalation override      deterministic keyword trigger (Q-D trigger 1)
    8. Memory append            one Redis SET, TTL refreshed
    9. ChatResponse

Design decisions specific to this file:

Decision E1 — Module-level singletons.
    `vector_store`, `policy`, `redis_client`, and `memory` are constructed
    at import time, mirroring the existing pattern in `query.py`. The
    policy is pure config (no I/O). `redis.asyncio.from_url` is lazy — no
    socket is opened until the first command — so import-time construction
    does not require an event loop. Phase 10b may move these into the
    FastAPI lifespan handler for cleaner shutdown; the route body does not
    change when it does.

Decision E2 — The relevance gate is the `no_context` short-circuit.
    Q-D specifies the route short-circuits on `no_context` before invoking
    the LLM. But `no_context` is produced by `apply_retrieval_guards`,
    which runs *inside* the `retrieve_knowledge` tool — after the agent has
    already started. The relevance gate is the route-level equivalent: it
    uses `policy.relevance_distance_threshold`, which `from_settings` binds
    to the *same* `RetrievalSettings.distance_threshold` the retrieval
    guards use. So a passing gate guarantees at least one chunk survives
    the distance filter downstream. `refuse_when_no_context` is honored
    here as the operator switch it was designed to be.

Decision E3 — Keyword escalation forces the flag, does not skip the agent.
    Per `agent.py` Decision B3, deterministic escalation triggers belong in
    this handler, not in the agent. Per Q-D, they are *additive*: they only
    force `needs_human=True` when the LLM didn't already set it. The
    customer still receives the agent's grounded answer — we escalate *and*
    answer, rather than escalating instead of answering.

Decision E4 — Citation source/score are not backfilled in v1.
    Q-D and `models.py` note that the route backfills `Citation.source` and
    `Citation.score` from the retrieval result. It cannot yet: `ChatDeps`
    carries only `retrieved_chunk_ids` (a frozenset), not the chunk bodies.
    Backfilling requires an additive change to `deps.py` — a separate
    reviewable unit per working rule 3. Citations are returned with
    `chunk_id` only until then. Tracked as a Phase 10 follow-up.

Decision E5 — Hard timeout wraps the whole agent run.
    `ChatbotSettings.request_timeout_seconds` (default 30) is the wall.
    `UsageLimits` bounds tokens and `max_tool_iterations` bounds the tool
    loop indirectly, but neither bounds wall-clock time on a hung network
    call. `asyncio.wait_for` does.
"""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as aioredis
from fastapi import APIRouter
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

from chatbot.agent import agent, default_usage_limits
from chatbot.cache import EmbeddingCache
from chatbot.condenser import condense_query
from chatbot.deps import ChatDeps
from chatbot.guardrails.input_guards import (
    evaluate_input,
    refusal_message_for,
    relevance_gate,
)
from chatbot.guardrails.policy import GuardrailPolicy
from chatbot.memory import ConversationMemory
from chatbot.models import ChatRequest, ChatResponse
from config.settings import get_settings
from database.vector_store import VectorStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# Module-level singletons (Decision E1)
# ---------------------------------------------------------------------------

_settings = get_settings()

# decode_responses=False: ModelMessagesTypeAdapter.validate_json accepts
# bytes, and EmbeddingCache stores JSON. Keeping raw bytes avoids a
# decode/encode round-trip on every turn.
redis_client = aioredis.from_url(_settings.redis.url, decode_responses=False)

# Embedding cache (built Phase 8, wired here). The enable_embedding_cache
# flag is honored at this call site per cache.py's design note: when off,
# VectorStore receives None and behaves exactly as before. Injected into
# VectorStore rather than imported by it — database/ must not import from
# chatbot/ (layering rule, docs/architecture.md).
embedding_cache = (
    EmbeddingCache(redis_client, _settings.retrieval)
    if _settings.ops.enable_embedding_cache
    else None
)

vector_store = VectorStore(embedding_cache=embedding_cache)

policy: GuardrailPolicy = GuardrailPolicy.from_settings(
    guardrails=_settings.guardrails,
    retrieval=_settings.retrieval,
)

memory = ConversationMemory(redis_client, _settings.chatbot)

# ---------------------------------------------------------------------------
# Deterministic escalation keywords (Q-D trigger 1, Decision E3)
# ---------------------------------------------------------------------------
# Module-level constant rather than a policy field, mirroring how
# output_guards.py parks _ENOUGH_CONTEXT_OFFRAMP at module level.
# Promoting this to GuardrailSettings/GuardrailPolicy is queued as a
# follow-up — it is an operator-tunable wordlist, same as the profanity
# list, and belongs beside it once we touch policy.py again.

_ESCALATION_KEYWORDS: frozenset[str] = frozenset(
    {"human", "agent", "representative", "manager", "supervisor", "person"}
)

_SAFE_FALLBACK_MESSAGE: str = (
    "Sorry — I ran into a problem answering that. "
    "Please try rephrasing, or ask to speak with a human agent."
)


def _mentions_human_handoff(message: str) -> bool:
    """True when the user explicitly asks for a human (Q-D trigger 1).

    Word-boundary matching on a lowercased copy. Deliberately simple:
    a substring check would fire on "management" or "agenda". False
    positives here are cheap (we escalate a turn that didn't need it);
    false negatives are expensive (we ignore a customer asking for help).
    """
    tokens = set(message.lower().replace("?", " ").replace(",", " ").split())
    return bool(tokens & _ESCALATION_KEYWORDS)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post("/", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Handle one conversational turn.

    Never raises to the client. Every failure path — guardrail refusal,
    model misbehavior, timeout, unexpected exception — returns a valid
    ChatResponse with `refused_reason` set so callers can distinguish an
    LLM-answered turn from a guarded one. Stack traces are logged, never
    returned (Guardrail G).
    """
    session_id = request.session_id
    message = request.message

    # --- Stage 2: input guards (pure, sync, no network) --------------------
    decision = evaluate_input(message, policy)
    if not decision.allowed:
        logger.info(
            "chat_refused_input session_id=%s category=%s",
            session_id,
            decision.category,
        )
        return ChatResponse(
            session_id=session_id,
            answer=refusal_message_for(decision),
            citations=[],
            enough_context=False,
            needs_human=False,
            refused_reason=f"input_guard:{decision.category}",
        )

    if decision.flagged:
        # Soft signal (e.g. profanity) — allowed through, worth a trace.
        logger.info(
            "chat_input_flagged session_id=%s category=%s",
            session_id,
            decision.category,
        )

    # --- Stage 3: load prior conversation ----------------------------------
    history = await memory.load(session_id)

    # --- Stage 4: conversation cap (Guardrail F) ---------------------------
    # Compared against raw message count, not turns. `max_turns_per_session`
    # is a coarse abuse bound, and ConversationMemory already trims storage
    # at `max_history_turns * 4` using the same message-count approximation.
    # Using the same unit in both places keeps the two caps comparable.
    if len(history) >= policy.max_turns_per_session:
        logger.info(
            "chat_refused_session_cap session_id=%s messages=%d",
            session_id,
            len(history),
        )
        return ChatResponse(
            session_id=session_id,
            answer=(
                "This conversation has reached its length limit. "
                "Please start a new session to continue."
            ),
            citations=[],
            enough_context=False,
            needs_human=True,
            refused_reason="conversation_guard:session_cap",
        )

    
    # --- Stage 4.5: condense follow-ups into standalone queries (ADR-001) --
    # The gate embeds a single message; pronoun follow-ups ("what about
    # ductwork for that?") embed as noise and get wrongly refused. The
    # condenser rewrites them using history (see ADR-004 for why it takes
    # a rendered transcript, not message_history). First turns skip the
    # call. The AGENT still receives the raw message + full history — the
    # condensed form exists only for the gate's embedding.
    condensed_query = message
    if policy.relevance_gate_enabled and history:
        condensed_query = await condense_query(message, history)

    # --- Stage 5: relevance gate (Decision E2, ADR-001) --------------------
    gate = await relevance_gate(condensed_query, vector_store, policy)
    if not gate.allowed and policy.refuse_when_no_context:
        logger.info(
            "chat_refused_relevance session_id=%s best_distance=%s reason=%s",
            session_id,
            gate.best_distance,
            gate.reason,
        )
        return ChatResponse(
            session_id=session_id,
            answer=(
                gate.out_of_scope_message
                or policy.relevance_out_of_scope_message
            ),
            citations=[],
            enough_context=False,
            needs_human=False,
            refused_reason="relevance_gate:out_of_scope",
        )

    if not gate.allowed:
        # Gate said out-of-scope but the operator has refuse_when_no_context
        # off — proceed to the agent, which has its own retrieval guards.
        logger.info(
            "chat_relevance_bypassed session_id=%s best_distance=%s",
            session_id,
            gate.best_distance,
        )

    # --- Stage 6: run the agent --------------------------------------------
    deps = ChatDeps(
        vector_store=vector_store,
        policy=policy,
        session_id=session_id,
        user_id=None,  # no auth in v1
    )

    try:
        result = await asyncio.wait_for(
            agent.run(
                message,
                deps=deps,
                message_history=history,
                usage_limits=default_usage_limits,
            ),
            timeout=_settings.chatbot.request_timeout_seconds,
        )
    except UnexpectedModelBehavior:
        # Output validator raised ModelRetry, the retry also failed
        # validation. Decision A2 in agent.py routes this here.
        logger.warning(
            "chat_model_behavior_failed session_id=%s", session_id, exc_info=True
        )
        return ChatResponse(
            session_id=session_id,
            answer=_SAFE_FALLBACK_MESSAGE,
            citations=[],
            enough_context=False,
            needs_human=True,
            refused_reason="agent:ungrounded_after_retry",
        )
    except UsageLimitExceeded:
        logger.warning("chat_usage_limit session_id=%s", session_id)
        return ChatResponse(
            session_id=session_id,
            answer=_SAFE_FALLBACK_MESSAGE,
            citations=[],
            enough_context=False,
            needs_human=True,
            refused_reason="agent:usage_limit_exceeded",
        )
    except asyncio.TimeoutError:
        logger.warning(
            "chat_timeout session_id=%s seconds=%s",
            session_id,
            _settings.chatbot.request_timeout_seconds,
        )
        return ChatResponse(
            session_id=session_id,
            answer=_SAFE_FALLBACK_MESSAGE,
            citations=[],
            enough_context=False,
            needs_human=True,
            refused_reason="agent:timeout",
        )
    except Exception:
        # Catch-all so no stack trace ever reaches the client (Guardrail G).
        logger.exception("chat_unexpected_error session_id=%s", session_id)
        return ChatResponse(
            session_id=session_id,
            answer=_SAFE_FALLBACK_MESSAGE,
            citations=[],
            enough_context=False,
            needs_human=True,
            refused_reason="agent:unexpected_error",
        )

    answer = result.output

    # --- Stage 7: deterministic escalation override (Decision E3) ----------
    needs_human = answer.needs_human
    if not needs_human and _mentions_human_handoff(message):
        needs_human = True
        logger.info("chat_escalation_forced_by_keyword session_id=%s", session_id)

    # --- Stage 8: persist the turn -----------------------------------------
    # Deliberately after the agent succeeded: a failed turn should not
    # pollute the session history with a half-finished exchange.
    await memory.append(session_id, result.new_messages())

    logger.info(
        "chat_turn_ok session_id=%s enough_context=%s citations=%d needs_human=%s",
        session_id,
        answer.enough_context,
        len(answer.citations),
        needs_human,
    )

    # --- Stage 8.5: backfill citation source/score (ADR-002) ---------------
    # The LLM only sees chunk_ids; filename and distance live on the
    # RetrievedChunk objects the tool wrote onto deps. Surviving citations
    # are guaranteed to be in this turn's set; .get() is belt-and-braces.
    _chunk_by_id = {c.chunk_id: c for c in deps.retrieved_chunks}
    enriched_citations = []
    for citation in answer.citations:
        chunk = _chunk_by_id.get(citation.chunk_id)
        if chunk is not None:
            citation = citation.model_copy(
                update={"source": chunk.filename, "score": chunk.distance}
            )
        enriched_citations.append(citation)

    # --- Stage 9: respond ---------------------------------------------------
    # `answer.answer` is already the guard-processed body: the output
    # validator in agent.py returned `result.final_body`, which is either
    # the scrubbed LLM text or the canned soft off-ramp. `thought_process`
    # is intentionally dropped — trace-only, per models.py.
    return ChatResponse(
        session_id=session_id,
        answer=answer.answer,
        citations=enriched_citations,
        enough_context=answer.enough_context,
        needs_human=needs_human,
        refused_reason=None,
    )
