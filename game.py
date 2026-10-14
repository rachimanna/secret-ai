"""Group quiz: a lobby with topics, timed questions with buttons, points for speed and streaks, a podium card at the end
and an all-time leaderboard per group. Questions are written by the model (in Russian) — including ones about the
chat itself — with a built-in set as a fallback.
"""
import asyncio
import html
import io
import json
import logging
import random
import re
import time

from PIL import Image, ImageDraw, ImageFilter

from fun import ASSETS, _font, _gradient, _wrap

LOG = logging.getLogger(__name__)

TOPICS = {
    "mix": ("🎲", "Всё подряд"), "chat": ("💬", "Про наш чат"), "movies": ("🎬", "Кино и сериалы"),
    "music": ("🎧", "Музыка"), "sport": ("⚽", "Спорт"), "science": ("🧪", "Наука и мир"),
    "games": ("🎮", "Игры и аниме"), "caucasus": ("🏔", "Кавказ"),
}
TOPIC_PROMPTS = {
    "mix": "general knowledge, pop culture, history, geography, internet culture — a fun mix",
    "movies": "movies and TV series, Russian and world, recent and classic",
    "music": "music: Russian and world artists, hits, rap, pop",
    "sport": "sport: football, MMA, Olympics, famous athletes",
    "science": "science, space, nature, the human body, technology",
    "games": "video games, anime, memes",
    "caucasus": "the Caucasus: history, geography, peoples, languages, traditions, cuisine, famous people — respectful",
}
QUESTIONS = 8
ANSWER_SECONDS = 20
LOBBY_SECONDS = 180
LETTERS = ["A", "B", "C", "D"]
FALLBACK = [
    ("Какая планета самая большая в Солнечной системе?", ["Юпитер", "Сатурн", "Нептун", "Земля"], 0, "Юпитер тяжелее всех остальных планет вместе взятых."),
    ("Сколько игроков одной команды на поле в футболе?", ["11", "10", "9", "12"], 0, "10 полевых и вратарь."),
    ("Какой океан самый большой?", ["Тихий", "Атлантический", "Индийский", "Северный Ледовитый"], 0, "Тихий океан больше всей суши Земли."),
    ("Самая высокая гора Кавказа?", ["Эльбрус", "Казбек", "Шхара", "Дыхтау"], 0, "Эльбрус — 5642 м, высшая точка России и Европы."),
    ("Сколько костей у взрослого человека?", ["206", "180", "250", "312"], 0, "У младенцев их около 300, часть срастается."),
    ("Кто написал «Войну и мир»?", ["Лев Толстой", "Фёдор Достоевский", "Антон Чехов", "Иван Тургенев"], 0, "Роман писался около шести лет."),
    ("Какой газ мы выдыхаем больше, чем вдыхаем?", ["Углекислый", "Кислород", "Азот", "Гелий"], 0, "Кислород идёт на дыхание клеток, а CO₂ выводится."),
    ("Столица Армении?", ["Ереван", "Гюмри", "Ванадзор", "Тбилиси"], 0, "Ереван старше Рима — основан в 782 году до н. э."),
    ("В какой игре есть Крипер?", ["Minecraft", "Roblox", "Fortnite", "Terraria"], 0, "Крипер появился из-за ошибки в модели свиньи."),
    ("Сколько секунд в одном часе?", ["3600", "6000", "1440", "360"], 0, "60 × 60 = 3600."),
    ("Какая самая быстрая птица?", ["Сапсан", "Орёл", "Стриж", "Ласточка"], 0, "В пике сапсан разгоняется до 390 км/ч."),
    ("Самое глубокое озеро мира?", ["Байкал", "Танганьика", "Каспийское море", "Виктория"], 0, "Глубина Байкала — 1642 м."),
]

GAME_ASK = re.compile(r"(поигра|сыграем|давай\s+(?:в\s+)?игр|викторин|квиз|\bquiz\b|let'?s play)", re.I)


def esc(text):
    return html.escape(str(text or ""), quote=False)


class Game:
    def __init__(self, chat_id, host_id, host_name):
        self.chat_id, self.host_id = chat_id, host_id
        self.players = {host_id: host_name}
        self.topic = "mix"
        self.state = "lobby"
        self.lobby_message = None
        self.questions = []
        self.index = -1
        self.answers = {}  # user_id -> (option index, seconds taken)
        self.asked_at = 0.0
        self.scores, self.right, self.streak = {}, {}, {}
        self.lock = asyncio.Lock()
        self.answered = asyncio.Event()
        self.task = None
        self.created = time.monotonic()


class Quiz:
    def __init__(self, db, ask_model):
        """db: the GroupStore's SQLite connection; ask_model(chat_id, instructions, text) -> answer text."""
        self.db, self.ask_model = db, ask_model
        self.games = {}
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS game_scores (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                            "name TEXT NOT NULL, points INTEGER NOT NULL, wins INTEGER NOT NULL, games INTEGER NOT NULL, "
                            "PRIMARY KEY (chat_id, user_id))")

    # ---------- lobby ----------

    def lobby_text(self, game):
        emoji, title = TOPICS[game.topic]
        people = "\n".join(f"• {esc(name)}" for name in game.players.values())
        return (f"<b>🧠 Викторина</b>\n\nТема: {emoji} <b>{esc(title)}</b> · {QUESTIONS} вопросов · {ANSWER_SECONDS} с на ответ\n"
                f"Быстрый правильный ответ — больше очков, серия из трёх — бонус.\n\n"
                f"<b>Игроки ({len(game.players)}):</b>\n{people}\n\n"
                f"Жмите «Я играю», ведущий запускает ▶️")

    def lobby_keyboard(self, game, IKB, IKM):
        topics = [IKB(f"{'✅ ' if key == game.topic else ''}{emoji} {title}", callback_data=f"g:t:{key}")
                  for key, (emoji, title) in TOPICS.items()]
        rows = [[IKB("✋ Я играю", callback_data="g:j")]]
        rows += [topics[i:i + 2] for i in range(0, len(topics), 2)]
        rows.append([IKB("▶️ Начать", callback_data="g:s"), IKB("✖️ Отмена", callback_data="g:x")])
        return IKM(rows)

    async def open_lobby(self, bot, message, user, name, IKB, IKM):
        chat_id = message.chat_id
        current = self.games.get(chat_id)
        if current and current.state != "done" and time.monotonic() - current.created < 1800:
            await message.reply_text("Игра уже идёт — присоединяйтесь к ней 👆")
            return
        game = self.games[chat_id] = Game(chat_id, user.id, name)
        game.lobby_message = await bot.send_photo(chat_id, banner(), caption=self.lobby_text(game), parse_mode="HTML",
                                                  reply_markup=self.lobby_keyboard(game, IKB, IKM))
        game.task = asyncio.create_task(self._expire(bot, game))

    async def _expire(self, bot, game):
        await asyncio.sleep(LOBBY_SECONDS)
        if game.state == "lobby" and self.games.get(game.chat_id) is game:
            game.state = "done"
            try:
                await game.lobby_message.edit_caption("⌛ Викторина не началась — ведущий не нажал «Начать».")
            except Exception:
                pass

    # ---------- buttons ----------

    async def on_button(self, query, user, name, bot, IKB, IKM):
        parts = (query.data or "").split(":")
        game = self.games.get(query.message.chat_id if query.message else 0)
        if not game or game.state == "done":
            await query.answer("Эта игра уже закончилась. Новая: /game")
            return
        action = parts[1] if len(parts) > 1 else ""
        if action == "a":
            await self._answer(query, game, user, name, parts)
            return
        if game.state != "lobby":
            await query.answer()
            return
        async with game.lock:
            if action == "j":
                if user.id in game.players:
                    await query.answer("Ты уже в игре 👍")
                    return
                game.players[user.id] = name
                await query.answer("Ты в игре! ✋")
            elif action == "t" and len(parts) > 2 and parts[2] in TOPICS:
                if user.id != game.host_id:
                    await query.answer("Тему выбирает ведущий")
                    return
                game.topic = parts[2]
                await query.answer(TOPICS[game.topic][1])
            elif action == "x":
                if user.id != game.host_id:
                    await query.answer("Отменить может только ведущий")
                    return
                game.state = "done"
                await query.answer("Отменено")
                await game.lobby_message.edit_caption("Викторина отменена.")
                return
            elif action == "s":
                if user.id != game.host_id:
                    await query.answer("Запускает ведущий")
                    return
                game.state = "starting"
                await query.answer("Поехали!")
                if game.task:
                    game.task.cancel()
                game.task = asyncio.create_task(self._play(bot, game, IKB, IKM))
                return
            else:
                await query.answer()
                return
            try:
                await game.lobby_message.edit_caption(self.lobby_text(game), parse_mode="HTML",
                                                      reply_markup=self.lobby_keyboard(game, IKB, IKM))
            except Exception as exc:  # "message is not modified" and the like
                LOG.debug("Lobby update skipped: %s", exc)

    async def _answer(self, query, game, user, name, parts):
        try:
            number, choice = int(parts[2]), int(parts[3])
        except (IndexError, ValueError):
            await query.answer()
            return
        if game.state != "question" or number != game.index:
            await query.answer("Время на этот вопрос вышло ⏱")
            return
        if user.id in game.answers:
            await query.answer("Ответ уже принят — ждём остальных")
            return
        game.players.setdefault(user.id, name)  # late joiners just play
        game.answers[user.id] = (choice, time.monotonic() - game.asked_at)
        await query.answer("Ответ принят ✅")
        if len(game.answers) >= len(game.players):
            game.answered.set()

    # ---------- the game ----------

    async def _play(self, bot, game, IKB, IKM):
        try:
            emoji, title = TOPICS[game.topic]
            await game.lobby_message.edit_caption(
                f"<b>🧠 Викторина · {emoji} {esc(title)}</b>\n\nИгроки: {esc(', '.join(game.players.values()))}\n\n⏳ Готовлю вопросы…",
                parse_mode="HTML")
            game.questions = await self._questions(game)
            game.state = "question"
            for game.index in range(len(game.questions)):
                await self._ask(bot, game, IKB, IKM)
                if game.state == "done":
                    return
            await self._finish(bot, game)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Quiz failed: %s %s", type(exc).__name__, str(exc)[:200])
            game.state = "done"
            try:
                await bot.send_message(game.chat_id, "Викторина сломалась на полпути 😵 Попробуйте ещё раз: /game")
            except Exception:
                pass

    async def _ask(self, bot, game, IKB, IKM):
        question, options, correct, fact = game.questions[game.index]
        number, total = game.index + 1, len(game.questions)
        game.answers, game.answered = {}, asyncio.Event()

        def body(seconds_left=None, reveal=False):
            head = f"<b>Вопрос {number}/{total}</b> · {TOPICS[game.topic][0]} {esc(TOPICS[game.topic][1])}\n\n<b>{esc(question)}</b>\n\n"
            if not reveal:
                bar = "▰" * round(10 * seconds_left / ANSWER_SECONDS) + "▱" * (10 - round(10 * seconds_left / ANSWER_SECONDS))
                return head + f"⏱ {bar} {seconds_left} с · ответили {len(game.answers)}/{len(game.players)}"
            lines = [f"{'✅' if i == correct else '▫️'} {LETTERS[i]}. {esc(opt)}" for i, opt in enumerate(options)]
            return head + "\n".join(lines) + (f"\n\n💡 {esc(fact)}" if fact else "")

        keyboard = IKM([[IKB(f"{LETTERS[i]}. {opt}"[:60], callback_data=f"g:a:{game.index}:{i}")] for i, opt in enumerate(options)])
        message = await bot.send_message(game.chat_id, body(ANSWER_SECONDS), parse_mode="HTML", reply_markup=keyboard)
        game.asked_at = time.monotonic()
        deadline = game.asked_at + ANSWER_SECONDS
        while time.monotonic() < deadline and not game.answered.is_set():
            try:
                await asyncio.wait_for(game.answered.wait(), min(5, max(0.1, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                left = int(round(deadline - time.monotonic()))
                if left > 0:
                    try:
                        await message.edit_text(body(left), parse_mode="HTML", reply_markup=keyboard)
                    except Exception:
                        pass
        game.state = "reveal"
        gained = []
        for user_id, (choice, took) in sorted(game.answers.items(), key=lambda item: item[1][1]):
            if choice == correct:
                game.streak[user_id] = game.streak.get(user_id, 0) + 1
                points = 500 + int(500 * max(0.0, 1 - took / ANSWER_SECONDS)) // 10 * 10
                if game.streak[user_id] % 3 == 0:
                    points += 200
                game.scores[user_id] = game.scores.get(user_id, 0) + points
                game.right[user_id] = game.right.get(user_id, 0) + 1
                gained.append(f"{esc(game.players[user_id])} +{points}" + (" 🔥" if game.streak[user_id] % 3 == 0 else ""))
            else:
                game.streak[user_id] = 0
        result = ("\n\n🏅 " + ", ".join(gained)) if gained else "\n\n😶 Никто не угадал"
        try:
            await message.edit_text(body(reveal=True) + result, parse_mode="HTML")
        except Exception:
            pass
        game.state = "question"
        await asyncio.sleep(4)

    async def _finish(self, bot, game):
        game.state = "done"
        ranking = sorted(game.players, key=lambda uid: -game.scores.get(uid, 0))
        rows = [(game.players[uid], game.scores.get(uid, 0), game.right.get(uid, 0)) for uid in ranking]
        with self.db:
            for place, uid in enumerate(ranking):
                self.db.execute("INSERT INTO game_scores(chat_id, user_id, name, points, wins, games) VALUES (?, ?, ?, ?, ?, 1) "
                                "ON CONFLICT(chat_id, user_id) DO UPDATE SET name=excluded.name, points=points+excluded.points, "
                                "wins=wins+excluded.wins, games=games+1",
                                (game.chat_id, uid, game.players[uid], game.scores.get(uid, 0),
                                 1 if place == 0 and game.scores.get(uid, 0) > 0 else 0))
        winner = rows[0] if rows and rows[0][1] > 0 else None
        caption = (f"🏆 Победа: <b>{esc(winner[0])}</b> — {winner[1]} очков!" if winner else "Ничья по нулям 😅") + \
                  "\n\nЕщё раз — /game · рейтинг группы — /top"
        await bot.send_photo(game.chat_id, podium(rows, len(game.questions), TOPICS[game.topic][1]), caption=caption,
                             parse_mode="HTML")

    async def top(self, bot, chat_id, title):
        rows = self.db.execute("SELECT name, points, wins, games FROM game_scores WHERE chat_id=? ORDER BY points DESC "
                               "LIMIT 10", (chat_id,)).fetchall()
        if not rows:
            await bot.send_message(chat_id, "Тут ещё никто не играл. Начать: /game")
            return
        await bot.send_photo(chat_id, leaderboard(rows, title), caption="🏆 Рейтинг викторины за всё время · новая игра — /game")

    # ---------- questions ----------

    async def _questions(self, game):
        try:
            questions = await self._generate(game)
        except Exception as exc:
            LOG.warning("Quiz questions failed: %s", type(exc).__name__)
            questions = []
        if len(questions) < QUESTIONS:
            spare = [q for q in FALLBACK if q[0] not in {x[0] for x in questions}]
            random.shuffle(spare)
            questions += spare[:QUESTIONS - len(questions)]
        shuffled = []
        for question, options, correct, fact in questions[:QUESTIONS]:  # the right answer must not always be A
            order = list(range(len(options)))
            random.shuffle(order)
            shuffled.append((question, [options[i] for i in order], order.index(correct), fact))
        return shuffled

    async def _generate(self, game):
        if game.topic == "chat":
            lines = self.history(game.chat_id)
            if len(lines) < 30:
                game.topic = "mix"  # too little to ask about
                return await self._generate(game)
            subject = ("questions about THIS group chat, from its messages below: who said a phrase, who talks about what, "
                       "running jokes, the chat's words and habits. Use the members' names as options. Funny, never insulting "
                       "or revealing anything private. Messages:\n" + "\n".join(lines[-400:]))
        else:
            subject = TOPIC_PROMPTS[game.topic]
        instructions = (
            f"Write {QUESTIONS} quiz questions in Russian about {subject}. Mix easy and harder ones, make them fun and "
            "surprising, each with exactly 4 short options (max 40 characters), only one correct, and a one-sentence fun "
            'fact. The facts must be accurate. Return only JSON: {"questions": [{"q": "...", "options": ["...", "...", '
            '"...", "..."], "answer": 0, "fact": "..."}]} where answer is the index of the correct option.')
        text = await self.ask_model(game.chat_id, instructions, "Сделай вопросы.")
        found = re.search(r"\{.*\}", text or "", re.S)
        data = json.loads(found.group(0)) if found else {}
        questions = []
        for item in data.get("questions", []):
            options = [str(o).strip()[:60] for o in item.get("options", []) if str(o).strip()]
            answer = item.get("answer")
            if str(item.get("q", "")).strip() and len(options) == 4 and isinstance(answer, int) and 0 <= answer < 4:
                questions.append((str(item["q"]).strip()[:300], options, answer, str(item.get("fact") or "").strip()[:300]))
        return questions

    def history(self, chat_id):
        rows = self.db.execute("SELECT line FROM group_messages WHERE chat_id=? AND line NOT LIKE 'Secret AI%' "
                               "ORDER BY id DESC LIMIT 600", (chat_id,)).fetchall()
        return [row[0] for row in reversed(rows)]


# ---------- pictures ----------

def _png(image):
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


def _backdrop(width, height):
    card = _gradient((width, height), (40, 30, 120), (8, 8, 20)).convert("RGBA")
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    draw.ellipse((-260, -300, width * 0.75, height * 0.55), fill=(90, 80, 255, 120))
    draw.ellipse((width * 0.45, height * 0.35, width + 300, height + 300), fill=(217, 70, 239, 90))
    card.alpha_composite(glow.filter(ImageFilter.GaussianBlur(130)))
    return card


def banner():
    """The lobby picture: the mascot, a big title and a hint of the rules."""
    width, height = 1280, 640
    card = _backdrop(width, height)
    draw = ImageDraw.Draw(card)
    mascot = Image.open(ASSETS / "mascot.png").convert("RGBA").resize((380, 380), Image.LANCZOS)
    card.alpha_composite(mascot.rotate(-8, resample=Image.BICUBIC, expand=False), (840, 150))
    draw.text((90, 150), "ВИКТОРИНА", font=_font("DejaVuSans-Bold.ttf", 110), fill="white")
    draw.text((94, 285), "Secret AI · игра для группы", font=_font("DejaVuSans.ttf", 42), fill=(200, 200, 255))
    for i, (chip, color) in enumerate([("8 вопросов", (90, 80, 255)), ("20 секунд", (217, 70, 239)),
                                       ("очки за скорость", (20, 184, 166))]):
        font = _font("DejaVuSans-Bold.ttf", 34)
        x = 94 + sum(int(draw.textlength(t, font=font)) + 90 for t, _ in [("8 вопросов", 0), ("20 секунд", 0)][:i])
        w = int(draw.textlength(chip, font=font)) + 56
        draw.rounded_rectangle((x, 400, x + w, 470), 35, fill=(*color, 255))
        draw.text((x + 28, 435), chip, font=font, fill="white", anchor="lm")
    return _png(card)


def _panel(card, box, radius, alpha):
    """A translucent rounded panel, blended over the card (drawing RGBA straight onto it would just overwrite)."""
    layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius, fill=(255, 255, 255, alpha))
    card.alpha_composite(layer)


def _fit(draw, text, font, width):
    text = str(text)
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


MEDALS = [(255, 204, 77), (205, 214, 230), (230, 150, 90)]


def podium(rows, questions, topic):
    """The final card: a podium for the top three and the rest below."""
    width, height = 1080, 1350
    card = _backdrop(width, height)
    draw = ImageDraw.Draw(card)
    draw.text((width / 2, 110), "ИТОГИ", font=_font("DejaVuSans-Bold.ttf", 96), fill="white", anchor="mm")
    draw.text((width / 2, 190), f"Викторина · {topic}", font=_font("DejaVuSans.ttf", 38), fill=(200, 200, 255), anchor="mm")
    spots = [(width / 2, 400, 350), (width / 2 - 320, 480, 270), (width / 2 + 320, 540, 210)]  # 1st, 2nd, 3rd
    for place in range(min(3, len(rows))):
        name, points, right = rows[place]
        cx, top, column = spots[place]
        _panel(card, (cx - 145, top + 150, cx + 145, top + 150 + column), 28, 40 if place else 60)
        draw.ellipse((cx - 70, top - 10, cx + 70, top + 130), fill=(*MEDALS[place], 255))
        draw.text((cx, top + 60), str(place + 1), font=_font("DejaVuSans-Bold.ttf", 76), fill=(30, 26, 60), anchor="mm")
        name_font = _font("DejaVuSans-Bold.ttf", 40)
        short = _fit(draw, name, name_font, 260)
        draw.text((cx, top + 195), short, font=name_font, fill="white", anchor="mm")
        draw.text((cx, top + 250), f"{points}", font=_font("DejaVuSans-Bold.ttf", 52), fill=(*MEDALS[place], 255), anchor="mm")
        draw.text((cx, top + 300), f"{right}/{questions} верно", font=_font("DejaVuSans.ttf", 30), fill=(210, 210, 235), anchor="mm")
    y = 920
    for place, (name, points, right) in enumerate(rows[3:8], start=4):
        _panel(card, (90, y, width - 90, y + 70), 22, 30)
        draw.text((130, y + 35), f"{place}", font=_font("DejaVuSans-Bold.ttf", 34), fill=(200, 200, 255), anchor="lm")
        draw.text((200, y + 35), _fit(draw, name, _font("DejaVuSans.ttf", 34), 600), font=_font("DejaVuSans.ttf", 34), fill="white", anchor="lm")
        draw.text((width - 130, y + 35), str(points), font=_font("DejaVuSans-Bold.ttf", 34), fill="white", anchor="rm")
        y += 82
    draw.text((width / 2, height - 60), "Secret AI", font=_font("DejaVuSans-Bold.ttf", 30), fill=(170, 170, 220), anchor="mm")
    return _png(card)


def leaderboard(rows, title):
    width, height = 1080, 300 + 96 * len(rows)
    card = _backdrop(width, height)
    draw = ImageDraw.Draw(card)
    draw.text((width / 2, 100), "РЕЙТИНГ", font=_font("DejaVuSans-Bold.ttf", 84), fill="white", anchor="mm")
    sub = _fit(draw, title, _font("DejaVuSans.ttf", 36), 900)
    draw.text((width / 2, 175), sub, font=_font("DejaVuSans.ttf", 36), fill=(200, 200, 255), anchor="mm")
    y = 240
    for place, (name, points, wins, games) in enumerate(rows, start=1):
        _panel(card, (70, y, width - 70, y + 80), 24, 34 if place > 3 else 56)
        color = MEDALS[place - 1] if place <= 3 else (200, 200, 255)
        draw.ellipse((95, y + 14, 147, y + 66), fill=(*color, 255))
        draw.text((121, y + 40), str(place), font=_font("DejaVuSans-Bold.ttf", 30), fill=(30, 26, 60), anchor="mm")
        draw.text((175, y + 40), _fit(draw, name, _font("DejaVuSans-Bold.ttf", 36), 520), font=_font("DejaVuSans-Bold.ttf", 36), fill="white", anchor="lm")
        draw.text((width - 100, y + 28), f"{points}", font=_font("DejaVuSans-Bold.ttf", 34), fill="white", anchor="rm")
        draw.text((width - 100, y + 60), f"побед {wins} · игр {games}", font=_font("DejaVuSans.ttf", 22),
                  fill=(200, 200, 235), anchor="rm")
        y += 96
    return _png(card)
