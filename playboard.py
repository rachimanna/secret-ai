"""The weekly tournament of the Mini App's solo games: each person's best result per game, summed over the week."""
import time
from datetime import datetime, timezone

KINDS = ("signal", "vault", "route")
MAX_SCORE = 5000  # one game can't score more: an obviously forged number is refused
MIN_GAP = 2.0
TOP = 10


def week(now=None):
    year, number, _ = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isocalendar()
    return f"{year}-W{number:02d}"


class Board:
    def __init__(self, db):
        self.db, self.last = db, {}
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS play_scores (week TEXT NOT NULL, user_id INTEGER NOT NULL, kind TEXT NOT NULL, "
                            "name TEXT NOT NULL, best INTEGER NOT NULL, PRIMARY KEY(week, user_id, kind))")

    def submit(self, user_id, name, kind, score, now=None):
        """True if the result was accepted. Scores come from the app, so this limits forgery but can't exclude it."""
        now = now if now is not None else time.time()
        if kind not in KINDS or not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= MAX_SCORE:
            return False
        if now - self.last.get(user_id, float("-inf")) < MIN_GAP:
            return False
        self.last[user_id] = now
        shown = " ".join(str(name or "").split())[:32] or "Игрок"
        with self.db:
            self.db.execute("INSERT INTO play_scores(week, user_id, kind, name, best) VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(week, user_id, kind) DO UPDATE SET best=MAX(best, excluded.best), name=excluded.name",
                            (week(now), user_id, kind, shown, score))
        return True

    def top(self, user_id=None, now=None, limit=TOP):
        current = week(now)
        rows = self.db.execute("SELECT user_id, MAX(name), SUM(best) AS total FROM play_scores WHERE week=? "
                               "GROUP BY user_id ORDER BY total DESC, user_id LIMIT ?", (current, limit)).fetchall()
        result = {"week": current, "top": [{"name": r[1], "score": r[2], "me": r[0] == user_id} for r in rows], "mine": None}
        if user_id is not None:
            mine = self.db.execute("SELECT SUM(best) FROM play_scores WHERE week=? AND user_id=?", (current, user_id)).fetchone()[0]
            if mine is not None:
                ahead = self.db.execute("SELECT COUNT(*) FROM (SELECT SUM(best) AS t FROM play_scores WHERE week=? GROUP BY user_id) "
                                        "WHERE t>?", (current, mine)).fetchone()[0]
                result["mine"] = {"score": mine, "place": ahead + 1}
        return result

    def forget(self, user_id=None):
        with self.db:
            if user_id is None:
                self.db.execute("DELETE FROM play_scores")
            else:
                self.db.execute("DELETE FROM play_scores WHERE user_id=?", (user_id,))
