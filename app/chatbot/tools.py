"""Tools exposed to the customer-care chatbot agent.

Two tools, both async, both taking `RunContext[ChatDeps]`:

    - `retrieve_knowledge(query)`: embeds the query, runs
      `VectorStore.search`, applies retrieval guards (Phase 5b), and
      returns a formatted context string for the LLM to read.
      Side-effect: writes back the surviving chunk IDs onto
      `ctx.deps.retrieved_chunk_ids` so Phase 6 / Phase 9 output
      guards can validate citations.

    - `escalate_to_human(reason)`: structured escalation signal. Does
      not perform any I/O in v1; returns a short acknowledgement and
      is paired with `ChatAnswer.needs_human=True` set by the LLM.
      The route handler (Phase 10) is responsible for any real
      handoff action (ticket creation, queue notification, etc.).

These are plain async callables; the agent wiring (Phase 8) registers
them via `Agent(..., tools=[retrieve_knowledge, escalate_to_human])`
or per-tool decorators.

Design notes:
- Both tools are async because `VectorStore.search` is async (it
  awaits embedding + similarity search). `escalate_to_human` is
  sync-shaped today but kept async-signatured for symmetry and to
  leave room for a real handoff call later.
- The retrieval tool returns a JSON-formatted string rather than a
  DataFrame so the LLM sees stable, structured context with explicit
  `chunk_id` fields — this is what enables citation grounding to
  work at all.
- "NO_CONTEXT" sentinel: when retrieval returns zero surviving
  chunks, we tell the LLM explicitly so it sets `enough_context=False`
  rather than hallucinating from training data. The route handler
  may also short-circuit before this point (Phase 5b/Phase 10) if
  `policy.refuse_when_no_context` is True.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from chatbot.deps import ChatDeps
from chatbot.guardrails.retrieval_guards import (
    RetrievedChunk,
    apply_retrieval_guards,
)

if TYPE_CHECKING:
    from pydantic_ai import RunContext

logger = logging.getLogger(__name__)


# --- Tool 1: retrieve_knowledge ---------------------------------------------

async def retrieve_knowledge(
    ctx: "RunContext[ChatDeps]",
    query: str,
) -> str:
    """Retrieve and guard knowledge-base chunks most relevant to `query`.

    Steps:
      1. `VectorStore.search(query, return_dataframe=True)` — embeds
         and runs similarity search (network I/O).
      2. `apply_retrieval_guards(df, policy)` — drops empty,
         off-allowlist, too-far, over-top-k, and over-budget chunks.
      3. Writes surviving chunk IDs onto `ctx.deps.retrieved_chunk_ids`
         (replacement, not mutation).
      4. Returns a JSON-formatted string of surviving chunks for the
         LLM to read, or the sentinel "NO_CONTEXT" when none survive.

    Args:
        ctx: Pydantic AI run context carrying `ChatDeps`.
        query: natural-language query to embed and search.

    Returns:
        JSON string with one object per surviving chunk
        ({"chunk_id", "content", "filename", "file_type", "distance"}),
        or the literal string "NO_CONTEXT" when no chunks survive.
        The system prompt instructs the LLM to set
        `enough_context=False` on NO_CONTEXT.
    """
    deps = ctx.deps

    # Use top_k as the search limit. Retrieval guards may drop further
    # chunks via metadata/distance filters, so this is a soft upper
    # bound on what the LLM ultimately sees. Over-fetching to give
    # filters more headroom is a clean follow-up if recall regresses.
    df = await deps.vector_store.search(
        query_text=query,
        limit=max(deps.policy.retrieval_top_k, 1),
        return_dataframe=True,
    )

    result = apply_retrieval_guards(df, deps.policy)

    # Hand the surviving IDs to the output validator / output guards.
    # frozenset(...) replacement keeps the type contract on
    # `ChatDeps.retrieved_chunk_ids` intact.
    deps.retrieved_chunk_ids = frozenset(c.chunk_id for c in result.chunks)

    logger.info(
        "tool_retrieve_knowledge survived=%d total_tokens=%d dropped=%s",
        len(result.chunks),
        result.total_context_tokens,
        result.dropped_counts,
    )

    if result.no_context:
        return "NO_CONTEXT"

    return _format_chunks_for_llm(result.chunks)


# --- Tool 2: escalate_to_human ----------------------------------------------

async def escalate_to_human(
    ctx: "RunContext[ChatDeps]",
    reason: str,
) -> str:
    """Signal that this turn should be handed off to a human agent.

    The LLM should call this when:
      - The user explicitly asks for a human / supervisor / agent.
      - An urgent issue is reported (safety, legal, billing dispute).
      - Documented procedures don't cover the question.

    The tool is intentionally I/O-free in v1 — the actual handoff
    (ticket creation, queue notification) happens in the route
    handler (Phase 10) by inspecting `ChatAnswer.needs_human`.

    Args:
        ctx: Pydantic AI run context carrying `ChatDeps`.
        reason: short, neutral explanation. Logged for traceability;
            not shown verbatim to the user.

    Returns:
        Acknowledgement string the agent can incorporate into its
        final answer.
    """
    deps = ctx.deps
    logger.info(
        "tool_escalate_to_human session_id=%s user_id=%s reason=%s",
        deps.session_id,
        deps.user_id,
        reason,
    )
    return (
        "Acknowledged — a human agent will follow up. "
        "Please summarize anything urgent in your next message."
    )


# --- Helpers ----------------------------------------------------------------

def _format_chunks_for_llm(chunks: list[RetrievedChunk]) -> str:
    """Render surviving chunks as a JSON string for the LLM.

    Mirrors `Synthesizer.dataframe_to_json` in shape (one record per
    chunk) but adds `chunk_id` so the LLM can populate
    `Citation.chunk_id` correctly.
    """
    payload = [
        {
            "chunk_id": c.chunk_id,
            "content": c.content,
            "filename": c.filename,
            "file_type": c.file_type,
            "distance": round(c.distance, 4),
        }
        for c in chunks
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)