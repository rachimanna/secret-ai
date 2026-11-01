"""Secret AI Pro: a monthly Telegram Stars subscription and the daily message limits it lifts."""
import os
import sqlite3
import time
from datetime import datetime, timezone

PERIOD = 2592000  # Telegram Stars subscriptions renew every 30 days: the only period the Bot API accepts
GRACE = 86400  # a renewal may arrive a little late: Pro doesn't drop for that


def _number(name, default):
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def parse(payload):
    """'sub:pro:<user id>' -> user id, or None for anything else."""
    parts = (payload or "").split(":")
    if len(parts) == 3 and parts[0] == "sub" and parts[1] == "pro" and parts[2].isdigit():
        return int(parts[2])
    return None


class Store:
    """Limits are counted per UTC day. A limit of 0 turns limits off for that plan."""

    def __init__(self, db):
        self.db = db
        self.free_limit = _number("FREE_MESSAGES_PER_DAY", 50)
        self.pro_limit = _number("PRO_MESSAGES_PER_DAY", 2000)
        self.stars = max(1, _number("PRO_STARS", 299))
        self.bonus = _number("PRO_MONTHLY_CREDITS", 150)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS sub_plans (user_id INTEGER PRIMARY KEY, until INTEGER NOT NULL, "
                            "started INTEGER NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS sub_usage (user_id INTEGER NOT NULL, day TEXT NOT NULL, "
                            "used INTEGER NOT NULL, PRIMARY KEY(user_id, day))")

    @staticmethod
    def _day(now=None):
        return datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).strftime("%Y-%m-%d")

    def until(self, user_id):
        row = self.db.execute("SELECT until FROM sub_plans WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else 0

    def is_pro(self, user_id, now=None):
        until = self.until(user_id)
        return bool(until) and until + GRACE > (now if now is not None else time.time())

    def limit(self, user_id, now=None):
        return self.pro_limit if self.is_pro(user_id, now) else self.free_limit

    def activate(self, user_id, until=None, now=None):
        """Pro up to `until` (what Telegram reports), never shortening what was already paid for."""
        now = int(now if now is not None else time.time())
        until = int(until or now + PERIOD)
        with self.db:
            self.db.execute("INSERT INTO sub_plans(user_id, until, started) VALUES (?, ?, ?) ON CONFLICT(user_id) "
                            "DO UPDATE SET until=MAX(until, excluded.until)", (user_id, until, now))
        return self.until(user_id)

    def used(self, user_id, now=None):
        row = self.db.execute("SELECT used FROM sub_usage WHERE user_id=? AND day=?", (user_id, self._day(now))).fetchone()
        return row[0] if row else 0

    def left(self, user_id, now=None):
        """Messages left today; None means no limit."""
        limit = self.limit(user_id, now)
        return None if not limit else max(0, limit - self.used(user_id, now))

    def spend(self, user_id, now=None):
        """Take one message from today's allowance; False when there is none left. Atomic."""
        limit, day = self.limit(user_id, now), self._day(now)
        try:
            with self.db:
                if not limit:
                    self.db.execute("INSERT INTO sub_usage(user_id, day, used) VALUES (?, ?, 1) ON CONFLICT(user_id, day) "
                                    "DO UPDATE SET used=used+1", (user_id, day))
                    return True
                taken = self.db.execute("INSERT INTO sub_usage(user_id, day, used) VALUES (?, ?, 1) ON CONFLICT(user_id, day) "
                                        "DO UPDATE SET used=used+1 WHERE used<?", (user_id, day, limit))
                return taken.rowcount > 0
        except sqlite3.Error:  # a storage hiccup is no reason to refuse someone an answer
            return True

    def refund(self, user_id, now=None):
        """The answer failed: the message doesn't count."""
        with self.db:
            self.db.execute("UPDATE sub_usage SET used=MAX(0, used-1) WHERE user_id=? AND day=?", (user_id, self._day(now)))

    def state(self, user_id, now=None):
        return {"pro": self.is_pro(user_id, now), "until": self.until(user_id), "left": self.left(user_id, now),
                "limit": self.limit(user_id, now), "stars": self.stars, "bonus": self.bonus,
                "free_limit": self.free_limit, "pro_limit": self.pro_limit}
