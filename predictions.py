"""Personal prediction cards: a member gets a funny "forecast by the chat" card they can share to stories.

The model writes a short playful horoscope-style forecast grounded in what the person actually said in the
group lately (their topics, catchphrases, mood). The card is a tall 1080x1920 image built for Telegram stories.

In a group: "/predict" gives the caller their own card; replying to someone's message with /predict makes theirs.
In private chat: "/predict" uses what the person has said across groups.
A person gets one fresh card a day (same card returns if asked again the same day) to keep the server light.
"""
import asyncio
import datetime
import hashlib
import io
import logging
import random
import re

from PIL import ImageDraw

import quizart
from quizart import backdrop, font, label, panel

LOG = logging.getLogger("secret_ai.predictions")
MSK = datetime.timezone(datetime.timedelta(hours=3))
COMMAND = "predict"
BOT_NAME = "Secret AI"
DAY = 86400

# Fortune-style signs chosen for the day from the person+date, so it's stable within a day and varies between them.
SIGNS = [
    ("🔮", "Туман ясности"), ("⚡", "Заряд дня"), ("🌙", "Лунный вайб"), ("🔥", "Огонь внутри"),
    ("💎", "Редкий настрой"), ("🎲", "Рулетка судьбы"), ("🚀", "Режим разгона"), ("🌊", "Поток"),
    ("🍀", "Удачная полоса"), ("☕", "Медленный старт"), ("🎯", "Точно в цель"), ("🧲", "Притяжение"),
]
MOODS = ["космос", "чилл", "хаос", "дзен", "турбо", "лёгкая муть", "ясность", "движ", "залипание", "кураж"]


class Predictions:
    def __init__(self, store, ask_model):
        """store: the GroupStore; ask_model(chat_id, instructions, text) -> answer text."""
        self.store, self.db, self.ask_model = store, store.db, ask_model
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS predictions (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                            "day TEXT NOT NULL, text TEXT NOT NULL, created INTEGER NOT NULL, "
                            "PRIMARY KEY (chat_id, user_id, day))")

    # ---------- material ----------

    def member_lines(self, chat_id, name, limit=120):
        """Recent lines by a member in this group (matched by the 'Name:' prefix), oldest first."""
        like = name.replace("%", "").replace("_", "") + ":%"
        rows = self.db.execute("SELECT line FROM group_messages WHERE chat_id=? AND line LIKE ? ORDER BY id DESC LIMIT ?",
                               (chat_id, like, limit)).fetchall()
        return [self._body(line) for (line,) in rows][::-1]

    def member_lines_by_id(self, user_id, limit=120):
        """Recent lines by a user across all groups (used in private chat), oldest first."""
        rows = self.db.execute("SELECT line FROM group_messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
                               (user_id, limit)).fetchall()
        return [self._body(line) for (line,) in rows][::-1]

    @staticmethod
    def _body(line):
        return line.split(":", 1)[1].strip() if ":" in line else line

    # ---------- cache (one card a day per person per chat) ----------

    def cached(self, chat_id, user_id, day):
        row = self.db.execute("SELECT text FROM predictions WHERE chat_id=? AND user_id=? AND day=?",
                              (chat_id, user_id, day)).fetchone()
        return row[0] if row else None

    def save(self, chat_id, user_id, day, text):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO predictions(chat_id, user_id, day, text, created) VALUES (?, ?, ?, ?, ?)",
                            (chat_id, user_id, day, text, int(datetime.datetime.now(MSK).timestamp())))
            self.db.execute("DELETE FROM predictions WHERE created < ?",
                            (int(datetime.datetime.now(MSK).timestamp()) - 7 * DAY,))

    # ---------- posting ----------

    async def post(self, bot, chat_id, user_id, name, avatar=None, by_id=False):
        """Send the person's prediction card. name is their display name; avatar is JPEG bytes or None."""
        day = datetime.datetime.now(MSK).date().isoformat()
        text = self.cached(chat_id, user_id, day)
        if not text:
            material = self.member_lines_by_id(user_id) if by_id else self.member_lines(chat_id, name)
            if len([m for m in material if m]) < 3:
                await bot.send_message(chat_id, f"Пока мало знаю о {name}, чтобы предсказывать. Пусть напишет "
                                                f"ещё пару сообщений 🙂")
                return
            try:
                text = await self.write_forecast(chat_id, name, material)
            except Exception as exc:
                LOG.warning("Prediction text failed: %s %s", type(exc).__name__, str(exc)[:200])
                await bot.send_message(chat_id, "Не получилось нагадать прямо сейчас, попробуй позже 🔮")
                return
            text = (text or "").strip()
            if not text:
                await bot.send_message(chat_id, "Не получилось нагадать прямо сейчас, попробуй позже 🔮")
                return
            self.save(chat_id, user_id, day, text)
        sign, mood, luck = daily_fortune(user_id, day)
        card = await asyncio.to_thread(render, name, text, sign, mood, luck, avatar,
                                       datetime.datetime.now(MSK).date())
        await bot.send_photo(chat_id, card, caption=f"🔮 Прогноз для {name} на сегодня")

    async def write_forecast(self, chat_id, name, material):
        lines, size = [], 0
        for line in reversed(material):
            if not line:
                continue
            size += len(line)
            if size > 12_000:
                break
            lines.append(line[:300])
        lines.reverse()
        instructions = (
            f"Ты — Secret AI, свой человек в групповом чате. Сочини шуточный «прогноз на сегодня» для участника по "
            f"имени {name}, опираясь на его реальные сообщения ниже (его темы, словечки, настроение). Пиши по-русски, "
            "по-доброму и весело, как гороскоп-прикол от друга. Три коротких пункта, каждый с новой строки, каждый "
            "начинается с эмодзи и 1–2 предложения:\n"
            "💼 Дела — про учёбу/работу/движ;\n"
            "❤️ Общение — про чат, друзей, отношения;\n"
            "🎁 Совет дня — лёгкий шуточный совет.\n"
            "Всего не больше 320 знаков. Без markdown и без HTML-тегов, просто текст с эмодзи. Опирайся на то, о чём "
            "человек реально писал, но не цитируй дословно личное. Без оскорблений и пошлостей, по-доброму.")
        return await self.ask_model(chat_id, instructions, f"Сообщения {name}:\n" + "\n".join(lines))


def daily_fortune(user_id, day):
    """Stable-per-day sign, mood and a luck percentage, derived from the person and the date."""
    seed = int(hashlib.sha256(f"{user_id}:{day}".encode()).hexdigest(), 16)
    rng = random.Random(seed)
    sign = rng.choice(SIGNS)
    mood = rng.choice(MOODS)
    luck = rng.randint(35, 99)
    return sign, mood, luck


# ---------- the card (1080x1920, story format) ----------

def _avatar_circle(avatar, name, size):
    from PIL import Image, ImageOps
    image = None
    if avatar:
        try:
            image = ImageOps.fit(Image.open(io.BytesIO(avatar)).convert("RGB"), (size, size))
        except OSError:
            image = None
    if image is None:
        image = Image.new("RGB", (size, size), (99, 102, 241))
        d = ImageDraw.Draw(image)
        letter = (quizart.clean(name).strip() or "?")[0].upper()
        d.text((size / 2, size / 2), letter, font=font(size // 2), fill="white", anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    return image, mask


def render(name, forecast, sign, mood, luck, avatar, day):
    from PIL import Image
    width, height = 1080, 1920
    card = backdrop(width, height)
    draw = ImageDraw.Draw(card)

    emoji, sign_title = sign
    label(card, "ПРОГНОЗ ОТ ЧАТА", 56, (205, 205, 255), width / 2, 150, bold=False, shadow=True)

    # Avatar with a glowing ring.
    face, mask = _avatar_circle(avatar, name, 300)
    ring = Image.new("RGBA", (328, 328), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((0, 0, 327, 327), fill=(217, 70, 239, 255))
    card.alpha_composite(ring, (width // 2 - 164, 230))
    card.paste(face, (width // 2 - 150, 244), mask)

    short = quizart.clean(name) or "?"
    while font(64).getlength(short) > 900 and len(short) > 2:
        short = short.rstrip("…")[:-1].rstrip() + "…"
    label(card, short, 64, (255, 255, 255), width / 2, 620, shadow=True)

    # Sign tile.
    sign_text = quizart.clean(sign_title) or "Знак дня"
    panel(card, (240, 690, 840, 770), 24, (90, 80, 255), 0.85)
    label(card, sign_text, 36, (255, 255, 255), width / 2, 730, bold=False)

    # The forecast text in a soft panel.
    box = (80, 820, width - 80, 1500)
    panel(card, box, 40, (255, 255, 255), 0.10)
    face_font, lines = _fit_text(draw, quizart.clean(forecast), box[2] - box[0] - 100, box[3] - box[1] - 100)
    line_h = int(face_font.size * 1.4)
    y = box[1] + 60
    for line in lines:
        label(card, line, face_font.size, (245, 245, 255), box[0] + 50, y + line_h / 2, anchor="lm", bold=False)
        y += line_h

    # Mood + luck meter.
    label(card, f"Настроение: {quizart.clean(mood)}", 36, (205, 205, 255), width / 2, 1560, bold=False)
    panel(card, (180, 1610, 900, 1670), 30, (255, 255, 255), 0.12)
    panel(card, (180, 1610, 180 + int(720 * luck / 100), 1670), 30, (34, 197, 94), 0.85)
    label(card, f"Удача {luck}%", 34, (255, 255, 255), width / 2, 1640)

    label(card, f"{day.day:02d}.{day.month:02d}  ·  Secret AI", 30, (170, 170, 220), width / 2, height - 80, bold=False)
    out = io.BytesIO()
    card.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def _fit_text(draw, text, max_width, max_height, sizes=(52, 46, 42, 38, 34, 30)):
    for size in sizes:
        face = font(size, False)
        lines = quizart.wrap(text, face, max_width)
        if len(lines) * int(size * 1.4) <= max_height:
            return face, lines
    face = font(sizes[-1], False)
    lines = quizart.wrap(text, face, max_width)
    keep = max(1, max_height // int(sizes[-1] * 1.4))
    return face, lines[:keep]
