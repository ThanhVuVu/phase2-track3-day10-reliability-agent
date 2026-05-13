from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache skeleton.

    TODO(student): Add a better semantic similarity function and false-hit guardrails.
    Use the module-level _is_uncacheable() and _looks_like_false_hit() helpers in your
    get() and set() methods.  For production, replace with SharedRedisCache.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        best_value: str | None = None
        best_score = 0.0
        now = time.time()
        
        # Cleanup expired entries
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
        
        best_entry: CacheEntry | None = None
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_entry = entry

        if best_score >= self.similarity_threshold and best_entry:
            # Final safety check for false hits (e.g., same text but different years)
            if _looks_like_false_hit(query, best_entry.key):
                return None, best_score
            return best_value, best_score
            
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Improved similarity using character 3-grams."""
        a_clean = a.lower().strip()
        b_clean = b.lower().strip()
        
        if a_clean == b_clean:
            return 1.0
            
        def get_ngrams(text: str, n: int = 3) -> set[str]:
            if len(text) < n:
                return {text}
            return {text[i:i+n] for i in range(len(text) - n + 1)}

        left = get_ngrams(a_clean)
        right = get_ngrams(b_clean)
        
        if not left or not right:
            return 0.0
            
        intersection = left & right
        union = left | right
        return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    TODO(student): Implement the get() and set() methods using Redis commands
    so that cache state is shared across multiple gateway instances.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib  # type: ignore

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis."""
        if _is_uncacheable(query):
            return None, 0.0

        # 1. Exact match check
        exact_key = f"{self.prefix}{self._query_hash(query)}"
        try:
            cached_data = self._redis.hgetall(exact_key)
            if cached_data:
                # Still check for false hits even on exact hash match (unlikely but safe)
                if _looks_like_false_hit(query, cached_data["query"]):
                    self.false_hit_log.append({"query": query, "cached": cached_data["query"]})
                    return None, 1.0
                return cached_data["response"], 1.0
        except Exception:
            # Redis down? Fallback gracefully
            pass

        # 2. Similarity scan
        best_value: str | None = None
        best_score = 0.0
        best_query: str | None = None

        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                entry = self._redis.hgetall(key)
                if not entry or "query" not in entry:
                    continue
                
                score = ResponseCache.similarity(query, entry["query"])
                if score > best_score:
                    best_score = score
                    best_value = entry["response"]
                    best_query = entry["query"]
        except Exception:
            pass

        if best_score >= self.similarity_threshold and best_value and best_query:
            if _looks_like_false_hit(query, best_query):
                self.false_hit_log.append({"query": query, "cached": best_query})
                return None, best_score
            return best_value, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL."""
        if _is_uncacheable(query):
            return

        key = f"{self.prefix}{self._query_hash(query)}"
        mapping = {
            "query": query,
            "response": value,
            "created_at": str(time.time()),
        }
        if metadata:
            mapping.update(metadata)

        try:
            self._redis.hset(key, mapping=mapping)
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            # Redis down? Fail silently (cache is optional)
            pass

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
