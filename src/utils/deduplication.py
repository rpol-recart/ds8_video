"""Deduplication logic to prevent re-reporting the same container.

Supports two backends:
  - Redis  (production, shared across restarts)
  - Memory (development / testing)

Fuzzy matching strategy (replacing O(n) full-scan):
  ISO 6346 numbers have the form ABCD1234567 (11 chars).  OCR errors
  typically confuse visually similar characters (O↔0, I↔1, S↔5, B↔8,
  Z↔2, G↔6).  Instead of scanning ALL stored keys, we generate a
  small set of plausible OCR-error variants and check each one — O(1)
  Redis lookups per variant, bounded total.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Characters commonly confused by OCR on container markings
_OCR_CONFUSIONS: dict[str, str] = {
    "O": "0", "0": "O",
    "I": "1", "1": "I",
    "S": "5", "5": "S",
    "B": "8", "8": "B",
    "Z": "2", "2": "Z",
    "G": "6", "6": "G",
    "D": "0",
    "Q": "0",
}


def _normalize(iso_number: str) -> str:
    """Canonical form: uppercase, no spaces/dashes."""
    return iso_number.strip().upper().replace(" ", "").replace("-", "")


def _ocr_variants(iso: str, max_variants: int = 50) -> list[str]:
    """Generate plausible OCR-error variants of an ISO number.

    For each position where a confusion is possible, yield the variant
    with that single character swapped.  Keeps total variants bounded.
    """
    variants = []
    for i, ch in enumerate(iso):
        alt = _OCR_CONFUSIONS.get(ch)
        if alt:
            variant = iso[:i] + alt + iso[i + 1:]
            variants.append(variant)
            if len(variants) >= max_variants:
                break
    return variants


class DeduplicationStore:
    """Abstract deduplication interface."""

    def __init__(self, ttl_seconds: int = 86400, similarity_threshold: float = 0.85):
        self.ttl = ttl_seconds
        self.sim_threshold = similarity_threshold

    @staticmethod
    def _make_key(iso_number: str) -> str:
        return f"container:dedup:{_normalize(iso_number)}"

    def is_duplicate(self, iso_number: str) -> bool:
        raise NotImplementedError

    def mark_seen(self, iso_number: str, metadata: Optional[dict] = None):
        raise NotImplementedError


class RedisDeduplicationStore(DeduplicationStore):
    """Redis-backed deduplication."""

    def __init__(self, redis_client, ttl_seconds: int = 86400,
                 similarity_threshold: float = 0.85):
        super().__init__(ttl_seconds, similarity_threshold)
        self.redis = redis_client

    def is_duplicate(self, iso_number: str) -> bool:
        normalized = _normalize(iso_number)
        key = self._make_key(normalized)

        # Exact match — single O(1) lookup
        if self.redis.exists(key):
            return True

        # Fuzzy match — check OCR-confusion variants (bounded set of O(1) lookups)
        for variant in _ocr_variants(normalized):
            variant_key = f"container:dedup:{variant}"
            if self.redis.exists(variant_key):
                logger.debug("Fuzzy duplicate found: %s ~ %s", iso_number, variant)
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

    def __init__(self, ttl_seconds: int = 86400, similarity_threshold: float = 0.85):
        super().__init__(ttl_seconds, similarity_threshold)
        self._store: dict[str, float] = {}  # key -> expiry timestamp

    def _cleanup(self):
        now = time.time()
        expired = [k for k, exp in self._store.items() if exp < now]
        for k in expired:
            del self._store[k]

    def is_duplicate(self, iso_number: str) -> bool:
        self._cleanup()
        normalized = _normalize(iso_number)
        key = self._make_key(normalized)

        # Exact match
        if key in self._store:
            return True

        # Fuzzy match — OCR-confusion variants
        for variant in _ocr_variants(normalized):
            variant_key = f"container:dedup:{variant}"
            if variant_key in self._store:
                logger.debug("Fuzzy duplicate found (memory): %s ~ %s",
                             iso_number, variant)
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
    ttl = cfg.get("ttl_seconds", 86400)
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
