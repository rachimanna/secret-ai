"""Scheduled tasks: the model runs a person's task on a schedule and sends them the result. It may only read in apps."""
import asyncio
import json
import logging
import re
import time

import recurring

LOG = logging.getLogger(__name__)

COMMAND = "auto"
FREE_SLOTS, PRO_SLOTS = 1, 10
MAX_TASK = 1500
TICK_SECONDS = 30
RUN_SECONDS = 240
PARALLEL = 2
FAILURES_TO_PAUSE = 5
FORM = re.compile(r"^(.+?\d{2}:\d{2})\s*[:—–-]\s*(.+)$", re.S)


def parse(text, tz):
    """'каждый день в 09:00: собери письма' -> (rule, task), or None."""
    match = FORM.match((text or "").strip())
    if not match or not match[2].strip():
        return None
    try:
        rule = recurring.parse(match[1], tz)
    except Exception:
        return None
    return (rule, match[2].strip()[:MAX_TASK]) if rule else None


def rule_text(rule, ru=True):
    days = rule["days"]
    names = ("понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям") if ru else (
        "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")
    if len(days) == 7:
        when = "каждый день" if ru else "every day"
    elif days == [0, 1, 2, 3, 4]:
        when = "по будням" if ru else "on weekdays"
    else:
        when = ("по " if ru else "on ") + names[days[0]]
    return f"{when} {'в' if ru else 'at'} {rule['time']} ({rule['tz']})"


class Store:
    def __init__(self, db):
        self.db = db
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS automations (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                            "rule TEXT NOT NULL, task TEXT NOT NULL, language TEXT NOT NULL, next_at INTEGER NOT NULL, "
                            "failures INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0)")
            self.db.execute("CREATE INDEX IF NOT EXISTS automations_due ON automations(paused, next_at)")

    def add(self, user_id, rule, task, language, slots, now=None):
        """The new task's id, or None when all the person's slots are taken."""
        now = int(now if now is not None else time.time())
        with self.db:
            if self.db.execute("SELECT COUNT(*) FROM automations WHERE user_id=?", (user_id,)).fetchone()[0] >= slots:
                return None
            return self.db.execute("INSERT INTO automations(user_id, rule, task, language, next_at) VALUES (?, ?, ?, ?, ?)",
                                   (user_id, json.dumps(rule), task, language, recurring.next_at(rule, now))).lastrowid

    def list(self, user_id):
        rows = self.db.execute("SELECT id, rule, task, next_at, paused FROM automations WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        return [{"id": r[0], "rule": json.loads(r[1]), "task": r[2], "next_at": r[3], "paused": bool(r[4])} for r in rows]

    def delete(self, user_id, item):
        with self.db:
            return self.db.execute("DELETE FROM automations WHERE id=? AND user_id=?", (item, user_id)).rowcount > 0

    def reset(self, user_id=None):
        with self.db:
            if user_id is None:
                self.db.execute("DELETE FROM automations")
            else:
                self.db.execute("DELETE FROM automations WHERE user_id=?", (user_id,))

    def claim_due(self, now=None, limit=PARALLEL):
        """Due tasks, moved to their next time first: a crash mid-run skips one run instead of repeating it forever."""
        now = int(now if now is not None else time.time())
        with self.db:
            rows = self.db.execute("SELECT id, user_id, rule, task, language, failures FROM automations "
                                   "WHERE paused=0 AND next_at<=? ORDER BY next_at LIMIT ?", (now, limit)).fetchall()
            for row in rows:
                self.db.execute("UPDATE automations SET next_at=? WHERE id=?", (recurring.next_at(row[2], now), row[0]))
        return [{"id": r[0], "user_id": r[1], "rule": json.loads(r[2]), "task": r[3], "language": r[4], "failures": r[5]} for r in rows]

    def finished(self, item, ok):
        """Remember the outcome; True if the task has just been paused after failing too many times in a row."""
        with self.db:
            if ok:
                self.db.execute("UPDATE automations SET failures=0 WHERE id=?", (item,))
                return False
            self.db.execute("UPDATE automations SET failures=failures+1, "
                            "paused=CASE WHEN failures+1>=? THEN 1 ELSE paused END WHERE id=?", (FAILURES_TO_PAUSE, item))
            row = self.db.execute("SELECT paused FROM automations WHERE id=?", (item,)).fetchone()
            return bool(row and row[0])

    def pause_user(self, user_id):
        with self.db:
            self.db.execute("UPDATE automations SET paused=1 WHERE user_id=?", (user_id,))


async def run_forever(store, execute, tick=TICK_SECONDS):
    """execute(item) runs one task. One failure never stops the loop."""
    running = set()
    while True:
        try:
            free = PARALLEL - len(running)
            for item in store.claim_due(limit=free) if free > 0 else []:
                task = asyncio.create_task(execute(item))
                running.add(task)
                task.add_done_callback(running.discard)
        except Exception as exc:
            LOG.warning("Automation tick failed: %s", type(exc).__name__)
        await asyncio.sleep(tick)
