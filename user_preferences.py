"""User preferences and personalization settings.

Stores:
- Response style (brief/detailed, formal/casual)
- Language preferences
- Model preferences with history
- Privacy settings
- Notification preferences
"""
import json
import sqlite3
import time
from typing import Optional, Dict, Any
from threading import Lock

class UserPreferences:
    """Thread-safe user preferences manager."""

    # Default preferences (keys match the `preferences` table columns)
    DEFAULTS = {
        "response_style": "auto",  # auto, brief, detailed
        "tone": "friendly",  # friendly, formal, casual, expert
        "language": "auto",  # auto-detect or specific language code
        "preferred_model": None,  # None = use bot default
        "enable_cache": True,
        "enable_history": True,
        "auto_delete_history_days": 0,  # 0 = never
        "enable_voice_replies": True,
        "enable_notifications": True,
        "emoji_density": "auto",  # none, low, auto, high
        "code_style": "complete",  # complete, concise
        "timezone": "UTC",
        "custom_system_prompt": None,
    }

    RESPONSE_STYLES = {
        "brief": "Keep answers short and concise. One or two sentences for simple questions.",
        "detailed": "Provide comprehensive, detailed explanations with examples and context.",
        "auto": ""  # Let model decide based on question complexity
    }

    TONES = {
        "friendly": "Warm, approachable, conversational tone.",
        "formal": "Professional, precise, business-appropriate language.",
        "casual": "Relaxed, informal style with slang and colloquialisms.",
        "expert": "Technical, thorough explanations assuming advanced knowledge."
    }

    def __init__(self, db_path: str = "preferences.db"):
        self.db_path = db_path
        self.lock = Lock()
        self.init_db()
        self._cache = {}  # {user_id: (prefs_dict, timestamp)}
        self.CACHE_TTL = 300  # 5 minutes

    def init_db(self):
        """Initialize preferences database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS preferences (
                    user_id INTEGER PRIMARY KEY,
                    response_style TEXT DEFAULT 'auto',
                    tone TEXT DEFAULT 'friendly',
                    language TEXT DEFAULT 'auto',
                    preferred_model TEXT,
                    enable_cache INTEGER DEFAULT 1,
                    enable_history INTEGER DEFAULT 1,
                    auto_delete_history_days INTEGER DEFAULT 0,
                    enable_voice_replies INTEGER DEFAULT 1,
                    enable_notifications INTEGER DEFAULT 1,
                    emoji_density TEXT DEFAULT 'auto',
                    code_style TEXT DEFAULT 'complete',
                    timezone TEXT DEFAULT 'UTC',
                    custom_system_prompt TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_history (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    used_at REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES preferences(user_id)
                )
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_model_history_user ON model_history(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_model_history_time ON model_history(used_at)")
            conn.commit()

    def get(self, user_id: int) -> Dict[str, Any]:
        """Get user preferences with caching."""
        now = time.time()

        # Check cache
        if user_id in self._cache:
            prefs, cached_at = self._cache[user_id]
            if now - cached_at < self.CACHE_TTL:
                return prefs

        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM preferences WHERE user_id = ?",
                    (user_id,)
                ).fetchone()

                if row:
                    prefs = dict(row)
                    # Convert integers to booleans
                    for key in ["enable_cache", "enable_history", "enable_voice_replies", "enable_notifications"]:
                        prefs[key] = bool(prefs[key])
                else:
                    prefs = self.DEFAULTS.copy()
                    prefs["user_id"] = user_id

                # Cache it
                self._cache[user_id] = (prefs, now)
                return prefs

    def set(self, user_id: int, **kwargs):
        """Update user preferences."""
        now = time.time()

        # Filter valid preferences
        valid_keys = set(self.DEFAULTS.keys())
        updates = {k: v for k, v in kwargs.items() if k in valid_keys}

        if not updates:
            return

        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                # Check if user exists
                exists = conn.execute(
                    "SELECT 1 FROM preferences WHERE user_id = ?",
                    (user_id,)
                ).fetchone()

                if exists:
                    # Build UPDATE query
                    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
                    values = list(updates.values()) + [now, user_id]
                    conn.execute(
                        f"UPDATE preferences SET {set_clause}, updated_at = ? WHERE user_id = ?",
                        values
                    )
                else:
                    # INSERT with defaults + updates
                    defaults = self.DEFAULTS.copy()
                    defaults.update(updates)
                    defaults["user_id"] = user_id
                    defaults["created_at"] = now
                    defaults["updated_at"] = now

                    columns = ", ".join(defaults.keys())
                    placeholders = ", ".join("?" * len(defaults))
                    conn.execute(
                        f"INSERT INTO preferences ({columns}) VALUES ({placeholders})",
                        list(defaults.values())
                    )

                conn.commit()

        # Invalidate cache
        if user_id in self._cache:
            del self._cache[user_id]

    def log_model_usage(self, user_id: int, model: str):
        """Track which models user prefers."""
        now = time.time()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO model_history (user_id, model, used_at) VALUES (?, ?, ?)",
                (user_id, model, now)
            )
            conn.commit()

    def get_recommended_model(self, user_id: int) -> Optional[str]:
        """Get user's most frequently used model."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT model, COUNT(*) as count
                FROM model_history
                WHERE user_id = ? AND used_at > ?
                GROUP BY model
                ORDER BY count DESC
                LIMIT 1
            """, (user_id, time.time() - 2592000)).fetchone()  # last 30 days

            return row[0] if row else None

    def get_system_prompt_additions(self, user_id: int) -> str:
        """Get system prompt modifications based on user preferences."""
        prefs = self.get(user_id)
        additions = []

        # Response style
        style = prefs.get("response_style", "auto")
        if style != "auto" and style in self.RESPONSE_STYLES:
            additions.append(self.RESPONSE_STYLES[style])

        # Tone
        tone = prefs.get("tone", "friendly")
        if tone in self.TONES:
            additions.append(self.TONES[tone])

        # Emoji density
        emoji = prefs.get("emoji_density", "auto")
        if emoji == "none":
            additions.append("Never use emoji in responses.")
        elif emoji == "high":
            additions.append("Use emoji liberally to add personality and clarity.")

        # Custom system prompt
        custom = prefs.get("custom_system_prompt")
        if custom:
            additions.append(custom)

        return " ".join(additions)

    def reset(self, user_id: int):
        """Reset user preferences to defaults."""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM preferences WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM model_history WHERE user_id = ?", (user_id,))
                conn.commit()

        # Invalidate cache
        if user_id in self._cache:
            del self._cache[user_id]

    def export(self, user_id: int) -> dict:
        """Export all user preferences as JSON-serializable dict."""
        prefs = self.get(user_id)

        with sqlite3.connect(self.db_path) as conn:
            model_history = conn.execute(
                """SELECT model, used_at FROM model_history
                   WHERE user_id = ?
                   ORDER BY used_at DESC
                   LIMIT 100""",
                (user_id,)
            ).fetchall()

        return {
            "preferences": prefs,
            "model_history": [{"model": m, "timestamp": t} for m, t in model_history]
        }

    def get_stats(self) -> dict:
        """Get overall preference statistics."""
        with sqlite3.connect(self.db_path) as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0]

            style_dist = conn.execute(
                "SELECT response_style, COUNT(*) FROM preferences GROUP BY response_style"
            ).fetchall()

            tone_dist = conn.execute(
                "SELECT tone, COUNT(*) FROM preferences GROUP BY tone"
            ).fetchall()

            popular_models = conn.execute("""
                SELECT model, COUNT(*) as usage_count
                FROM model_history
                WHERE used_at > ?
                GROUP BY model
                ORDER BY usage_count DESC
                LIMIT 5
            """, (time.time() - 2592000,)).fetchall()

        return {
            "total_users": total_users,
            "response_styles": dict(style_dist),
            "tones": dict(tone_dist),
            "popular_models": [{"model": m, "count": c} for m, c in popular_models]
        }
