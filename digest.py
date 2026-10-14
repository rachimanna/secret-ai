"""Daily summary in groups: every evening (22:00 Moscow) a card with the day's numbers — messages, people, the busiest
hour, who talked most, activity by hour — and a funny write-up with nominations ("фраза дня"…) by the model.
/sday posts it right away; "/sday off" and "/sday on" (group admins) turn the evening post off and on.
"""
import asyncio
import collections
import datetime
import io
import logging
import re

from PIL import Image, ImageDraw

import quizart
from quizart import backdrop, font, label, panel

LOG = logging.getLogger("secret_ai.digest")
MSK = datetime.timezone(datetime.timedelta(hours=3))
HOUR = 22  # Moscow time
MIN_MESSAGES = 20  # a quieter day gets no evening post
COMMAND = "sday"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
          "декабря"]
BOT_NAME = "Secret AI"


def today():
    return datetime.datetime.now(MSK).date()


class Digest:
    def __init__(self, store, ask_model):
        """store: the GroupStore; ask_model(chat_id, instructions, text) -> answer text."""
        self.store, self.db, self.ask_model = store, store.db, ask_model
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS group_digest (chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL "
                            "DEFAULT 1, last_day TEXT NOT NULL DEFAULT '')")

    # ---------- settings ----------

    def enabled(self, chat_id):
        row = self.db.execute("SELECT enabled FROM group_digest WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row[0]) if row else True

    def set_enabled(self, chat_id, on):
        with self.db:
            self.db.execute("INSERT INTO group_digest(chat_id, enabled) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                            "enabled=excluded.enabled", (chat_id, int(on)))

    def _mark(self, chat_id, day):
        with self.db:
            self.db.execute("INSERT INTO group_digest(chat_id, last_day) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                            "last_day=excluded.last_day", (chat_id, day))

    def _due(self, day):
        """Groups with enough talk in the last 24 hours that haven't had today's summary and didn't turn it off."""
        since = int(datetime.datetime.now(MSK).timestamp()) - 86400
        rows = self.db.execute("SELECT m.chat_id, COUNT(*) FROM group_messages m LEFT JOIN group_digest d USING(chat_id) "
                               "WHERE m.created >= ? AND m.line NOT LIKE ? AND COALESCE(d.enabled, 1) = 1 AND "
                               "COALESCE(d.last_day, '') != ? GROUP BY m.chat_id HAVING COUNT(*) >= ?",
                               (since, BOT_NAME + ":%", day, MIN_MESSAGES)).fetchall()
        return [row[0] for row in rows]

    # ---------- numbers ----------

    def day_lines(self, chat_id):
        since = int(datetime.datetime.now(MSK).timestamp()) - 86400
        return self.db.execute("SELECT created, line FROM group_messages WHERE chat_id=? AND created >= ? AND line NOT LIKE ? "
                               "ORDER BY id", (chat_id, since, BOT_NAME + ":%")).fetchall()

    @staticmethod
    def stats(rows):
        names, hours = collections.Counter(), [0] * 24
        for created, line in rows:
            name = line.split(":", 1)[0].strip() if ":" in line else "?"
            names[name] += 1
            hours[datetime.datetime.fromtimestamp(created, MSK).hour] += 1
        peak = max(range(24), key=lambda h: hours[h]) if rows else 0
        return {"total": len(rows), "people": len(names), "top": names.most_common(5), "hours": hours, "peak": peak}

    # ---------- posting ----------

    async def post(self, bot, chat_id, title, manual=False):
        rows = self.day_lines(chat_id)
        if len(rows) < (5 if manual else MIN_MESSAGES):
            if manual:
                await bot.send_message(chat_id, "За последние сутки тут почти не писали — подводить нечего 😴")
            return
        numbers = self.stats(rows)
        card = await asyncio.to_thread(render, numbers, title or "Группа", today())
        try:
            text = await self.write_up(chat_id, rows, numbers)
        except Exception as exc:  # the card alone is still a summary
            LOG.warning("Day summary text failed: %s %s", type(exc).__name__, str(exc)[:200])
            text = ""
        caption = text if len(text) <= 1000 else ""
        try:
            await bot.send_photo(chat_id, card, caption=caption or None, parse_mode="HTML" if caption else None)
        except Exception:  # the model's HTML didn't parse: plain text
            await bot.send_photo(chat_id, card, caption=re.sub(r"<[^>]+>", "", caption)[:1000] or None)
        if text and not caption:
            try:
                await bot.send_message(chat_id, text[:4000], parse_mode="HTML")
            except Exception:
                await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text)[:4000])
        self.store.add_line(chat_id, f"{BOT_NAME}: [итоги дня] " + re.sub(r"<[^>]+>", "", text)[:500])

    async def write_up(self, chat_id, rows, numbers):
        lines, size = [], 0
        for _, line in reversed(rows):  # the newest first, until the budget is spent, then back in order
            size += len(line)
            if size > 60_000:
                break
            lines.append(line[:400])
        lines.reverse()
        top = ", ".join(f"{name} ({count})" for name, count in numbers["top"][:3])
        instructions = (
            "Ты — Secret AI, свой человек в этом групповом чате. Подведи итоги дня по сообщениям ниже. Пиши по-русски, "
            "весело и по-дружески, как друг из этой компании, а не как отчёт. Формат — Telegram HTML (только <b>, <i>), "
            "без markdown, всего не больше 700 знаков:\n"
            "<b>📌 О чём говорили</b> — 2–3 коротких живых предложения.\n"
            "Потом номинации, каждая с новой строки, только те, для которых правда есть материал:\n"
            f"🏆 Болтун дня — имя (уже посчитано: {top});\n"
            "💬 Фраза дня — «дословная цитата» — имя;\n"
            "😂 Момент дня — что было самым смешным;\n"
            "🔥 Спор дня — о чём спорили (если спорили);\n"
            "🌙 Последнее слово — кто написал последним и что.\n"
            "Цитаты — только дословно из сообщений, ничего не выдумывай. Без оскорблений, без пошлых и личных "
            "подробностей, никого не высмеивай зло.")
        return (await self.ask_model(chat_id, instructions, "Сообщения за сутки:\n" + "\n".join(lines))).strip()

    async def run(self, app):
        """Every minute: at HOUR Moscow time, post the summary in each active group once a day."""
        while True:
            await asyncio.sleep(60)
            now = datetime.datetime.now(MSK)
            if now.hour != HOUR:
                continue
            day = now.date().isoformat()
            for chat_id in self._due(day):
                self._mark(chat_id, day)  # once, even if posting fails
                title = (self.db.execute("SELECT title FROM group_info WHERE chat_id=?", (chat_id,)).fetchone() or [""])[0]
                try:
                    await self.post(app.bot, chat_id, title)
                except Exception as exc:  # removed from the group, no rights to post…
                    LOG.warning("Day summary failed in %s: %s", chat_id, type(exc).__name__)
                await asyncio.sleep(3)


# ---------- the card ----------

def render(numbers, title, day):
    width, height = 1080, 1260
    card = backdrop(width, height)
    draw = ImageDraw.Draw(card)
    label(card, "ИТОГИ ДНЯ", 92, (255, 255, 255), width / 2, 110, shadow=True)
    sub = quizart.clean(title)
    while font(38, False).getlength(sub) > 860 and len(sub) > 2:
        sub = sub.rstrip("…")[:-1].rstrip() + "…"
    label(card, sub, 38, (205, 205, 255), width / 2, 190, bold=False)
    label(card, f"{day.day} {MONTHS[day.month - 1]}", 30, (170, 170, 220), width / 2, 240, bold=False)

    tiles = [(str(numbers["total"]), "сообщений", (90, 80, 255)), (str(numbers["people"]), "участников", (217, 70, 239)),
             (f"{numbers['peak']:02d}:00", "пик активности", (20, 184, 166))]
    x = 70
    for value, caption, color in tiles:
        panel(card, (x, 290, x + 300, 450), 30, color, 0.85)
        label(card, value, 66, (255, 255, 255), x + 150, 355)
        label(card, caption, 26, (235, 235, 255), x + 150, 418, bold=False)
        x += 320

    label(card, "Самые активные", 36, (255, 255, 255), 70, 515, anchor="lm")
    top = numbers["top"][:5]
    most = top[0][1] if top else 1
    y = 560
    for place, (name, count) in enumerate(top):
        panel(card, (70, y, 1010, y + 66), 22, (255, 255, 255), 0.10)
        share = count / most
        color = quizart.MEDALS[place] if place < 3 else (200, 200, 255)
        panel(card, (70, y, 70 + max(80, int(940 * share)), y + 66), 22, color, 0.30)
        short = quizart.clean(name) or "?"
        while font(32).getlength(short) > 700 and len(short) > 2:
            short = short.rstrip("…")[:-1].rstrip() + "…"
        label(card, f"{place + 1}. {short}", 32, (255, 255, 255), 100, y + 34, anchor="lm")
        label(card, str(count), 32, (255, 255, 255), 980, y + 34, anchor="rm")
        y += 80

    chart_top, chart_bottom = 1025, 1160
    label(card, "Активность по часам", 30, (205, 205, 255), 70, 990, anchor="lm", bold=False)
    hours, peak = numbers["hours"], max(1, max(numbers["hours"]))
    step = 940 / 24
    for hour, count in enumerate(hours):
        h = int((chart_bottom - chart_top) * count / peak)
        x0 = 70 + hour * step
        color = (45, 212, 191) if hour == numbers["peak"] else (140, 130, 255)
        if h:
            panel(card, (x0 + 4, chart_bottom - h, x0 + step - 4, chart_bottom), 6, color, 0.95)
        else:
            draw.line((x0 + 4, chart_bottom, x0 + step - 4, chart_bottom), fill=(255, 255, 255, 60), width=2)
    for hour in (0, 6, 12, 18, 23):
        label(card, f"{hour:02d}", 22, (170, 170, 220), 70 + hour * step + step / 2, chart_bottom + 26, bold=False)
    label(card, "Secret AI", 26, (170, 170, 220), width / 2, height - 30)
    out = io.BytesIO()
    card.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()
