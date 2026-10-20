"""Analytics and monitoring dashboard.

Tracks:
- Usage statistics (commands, messages, users)
- Model usage and costs
- Performance metrics
- Popular features
- Error rates
"""
import json
import sqlite3
import time
from collections import defaultdict
from typing import Dict, List, Optional
from threading import Lock

class Analytics:
    """Thread-safe analytics tracker."""

    def __init__(self, db_path: str = "analytics.db"):
        self.db_path = db_path
        self.lock = Lock()
        self.init_db()

    def init_db(self):
        """Initialize analytics database."""
        with sqlite3.connect(self.db_path) as conn:
            # User activity
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_activity (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    chat_type TEXT,
                    activity_type TEXT NOT NULL,
                    model TEXT,
                    tokens_used INTEGER DEFAULT 0,
                    cost_usd REAL DEFAULT 0,
                    response_time_ms INTEGER,
                    success INTEGER DEFAULT 1,
                    timestamp REAL NOT NULL
                )
            """)

            # Command usage
            conn.execute("""
                CREATE TABLE IF NOT EXISTS command_usage (
                    id INTEGER PRIMARY KEY,
                    command TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    chat_type TEXT,
                    success INTEGER DEFAULT 1,
                    timestamp REAL NOT NULL
                )
            """)

            # Model usage
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_usage (
                    id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    cost_usd REAL DEFAULT 0,
                    cached INTEGER DEFAULT 0,
                    timestamp REAL NOT NULL
                )
            """)

            # Daily summaries
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date TEXT PRIMARY KEY,
                    total_users INTEGER DEFAULT 0,
                    total_messages INTEGER DEFAULT 0,
                    total_commands INTEGER DEFAULT 0,
                    total_images INTEGER DEFAULT 0,
                    total_videos INTEGER DEFAULT 0,
                    total_cost_usd REAL DEFAULT 0,
                    avg_response_time_ms INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0
                )
            """)

            # Indices
            conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON user_activity(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_user ON user_activity(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_type ON user_activity(activity_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_command_timestamp ON command_usage(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_model_timestamp ON model_usage(timestamp)")

            conn.commit()

    def track_activity(self, user_id: int, chat_id: int, chat_type: str, activity_type: str,
                      model: str = "", tokens: int = 0, cost: float = 0.0,
                      response_time_ms: int = 0, success: bool = True):
        """Track user activity."""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO user_activity
                    (user_id, chat_id, chat_type, activity_type, model, tokens_used, cost_usd, response_time_ms, success, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, chat_id, chat_type, activity_type, model, tokens, cost, response_time_ms, int(success), time.time()))
                conn.commit()

    def track_command(self, command: str, user_id: int, chat_id: int, chat_type: str, success: bool = True):
        """Track command usage."""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO command_usage
                    (command, user_id, chat_id, chat_type, success, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (command, user_id, chat_id, chat_type, int(success), time.time()))
                conn.commit()

    def track_model_usage(self, model: str, user_id: int, chat_id: int,
                         prompt_tokens: int = 0, completion_tokens: int = 0,
                         cost: float = 0.0, cached: bool = False):
        """Track model API usage."""
        total_tokens = prompt_tokens + completion_tokens

        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO model_usage
                    (model, user_id, chat_id, prompt_tokens, completion_tokens, total_tokens, cost_usd, cached, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (model, user_id, chat_id, prompt_tokens, completion_tokens, total_tokens, cost, int(cached), time.time()))
                conn.commit()

    def get_dashboard(self, hours: int = 24) -> dict:
        """Get comprehensive dashboard data."""
        since = time.time() - (hours * 3600)

        with sqlite3.connect(self.db_path) as conn:
            # Overall stats
            total_users = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM user_activity WHERE timestamp > ?",
                (since,)
            ).fetchone()[0]

            total_messages = conn.execute(
                "SELECT COUNT(*) FROM user_activity WHERE activity_type = 'message' AND timestamp > ?",
                (since,)
            ).fetchone()[0]

            total_commands = conn.execute(
                "SELECT COUNT(*) FROM command_usage WHERE timestamp > ?",
                (since,)
            ).fetchone()[0]

            # Activity breakdown
            activity_types = conn.execute("""
                SELECT activity_type, COUNT(*) as count
                FROM user_activity
                WHERE timestamp > ?
                GROUP BY activity_type
                ORDER BY count DESC
            """, (since,)).fetchall()

            # Most active users
            top_users = conn.execute("""
                SELECT user_id, COUNT(*) as activity_count
                FROM user_activity
                WHERE timestamp > ?
                GROUP BY user_id
                ORDER BY activity_count DESC
                LIMIT 10
            """, (since,)).fetchall()

            # Command popularity
            top_commands = conn.execute("""
                SELECT command, COUNT(*) as usage_count
                FROM command_usage
                WHERE timestamp > ?
                GROUP BY command
                ORDER BY usage_count DESC
                LIMIT 10
            """, (since,)).fetchall()

            # Model usage and costs
            model_stats = conn.execute("""
                SELECT model,
                       COUNT(*) as requests,
                       SUM(total_tokens) as total_tokens,
                       SUM(cost_usd) as total_cost,
                       AVG(total_tokens) as avg_tokens,
                       SUM(cached) as cache_hits
                FROM model_usage
                WHERE timestamp > ?
                GROUP BY model
                ORDER BY total_cost DESC
            """, (since,)).fetchall()

            # Performance metrics
            avg_response_time = conn.execute("""
                SELECT AVG(response_time_ms)
                FROM user_activity
                WHERE timestamp > ? AND response_time_ms > 0
            """, (since,)).fetchone()[0] or 0

            # Success rate
            total_activities = conn.execute(
                "SELECT COUNT(*) FROM user_activity WHERE timestamp > ?",
                (since,)
            ).fetchone()[0]

            successful_activities = conn.execute(
                "SELECT COUNT(*) FROM user_activity WHERE timestamp > ? AND success = 1",
                (since,)
            ).fetchone()[0]

            success_rate = (successful_activities / max(total_activities, 1)) * 100

            # Chat type distribution
            chat_types = conn.execute("""
                SELECT chat_type, COUNT(*) as count
                FROM user_activity
                WHERE timestamp > ?
                GROUP BY chat_type
            """, (since,)).fetchall()

        return {
            "period_hours": hours,
            "overview": {
                "total_users": total_users,
                "total_messages": total_messages,
                "total_commands": total_commands,
                "avg_response_time_ms": int(avg_response_time),
                "success_rate": round(success_rate, 2)
            },
            "activity_types": [{"type": t, "count": c} for t, c in activity_types],
            "top_users": [{"user_id": uid, "activity_count": c} for uid, c in top_users],
            "top_commands": [{"command": cmd, "count": c} for cmd, c in top_commands],
            "models": [
                {
                    "model": m,
                    "requests": r,
                    "total_tokens": t,
                    "total_cost_usd": round(cost, 4),
                    "avg_tokens": int(avg_t),
                    "cache_hits": ch,
                    "cache_rate": round((ch / max(r, 1)) * 100, 1)
                }
                for m, r, t, cost, avg_t, ch in model_stats
            ],
            "chat_types": dict(chat_types)
        }

    def get_user_stats(self, user_id: int, days: int = 30) -> dict:
        """Get statistics for a specific user."""
        since = time.time() - (days * 86400)

        with sqlite3.connect(self.db_path) as conn:
            total_messages = conn.execute(
                "SELECT COUNT(*) FROM user_activity WHERE user_id = ? AND timestamp > ? AND activity_type = 'message'",
                (user_id, since)
            ).fetchone()[0]

            total_commands = conn.execute(
                "SELECT COUNT(*) FROM command_usage WHERE user_id = ? AND timestamp > ?",
                (user_id, since)
            ).fetchone()[0]

            favorite_commands = conn.execute("""
                SELECT command, COUNT(*) as count
                FROM command_usage
                WHERE user_id = ? AND timestamp > ?
                GROUP BY command
                ORDER BY count DESC
                LIMIT 5
            """, (user_id, since)).fetchall()

            models_used = conn.execute("""
                SELECT model, COUNT(*) as count, SUM(total_tokens) as tokens
                FROM model_usage
                WHERE user_id = ? AND timestamp > ?
                GROUP BY model
                ORDER BY count DESC
            """, (user_id, since)).fetchall()

            # Activity by hour of day
            hourly_activity = conn.execute("""
                SELECT strftime('%H', datetime(timestamp, 'unixepoch')) as hour, COUNT(*) as count
                FROM user_activity
                WHERE user_id = ? AND timestamp > ?
                GROUP BY hour
                ORDER BY hour
            """, (user_id, since)).fetchall()

        return {
            "user_id": user_id,
            "period_days": days,
            "total_messages": total_messages,
            "total_commands": total_commands,
            "favorite_commands": [{"command": cmd, "count": c} for cmd, c in favorite_commands],
            "models_used": [{"model": m, "count": c, "tokens": t} for m, c, t in models_used],
            "hourly_activity": dict(hourly_activity)
        }

    def update_daily_stats(self):
        """Update daily summary statistics."""
        today = time.strftime("%Y-%m-%d")
        start_of_day = time.time() - (time.time() % 86400)

        with sqlite3.connect(self.db_path) as conn:
            stats = conn.execute("""
                SELECT
                    COUNT(DISTINCT user_id) as users,
                    SUM(CASE WHEN activity_type = 'message' THEN 1 ELSE 0 END) as messages,
                    COUNT(*) as activities,
                    SUM(cost_usd) as cost,
                    AVG(CASE WHEN response_time_ms > 0 THEN response_time_ms ELSE NULL END) as avg_time,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors
                FROM user_activity
                WHERE timestamp >= ?
            """, (start_of_day,)).fetchone()

            command_count = conn.execute(
                "SELECT COUNT(*) FROM command_usage WHERE timestamp >= ?",
                (start_of_day,)
            ).fetchone()[0]

            conn.execute("""
                INSERT OR REPLACE INTO daily_stats
                (date, total_users, total_messages, total_commands, total_cost_usd, avg_response_time_ms, error_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (today, stats[0], stats[1], command_count, stats[3] or 0, int(stats[4] or 0), stats[5]))

            conn.commit()

    def get_trends(self, days: int = 7) -> dict:
        """Get trend data over time."""
        with sqlite3.connect(self.db_path) as conn:
            daily_trends = conn.execute("""
                SELECT date, total_users, total_messages, total_commands, total_cost_usd, error_count
                FROM daily_stats
                ORDER BY date DESC
                LIMIT ?
            """, (days,)).fetchall()

        return {
            "daily": [
                {
                    "date": d,
                    "users": u,
                    "messages": m,
                    "commands": c,
                    "cost_usd": round(cost, 4),
                    "errors": e
                }
                for d, u, m, c, cost, e in reversed(daily_trends)
            ]
        }
