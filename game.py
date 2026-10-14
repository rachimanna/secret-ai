"""Group quiz: a lobby with topics, timed questions with buttons, points for speed and streaks, a podium at the end and
an all-time leaderboard per group. Questions are written by the model (in Russian) — including ones about the chat
itself — never repeat within a chat, and fall back to a built-in set. Every card is a short animation (quizart.py).
"""
import asyncio
import html
import json
import logging
import random
import re
import time

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, InputFile, InputMediaAnimation

import quizart

LOG = logging.getLogger(__name__)

START, RATING, STOP = "sgame", "srating", "sstop"  # unusual names: other bots in the group answer /game and /top too
TOPICS = {
    "mix": ("🎲", "Всё подряд"), "chat": ("💬", "Про наш чат"), "movies": ("🎬", "Кино и сериалы"),
    "music": ("🎧", "Музыка"), "sport": ("⚽", "Спорт"), "science": ("🧪", "Наука и мир"),
    "games": ("🎮", "Игры и аниме"), "caucasus": ("🏔", "Кавказ"),
}
ANGLES = {
    "mix": ["история", "география и страны", "животные", "интернет и мемы", "еда и напитки", "технологии и гаджеты",
            "кино", "спорт", "слова и языки", "человеческое тело", "космос", "бренды и компании", "рекорды",
            "музыка", "изобретения", "деньги и экономика", "современная жизнь"],
    "movies": ["голливуд последних 15 лет", "российское кино и сериалы", "мультфильмы и Pixar", "Marvel и DC",
               "Netflix и стриминговые сериалы", "закулисье съёмок", "актёры и их роли", "культовые фильмы 90-х и 2000-х",
               "аниме-фильмы", "Оскар и кассовые рекорды"],
    "music": ["русский рэп", "мировой поп", "клипы и рекорды YouTube и Spotify", "легенды рока", "истории песен",
              "K-pop", "музыкальные инструменты", "TikTok-хиты", "кавказская и восточная эстрада", "фестивали и концерты"],
    "sport": ["футбол: клубы и трансферы", "чемпионаты мира и Европы", "UFC и бокс", "борьба и кавказские чемпионы",
              "Олимпийские игры", "баскетбол и НБА", "Формула-1", "спортивные рекорды", "киберспорт", "правила и курьёзы"],
    "science": ["космос", "животные и их суперспособности", "человеческое тело", "физика вокруг нас", "химия в быту",
                "изобретения и открытия", "география и природа", "технологии и ИИ", "погода и климат", "океаны"],
    "games": ["Minecraft, Roblox и Fortnite", "GTA и Rockstar", "Dota, CS и киберспорт", "Nintendo и Pokémon",
              "история консолей", "аниме-сериалы", "манга", "мобильные игры", "мемы из игр", "персонажи и злодеи"],
    "caucasus": ["история Кавказа", "горы и природа", "языки и народы", "кухня", "традиции и обычаи",
                 "известные спортсмены", "города и архитектура", "музыка и танцы", "легенды и эпос", "известные люди"],
}
QUESTIONS = 8
ANSWER_SECONDS = 20
REVEAL_PAUSE = 6
LOBBY_SECONDS = 180
LETTERS = quizart.LETTERS
FALLBACK = [
    ("Что из этого с точки зрения ботаники — ягода?", ["Банан", "Клубника", "Малина", "Вишня"], 0, "А клубника — «ложная ягода»: её настоящие плоды — это семечки снаружи."),
    ("Сколько сердец у осьминога?", ["Три", "Одно", "Два", "Четыре"], 0, "И кровь у него голубая — из-за меди вместо железа."),
    ("Какая страна первой дала женщинам право голоса на выборах?", ["Новая Зеландия", "США", "Великобритания", "Франция"], 0, "Это случилось в 1893 году."),
    ("Что из этого появилось раньше всего?", ["Оксфордский университет", "Ацтекский Теночтитлан", "Империя инков", "Великая хартия вольностей"], 0, "В Оксфорде преподавали уже в 1096 году, а Теночтитлан основан в 1325-м."),
    ("У какой планеты сутки длиннее её года?", ["Венера", "Меркурий", "Марс", "Юпитер"], 0, "Венера оборачивается вокруг оси за 243 земных дня, а вокруг Солнца — за 225."),
    ("Какого цвета «чёрный ящик» самолёта?", ["Оранжевого", "Чёрного", "Красного", "Жёлтого"], 0, "Яркий цвет — чтобы его было легче найти среди обломков."),
    ("В какой стране больше всего пирамид?", ["Судан", "Египет", "Мексика", "Перу"], 0, "В Судане их больше двухсот — больше, чем в Египте."),
    ("Что изобрели раньше всего?", ["Зажигалку", "Спички-чиркалки", "Шариковую ручку", "Застёжку-молнию"], 0, "Зажигалку Дёберейнера сделали в 1823 году, а чиркающие спички — в 1826-м."),
    ("Сколько на самом деле длилась Столетняя война?", ["116 лет", "100 лет", "99 лет", "124 года"], 0, "С 1337 по 1453 год, с долгими перемириями."),
    ("У какой страны больше всего часовых поясов?", ["Франция", "Россия", "США", "Великобритания"], 0, "Благодаря заморским территориям у Франции их двенадцать."),
    ("Сколько глаз у пчелы?", ["Пять", "Два", "Три", "Шесть"], 0, "Два больших фасеточных и три простых на макушке."),
    ("Какая из этих стран не имеет выхода к морю?", ["Боливия", "Чили", "Перу", "Эквадор"], 0, "Боливия потеряла побережье в войне с Чили в 1880-х, но флот у неё есть до сих пор."),
    ("Кто из этих писателей по профессии был врачом?", ["Чехов", "Пушкин", "Толстой", "Гоголь"], 0, "«Медицина — моя законная жена, а литература — любовница», — шутил Чехов."),
    ("Сколько раз Бразилия выигрывала чемпионат мира по футболу?", ["5", "4", "6", "3"], 0, "Больше, чем любая другая сборная."),
    ("Какая часть поверхности Земли покрыта водой?", ["Около 71%", "Около 51%", "Около 81%", "Около 61%"], 0, "При этом пресной воды из всей — лишь около 2,5%."),
    ("Как зовут главного героя серии The Legend of Zelda?", ["Линк", "Зельда", "Ганон", "Эпона"], 0, "Многие думают, что Зельда — это он, но Зельда — принцесса."),
    ("Из какой страны футбольный клуб «Аякс»?", ["Нидерланды", "Бельгия", "Дания", "Греция"], 0, "Клуб из Амстердама назван в честь героя греческих мифов."),
    ("Где находится Анхель — самый высокий водопад мира?", ["Венесуэла", "Бразилия", "Колумбия", "Перу"], 0, "Вода падает почти с километра — 979 метров."),
    ("Какая самая высокая гора Грузии?", ["Шхара", "Казбек", "Тетнульди", "Ушба"], 0, "Шхара — 5193 метра, на границе Сванетии и Рачи."),
    ("Какой город считается самым древним в России?", ["Дербент", "Великий Новгород", "Старая Ладога", "Керчь"], 0, "Дербенту больше 5000 лет, его крепость — в списке ЮНЕСКО."),
    ("Сколько струн у классической балалайки?", ["Три", "Четыре", "Шесть", "Пять"], 0, "А у домры бывает и три, и четыре."),
    ("Какой самый большой орган человеческого тела?", ["Кожа", "Печень", "Лёгкие", "Кишечник"], 0, "У взрослого кожа весит несколько килограммов."),
    ("В каком году вышел первый iPhone?", ["2007", "2005", "2008", "2010"], 0, "В нём не было ни App Store, ни копирования текста."),
    ("Кому принадлежит Гренландия?", ["Дании", "Норвегии", "Исландии", "Канаде"], 0, "Это самый большой остров мира, который не считается материком."),
]

GAME_ASK = re.compile(r"(поигра|сыграем|давай\s+(?:в\s+)?игр|викторин|квиз|\bquiz\b|let'?s play)", re.I)


def mp4(data, name="quiz"):
    """Raw bytes arrive as a nameless file ("application/octet-stream"); a .mp4 name makes them a GIF in the chat."""
    return InputFile(data, filename=f"{name}.mp4")


def jpg(data):
    return InputFile(data, filename="preview.jpg")


def esc(text):
    return html.escape(str(text or ""), quote=False)


def key(question):
    """A question's identity for "already asked": letters and digits only, lower case."""
    return re.sub(r"[^a-zа-яё0-9]", "", str(question).lower())[:80]


class Game:
    def __init__(self, chat_id, host_id, host_name):
        self.chat_id, self.host_id = chat_id, host_id
        self.players = {host_id: host_name}
        self.topic = "mix"
        self.state = "lobby"
        self.lobby_message = None
        self.questions = []
        self.clips = []
        self.index = -1
        self.answers = {}  # user_id -> (option index, seconds taken)
        self.asked_at = 0.0
        self.scores, self.right, self.streak = {}, {}, {}
        self.lock = asyncio.Lock()
        self.answered = asyncio.Event()
        self.task = self.render = None
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
            self.db.execute("CREATE TABLE IF NOT EXISTS game_asked (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, "
                            "question TEXT NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS game_asked_chat ON game_asked(chat_id, id)")

    # ---------- lobby ----------

    def lobby_text(self, game):
        emoji, title = TOPICS[game.topic]
        people = ", ".join(esc(name) for name in game.players.values())
        return f"<b>🧠 Викторина</b> · {emoji} {esc(title)}\n👥 {people}\nОстановить — /{STOP}"

    def lobby_keyboard(self, game):
        topics = [Button(f"{'✅ ' if key == game.topic else ''}{emoji} {title}", callback_data=f"g:t:{key}")
                  for key, (emoji, title) in TOPICS.items()]
        rows = [[Button("✋ Я играю", callback_data="g:j")]]
        rows += [topics[i:i + 2] for i in range(0, len(topics), 2)]
        rows.append([Button("▶️ Начать", callback_data="g:s")])
        return Keyboard(rows)

    async def open_lobby(self, bot, message, user, name):
        chat_id = message.chat_id
        current = self.games.get(chat_id)
        if current and current.state != "done" and time.monotonic() - current.created < 1800:
            await message.reply_text("Игра уже идёт — присоединяйтесь к ней 👆")
            return
        game = self.games[chat_id] = Game(chat_id, user.id, name)
        caption, keyboard = self.lobby_text(game), self.lobby_keyboard(game)
        try:
            clip, preview = await asyncio.to_thread(lambda: (quizart.banner_clip(), quizart.banner_thumb()))
            game.lobby_message = await bot.send_animation(chat_id, mp4(clip, "lobby"), caption=caption, parse_mode="HTML",
                                                          reply_markup=keyboard, width=800, height=400,
                                                          thumbnail=jpg(preview))
        except Exception as exc:
            LOG.warning("Quiz banner animation failed: %s", type(exc).__name__)
            game.lobby_message = await bot.send_photo(chat_id, quizart.banner(), caption=caption, parse_mode="HTML",
                                                      reply_markup=keyboard)
        game.task = asyncio.create_task(self._expire(game))

    async def _expire(self, game):
        await asyncio.sleep(LOBBY_SECONDS)
        if game.state == "lobby" and self.games.get(game.chat_id) is game:
            game.state = "done"
            try:
                await game.lobby_message.edit_caption("⌛ Викторина не началась — ведущий не нажал «Начать».")
            except Exception:
                pass

    async def stop(self, bot, chat_id, user_id, is_admin):
        """/sstop: end the game in this chat — its host or a group admin. Returns the reply text."""
        game = self.games.get(chat_id)
        if not game or game.state == "done":
            return f"Сейчас игры нет. Начать: /{START}"
        if user_id != game.host_id and not is_admin:
            return "Остановить игру может ведущий или админ группы"
        started = game.state != "lobby"
        game.state = "done"
        if game.task:
            game.task.cancel()
        if game.render:
            game.render.cancel()
        if not started:
            try:
                await game.lobby_message.edit_caption("Викторина отменена.")
            except Exception:
                pass
        return "⏹ Викторина остановлена" + (" — очки этой игры не считаются." if started else ".")

    # ---------- buttons ----------

    async def on_button(self, query, user, name, bot):
        parts = (query.data or "").split(":")
        game = self.games.get(query.message.chat_id if query.message else 0)
        if not game or game.state == "done":
            await query.answer(f"Эта игра уже закончилась. Новая: /{START}")
            return
        action = parts[1] if len(parts) > 1 else ""
        if action == "a":
            await self._answer(query, game, user, parts)
            return
        if game.state != "lobby":
            await query.answer("Игра уже началась — жди следующую 🙂" if action == "j" else None)
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
            elif action == "s":
                if user.id != game.host_id:
                    await query.answer("Запускает ведущий")
                    return
                game.state = "starting"
                await query.answer("Поехали!")
                if game.task:
                    game.task.cancel()
                game.task = asyncio.create_task(self._play(bot, game))
                return
            else:
                await query.answer()
                return
            try:
                await game.lobby_message.edit_caption(self.lobby_text(game), parse_mode="HTML",
                                                      reply_markup=self.lobby_keyboard(game))
            except Exception as exc:  # "message is not modified" and the like
                LOG.debug("Lobby update skipped: %s", exc)

    async def _answer(self, query, game, user, parts):
        try:
            number, choice = int(parts[2]), int(parts[3])
        except (IndexError, ValueError):
            await query.answer()
            return
        if user.id not in game.players:
            await query.answer("Ты не в этой игре 🙅 Нажми «Я играю», когда начнётся следующая.", show_alert=True)
            return
        if game.state != "question" or number != game.index or not game.asked_at:
            await query.answer("Время на этот вопрос вышло ⏱")
            return
        if user.id in game.answers:
            await query.answer("Ответ уже принят — ждём остальных")
            return
        game.answers[user.id] = (choice, time.monotonic() - game.asked_at)
        await query.answer(f"Ответ {LETTERS[choice] if 0 <= choice < 4 else ''} принят ✅")
        if len(game.answers) >= len(game.players):
            game.answered.set()

    # ---------- the game ----------

    async def _play(self, bot, game):
        try:
            emoji, title = TOPICS[game.topic]
            await game.lobby_message.edit_caption(
                f"<b>🧠 Викторина</b> · {emoji} {esc(title)}\n⏳ Готовлю вопросы…",
                parse_mode="HTML")
            game.questions = await self._questions(game)
            loop = asyncio.get_running_loop()
            game.clips = [loop.create_future() for _ in game.questions]
            game.render = asyncio.create_task(self._render(game))
            game.state = "question"
            for game.index in range(len(game.questions)):
                await self._ask(bot, game)
                if game.state == "done":
                    return
            await self._finish(bot, game)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Quiz failed: %s %s", type(exc).__name__, str(exc)[:200])
            game.state = "done"
            try:
                await bot.send_message(game.chat_id, f"Викторина сломалась на полпути 😵 Попробуйте ещё раз: /{START}")
            except Exception:
                pass
        finally:
            if game.render:
                game.render.cancel()

    async def _render(self, game):
        """Draw the clips ahead of the game: (question clip, its preview, reveal clip, its preview), or None where
        drawing failed."""
        title = TOPICS[game.topic][1]
        for number, (question, options, correct, _) in enumerate(game.questions):
            try:
                art = quizart.QuestionArt(number + 1, len(game.questions), title, question, options, ANSWER_SECONDS)
                clips = await asyncio.to_thread(lambda: (art.question_clip(), art.question_thumb(),
                                                         art.reveal_clip(correct), art.reveal_thumb(correct)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Quiz clip failed: %s %s", type(exc).__name__, str(exc)[:200])
                clips = None
            if not game.clips[number].done():
                game.clips[number].set_result(clips)

    async def _ask(self, bot, game):
        question, options, correct, fact = game.questions[game.index]
        number, total = game.index + 1, len(game.questions)
        game.answers, game.answered, game.asked_at = {}, asyncio.Event(), 0.0
        self.mark_asked(game.chat_id, question)
        head = f"<b>Вопрос {number}/{total}</b> · {TOPICS[game.topic][0]} {esc(TOPICS[game.topic][1])}\n\n<b>{esc(question)}</b>\n\n"

        def waiting(seconds_left=None):
            count = f"ответили {len(game.answers)}/{len(game.players)}"
            if seconds_left is None:  # the animation has the timer; the question stays here too, readable without playing
                return f"<b>{number}/{total}. {esc(question)}</b>\n{count}"
            filled = round(10 * seconds_left / ANSWER_SECONDS)
            return head + f"⏱ {'▰' * filled}{'▱' * (10 - filled)} {seconds_left} с · {count}"

        keyboard = Keyboard([[Button(f"{LETTERS[i]}. {opt}"[:60], callback_data=f"g:a:{game.index}:{i}")]
                             for i, opt in enumerate(options)])
        try:
            clips = await asyncio.wait_for(asyncio.shield(game.clips[game.index]), 40)
        except asyncio.TimeoutError:
            clips = None
        message = None
        if clips:
            try:
                message = await bot.send_animation(game.chat_id, mp4(clips[0], "question"), caption=waiting(), parse_mode="HTML",
                                                   reply_markup=keyboard, width=quizart.W, height=quizart.H,
                                                   thumbnail=jpg(clips[1]))
            except Exception as exc:
                LOG.warning("Quiz animation failed: %s", type(exc).__name__)
                clips = None
        if not message:
            message = await bot.send_message(game.chat_id, waiting(ANSWER_SECONDS), parse_mode="HTML", reply_markup=keyboard)
        game.asked_at = time.monotonic()
        deadline = game.asked_at + ANSWER_SECONDS
        shown, step = 0, 3 if clips else 5
        while time.monotonic() < deadline and not game.answered.is_set():
            try:
                await asyncio.wait_for(game.answered.wait(), min(step, max(0.1, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                left = int(round(deadline - time.monotonic()))
                if left <= 0 or (clips and len(game.answers) == shown):
                    continue
                shown = len(game.answers)
                try:
                    if clips:
                        await message.edit_caption(waiting(), parse_mode="HTML", reply_markup=keyboard)
                    else:
                        await message.edit_text(waiting(left), parse_mode="HTML", reply_markup=keyboard)
                except Exception:
                    pass
        game.state = "reveal"
        gained, missed = [], []
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
                missed.append(esc(game.players[user_id]))
        result = f"<b>{number}/{total}. {esc(question)}</b>\n✅ <b>{esc(options[correct])}</b>" + (f"\n💡 {esc(fact)}" if fact else "")
        result += ("\n🏅 " + ", ".join(gained)) if gained else "\n😶 Никто не угадал"
        if missed:
            result += "\n❌ " + ", ".join(missed)
        try:
            if clips:
                await message.edit_media(InputMediaAnimation(mp4(clips[2], "answer"), caption=result, parse_mode="HTML",
                                                             width=quizart.W, height=quizart.H, thumbnail=jpg(clips[3])))
            else:
                await message.edit_text(result, parse_mode="HTML")
        except Exception as exc:
            LOG.warning("Quiz reveal failed: %s", type(exc).__name__)
            try:
                await bot.send_message(game.chat_id, result, parse_mode="HTML")
            except Exception:
                pass
        game.state = "question"
        await asyncio.sleep(REVEAL_PAUSE)

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
                  f"\n\nЕщё раз — /{START} · рейтинг группы — /{RATING}"
        topic = TOPICS[game.topic][1]
        try:
            clip, preview = await asyncio.to_thread(lambda: (quizart.podium_clip(rows, len(game.questions), topic),
                                                             quizart.podium_thumb(rows, len(game.questions), topic)))
            await bot.send_animation(game.chat_id, mp4(clip, "results"), caption=caption, parse_mode="HTML", width=720,
                                     height=900, thumbnail=jpg(preview))
        except Exception as exc:
            LOG.warning("Quiz podium animation failed: %s", type(exc).__name__)
            await bot.send_photo(game.chat_id, quizart.podium(rows, len(game.questions), topic), caption=caption,
                                 parse_mode="HTML")

    async def top(self, bot, chat_id, title):
        rows = self.db.execute("SELECT name, points, wins, games FROM game_scores WHERE chat_id=? ORDER BY points DESC "
                               "LIMIT 10", (chat_id,)).fetchall()
        if not rows:
            await bot.send_message(chat_id, f"Тут ещё никто не играл. Начать: /{START}")
            return
        await bot.send_photo(chat_id, quizart.leaderboard(rows, title),
                             caption=f"🏆 Рейтинг викторины за всё время · новая игра — /{START}")

    # ---------- questions ----------

    def asked(self, chat_id, limit=400):
        rows = self.db.execute("SELECT question FROM game_asked WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                               (chat_id, limit)).fetchall()
        return [row[0] for row in rows]

    def mark_asked(self, chat_id, question):
        with self.db:
            self.db.execute("INSERT INTO game_asked(chat_id, question) VALUES (?, ?)", (chat_id, question))
            self.db.execute("DELETE FROM game_asked WHERE chat_id=? AND id <= (SELECT id FROM game_asked WHERE chat_id=? "
                            "ORDER BY id DESC LIMIT 1 OFFSET 1000)", (chat_id, chat_id))

    async def _questions(self, game):
        seen = {key(q) for q in self.asked(game.chat_id)}
        try:
            questions = await self._generate(game, seen)
        except Exception as exc:
            LOG.warning("Quiz questions failed: %s %s", type(exc).__name__, str(exc)[:200])
            questions = []
        questions = questions[:QUESTIONS]
        if len(questions) < QUESTIONS:
            have = {key(q[0]) for q in questions}
            fresh = [q for q in FALLBACK if key(q[0]) not in seen | have]
            heard = [q for q in FALLBACK if key(q[0]) in seen - have]  # repeats only when the chat heard them all
            random.shuffle(fresh)
            random.shuffle(heard)
            questions += (fresh + heard)[:QUESTIONS - len(questions)]
        shuffled = []
        for question, options, correct, fact in questions:  # the right answer must not always be A
            order = list(range(len(options)))
            random.shuffle(order)
            shuffled.append((question, [options[i] for i in order], order.index(correct), fact))
        return shuffled

    async def _generate(self, game, seen):
        wanted = QUESTIONS + 4  # some get filtered out
        if game.topic == "chat":
            lines = self.history(game.chat_id)
            if len(lines) < 30:
                game.topic = "mix"  # too little to ask about
                return await self._generate(game, seen)
            names = ", ".join(game.players.values())
            subject = ("THIS group chat, from its messages below: who said a phrase, who talks about what, running jokes, "
                       f"the chat's words and habits. Players now: {names}. Use members' names as options. Funny, never "
                       "insulting, never revealing anything private. Messages:\n" + "\n".join(lines[-400:]))
        else:
            angles = random.sample(ANGLES[game.topic], min(5, len(ANGLES[game.topic])))
            subject = f"{TOPICS[game.topic][1]}. This time cover these angles: {', '.join(angles)}"
        recent = self.asked(game.chat_id, 120)
        avoid = ("\nThis chat has already played these — never repeat them or ask the same thing in other words:\n- " +
                 "\n- ".join(recent)) if recent else ""
        instructions = f"""Write {wanted} quiz questions in Russian for a group chat of Russian-speaking friends, mostly 16–35 years old.
Topic: {subject}

Difficulty is the most important part: MEDIUM. Every question should split the players roughly 50/50 — not school
trivia everyone answers instantly, and not obscure facts nobody could guess. Someone sharp should be able to recall it
or reason their way to it.

Rules:
- Make them interesting, the kind that makes people say «о, не знал». Modern things are welcome (internet, games,
  music, films, sport, technology of the last 15 years) mixed with timeless ones.
- No clichés: no capitals of famous countries, no biggest/smallest ocean or planet, no «who wrote War and Peace», no
  Berlin Wall year, Mickey Mouse, Gangnam Style, Doge and the like.
- The question must never contain or give away its answer (bad: «Какой элемент назван в честь планеты Уран?» — Уран).
- 4 options of the same kind, all plausible, short (max 40 characters). No joke options, no «все варианты».
- Exactly one correct option, and you must be certain of it. If you are not sure about a fact, drop that question.
- Vary the form: «что из этого…», «сколько примерно…», «что появилось раньше…», «кто…», «где…», «какой из…».
- fact: one short surprising sentence that adds something new rather than restating the answer. It must be true.
- No emoji anywhere.{avoid}

Return only JSON: {{"questions": [{{"q": "...", "options": ["...", "...", "...", "..."], "answer": 0, "fact": "..."}}]}}
where answer is the index of the correct option."""
        text = await self.ask_model(game.chat_id, instructions,
                                    f"Сделай {wanted} вопросов. Случайное зерно: {random.randint(1, 10 ** 6)}")
        found = re.search(r"\{.*\}", text or "", re.S)
        data = json.loads(found.group(0)) if found else {}
        questions, keys = [], set(seen)
        for item in data.get("questions", []):
            question = str(item.get("q", "")).strip()[:300]
            options = [str(o).strip()[:60] for o in item.get("options", []) if str(o).strip()]
            answer = item.get("answer")
            if not question or len(options) != 4 or not isinstance(answer, int) or not 0 <= answer < 4:
                continue
            if len({o.lower() for o in options}) < 4 or key(question) in keys:
                continue
            right = options[answer].lower()
            if len(right) >= 4 and right in question.lower():  # gives itself away
                continue
            keys.add(key(question))
            questions.append((question, options, answer, str(item.get("fact") or "").strip()[:300]))
        return questions

    def history(self, chat_id):
        rows = self.db.execute("SELECT line FROM group_messages WHERE chat_id=? AND line NOT LIKE 'Secret AI%' "
                               "ORDER BY id DESC LIMIT 600", (chat_id,)).fetchall()
        return [row[0] for row in reversed(rows)]
