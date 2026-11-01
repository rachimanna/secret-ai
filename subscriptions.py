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


def parse_group(payload):
    """'gsub:<chat id>:<user id>' -> (chat id, user id), or None."""
    parts = (payload or "").split(":")
    try:
        return (int(parts[1]), int(parts[2])) if len(parts) == 3 and parts[0] == "gsub" and int(parts[1]) < 0 else None
    except ValueError:
        return None


class Store:
    """Limits are counted per UTC day. A limit of 0 turns limits off for that plan."""

    def __init__(self, db):
        self.db = db
        self.free_limit = _number("FREE_MESSAGES_PER_DAY", 50)
        self.pro_limit = _number("PRO_MESSAGES_PER_DAY", 2000)
        self.stars = max(1, _number("PRO_STARS", 299))
        self.bonus = _number("PRO_MONTHLY_CREDITS", 150)
        self.group_stars = max(1, _number("GROUP_PRO_STARS", 799))
        self.group_pool = _number("GROUP_PRO_MESSAGES_PER_DAY", 3000)
        self.group_member = _number("GROUP_PRO_MEMBER_LIMIT", 200)
        self.research_free = _number("FREE_RESEARCH_PER_DAY", 1)
        self.research_pro = _number("PRO_RESEARCH_PER_DAY", 10)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS sub_plans (user_id INTEGER PRIMARY KEY, until INTEGER NOT NULL, "
                            "started INTEGER NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS sub_usage (user_id INTEGER NOT NULL, day TEXT NOT NULL, "
                            "used INTEGER NOT NULL, PRIMARY KEY(user_id, day))")
            self.db.execute("CREATE TABLE IF NOT EXISTS group_plans (chat_id INTEGER PRIMARY KEY, until INTEGER NOT NULL, started INTEGER NOT NULL, "
                            "payer INTEGER NOT NULL, member_limit INTEGER)")
            self.db.execute("CREATE TABLE IF NOT EXISTS group_usage (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day TEXT NOT NULL, "
                            "used INTEGER NOT NULL, PRIMARY KEY(chat_id, user_id, day))")
            self.db.execute("CREATE TABLE IF NOT EXISTS sub_payments (charge TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER, "
                            "stars INTEGER NOT NULL, at INTEGER NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS sub_limit_hits (user_id INTEGER NOT NULL, day TEXT NOT NULL, PRIMARY KEY(user_id, day))")
            self.db.execute("CREATE TABLE IF NOT EXISTS research_usage (user_id INTEGER NOT NULL, day TEXT NOT NULL, "
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

    # ----- one subscription for a whole group -----
    def group_until(self, chat_id):
        row = self.db.execute("SELECT until FROM group_plans WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else 0

    def group_is_pro(self, chat_id, now=None):
        until = self.group_until(chat_id)
        return bool(until) and until + GRACE > (now if now is not None else time.time())

    def activate_group(self, chat_id, payer, until=None, now=None):
        now = int(now if now is not None else time.time())
        until = int(until or now + PERIOD)
        with self.db:
            self.db.execute("INSERT INTO group_plans(chat_id, until, started, payer) VALUES (?, ?, ?, ?) ON CONFLICT(chat_id) "
                            "DO UPDATE SET until=MAX(until, excluded.until), payer=excluded.payer", (chat_id, until, now, payer))
        return self.group_until(chat_id)

    def member_limit(self, chat_id):
        """Messages one member may spend a day from the group's plan; 0 means no personal cap."""
        row = self.db.execute("SELECT member_limit FROM group_plans WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row and row[0] is not None else self.group_member

    def set_member_limit(self, chat_id, limit):
        with self.db:
            return self.db.execute("UPDATE group_plans SET member_limit=? WHERE chat_id=?", (max(0, int(limit)), chat_id)).rowcount > 0

    def spend_in_group(self, chat_id, user_id, now=None):
        """Take a message from the group's plan. False (no plan or it ran out): the person's own allowance applies."""
        if not self.group_is_pro(chat_id, now):
            return False
        day, cap = self._day(now), self.member_limit(chat_id)
        with self.db:
            total = self.db.execute("SELECT COALESCE(SUM(used), 0) FROM group_usage WHERE chat_id=? AND day=?", (chat_id, day)).fetchone()[0]
            if self.group_pool and total >= self.group_pool:
                return False
            mine = self.db.execute("SELECT used FROM group_usage WHERE chat_id=? AND user_id=? AND day=?", (chat_id, user_id, day)).fetchone()
            if cap and mine and mine[0] >= cap:
                return False
            self.db.execute("INSERT INTO group_usage(chat_id, user_id, day, used) VALUES (?, ?, ?, 1) "
                            "ON CONFLICT(chat_id, user_id, day) DO UPDATE SET used=used+1", (chat_id, user_id, day))
        return True

    def group_state(self, chat_id, now=None):
        total = self.db.execute("SELECT COALESCE(SUM(used), 0) FROM group_usage WHERE chat_id=? AND day=?", (chat_id, self._day(now))).fetchone()[0]
        return {"active": self.group_is_pro(chat_id, now), "until": self.group_until(chat_id), "stars": self.group_stars,
                "pool": self.group_pool, "member_limit": self.member_limit(chat_id), "used_today": total}

    # ----- deep research has its own, smaller allowance -----
    def research_limit(self, user_id, now=None):
        return self.research_pro if self.is_pro(user_id, now) else self.research_free

    def research_take(self, user_id, now=None):
        limit, day = self.research_limit(user_id, now), self._day(now)
        with self.db:
            taken = self.db.execute("INSERT INTO research_usage(user_id, day, used) VALUES (?, ?, 1) ON CONFLICT(user_id, day) "
                                    "DO UPDATE SET used=used+1 WHERE used<?", (user_id, day, limit)) if limit else None
        return taken.rowcount > 0 if taken is not None else False

    def research_refund(self, user_id, now=None):
        with self.db:
            self.db.execute("UPDATE research_usage SET used=MAX(0, used-1) WHERE user_id=? AND day=?", (user_id, self._day(now)))

    # ----- the numbers that show whether the limit and the price work -----
    def note_limit_hit(self, user_id, now=None):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO sub_limit_hits(user_id, day) VALUES (?, ?)", (user_id, self._day(now)))

    def record_payment(self, charge, user_id, stars, chat_id=None, now=None):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO sub_payments(charge, user_id, chat_id, stars, at) VALUES (?, ?, ?, ?, ?)",
                            (charge, user_id, chat_id, stars, int(now if now is not None else time.time())))

    def numbers(self, now=None):
        now = int(now if now is not None else time.time())
        week, alive = now - 7 * 86400, now - GRACE
        since = self._day(week)
        one = lambda sql, args=(): self.db.execute(sql, args).fetchone()[0] or 0
        hit = one("SELECT COUNT(DISTINCT user_id) FROM sub_limit_hits WHERE day>=?", (since,))
        first_payers = "SELECT user_id, chat_id, MIN(at) AS first FROM sub_payments GROUP BY user_id, COALESCE(chat_id, 0)"
        bought = one(f"SELECT COUNT(*) FROM ({first_payers}) WHERE first>=?", (week,))
        payments = one("SELECT COUNT(*) FROM sub_payments WHERE at>=?", (week,))
        return {"pro_active": one("SELECT COUNT(*) FROM sub_plans WHERE until>?", (alive,)),
                "groups_active": one("SELECT COUNT(*) FROM group_plans WHERE until>?", (alive,)),
                "limit_hit_week": hit, "bought_week": bought, "renewed_week": max(0, payments - bought),
                "stars_week": one("SELECT SUM(stars) FROM sub_payments WHERE at>=?", (week,)),
                "conversion": round(100 * bought / hit, 1) if hit else 0}

    def state(self, user_id, now=None):
        return {"pro": self.is_pro(user_id, now), "until": self.until(user_id), "left": self.left(user_id, now),
                "limit": self.limit(user_id, now), "stars": self.stars, "bonus": self.bonus,
                "free_limit": self.free_limit, "pro_limit": self.pro_limit, "group_stars": self.group_stars}
