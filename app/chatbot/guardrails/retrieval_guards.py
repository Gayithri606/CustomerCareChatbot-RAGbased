"""Post-retrieval, pre-LLM guards on the chunks returned by the vector store.

Pure CPU-only filters/transforms applied to the DataFrame returned by
`VectorStore.search(...)`. No I/O, no async — the network work
(embedding + similarity search) already happened upstream in the
retrieval tool (Phase 7); this module only decides which of those
chunks the LLM is allowed to see and how many tokens of them.

Pipeline (cheapest filters first; each stage is independent and
testable):

    1. Drop chunks with empty/whitespace-only content.
    2. Metadata allowlist (filename, file_type) — gate at the answer.
    3. Distance threshold — drop chunks too far from the query.
    4. Top-k cap — bound breadth.
    5. Token budget — greedy sum until cap; never split a chunk.

The orchestrator returns a structured `RetrievalGuardResult` so
Langfuse traces show exactly which stage dropped what. The
`no_context=True` signal fires when zero chunks survive — the route
handler combines this with `policy.refuse_when_no_context` (Q10/Q11)
to decide whether to short-circuit before calling the LLM.

Design notes:
- All guards are sync (mirrors input_guards.py's CPU-only guards;
  contrast with the async relevance gate which does network I/O).
- Token counting uses tiktoken's `cl100k_base` encoding, which is
  correct for both gpt-4o family and text-embedding-3-small. Encoder
  is module-level lazy-cached.
- Input is a pandas DataFrame to match `VectorStore.search(
  return_dataframe=True)`; output is typed `RetrievedChunk` objects
  for clean downstream consumption (the agent's tool, citation
  integrity in Phase 6).
- Filetype comparison is case-insensitive: metadata stores `.pdf`,
  policy stores `.pdf`, both lowercased.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
from pydantic import BaseModel, Field

from chatbot.guardrails.policy import GuardrailPolicy

logger = logging.getLogger(__name__)


# --- Token encoder (lazy, cached at module level) ---------------------------

_TOKEN_ENCODER = None


def _get_encoder():
    """Lazy-load tiktoken's cl100k_base encoder.

    cl100k_base is the encoding for gpt-4o, gpt-4o-mini, and
    text-embedding-3-small, so a single encoder is correct for both
    the embedding side (already used in chunker.py) and the LLM side.
    """
    global _TOKEN_ENCODER
    if _TOKEN_ENCODER is None:
        import tiktoken
        _TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
    return _TOKEN_ENCODER


def _count_tokens(text: str) -> int:
    """Token count for budget accounting. Falls back to chars/4 if
    tiktoken is unavailable for any reason (it is in requirements.txt,
    so this is purely defensive)."""
    try:
        return len(_get_encoder().encode(text))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("tiktoken unavailable, falling back to char/4 estimate: %s", exc)
        return max(1, len(text) // 4)


# --- Result types ------------------------------------------------------------

class RetrievedChunk(BaseModel):
    """A single chunk that has survived retrieval guards.

    Carries everything the agent's prompt builder + citation validator
    need. `chunk_id` corresponds to `Citation.chunk_id` in the LLM's
    structured output.
    """

    chunk_id: str
    content: str
    distance: float
    filename: Optional[str] = None
    file_type: Optional[str] = None
    token_count: int = Field(
        description="tiktoken cl100k_base length of `content`."
    )


class RetrievalGuardResult(BaseModel):
    """Outcome of running retrieval guards on a search-result frame."""

    chunks: list[RetrievedChunk] = Field(
        default_factory=list,
        description="Surviving chunks, ordered by ascending distance.",
    )
    no_context: bool = Field(
        description=(
            "True when zero chunks survive. The route handler combines "
            "this with policy.refuse_when_no_context to short-circuit "
            "before invoking the LLM."
        ),
    )
    total_context_tokens: int = Field(
        default=0,
        description="Sum of token_count across surviving chunks.",
    )
    dropped_counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-stage drop counts for Langfuse traces. Keys: "
            "'empty', 'metadata', 'distance', 'top_k', 'token_budget'."
        ),
    )


# --- Per-stage guards (pure functions on lists of RetrievedChunk) -----------

def _frame_to_chunks(df: pd.DataFrame) -> list[RetrievedChunk]:
    """Project the DataFrame returned by VectorStore.search into typed chunks.

    Tolerates missing optional metadata columns (filename / file_type)
    by defaulting to None. token_count is computed once here and
    carried for the budget pass.
    """
    if df is None or df.empty:
        return []

    chunks: list[RetrievedChunk] = []
    for _, row in df.iterrows():
        content = row.get("content") or ""
        chunks.append(
            RetrievedChunk(
                chunk_id=str(row.get("id", "")),
                content=content,
                distance=float(row.get("distance", float("inf"))),
                filename=row.get("filename"),
                file_type=row.get("file_type"),
                token_count=_count_tokens(content),
            )
        )
    return chunks


def _drop_empty(chunks: list[RetrievedChunk]) -> tuple[list[RetrievedChunk], int]:
    kept = [c for c in chunks if c.content and c.content.strip()]
    return kept, len(chunks) - len(kept)


def _filter_by_metadata(
    chunks: list[RetrievedChunk],
    filename_allow: Optional[frozenset[str]],
    filetype_allow: frozenset[str],
) -> tuple[list[RetrievedChunk], int]:
    """Apply filename + filetype allowlists.

    - filename_allow=None means "no scoping" (the v1 default).
    - filetype_allow is always active; metadata file_type is compared
      case-insensitively. Chunks missing file_type metadata are
      dropped when the allowlist is active (conservative).
    """
    kept: list[RetrievedChunk] = []
    for c in chunks:
        if filename_allow is not None:
            if c.filename is None or c.filename not in filename_allow:
                continue
        if filetype_allow:
            ft = (c.file_type or "").lower()
            if ft not in filetype_allow:
                continue
        kept.append(c)
    return kept, len(chunks) - len(kept)


def _filter_by_distance(
    chunks: list[RetrievedChunk],
    threshold: float,
) -> tuple[list[RetrievedChunk], int]:
    kept = [c for c in chunks if c.distance <= threshold]
    return kept, len(chunks) - len(kept)


def _apply_top_k(
    chunks: list[RetrievedChunk],
    k: int,
) -> tuple[list[RetrievedChunk], int]:
    if k <= 0 or len(chunks) <= k:
        return chunks, 0
    return chunks[:k], len(chunks) - k


def _apply_token_budget(
    chunks: list[RetrievedChunk],
    max_tokens: int,
) -> tuple[list[RetrievedChunk], int, int]:
    """Greedy cumulative-sum cap; never splits a chunk.

    Returns (kept, dropped_count, total_tokens).
    """
    kept: list[RetrievedChunk] = []
    used = 0
    for c in chunks:
        if used + c.token_count > max_tokens:
            break
        kept.append(c)
        used += c.token_count
    return kept, len(chunks) - len(kept), used


# --- Orchestrator ------------------------------------------------------------

def apply_retrieval_guards(
    df: pd.DataFrame,
    policy: GuardrailPolicy,
) -> RetrievalGuardResult:
    """Run all retrieval guards in fixed order; return a structured result.

    Args:
        df: DataFrame as returned by `VectorStore.search(return_dataframe=True)`.
            Expected columns: id, content, distance, plus expanded metadata
            (filename, file_type, ...). Missing optional columns are tolerated.
        policy: the immutable guardrail policy (Phase 4 + Phase 5a fields).

    Returns:
        RetrievalGuardResult with surviving chunks, `no_context` flag,
        running token total, and per-stage drop counts.
    """
    dropped: dict[str, int] = {}

    chunks = _frame_to_chunks(df)
    # The vector store returns rows in ascending-distance order already,
    # but we re-sort to be explicit and resilient to upstream changes.
    chunks.sort(key=lambda c: c.distance)

    chunks, dropped["empty"] = _drop_empty(chunks)
    chunks, dropped["metadata"] = _filter_by_metadata(
        chunks,
        policy.retrieval_metadata_filename_allowlist,
        policy.retrieval_metadata_filetype_allowlist,
    )
    chunks, dropped["distance"] = _filter_by_distance(
        chunks, policy.relevance_distance_threshold
    )
    chunks, dropped["top_k"] = _apply_top_k(chunks, policy.retrieval_top_k)
    chunks, dropped["token_budget"], total_tokens = _apply_token_budget(
        chunks, policy.retrieval_max_context_tokens
    )

    result = RetrievalGuardResult(
        chunks=chunks,
        no_context=(len(chunks) == 0),
        total_context_tokens=total_tokens,
        dropped_counts=dropped,
    )

    logger.info(
        "retrieval_guards survived=%d total_tokens=%d dropped=%s",
        len(chunks),
        total_tokens,
        dropped,
    )
    return result