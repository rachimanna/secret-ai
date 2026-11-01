"""Optional anti-spam for groups. Off until an admin turns it on with /antispam on; admins are never touched."""
import logging
import re
import time
from collections import defaultdict, deque

from telegram.error import TelegramError

LOG = logging.getLogger(__name__)

COMMAND = "antispam"
FLOOD_MESSAGES, FLOOD_SECONDS = 6, 10
TRUSTED_AFTER = 5  # ordinary messages after which someone may post links
LINK = re.compile(r"(?i)(?:https?://|www\.|t\.me/|telegram\.me/|\b[\w-]+\.(?:com|ru|net|org|io|me|xyz|top|site|online|shop)\b)")
ADMIN_SECONDS = 600
NOTICE_SECONDS = 60
TELEGRAM_SERVICE = 777000


def has_link(message):
    text = message.text or message.caption or ""
    entities = list(message.entities or ()) + list(message.caption_entities or ())
    return bool(LINK.search(text)) or any(e.type in ("url", "text_link") for e in entities)


class Moderation:
    def __init__(self, db):
        self.db = db
        self.recent, self.admins, self.noticed = defaultdict(deque), {}, {}
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS mod_groups (chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS mod_seen (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                            "messages INTEGER NOT NULL, PRIMARY KEY(chat_id, user_id))")

    def enabled(self, chat_id):
        row = self.db.execute("SELECT enabled FROM mod_groups WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row and row[0])

    def set_enabled(self, chat_id, on):
        with self.db:
            self.db.execute("INSERT INTO mod_groups(chat_id, enabled) VALUES (?, ?) ON CONFLICT(chat_id) "
                            "DO UPDATE SET enabled=excluded.enabled", (chat_id, int(on)))

    def verdict(self, chat_id, user_id, message, now=None):
        """None, 'flood' or 'link'. Pure bookkeeping: nothing is deleted here."""
        now = now if now is not None else time.monotonic()
        recent = self.recent[(chat_id, user_id)]
        recent.append(now)
        while recent and now - recent[0] > FLOOD_SECONDS:
            recent.popleft()
        if len(recent) > FLOOD_MESSAGES:
            return "flood"
        row = self.db.execute("SELECT messages FROM mod_seen WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        if (row[0] if row else 0) < TRUSTED_AFTER and has_link(message):
            return "link"
        with self.db:
            self.db.execute("INSERT INTO mod_seen(chat_id, user_id, messages) VALUES (?, ?, 1) ON CONFLICT(chat_id, user_id) "
                            "DO UPDATE SET messages=messages+1", (chat_id, user_id))
        return None

    async def is_admin(self, bot, chat_id, user_id, now=None):
        now = now if now is not None else time.monotonic()
        known = self.admins.get((chat_id, user_id))
        if known and known[0] > now:
            return known[1]
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except TelegramError:
            return True  # can't tell: leave the person alone
        answer = member.status in ("administrator", "creator")
        if len(self.admins) > 5000:
            self.admins.clear()
        self.admins[(chat_id, user_id)] = (now + ADMIN_SECONDS, answer)
        return answer

    async def check(self, bot, message, user):
        """True if the message was spam and has been removed: the bot does nothing else with it."""
        chat_id = message.chat_id
        if not self.enabled(chat_id) or user.id == TELEGRAM_SERVICE or getattr(message, "is_automatic_forward", False):
            return False  # a linked channel's posts arrive from Telegram itself
        reason = self.verdict(chat_id, user.id, message)
        if not reason or await self.is_admin(bot, chat_id, user.id):
            return False
        try:
            await message.delete()
        except TelegramError as exc:  # no right to delete: the group keeps working as before
            LOG.info("Anti-spam couldn't delete a message: %s", type(exc).__name__)
            return False
        now = time.monotonic()
        if now - self.noticed.get(chat_id, float("-inf")) > NOTICE_SECONDS:
            self.noticed[chat_id] = now
            why = "слишком много сообщений подряд" if reason == "flood" else "ссылки можно присылать после нескольких обычных сообщений"
            try:
                await bot.send_message(chat_id, f"🛡 Сообщение от {user.first_name or 'участника'} удалено: {why}.")
            except TelegramError:
                pass
        return True
