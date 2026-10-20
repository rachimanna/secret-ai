"""Rate limiting and quotas for users and groups.

Semantics: every counter stores how much has been USED today (starts at 0,
increments on use, compared against the configured limit). A row's `reset_at`
is when the daily window rolls over; past it, counters reset to 0.

Personal chats: per-user daily limits.
Groups/supergroups: a shared group pool AND a per-user cap inside the group,
so one member can't drain the whole group's quota. Supergroups are stricter.
"""
import sqlite3
import time
from typing import Tuple
from threading import Lock

DAY = 86400


class RateLimiter:
    """Thread-safe rate limiter with SQLite persistence."""

    # Daily limits per chat type and feature.
    PERSONAL_LIMITS = {
        "requests": 50,   # chat messages per day
        "images": 10,     # image generations per day
        "videos": 3,      # video generations per day
    }

    GROUP_LIMITS = {
        "requests": 200, "requests_per_user": 30,
        "images": 30, "images_per_user": 5,
        "videos": 5, "videos_per_user": 1,
    }

    SUPERGROUP_LIMITS = {
        "requests": 300, "requests_per_user": 25,
        "images": 40, "images_per_user": 4,
        "videos": 8, "videos_per_user": 1,
    }

    FEATURES = ("requests", "images", "videos")

    def __init__(self, db_path: str = "quotas.db"):
        self.db_path = db_path
        self.lock = Lock()
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS quotas (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    chat_type TEXT,
                    requests INTEGER DEFAULT 0,
                    images INTEGER DEFAULT 0,
                    videos INTEGER DEFAULT 0,
                    reset_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS group_pools (
                    group_id INTEGER PRIMARY KEY,
                    chat_type TEXT,
                    requests INTEGER DEFAULT 0,
                    images INTEGER DEFAULT 0,
                    videos INTEGER DEFAULT 0,
                    reset_at REAL NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.commit()

    def get_limits(self, chat_type: str) -> dict:
        if chat_type == "private":
            return self.PERSONAL_LIMITS
        if chat_type == "supergroup":
            return self.SUPERGROUP_LIMITS
        return self.GROUP_LIMITS

    def check_quota(self, chat_id: int, user_id: int, chat_type: str,
                    feature: str = "requests") -> Tuple[bool, str]:
        """Check and, if allowed, consume one unit. Returns (allowed, message)."""
        if feature not in self.FEATURES:
            feature = "requests"
        limits = self.get_limits(chat_type)
        now = time.time()

        with self.lock, sqlite3.connect(self.db_path) as conn:
            user_used, reset_at = self._user_counter(conn, chat_id, user_id, chat_type, feature, now)

            if chat_type == "private":
                limit = limits.get(feature, 0)
                if user_used >= limit:
                    return False, self._limit_msg(feature, limit, reset_at, now, scope="personal")
                self._increment(conn, "quotas", "chat_id", chat_id, user_id, feature)
                conn.commit()
                return True, ""

            # Group / supergroup: enforce the shared pool first, then the per-user cap.
            pool_limit = limits.get(feature, 0)
            user_limit = limits.get(f"{feature}_per_user", 0)
            pool_used, pool_reset = self._pool_counter(conn, chat_id, chat_type, feature, now)

            if pool_used >= pool_limit:
                return False, self._limit_msg(feature, pool_limit, pool_reset, now, scope="group")
            if user_used >= user_limit:
                return False, self._limit_msg(feature, user_limit, reset_at, now, scope="group_user")

            self._increment(conn, "quotas", "chat_id", chat_id, user_id, feature)
            self._increment(conn, "group_pools", "group_id", chat_id, None, feature)
            conn.commit()
            return True, ""

    def _user_counter(self, conn, chat_id, user_id, chat_type, feature, now):
        """Return (used, reset_at) for a user row, creating/resetting it as needed."""
        row = conn.execute(
            "SELECT requests, images, videos, reset_at FROM quotas WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
        if row is None:
            reset_at = now + DAY
            conn.execute(
                "INSERT INTO quotas (chat_id, user_id, chat_type, reset_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, user_id, chat_type, reset_at, now)
            )
            return 0, reset_at
        requests, images, videos, reset_at = row
        if reset_at < now:
            reset_at = now + DAY
            conn.execute(
                "UPDATE quotas SET requests=0, images=0, videos=0, reset_at=? WHERE chat_id=? AND user_id=?",
                (reset_at, chat_id, user_id)
            )
            return 0, reset_at
        used = {"requests": requests, "images": images, "videos": videos}[feature]
        return used, reset_at

    def _pool_counter(self, conn, group_id, chat_type, feature, now):
        """Return (used, reset_at) for a group's shared pool, creating/resetting as needed."""
        row = conn.execute(
            "SELECT requests, images, videos, reset_at FROM group_pools WHERE group_id=?",
            (group_id,)
        ).fetchone()
        if row is None:
            reset_at = now + DAY
            conn.execute(
                "INSERT INTO group_pools (group_id, chat_type, reset_at, created_at) VALUES (?, ?, ?, ?)",
                (group_id, chat_type, reset_at, now)
            )
            return 0, reset_at
        requests, images, videos, reset_at = row
        if reset_at < now:
            reset_at = now + DAY
            conn.execute(
                "UPDATE group_pools SET requests=0, images=0, videos=0, reset_at=? WHERE group_id=?",
                (reset_at, group_id)
            )
            return 0, reset_at
        used = {"requests": requests, "images": images, "videos": videos}[feature]
        return used, reset_at

    def _increment(self, conn, table, key_col, group_or_chat_id, user_id, feature, amount=1):
        if user_id is None:
            conn.execute(
                f"UPDATE {table} SET {feature}={feature}+? WHERE {key_col}=?",
                (amount, group_or_chat_id)
            )
        else:
            conn.execute(
                f"UPDATE {table} SET {feature}={feature}+? WHERE {key_col}=? AND user_id=?",
                (amount, group_or_chat_id, user_id)
            )

    def _limit_msg(self, feature, limit, reset_at, now, scope):
        hours = max(1, int((reset_at - now) / 3600) + 1)
        if scope == "personal":
            return f"Daily {feature} limit reached ({limit}/day). Resets in {hours}h."
        if scope == "group":
            return f"Group {feature} limit reached ({limit}/day). Resets in {hours}h."
        return f"Your personal {feature} limit in this group reached ({limit}/day). Resets in {hours}h."

    def get_stats(self, chat_id: int, user_id: int = None) -> dict:
        """Current usage. With user_id: that user's row; without: the group pool."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            if user_id is not None:
                row = conn.execute(
                    "SELECT requests, images, videos, reset_at FROM quotas WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT requests, images, videos, reset_at FROM group_pools WHERE group_id=?",
                    (chat_id,)
                ).fetchone()
        if not row:
            return {}
        requests, images, videos, reset_at = row
        if reset_at < now:  # window has rolled over; report a fresh slate
            return {"requests_used": 0, "images_used": 0, "videos_used": 0, "reset_at": now + DAY}
        return {"requests_used": requests, "images_used": images,
                "videos_used": videos, "reset_at": reset_at}
