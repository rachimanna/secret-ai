"""Actions in connected apps wait for the person: reading runs at once, anything that changes something asks first."""
import asyncio
import json
import re
import secrets
import time

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram.error import TelegramError

WAIT_SECONDS = 180
TRUST_SECONDS = 1800
PREFIX = "agt"
READ_WORDS = frozenset(("get", "list", "search", "find", "read", "fetch", "query", "describe", "show", "view", "retrieve",
                        "lookup", "count", "check", "browse", "whoami"))
WRITE_WORDS = frozenset(("send", "create", "update", "delete", "remove", "add", "post", "write", "set", "edit", "modify",
                         "move", "archive", "cancel", "pay", "charge", "refund", "transfer", "publish", "invite", "share",
                         "merge", "close", "reply", "run", "execute", "trigger", "upload", "assign", "approve", "book"))
REFUSALS = {
    "no": "The user declined this action. It was NOT performed. Say so and don't retry it.",
    "timeout": "The user didn't confirm in time. The action was NOT performed. Say so; they can ask again.",
    "undelivered": "The confirmation couldn't reach the user's private chat. The action was NOT performed. Ask them to open "
                   "a private chat with the bot.",
    "unattended": "This scheduled task runs without the user, so it may only read. The action was NOT performed: say what "
                  "you would have done so they can ask for it in the chat.",
}


def words(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def read_only(tool):
    """Does this MCP tool only read? The server's own annotation decides; without one, its name. When in doubt: no."""
    notes = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    if notes.get("readOnlyHint") is True:
        return True
    if notes.get("readOnlyHint") is False or notes.get("destructiveHint") is True:
        return False
    found = words(tool.get("name"))
    return bool(READ_WORDS.intersection(found)) and not WRITE_WORDS.intersection(found)


def describe(arguments, limit=500):
    try:
        text = json.dumps(arguments or {}, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        text = str(arguments)
    return text if len(text) <= limit else text[:limit] + " …"


class Approvals:
    def __init__(self, wait=WAIT_SECONDS):
        self.wait, self.waiting, self.trusted = wait, {}, {}

    def trusts(self, user_id, name, now=None):
        return self.trusted.get((user_id, name), 0) > (now if now is not None else time.monotonic())

    async def ask(self, bot, user_id, language, app, name, arguments):
        """'yes', 'no', 'timeout' or 'undelivered'. The question goes to the person's private chat."""
        if self.trusts(user_id, name):
            return "yes"
        ru, code = language == "ru", secrets.token_urlsafe(8)
        text = (f"🔐 Подтвердите действие в {app}\n\n{name}\n{describe(arguments)}\n\nБез вашего ответа ничего не выполнится."
                if ru else
                f"🔐 Confirm this action in {app}\n\n{name}\n{describe(arguments)}\n\nNothing happens without your answer.")
        keyboard = Keyboard([[Button("✅ Разрешить" if ru else "✅ Allow", callback_data=f"{PREFIX}:{code}:y"),
                              Button("❌ Отклонить" if ru else "❌ Decline", callback_data=f"{PREFIX}:{code}:n")],
                             [Button("✅ Разрешать это 30 минут" if ru else "✅ Allow this for 30 minutes",
                                     callback_data=f"{PREFIX}:{code}:t")]])
        answer = asyncio.get_running_loop().create_future()
        self.waiting[code] = {"user": user_id, "name": name, "answer": answer}
        try:
            try:
                sent = await bot.send_message(user_id, text, reply_markup=keyboard)
            except TelegramError:
                return "undelivered"
            try:
                return await asyncio.wait_for(answer, self.wait)
            except asyncio.TimeoutError:
                note = "⌛ Время вышло, действие не выполнено.\n\n" if ru else "⌛ Timed out, nothing was done.\n\n"
                try:
                    await sent.edit_text(note + text)
                except TelegramError:
                    pass
                return "timeout"
        finally:
            self.waiting.pop(code, None)

    def resolve(self, code, user_id, choice):
        """A button was pressed. Returns 'ok', 'gone' (no longer waiting) or 'foreign'."""
        item = self.waiting.get(code)
        if not item or item["answer"].done():
            return "gone"
        if item["user"] != user_id:
            return "foreign"
        if choice == "t":
            self.trusted[(user_id, item["name"])] = time.monotonic() + TRUST_SECONDS
        item["answer"].set_result("yes" if choice in ("y", "t") else "no")
        return "ok"
