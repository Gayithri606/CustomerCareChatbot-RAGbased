"""Per-turn dependencies injected into the Pydantic AI agent.

Carries the shared VectorStore, the immutable GuardrailPolicy, and
per-turn state (session_id, user_id, retrieved_chunk_ids). The instance
is constructed in the /chat route handler (Phase 10) and passed to
`agent.run(..., deps=...)`. Every tool receives it via
`RunContext[ChatDeps]`.

Design:
- Unfrozen dataclass: the retrieval tool writes back the surviving
  chunk IDs after running retrieval guards so the output validator
  (Phase 9) and the route handler (Phase 10) can both see them. A
  frozen container would force tools to thread the IDs through every
  return value, which adds noise without buying anything — the policy
  itself is already immutable, which is where the safety value lives.
- `retrieved_chunk_ids` is typed `frozenset[str]` to match the
  input contract of `apply_output_guards(retrieved_chunk_ids=...)`
  exactly. The frozenset prevents accidental in-place mutation;
  tools always replace the field rather than mutate it.
- `policy` lives on the deps rather than as a module-level global so
  tests can override it per-turn and tools never reach for module
  state (working rule 7: no globals for tool-injected state).
- `slots=True` is belt-and-suspenders: prevents accidental field
  creation on this hot-path object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from chatbot.guardrails.policy import GuardrailPolicy
from database.vector_store import VectorStore


@dataclass(slots=True)
class ChatDeps:
    """Per-turn dependencies injected via `RunContext[ChatDeps]`.

    Attributes:
        vector_store: shared async-capable VectorStore singleton.
        policy: immutable guardrail policy (built once at startup via
            `GuardrailPolicy.from_settings(...)`).
        session_id: UUID string identifying the conversation session.
            Used for Langfuse tagging and any future history lookup.
        user_id: optional opaque user identifier. None in v1 (no auth).
        retrieved_chunk_ids: chunk IDs that survived this turn's
            retrieval guards, written back by `retrieve_knowledge`.
            Consumed by `apply_output_guards` for the citation-integrity
            check. Defaults to an empty frozenset so the no-retrieval
            path (out-of-scope short-circuit) still has a valid value.
    """

    vector_store: VectorStore
    policy: GuardrailPolicy
    session_id: str
    user_id: Optional[str] = None
    retrieved_chunk_ids: frozenset[str] = field(default_factory=frozenset)