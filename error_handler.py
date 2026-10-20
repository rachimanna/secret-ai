"""Enhanced error handling and structured logging.

Features:
- Structured JSON logging
- Request tracing
- Automatic retry with exponential backoff
- User-friendly error messages
- Error categorization and statistics
"""
import json
import logging
import sqlite3
import sys
import time
import traceback
from functools import wraps
from typing import Optional, Callable, Any
from threading import Lock

class StructuredLogger:
    """Structured JSON logger with request tracing."""

    def __init__(self, name: str, db_path: str = "errors.db"):
        self.logger = logging.getLogger(name)
        self.db_path = db_path
        self.lock = Lock()
        self.init_db()

    def init_db(self):
        """Initialize error tracking database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS error_log (
                    id INTEGER PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    level TEXT NOT NULL,
                    category TEXT,
                    error_type TEXT,
                    message TEXT NOT NULL,
                    user_id INTEGER,
                    chat_id INTEGER,
                    trace_id TEXT,
                    stack_trace TEXT,
                    context TEXT,
                    resolved INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_error_timestamp ON error_log(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_error_category ON error_log(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_error_type ON error_log(error_type)")
            conn.commit()

    def log(self, level: str, message: str, **context):
        """Log structured message."""
        log_entry = {
            "timestamp": time.time(),
            "level": level,
            "message": message,
            **context
        }

        # Log to standard logger
        getattr(self.logger, level.lower(), self.logger.info)(json.dumps(log_entry, ensure_ascii=False))

        # Store errors in database
        if level in ["ERROR", "CRITICAL"]:
            self._store_error(log_entry)

    def _store_error(self, log_entry: dict):
        """Store error in database for analytics."""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO error_log
                    (timestamp, level, category, error_type, message, user_id, chat_id, trace_id, stack_trace, context)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    log_entry.get("timestamp", time.time()),
                    log_entry.get("level", "ERROR"),
                    log_entry.get("category"),
                    log_entry.get("error_type"),
                    log_entry.get("message"),
                    log_entry.get("user_id"),
                    log_entry.get("chat_id"),
                    log_entry.get("trace_id"),
                    log_entry.get("stack_trace"),
                    json.dumps(log_entry.get("context", {}))
                ))
                conn.commit()

    def info(self, message: str, **context):
        self.log("INFO", message, **context)

    def warning(self, message: str, **context):
        self.log("WARNING", message, **context)

    def error(self, message: str, **context):
        self.log("ERROR", message, **context)

    def critical(self, message: str, **context):
        self.log("CRITICAL", message, **context)

    def get_error_stats(self, hours: int = 24) -> dict:
        """Get error statistics for the last N hours."""
        since = time.time() - (hours * 3600)

        with sqlite3.connect(self.db_path) as conn:
            total_errors = conn.execute(
                "SELECT COUNT(*) FROM error_log WHERE timestamp > ?",
                (since,)
            ).fetchone()[0]

            by_category = conn.execute(
                "SELECT category, COUNT(*) FROM error_log WHERE timestamp > ? GROUP BY category",
                (since,)
            ).fetchall()

            by_type = conn.execute(
                "SELECT error_type, COUNT(*) FROM error_log WHERE timestamp > ? GROUP BY error_type ORDER BY COUNT(*) DESC LIMIT 10",
                (since,)
            ).fetchall()

            recent_errors = conn.execute(
                """SELECT timestamp, category, error_type, message
                   FROM error_log
                   WHERE timestamp > ?
                   ORDER BY timestamp DESC
                   LIMIT 20""",
                (since,)
            ).fetchall()

        return {
            "total_errors": total_errors,
            "by_category": dict(by_category),
            "top_error_types": [{"type": t, "count": c} for t, c in by_type],
            "recent": [
                {
                    "timestamp": ts,
                    "category": cat,
                    "type": typ,
                    "message": msg[:200]
                }
                for ts, cat, typ, msg in recent_errors
            ]
        }


class RetryHandler:
    """Automatic retry with exponential backoff."""

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 60.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    def __call__(self, func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(self.max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    # Don't retry certain errors
                    if self._is_fatal(e):
                        raise

                    if attempt < self.max_retries - 1:
                        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                        await asyncio.sleep(delay)

            raise last_exception

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(self.max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    if self._is_fatal(e):
                        raise

                    if attempt < self.max_retries - 1:
                        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                        time.sleep(delay)

            raise last_exception

        # Return appropriate wrapper based on function type
        import inspect
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    def _is_fatal(self, error: Exception) -> bool:
        """Check if error should not be retried."""
        fatal_types = [
            "AuthenticationError",
            "PermissionDeniedError",
            "BadRequestError",
            "ValueError",
            "KeyError",
        ]
        return type(error).__name__ in fatal_types


class UserFriendlyErrors:
    """Convert technical errors to user-friendly messages."""

    ERROR_MESSAGES = {
        "APIConnectionError": "⚠️ Не могу подключиться к AI модели. Попробуйте через минуту.",
        "APITimeoutError": "⏱ Запрос занял слишком много времени. Попробуйте еще раз.",
        "RateLimitError": "🚦 Слишком много запросов. Подождите немного.",
        "AuthenticationError": "🔑 Проблема с авторизацией API. Обратитесь к администратору.",
        "NetworkError": "🌐 Проблема с сетью. Проверьте подключение.",
        "BadRequest": "❌ Некорректный запрос. Попробуйте переформулировать.",
        "Forbidden": "🚫 У бота нет доступа к этому действию.",
        "MediaError": "🎨 Не удалось сгенерировать медиа. Попробуйте другой запрос.",
        "GitHubError": "⚙️ Ошибка при работе с GitHub. Проверьте подключение.",
        "QuotaExceeded": "📊 Достигнут лимит запросов. Попробуйте позже.",
    }

    @classmethod
    def get_message(cls, error: Exception, language: str = "ru") -> str:
        """Get user-friendly error message."""
        error_type = type(error).__name__

        # Check for specific error type
        if error_type in cls.ERROR_MESSAGES:
            return cls.ERROR_MESSAGES[error_type]

        # Check for substring matches
        for key, message in cls.ERROR_MESSAGES.items():
            if key.lower() in str(error).lower():
                return message

        # Generic fallback
        if language == "ru":
            return "❌ Произошла ошибка. Попробуйте еще раз или обратитесь к администратору."
        return "❌ An error occurred. Please try again or contact the administrator."

    @classmethod
    def categorize(cls, error: Exception) -> str:
        """Categorize error type."""
        error_type = type(error).__name__

        if "API" in error_type or "Connection" in error_type:
            return "api"
        elif "Network" in error_type or "Timeout" in error_type:
            return "network"
        elif "Auth" in error_type or "Permission" in error_type:
            return "auth"
        elif "Rate" in error_type or "Quota" in error_type:
            return "rate_limit"
        elif "Media" in error_type or "Image" in error_type or "Video" in error_type:
            return "media"
        elif "GitHub" in error_type or "Git" in error_type:
            return "integration"
        else:
            return "general"


# Global logger instance
logger = StructuredLogger("secret_ai")

# Import asyncio for async retry
import asyncio
