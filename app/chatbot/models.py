"""Pydantic schemas for the customer-care chatbot.

Defines the wire format for the /chat endpoint (ChatRequest, ChatResponse),
the LLM's structured output (ChatAnswer), and the Citation primitive.

ChatAnswer intentionally mirrors the existing SynthesizedResponse pattern
in app/services/synthesizer.py: the LLM is responsible for setting
`enough_context: bool` honestly as part of structured output. The chatbot's
system prompt (Phase 7) carries forward the same "be transparent when
context is insufficient" instruction that Synthesizer.SYSTEM_PROMPT uses.

Note on separation of concerns:
- This module is structural only. PII scrubbing, citation integrity,
  grounding checks, and other policy enforcement live in
  app/chatbot/guardrails/output_guards.py and the agent's output
  validators (Phase 6 / Phase 9).
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class Citation(BaseModel):
    """A single chunk reference cited in an answer.

    The LLM is required to populate `chunk_id` (it sees chunk IDs in its
    retrieved context). `source` and `score` are optional metadata that
    the route handler backfills from the retrieval result before returning
    to the client — they are not produced by the LLM.
    """

    chunk_id: str = Field(
        description="ID of the retrieved chunk this citation references."
    )
    source: Optional[str] = Field(
        default=None,
        description="Human-readable source (e.g., filename). Backfilled by route.",
    )
    score: Optional[float] = Field(
        default=None,
        description="Cosine distance from the user query, for traceability.",
    )


class ChatAnswer(BaseModel):
    """The LLM's structured output for a single turn.

    Mirrors SynthesizedResponse:
      - thought_process: kept for Langfuse traces, stripped from user response.
      - answer: free-text reply.
      - enough_context: LLM's self-assessment of context sufficiency.
    Adds:
      - citations: chunks the answer is grounded in (chunk_id required;
        source/score backfilled by route).
      - needs_human: True when the LLM judges the question requires
        human handoff (typically set after escalate_to_human tool is called).
    """

    thought_process: List[str] = Field(
        default_factory=list,
        description=(
            "Internal reasoning steps. Logged to Langfuse, "
            "not returned to clients."
        ),
    )
    answer: str = Field(
        description="The synthesized answer to the user's question.",
    )
    citations: List[Citation] = Field(
        default_factory=list,
        description="Chunks the answer is grounded in. Empty when enough_context=False.",
    )
    enough_context: bool = Field(
        description="LLM's judgment: did retrieved context suffice to answer?",
    )
    needs_human: bool = Field(
        default=False,
        description="True when the LLM has decided this question should be escalated.",
    )


class ChatRequest(BaseModel):
    """Inbound payload for POST /chat."""

    session_id: str = Field(
        description="UUID identifying the conversation session.",
    )
    message: str = Field(
        description="The user's message for this turn.",
    )

    @field_validator("session_id")
    @classmethod
    def _validate_session_id_is_uuid(cls, v: str) -> str:
        try:
            UUID(v)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("session_id must be a valid UUID")
        return v

    @field_validator("message")
    @classmethod
    def _validate_message_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message must not be empty or whitespace-only")
        return v


class ChatResponse(BaseModel):
    """Outbound payload from POST /chat.

    Flattens ChatAnswer for client ergonomics and adds operational
    metadata. When `refused_reason` is set, `answer` may contain a canned
    guardrail message rather than LLM output, and `citations` may be empty.

    Note: `thought_process` from ChatAnswer is intentionally NOT exposed
    here — it's an internal/traceability concept only.
    """

    session_id: str
    answer: str
    citations: List[Citation] = Field(default_factory=list)
    enough_context: bool
    needs_human: bool = False
    refused_reason: Optional[str] = Field(
        default=None,
        description=(
            "Set when input/relevance/output guardrails refused the turn. "
            "Null on normal LLM-answered turns."
        ),
    )
