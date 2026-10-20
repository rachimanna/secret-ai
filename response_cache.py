"""Response caching layer to reduce API calls and latency.

Caches:
- Common questions/answers (fuzzy matching)
- Model responses for identical prompts
- FAQ-style queries
- Weather/time queries with TTL
"""
import hashlib
import json
import sqlite3
import time
from typing import Optional, List, Tuple
from threading import Lock
from difflib import SequenceMatcher

class ResponseCache:
    """Thread-safe response cache with SQLite persistence."""

    def __init__(self, db_path: str = "cache.db"):
        self.db_path = db_path
        self.lock = Lock()
        self.init_db()

        # Default TTLs in seconds
        self.DEFAULT_TTL = 86400  # 24 hours
        self.SHORT_TTL = 3600     # 1 hour for time-sensitive
        self.LONG_TTL = 604800    # 7 days for stable content

        # Similarity threshold for fuzzy matching
        self.SIMILARITY_THRESHOLD = 0.85

    def init_db(self):
        """Initialize cache database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    id INTEGER PRIMARY KEY,
                    cache_key TEXT UNIQUE NOT NULL,
                    prompt TEXT NOT NULL,
                    response TEXT NOT NULL,
                    model TEXT,
                    user_id INTEGER,
                    chat_id INTEGER,
                    hits INTEGER DEFAULT 0,
                    ttl INTEGER DEFAULT 86400,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_key ON cache(cache_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON cache(expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prompt ON cache(prompt)")
            conn.commit()

    def _make_key(self, prompt: str, model: str = "", user_id: int = 0) -> str:
        """Generate cache key from prompt."""
        # Normalize prompt
        normalized = prompt.lower().strip()
        # Include model and user for personalization
        key_string = f"{normalized}:{model}:{user_id}"
        return hashlib.sha256(key_string.encode()).hexdigest()

    def get(self, prompt: str, model: str = "", user_id: int = 0) -> Optional[str]:
        """Get cached response if exists and not expired."""
        cache_key = self._make_key(prompt, model, user_id)
        now = time.time()

        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """SELECT response, expires_at, hits FROM cache
                       WHERE cache_key = ? AND expires_at > ?""",
                    (cache_key, now)
                ).fetchone()

                if row:
                    response, expires_at, hits = row
                    # Increment hit counter
                    conn.execute(
                        "UPDATE cache SET hits = hits + 1 WHERE cache_key = ?",
                        (cache_key,)
                    )
                    conn.commit()
                    return response

        # Try fuzzy matching if exact match not found
        return self._fuzzy_get(prompt, model, user_id)

    def _fuzzy_get(self, prompt: str, model: str = "", user_id: int = 0) -> Optional[str]:
        """Try to find similar cached prompts."""
        normalized = prompt.lower().strip()
        now = time.time()

        with sqlite3.connect(self.db_path) as conn:
            # Get recent similar prompts
            rows = conn.execute(
                """SELECT prompt, response, cache_key FROM cache
                   WHERE expires_at > ? AND model = ?
                   ORDER BY created_at DESC LIMIT 50""",
                (now, model)
            ).fetchall()

            best_match = None
            best_score = 0

            for cached_prompt, response, cache_key in rows:
                similarity = SequenceMatcher(None, normalized, cached_prompt.lower()).ratio()
                if similarity > best_score and similarity >= self.SIMILARITY_THRESHOLD:
                    best_score = similarity
                    best_match = (response, cache_key)

            if best_match:
                response, cache_key = best_match
                # Increment hits
                conn.execute(
                    "UPDATE cache SET hits = hits + 1 WHERE cache_key = ?",
                    (cache_key,)
                )
                conn.commit()
                return response

        return None

    def set(self, prompt: str, response: str, model: str = "", user_id: int = 0,
            chat_id: int = 0, ttl: Optional[int] = None):
        """Cache a response."""
        cache_key = self._make_key(prompt, model, user_id)
        now = time.time()
        ttl = ttl or self.DEFAULT_TTL
        expires_at = now + ttl

        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO cache
                       (cache_key, prompt, response, model, user_id, chat_id, ttl, expires_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (cache_key, prompt, response, model, user_id, chat_id, ttl, expires_at, now)
                )
                conn.commit()

    def invalidate(self, prompt: str = "", model: str = "", user_id: int = 0):
        """Invalidate specific cache entry."""
        if prompt:
            cache_key = self._make_key(prompt, model, user_id)
            with self.lock:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("DELETE FROM cache WHERE cache_key = ?", (cache_key,))
                    conn.commit()

    def clean_expired(self):
        """Remove expired cache entries."""
        now = time.time()
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                result = conn.execute("DELETE FROM cache WHERE expires_at < ?", (now,))
                conn.commit()
                return result.rowcount

    def get_stats(self) -> dict:
        """Get cache statistics."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM cache WHERE expires_at > ?", (now,)).fetchone()[0]
            total_hits = conn.execute("SELECT SUM(hits) FROM cache WHERE expires_at > ?", (now,)).fetchone()[0] or 0
            expired = conn.execute("SELECT COUNT(*) FROM cache WHERE expires_at <= ?", (now,)).fetchone()[0]

            top_hits = conn.execute(
                """SELECT prompt, hits, model FROM cache
                   WHERE expires_at > ?
                   ORDER BY hits DESC LIMIT 10""",
                (now,)
            ).fetchall()

            return {
                "total_entries": total,
                "total_hits": total_hits,
                "expired_entries": expired,
                "hit_rate": f"{(total_hits / max(total, 1)):.2f}",
                "top_queries": [{"prompt": p[:100], "hits": h, "model": m} for p, h, m in top_hits]
            }

    def should_cache(self, prompt: str, response: str) -> bool:
        """Determine if a response should be cached."""
        # Don't cache very short prompts or responses
        if len(prompt) < 10 or len(response) < 20:
            return False

        # Don't cache personal/sensitive data patterns
        sensitive_patterns = ["мой", "пароль", "токен", "ключ", "секрет", "api key"]
        prompt_lower = prompt.lower()
        if any(pattern in prompt_lower for pattern in sensitive_patterns):
            return False

        return True

    def get_cache_ttl(self, prompt: str) -> int:
        """Determine appropriate TTL based on prompt type."""
        prompt_lower = prompt.lower()

        # Time-sensitive queries: short TTL
        if any(word in prompt_lower for word in ["погода", "weather", "время", "time", "курс", "цена"]):
            return self.SHORT_TTL

        # Stable content: long TTL
        if any(word in prompt_lower for word in ["что такое", "what is", "как работает", "how does",
                                                   "объясни", "explain", "история", "history"]):
            return self.LONG_TTL

        return self.DEFAULT_TTL
