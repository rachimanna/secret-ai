"""Secret AI: Telegram polling bot with a Render HTTP health endpoint."""
import asyncio
import base64
import logging
import os
import sqlite3
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import imageio_ffmpeg
from openai import AsyncOpenAI, AuthenticationError, BadRequestError, NotFoundError, RateLimitError
from telegram import Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

LOG = logging.getLogger("secret_ai")
SYSTEM = (
    "You are Secret AI, a helpful, accurate and friendly assistant. "
    "Introduce yourself as Secret AI when asked. Reply in the user's language. "
    "Use the language of their latest message, caption or voice transcription; "
    "if none is available, use their Telegram language. Admit uncertainty. "
    "Reply in plain text, without Markdown formatting. "
    "Previous photo markers are not images; do not invent unseen visual details."
)


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
        self.db.commit()

    def history(self, user_id):
        rows = self.db.execute("SELECT role, content FROM (SELECT id, role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 30) ORDER BY id", (user_id,)).fetchall()
        return [{"role": role, "content": content} for role, content in rows]

    def append_turn(self, user_id, question, answer):
        with self.db:
            self.db.executemany("INSERT INTO messages(user_id, role, content) VALUES (?, ?, ?)", [(user_id, "user", question), (user_id, "assistant", answer)])
            self.db.execute("DELETE FROM messages WHERE user_id=? AND id NOT IN (SELECT id FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 30)", (user_id, user_id))

    def reset(self, user_id):
        with self.db:
            self.db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))

    def close(self):
        self.db.close()


def chunks(text, limit=4000):
    # Count UTF-16 units conservatively, including astral emoji.
    start = units = 0
    for index, char in enumerate(text):
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            yield text[start:index]
            start, units = index, 0
        units += size
    if start < len(text):
        yield text[start:]


async def send(message, text):
    for part in chunks(text):
        for attempt in range(3):
            try:
                await message.reply_text(part, parse_mode=None)
                break
            except RetryAfter as exc:
                if attempt == 2:
                    raise
                delay = exc.retry_after
                await asyncio.sleep(delay.total_seconds() if hasattr(delay, "total_seconds") else delay)


def ui(update, ru, en):
    return ru if (update.effective_user.language_code or "ru").startswith("ru") else en


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        await send(update.effective_message, ui(update,
            "Привет! Я Secret AI. Отправьте текст, голосовое сообщение или фото с вопросом. Я помню последние 30 сообщений нашего диалога.\n/reset — очистить память\n/myid — ваш Telegram ID",
            "Hi! I'm Secret AI. Send text, a voice message, or a photo with a question. I remember our last 30 messages.\n/reset — clear memory\n/myid — your Telegram ID"))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        context.bot_data["memory"].reset(update.effective_user.id)
        await send(update.effective_message, ui(update, "Память очищена. Начнём новый диалог!", "Memory cleared. Let's start a new conversation!"))


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        await send(update.effective_message, f"Telegram ID: {update.effective_user.id}")


async def voice_text(message, client):
    if (message.voice.file_size or 0) > 19_000_000 or message.voice.duration > 600:
        raise ValueError("voice_limit")
    with tempfile.TemporaryDirectory(prefix="secret-ai-") as folder:
        source, target = Path(folder) / "voice.ogg", Path(folder) / "voice.wav"
        remote = await message.voice.get_file()
        await remote.download_to_drive(source)
        process = await asyncio.create_subprocess_exec(
            imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-y", "-v", "error",
            "-i", str(source), "-t", "601", "-ac", "1", "-ar", "16000", str(target),
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


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update, context):
        return
    message = update.effective_message
    client, memory = context.bot_data["client"], context.bot_data["memory"]
    question = message.text or message.caption or ui(update, "Что изображено на фото?", "What is in this photo?")
    if message.voice:
        question = await voice_text(message, client)
        if not question:
            await send(message, ui(update, "Не удалось распознать речь. Запишите ещё раз.", "No speech recognized. Please try again."))
            return
        await send(message, ui(update, "🎙 Расшифровка:\n", "🎙 Transcription:\n") + question)
    content = question
    saved_question = question
    if message.photo:
        photo = message.photo[-1]
        if (photo.file_size or 0) > 19_000_000:
            raise ValueError("photo_limit")
        remote = await photo.get_file()
        raw = await remote.download_as_bytearray()
        encoded = base64.b64encode(raw).decode("ascii")
        content = [{"type": "input_text", "text": question}, {"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}"}]
        saved_question = "[Photo attached; image is not retained] " + question
    history = memory.history(update.effective_user.id)
    response = await client.responses.create(
        model=context.bot_data["model"],
        instructions=SYSTEM + " Telegram language: " + (update.effective_user.language_code or "ru"),
        input=history + [{"role": "user", "content": content}],
        store=False, max_output_tokens=6000)
    answer = response.output_text.strip()
    if not answer:
        await send(message, ui(update, "Модель не вернула текст. Попробуйте переформулировать вопрос.", "The model returned no text. Please rephrase your question."))
        return
    if response.status == "incomplete":
        answer += ui(update, "\n\nОтвет достиг лимита. Напишите «продолжи».", "\n\nOutput limit reached. Ask me to continue.")
    memory.append_turn(update.effective_user.id, saved_question, answer)
    await send(message, answer)


async def unsupported(update, context):
    if await authorized(update, context):
        await send(update.effective_message, ui(update, "Отправьте текст, голосовое или фото. Команды: /start /reset /myid", "Send text, voice or a photo. Commands: /start /reset /myid"))


async def on_error(update, context):
    error = context.error
    # Never log exception bodies: they can contain tokens, URLs or user content.
    LOG.error("Handler error: %s", type(error).__name__)
    if not isinstance(update, Update) or not update.effective_message or not update.effective_user:
        return
    ru, en = "Не удалось обработать сообщение. Попробуйте ещё раз чуть позже.", "Couldn't process your message. Please try again later."
    if isinstance(error, RateLimitError):
        ru, en = "Достигнут лимит OpenAI или закончился баланс API. Попробуйте позже или сообщите владельцу бота.", "OpenAI rate or billing limit reached. Try later or contact the bot owner."
    elif isinstance(error, (AuthenticationError, NotFoundError)):
        ru, en = "Проверьте с владельцем бота ключ OpenAI и доступ к выбранной модели.", "Ask the owner to check the OpenAI key and model access."
    elif isinstance(error, BadRequestError):
        ru, en = "OpenAI отклонил запрос. Попробуйте более короткий текст или другое фото/голосовое.", "OpenAI rejected the request. Try shorter text or different media."
    elif isinstance(error, ValueError):
        ru, en = "Не удалось обработать файл. Голосовое — до 10 минут и 19 МБ, фото — до 19 МБ.", "Couldn't process this file. Voice limit: 10 minutes and 19 MB; photo limit: 19 MB."
    try:
        await send(update.effective_message, ui(update, ru, en))
    except TelegramError:
        LOG.warning("Error notification could not be delivered")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Secret AI is running\n"
        self.send_response(200 if self.path in ("/", "/health") else 404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


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
    app = Application.builder().token(token).concurrent_updates(False).post_shutdown(shutdown).build()
    app.bot_data.update(client=AsyncOpenAI(api_key=key, timeout=120, max_retries=2),
                        memory=Memory(os.environ.get("DATABASE_PATH", "memory.db")), allowed=allowed,
                        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna")
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND | filters.VOICE | filters.PHOTO, chat))
    app.add_handler(MessageHandler(filters.ALL, unsupported))
    app.add_error_handler(on_error)
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Sequential processing preserves conversation order and /reset semantics.
        app.run_polling(allowed_updates=["message"], drop_pending_updates=False, bootstrap_retries=3)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
