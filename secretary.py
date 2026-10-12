"""AI secretary through Telegram Business: answers people in the owner's personal chats while the owner is away.

The owner connects the bot in Telegram → Settings → Telegram Business → Chatbots and tells the bot in its private chat
how to answer ("reply to everyone: hi, I'm busy, I'll answer later, this is an AI"). Settings live in the encrypted store.
"""
import time

OWNER_ACTIVE = 10 * 60  # after the owner writes in a chat, the secretary stays quiet there for 10 minutes
MIN_GAP = 20  # seconds between two replies in one chat (no reply storms)
HISTORY = 14  # recent lines of a chat the secretary sees

TOOL = {"type": "function", "name": "secretary_settings",
        "description": ("Turn the user's AI secretary on or off and set how it answers people who write to the user in "
                        "their personal Telegram chats (Telegram Business) while the user is away. Use it when the user asks "
                        "to answer people for them, e.g. 'отвечай всем: привет, я занят', 'выключи секретаря'."),
        "parameters": {"type": "object", "properties": {
            "enabled": {"type": "boolean"},
            "rules": {"type": "string", "description": "how to answer, in the user's own words (keep exact phrases they want said)"},
            "notify": {"type": "boolean", "description": "send the user a short note after each answer"}},
            "required": ["enabled"], "additionalProperties": False}}

SETUP_RU = ("Чтобы секретарь заработал, подключи бота один раз: Telegram → Настройки → Telegram для бизнеса → Чат-боты → "
            "выбери этого бота, отметь чаты и разреши «Отвечать на сообщения».")
SETUP_EN = ("To make the secretary work, connect the bot once: Telegram → Settings → Telegram Business → Chatbots → pick this "
            "bot, choose the chats and allow it to reply to messages.")


def settings(store, user_id):
    return store.get_meta(f"secretary:{user_id}") or {"enabled": False, "rules": "", "notify": True}


def save_settings(store, user_id, value):
    store.set_meta(f"secretary:{user_id}", value)


def connection(store, connection_id):
    return store.get_meta(f"bizconn:{connection_id}")


def can_reply(business_connection):
    """Bot API 9 moved can_reply into rights; older servers send can_reply."""
    rights = getattr(business_connection, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(business_connection, "can_reply", False) or business_connection.api_kwargs.get("can_reply"))


def save_connection(store, business_connection):
    value = {"user_id": business_connection.user.id, "chat_id": business_connection.user_chat_id,
             "name": business_connection.user.first_name or "the owner",
             "enabled": bool(business_connection.is_enabled), "can_reply": can_reply(business_connection)}
    store.set_meta(f"bizconn:{business_connection.id}", value)
    store.set_meta(f"biz:{business_connection.user.id}", business_connection.id)
    return value


def status_text(store, user_id, ru):
    """One line for the model: is the secretary connected, on, and with which rules."""
    conn_id = store.get_meta(f"biz:{user_id}")
    conn = connection(store, conn_id) if conn_id else None
    current = settings(store, user_id)
    connected = bool(conn and conn["enabled"] and conn["can_reply"])
    return (f"AI secretary: Telegram Business connection {'ready' if connected else 'NOT set up'}; "
            f"secretary {'ON' if current['enabled'] else 'off'}; rules: «{current['rules'] or 'none'}»; "
            f"notifications {'on' if current.get('notify', True) else 'off'}." + ("" if connected else " " + (SETUP_RU if ru else SETUP_EN)))


def instructions(owner_name, rules):
    return (
        f"You are Secret AI, the AI secretary of {owner_name}. You answer in {owner_name}'s personal Telegram chats on their "
        f"behalf while they are away. {owner_name}'s instructions: «{rules}». Follow them exactly: say what they asked, in "
        "their tone; keep exact phrases they wrote. Answer in the language of the person writing, short and natural, like a "
        "message. Never promise anything for the owner beyond the instructions, never agree to payments, meetings or "
        "favours, never share private information, never pretend to be the owner in person. If you already answered this "
        "person recently and their new message doesn't need another answer (thanks, ok, emoji), reply exactly SKIP.")


class Activity:
    """When the owner last wrote and when the secretary last answered, per chat (in memory)."""

    def __init__(self):
        self.owner_seen, self.answered, self.lines = {}, {}, {}

    def log(self, chat_id, line):
        lines = self.lines.setdefault(chat_id, [])
        lines.append(line[:600])
        del lines[:-HISTORY]

    def owner_wrote(self, chat_id):
        self.owner_seen[chat_id] = time.monotonic()

    def may_answer(self, chat_id):
        now = time.monotonic()
        return now - self.owner_seen.get(chat_id, -1e9) > OWNER_ACTIVE and now - self.answered.get(chat_id, -1e9) > MIN_GAP

    def answered_now(self, chat_id):
        self.answered[chat_id] = time.monotonic()
