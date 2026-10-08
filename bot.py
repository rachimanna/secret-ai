"""Secret AI: Telegram polling bot with a Render HTTP health endpoint."""
import asyncio
import base64
import io
import logging
import os
import sqlite3
import tempfile
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import imageio_ffmpeg
from openai import (
    APIConnectionError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

LOG = logging.getLogger("secret_ai")
SYSTEM = (
    "You are Secret AI, a helpful, accurate and friendly assistant. "
    "Introduce yourself as Secret AI when asked. Reply in the user's language. "
    "Use the language of their latest message, caption or voice transcription; "
    "if none is available, use their Telegram language. Admit uncertainty. "
    "Reply in plain text, without Markdown formatting. "
    "Previous photo markers are not images; do not invent unseen visual details."
)
PHOTO_MARKER = "[Photo attached; image is not retained] "
HISTORY_LIMIT = 30

# id: (emoji, Russian name, English name, extra instructions)
PERSONAS = {
    "assistant": ("🕶", "Ассистент", "Assistant", ""),
    "coder": ("💻", "Программист", "Coder",
              "Act as a senior software engineer: give complete working code, explain decisions briefly and point out pitfalls."),
    "translator": ("🌍", "Переводчик", "Translator",
                   "Act as a translator: translate Russian text into English and any other language into Russian. "
                   "Preserve tone and line breaks. Output only the translation."),
    "creative": ("✨", "Креатив", "Creative",
                 "Be bold and original: suggest unexpected ideas and write vividly."),
}
DEFAULT_PERSONA = "assistant"

THINKING = {
    "ru": ["🤔 Думаю", "🧠 Анализирую вопрос", "🔎 Ищу лучший ответ", "✍️ Формулирую", "⏳ Почти готово"],
    "en": ["🤔 Thinking", "🧠 Analyzing your question", "🔎 Looking for the best answer", "✍️ Writing", "⏳ Almost done"],
}
SPINNER = "◐◓◑◒"
CURSOR = " ▌"
# Telegram allows roughly one edit per second per chat; faster edits get RetryAfter.
ANIMATION_INTERVAL = 1.5
STREAM_EDIT_INTERVAL = 1.2
TYPING_INTERVAL = 4.5


def parse_allowed(value):
    ids = set()
    for part in value.split(","):
        if part.strip():
            user_id = int(part.strip())
            if user_id <= 0:
                raise ValueError("ALLOWED_USERS must contain positive Telegram IDs")
            ids.add(user_id)
    return ids


class Memory:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS by_user ON messages(user_id, id)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (user_id INTEGER PRIMARY KEY, persona TEXT NOT NULL)")
        self.db.commit()

    def history(self, user_id):
        rows = self.db.execute("SELECT role, content FROM (SELECT id, role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?) ORDER BY id", (user_id, HISTORY_LIMIT)).fetchall()
        return [{"role": role, "content": content} for role, content in rows]

    def append_turn(self, user_id, question, answer):
        with self.db:
            self.db.executemany("INSERT INTO messages(user_id, role, content) VALUES (?, ?, ?)", [(user_id, "user", question), (user_id, "assistant", answer)])
            self.db.execute("DELETE FROM messages WHERE user_id=? AND id NOT IN (SELECT id FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?)", (user_id, user_id, HISTORY_LIMIT))

    def _last_turn(self, user_id):
        rows = self.db.execute("SELECT id, role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 2", (user_id,)).fetchall()
        if len(rows) < 2 or rows[0][1] != "assistant" or rows[1][1] != "user":
            return None
        return rows

    def last_question(self, user_id):
        rows = self._last_turn(user_id)
        return rows[1][2] if rows else None

    def pop_last_turn(self, user_id):
        """Remove the latest question/answer pair and return the question, or None."""
        rows = self._last_turn(user_id)
        if not rows:
            return None
        with self.db:
            self.db.execute("DELETE FROM messages WHERE id IN (?, ?)", (rows[0][0], rows[1][0]))
        return rows[1][2]

    def reset(self, user_id):
        with self.db:
            self.db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))

    def persona(self, user_id):
        row = self.db.execute("SELECT persona FROM settings WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row and row[0] in PERSONAS else DEFAULT_PERSONA

    def set_persona(self, user_id, persona):
        with self.db:
            self.db.execute("INSERT INTO settings(user_id, persona) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET persona=excluded.persona", (user_id, persona))

    def close(self):
        self.db.close()


def chunks(text, limit=4000):
    """Split text into Telegram-sized parts, preferring paragraph, line and word boundaries.

    Sizes are counted in UTF-16 units, conservatively including astral emoji.
    """
    while text:
        units, cut = 0, len(text)
        for index, char in enumerate(text):
            units += 2 if ord(char) > 0xFFFF else 1
            if units > limit:
                cut = index
                break
        if cut < len(text):
            for separator in ("\n\n", "\n", " "):
                found = text.rfind(separator, 0, cut)
                if found > cut // 2:
                    cut = found + len(separator)
                    break
        part, text = text[:cut], text[cut:]
        if part.strip():
            yield part


def seconds(delay):
    return delay.total_seconds() if hasattr(delay, "total_seconds") else delay


async def reply(message, text, reply_markup=None):
    for attempt in range(3):
        try:
            return await message.reply_text(text, parse_mode=None, reply_markup=reply_markup)
        except RetryAfter as exc:
            if attempt == 2:
                raise
            await asyncio.sleep(seconds(exc.retry_after))


async def send(message, text, reply_markup=None):
    parts = list(chunks(text))
    for index, part in enumerate(parts):
        await reply(message, part, reply_markup if index == len(parts) - 1 else None)


def lang(update):
    return "ru" if (update.effective_user.language_code or "ru").startswith("ru") else "en"


def ui(update, ru, en):
    return ru if lang(update) == "ru" else en


def persona_label(update, persona_id):
    emoji, ru, en, _ = PERSONAS[persona_id]
    return f"{emoji} {ui(update, ru, en)}"


def user_lock(context, user_id):
    return context.bot_data["locks"][user_id]


class LiveReply:
    """A reply that animates while the model thinks and then streams the answer in place."""

    def __init__(self, target, language="ru", action=ChatAction.TYPING):
        self.target, self.language, self.action = target, language, action
        self.messages, self.shown = [], []
        self.text = ""
        self.started = time.monotonic()
        self.last_edit = 0.0
        self.lock = asyncio.Lock()
        self.tasks = []

    def frame(self, tick):
        phrases = THINKING[self.language]
        phrase = phrases[min(len(phrases) - 1, tick // 3)]
        elapsed = int(time.monotonic() - self.started)
        unit = "с" if self.language == "ru" else "s"
        return f"{SPINNER[tick % len(SPINNER)]} {phrase}{'.' * (tick % 3 + 1)}  {elapsed} {unit}"

    async def start(self):
        first = self.frame(0)
        self.messages.append(await reply(self.target, first))
        self.shown.append(first)
        self.tasks = [asyncio.create_task(self._animate()), asyncio.create_task(self._typing())]

    async def _animate(self):
        tick = 0
        while True:
            await asyncio.sleep(ANIMATION_INTERVAL)
            tick += 1
            async with self.lock:
                if self.text.strip():
                    return
                await self._edit(0, self.frame(tick))

    async def _typing(self):
        while True:
            try:
                await self.target.get_bot().send_chat_action(chat_id=self.target.chat_id, action=self.action)
            except TelegramError:
                pass
            await asyncio.sleep(TYPING_INTERVAL)

    async def _edit(self, index, text, reply_markup=None, retries=0):
        if self.shown[index] == text and reply_markup is None:
            return
        for attempt in range(retries + 1):
            try:
                await self.messages[index].edit_text(text, parse_mode=None, reply_markup=reply_markup)
                self.shown[index] = text
                return
            except RetryAfter as exc:
                if attempt == retries:
                    return
                await asyncio.sleep(seconds(exc.retry_after))
            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    raise
                self.shown[index] = text
                return

    async def _render(self, final, reply_markup=None):
        async with self.lock:
            self.last_edit = time.monotonic()
            parts = list(chunks(self.text.strip()))
            if not parts:
                return
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                shown = part if final or not last else part + CURSOR
                markup = reply_markup if final and last else None
                if index < len(self.messages):
                    await self._edit(index, shown, markup, retries=2 if final else 0)
                else:
                    self.messages.append(await reply(self.target, shown, markup))
                    self.shown.append(shown)

    async def update(self, text):
        self.text = text
        if self.text.strip() and time.monotonic() - self.last_edit >= STREAM_EDIT_INTERVAL:
            await self._render(final=False)

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def finish(self, text, reply_markup=None):
        await self.stop()
        self.text = text
        await self._render(final=True, reply_markup=reply_markup)
        return self.messages[-1]

    async def abort(self, note):
        await self.stop()
        try:
            if self.text.strip():
                self.text = self.text.rstrip() + "\n\n" + note
                await self._render(final=True)
            elif self.messages:
                await self.messages[0].delete()
        except TelegramError:
            LOG.warning("Could not clean up an interrupted reply")


async def generate(context, live, **request):
    """Stream the model output into `live`; fall back to a single request if streaming is refused."""
    client = context.bot_data["client"]
    if context.bot_data.get("stream", True):
        try:
            text = ""
            async with client.responses.stream(**request) as stream:
                async for event in stream:
                    if event.type == "response.output_text.delta":
                        text += event.delta
                        await live.update(text)
                final = await stream.get_final_response()
            return (final.output_text or text), final.status == "incomplete"
        except BadRequestError as exc:
            # Some models require a verified OpenAI organization for streaming.
            if live.text or "stream" not in str(exc).lower():
                raise
            context.bot_data["stream"] = False
            LOG.warning("Streaming is not available for this model; using regular responses")
    response = await client.responses.create(**request)
    return response.output_text, response.status == "incomplete"


def answer_keyboard(update, context):
    row = [InlineKeyboardButton(ui(update, "🔄 Другой ответ", "🔄 Regenerate"), callback_data="regen")]
    if context.bot_data.get("tts_model"):
        row.append(InlineKeyboardButton(ui(update, "🔊 Озвучить", "🔊 Read aloud"), callback_data="tts"))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton(ui(update, "🧹 Новый диалог", "🧹 New chat"), callback_data="reset")]])


def persona_keyboard(update, current):
    buttons = [
        InlineKeyboardButton(("✅ " if pid == current else "") + persona_label(update, pid), callback_data=f"mode:{pid}")
        for pid in PERSONAS
    ]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


async def authorized(update, context):
    if not update.effective_user or not update.effective_message:
        return False
    allowed = context.bot_data["allowed"]
    if allowed and update.effective_user.id not in allowed:
        await send(update.effective_message, ui(update, "Доступ к Secret AI ограничен.", "Access to Secret AI is restricted."))
        return False
    # Avoid exposing personal history to group chats.
    if update.effective_chat.type != "private":
        await send(update.effective_message, ui(update, "Напишите Secret AI в личные сообщения.", "Please message Secret AI privately."))
        return False
    return True


HELP_RU = (
    "Я Secret AI 🕶 Отправьте текст, голосовое, аудио, кружок или фото (можно файлом) с вопросом.\n\n"
    "Пока я думаю, вы увидите анимацию, а ответ будет появляться прямо по ходу печати.\n"
    "Под ответом есть кнопки: 🔄 другой ответ, 🔊 озвучить, 🧹 новый диалог.\n\n"
    "/mode — режим: ассистент, программист, переводчик, креатив\n"
    "/reset — очистить память\n"
    "/export — скачать историю диалога\n"
    "/myid — ваш Telegram ID\n"
    "/help — эта справка\n\n"
    "Я помню последние 30 сообщений нашего диалога."
)
HELP_EN = (
    "I'm Secret AI 🕶 Send text, a voice message, audio, a video note or a photo (also as a file) with a question.\n\n"
    "While I'm thinking you'll see an animation, and the answer appears as it is being written.\n"
    "Buttons under each answer: 🔄 regenerate, 🔊 read aloud, 🧹 new chat.\n\n"
    "/mode — mode: assistant, coder, translator, creative\n"
    "/reset — clear memory\n"
    "/export — download the conversation\n"
    "/myid — your Telegram ID\n"
    "/help — this help\n\n"
    "I remember our last 30 messages."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        await send(update.effective_message, ui(update, "Привет! ", "Hi! ") + ui(update, HELP_RU, HELP_EN))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        await send(update.effective_message, ui(update, HELP_RU, HELP_EN))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        async with user_lock(context, update.effective_user.id):
            context.bot_data["memory"].reset(update.effective_user.id)
        await send(update.effective_message, ui(update, "🧹 Память очищена. Начнём новый диалог!", "🧹 Memory cleared. Let's start a new conversation!"))


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        await send(update.effective_message, f"Telegram ID: {update.effective_user.id}")


async def mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        current = context.bot_data["memory"].persona(update.effective_user.id)
        await send(update.effective_message,
                   ui(update, f"Текущий режим: {persona_label(update, current)}\nВыберите новый:",
                      f"Current mode: {persona_label(update, current)}\nChoose a new one:"),
                   persona_keyboard(update, current))


async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update, context):
        return
    history = context.bot_data["memory"].history(update.effective_user.id)
    if not history:
        await send(update.effective_message, ui(update, "История пока пуста.", "The history is empty."))
        return
    you = ui(update, "Вы", "You")
    text = "\n\n".join(f"{you if item['role'] == 'user' else 'Secret AI'}:\n{item['content']}" for item in history)
    await update.effective_message.reply_document(
        document=io.BytesIO(text.encode("utf-8")), filename="secret-ai-history.txt",
        caption=ui(update, f"📄 История: {len(history)} сообщений", f"📄 History: {len(history)} messages"))


async def media_text(media, client):
    duration = seconds(getattr(media, "duration", 0) or 0)
    if (media.file_size or 0) > 19_000_000 or duration > 600:
        raise ValueError("voice_limit")
    with tempfile.TemporaryDirectory(prefix="secret-ai-") as folder:
        source, target = Path(folder) / "input", Path(folder) / "voice.wav"
        remote = await media.get_file()
        await remote.download_to_drive(source)
        process = await asyncio.create_subprocess_exec(
            imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-y", "-v", "error",
            "-i", str(source), "-vn", "-t", "601", "-ac", "1", "-ar", "16000", str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), timeout=60)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode or not target.exists():
            raise ValueError("voice_format")
        if target.stat().st_size > 20_000_000:
            raise ValueError("voice_limit")
        with target.open("rb") as audio:
            result = await client.audio.transcriptions.create(model="whisper-1", file=audio)
        return result.text.strip()


async def read_question(update, context):
    """Return (question, model content, text to remember) or None if nothing to answer."""
    message = update.effective_message
    client = context.bot_data["client"]
    question = message.text or message.caption or ui(update, "Что изображено на фото?", "What is in this photo?")
    media = message.voice or message.audio or message.video_note
    if media:
        status = await reply(message, ui(update, "🎧 Слушаю и расшифровываю…", "🎧 Listening and transcribing…"))
        try:
            transcript = await media_text(media, client)
        except BaseException:
            await status.delete()
            raise
        if not transcript:
            await status.edit_text(ui(update, "Не удалось распознать речь. Запишите ещё раз.", "No speech recognized. Please try again."))
            return None
        await status.edit_text(ui(update, "🎙 Расшифровка:\n", "🎙 Transcription:\n") + transcript)
        question = transcript if not message.caption else f"{message.caption}\n\n{transcript}"
    image = message.photo[-1] if message.photo else message.document
    if image:
        if (image.file_size or 0) > 19_000_000:
            raise ValueError("photo_limit")
        mime = getattr(image, "mime_type", None) or "image/jpeg"
        remote = await image.get_file()
        raw = await remote.download_as_bytearray()
        encoded = base64.b64encode(raw).decode("ascii")
        content = [{"type": "input_text", "text": question}, {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"}]
        return question, content, PHOTO_MARKER + question
    return question, question, question


async def respond(update, context, target, content, saved_question):
    user_id = update.effective_user.id
    memory = context.bot_data["memory"]
    persona = PERSONAS[memory.persona(user_id)]
    instructions = SYSTEM + (" " + persona[3] if persona[3] else "") + " Telegram language: " + (update.effective_user.language_code or "ru")
    live = LiveReply(target, lang(update))
    await live.start()
    try:
        answer, incomplete = await generate(
            context, live, model=context.bot_data["model"], instructions=instructions,
            input=memory.history(user_id) + [{"role": "user", "content": content}],
            store=False, max_output_tokens=6000)
    except BaseException:
        await live.abort(ui(update, "⚠️ Ответ прерван.", "⚠️ The answer was interrupted."))
        raise
    answer = answer.strip()
    if not answer:
        await live.finish(ui(update, "Модель не вернула текст. Попробуйте переформулировать вопрос.", "The model returned no text. Please rephrase your question."))
        return
    if incomplete:
        answer += ui(update, "\n\nОтвет достиг лимита. Напишите «продолжи».", "\n\nOutput limit reached. Ask me to continue.")
    memory.append_turn(user_id, saved_question, answer)
    last = await live.finish(answer, answer_keyboard(update, context))
    context.bot_data["last_answer"][user_id] = last.message_id


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update, context):
        return
    message = update.effective_message
    lock = user_lock(context, update.effective_user.id)
    if lock.locked():
        await send(message, ui(update, "⏳ Ещё отвечаю на предыдущее сообщение — это обработаю следом.",
                               "⏳ Still answering your previous message — this one is next."))
    async with lock:
        parsed = await read_question(update, context)
        if parsed:
            _, content, saved_question = parsed
            await respond(update, context, message, content, saved_question)


async def speak(update, context, source):
    text = (source.text or "").replace(CURSOR, "").strip()[:4000]
    if not text:
        return
    await source.get_bot().send_chat_action(chat_id=source.chat_id, action=ChatAction.RECORD_VOICE)
    audio = await context.bot_data["client"].audio.speech.create(
        model=context.bot_data["tts_model"], voice=context.bot_data["tts_voice"], input=text, response_format="opus")
    await source.reply_voice(voice=audio.content, filename="secret-ai.ogg")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await authorized(update, context):
        await query.answer()
        return
    user_id, data, memory = update.effective_user.id, query.data or "", context.bot_data["memory"]

    if data.startswith("mode:") and data[5:] in PERSONAS:
        memory.set_persona(user_id, data[5:])
        label = persona_label(update, data[5:])
        await query.answer(label)
        await query.edit_message_text(ui(update, f"Режим включён: {label}", f"Mode enabled: {label}"),
                                      reply_markup=persona_keyboard(update, data[5:]))
        return

    if data == "reset":
        async with user_lock(context, user_id):
            memory.reset(user_id)
        await query.answer(ui(update, "Память очищена", "Memory cleared"))
        await send(query.message, ui(update, "🧹 Память очищена. Начнём новый диалог!", "🧹 Memory cleared. Let's start a new conversation!"))
        return

    if data == "tts":
        await query.answer(ui(update, "Записываю голосовое…", "Recording a voice message…"))
        await speak(update, context, query.message)
        return

    if data == "regen":
        lock = user_lock(context, user_id)
        if lock.locked() or context.bot_data["last_answer"].get(user_id) != query.message.message_id:
            await query.answer(ui(update, "Можно пересоздать только последний ответ", "Only the latest answer can be regenerated"), show_alert=True)
            return
        async with lock:
            question = memory.last_question(user_id)
            if question is None:
                await query.answer(ui(update, "Отправьте вопрос ещё раз", "Please send the question again"), show_alert=True)
                return
            if question.startswith(PHOTO_MARKER):
                await query.answer(ui(update, "Фото не сохраняется — отправьте его ещё раз", "Photos aren't kept — please send it again"), show_alert=True)
                return
            memory.pop_last_turn(user_id)
            await query.answer(ui(update, "Генерирую другой ответ…", "Generating another answer…"))
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
            await respond(update, context, query.message, question, question)
        return

    await query.answer()


async def unsupported(update, context):
    if await authorized(update, context):
        await send(update.effective_message, ui(update, "Отправьте текст, голосовое, аудио или фото. Команды: /help", "Send text, voice, audio or a photo. Commands: /help"))


async def on_error(update, context):
    error = context.error
    # Never log exception bodies: they can contain tokens, URLs or user content.
    LOG.error("Handler error: %s", type(error).__name__)
    if not isinstance(update, Update) or not update.effective_message or not update.effective_user:
        return
    ru, en = "Не удалось обработать сообщение. Попробуйте ещё раз чуть позже.", "Couldn't process your message. Please try again later."
    if isinstance(error, RateLimitError):
        ru, en = "Достигнут лимит OpenAI или закончился баланс API. Попробуйте позже или сообщите владельцу бота.", "OpenAI rate or billing limit reached. Try later or contact the bot owner."
    elif isinstance(error, (AuthenticationError, NotFoundError, PermissionDeniedError)):
        ru, en = "Проверьте с владельцем бота ключ OpenAI и доступ к выбранной модели.", "Ask the owner to check the OpenAI key and model access."
    elif isinstance(error, BadRequestError):
        ru, en = "OpenAI отклонил запрос. Попробуйте более короткий текст или другое фото/голосовое.", "OpenAI rejected the request. Try shorter text or different media."
    elif isinstance(error, APIConnectionError):
        ru, en = "OpenAI не ответил вовремя или недоступна сеть. Попробуйте ещё раз через минуту.", "OpenAI timed out or the network is unavailable. Please try again in a minute."
    elif isinstance(error, TimeoutError):
        ru, en = "Файл обрабатывался слишком долго. Попробуйте запись покороче.", "Processing the file took too long. Try a shorter recording."
    elif isinstance(error, ValueError):
        ru, en = "Не удалось обработать файл. Голосовое — до 10 минут и 19 МБ, фото — до 19 МБ.", "Couldn't process this file. Voice limit: 10 minutes and 19 MB; photo limit: 19 MB."
    try:
        await send(update.effective_message, ui(update, ru, en))
    except TelegramError:
        LOG.warning("Error notification could not be delivered")


class HealthHandler(BaseHTTPRequestHandler):
    body = b"Secret AI is running\n"

    def _headers(self):
        self.send_response(200 if self.path in ("/", "/health") else 404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()

    def do_GET(self):
        self._headers()
        self.wfile.write(self.body)

    # Uptime monitors often probe with HEAD; without this they get 501 and report the bot as down.
    def do_HEAD(self):
        self._headers()

    def log_message(self, *args):
        pass


COMMANDS = {
    "ru": [("help", "Что я умею"), ("mode", "Сменить режим"), ("reset", "Очистить память"),
           ("export", "Скачать историю"), ("myid", "Мой Telegram ID")],
    "en": [("help", "What I can do"), ("mode", "Change mode"), ("reset", "Clear memory"),
           ("export", "Download history"), ("myid", "My Telegram ID")],
}


async def post_init(app):
    try:
        await app.bot.set_my_commands([BotCommand(*c) for c in COMMANDS["en"]])
        await app.bot.set_my_commands([BotCommand(*c) for c in COMMANDS["ru"]], language_code="ru")
    except TelegramError:
        LOG.warning("Could not register the command menu")


async def shutdown(app):
    await app.bot_data["client"].close()
    app.bot_data["memory"].close()


def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token, key = os.environ.get("TELEGRAM_TOKEN", "").strip(), os.environ.get("OPENAI_API_KEY", "").strip()
    if not token or not key:
        raise SystemExit("Set TELEGRAM_TOKEN and OPENAI_API_KEY environment variables.")
    try:
        allowed = parse_allowed(os.environ.get("ALLOWED_USERS", ""))
        port = int(os.environ.get("PORT", "10000"))
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise SystemExit("Invalid ALLOWED_USERS or PORT configuration.") from None
    # Updates run concurrently across users; a per-user lock keeps each conversation and /reset in order.
    app = Application.builder().token(token).concurrent_updates(True).post_init(post_init).post_shutdown(shutdown).build()
    app.bot_data.update(
        client=AsyncOpenAI(api_key=key, timeout=120, max_retries=2),
        memory=Memory(os.environ.get("DATABASE_PATH", "memory.db")), allowed=allowed,
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna",
        stream=os.environ.get("STREAM_REPLIES", "1").strip() != "0",
        tts_model=os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts").strip(),
        tts_voice=os.environ.get("OPENAI_TTS_VOICE", "alloy").strip() or "alloy",
        locks=defaultdict(asyncio.Lock), last_answer={})
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mode", mode))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.PHOTO | filters.Document.IMAGE,
        chat))
    app.add_handler(MessageHandler(filters.ALL, unsupported))
    app.add_error_handler(on_error)
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False, bootstrap_retries=3)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
