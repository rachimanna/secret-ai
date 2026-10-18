"""Music lookup and full audio delivery; no chat model or music-generation API needed.

yt-dlp runs in a disposable subprocess so timeouts stop network work as well as the UI.
Only server-side search results can be downloaded, never a URL supplied in a callback.
"""
import asyncio
import json
import logging
import math
import os
import re
import secrets
import signal
import sys
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

LOG = logging.getLogger("secret_ai.music")
MAX_DURATION = 20 * 60
MAX_BYTES = 45 * 1024 * 1024
RESULT_TTL = 15 * 60
MAX_SEARCHES = 256
PREFIX = "music:"
HELP = 'Напиши «найди песню название и исполнитель». Покажу варианты — выбери нужный кнопкой.'
ERRORS = {
    "timeout": "Поиск или загрузка заняли слишком много времени. Попробуй ещё раз чуть позже.",
    "too_long": "Этот трек длиннее 20 минут. Выбери другой вариант.",
    "too_large": "Этот файл слишком большой для отправки. Выбери другой вариант.",
    "unavailable": "Не удалось загрузить этот трек: источник недоступен или ограничил скачивание. Попробуй другой вариант.",
}
REQUEST = re.compile(
    r"^\s*(?:(?:пожалуйста|можешь|можете)\s*[,،]?\s*)?"
    r"(?:найди(?:те)?|найти|поищи(?:те)?|отыщи|скинь|пришли|отправь|скачай|включи)\s+"
    r"(?:мне\s+)?(?:песню|песенку|трек|музыку|композицию)\b"
    r"(?:\s+пожалуйста\b)?\s*[:—–-]?\s*(.*)$", re.I | re.S)
EN_REQUEST = re.compile(r"^\s*(?:please\s+)?(?:find|search|send|download|play)\s+(?:me\s+)?(?:a\s+)?(?:song|track|music)\b\s*[:—–-]?\s*(.*)$", re.I | re.S)


def request(text, username=""):
    """None means ordinary chat; an empty string means a search missing its query."""
    text = text or ""
    if username:
        text = re.sub(r"(?<!\w)@" + re.escape(username.lstrip("@")) + r"\b[,:]?", "", text, flags=re.I)
    match = REQUEST.match(text) or EN_REQUEST.match(text)
    return match.group(1).strip().strip('«»"')[:250] if match else None


class MusicError(Exception):
    def __init__(self, code="unavailable"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    performer: str
    duration: int

    @property
    def url(self):
        return "https://www.youtube.com/watch?v=" + self.id

    @property
    def label(self):
        return f"{self.title[:65]} · {self.duration // 60}:{self.duration % 60:02d}"


def tracks(entries):
    found, seen = [], set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        ident, seconds = str(entry.get("id") or ""), entry.get("duration")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", ident) or ident in seen:
            continue
        if entry.get("is_live") or entry.get("live_status") in ("is_live", "is_upcoming"):
            continue
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= MAX_DURATION:
            continue
        title = " ".join(str(entry.get("title") or "").split())
        if not title or entry.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
            continue
        seen.add(ident)
        found.append(Track(ident, title[:200], str(entry.get("artist") or entry.get("uploader") or entry.get("channel") or "")[:100], int(seconds)))
        if len(found) == 5:
            break
    return found


async def run_process(*args, timeout):
    """Kill the subprocess group on timeout/cancellation, including yt-dlp's JS runtime."""
    process = await asyncio.create_subprocess_exec(
        *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix")
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException as exc:
        if process.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        if isinstance(exc, asyncio.TimeoutError):
            raise MusicError("timeout") from None
        raise
    if process.returncode:
        LOG.warning("Music worker failed (exit %s): %s", process.returncode, err[-700:].decode(errors="replace"))
        raise MusicError()
    return out


async def worker(action, *args, timeout):
    raw = await run_process(sys.executable, Path(__file__).resolve(), action, *args, timeout=timeout)
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise MusicError() from None
    if result.get("error"):
        raise MusicError(result["error"])
    return result


async def search(query):
    result = await worker("search", query, timeout=45)
    return tracks(result.get("entries"))


async def download(track, folder):
    import imageio_ffmpeg
    result = await worker("download", track.id, folder, timeout=150)
    source = Path(result["path"]).resolve()
    root = Path(folder).resolve()
    if source.parent != root or not source.is_file():
        raise MusicError()
    destination = root / "track.mp3"
    # No -t, audio slicing or preview API: convert the complete downloaded audio.
    await run_process(imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
                      "-y", "-i", source, "-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "192k",
                      destination, timeout=75)
    size = destination.stat().st_size
    if not size:
        raise MusicError()
    if size > MAX_BYTES:
        raise MusicError("too_large")
    return destination


@dataclass
class Selection:
    user_id: int
    chat_id: int
    message_id: int
    created: float
    tracks: list


class Music:
    def __init__(self):
        self.selections = OrderedDict()
        self.files = OrderedDict()
        self.busy = set()
        self.searching = set()

    def prune(self):
        now = time.monotonic()
        for token, selection in list(self.selections.items()):
            if now - selection.created > RESULT_TTL:
                del self.selections[token]
        while len(self.selections) > MAX_SEARCHES:
            self.selections.popitem(last=False)

    async def find(self, message, user_id, query):
        if not query:
            await message.reply_text(HELP)
            return
        if user_id in self.searching:
            await message.reply_text("Предыдущий поиск ещё идёт. Подожди немного.")
            return
        if len(self.searching) >= 4:
            await message.reply_text("Сейчас много запросов на музыку. Попробуй через несколько секунд.")
            return
        self.searching.add(user_id)
        try:
            status = await message.reply_text("Ищу песню…")
            try:
                found = await search(query)
            except MusicError as exc:
                await status.edit_text(ERRORS.get(exc.code, ERRORS["unavailable"]))
                return
            if not found:
                await status.edit_text("Ничего не нашёл. Уточни название песни или исполнителя.")
                return
            self.prune()
            token = secrets.token_urlsafe(8)
            self.selections[token] = Selection(user_id, message.chat_id, status.message_id, time.monotonic(), found)
            self.prune()
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{i + 1}. {track.label}", callback_data=f"{PREFIX}{token}:{i}")]
                for i, track in enumerate(found)])
            text = "Выбери песню — пришлю полный аудиофайл:\n\n" + "\n".join(
                f"{i + 1}. {track.title} · {track.duration // 60}:{track.duration % 60:02d}" for i, track in enumerate(found))
            await status.edit_text(text, reply_markup=keyboard)
        finally:
            self.searching.discard(user_id)

    async def choose(self, query, user_id):
        self.prune()
        match = re.fullmatch(r"music:([\w-]{11}):([0-4])", query.data or "")
        selection = self.selections.get(match[1]) if match else None
        message = query.message
        if not selection or not message or not hasattr(message, "reply_audio") or (
                selection.chat_id != message.chat_id or selection.message_id != message.message_id):
            await query.answer("Этот список устарел. Напиши «найди песню …» ещё раз.", show_alert=True)
            return
        if user_id != selection.user_id:
            await query.answer("Это поиск другого участника. Напиши свой запрос на песню.", show_alert=True)
            return
        index = int(match[2])
        if index >= len(selection.tracks):
            await query.answer("Этот вариант недоступен.", show_alert=True)
            return
        if user_id in self.busy or len(self.busy) >= 2:
            await query.answer("Уже загружаю музыку. Подожди немного.", show_alert=True)
            return
        self.busy.add(user_id)
        try:
            await query.answer("Готовлю аудиофайл…")
            track = selection.tracks[index]
            status = await message.reply_text(f"Загружаю «{track.title}»…")
            try:
                cached = self.files.get(track.id)
                if cached:
                    try:
                        await message.reply_audio(cached, title=track.title, performer=track.performer or None)
                        await status.edit_text("Песня отправлена.")
                        return
                    except BadRequest:
                        self.files.pop(track.id, None)
                with tempfile.TemporaryDirectory(prefix="secret-music-") as folder:
                    audio = await download(track, folder)
                    filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", track.title)[:100] + ".mp3"
                    with audio.open("rb") as stream:
                        sent = await message.reply_audio(stream, filename=filename, title=track.title,
                                                         performer=track.performer or None, duration=track.duration,
                                                         read_timeout=120, write_timeout=120)
                if sent.audio:
                    self.files[track.id] = sent.audio.file_id
                    self.files.move_to_end(track.id)
                    while len(self.files) > 256:
                        self.files.popitem(last=False)
                await status.edit_text("Песня отправлена.")
            except MusicError as exc:
                await status.edit_text(ERRORS.get(exc.code, ERRORS["unavailable"]))
            except TelegramError:
                LOG.warning("Telegram music upload failed", exc_info=True)
                await status.edit_text("Не удалось отправить аудио в Telegram. Попробуй нажать кнопку ещё раз.")
        finally:
            self.busy.discard(user_id)


def service(data):
    if "music" not in data:
        data["music"] = Music()
    return data["music"]


async def handle_text(update, context):
    message, user, data = update.effective_message, update.effective_user, context.bot_data
    if not message or not user or user.is_bot or not message.text:
        return False
    query = request(message.text, data.get("bot_name") or context.bot.username or "")
    if query is None:
        return False
    if not data.get("allowed") or user.id in data["allowed"]:
        await service(data).find(message, user.id, query)
    return True


async def handle_callback(update, context):
    query, user, data = update.callback_query, update.effective_user, context.bot_data
    if not user or (data.get("allowed") and user.id not in data["allowed"]):
        await query.answer("Доступ к Secret AI ограничен.", show_alert=True)
        return
    await service(data).choose(query, user.id)


def worker_main(action, value, folder=None):
    """Isolated yt-dlp entry point. Logs stay on stderr, one small JSON object on stdout."""
    from yt_dlp import YoutubeDL
    import deno

    class Logger:
        def debug(self, text):
            pass

        def warning(self, text):
            pass

        def error(self, text):
            print(str(text)[:700], file=sys.stderr)

    def limits(info, *, incomplete=False):
        duration = info.get("duration")
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise MusicError("too_long")
        if not incomplete and (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0):
            raise MusicError()
        if duration and duration > MAX_DURATION:
            raise MusicError("too_long")
        if (info.get("filesize") or info.get("filesize_approx") or 0) > MAX_BYTES:
            raise MusicError("too_large")

    def progress(event):
        if event.get("downloaded_bytes", 0) > MAX_BYTES:
            raise MusicError("too_large")

    options = dict(quiet=True, no_warnings=True, logger=Logger(), cachedir=False,
                   socket_timeout=12, retries=1, fragment_retries=1, noplaylist=True,
                   js_runtimes={"deno": {"path": str(deno.find_deno_bin())}})
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        # Respect an explicitly configured system trust store; TLS verification stays on.
        options["compat_opts"] = {"no-certifi"}
    if action == "search":
        options.update(extract_flat="in_playlist", skip_download=True, playlistend=12)
        with YoutubeDL(options) as ydl:
            result = ydl.extract_info("ytsearch12:" + value, download=False) or {}
        return {"entries": [dict(id=t.id, title=t.title, uploader=t.performer, duration=t.duration)
                            for t in tracks(result.get("entries"))]}
    if action != "download" or not re.fullmatch(r"[A-Za-z0-9_-]{11}", value) or not folder:
        raise MusicError()
    options.update(format="bestaudio/best", outtmpl=str(Path(folder) / "source.%(ext)s"),
                   match_filter=limits, max_filesize=MAX_BYTES, progress_hooks=[progress],
                   fixup="never", hls_prefer_native=True)
    with YoutubeDL(options) as ydl:
        result = ydl.extract_info("https://www.youtube.com/watch?v=" + value, download=True)
        if not result:
            raise MusicError()
        limits(result)
        path = ydl.prepare_filename(result)
    if not Path(path).is_file():
        raise MusicError("too_large")
    if Path(path).stat().st_size > MAX_BYTES:
        raise MusicError("too_large")
    return {"path": path}


if __name__ == "__main__":
    try:
        print(json.dumps(worker_main(*sys.argv[1:]), ensure_ascii=False))
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc)[:700], file=sys.stderr)
        print(json.dumps({"error": exc.code if isinstance(exc, MusicError) else "unavailable"}))
