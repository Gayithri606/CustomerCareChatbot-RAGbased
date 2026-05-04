"""Embedding cache for the customer-care chatbot.

Caches OpenAI embedding vectors for repeated query text so that
high-frequency questions don't re-hit the embeddings API on every
turn. Backed by Redis with a per-entry TTL.

Design:
- Cache key: `chatbot:embedding:{model}:{sha256(text)}`. Including the
  model name in the key prevents collisions if the embedding model is
  switched — different models produce different vectors.
- TTL from `RetrievalSettings.embedding_cache_ttl_seconds` (default
  86400 = 24h). Refreshed on every set, NOT on every get — a hot key
  will eventually expire and re-embed once. This bounds staleness if
  the embedding model is silently upgraded.
- Encoding: JSON list of floats. Human-debuggable; size penalty vs
  msgpack is negligible at 1536 dims.
- Stampede protection: deferred (Q14 — Phase 8 follow-up). v1
  accepts that under burst load, multiple parallel callers may all
  miss the cache for the same key and all call the embeddings API
  in parallel.
- Feature flag: `OpsSettings.enable_embedding_cache` is honored at
  the call site, NOT inside this class. The class is always
  functional; whether anyone calls it is an operator choice.
- Wiring: standalone in Phase 8. Integration with
  `VectorStore.get_embedding_async` (or the chatbot's retrieval
  tool) is deferred to Phase 9/10.

Usage:
    cache = EmbeddingCache(redis_client, settings.retrieval)
    hit = await cache.get(query_text, model="text-embedding-3-small")
    if hit is not None:
        return hit
    embedding = await openai_client.embeddings.create(...)
    await cache.set(query_text, "text-embedding-3-small", embedding)
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

import redis.asyncio as aioredis

from config.settings import RetrievalSettings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "chatbot:embedding:"


class EmbeddingCache:
    """Async Redis-backed embedding cache.

    Keyed by `(model, sha256(text))` so entries remain correct across
    embedding-model changes. TTL is applied on every set.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        settings: RetrievalSettings,
    ) -> None:
        self._redis = redis_client
        self._ttl = settings.embedding_cache_ttl_seconds

    @staticmethod
    def _key(text: str, model: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{_KEY_PREFIX}{model}:{digest}"

    async def get(self, text: str, model: str) -> Optional[list[float]]:
        """Return the cached embedding for (text, model), or None."""
        raw = await self._redis.get(self._key(text, model))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.exception(
                "embedding_cache_corrupt_payload model=%s", model
            )
            return None

    async def set(
        self,
        text: str,
        model: str,
        embedding: list[float],
    ) -> None:
        """Store an embedding under (model, text) with the configured TTL."""
        payload = json.dumps(embedding)
        await self._redis.set(self._key(text, model), payload, ex=self._ttl)
        logger.debug(
            "embedding_cache_set model=%s dim=%d", model, len(embedding)
        )

    async def delete(self, text: str, model: str) -> None:
        """Remove a single cache entry (mostly for tests / admin)."""
        await self._redis.delete(self._key(text, model))