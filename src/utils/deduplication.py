"""Deduplication logic to prevent re-reporting the same container.

Supports two backends:
  - Redis  (production, shared across restarts)
  - Memory (development / testing)
"""

import hashlib
import logging
import time
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)


class DeduplicationStore:
    """Abstract deduplication interface."""

    def __init__(self, ttl_seconds: int = 300, similarity_threshold: float = 0.85):
        self.ttl = ttl_seconds
        self.sim_threshold = similarity_threshold

    def _make_key(self, iso_number: str) -> str:
        normalized = iso_number.strip().upper().replace(" ", "")
        return f"container:dedup:{normalized}"

    def _is_similar(self, a: str, b: str) -> bool:
        return SequenceMatcher(None, a.upper(), b.upper()).ratio() >= self.sim_threshold

    def is_duplicate(self, iso_number: str) -> bool:
        raise NotImplementedError

    def mark_seen(self, iso_number: str, metadata: Optional[dict] = None):
        raise NotImplementedError


class RedisDeduplicationStore(DeduplicationStore):
    """Redis-backed deduplication."""

    def __init__(self, redis_client, ttl_seconds: int = 300,
                 similarity_threshold: float = 0.85):
        super().__init__(ttl_seconds, similarity_threshold)
        self.redis = redis_client

    def is_duplicate(self, iso_number: str) -> bool:
        key = self._make_key(iso_number)
        if self.redis.exists(key):
            return True
        # Check for similar keys (fuzzy match) among recent entries
        pattern = "container:dedup:*"
        for existing_key in self.redis.scan_iter(match=pattern, count=100):
            existing_number = existing_key.decode().split(":", 2)[-1]
            if self._is_similar(iso_number, existing_number):
                logger.debug(
                    "Fuzzy duplicate found: %s ~ %s", iso_number, existing_number
                )
                return True
        return False

    def mark_seen(self, iso_number: str, metadata: Optional[dict] = None):
        key = self._make_key(iso_number)
        value = iso_number
        if metadata:
            import json
            value = json.dumps({"iso_number": iso_number, **metadata})
        self.redis.setex(key, self.ttl, value)
        logger.info("Marked container as seen: %s (TTL=%ds)", iso_number, self.ttl)


class MemoryDeduplicationStore(DeduplicationStore):
    """In-memory deduplication for dev/testing."""

    def __init__(self, ttl_seconds: int = 300, similarity_threshold: float = 0.85):
        super().__init__(ttl_seconds, similarity_threshold)
        self._store: dict[str, float] = {}  # key -> expiry timestamp

    def _cleanup(self):
        now = time.time()
        expired = [k for k, exp in self._store.items() if exp < now]
        for k in expired:
            del self._store[k]

    def is_duplicate(self, iso_number: str) -> bool:
        self._cleanup()
        key = self._make_key(iso_number)
        if key in self._store:
            return True
        for existing_key in self._store:
            existing_number = existing_key.split(":", 2)[-1]
            if self._is_similar(iso_number, existing_number):
                return True
        return False

    def mark_seen(self, iso_number: str, metadata: Optional[dict] = None):
        self._cleanup()
        key = self._make_key(iso_number)
        self._store[key] = time.time() + self.ttl
        logger.info("Marked container as seen (memory): %s", iso_number)


def create_dedup_store(cfg: dict):
    """Factory: build the appropriate deduplication store from config."""
    backend = cfg.get("backend", "memory")
    ttl = cfg.get("ttl_seconds", 300)
    sim = cfg.get("similarity_threshold", 0.85)

    if backend == "redis":
        import redis as redis_lib
        import os
        client = redis_lib.Redis(
            host=os.environ.get("REDIS_HOST", "redis"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            db=0,
            decode_responses=False,
        )
        client.ping()
        logger.info("Redis deduplication store connected")
        return RedisDeduplicationStore(client, ttl, sim)

    logger.info("Using in-memory deduplication store")
    return MemoryDeduplicationStore(ttl, sim)
