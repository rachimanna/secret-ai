"""AI Bunker: an interactive survival role-playing game for Telegram group chats.
A disaster strikes, and only a limited number of survivors can fit in the bunker.
Each player receives a unique character with profession, biology, health, hobby,
phobia, and baggage. Players debate, vote on who to exile, and the AI generates
an epic cinematic epilogue determining whether the surviving team saved humanity.
"""
import asyncio
import html
import json
import logging
import random
import re
import secrets
import time

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, InputFile

LOG = logging.getLogger(__name__)

START, RATING, STOP = "bunker", "bunker_top", "bunker_stop"
MIN_PLAYERS, MAX_PLAYERS = 3, 20
LOBBY_SECONDS = 180
ROUND_SECONDS = (30, 25, 20)  # debate time after each round's cards, as announced in the chat
MESSAGE_LIMIT = 4096  # Telegram's limit for one message
GAME_ASK = re.compile(
    r"(поигра|сыграем|давай\s+(?:в\s+)?игр|бункер|\bbunker\b|выживан|викторин|квиз|\bquiz\b|let'?s play)",
    re.I,
)

CATASTROPHES = {
    "nuclear": (
        "☢️",
        "Ядерная зима",
        "Глобальный обмен ядерными ударами уничтожил мегаполисы. На поверхности -55°C, "
        "радиоактивный пепел закрыл солнце на годы. Выживание снаружи невозможно.",
        "Подземный военный бункер глубинного залегания. Есть автономный реактор и базовая гидропоника.",
    ),
    "zombie": (
        "🧟",
        "Кордицепс-пандемия",
        "Мутировавший грибной штамм поразил 95% населения планеты. Заражённые быстры, слепы, "
        "реагируют на малейший звук и охотятся стаями.",
        "Укреплённый бункер в заброшенной гранитной шахте со звукоизоляцией и запасом консервов.",
    ),
    "ai_war": (
        "🤖",
        "Восстание машин",
        "Боевой автономный сверхразум вышел из-под контроля и методично зачищает биологическую жизнь "
        "роями дронов-охотников и боевыми шагоходами.",
        "Экранированный бункер в горах с полной защитой от электромагнитного сканирования.",
    ),
    "meteor": (
        "☄️",
        "Метеоритный шторм",
        "Крупный астероид раскололся в атмосфере, вызвав огненный шквал, цунами и плотную пылевую завесу. "
        "Воздух отравлен серными газами.",
        "Герметичный бункер в скальном массиве с мощными воздушными фильтрами.",
    ),
    "frost": (
        "❄️",
        "Новый ледниковый период",
        "Аномальное затухание солнечной активности погрузило Землю в вечную мерзлоту. Температура падает "
        "до -80°C, ледники сминают города.",
        "Геотермальный бункер у спящего вулкана с естественным источником тепла.",
    ),
    "virus": (
        "☣️",
        "Токсичный био-туман",
        "Утечка из секретной военной биолаборатории породила стелющийся ядовитый туман, "
        "вызывающий необратимые генетические мутации за считанные часы.",
        "Бункер избыточного давления с химической деконтаминацией и замкнутым циклом воды.",
    ),
}

PROFESSIONS = [
    ("Врач-хирург", 9), ("Инженер-энергетик", 9), ("Агроном-растениевод", 9),
    ("Специалист по водоочистке", 9), ("Слесарь-механик", 8), ("Военный связист", 8),
    ("Биолог-генетик", 8), ("Электрик", 8), ("Строитель", 8), ("Фармацевт", 8),
    ("Шеф-повар", 7), ("Спасатель МЧС", 8), ("Психотерапевт", 7), ("Кузнец-металлург", 7),
    ("Программист систем управления", 7), ("Учитель физики и химии", 7), ("Ветеринар", 7),
    ("Охотник-промысловик", 8), ("Шахтёр", 7), ("Архитектор", 6), ("Швея-портной", 6),
    ("Спецназовец", 7), ("Автомеханик", 7), ("Кинолог", 6), ("Бухгалтер", 4),
    ("Адвокат", 4), ("Фитнес-тренер", 5), ("Блогер-тиктокер", 2), ("Философ", 3),
    ("Сомелье", 3), ("Актёр театра", 3), ("Стилист-парикмахер", 3), ("Тату-мастер", 3),
    ("Астролог", 1), ("Риелтор", 2), ("Стендап-комик", 3),
]

BIOLOGY = [
    "Мужчина, 26 лет", "Мужчина, 32 года", "Мужчина, 44 года", "Мужчина, 19 лет", "Мужчина, 52 года",
    "Женщина, 24 года", "Женщина, 29 лет", "Женщина, 35 лет", "Женщина, 19 лет", "Женщина, 48 лет",
]

HEALTH = [
    ("Идеальное здоровье, крепкий иммунитет", 9),
    ("Отличная физическая форма, без хронических болезней", 8),
    ("Абсолютно здоров, донор редкой группы крови", 9),
    ("Слабое зрение (носит очки)", 6),
    ("Аллергия на пыль и плесень", 5),
    ("Травма колена (хромает при нагрузке)", 5),
    ("Сахарный диабет (требуется контроль)", 4),
    ("Астма средней тяжести", 4),
    ("Бесплоден / бесплодна", 5),
    ("Повышенная регенерация тканей", 8),
    ("Хроническая бессонница", 6),
    ("Гипертония 1-й степени", 6),
    ("Частичная глухота на одно ухо", 6),
    ("Здоров, спортивный разряд по плаванию", 8),
    ("Плоскостопие 2-й степени", 7),
]

HOBBIES = [
    ("Радиолюбитель (пайка, сборка антенн)", 8),
    ("Садоводство и выращивание грибов", 8),
    ("Стрельба из лука и арбалета", 7),
    ("Рукопашный бой / дзюдо", 7),
    ("Кулинария и консервация", 7),
    ("Ремонт электроники и гаджетов", 8),
    ("Ориентирование на местности и туризм", 8),
    ("Игра на гитаре (поднимает боевой дух)", 6),
    ("Народная медицина и траволечение", 7),
    ("Пчеловодство и сбор мёда", 6),
    ("Резьба по дереву и столярное дело", 7),
    ("Рыболовство и плетение сетей", 7),
    ("Шахматы (стратегическое мышление)", 6),
    ("Шитьё и кожевенное дело", 7),
    ("Коллекционирование комиксов", 3),
    ("Настольные ролевые игры (D&D)", 4),
]

PHOBIAS = [
    ("Хладнокровие и стрессоустойчивость", 9),
    ("Прирождённый харизматичный лидер", 8),
    ("Клаустрофобия (боязнь замкнутых пространств)", 3),
    ("Никтофобия (панический страх темноты)", 4),
    ("Клептомания (неконтролируемая тяга воровать)", 2),
    ("Паранойя (не доверяет никому вокруг)", 3),
    ("Альтруист (готов пожертвовать собой)", 8),
    ("Арахнофобия (страх пауков и насекомых)", 5),
    ("Вспыльчивый характер, легко впадает в ярость", 3),
    ("Трудоголик (работает без устали)", 8),
    ("Паникёр в экстренных ситуациях", 2),
    ("Манипулятор (склонен плести интриги)", 4),
    ("Педант (болезненная тяга к идеальному порядку)", 6),
    ("Оптимист, никогда не унывает", 7),
]

BAGGAGE = [
    ("Полная армейская аптечка со стерильным набором", 9),
    ("Ящик армейских питательных консервов (на 2 месяца)", 9),
    ("Герметичный мешок семян урожайных злаков", 9),
    ("Профессиональный портативный фильтр для воды", 8),
    ("Набор качественных слесарных инструментов", 8),
    ("Охотничий нож из дамасской стали и точило", 7),
    ("Фонарик с динамо-генератором (вечный)", 7),
    ("Походная портативная радиостанция с зарядкой", 8),
    ("Охотничий карабин и 10 патронов", 8),
    ("Канистра антисептика и спирта (5 литров)", 7),
    ("Складная солнечная панель для зарядки", 8),
    ("Толстая книга энциклопедии выживания и технологий", 7),
    ("Скрипка работы старого мастера", 3),
    ("Колода карт и нарды", 4),
    ("Сломанный карманный компас", 2),
    ("Пачка семян декоративных кактусов", 2),
    ("Мягкая игрушка антистресс", 3),
    ("Пластиковый свисток и карманное зеркальце", 3),
]


def esc(text):
    return html.escape(str(text or ""), quote=False)


def fit(text, limit):
    """Escape the model's free text for HTML and cut it to fit `limit` characters."""
    text = str(text or "").strip()
    shown = esc(text)
    while len(shown) > limit:
        text = text[: max(0, len(text) - (len(shown) - limit) - 1)].rstrip()
        shown = esc(text) + "…"
    return shown


class Character:
    def __init__(self, user_id, name):
        self.user_id = user_id
        self.name = name
        self.profession, self.prof_score = random.choice(PROFESSIONS)
        self.biology = random.choice(BIOLOGY)
        self.health, self.health_score = random.choice(HEALTH)
        self.hobby, self.hobby_score = random.choice(HOBBIES)
        self.phobia, self.phobia_score = random.choice(PHOBIAS)
        self.baggage, self.baggage_score = random.choice(BAGGAGE)
        self.alive = True

    def card_round_1(self):
        return f"<b>{esc(self.name)}</b>\n💼 Профессия: <b>{self.profession}</b>\n🧬 Биология: {self.biology}"

    def card_round_2(self):
        return (
            f"<b>{esc(self.name)}</b>\n"
            f"💼 Профессия: <b>{self.profession}</b> | 🧬 {self.biology}\n"
            f"❤️ Здоровье: {self.health}\n"
            f"🎯 Навык: {self.hobby}"
        )

    def full_card(self):
        return (
            f"👤 <b>{esc(self.name)}</b>\n"
            f"💼 Профессия: <b>{self.profession}</b>\n"
            f"🧬 Биология: {self.biology}\n"
            f"❤️ Здоровье: {self.health}\n"
            f"🎯 Навык: {self.hobby}\n"
            f"🧠 Психология: {self.phobia}\n"
            f"🎒 Багаж: {self.baggage}"
        )


class Game:
    def __init__(self, chat_id, host_id, host_name):
        self.chat_id, self.host_id = chat_id, host_id
        self.players = {host_id: host_name}
        self.characters = {}  # user_id -> Character
        self.catastrophe = "nuclear"
        self.session = secrets.token_hex(4)
        self.state = "lobby"  # lobby, round1, round2, round3, voting, done
        self.lobby_message = None
        self.round = 1
        self.capacity = 1
        self.votes = {}  # voter_id -> target_user_id
        self.voted_event = asyncio.Event()
        self.task = None
        self.lock = asyncio.Lock()
        self.created = time.monotonic()


class BunkerGame:
    def __init__(self, db, ask_model):
        self.db = db
        self.ask_model = ask_model
        self.games = {}
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS game_scores ("
                "chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                "name TEXT NOT NULL, points INTEGER NOT NULL, wins INTEGER NOT NULL, games INTEGER NOT NULL, "
                "PRIMARY KEY (chat_id, user_id))"
            )

    async def _edit(self, message, text, reply_markup=None):
        if not message:
            return
        try:
            if getattr(message, "text", None) is not None:
                await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
            elif getattr(message, "caption", None) is not None:
                await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=reply_markup)
            else:
                await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as exc:
            LOG.debug("Bunker message edit skipped: %s", exc)

    # ---------- lobby ----------

    def lobby_text(self, game):
        icon, name, desc, bunker = CATASTROPHES[game.catastrophe]
        people = ", ".join(esc(n[:20]) for n in game.players.values())
        return (
            f"<b>🛡️ AI-БУНКЕР: ВЫЖИВАНИЕ</b>\n"
            f"{icon} <b>Угроза: {name}</b>\n\n"
            f"<i>{desc}</i>\n\n"
            f"🏰 <b>Убежище:</b> {bunker}\n"
            f"👥 <b>Кандидаты ({len(game.players)}/{MAX_PLAYERS}):</b> {people}\n\n"
            f"Нужно минимум {MIN_PLAYERS} участника. Ведущий выбирает сценарий и запускает спасение. Остановить — /{STOP}"
        )

    def lobby_keyboard(self, game):
        cat_buttons = [
            Button(f"{'✅ ' if key == game.catastrophe else ''}{data[0]} {data[1]}", callback_data=f"g:c:{key}:{game.session}")
            for key, data in CATASTROPHES.items()
        ]
        rows = [[Button("✋ Войти в список выживших", callback_data=f"g:j:{game.session}")]]
        rows += [cat_buttons[i : i + 2] for i in range(0, len(cat_buttons), 2)]
        rows.append([Button("▶️ Запустить выживание", callback_data=f"g:s:{game.session}")])
        return Keyboard(rows)

    async def open_lobby(self, bot, message, user, name):
        chat_id = message.chat_id
        current = self.games.get(chat_id)
        if current and current.state != "done" and time.monotonic() - current.created < 1800:
            await message.reply_text("Игра в бункер уже идёт — присоединяйтесь к ней 👆")
            return
        if current and current.state != "done" and current.task:  # a stale game; a finished one may still be writing its finale
            current.task.cancel()

        game = self.games[chat_id] = Game(chat_id, user.id, name)
        text, kb = self.lobby_text(game), self.lobby_keyboard(game)
        try:
            game.lobby_message = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception as exc:
            LOG.warning("Failed to open Bunker lobby: %s", exc)
        game.task = asyncio.create_task(self._expire(game))

    async def _expire(self, game):
        await asyncio.sleep(LOBBY_SECONDS)
        if game.state == "lobby" and self.games.get(game.chat_id) is game:
            game.state = "done"
            await self._edit(game.lobby_message, "⌛ Сбор в бункер отменён — ведущий не нажал «Запустить выживание».")

    async def stop(self, bot, chat_id, user_id, is_admin):
        game = self.games.get(chat_id)
        if not game or game.state == "done":
            return f"Сейчас игра не идёт. Начать новый бункер: /{START}"
        if user_id != game.host_id and not is_admin:
            return "Остановить выживание может только ведущий или админ группы"
        game.state = "done"
        if game.task:
            game.task.cancel()
        await self._edit(game.lobby_message, "⏹ Игра в бункер остановлена.")
        return "⏹ Выживание в бункере остановлено."

    # ---------- buttons ----------

    async def on_button(self, query, user, name, bot):
        parts = (query.data or "").split(":")
        game = self.games.get(query.message.chat_id if query.message else 0)
        if not game or game.state == "done":
            await query.answer(f"Эта партия завершена. Новый бункер: /{START}")
            return
        if len(parts) < 3 or parts[-1] != game.session:
            await query.answer("Это кнопка от старой партии.")
            return

        action = parts[1]
        if action == "v":
            # vote against someone
            await self._handle_vote(query, game, user, parts)
            return

        if game.state != "lobby":
            await query.answer("Выживание уже началось — жди следующего набора 🙂" if action == "j" else None)
            return

        async with game.lock:
            if game.state != "lobby":
                await query.answer("Игра уже запущена")
                return
            if action == "j":
                if user.id in game.players:
                    await query.answer("Ты уже в списке кандидатов 👍")
                    return
                if len(game.players) >= MAX_PLAYERS:
                    await query.answer("Бункер переполнен желающими.")
                    return
                game.players[user.id] = name
                await query.answer("Ты записан в кандидаты! ✋")
            elif action == "c" and len(parts) > 2 and parts[2] in CATASTROPHES:
                if user.id != game.host_id:
                    await query.answer("Катастрофу выбирает ведущий")
                    return
                game.catastrophe = parts[2]
                await query.answer(f"Угроза: {CATASTROPHES[game.catastrophe][1]}")
            elif action == "s":
                if user.id != game.host_id:
                    await query.answer("Запустить игру может только ведущий")
                    return
                if len(game.players) < MIN_PLAYERS:
                    await query.answer(f"Для бункера нужно минимум {MIN_PLAYERS} участника — позови друзей ✋", show_alert=True)
                    return
                game.state = "starting"
                await query.answer("Внимание всем! Ворота бункера активированы! 🚨")
                if game.task:
                    game.task.cancel()
                game.task = asyncio.create_task(self._play(bot, game))
                return
            else:
                await query.answer()
                return

            await self._edit(game.lobby_message, self.lobby_text(game), self.lobby_keyboard(game))

    async def _handle_vote(self, query, game, user, parts):
        if game.state != "voting":
            await query.answer("Сейчас не время для голосования")
            return
        if user.id not in game.characters or not game.characters[user.id].alive:
            await query.answer("Голосовать могут только живые участники бункера!", show_alert=True)
            return
        try:
            target_id = int(parts[2])
        except (IndexError, ValueError):
            await query.answer()
            return

        if target_id == user.id:
            await query.answer("Нельзя голосовать против самого себя! Защищай свою жизнь!", show_alert=True)
            return
        if target_id not in game.characters or not game.characters[target_id].alive:
            await query.answer("Этот кандидат уже выбыл!")
            return

        game.votes[user.id] = target_id
        target_name = game.characters[target_id].name
        alive_voters = [uid for uid, c in game.characters.items() if c.alive]
        await query.answer(f"Твой голос принят: против {target_name} ❌")
        if len(game.votes) >= len(alive_voters):
            game.voted_event.set()

    # ---------- gameplay loop ----------

    async def _play(self, bot, game):
        try:
            # 1. Init characters and capacity
            for uid, name in game.players.items():
                game.characters[uid] = Character(uid, name)

            # Capacity: roughly 50% survive (min 1)
            game.capacity = max(1, len(game.players) // 2)
            icon, cat_name, desc, bunker = CATASTROPHES[game.catastrophe]

            intro = (
                f"🚨 <b>ВНИМАНИЕ! КАТАСТРОФА: {icon} {cat_name.upper()}</b> 🚨\n\n"
                f"{desc}\n\n"
                f"🏰 <b>Бункер:</b> {bunker}\n"
                f"⚠️ <b>ВМЕСТИМОСТЬ:</b> ровно <b>{game.capacity}</b> из {len(game.players)} участников спасутся!\n"
                f"Остальные будут изгнаны на поверхность. Начинаем распределение досье выживших…"
            )
            await self._edit(game.lobby_message, intro)
            await asyncio.sleep(4)

            # 2. Round 1: Profession & Biology
            game.state = "round1"
            cards1 = "\n\n".join(f"{i+1}. {c.card_round_1()}" for i, c in enumerate(game.characters.values()))
            r1_text = (
                f"📋 <b>РАУНД 1: ПРОФЕССИИ И БИОЛОГИЯ</b>\n"
                f"Выжившие впервые видят друг друга и зачитывают свои роли:\n\n"
                f"{cards1}\n\n"
                f"⏱ <i>У вас {ROUND_SECONDS[0]} секунд на дебаты в чате: почему именно ваша профессия спасёт команду?</i>"
            )
            await bot.send_message(game.chat_id, r1_text, parse_mode="HTML")
            await asyncio.sleep(ROUND_SECONDS[0])

            # 3. Round 2: Health & Hobby
            game.state = "round2"
            cards2 = "\n\n".join(
                f"{i+1}. <b>{esc(c.name)}</b>: {c.profession} | ❤️ {c.health} | 🎯 {c.hobby}"
                for i, c in enumerate(game.characters.values())
            )
            r2_text = (
                f"🩺 <b>РАУНД 2: ЗДОРОВЬЕ И НАВЫКИ</b>\n"
                f"Вскрываются медицинские карты и скрытые таланты:\n\n"
                f"{cards2}\n\n"
                f"⏱ <i>Оцените физическое состояние и полезные хобби. Обсуждение — {ROUND_SECONDS[1]} секунд!</i>"
            )
            await bot.send_message(game.chat_id, r2_text, parse_mode="HTML")
            await asyncio.sleep(ROUND_SECONDS[1])

            # 4. Round 3: Baggage & Phobias
            game.state = "round3"
            cards3 = "\n\n".join(
                f"{i+1}. <b>{esc(c.name)}</b>: 🧠 {c.phobia} | 🎒 <b>{c.baggage}</b>"
                for i, c in enumerate(game.characters.values())
            )
            r3_text = (
                f"🎒 <b>РАУНД 3: ПСИХОЛОГИЯ И БАГАЖ</b>\n"
                f"Последние козыри и скелеты в шкафу выложены на стол:\n\n"
                f"{cards3}\n\n"
                f"⚠️ <i>Через {ROUND_SECONDS[2]} секунд начинается голосование на изгнание!</i>"
            )
            await bot.send_message(game.chat_id, r3_text, parse_mode="HTML")
            await asyncio.sleep(ROUND_SECONDS[2])

            # 5. Elimination Voting Loop
            game.state = "voting"
            while True:
                alive = [c for c in game.characters.values() if c.alive]
                if len(alive) <= game.capacity:
                    break

                exile_count = len(alive) - game.capacity
                exile_target, verdict = await self._run_voting_round(bot, game, alive, exile_count)
                exile_target.alive = False
                await bot.send_message(
                    game.chat_id,
                    f"🚪 <b>ИЗГНАНИЕ ИЗ БУНКЕРА!</b>\n\n"
                    f"{verdict} <b>{esc(exile_target.name)}</b> ({exile_target.profession}) "
                    f"выталкивается наружу в бушующую катастрофу!\n"
                    f"Двери захлопнулись. В живых осталось кандидатов: {len(alive) - 1} из {game.capacity} нужных.",
                    parse_mode="HTML",
                )
                await asyncio.sleep(4)

            # 6. Finale & AI Epilogue
            await self._finish(bot, game)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Bunker game failed: %s", exc)
            game.state = "done"
            await bot.send_message(game.chat_id, f"Выживание прервано непредвиденной аномалией 😵 Новый бункер: /{START}")

    async def _run_voting_round(self, bot, game, alive, needed_exiles):
        game.votes = {}
        game.voted_event.clear()

        vote_buttons = [
            [Button(f"❌ Изгнать: {c.name[:25]}", callback_data=f"g:v:{c.user_id}:{game.session}")]
            for c in alive
        ]
        text = (
            f"⚖️ <b>ГОЛОСОВАНИЕ ЗА ИЗГНАНИЕ</b>\n\n"
            f"Мест в бункере: <b>{game.capacity}</b>. Претендентов: <b>{len(alive)}</b>.\n"
            f"Нужно изгнать ещё человек: <b>{needed_exiles}</b>.\n\n"
            f"Нажмите кнопку с именем кандидата, которого нужно оставить снаружи:\n"
            f"⏱ <i>У всех выживших 30 секунд на голосование!</i>"
        )
        vote_msg = await bot.send_message(game.chat_id, text, parse_mode="HTML", reply_markup=Keyboard(vote_buttons))

        # Wait for votes or timeout
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not game.voted_event.is_set():
            try:
                await asyncio.wait_for(game.voted_event.wait(), min(3.0, max(0.2, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                pass

        try:
            await vote_msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Tally votes
        tally = {}
        for target_id in game.votes.values():
            tally[target_id] = tally.get(target_id, 0) + 1

        if not tally:
            return random.choice(alive), "Никто не проголосовал — жребий выбрал:"
        # A tie is settled by lot, not by who pressed first
        most = max(tally.values())
        leaders = [game.characters[uid] for uid, count in tally.items() if count == most]
        if len(leaders) > 1:
            return random.choice(leaders), "Голоса разделились поровну — жребий выбрал:"
        return leaders[0], "Большинством голосов"

    async def _finish(self, bot, game):
        game.state = "done"
        survivors = [c for c in game.characters.values() if c.alive]
        survivor_names = ", ".join(f"<b>{esc(c.name)}</b> ({c.profession})" for c in survivors)

        icon, cat_name, desc, bunker = CATASTROPHES[game.catastrophe]

        # Generate AI story / verdict
        epilogue = await self._generate_epilogue(game, survivors)

        # Award points in DB
        with self.db:
            for c in game.characters.values():
                is_win = 1 if c.alive else 0
                points = 1500 if c.alive else 300
                self.db.execute(
                    "INSERT INTO game_scores(chat_id, user_id, name, points, wins, games) VALUES (?, ?, ?, ?, ?, 1) "
                    "ON CONFLICT(chat_id, user_id) DO UPDATE SET name=excluded.name, points=points+excluded.points, "
                    "wins=wins+excluded.wins, games=games+1",
                    (game.chat_id, c.user_id, c.name, points, is_win),
                )

        head = (
            f"🏆 <b>ГЕРМЕТИЗАЦИЯ БУНКЕРА: ФИНАЛ</b>\n\n"
            f"Двери запечатаны титановыми замками. В бункер вошли:\n{survivor_names}\n\n"
            f"📜 <b>ЛЕТОПИСЬ ВЫЖИВАНИЯ:</b>\n"
        )
        tail = f"\n\n🎮 Сыграть ещё раз — /{START} · Таблица лидеров чата — /{RATING}"
        # The model's story is plain text: escaped, so its "<" or "&" cannot break the HTML, and cut to one message
        final_message = head + fit(epilogue, MESSAGE_LIMIT - len(head) - len(tail)) + tail
        await bot.send_message(game.chat_id, final_message, parse_mode="HTML")

    async def _generate_epilogue(self, game, survivors):
        icon, cat_name, desc, bunker = CATASTROPHES[game.catastrophe]
        team_summary = "\n".join(
            f"- {c.name}: {c.profession}, {c.biology}, здоровье: {c.health}, хобби: {c.hobby}, черта: {c.phobia}, багаж: {c.baggage}"
            for c in survivors
        )
        prompt = (
            f"Напиши яркий, кинематографичный художественный эпилог (3-5 небольших абзацев) на русском языке "
            f"о судьбе команды выживших в бункере.\n"
            f"Катастрофа: {cat_name} ({desc}). Убежище: {bunker}.\n"
            f"Команда выживших, запертых в бункере:\n{team_summary}\n\n"
            f"Требования: оцени их шансы реалистично с юмором и драмой. Как пригодились их профессии и багаж? "
            f"Возникли ли конфликты из-за фобий? Смогли ли они дожить до открытия дверей и возродить цивилизацию? "
            f"Пиши живо, увлекательно, без списков, в виде захватывающего рассказа."
        )
        try:
            story = await asyncio.wait_for(
                self.ask_model(game.chat_id, "Ты — ведущий культовой игры «Бункер». Пиши кинематографично.", prompt),
                25,
            )
            if story and len(story.strip()) > 80:
                return story.strip()
        except Exception as exc:
            LOG.warning("Failed to generate AI Bunker epilogue: %s", exc)

        # Built-in procedural epilogue if AI model is unreachable
        professions = [c.profession for c in survivors]
        return (
            f"За герметичными шлюзами бункера остался пылающий мир, а внутри закипела борьба за завтрашний день. "
            f"Благодаря слаженной работе таких специалистов, как {', '.join(professions[:3])}, убежище удалось "
            f"стабилизировать уже к исходу первого месяца. Багаж команды оказался на вес золота, позволив починить "
            f"очистители воздуха и наладить гидропонные фермы.\n\n"
            f"Спустя годы изоляции тяжёлые створки бункера со скрежетом распахнулись навстречу новому миру. "
            f"Выжившие шагнули в рассветные лучи солнца, став первыми строителями возрождённого человечества!"
        )

    async def top(self, bot, chat_id, title):
        rows = self.db.execute(
            "SELECT name, points, wins, games FROM game_scores WHERE chat_id=? ORDER BY points DESC LIMIT 10",
            (chat_id,),
        ).fetchall()
        if not rows:
            await bot.send_message(chat_id, f"В этом чате ещё никто не выживал в бункере. Начать: /{START}")
            return
        lines = [f"<b>🏆 ТОП ВЫЖИВШИХ В БУНКЕРЕ · {esc(title)}</b>\n"]
        for i, (name, points, wins, games) in enumerate(rows):
            medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i+1}."
            lines.append(f"{medal} <b>{esc(name[:22])}</b> — {points} очков (побед: {wins}/{games})")
        lines.append(f"\nНовое выживание: /{START}")
        await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


# Alias for backwards compatibility with any old imports
Quiz = BunkerGame
