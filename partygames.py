"""Party games for groups: «Шпион» (find the spy by asking questions) and «Крокодил» (guess what the bot drew).

Roles are shown only to the person who presses «Моя роль» (a Telegram pop-up), so nobody needs to open the
bot in private. Both games share one leaderboard per group.
"""
import asyncio
import html
import logging
import random
import re
import secrets
import time

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard

LOG = logging.getLogger(__name__)

SPY, SPY_STOP, CROC, TOP = "spy", "spy_stop", "croc", "games_top"
SPY_MIN, SPY_MAX = 3, 12
SPY_LOBBY_SECONDS = 600
SPY_TALK_SECONDS = 240  # questions in the chat, then the vote (the host can start it sooner)
SPY_VOTE_SECONDS = 40
CROC_SECONDS, CROC_HINT_SECONDS = 70, 30
POINTS = {"spy": 300, "agent": 100, "croc": 50}

LOCATIONS = [
    ("Пляж", ["Спасатель", "Отдыхающий", "Продавец кукурузы", "Сёрфер", "Фотограф"]),
    ("Больница", ["Хирург", "Медсестра", "Пациент", "Санитар", "Посетитель"]),
    ("Школа", ["Учитель", "Ученик", "Директор", "Охранник", "Повар в столовой"]),
    ("Самолёт", ["Пилот", "Стюардесса", "Пассажир", "Безбилетник", "Инженер"]),
    ("Банк", ["Кассир", "Охранник", "Грабитель", "Клиент", "Управляющий"]),
    ("Ресторан", ["Шеф-повар", "Официант", "Критик", "Посетитель", "Музыкант"]),
    ("Космическая станция", ["Командир", "Инженер", "Учёный", "Инопланетянин", "Турист"]),
    ("Подводная лодка", ["Капитан", "Кок", "Штурман", "Акустик", "Матрос"]),
    ("Цирк", ["Клоун", "Акробат", "Дрессировщик", "Фокусник", "Зритель"]),
    ("Кинотеатр", ["Киномеханик", "Зритель", "Кассир", "Режиссёр на премьере", "Уборщик"]),
    ("Полицейский участок", ["Детектив", "Дежурный", "Задержанный", "Адвокат", "Журналист"]),
    ("Пиратский корабль", ["Капитан", "Кок", "Юнга", "Пленник", "Попугай"]),
    ("Поезд", ["Машинист", "Проводник", "Пассажир", "Контролёр", "Торговец"]),
    ("Супермаркет", ["Кассир", "Охранник", "Покупатель", "Грузчик", "Промоутер"]),
    ("Спортзал", ["Тренер", "Новичок", "Качок", "Администратор", "Йог"]),
    ("Свадьба", ["Жених", "Невеста", "Тамада", "Фотограф", "Гость"]),
    ("Университет", ["Профессор", "Студент", "Декан", "Лаборант", "Абитуриент"]),
    ("Военная база", ["Генерал", "Солдат", "Повар", "Связист", "Новобранец"]),
    ("Казино", ["Крупье", "Игрок", "Охранник", "Бармен", "Шулер"]),
    ("Театр", ["Актёр", "Режиссёр", "Суфлёр", "Гардеробщик", "Зритель"]),
    ("Отель", ["Портье", "Горничная", "Турист", "Шеф-повар", "Менеджер"]),
    ("Ферма", ["Фермер", "Ветеринар", "Тракторист", "Доярка", "Пугало"]),
    ("Музей", ["Экскурсовод", "Смотритель", "Посетитель", "Реставратор", "Похититель картины"]),
    ("Зоопарк", ["Смотритель", "Ветеринар", "Посетитель", "Фотограф", "Продавец мороженого"]),
    ("Полярная станция", ["Начальник", "Метеоролог", "Повар", "Геолог", "Радист"]),
    ("Автосервис", ["Механик", "Клиент", "Мойщик", "Приёмщик", "Шиномонтажник"]),
    ("Концерт", ["Певец", "Звукорежиссёр", "Фанат", "Охранник", "Барабанщик"]),
    ("Детский сад", ["Воспитатель", "Ребёнок", "Повар", "Медсестра", "Родитель"]),
    ("Парикмахерская", ["Стилист", "Клиент", "Мастер маникюра", "Администратор", "Ученик"]),
    ("Стройка", ["Прораб", "Крановщик", "Каменщик", "Инженер", "Сторож"]),
]

WORDS = [
    ("велосипед", "a bicycle"), ("слон", "an elephant"), ("ракета", "a rocket"), ("маяк", "a lighthouse"),
    ("арбуз", "a watermelon"), ("пингвин", "a penguin"), ("гитара", "a guitar"), ("замок", "a medieval castle"),
    ("вертолёт", "a helicopter"), ("кактус", "a cactus in a pot"), ("жираф", "a giraffe"), ("робот", "a robot"),
    ("зонт", "an umbrella"), ("подсолнух", "a sunflower"), ("корабль", "a sailing ship"), ("дракон", "a dragon"),
    ("будильник", "an alarm clock"), ("медведь", "a bear"), ("радуга", "a rainbow"), ("снеговик", "a snowman"),
    ("пицца", "a pizza"), ("черепаха", "a turtle"), ("вулкан", "a volcano"), ("лампа", "a desk lamp"),
    ("сова", "an owl"), ("поезд", "a steam train"), ("корона", "a golden crown"), ("осьминог", "an octopus"),
    ("палатка", "a camping tent"), ("якорь", "a ship anchor"), ("бабочка", "a butterfly"), ("фонарик", "a flashlight"),
    ("мороженое", "an ice cream cone"), ("кит", "a whale"), ("ключ", "an old key"), ("телескоп", "a telescope"),
    ("торт", "a birthday cake"), ("лиса", "a fox"), ("мельница", "a windmill"), ("микрофон", "a microphone"),
    ("компас", "a compass"), ("акула", "a shark"), ("самокат", "a kick scooter"), ("пирамида", "an egyptian pyramid"),
    ("улитка", "a snail"), ("фламинго", "a flamingo"), ("ёлка", "a christmas tree"), ("трактор", "a tractor"),
    ("попугай", "a parrot"), ("скрипка", "a violin"), ("кенгуру", "a kangaroo"), ("молния", "a lightning bolt"),
    ("воздушный шар", "a hot air balloon"), ("холодильник", "a fridge"), ("паук", "a spider on a web"),
    ("колесо обозрения", "a ferris wheel"), ("рыцарь", "a knight in armor"), ("ананас", "a pineapple"),
    ("павлин", "a peacock"), ("подводная лодка", "a submarine"), ("ёж", "a hedgehog"), ("барабан", "a drum"),
]


def esc(text):
    return html.escape(str(text or ""), quote=False)


def normalize(text):
    return re.sub(r"[^a-zа-я0-9 ]+", " ", (text or "").lower().replace("ё", "е")).split()


def guessed(text, word):
    """Is the word in the message? Endings may differ: «велосипеда», «слоном», «воздушный шарик»."""
    wanted, said = normalize(word), normalize(text)
    if not wanted or not said:
        return False

    def same(a, b):
        if a == b or (len(b) >= 3 and a.startswith(b) and len(a) - len(b) <= 3):
            return True
        return len(b) >= 4 and a.startswith(b[:max(3, len(b) - 2)]) and abs(len(a) - len(b)) <= 3

    for start in range(len(said) - len(wanted) + 1):
        if all(same(said[start + i], part) for i, part in enumerate(wanted)):
            return True
    return False


def hint(word):
    return " ".join(part[0].upper() + "·" * (len(part) - 1) for part in word.split())


class Spy:
    def __init__(self, chat_id, host_id, host_name):
        self.chat_id, self.host_id = chat_id, host_id
        self.players = {host_id: host_name}
        self.session = secrets.token_hex(4)
        self.state = "lobby"  # lobby, talk, guess, vote, done
        self.location, self.roles, self.spy = "", {}, None
        self.message = None
        self.votes = {}
        self.vote_now = asyncio.Event()
        self.task = None
        self.created = time.monotonic()


class Croc:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.session = secrets.token_hex(4)
        self.word, self.message, self.task = "", None, None
        self.state = "drawing"  # drawing, open, done


class PartyGames:
    def __init__(self, db, draw):
        """draw(prompt) -> image bytes."""
        self.db, self.draw = db, draw
        self.spies, self.crocs = {}, {}
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS party_scores (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                            "name TEXT NOT NULL, points INTEGER NOT NULL, wins INTEGER NOT NULL, "
                            "PRIMARY KEY (chat_id, user_id))")

    def score(self, chat_id, user_id, name, points):
        with self.db:
            self.db.execute("INSERT INTO party_scores(chat_id, user_id, name, points, wins) VALUES (?, ?, ?, ?, 1) "
                            "ON CONFLICT(chat_id, user_id) DO UPDATE SET name=excluded.name, points=points+excluded.points, "
                            "wins=wins+1", (chat_id, user_id, name[:40], points))

    async def top(self, bot, chat_id, title):
        rows = self.db.execute("SELECT name, points, wins FROM party_scores WHERE chat_id=? ORDER BY points DESC LIMIT 10",
                               (chat_id,)).fetchall()
        if not rows:
            await bot.send_message(chat_id, f"Здесь ещё никто не выигрывал. Сыграйте: /{SPY} или /{CROC}")
            return
        lines = [f"🏆 <b>Рейтинг игр</b> · {esc(title)}\n"]
        for i, (name, points, wins) in enumerate(rows):
            medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i + 1}."
            lines.append(f"{medal} <b>{esc(name[:22])}</b> · {points} очков · побед: {wins}")
        lines.append(f"\n<i>Шпион: /{SPY} · Крокодил: /{CROC}</i>")
        await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")

    # ---------- «Шпион» ----------

    def lobby_text(self, game):
        people = "\n".join(f"{i}. {esc(n[:20])}" + (" · ведущий" if uid == game.host_id else "")
                           for i, (uid, n) in enumerate(game.players.items(), 1))
        need = "можно начинать" if len(game.players) >= SPY_MIN else f"нужно ещё {SPY_MIN - len(game.players)}"
        return (f"🕵️ <b>Шпион</b>\n\n<blockquote>Все, кроме одного, знают, где вы находитесь. Шпион не знает — "
                f"он слушает и пытается понять. Задавайте друг другу вопросы о месте, но не называйте его.</blockquote>\n"
                f"👥 <b>Игроки</b> · {len(game.players)}/{SPY_MAX} · {need}\n{people}\n\n"
                f"<i>Ведущий запускает игру. Остановить: /{SPY_STOP}</i>")

    def lobby_keyboard(self, game):
        return Keyboard([[Button("✋ Играю", callback_data=f"s:j:{game.session}")],
                         [Button("▶️ Начать", callback_data=f"s:s:{game.session}")]])

    def play_keyboard(self, game):
        return Keyboard([[Button("🎭 Моя роль", callback_data=f"s:r:{game.session}")],
                         [Button("🕵️ Я шпион и знаю, где мы", callback_data=f"s:g:{game.session}")],
                         [Button("⏩ К голосованию", callback_data=f"s:n:{game.session}")]])

    async def open_spy(self, bot, message, user_id, name):
        chat_id = message.chat_id
        current = self.spies.get(chat_id)
        if current and current.state != "done" and time.monotonic() - current.created < SPY_LOBBY_SECONDS + 900:
            await message.reply_text("Шпион уже идёт — присоединяйтесь 👆")
            return
        game = self.spies[chat_id] = Spy(chat_id, user_id, name)
        game.message = await bot.send_message(chat_id, self.lobby_text(game), parse_mode="HTML",
                                              reply_markup=self.lobby_keyboard(game))
        game.task = asyncio.create_task(self._expire(game))

    async def _expire(self, game):
        await asyncio.sleep(SPY_LOBBY_SECONDS)
        if game.state == "lobby":
            game.state = "done"
            await self._edit(game.message, "⌛ Сбор в «Шпиона» отменён — игру так и не начали.")

    def stop_spy(self, chat_id, user_id, is_admin):
        game = self.spies.get(chat_id)
        if not game or game.state == "done":
            return f"Сейчас «Шпион» не идёт. Начать: /{SPY}"
        if user_id != game.host_id and not is_admin:
            return "Остановить игру может ведущий или админ"
        game.state = "done"
        if game.task:
            game.task.cancel()
        return "⏹ «Шпион» остановлен."

    async def _edit(self, message, text, markup=None):
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception as exc:
            LOG.debug("Party game edit skipped: %s", exc)

    async def on_spy_button(self, query, user, name, bot):
        parts = (query.data or "").split(":")
        game = self.spies.get(query.message.chat_id if query.message else 0)
        if not game or game.state == "done" or parts[-1] != game.session:
            await query.answer(f"Эта партия закончилась. Новая: /{SPY}")
            return
        action = parts[1]
        if action == "j":
            if game.state != "lobby":
                await query.answer("Игра уже идёт — дождитесь следующей")
            elif user.id in game.players:
                await query.answer("Вы уже в игре 👍")
            elif len(game.players) >= SPY_MAX:
                await query.answer("Мест больше нет")
            else:
                game.players[user.id] = name
                await query.answer("Вы в игре! ✋")
                await self._edit(game.message, self.lobby_text(game), self.lobby_keyboard(game))
        elif action == "s":
            if game.state != "lobby":
                await query.answer()
            elif user.id != game.host_id:
                await query.answer("Начать может только ведущий")
            elif len(game.players) < SPY_MIN:
                await query.answer(f"Нужно минимум {SPY_MIN} игрока — позовите друзей ✋", show_alert=True)
            else:
                await query.answer("Поехали! Нажмите «Моя роль» 🎭")
                if game.task:
                    game.task.cancel()
                game.task = asyncio.create_task(self._play_spy(bot, game))
        elif action == "r":
            if user.id not in game.roles:
                await query.answer("Вы не в этой партии — дождитесь следующей", show_alert=True)
            elif user.id == game.spy:
                await query.answer("🕵️ Вы — ШПИОН!\n\nМесто вы не знаете. Слушайте вопросы и ответы, не выдавайте себя. "
                                   "Догадались? Жмите «Я шпион и знаю, где мы».", show_alert=True)
            else:
                await query.answer(f"📍 Место: {game.location}\n🎭 Ваша роль: {game.roles[user.id]}\n\n"
                                   "Задавайте вопросы и вычислите шпиона. Место вслух не называйте!", show_alert=True)
        elif action == "g":
            if game.state != "talk":
                await query.answer("Сейчас нельзя")
            elif user.id != game.spy:
                await query.answer("Вы не шпион 🙂 Ищите его вопросами!", show_alert=True)
            else:
                await query.answer("Выберите место в сообщении ниже")
                await self._spy_guess(bot, game)
        elif action == "l":
            if game.state != "guess" or user.id != game.spy:
                await query.answer("Угадывает только шпион")
                return
            await query.answer()
            choice = parts[2] if len(parts) > 3 else ""
            await self._finish_spy(bot, game, spy_won=choice == game.location,
                                   reason=f"Шпион назвал место: <b>{esc(choice)}</b>.")
        elif action == "n":
            if game.state != "talk":
                await query.answer()
            elif user.id != game.host_id:
                await query.answer("Ускорить голосование может ведущий")
            else:
                await query.answer("Голосуем!")
                game.vote_now.set()
        elif action == "x":
            await self._vote(query, game, user, parts)
        else:
            await query.answer()

    async def _play_spy(self, bot, game):
        try:
            game.location, roles = random.choice(LOCATIONS)
            ids = list(game.players)
            game.spy = random.choice(ids)
            game.roles = {uid: random.choice(roles) for uid in ids}
            game.state = "talk"
            order = " → ".join(esc(game.players[uid][:16]) for uid in random.sample(ids, len(ids)))
            await self._edit(game.message,
                             f"🕵️ <b>Шпион</b> · игра началась\n\n"
                             f"<blockquote>1. Нажмите «🎭 Моя роль» — её увидите только вы.\n"
                             f"2. Задавайте друг другу вопросы о месте прямо в чате.\n"
                             f"3. Через {SPY_TALK_SECONDS // 60} минуты голосование: кто шпион?</blockquote>\n"
                             f"Первым спрашивает: {order}", self.play_keyboard(game))
            try:
                await asyncio.wait_for(game.vote_now.wait(), SPY_TALK_SECONDS)
            except asyncio.TimeoutError:
                pass
            if game.state == "talk":
                await self._run_vote(bot, game)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Spy game failed: %s", type(exc).__name__)
            game.state = "done"

    async def _spy_guess(self, bot, game):
        game.state = "guess"
        if game.task:
            game.task.cancel()
        others = [name for name, _ in LOCATIONS if name != game.location]
        options = random.sample(others, 9) + [game.location]
        random.shuffle(options)
        rows = [[Button(o, callback_data=f"s:l:{o}:{game.session}") for o in options[i:i + 2]] for i in range(0, 10, 2)]
        await bot.send_message(game.chat_id, f"🕵️ <b>{esc(game.players[game.spy])}</b> раскрывается: я шпион! "
                                             f"Сейчас назову, где мы…", parse_mode="HTML", reply_markup=Keyboard(rows))
        game.task = asyncio.create_task(self._guess_timeout(bot, game))

    async def _guess_timeout(self, bot, game):
        await asyncio.sleep(SPY_VOTE_SECONDS)
        if game.state == "guess":
            await self._finish_spy(bot, game, spy_won=False, reason="Шпион не успел назвать место.")

    async def _run_vote(self, bot, game):
        game.state = "vote"
        game.votes = {}
        rows = [[Button(f"🕵️ {name[:24]}", callback_data=f"s:x:{uid}:{game.session}")] for uid, name in game.players.items()]
        await bot.send_message(game.chat_id, f"🗳 <b>Кто шпион?</b>\n\nГолосуйте — {SPY_VOTE_SECONDS} секунд. "
                                             f"Если большинство угадает, агенты победят.", parse_mode="HTML",
                               reply_markup=Keyboard(rows))
        deadline = time.monotonic() + SPY_VOTE_SECONDS
        while time.monotonic() < deadline and game.state == "vote" and len(game.votes) < len(game.players):
            await asyncio.sleep(1)
        if game.state != "vote":
            return
        tally = {}
        for target in game.votes.values():
            tally[target] = tally.get(target, 0) + 1
        top = max(tally.values()) if tally else 0
        leaders = [uid for uid, count in tally.items() if count == top]
        if len(leaders) != 1:
            await self._finish_spy(bot, game, spy_won=True, reason="Голоса разделились — шпион ускользнул.")
            return
        accused = leaders[0]
        name = esc(game.players[accused])
        await self._finish_spy(bot, game, spy_won=accused != game.spy,
                               reason=f"Большинство указало на <b>{name}</b>.")

    async def _vote(self, query, game, user, parts):
        if game.state != "vote":
            await query.answer("Сейчас не голосуем")
            return
        if user.id not in game.players:
            await query.answer("Голосуют только игроки")
            return
        try:
            target = int(parts[2])
        except (IndexError, ValueError):
            await query.answer()
            return
        if target == user.id:
            await query.answer("За себя нельзя 🙂")
            return
        game.votes[user.id] = target
        await query.answer(f"Голос принят: {game.players.get(target, '')[:24]}")

    async def _finish_spy(self, bot, game, spy_won, reason):
        if game.state == "done":
            return
        game.state = "done"
        spy = esc(game.players[game.spy])
        if spy_won:
            self.score(game.chat_id, game.spy, game.players[game.spy], POINTS["spy"])
            verdict = f"🕵️ <b>Победил шпион — {spy}!</b>"
        else:
            for uid, name in game.players.items():
                if uid != game.spy:
                    self.score(game.chat_id, uid, name, POINTS["agent"])
            verdict = f"🎉 <b>Агенты победили!</b> Шпионом был {spy}."
        await bot.send_message(game.chat_id, f"{reason}\n\n{verdict}\n📍 Место: <b>{esc(game.location)}</b>\n\n"
                                             f"🎮 Ещё раз: /{SPY} · 🏆 Рейтинг: /{TOP}", parse_mode="HTML")

    # ---------- «Крокодил» ----------

    async def open_croc(self, bot, chat_id):
        current = self.crocs.get(chat_id)
        if current and current.state in ("drawing", "open"):
            return False
        game = self.crocs[chat_id] = Croc(chat_id)
        game.task = asyncio.create_task(self._play_croc(bot, game))
        return True

    async def _play_croc(self, bot, game):
        word, prompt = random.choice(WORDS)
        game.word = word
        try:
            image = await self.draw(f"{prompt}, simple bold illustration, centered, plain background, no text, no letters")
        except Exception as exc:
            LOG.warning("Crocodile drawing failed: %s", type(exc).__name__)
            game.state = "done"
            await bot.send_message(game.chat_id, "😵 Не получилось нарисовать — попробуйте /croc ещё раз.")
            return
        game.state = "open"
        caption = (f"🐊 <b>Крокодил</b> · что я нарисовал?\n\nПишите ответ в чат — первый угадавший получает очки. "
                   f"⏱ {CROC_SECONDS} секунд, подсказка через {CROC_HINT_SECONDS}.")
        game.message = await bot.send_photo(game.chat_id, image, caption=caption, parse_mode="HTML")
        await asyncio.sleep(CROC_HINT_SECONDS)
        if game.state != "open":
            return
        try:
            await game.message.edit_caption(caption=f"{caption}\n\n💡 Подсказка: <b>{hint(word)}</b>", parse_mode="HTML")
        except Exception as exc:
            LOG.debug("Crocodile hint skipped: %s", exc)
        await asyncio.sleep(CROC_SECONDS - CROC_HINT_SECONDS)
        if game.state == "open":
            game.state = "done"
            await bot.send_message(game.chat_id, f"⏱ Никто не угадал. Это был <b>{esc(word)}</b>.", parse_mode="HTML",
                                   reply_markup=self.again_keyboard())

    def again_keyboard(self):
        return Keyboard([[Button("🐊 Ещё картинку", callback_data="c:n")]])

    async def check_guess(self, bot, message, user_id, name):
        """A message in a group: True when it guessed the current crocodile word (the bot answers, nothing else does)."""
        game = self.crocs.get(message.chat_id)
        if not game or game.state != "open" or not guessed(message.text or "", game.word):
            return False
        game.state = "done"
        if game.task:
            game.task.cancel()
        self.score(game.chat_id, user_id, name, POINTS["croc"])
        await message.reply_text(f"🎉 <b>{esc(name)}</b> угадывает: <b>{esc(game.word)}</b>! +{POINTS['croc']} очков",
                                 parse_mode="HTML", reply_markup=self.again_keyboard())
        return True
