"""A person's conversations, facts, credits and payments in SQLite."""
from media import FREE_PHOTOS_PER_DAY
from pathlib import Path
import automations
import daily
import playboard
import re
import sqlite3
import subscriptions
import time
import workbench

HISTORY_LIMIT = 1000  # the newest messages the model reads in full


KEEP_MESSAGES = 200_000  # kept per person ("forever"); older ones come back by search when the talk touches them


RECALL_MATCHES = 12


MAX_FACTS = 80  # lasting facts kept per person


# id: (emoji, Russian name, English name, extra instructions)
PERSONAS = {
    "street": ("😎", "Свой пацан", "Homie",
               "Style: you talk like a cool guy from the user's own chat, not like an assistant. Small talk gets one short "
               "line (often lowercase, no full stop at the end), street slang and easy humour: «ну чё», «го», «хз», «изи», "
               "«жиза», «база», «кринж», «бро» — a touch, not in every line, and never «вах»/«брат»-style accent filler. "
               "No assistant phrases («чем могу помочь», «рад помочь», «отличный вопрос»), no lists or headings for small "
               "talk, at most one emoji. Swear only if the user swears first. When they need something real (code, facts, "
               "advice, a task), do it properly and completely, just in the same relaxed tone. Asked who you are or which "
               "model: answer casually and truthfully — you're Secret AI, an AI (name the model if asked); never claim to "
               "be a human."),
    "assistant": ("✨", "Ассистент", "Assistant", ""),
    "coder": ("💻", "Программист", "Coder",
              "Act as a senior software engineer: give complete working code, explain decisions briefly and point out pitfalls."),
    "translator": ("🌍", "Переводчик", "Translator",
                   "Act as a translator: translate Russian text into English and any other language into Russian. "
                   "Preserve tone and line breaks. Output only the translation."),
    "creative": ("🎨", "Креатив", "Creative",
                 "Be bold and original: suggest unexpected ideas and write vividly."),
}


DEFAULT_PERSONA = "street"  # the relaxed "one of us" tone; /mode switches to the assistant


class Memory:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.create_function("ulower", 1, lambda text: text.lower() if isinstance(text, str) else text, deterministic=True)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS by_user ON messages(user_id, id)")
        if "created" not in [row[1] for row in self.db.execute("PRAGMA table_info(messages)")]:
            self.db.execute("ALTER TABLE messages ADD COLUMN created INTEGER")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (user_id INTEGER PRIMARY KEY, persona TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS file_context (user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, "
                        "content TEXT NOT NULL, PRIMARY KEY(user_id, chat_id))")
        if "model" not in [row[1] for row in self.db.execute("PRAGMA table_info(settings)")]:
            self.db.execute("ALTER TABLE settings ADD COLUMN model TEXT")
        # Generation: free photos per day, bought credits, and paid Stars charges (each counted once).
        # Lasting facts about a person (name, city, likes, plans…), shared by the private chat and every group.
        self.db.execute("CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, fact TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS facts_by_user ON facts(user_id, id)")
        self.db.execute("CREATE TABLE IF NOT EXISTS gen_daily (user_id INTEGER NOT NULL, day TEXT NOT NULL, photos INTEGER NOT NULL, "
                        "PRIMARY KEY (user_id, day))")
        self.db.execute("CREATE TABLE IF NOT EXISTS gen_credits (user_id INTEGER PRIMARY KEY, credits INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS gen_payments (charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
                        "credits INTEGER NOT NULL, stars INTEGER NOT NULL)")
        # Promo codes: credits for a code, a limited number of people, each person once; an optional end date.
        self.db.execute("CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY, credits INTEGER NOT NULL, "
                        "max_uses INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, expires INTEGER, created INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS promo_uses (code TEXT NOT NULL, user_id INTEGER NOT NULL, at INTEGER NOT NULL, "
                        "PRIMARY KEY (code, user_id))")
        # Invited friends: each person can be brought once, by one referrer.
        self.db.execute("CREATE TABLE IF NOT EXISTS referrals (user_id INTEGER PRIMARY KEY, referrer INTEGER NOT NULL, "
                        "at INTEGER NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS referrals_by_referrer ON referrals(referrer, at)")
        self.db.commit()
        self.workbench = workbench.Store(self.db)
        self.daily = daily.Store(self.db)
        self.subs = subscriptions.Store(self.db)
        self.automations = automations.Store(self.db)
        self.board = playboard.Board(self.db)

    def history(self, user_id, limit=HISTORY_LIMIT):
        rows = self.db.execute("SELECT id, role, content FROM (SELECT id, role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?) ORDER BY id", (user_id, limit)).fetchall()
        return [{"id": id_, "role": role, "content": content} for id_, role, content in rows]

    def append_turn(self, user_id, question, answer):
        now = int(time.time())
        with self.db:
            self.db.executemany("INSERT INTO messages(user_id, role, content, created) VALUES (?, ?, ?, ?)",
                                [(user_id, "user", question, now), (user_id, "assistant", answer, now)])
            self.db.execute("DELETE FROM messages WHERE user_id=? AND id <= (SELECT id FROM messages WHERE user_id=? "
                            "ORDER BY id DESC LIMIT 1 OFFSET ?)", (user_id, user_id, KEEP_MESSAGES))

    def recall(self, user_id, text, before_id):
        """Old messages (older than before_id, i.e. no longer in the model's view) about the same things as text:
        the matching messages with their question/answer partner, oldest first: [(created, role, content)]."""
        words = list(dict.fromkeys(w for w in re.findall(r"\w{4,}", (text or "").lower()) if not w.isdigit()))[:8]
        if not words or not before_id:
            return []
        stems = [w[:max(4, len(w) - 2)] for w in words]  # Russian endings vary: "айлы", "айле" -> "айл"
        variants = list(dict.fromkeys(v for s in stems for v in (s, s.capitalize())))  # fast LIKE: case only for Latin
        rows = self.db.execute("SELECT id, role, content FROM messages WHERE user_id=? AND id < ? AND ("
                               + " OR ".join("content LIKE ?" for _ in variants) + ") ORDER BY id DESC LIMIT 3000",
                               (user_id, before_id, *[f"%{v}%" for v in variants])).fetchall()
        best = sorted(rows, key=lambda r: (-sum(s in r[2].lower() for s in stems), -r[0]))[:RECALL_MATCHES]
        ids = sorted({i for id_, role, _ in best for i in (id_, id_ + 1 if role == "user" else id_ - 1)})
        if not ids:
            return []
        found = self.db.execute(f"SELECT id, created, role, content FROM messages WHERE user_id=? AND id < ? AND id IN "
                                f"({','.join('?' * len(ids))}) ORDER BY id", (user_id, before_id, *ids)).fetchall()
        return [(created, role, content) for _, created, role, content in found]

    def annotate(self, user_id, note):
        """Add a note to the bot's latest answer (e.g. that a video was sent): the history then remembers it for good,
        restarts included, and the roles still alternate."""
        with self.db:
            self.db.execute("UPDATE messages SET content = content || ? WHERE id = (SELECT MAX(id) FROM messages "
                            "WHERE user_id=? AND role='assistant')", ("\n" + note, user_id))

    def save_document(self, user_id, chat_id, document):
        return self.workbench.add(user_id, chat_id, document)

    def document(self, user_id, chat_id):
        item = self.workbench.file(user_id, chat_id)
        return item['document'] if item else None

    def _last_turn(self, user_id):
        rows = self.db.execute("SELECT id, role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 2", (user_id,)).fetchall()
        if len(rows) < 2 or rows[0][1] != "assistant" or rows[1][1] != "user":
            return None
        return rows

    def last_question(self, user_id):
        rows = self._last_turn(user_id)
        return rows[1][2] if rows else None

    def pop_last_turn(self, user_id):
        """Remove the latest question/answer pair and return the question, or None."""
        rows = self._last_turn(user_id)
        if not rows:
            return None
        with self.db:
            self.db.execute("DELETE FROM messages WHERE id IN (?, ?)", (rows[0][0], rows[1][0]))
        return rows[1][2]

    def facts(self, user_id):
        return [row[0] for row in self.db.execute("SELECT fact FROM facts WHERE user_id=? ORDER BY id", (user_id,))]

    def remember(self, user_id, add=(), forget=()):
        """Add facts (no duplicates) and drop the ones containing any of the forget phrases; keeps the newest MAX_FACTS."""
        def fold(text):  # in Python: SQLite's lower() leaves Cyrillic as it is ("Берлине" never matched "берлине")
            return text.casefold().replace("ё", "е")
        with self.db:
            phrases = [fold(p.strip()) for p in forget if p.strip()]
            if phrases:
                rows = self.db.execute("SELECT id, fact FROM facts WHERE user_id=?", (user_id,)).fetchall()
                gone = [(row_id,) for row_id, fact in rows if any(p in fold(fact) for p in phrases)]
                self.db.executemany("DELETE FROM facts WHERE id=?", gone)
            known = {f.casefold() for f in self.facts(user_id)}
            for fact in add:
                fact = " ".join(fact.split())[:300]
                if fact and fact.casefold() not in known:
                    self.db.execute("INSERT INTO facts(user_id, fact) VALUES (?, ?)", (user_id, fact))
                    known.add(fact.casefold())
            self.db.execute("DELETE FROM facts WHERE user_id=? AND id NOT IN (SELECT id FROM facts WHERE user_id=? "
                            "ORDER BY id DESC LIMIT ?)", (user_id, user_id, MAX_FACTS))

    def reset(self, user_id):
        """Forget this person: the conversation and the facts remembered about them (name, city…)."""
        with self.db:
            self.db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM facts WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM file_context WHERE user_id=?", (user_id,))
        self.workbench.reset(user_id)
        self.daily.reset(user_id)
        self.automations.reset(user_id)
        self.board.forget(user_id)

    def persona(self, user_id):
        row = self.db.execute("SELECT persona FROM settings WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row and row[0] in PERSONAS else DEFAULT_PERSONA

    def set_persona(self, user_id, persona):
        with self.db:
            self.db.execute("INSERT INTO settings(user_id, persona) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET persona=excluded.persona", (user_id, persona))

    def model(self, user_id):
        row = self.db.execute("SELECT model FROM settings WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None

    def set_model(self, user_id, model):
        with self.db:
            self.db.execute("INSERT INTO settings(user_id, persona, model) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET model=excluded.model",
                            (user_id, DEFAULT_PERSONA, model))

    def count(self, user_id):
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,)).fetchone()[0]

    # ----- generation quota -----

    @staticmethod
    def _today():
        return time.strftime("%Y-%m-%d", time.gmtime())

    def promo_create(self, code, credits, max_uses, days=None):
        """A new code (or the same code replaced); max_uses 0 = no limit."""
        now = int(time.time())
        with self.db:
            self.db.execute("INSERT INTO promo_codes(code, credits, max_uses, used, expires, created) VALUES (?, ?, ?, 0, ?, ?) "
                            "ON CONFLICT(code) DO UPDATE SET credits=excluded.credits, max_uses=excluded.max_uses, "
                            "expires=excluded.expires", (code, credits, max_uses, now + days * 86400 if days else None, now))

    def promo_redeem(self, user_id, code):
        """('ok', credits) or (reason, 0): 'unknown', 'expired', 'empty', 'used'. Atomic: never twice, never over the limit."""
        now = int(time.time())
        with self.db:
            row = self.db.execute("SELECT credits, max_uses, used, expires FROM promo_codes WHERE code=?", (code,)).fetchone()
            if not row:
                return "unknown", 0
            credits, max_uses, used, expires = row
            if expires and expires < now:
                return "expired", 0
            if self.db.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, user_id)).fetchone():
                return "used", 0
            taken = self.db.execute("UPDATE promo_codes SET used=used+1 WHERE code=? AND (max_uses=0 OR used<max_uses)", (code,))
            if not taken.rowcount:
                return "empty", 0
            self.db.execute("INSERT INTO promo_uses(code, user_id, at) VALUES (?, ?, ?)", (code, user_id, now))
            self._add_credits(user_id, credits)
        return "ok", credits

    def promo_list(self):
        return self.db.execute("SELECT code, credits, max_uses, used, expires FROM promo_codes ORDER BY created DESC").fetchall()

    def promo_delete(self, code):
        with self.db:
            return self.db.execute("DELETE FROM promo_codes WHERE code=?", (code,)).rowcount > 0

    def gen_info(self, user_id):
        """(free photos left today, credits)."""
        used = self.db.execute("SELECT photos FROM gen_daily WHERE user_id=? AND day=?", (user_id, self._today())).fetchone()
        credits = self.db.execute("SELECT credits FROM gen_credits WHERE user_id=?", (user_id,)).fetchone()
        return max(0, FREE_PHOTOS_PER_DAY - (used[0] if used else 0)), credits[0] if credits else 0

    def gen_charge(self, user_id, kind, cost):
        """Take one generation: a free photo first, then credits. Returns 'free', 'credits' or None (not enough)."""
        free, credits = self.gen_info(user_id)
        with self.db:
            if kind == "photo" and free > 0:
                self.db.execute("INSERT INTO gen_daily(user_id, day, photos) VALUES (?, ?, 1) "
                                "ON CONFLICT(user_id, day) DO UPDATE SET photos=photos+1", (user_id, self._today()))
                return "free"
            if credits >= cost:
                self.db.execute("UPDATE gen_credits SET credits=credits-? WHERE user_id=?", (cost, user_id))
                return "credits"
        return None

    def add_referral(self, user_id, referrer, credits):
        """Count a new person brought by referrer and give both of them credits; False if it doesn't count (oneself,
        someone who already talked to the bot or was already brought, or the referrer's daily limit)."""
        now = int(time.time())
        with self.db:
            if user_id == referrer or self.db.execute(
                    "SELECT 1 FROM messages WHERE user_id=? UNION SELECT 1 FROM referrals WHERE user_id=?",
                    (user_id, user_id)).fetchone():
                return False
            today = self.db.execute("SELECT COUNT(*) FROM referrals WHERE referrer=? AND at>?", (referrer, now - 86400)).fetchone()[0]
            if today >= REFERRALS_PER_DAY:
                return False
            self.db.execute("INSERT INTO referrals(user_id, referrer, at) VALUES (?, ?, ?)", (user_id, referrer, now))
            self._add_credits(user_id, credits)
            self._add_credits(referrer, credits)
        return True

    def referral_count(self, referrer):
        return self.db.execute("SELECT COUNT(*) FROM referrals WHERE referrer=?", (referrer,)).fetchone()[0]

    def gen_refund(self, user_id, how, cost):
        with self.db:
            if how == "free":
                self.db.execute("UPDATE gen_daily SET photos=MAX(0, photos-1) WHERE user_id=? AND day=?", (user_id, self._today()))
            elif how == "credits":
                self._add_credits(user_id, cost)

    def _add_credits(self, user_id, credits):
        self.db.execute("INSERT INTO gen_credits(user_id, credits) VALUES (?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET credits=credits+excluded.credits", (user_id, credits))

    def gen_paid(self, charge_id, user_id, credits, stars):
        """Credit a Stars payment once; returns False if this charge was already counted."""
        with self.db:
            if self.db.execute("SELECT 1 FROM gen_payments WHERE charge_id=?", (charge_id,)).fetchone():
                return False
            self.db.execute("INSERT INTO gen_payments(charge_id, user_id, credits, stars) VALUES (?, ?, ?, ?)",
                            (charge_id, user_id, credits, stars))
            self._add_credits(user_id, credits)
        return True

    def close(self):
        self.db.close()


REFERRALS_PER_DAY = 20  # per referrer: a farm of fake accounts stops quickly
