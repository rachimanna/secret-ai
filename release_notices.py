"""Versioned release announcements; SQLite progress shares the bot's existing backups."""
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

LOG = logging.getLogger(__name__)
MANIFEST = Path(__file__).with_name("release_notes.json")
LEASE_SECONDS = 120
MAX_TRIES = 5


def load_release(path=MANIFEST):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if (type(value.get("version")) is not int or value["version"] <= 0
            or not isinstance(value.get("text"), str) or not value["text"].strip()
            or len(value["text"].encode("utf-16-le")) // 2 > 3900):
        raise ValueError("Release needs an increasing positive version and 1–3900 UTF-16 units of plain text")
    return value


class Store:
    def __init__(self, db):
        self.db = db
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS release_chats (chat_id INTEGER PRIMARY KEY)")
            db.execute("CREATE TABLE IF NOT EXISTS release_batches (version INTEGER PRIMARY KEY, text TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS release_deliveries (version INTEGER NOT NULL, chat_id INTEGER NOT NULL, "
                       "status TEXT NOT NULL DEFAULT 'pending', tries INTEGER NOT NULL DEFAULT 0, "
                       "due REAL NOT NULL DEFAULT 0, token TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', "
                       "PRIMARY KEY(version,chat_id))")
            db.execute("CREATE INDEX IF NOT EXISTS release_due ON release_deliveries(version,status,due)")

    def remember(self, chats):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO release_chats(chat_id) VALUES (?)",
                                [(c,) for c in set(chats) if type(c) is int and c])

    def seed(self):
        # Existing broadcast audience plus /start-only users. Never DM group_members.
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        chats = set()
        for table in ("messages", "settings"):
            if table in tables:
                chats.update(r[0] for r in self.db.execute(f"SELECT DISTINCT user_id FROM {table}") if r[0] > 0)
        if "bot_meta" in tables:
            for (key,) in self.db.execute("SELECT key FROM bot_meta WHERE key LIKE 'welcomed:%'"):
                user = key.partition(":")[2]
                if user.isdigit() and int(user) > 0:
                    chats.add(int(user))
        if "group_info" in tables:
            chats.update(r[0] for r in self.db.execute("SELECT chat_id FROM group_info") if r[0] < 0)
        self.remember(chats)

    def stage(self, release, allowed=()):
        version, text = release["version"], release["text"]
        with self.db:
            # Acquire the SQLite write lock before checking/staging (also safe for two workers).
            self.db.execute("UPDATE release_batches SET version=version WHERE 0")
            latest = self.db.execute("SELECT MAX(version) FROM release_batches").fetchone()[0]
            existing = self.db.execute("SELECT text FROM release_batches WHERE version=?", (version,)).fetchone()
            if existing and existing[0] != text:
                raise ValueError("Published release text is immutable; increment version")
            if latest is not None and version <= latest:
                return False  # restart or rollback: no duplicate batch or historical announcements
            self.db.execute("INSERT INTO release_batches(version,text) VALUES (?,?)", (version,text))
            self.db.execute("UPDATE release_deliveries SET status='superseded',token='' WHERE status='pending'")
            recipients = [r[0] for r in self.db.execute("SELECT chat_id FROM release_chats")
                          if r[0] < 0 or not allowed or r[0] in allowed]
            self.db.executemany("INSERT INTO release_deliveries(version,chat_id) VALUES (?,?)",
                                [(version,c) for c in recipients])
        return True

    def claim(self, version, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("UPDATE release_deliveries SET status='failed',token='' "
                            "WHERE version=? AND status='pending' AND due<=? AND tries>=?",
                            (version,now,MAX_TRIES))
            row = self.db.execute("SELECT chat_id,tries FROM release_deliveries "
                                  "WHERE version=? AND status='pending' AND due<=? ORDER BY due,chat_id LIMIT 1",
                                  (version,now)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            changed = self.db.execute("UPDATE release_deliveries SET token=?,due=?,tries=tries+1 "
                                      "WHERE version=? AND chat_id=? AND status='pending' AND due<=?",
                                      (token,now+LEASE_SECONDS,version,row[0],now)).rowcount
        return (version,row[0],token,row[1]+1) if changed else None

    def finish(self, job, status, error="", delay=0, rate_limited=False):
        version, chat, token, _ = job
        with self.db:
            self.db.execute("UPDATE release_deliveries SET status=?,error=?,due=?,token='',"
                            "tries=MAX(0,tries-?) WHERE version=? AND chat_id=? AND token=? AND status='pending'",
                            (status,error,time.time()+delay,int(rate_limited),version,chat,token))

    def migrate(self, job, new_chat):
        version, old_chat, token, _ = job
        with self.db:
            changed = self.db.execute("UPDATE release_deliveries SET status='migrated',token='' "
                                      "WHERE version=? AND chat_id=? AND token=? AND status='pending'",
                                      (version,old_chat,token)).rowcount
            if changed:
                self.db.execute("DELETE FROM release_chats WHERE chat_id=?", (old_chat,))
                self.db.execute("INSERT OR IGNORE INTO release_chats VALUES (?)", (new_chat,))
                self.db.execute("INSERT OR IGNORE INTO release_deliveries(version,chat_id) VALUES (?,?)",
                                (version,new_chat))


async def track(update, context):
    """Record reachable chat IDs, including /start-only chats, without retaining message text."""
    data = context.bot_data
    store, chat, user = data.get("release_notices"), update.effective_chat, update.effective_user
    if (not store or not chat or not user or user.is_bot or not update.effective_message
            or update.business_message or update.business_connection
            or chat.type not in ("private","group","supergroup")
            or (data.get("allowed") and user.id not in data["allowed"])):
        return
    store.remember([chat.id])


class Worker:
    def __init__(self, app, *, is_test_bot, test_mode, rate=0.1):
        self.app, self.is_test_bot, self.test_mode, self.rate = app, is_test_bot, test_mode, rate
        self.release = None
        self.prepared = False

    async def checkpoint(self):
        snapshots = self.app.bot_data.get("snapshots")
        if snapshots:
            await asyncio.to_thread(snapshots.save)

    async def tick(self):
        from telegram.error import BadRequest, ChatMigrated, Forbidden, RetryAfter, TelegramError
        data = self.app.bot_data
        if self.is_test_bot() or self.test_mode(data):
            return
        if self.release is None:
            self.release = load_release()
        store, release = data["release_notices"], self.release
        if not self.prepared:
            store.seed()
            store.stage(release, data.get("allowed") or ())
            # Save the batch before delivery; don't send if its checkpoint failed.
            await self.checkpoint()
            self.prepared = True
        for _ in range(20):
            if self.is_test_bot() or self.test_mode(data):
                break
            job = store.claim(release["version"])
            if not job:
                break
            chat = job[1]
            if chat > 0 and data.get("allowed") and chat not in data["allowed"]:
                store.finish(job,"skipped","restricted")
                continue
            rate_wait = 0
            try:
                await self.app.bot.send_message(chat_id=chat,text=release["text"],parse_mode="",
                                                disable_web_page_preview=True,
                                                read_timeout=20,write_timeout=10,connect_timeout=10,pool_timeout=10)
            except RetryAfter as exc:
                delay = exc.retry_after
                rate_wait = (delay.total_seconds() if hasattr(delay,"total_seconds") else float(delay)) + 1
                store.finish(job,"pending","RetryAfter",delay=rate_wait,rate_limited=True)
            except ChatMigrated as exc:
                store.migrate(job,exc.new_chat_id)
            except (Forbidden, BadRequest) as exc:
                store.finish(job,"skipped",type(exc).__name__)
            except TelegramError as exc:
                store.finish(job,"pending",type(exc).__name__,delay=min(1800,30*2**(job[3]-1)))
            else:
                store.finish(job,"sent")
            # Keep delivery progress in the same durable snapshot as conversations.
            await self.checkpoint()
            if rate_wait:
                await asyncio.sleep(rate_wait)  # Telegram's limit applies to the whole bot, not just this chat
                break
            await asyncio.sleep(self.rate)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                LOG.warning("Release announcements paused: %s",type(exc).__name__)
            await asyncio.sleep(20)
