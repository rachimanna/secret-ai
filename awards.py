"""Weekly chat awards in groups: every week a card with funny nominations for the group's members.

Numbers come from the group's own messages (who wrote most, who writes at night, who dropped the most
links/stickers, the quietest "ghost"). The model turns them into a short funny write-up. People love being
nominated — it spreads by screenshots and keeps groups coming back.

/awards posts it right away; "/awards off" / "/awards on" (group admins) turn the weekly post off and on.
The scheduler fires once a week (Sunday 20:00 Moscow) in each active group that opted in.
"""
import asyncio
import collections
import datetime
import io
import logging
import re

from PIL import ImageDraw
from telegram.error import BadRequest

import quizart
from quizart import backdrop, font, label, panel

LOG = logging.getLogger("secret_ai.awards")
MSK = datetime.timezone(datetime.timedelta(hours=3))
WEEKDAY = 6  # Sunday (Monday is 0)
HOUR = 20  # Moscow time
MIN_MESSAGES = 40  # a quiet week gets no weekly post
COMMAND = "awards"
BOT_NAME = "Secret AI"
WEEK = 7 * 86400

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
          "декабря"]

# Fixed, computable nominations: (emoji, title, how it's measured). The model only writes the intro and a couple
# of soft extra lines; medals and the "ghost" are decided from the numbers so nobody is invented.
NOMINATIONS = [
    ("🏆", "Болтун недели", "most messages"),
    ("🌙", "Ночной житель", "most messages between 00:00 and 06:00"),
    ("🔗", "Поставщик контента", "most links, stickers and media"),
    ("📢", "Главный по буквам", "longest total text"),
    ("👻", "Призрак недели", "wrote the least (but at least once)"),
]


class Awards:
    def __init__(self, store, ask_model):
        """store: the GroupStore; ask_model(chat_id, instructions, text) -> answer text."""
        self.store, self.db, self.ask_model = store, store.db, ask_model
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS group_awards (chat_id INTEGER PRIMARY KEY, "
                            "enabled INTEGER NOT NULL DEFAULT 1, last_week TEXT NOT NULL DEFAULT '', "
                            "retry_at INTEGER NOT NULL DEFAULT 0)")

    # ---------- settings ----------

    def enabled(self, chat_id):
        row = self.db.execute("SELECT enabled FROM group_awards WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row[0]) if row else True

    def set_enabled(self, chat_id, on):
        with self.db:
            self.db.execute("INSERT INTO group_awards(chat_id, enabled) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                            "enabled=excluded.enabled", (chat_id, int(on)))

    def _mark(self, chat_id, week):
        with self.db:
            self.db.execute("INSERT INTO group_awards(chat_id, last_week) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                            "last_week=excluded.last_week", (chat_id, week))

    def _due(self, week):
        """Groups busy enough this week that haven't had this week's awards and didn't turn them off."""
        since = int(datetime.datetime.now(MSK).timestamp()) - WEEK
        rows = self.db.execute("SELECT m.chat_id, COUNT(*) FROM group_messages m LEFT JOIN group_awards a USING(chat_id) "
                               "WHERE m.created >= ? AND m.line NOT LIKE ? AND COALESCE(a.enabled, 1) = 1 AND "
                               "COALESCE(a.last_week, '') != ? AND COALESCE(a.retry_at, 0) <= ? "
                               "GROUP BY m.chat_id HAVING COUNT(*) >= ?",
                               (since, BOT_NAME + ":%", week, since + WEEK, MIN_MESSAGES)).fetchall()
        return [row[0] for row in rows]

    # ---------- numbers ----------

    def week_lines(self, chat_id):
        since = int(datetime.datetime.now(MSK).timestamp()) - WEEK
        return self.db.execute("SELECT created, line FROM group_messages WHERE chat_id=? AND created >= ? AND line NOT LIKE ? "
                               "ORDER BY id", (chat_id, since, BOT_NAME + ":%")).fetchall()

    @staticmethod
    def _name(line):
        return line.split(":", 1)[0].strip() if ":" in line else "?"

    @staticmethod
    def _body(line):
        return line.split(":", 1)[1].strip() if ":" in line else line

    def tally(self, rows):
        """Per-member counters and the winners of each computable nomination."""
        messages = collections.Counter()
        night = collections.Counter()
        content = collections.Counter()  # links + stickers/media markers
        letters = collections.Counter()
        for created, line in rows:
            name = self._name(line)
            if name in ("?", BOT_NAME):
                continue
            body = self._body(line)
            messages[name] += 1
            letters[name] += len(body)
            hour = datetime.datetime.fromtimestamp(created, MSK).hour
            if hour < 6:
                night[name] += 1
            if "http" in body.lower() or body.startswith("[") or "sticker" in body.lower() or "стикер" in body.lower():
                content[name] += 1

        def top(counter):
            best = counter.most_common(1)
            return best[0] if best else None

        # "Ghost": wrote at least once but the fewest, and isn't the top talker.
        ghost = None
        if len(messages) >= 3:
            quiet = sorted(messages.items(), key=lambda kv: kv[1])
            if quiet and quiet[0][0] != (top(messages) or (None,))[0]:
                ghost = quiet[0]

        return {
            "messages": messages,
            "winners": [
                ("🏆", "Болтун недели", top(messages)),
                ("🌙", "Ночной житель", top(night)),
                ("🔗", "Поставщик контента", top(content)),
                ("📢", "Главный по буквам", top(letters)),
                ("👻", "Призрак недели", ghost),
            ],
            "total": len(rows),
            "people": len(messages),
            "top": messages.most_common(5),
        }

    # ---------- posting ----------

    async def post(self, bot, chat_id, title, manual=False):
        rows = self.week_lines(chat_id)
        if len(rows) < (10 if manual else MIN_MESSAGES):
            if manual:
                await bot.send_message(chat_id, "За неделю тут писали слишком мало — награждать пока некого 😴")
            return
        numbers = self.tally(rows)
        if not numbers["people"]:
            if manual:
                await bot.send_message(chat_id, "Не вижу участников за неделю — награждать некого 😴")
            return
        card = await asyncio.to_thread(render, numbers, title or "Группа", datetime.datetime.now(MSK).date())
        try:
            text = await self.write_up(chat_id, rows, numbers)
        except Exception as exc:  # the card alone is still the awards
            LOG.warning("Awards text failed: %s %s", type(exc).__name__, str(exc)[:200])
            text = ""
        caption = text if len(text) <= 1000 else ""
        try:
            await bot.send_photo(chat_id, card, caption=caption or None, parse_mode="HTML" if caption else None)
        except BadRequest:
            await bot.send_photo(chat_id, card, caption=re.sub(r"<[^>]+>", "", caption)[:1000] or None)
        if text and not caption:
            try:
                await bot.send_message(chat_id, text[:4000], parse_mode="HTML")
            except BadRequest:
                await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text)[:4000])
        self.store.add_line(chat_id, f"{BOT_NAME}: [премии недели] " + re.sub(r"<[^>]+>", "", text)[:400])
        self._mark(chat_id, week_key())

    async def write_up(self, chat_id, rows, numbers):
        lines, size = [], 0
        for _, line in reversed(rows):
            size += len(line)
            if size > 60_000:
                break
            lines.append(line[:400])
        lines.reverse()
        won = "; ".join(f"{title}: {w[0]} ({w[1]})" for emoji, title, w in numbers["winners"] if w)
        instructions = (
            "Ты — Secret AI, свой человек в этом групповом чате. Объяви «Премии недели» по сообщениям ниже. Пиши "
            "по-русски, весело и по-дружески, как друг из этой компании, а не как отчёт. Формат — Telegram HTML "
            "(только <b>, <i>), без markdown, не больше 650 знаков:\n"
            "<b>🎉 Премии недели</b> — одно-два живых предложения о том, как прошла неделя.\n"
            "Потом коротко обыграй уже посчитанных победителей (имена и номинации не меняй):\n"
            f"{won}\n"
            "Для каждого — одна весёлая строка с эмодзи номинации, почему он её заслужил (по реальным сообщениям). "
            "Можно добавить одну-две свои шуточные мини-номинации, если для них правда есть материал в чате. "
            "Без оскорблений, без пошлых и личных подробностей, по-доброму, подкалывай, но не зло.")
        return (await self.ask_model(chat_id, instructions, "Сообщения за неделю:\n" + "\n".join(lines))).strip()

    async def run(self, app):
        """Every minute: on the award weekday/hour (Moscow), post once a week in each active group."""
        while True:
            await asyncio.sleep(60)
            now = datetime.datetime.now(MSK)
            if now.weekday() != WEEKDAY or now.hour != HOUR:
                continue
            week = week_key()
            for chat_id in self._due(week):
                title = (self.db.execute("SELECT title FROM group_info WHERE chat_id=?", (chat_id,)).fetchone() or [""])[0]
                try:
                    await self.post(app.bot, chat_id, title)
                except Exception as exc:  # removed from the group, no rights to post…
                    LOG.warning("Awards failed in %s: %s", chat_id, type(exc).__name__)
                    with self.db:
                        self.db.execute("INSERT INTO group_awards(chat_id, retry_at) VALUES (?, ?) "
                                        "ON CONFLICT(chat_id) DO UPDATE SET retry_at=excluded.retry_at",
                                        (chat_id, int(now.timestamp()) + 600))
                await asyncio.sleep(3)


def week_key():
    """ISO year-week, e.g. '2026-W42': one awards post per calendar week."""
    now = datetime.datetime.now(MSK)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


# ---------- the card ----------

def render(numbers, title, day):
    winners = [(emoji, title_, w) for emoji, title_, w in numbers["winners"] if w]
    height = 430 + len(winners) * 150
    width = 1080
    card = backdrop(width, height)
    label(card, "ПРЕМИИ НЕДЕЛИ", 88, (255, 255, 255), width / 2, 110, shadow=True)
    sub = quizart.clean(title)
    while font(38, False).getlength(sub) > 860 and len(sub) > 2:
        sub = sub.rstrip("…")[:-1].rstrip() + "…"
    label(card, sub, 38, (205, 205, 255), width / 2, 190, bold=False)
    label(card, f"неделя до {day.day} {MONTHS[day.month - 1]}", 30, (170, 170, 220), width / 2, 240, bold=False)

    tiles = [(str(numbers["total"]), "сообщений", (90, 80, 255)),
             (str(numbers["people"]), "номинантов", (217, 70, 239))]
    x = 230
    for value, caption, color in tiles:
        panel(card, (x, 285, x + 300, 400), 30, color, 0.85)
        label(card, value, 60, (255, 255, 255), x + 150, 335)
        label(card, caption, 26, (235, 235, 255), x + 150, 388, bold=False)
        x += 340

    y = 450
    for place, (emoji, nom_title, winner) in enumerate(winners):
        name, count = winner
        color = quizart.MEDALS[place] if place < 3 else (150, 140, 255)
        panel(card, (70, y, 1010, y + 128), 28, (255, 255, 255), 0.10)
        panel(card, (70, y, 90, y + 128), 28, color, 0.9)
        short = quizart.clean(name) or "?"
        while font(44).getlength(short) > 820 and len(short) > 2:
            short = short.rstrip("…")[:-1].rstrip() + "…"
        label(card, quizart.clean(emoji + " " + nom_title) or nom_title, 32, (210, 210, 255), 130, y + 42, anchor="lm",
              bold=False)
        label(card, short, 48, (255, 255, 255), 130, y + 90, anchor="lm")
        label(card, str(count), 40, color, 980, y + 64, anchor="rm")
        y += 150

    label(card, "Secret AI", 26, (170, 170, 220), width / 2, height - 30)
    out = io.BytesIO()
    card.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()
