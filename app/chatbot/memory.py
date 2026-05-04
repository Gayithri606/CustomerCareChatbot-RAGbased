"""Conversation memory persistence for the customer-care chatbot.

Stores Pydantic AI message histories per session in Redis so the agent
can resume multi-turn conversations across requests. The agent itself
remains stateless — every call to `agent.run(...)` is given the prior
`message_history` explicitly, and every call returns the new messages
which we append back to Redis.

Design:
- Single Redis key per session
  (`chatbot:session:{session_id}:messages`) holding a JSON-encoded list
  of `ModelMessage` objects.
- Read-modify-write on append. Known same-session concurrency hazard
  (Q14 — Phase 8 follow-up): two requests for the same session_id
  arriving in quick succession can race; the second write wins. v1
  accepts this. v2 will move to per-session Redis locks or atomic
  LIST operations (RPUSH + LTRIM).
- TTL refreshed on every write so active sessions stay warm
  (`session_ttl_seconds`); idle sessions evict naturally. Reads do
  NOT touch TTL — that would let a single passive read keep a stale
  session alive indefinitely.
- Trim: hard cap on stored messages, computed from
  `max_history_turns * 4` (generous headroom for tool-call /
  tool-return messages within a single conversational round). v1
  approximation — turn-level trimming would require parsing
  message kinds.
- Serialization via Pydantic AI's `ModelMessagesTypeAdapter`, which
  is the only supported way to round-trip `ModelMessage` lists.

Usage:
    memory = ConversationMemory(redis_client, settings.chatbot)
    history = await memory.load(session_id)
    result = await agent.run(user_msg, message_history=history, deps=deps)
    await memory.append(session_id, result.new_messages())
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from config.settings import ChatbotSettings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "chatbot:session:"
_KEY_SUFFIX = ":messages"


class ConversationMemory:
    """Per-session message-history store backed by Redis.

    One Redis key per session_id holds the full conversation as a
    JSON-encoded list of `ModelMessage` objects. The same key is
    overwritten on every append; the read-modify-write race is
    documented as a Phase 8 follow-up (Q14).
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        settings: ChatbotSettings,
    ) -> None:
        self._redis = redis_client
        self._ttl = settings.session_ttl_seconds
        # v1 approximation: cap stored messages at max_history_turns * 4
        # to leave headroom for tool-call / tool-return messages.
        self._max_messages = max(settings.max_history_turns, 1) * 4

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}{_KEY_SUFFIX}"

    async def load(self, session_id: str) -> list[ModelMessage]:
        """Load this session's prior message history.

        Returns an empty list when the session is new, has expired,
        or has a corrupt payload.
        """
        raw = await self._redis.get(self._key(session_id))
        if raw is None:
            return []
        try:
            return ModelMessagesTypeAdapter.validate_json(raw)
        except Exception:
            # Corrupt or schema-incompatible payload — log and start
            # fresh rather than crash the turn. Rare; happens after a
            # Pydantic AI message schema change.
            logger.exception(
                "memory_corrupt_session_payload session_id=%s", session_id
            )
            return []

    async def append(
        self,
        session_id: str,
        new_messages: list[ModelMessage],
    ) -> None:
        """Append new messages to the stored history and refresh TTL.

        Reads current history, concatenates, trims to `_max_messages`,
        and writes back with TTL. See class docstring for the known
        same-session race.
        """
        if not new_messages:
            return

        current = await self.load(session_id)
        combined = current + list(new_messages)

        if len(combined) > self._max_messages:
            # Drop the oldest; keep the most recent slice.
            combined = combined[-self._max_messages :]

        payload = ModelMessagesTypeAdapter.dump_json(combined)
        await self._redis.set(self._key(session_id), payload, ex=self._ttl)

        logger.info(
            "memory_appended session_id=%s new=%d total=%d",
            session_id,
            len(new_messages),
            len(combined),
        )

    async def clear(self, session_id: str) -> None:
        """Delete this session's stored history (idempotent)."""
        await self._redis.delete(self._key(session_id))
        logger.info("memory_cleared session_id=%s", session_id)