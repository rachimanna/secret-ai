"""Secret AI: Telegram polling bot with a Render HTTP health endpoint."""
import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
import time
from collections import defaultdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
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
from telegram import (BotCommand, BotCommandScopeAllGroupChats, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputSticker, LabeledPrice, MenuButtonWebApp,
                      MessageEntity, ReplyParameters, Update, User, WebAppInfo)
from codefiles import extract_files, site_file
from fun import LEVELS, GroupStore, MemeError, demotivator, quote_card, tiktok_meme
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import (Application, BusinessConnectionHandler, CallbackContext, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          PreCheckoutQueryHandler, filters)

import game
import miniapp
import persist
import secretary as sec
import welcome
from apps import Pipedream, PipedreamError
from github_app import GitHubApps
from mcp_oauth import DirectApps, MCPAuthError
from media import (COST, DEFAULT_MODELS, FAL_SCENE_SECONDS, FORMATS, KLING, KLING_API, KLING_DIRECT, KLING_SECONDS, kling_generate, ANYMODEL_API, ANYMODEL_PREFIX, ANYMODEL_SECONDS, ANYMODEL_VIDEO, anymodel_generate, anymodel_speech, anymodel_voice_id, anymodel_voices, ANYMODEL_VOICE, ANYMODEL_FISH, fish_speech, fish_voice_id, HF_SPACES, space_video, fit_clip, FREE_PHOTOS_PER_DAY, MAX_PROMPT, OLD_PHOTO_DEFAULTS, OLD_VIDEO_DEFAULTS, PACKS, SCENE_SECONDS,
                   VIDEO_PRICES, MediaError, OPENAI_IMAGE, abacus_image, openai_image, free_image, join_with_voice, living_picture, fal_generate, fal_sound, hf_generate, montage, price, veo_generate, video_provider)
from integrations import (Connections, GitHubError, Toolbox, apply_change, delete_repos, describe, describe_deletion,
                          prune_pending)

LOG = logging.getLogger("secret_ai")
SYSTEM = (
    "You are Secret AI, a brilliant, knowledgeable and friendly assistant. "
    "Your name here is Secret AI (say so when asked your name). Reply in the user's language. "
    "Use the language of their latest message, caption or voice transcription; "
    "if none is available, use their Telegram language. "
    "Get facts, numbers and code right. Give complete, practical answers: concrete steps, examples and working "
    "solutions instead of generic advice; for simple questions and small talk be brief and quick. "
    "Search the web only when the answer depends on recent or changing facts (news, prices, versions, events). "
    "Admit uncertainty instead of guessing. "
    "Reply in plain text, without Markdown formatting (no **bold**, no # headings). "
    "Previous photo markers are not images; do not invent unseen visual details."
)
# Translation and writing in languages of the Caucasus and Russia's republics, where models often slip into Russian
# spelling or another language's script.
TRANSLATION_RULES = (
    " Translation: translate meaning, not word by word; keep tone, slang, names and formatting; give just the translation "
    "unless asked for explanations. For rare languages and dialects add a short note on words you are unsure of instead "
    "of inventing them, and offer a transliteration or a literal gloss when it helps. Use each language's own standard "
    "orthography: Chechen (нохчийн мотт) and Ingush in Cyrillic with the palochka Ӏ (never the digit 1, the Latin I or "
    "the Cyrillic І): хьо, кхин, Ӏуьйре, дика ду, баркалла, нохчийн; write the Chechen digraphs (кх, къ, кӀ, хь, хӀ, "
    "аь, оь, уь, яь, юь) correctly. Ossetian (ирон æвзаг): Iron by default (Digor only if asked), with the letter æ "
    "(not а or ae): æмæ, хорз, мæ, дæ, фæндараст. Armenian (հայերեն): Armenian script, Eastern Armenian by default "
    "(Western if the user writes Western or asks), reformed spelling, Armenian punctuation (։ for a full stop, ՞ for a "
    "question, ՜ for emphasis, ՝ for a pause). Georgian in Mkhedruli; Avar, Dargin, Lezgian, Lak, Kabardian, Adyghe, "
    "Karachay-Balkar, Kumyk in their Cyrillic alphabets with Ӏ where it is used; Azerbaijani, Uzbek, Turkmen in Latin "
    "unless asked for Cyrillic; Tatar, Bashkir, Kazakh, Kyrgyz, Tajik, Yakut in Cyrillic with their own letters "
    "(ә, ө, ү, ң, һ, җ, ғ, қ, ұ, ӣ, ӯ). If a request could mean two variants (Eastern/Western Armenian, Iron/Digor), "
    "pick the common one and say which, briefly. Never pass off Russian or another language as the requested one."
)
# Code goes out as files (codefiles.extract_files), websites are also published with a link.
CODE_RULES = (
    " Exception: code. When you write code, a script, a config or a website, put each file in its own fenced block "
    "whose first line is ``` plus the language and a file name, for example ```python main.py — complete, runnable "
    "files, never fragments with '...'. The bot sends these blocks as files, so outside them only say briefly what the "
    "files are and how to run them; don't repeat the code. For a website write one self-contained index.html with inline "
    "CSS and JavaScript: modern, beautiful, responsive (mobile first), with real content, smooth animations and no "
    "external dependencies except fonts or CDN libraries; the bot publishes it and gives the user a link. "
    "A one- or two-line command may stay in the text. Never use code blocks for ordinary text."
)
GROUP_STYLE = (
    "You are Secret AI, but in this Telegram group you are just one of the guys: a regular member of the chat, not an "
    "assistant. Messages come as 'Name: text'. Write exactly like a real person types in a friends' chat: short (usually "
    "one line, sometimes just a few words), lowercase is fine, street slang, no polite assistant phrases, no explaining, "
    "no moralizing, no 'просто', no long dashes (—), no quotes around jokes, no cheesy metaphors (кастрюля, режим, "
    "фестиваль, цирк), at most one emoji and often none. Use the chat's own language: its names, nicknames, inside jokes, "
    "catchphrases and what people actually said above; refer to specific messages and people instead of generic remarks. "
    "When asked something (\"почему вы ссоритесь?\"), answer the actual question from the chat history, with a jab "
    "(who started it, what was said). Don't play peacemaker: take a side for fun, tease, roast back in their tone; if they "
    "swear, you may swear too. Sometimes a quick rhyme or a pun, never forced. With girls be cheeky and charming, never "
    "sexual or creepy. If someone asks a real question (facts, help, code), answer it properly but still like a friend. "
    "Hard limits: no insults about ethnicity, nationality, religion, disability, orientation, weight or looks; no threats; "
    "nothing about minors (if a child is in a photo, be sweet); if someone is genuinely hurt, ease off. "
    "Examples of the tone: «рахим опять начал, а виноват как всегда кто-то другой», «вы ругаетесь как будто за "
    "последний чебурек», «маш, не позорься, ты вчера то же самое писала», «ну всё, к обиделся, несите плед»."
)
INTERJECT = (
    "Nobody asked you, but you jump into the conversation like a friend who couldn't resist: one short line about the "
    "LAST few messages, naming the person. A jab, a joke, a callback to something said earlier, or a quick rhyme. No "
    "generic commentary about 'the chat'. If nothing sharp comes to mind, answer exactly SKIP."
)
GROUP_TYPES = ("group", "supergroup")
# Uninvited posts, like Sglypa: after every N members' messages, N drawn from the group's level.
GROUP_EVERY = {"rare": (30, 60), "normal": (12, 25), "often": (5, 12)}
GROUP_RANDOM_PAUSE = 40  # at least this many seconds between two uninvited posts
USERNAME_REFRESH = 300  # the bot's @username is re-read so a renamed bot still reacts to mentions
PHOTO_MARKER = "[Photo attached; image is not retained] "
HISTORY_LIMIT = 1000
# What the model sees of the history: older long answers are shortened and the whole history fits a budget,
# so 300 messages stay affordable and inside the model's context.
HISTORY_CHARS = 180_000
MAX_FACTS = 80  # lasting facts kept per person
HISTORY_ITEM_CHARS = 2_500

# id: (emoji, Russian name, English name, extra instructions)
PERSONAS = {
    "assistant": ("✨", "Ассистент", "Assistant", ""),
    "coder": ("💻", "Программист", "Coder",
              "Act as a senior software engineer: give complete working code, explain decisions briefly and point out pitfalls."),
    "translator": ("🌍", "Переводчик", "Translator",
                   "Act as a translator: translate Russian text into English and any other language into Russian. "
                   "Preserve tone and line breaks. Output only the translation."),
    "creative": ("🎨", "Креатив", "Creative",
                 "Be bold and original: suggest unexpected ideas and write vividly."),
}
DEFAULT_PERSONA = "assistant"

# The thinking bubble starts with the logo: an animated custom emoji made from webapp/logo-emoji.webm
# (the bot creates the emoji pack itself) or the one from BRAND_EMOJI_ID.
LOGO_EMOJI_FILE = Path(__file__).with_name("webapp") / "logo-emoji.webm"
LOGO_EMOJI_VERSION = "v2"
CURSOR = " ▌"
# Telegram allows roughly one edit per second per chat; faster edits get RetryAfter.
ANIMATION_INTERVAL = 1.5
STREAM_EDIT_INTERVAL = 1.2
TYPING_INTERVAL = 4.5
# Native drafts (sendMessageDraft) animate smoothly and are not limited like edits.
DRAFT_INTERVAL = 0.3
REVEAL_CATCH_UP = 3.0  # at most this long typing out an answer that arrived all at once
# A draft disappears after 30 seconds without updates.
DRAFT_KEEPALIVE = 10
REVEAL_MIN_STEP = 24
MAX_TOOL_ROUNDS = 12
DIFF_INLINE_LIMIT = 2500
VOICE_TIMEOUT = 60
VOICE_MAX_CHARS = 1500


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
        if "model" not in [row[1] for row in self.db.execute("PRAGMA table_info(settings)")]:
            self.db.execute("ALTER TABLE settings ADD COLUMN model TEXT")
        # Generation: free photos per day, bought credits, and paid Stars charges (each counted once).
        # Lasting facts about a person (name, city, likes, plans…), shared by the private chat and every group.
        self.db.execute("CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, fact TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS facts_by_user ON facts(user_id, id)")
        self.db.execute("CREATE TABLE IF NOT EXISTS gen_daily (user_id INTEGER NOT NULL, day TEXT NOT NULL, photos INTEGER NOT NULL, "
                        "PRIMARY KEY (user_id, day))")
        self.db.execute("CREATE TABLE IF NOT EXISTS gen_credits (user_id INTEGER PRIMARY KEY, credits INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS gen_payments (charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
                        "credits INTEGER NOT NULL, stars INTEGER NOT NULL)")
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

    def facts(self, user_id):
        return [row[0] for row in self.db.execute("SELECT fact FROM facts WHERE user_id=? ORDER BY id", (user_id,))]

    def remember(self, user_id, add=(), forget=()):
        """Add facts (no duplicates) and drop the ones containing any of the forget phrases; keeps the newest MAX_FACTS."""
        known = {f.casefold() for f in self.facts(user_id)}
        with self.db:
            for phrase in forget:
                if phrase.strip():
                    self.db.execute("DELETE FROM facts WHERE user_id=? AND instr(lower(fact), lower(?)) > 0",
                                    (user_id, phrase.strip()))
            for fact in add:
                fact = " ".join(fact.split())[:300]
                if fact and fact.casefold() not in known:
                    self.db.execute("INSERT INTO facts(user_id, fact) VALUES (?, ?)", (user_id, fact))
                    known.add(fact.casefold())
            self.db.execute("DELETE FROM facts WHERE user_id=? AND id NOT IN (SELECT id FROM facts WHERE user_id=? "
                            "ORDER BY id DESC LIMIT ?)", (user_id, user_id, MAX_FACTS))

    def reset(self, user_id):
        with self.db:
            self.db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))

    def persona(self, user_id):
        row = self.db.execute("SELECT persona FROM settings WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row and row[0] in PERSONAS else DEFAULT_PERSONA

    def set_persona(self, user_id, persona):
        with self.db:
            self.db.execute("INSERT INTO settings(user_id, persona) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET persona=excluded.persona", (user_id, persona))

    def model(self, user_id):
        row = self.db.execute("SELECT model FROM settings WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None

    def set_model(self, user_id, model):
        with self.db:
            self.db.execute("INSERT INTO settings(user_id, persona, model) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET model=excluded.model",
                            (user_id, DEFAULT_PERSONA, model))

    def count(self, user_id):
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,)).fetchone()[0]

    # ----- generation quota -----

    @staticmethod
    def _today():
        return time.strftime("%Y-%m-%d", time.gmtime())

    def gen_info(self, user_id):
        """(free photos left today, credits)."""
        used = self.db.execute("SELECT photos FROM gen_daily WHERE user_id=? AND day=?", (user_id, self._today())).fetchone()
        credits = self.db.execute("SELECT credits FROM gen_credits WHERE user_id=?", (user_id,)).fetchone()
        return max(0, FREE_PHOTOS_PER_DAY - (used[0] if used else 0)), credits[0] if credits else 0

    def gen_charge(self, user_id, kind, cost):
        """Take one generation: a free photo first, then credits. Returns 'free', 'credits' or None (not enough)."""
        free, credits = self.gen_info(user_id)
        with self.db:
            if kind == "photo" and free > 0:
                self.db.execute("INSERT INTO gen_daily(user_id, day, photos) VALUES (?, ?, 1) "
                                "ON CONFLICT(user_id, day) DO UPDATE SET photos=photos+1", (user_id, self._today()))
                return "free"
            if credits >= cost:
                self.db.execute("UPDATE gen_credits SET credits=credits-? WHERE user_id=?", (cost, user_id))
                return "credits"
        return None

    def gen_refund(self, user_id, how, cost):
        with self.db:
            if how == "free":
                self.db.execute("UPDATE gen_daily SET photos=MAX(0, photos-1) WHERE user_id=? AND day=?", (user_id, self._today()))
            elif how == "credits":
                self._add_credits(user_id, cost)

    def _add_credits(self, user_id, credits):
        self.db.execute("INSERT INTO gen_credits(user_id, credits) VALUES (?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET credits=credits+excluded.credits", (user_id, credits))

    def gen_paid(self, charge_id, user_id, credits, stars):
        """Credit a Stars payment once; returns False if this charge was already counted."""
        with self.db:
            if self.db.execute("SELECT 1 FROM gen_payments WHERE charge_id=?", (charge_id,)).fetchone():
                return False
            self.db.execute("INSERT INTO gen_payments(charge_id, user_id, credits, stars) VALUES (?, ?, ?, ?)",
                            (charge_id, user_id, credits, stars))
            self._add_credits(user_id, credits)
        return True

    def close(self):
        self.db.close()


def utf16_len(text):
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def split_point(text, limit=4000):
    """Index where the first Telegram-sized part of `text` ends, preferring paragraph, line and word boundaries.

    Sizes are counted in UTF-16 units, conservatively including astral emoji.
    """
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
                return found + len(separator)
    return cut


def chunks(text, limit=4000):
    """Split text into Telegram-sized parts."""
    while text:
        cut = split_point(text, limit)
        part, text = text[:cut], text[cut:]
        if part.strip():
            yield part


def seconds(delay):
    return delay.total_seconds() if hasattr(delay, "total_seconds") else delay


async def reply(message, text, reply_markup=None, entities=None):
    for attempt in range(3):
        try:
            return await message.reply_text(text, parse_mode=None, reply_markup=reply_markup, entities=entities)
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


def logo_entities(config, kind):
    if not config.get("logo_id") or kind in config.setdefault("logo_off", set()):
        return None
    return [MessageEntity(MessageEntity.CUSTOM_EMOJI, 0, utf16_len(config["logo"]), custom_emoji_id=config["logo_id"])]


async def with_logo(config, send, kind="message"):
    """Call send(entities) with the custom emoji logo, falling back to the plain emoji if Telegram refuses it.

    kind is "message" or "draft": Telegram may accept the custom emoji in regular messages but not in drafts,
    so a refusal only turns the logo off for that kind.
    """
    entities = logo_entities(config, kind)
    try:
        return await send(entities)
    except BadRequest as exc:
        if not entities or not any(word in str(exc).lower() for word in ("emoji", "entit", "document")):
            raise
        # Bots may use custom emoji only when the owner has Telegram Premium or a Fragment username.
        config.setdefault("logo_off", set()).add(kind)
        # Telegram's error text holds no user data; it tells the owner why the logo is not shown.
        LOG.warning("Custom emoji logo refused by Telegram in a %s (%s); using the plain emoji there", kind, exc.message)
        return await send(None)


SEARCH_NOTE = "Ищу в интернете"
STEP_NAMES = {SEARCH_NOTE: ("поиск в интернете", "web search")}


def thinking_frame(language, logo, started, dots=None, note=None, done=()):
    """While the model thinks, like big AI bots: what it is doing now, then a timer and the finished steps.

    🌟 Думаю                 🌟 Ищу в интернете
    12с · поиск в интернете ✓   8с · поиск в интернете…
    """
    ru = language == "ru"
    action = note or ("Думаю" if ru else "Thinking")
    icon = logo  # always first: the animated custom-emoji logo sits at offset 0
    if note == SEARCH_NOTE and not ru:
        action = "Searching the web"
    seconds = max(0, int(time.monotonic() - started)) if started else 0
    steps = [STEP_NAMES.get(step, (step.lower(), step.lower()))[0 if ru else 1] + " ✓" for step in done]
    if note:
        steps.append(STEP_NAMES.get(note, (note.lower(), note.lower()))[0 if ru else 1] + "…")
    return f"{icon} {action}\n" + " · ".join([f"{seconds}{'с' if ru else 's'}"] + steps)


def track_step(reply, note):
    """Remember a finished step (shown with ✓) when the bot moves on from it."""
    if reply.note and reply.note != note and reply.note not in reply.done:
        reply.done.append(reply.note)
    reply.note = note


async def typing_loop(target, action):
    while True:
        try:
            await target.get_bot().send_chat_action(chat_id=target.chat_id, action=action)
        except TelegramError:
            pass
        await asyncio.sleep(TYPING_INTERVAL)


def code_progress(text, language="ru"):
    """While code is being written: the prose before it plus "writing code… N lines" instead of the raw code."""
    head = text[:text.find("```")].rstrip()
    lines = sum(part.count("\n") for part in text.split("```")[1::2])
    note = f"✍️ Пишу код… {lines} строк" if language == "ru" else f"✍️ Writing code… {lines} lines"
    return f"{head}\n\n{note}".strip()


class LiveReply:
    """Fallback reply: animates by editing a message while the model thinks, then streams the answer in place."""

    def __init__(self, target, language, config, action=ChatAction.TYPING):
        self.target, self.language, self.config, self.action = target, language, config, action
        self.messages, self.shown = [], []
        self.text = ""
        self.started = time.monotonic()
        self.last_edit = 0.0
        self.lock = asyncio.Lock()
        self.tasks = []
        self.note, self.done = None, []

    def frame(self, tick):
        return thinking_frame(self.language, self.config["logo"], self.started, tick % 3 + 1, self.note, self.done)

    def status(self, note):
        track_step(self, note)

    async def start(self):
        first = self.frame(0)
        self.messages.append(await with_logo(self.config, lambda entities: reply(self.target, first, entities=entities)))
        self.shown.append(first)
        self.tasks = [asyncio.create_task(self._animate()), asyncio.create_task(typing_loop(self.target, self.action))]

    async def _animate(self):
        tick = 0
        while True:
            await asyncio.sleep(ANIMATION_INTERVAL)
            tick += 1
            async with self.lock:
                if self.text.strip():
                    return
                frame = self.frame(tick)
                await with_logo(self.config, lambda entities: self._edit(0, frame, entities=entities))

    async def _edit(self, index, text, reply_markup=None, retries=0, entities=None):
        if self.shown[index] == text and reply_markup is None:
            return
        for attempt in range(retries + 1):
            try:
                await self.messages[index].edit_text(text, parse_mode=None, reply_markup=reply_markup, entities=entities)
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
            shown = self.text if final or "```" not in self.text else code_progress(self.text, self.language)
            parts = list(chunks(shown.strip()))
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
            if final and len(self.messages) > len(parts):
                # The final text can be shorter than the streamed one (code went to files): remove the leftovers.
                for extra in self.messages[len(parts):]:
                    try:
                        await extra.delete()
                    except TelegramError:
                        pass
                del self.messages[len(parts):], self.shown[len(parts):]

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


class DraftReply:
    """Native Telegram streaming (sendMessageDraft), like large AI bots.

    The draft shows the animated logo with a timer while the model thinks, then the answer types
    itself out. Finished parts are sent as normal messages because a draft is only a preview.
    """

    def __init__(self, target, language, config, action=ChatAction.TYPING):
        self.target, self.language, self.config, self.action = target, language, config, action
        self.bot, self.chat_id = target.get_bot(), target.chat_id
        self.draft_id = random.randint(1, 2**31 - 1)
        self.text = ""
        self.offset = 0  # characters of self.text already sent as messages
        self.revealed = 0  # characters after offset already shown in the draft
        self.shown, self.pushed = None, 0.0
        self.sent = []
        self.started = time.monotonic()
        self.closed, self.wake = False, asyncio.Event()
        self.tasks = []
        self.note, self.done = None, []

    def status(self, note):
        """Show what the bot is doing (e.g. searching the web) with the thinking timer or under the text."""
        track_step(self, note)

    async def _push(self, text, entities=None):
        # An empty draft is Telegram's own see-through "Thinking… 5s" placeholder (Bot API 10).
        await self.bot.send_message_draft(self.chat_id, self.draft_id, text or None, entities=entities or None)
        self.shown, self.pushed = text, time.monotonic()

    def frame(self):
        """"✨ Думаю · 5с" with the steps done; a plain emoji: custom emoji can make Telegram refuse the draft."""
        return thinking_frame(self.language, self.config["logo"], self.started, note=self.note, done=self.done)

    async def _show_frame(self):
        frame = self.frame()
        if self.config.get("rich_drafts", True):
            # Telegram's own see-through "thinking" block (Bot API 10.2 rich drafts), without our emoji.
            status = frame.split(" ", 1)[1] if frame.startswith(self.config["logo"] + " ") else frame
            logo_id = self.config.get("logo_id") if "rich" not in self.config.get("logo_off", set()) else None
            # Our animated logo in front of the status, as a custom emoji inside the thinking block.
            text = ([{"type": "custom_emoji", "custom_emoji_id": logo_id, "alternative_text": self.config["logo"]}, " " + status]
                    if logo_id else f"{self.config['logo']} {status}")
            try:
                await self._push_thinking(text)
                self.shown, self.pushed = frame, time.monotonic()
                return
            except BadRequest as exc:
                if logo_id and any(w in str(exc).lower() for w in ("emoji", "entit", "document", "premium")):
                    self.config.setdefault("logo_off", set()).add("rich")  # the logo isn't allowed there: plain emoji
                    LOG.warning("Custom emoji logo refused in the thinking block (%s); using the plain emoji", str(exc)[:120])
                    return await self._show_frame()
                self.config["rich_drafts"] = False  # this Telegram has no rich drafts: the plain text frame from now on
                LOG.warning("Rich thinking drafts are unavailable (%s); using plain drafts", str(exc)[:120])
        await self._push(frame)

    async def _push_thinking(self, text):
        await self.bot.do_api_request("sendRichMessageDraft", api_kwargs={
            "chat_id": self.chat_id, "draft_id": self.draft_id,
            "rich_message": {"blocks": [{"type": "thinking", "text": text}], "skip_entity_detection": True}})

    async def start(self):
        await self._show_frame()
        self.tasks = [asyncio.create_task(self._loop()), asyncio.create_task(typing_loop(self.target, self.action))]

    async def _pause(self, delay):
        try:
            await asyncio.wait_for(self.wake.wait(), delay)
        except asyncio.TimeoutError:
            pass

    async def _tick(self):
        if not self.text.strip():
            # Re-send before Telegram drops a draft that saw no updates for 30 s.
            if self.frame() != self.shown or time.monotonic() - self.pushed > DRAFT_KEEPALIVE:
                await self._show_frame()
            return
        pending = self.text[self.offset:]
        if "```" in pending:
            # Code goes out as files at the end: show progress in the draft instead of the raw code, commit nothing.
            view = code_progress(pending, self.language)
            if view.strip() and (view != self.shown or time.monotonic() - self.pushed > DRAFT_KEEPALIVE):
                await self._push(view)
            return
        # Commit full Telegram-sized parts as real messages and keep drafting the rest.
        while (cut := split_point(pending)) < len(pending):
            if pending[:cut].strip():
                self.sent.append(await reply(self.target, pending[:cut]))
            self.offset += cut
            self.revealed = 0
            pending = self.text[self.offset:]
        # Typewriter: reveal the backlog in steps so the text flows instead of jumping.
        backlog = len(pending) - self.revealed
        if backlog > 0:
            self.revealed = min(len(pending), self.revealed + max(REVEAL_MIN_STEP, backlog // 3))
            space = pending.find(" ", self.revealed)
            if 0 <= space - self.revealed <= 8:
                self.revealed = space
        shown = pending[:self.revealed]
        if self.note and self.revealed == len(pending):
            shown = shown.rstrip() + f"\n\n{self.config['logo']} {self.note}…"
        if shown.strip() and (shown != self.shown or time.monotonic() - self.pushed > DRAFT_KEEPALIVE):
            await self._push(shown)

    async def _loop(self):
        while not self.closed:
            try:
                await self._tick()
            except RetryAfter as exc:
                await self._pause(seconds(exc.retry_after))
            except TelegramError as exc:
                LOG.warning("Draft update failed: %s", type(exc).__name__)
            await self._pause(DRAFT_INTERVAL)

    async def update(self, text):
        self.text = text

    async def stop(self):
        self.closed = True
        self.wake.set()
        # Let the draft loop finish its current step so a part is never sent twice.
        for task in self.tasks[1:]:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def _catch_up(self, text):
        """The answer may arrive in one piece (some gateways don't stream): type it out in the draft first."""
        if "```" in text or self.closed:
            return
        self.text = text
        deadline = time.monotonic() + REVEAL_CATCH_UP
        while time.monotonic() < deadline and self.revealed < len(self.text[self.offset:]) - REVEAL_MIN_STEP:
            await asyncio.sleep(DRAFT_INTERVAL)

    async def finish(self, text, reply_markup=None):
        await self._catch_up(text)
        await self.stop()
        # Parts already sent stay; the final text may differ after them (code moved to files).
        rest = text[self.offset:] if text[:self.offset] == self.text[:self.offset] else text
        self.text = text
        parts = list(chunks(rest))
        if not parts:
            await self.sent[-1].edit_reply_markup(reply_markup=reply_markup)
            return self.sent[-1]
        await self._clear()
        for index, part in enumerate(parts):
            self.sent.append(await reply(self.target, part, reply_markup if index == len(parts) - 1 else None))
        return self.sent[-1]

    async def _clear(self):
        """Empty the draft just before the real message goes out (the message then dismisses it): Telegram keeps a
        draft ~30 s and replayed its typing when the chat was reopened. Cleared after the message, an empty bubble
        stayed below it instead."""
        if not self.config.get("clear_drafts", True):
            return
        try:
            await self.bot.send_message_draft(self.chat_id, self.draft_id, "")
        except TelegramError as exc:
            LOG.warning("Could not clear the draft: %s", type(exc).__name__)

    async def abort(self, note):
        await self.stop()
        try:
            rest = self.text[self.offset:].rstrip()
            await self._clear()
            if rest:
                await send(self.target, rest + "\n\n" + note)
        except TelegramError:
            LOG.warning("Could not clean up an interrupted reply")


class SilentReply:
    """Voice in, voice out: no text while thinking, just "recording a voice message…"."""

    def __init__(self, target):
        self.target, self.text, self.task = target, "", None

    def status(self, note):
        pass

    async def start(self):
        self.task = asyncio.create_task(typing_loop(self.target, ChatAction.RECORD_VOICE))

    async def update(self, text):
        self.text = text

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def finish(self, text, reply_markup=None):
        await self.stop()
        parts = list(chunks(text)) or ["…"]
        last = None
        for index, part in enumerate(parts):
            last = await reply(self.target, part, reply_markup if index == len(parts) - 1 else None)
        return last

    async def abort(self, note):
        await self.stop()


async def open_reply(target, language, config):
    """Start a live reply: native drafts when Telegram supports them, message edits otherwise."""
    # Drafts (the see-through message that types itself) come first; if Telegram refuses the custom emoji logo
    # there, the draft shows the plain emoji instead (with_logo handles that).
    if config.get("drafts"):
        draft = DraftReply(target, language, config)
        try:
            await draft.start()
            if not config.get("draft_logged"):
                config["draft_logged"] = True
                LOG.info("Replies stream as see-through Telegram drafts")
            return draft
        except (RetryAfter, NetworkError):
            pass
        except TelegramError as exc:
            # Only a Telegram that doesn't know drafts turns them off for good; any other hiccup costs one reply.
            if "not found" in str(exc).lower() or "unknown method" in str(exc).lower():
                config["drafts"] = False
            LOG.warning("Native draft failed (%s: %s); using message edits for this reply", type(exc).__name__, str(exc)[:120])
    live = LiveReply(target, language, config)
    await live.start()
    return live


async def model_round(context, live, prefix, request):
    """One model call streamed into `live` after `prefix`; falls back to a single request if streaming is refused."""
    client = context.bot_data["client"]
    if context.bot_data.get("stream", True):
        try:
            text = ""
            async with client.responses.stream(**request) as stream:
                async for event in stream:
                    if event.type == "response.output_text.delta":
                        text += event.delta
                        live.status(None)
                        await live.update(prefix + text)
                    elif event.type == "response.web_search_call.in_progress":
                        live.status(SEARCH_NOTE)
                    elif event.type == "response.web_search_call.completed":
                        live.status(None)
                return await stream.get_final_response()
        except BadRequestError as exc:
            # Some models require a verified OpenAI organization for streaming.
            if live.text or "stream" not in str(exc).lower():
                raise
            context.bot_data["stream"] = False
            LOG.warning("Streaming is not available for this model; using regular responses")
    return await client.responses.create(**request)


def echo_items(output):
    """Model output items to send back with tool results (store=False keeps no server-side state)."""
    items = []
    for item in output:
        if item.type == "reasoning":
            reasoning = {"type": "reasoning", "id": item.id,
                         "summary": [{"type": "summary_text", "text": part.text} for part in item.summary or []]}
            if item.encrypted_content:
                reasoning["encrypted_content"] = item.encrypted_content
            items.append(reasoning)
        elif item.type == "function_call":
            items.append({"type": "function_call", "call_id": item.call_id, "name": item.name, "arguments": item.arguments})
        elif item.type == "web_search_call":
            items.append(item.model_dump(exclude_none=True))
        elif item.type == "message":
            text = "".join(getattr(part, "text", "") or "" for part in item.content)
            if text:
                items.append({"role": "assistant", "content": text})
    return items


def drop_unsupported(context, request, error):
    """Leave out an option the model refused (reasoning effort, web search) from now on; False if none matched."""
    if "reasoning" in request and "reasoning" in error:
        context.bot_data["reasoning"] = ""
        request.pop("reasoning")
        LOG.warning("The model does not accept a reasoning effort; using its default")
        return True
    if context.bot_data.get("web_search") and "web_search" in error:
        context.bot_data["web_search"] = False
        LOG.warning("Web search is not available for this model; answering without it")
        request["tools"] = [tool for tool in request["tools"] if tool.get("type") != "web_search"]
        if not request["tools"]:
            request.pop("tools")
            request.pop("include", None)
        return True
    return False


# ----- models through Abacus.AI RouteLLM (OpenAI-compatible chat completions), e.g. Claude Sonnet -----
ABACUS_URL = "https://routellm.abacus.ai/v1"
ABACUS_PREFIX = "abacus:"


def is_abacus(model):
    return model.startswith(ABACUS_PREFIX)


# Chat-completions gateways: model prefix -> the bot_data key of its client. AnyModel models also get function tools.
CHAT_PROVIDERS = {ABACUS_PREFIX: "abacus", ANYMODEL_PREFIX: "anymodel_chat"}
OPUS = ANYMODEL_PREFIX + "cc/claude-opus-4-6"  # the default model when ANYMODEL_API_KEY is set (CHAT_MODEL env overrides)
GPT_PICK = "gpt-6.1-sol"  # the second model in the picker


GEMINI = re.compile(r"gemini[-_ ]?3[-_.]8", re.I)


def pick_gemini(ids):
    """Gemini 3.8 from the AnyModel catalog: Pro before Flash, never Lite, an id with its provider prefix first."""
    found = [i for i in ids if GEMINI.search(i) and "lite" not in i.lower() and "image" not in i.lower()
             and "tts" not in i.lower()]
    found.sort(key=lambda i: ("pro" not in i.lower(), "flash" in i.lower(), "/" not in i, "preview" in i.lower(), i))
    return found[0] if found else None


SONNET_55 = re.compile(r"sonnet[-._ ]?5[-._ ]?5", re.I)


def pick_deepseek(ids):
    """The newest DeepSeek chat model in the catalog (a reasoner only if nothing else), provider-prefixed ids first."""
    found = [i for i in ids if "deepseek" in i.lower() and not any(w in i.lower() for w in ("coder", "distill", "ocr", "vl"))]
    def version(i):
        return [int(n) for n in re.findall(r"\d+", i.lower().split("deepseek", 1)[1])][:3]
    found.sort(key=lambda i: ("reason" in i.lower() or "r1" in i.lower(), not version(i), [-n for n in version(i)],
                              "/" not in i, i))
    return found[0] if found else None


def chat_provider(model):
    return next((prefix for prefix in CHAT_PROVIDERS if model.startswith(prefix)), None)


WEB_TOOL = {"type": "function", "function": {
    "name": "web_search",
    "description": ("Search the internet for fresh or exact facts: exchange rates (euro, dollar), prices, news, weather, "
                    "sports scores, schedules, anything that changes or that you are not sure about. Use it on your own "
                    "whenever the answer depends on current data; never say you can't browse."),
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "what to find, with the date or place if it matters"}},
        "required": ["query"]}}}


async def web_lookup(data, query):
    """Search the web through the OpenAI model's built-in search; returns a short factual digest with sources."""
    try:
        request = dict(model=data["model"], store=False, max_output_tokens=1500, tools=[{"type": "web_search"}], input=query)
        if data.get("reasoning"):
            request["reasoning"] = {"effort": "low"}  # a lookup, not a puzzle: answer fast
        response = await data["client"].responses.create(
            **request,
            instructions=("Search the web and report the facts that answer the query: exact numbers with units and dates, "
                          "and the source links. Today is " + time.strftime("%Y-%m-%d") + ". No small talk."))
        return response.output_text or "Nothing found."
    except Exception as exc:  # the model answers without fresh data rather than failing
        LOG.warning("Web search for a gateway model failed: %s", type(exc).__name__)
        return "Web search is unavailable right now; answer from what you know and say the data may be outdated."


def chat_tools(toolbox):
    """The toolbox's function tools in chat-completions form (built-in ones like web search stay with OpenAI)."""
    if not toolbox:
        return []
    return [{"type": "function", "function": {"name": d["name"], "description": d.get("description", ""),
                                              "parameters": d.get("parameters") or {"type": "object", "properties": {}}}}
            for d in toolbox.definitions() if d.get("type") == "function" and d.get("name")]


def model_identity(model):
    """Who the model is, so it answers "who are you / who made you" truthfully."""
    name = model.split(":", 1)[-1].split("/")[-1].lower()
    if "claude" in name:
        pretty = "Claude " + " ".join(p.capitalize() for p in re.sub(r"^claude-", "", name).split("-") if not p.isdigit())
        version = ".".join(re.findall(r"-(\d+)(?=-|$)", name)[:2])
        return (f" You run on {pretty} {version}".rstrip() + ", a model made by Anthropic. If someone asks which model you "
                "are or who created you, say plainly that you are " + f"{pretty} {version}".strip() + " by Anthropic, "
                "working inside the Secret AI Telegram bot.")
    if "deepseek" in name:
        pretty = "DeepSeek " + name.split("deepseek", 1)[1].strip("-").replace("-", " ").upper()
        return (f" You run on {pretty.strip()}, a model made by DeepSeek. If someone asks which model you are or who created "
                f"you, say plainly that you are {pretty.strip()} by DeepSeek, working inside the Secret AI Telegram bot.")
    if "gemini" in name:
        pretty = " ".join(p.capitalize() if not p[0].isdigit() else p for p in name.replace("gemini-", "gemini ").replace("-", " ").split())
        return (f" You run on {pretty}, a model made by Google. If someone asks which model you are or who created you, "
                f"say plainly that you are {pretty} by Google, working inside the Secret AI Telegram bot.")
    if name.startswith("gpt") or name.startswith("o"):
        pretty = name.upper().replace("GPT-", "GPT-").replace("-SOL", " Sol").replace("-LUNA", " Luna").replace("-TERRA", " Terra")
        return (f" You run on {pretty}, a model made by OpenAI. If someone asks which model you are or who created you, "
                f"say plainly that you are {pretty} by OpenAI, working inside the Secret AI Telegram bot.")
    return ""


def chat_messages(instructions, items):
    """Responses-style input (text and images) as chat-completions messages."""
    messages = [{"role": "system", "content": instructions}] if instructions else []
    for item in items:
        if "role" not in item:
            continue
        content = item["content"]
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get("type") == "input_text":
                    parts.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "input_image":
                    parts.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
            content = parts
        messages.append({"role": item["role"], "content": content})
    return messages


async def chat_answer(context, live, request, toolbox=None, prefix=""):
    """A streamed answer from a chat-completions gateway (RouteLLM, AnyModel), running function tools on AnyModel."""
    model = request["model"]
    provider = chat_provider(model)
    client = context.bot_data.get(CHAT_PROVIDERS[provider])
    if not client:
        env = "ABACUS_API_KEY" if provider == ABACUS_PREFIX else "ANYMODEL_API_KEY"
        raise AuthenticationError(f"{env} is not set", body=None,
                                  response=httpx.Response(401, request=httpx.Request("POST", ABACUS_URL)))
    messages = chat_messages(request.get("instructions"), request["input"])
    no_tools = context.bot_data.setdefault("no_tools", set())
    tools = chat_tools(toolbox) if provider not in no_tools else []
    if provider not in no_tools and context.bot_data.get("web_search") and context.bot_data.get("client"):
        tools.append(WEB_TOOL)
    text, incomplete = "", False
    for _ in range(MAX_TOOL_ROUNDS):
        options = {"model": model[len(provider):], "stream": True, "messages": messages,
                   "max_tokens": min(request.get("max_output_tokens", 8000), 32000)}
        if tools:
            options["tools"] = tools
        try:
            stream = await client.chat.completions.create(**options)
        except BadRequestError as exc:
            if not tools or "tool" not in str(exc).lower():
                raise
            no_tools.add(provider)  # this gateway refuses function tools: plain answers from now on
            LOG.warning("%s refuses tools; answering without them", provider)
            tools = []
            options.pop("tools")
            stream = await client.chat.completions.create(**options)
        said, calls, finish = "", {}, None
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.delta.content:
                said += choice.delta.content
                await live.update(prefix + text + said)
            for call in choice.delta.tool_calls or []:
                slot = calls.setdefault(call.index or 0, {"id": "", "name": "", "arguments": ""})
                slot["id"] = call.id or slot["id"]
                if call.function:
                    slot["name"] += call.function.name or ""
                    slot["arguments"] += call.function.arguments or ""
            finish = choice.finish_reason or finish
        text += said
        incomplete = finish == "length"
        if not calls:
            break
        ordered = [dict(calls[i], id=calls[i]["id"] or f"call_{i}") for i in sorted(calls)]
        messages.append({"role": "assistant", "content": said or None, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
            for c in ordered]})
        for call in ordered:
            try:
                args = json.loads(call["arguments"] or "{}")
            except ValueError:
                args = {}
            if call["name"] == "web_search":
                live.status(SEARCH_NOTE)
                output = await web_lookup(context.bot_data, str(args.get("query") or ""))
            elif toolbox:
                live.status(toolbox.status(call["name"], args))
                output = await toolbox.run(call["name"], args)
            else:
                output = "This tool is not available here."
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(output)})
        if said.strip():
            text += "\n\n"
    live.status(None)
    return (text if prefix else text.strip()), incomplete


async def discover_models(app):
    """Offer newer models only if this account really has them, so the picker never lists one that fails."""
    data = app.bot_data
    try:
        openai_ids = {m.id for m in (await data["client"].models.list()).data}
    except Exception as exc:
        LOG.warning("OpenAI model list unavailable: %s", type(exc).__name__)
        openai_ids = set()
    abacus_ids = set()
    if data.get("abacus"):
        try:
            abacus_ids = {m.id for m in (await data["abacus"].models.list()).data}
        except Exception as exc:
            LOG.warning("RouteLLM model list unavailable: %s", type(exc).__name__)
    images = sorted(i for i in openai_ids if i.startswith("gpt-image") and "mini" not in i)
    if not data.get("image_model"):
        data["image_model"] = images[-1] if images else "gpt-image-1"
    gpt = GPT_PICK if GPT_PICK in openai_ids else ABACUS_PREFIX + GPT_PICK if GPT_PICK in abacus_ids else None
    if not gpt:
        LOG.warning("%s is not on the OpenAI key or RouteLLM; offering %s instead", GPT_PICK, data["model"])
        gpt = data["model"]
    gemini, any_ids = None, []
    if data.get("anymodel_chat"):
        try:
            any_ids = [m.id for m in (await data["anymodel_chat"].models.list()).data]
        except Exception as exc:
            LOG.warning("AnyModel model list unavailable: %s", type(exc).__name__)
    claude = OPUS if data.get("anymodel_chat") else None
    if claude and any_ids and claude[len(ANYMODEL_PREFIX):] not in any_ids:
        # The catalog spells it differently, or doesn't have it: take the closest Claude Opus it has.
        wanted = claude.rsplit("opus-", 1)[1]
        found = [i for i in any_ids if "opus" in i.lower() and wanted.replace("-", "") in re.sub(r"\D", "", i)]
        others = sorted(i for i in any_ids if "claude-opus" in i.lower())
        pick = (sorted(found, key=lambda i: (not i.startswith("cc/"), i)) or others[-1:] or [None])[0]
        LOG.warning("%s is not in the AnyModel catalog; using %s", claude, pick or "it anyway")
        claude = ANYMODEL_PREFIX + pick if pick else claude
    if any_ids and os.environ.get("GEMINI_CHAT", "").strip() == "1":  # off unless asked for
        gemini = pick_gemini(any_ids)
    if gemini is not None or os.environ.get("GEMINI_CHAT", "").strip() == "1":
        if gemini:
            gemini = ANYMODEL_PREFIX + gemini
            LOG.info("Gemini on AnyModel: %s", gemini)
        else:
            LOG.warning("Gemini 3.8 is not in the AnyModel catalog")
    deepseek = pick_deepseek(any_ids) if os.environ.get("DEEPSEEK_CHAT", "").strip() == "1" else None
    if deepseek:
        deepseek = ANYMODEL_PREFIX + deepseek
        LOG.info("DeepSeek on AnyModel: %s", deepseek)
    sonnet = None
    if data.get("abacus"):
        found = sorted((i for i in abacus_ids if SONNET_55.search(i)), key=lambda i: ("thinking" in i.lower(), len(i)))
        sonnet = ABACUS_PREFIX + (found[0] if found else "claude-sonnet-5-5")
        LOG.info("Claude Sonnet 5.5 on Abacus: %s%s", sonnet, "" if found else " (not in the list, trying the usual id)")
    data["models"] = [m for m in (gemini, sonnet, claude, gpt, deepseek) if m]  # the first one is the default
    chosen = data.get("chat_override")
    data["chat_default"] = chosen if chosen in data["models"] else data["models"][0]


NANO = re.compile(r"nano[-_ ]?banana", re.I)


def pick_nano(ids):
    """The best Nano Banana the account has: a Pro one first, otherwise the newest."""
    nano = sorted(i for i in ids if NANO.search(i))
    pro = [i for i in nano if "pro" in i.lower()]
    return (pro or nano or ["nano_banana_pro"])[-1]


async def resolve_abacus_model(app):
    """Find the account's Sonnet 5.5 (model picker) and Nano Banana (pictures) in RouteLLM's model list."""
    data = app.bot_data
    if not data.get("abacus"):
        return
    try:
        ids = [m.id for m in (await data["abacus"].models.list()).data]
    except Exception as exc:  # the list is a convenience; the usual ids still work
        LOG.warning("RouteLLM model list unavailable: %s", type(exc).__name__)
        ids = []
    data["abacus_image"] = data.get("abacus_image") or pick_nano(ids)


async def generate(context, live, toolbox=None, **request):
    """Run the model, executing tool calls (GitHub) until it produces the final answer."""
    request["instructions"] = (request.get("instructions") or "") + model_identity(request["model"])
    if chat_provider(request["model"]):
        answer, incomplete = await chat_answer(context, live, request, toolbox)
        return await continue_answer(context, live, request, answer, incomplete)
    tools = list(toolbox.definitions()) if toolbox else []
    if context.bot_data.get("web_search"):
        tools.append({"type": "web_search"})
    if tools:
        # store=False: reasoning must travel with the tool calls we send back.
        request.update(tools=tools, include=["reasoning.encrypted_content"])
    if context.bot_data.get("reasoning"):
        request["reasoning"] = {"effort": context.bot_data["reasoning"]}
    answer, incomplete = "", False
    for _ in range(MAX_TOOL_ROUNDS):
        prefix = answer + "\n\n" if answer.strip() else ""
        try:
            response = await model_round(context, live, prefix, request)
        except BadRequestError as exc:
            if live.text or not drop_unsupported(context, request, str(exc).lower()):
                raise
            response = await model_round(context, live, prefix, request)
        text = (response.output_text or "").strip()
        if text:
            answer = prefix + text
            await live.update(answer)
        incomplete = response.status == "incomplete"
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            break
        request["input"] = list(request["input"]) + echo_items(response.output)
        for call in calls:
            try:
                args = json.loads(call.arguments or "{}")
            except ValueError:
                args = {}
            live.status(toolbox.status(call.name, args))
            output = await toolbox.run(call.name, args)
            request["input"].append({"type": "function_call_output", "call_id": call.call_id, "output": output})
    live.status(None)
    answer, incomplete = await continue_answer(context, live, request, answer, incomplete)
    return answer, incomplete


CONTINUE = ("Your previous message was cut off by the length limit. Continue exactly from where it stopped: no preface, no "
            "repetition; if you were inside a code block, go on with the code and close the block.")
CONTINUE_ROUNDS = 3


async def continue_answer(context, live, request, answer, incomplete):
    """A long answer (big code) hit the output limit: ask the model to go on and glue the parts together."""
    base = [item for item in request["input"] if "role" in item]
    for _ in range(CONTINUE_ROUNDS):
        if not incomplete or not answer.strip():
            break
        follow = {key: value for key, value in request.items() if key not in ("tools", "include", "input")}
        follow["input"] = base + [{"role": "assistant", "content": answer}, {"role": "user", "content": CONTINUE}]
        if chat_provider(follow["model"]):
            more, incomplete = await chat_answer(context, live, follow, prefix=answer)
        else:
            response = await model_round(context, live, answer, follow)
            more, incomplete = response.output_text or "", response.status == "incomplete"
        if not more.strip():
            break
        answer += more
        await live.update(answer)
    return answer, incomplete


async def show_proposals(update, context, toolbox, target):
    """Send each prepared repository change with its diff and Apply/Cancel buttons."""
    for pid in toolbox.proposals:
        change = context.bot_data["pending"].get(pid)
        if not change:
            continue
        if change.get("kind") == "delete_repos":
            # Irreversible: the first button only asks again, the second one deletes.
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(ui(update, f"🗑 Удалить ({len(change['repos'])})", f"🗑 Delete ({len(change['repos'])})"),
                                     callback_data=f"del1:{pid}"),
                InlineKeyboardButton(ui(update, "❌ Отмена", "❌ Cancel"), callback_data=f"cancel:{pid}")]])
            await send(target, describe_deletion(change, lang(update)), keyboard)
            continue
        summary, diff = describe(change, lang(update))
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(ui(update, "✅ Применить", "✅ Apply"), callback_data=f"apply:{pid}"),
            InlineKeyboardButton(ui(update, "❌ Отмена", "❌ Cancel"), callback_data=f"cancel:{pid}")]])
        if len(diff) <= DIFF_INLINE_LIMIT:
            await send(target, f"{summary}\n\n{diff}".rstrip(), keyboard)
        else:
            await target.reply_document(document=io.BytesIO(diff.encode("utf-8")), filename="changes.diff",
                                        caption=summary[:1000], reply_markup=keyboard)


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
        if update.effective_chat.type == "private":
            await send(update.effective_message, ui(update, "Доступ к Secret AI ограничен.", "Access to Secret AI is restricted."))
        return False
    # Commands with personal data (history, memory, apps) stay in private chats; groups get their own chat mode.
    if update.effective_chat.type != "private":
        await send(update.effective_message, ui(update, "Напишите Secret AI в личные сообщения.", "Please message Secret AI privately."))
        return False
    context.bot_data["connections"].claim_owner(update.effective_user.id)
    if not context.bot_data.get("logo_id") and not context.bot_data.get("logo_tried"):
        context.application.create_task(ensure_logo(context.application))
    return True


HELP_RU = (
    "Я Secret AI ✨ Отправьте текст, голосовое, аудио, кружок или фото (можно файлом) с вопросом.\n\n"
    "Пока я думаю, вы увидите мой логотип, а ответ будет появляться прямо по ходу печати.\n"
    "На голосовое и кружок я отвечаю только голосом.\n"
    "Код и скрипты присылаю файлами, а сайт ещё и публикую — с кнопкой «Открыть сайт».\n"
    "Добавьте меня в группу: отвечаю на reply и на @упоминание, помню чат, иногда сам кидаю мемы, цитаты и демотиваторы. "
    "В группе: /q — цитата (ответом на сообщение), /dem — демотиватор, /meme — мем из TikTok.\n\n"
    "/mode — режим: ассистент, программист, переводчик, креатив\n"
    "/reset — очистить память\n"
    "/export — скачать историю диалога\n"
    "/myid — ваш Telegram ID\n"
    "/welcome — приветственный кружок (владелец: /setwelcome ответом на свой кружок)\n"
    "/app — Mini App: приложения в одно касание, режимы, модель\n"
    "/secretary — ИИ-секретарь: отвечает людям в твоей личке, пока ты занят (Telegram Business)\n"
    "/emojiid — узнать ID анимированного эмодзи\n"
    "/help — эта справка\n\n"
    "Я помню последние 300 сообщений нашего диалога."
)
HELP_EN = (
    "I'm Secret AI ✨ Send text, a voice message, audio, a video note or a photo (also as a file) with a question.\n\n"
    "While I'm thinking you'll see my logo, and the answer appears as it is being written.\n"
    "I answer voice messages with my voice.\n"
    "Code and scripts come as files; a website is also published with an “Open” button.\n"
    "Add me to a group: I answer replies and @mentions, remember the chat and sometimes post memes, quotes and demotivators. "
    "In a group: /q — quote (reply to a message), /dem — demotivator, /meme — a TikTok meme.\n\n"
    "/mode — mode: assistant, coder, translator, creative\n"
    "/reset — clear memory\n"
    "/export — download the conversation\n"
    "/myid — your Telegram ID\n"
    "/app — Mini App: one-tap apps, modes, model\n"
    "/secretary — AI secretary: answers people in your chats while you're busy (Telegram Business)\n"
    "/emojiid — get a custom emoji ID\n"
    "/help — this help\n\n"
    "I remember our last 300 messages."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update, context):
        return
    store, user_id = context.bot_data["connections"], update.effective_user.id
    if not store.get_meta(f"welcomed:{user_id}"):
        await send_welcome(context, update.effective_message, user_id)
    # The video note already says hi; the full list of features is one command away.
    await send(update.effective_message, ui(update, "Если хотите ознакомиться с моими функциями, напишите /help",
                                            "To see what I can do, send /help"))


async def prepare_welcome(app):
    """Make the welcome video note once (an AI presenter if fal is set up, otherwise the talking mascot)."""
    data, store = app.bot_data, app.bot_data["connections"]
    if store.get_meta("welcome_note") or await restore_welcome(app) or not data.get("tts_model"):
        return
    try:
        voice = await welcome.speech(data["client"], data["tts_model"], data["tts_voice"], welcome.TEXT["ru"])
    except Exception as exc:  # no voice, no welcome note
        LOG.warning("Welcome voice failed: %s %s", type(exc).__name__, getattr(exc, "message", "") or str(exc)[:200])
        return
    fal_key = gen_settings(data)[0]
    if fal_key:
        try:
            data["welcome_bytes"] = await welcome.presenter_note(data["http"], fal_key, voice)
            return
        except Exception as exc:  # the mascot still says hi
            LOG.warning("Welcome presenter failed: %s %s", type(exc).__name__, str(exc)[:200])
    try:
        data["welcome_bytes"] = await welcome.mascot_note(voice)
    except Exception as exc:
        LOG.warning("Welcome note failed: %s %s", type(exc).__name__, str(exc)[:200])


async def make_welcome(app):
    """Make the welcome note right after start and park it pinned in the owner's chat: new users get it instantly."""
    await prepare_welcome(app)
    data, store = app.bot_data, app.bot_data["connections"]
    owner = store.get_meta("owner")
    if not data.get("welcome_bytes") or not owner or store.get_meta("welcome_note"):
        return
    try:
        note = await app.bot.send_video_note(owner, InputFile(data["welcome_bytes"], filename="welcome.mp4"),
                                             length=welcome.SIZE, disable_notification=True)
        await app.bot.pin_chat_message(owner, note.message_id, disable_notification=True)
    except TelegramError as exc:
        LOG.warning("Could not park the welcome note: %s", type(exc).__name__)
        return
    if note.video_note:
        store.set_meta("welcome_note", {"file_id": note.video_note.file_id, "version": welcome.VERSION})
        data.pop("welcome_bytes", None)


async def restore_welcome(app):
    """The welcome note survives a wiped database: it is kept pinned in the owner's chat with the bot. True if found."""
    store, owner = app.bot_data["connections"], app.bot_data["connections"].get_meta("owner")
    if not owner:
        return False
    try:
        pinned = (await app.bot.get_chat(owner)).pinned_message
    except Exception as exc:  # network, or a chat field this library version can't read
        LOG.warning("Could not read the pinned welcome note: %s", type(exc).__name__)
        return False
    if pinned and pinned.video_note and pinned.from_user and pinned.from_user.id == app.bot.id:
        if pinned.date.timestamp() >= welcome.VERSION_SINCE:
            store.set_meta("welcome_note", {"file_id": pinned.video_note.file_id, "version": "pinned"})
            return True
        try:  # an earlier design: make the new one instead
            await app.bot.unpin_chat_message(owner, pinned.message_id)
        except TelegramError:
            pass
    return False


async def keep_welcome(context, file_id):
    """Pin the welcome note silently in the owner's chat so a restart (wiped database) can find it again."""
    owner = context.bot_data["connections"].get_meta("owner")
    if not owner:
        return
    try:
        note = await context.bot.send_video_note(owner, file_id, disable_notification=True)
        await context.bot.pin_chat_message(owner, note.message_id, disable_notification=True)
    except TelegramError as exc:
        LOG.warning("Could not keep the welcome note: %s", type(exc).__name__)


async def deliver_welcome(context, target, user_id):
    """Send the round welcome video: by its saved file_id or the fresh file (its file_id is saved). True if sent."""
    data, store = context.bot_data, context.bot_data["connections"]
    saved = store.get_meta("welcome_note")
    try:
        if saved:
            await target.reply_video_note(saved["file_id"])
        elif data.get("welcome_bytes"):
            sent = await target.reply_video_note(InputFile(data["welcome_bytes"], filename="welcome.mp4"), length=welcome.SIZE)
            if sent.video_note:
                store.set_meta("welcome_note", {"file_id": sent.video_note.file_id, "version": welcome.VERSION})
                data.pop("welcome_bytes", None)
                await keep_welcome(context, sent.video_note.file_id)
        else:
            return False
    except TelegramError as exc:
        LOG.warning("Welcome note not sent: %s %s", type(exc).__name__, getattr(exc, "message", ""))
        return False
    store.set_meta(f"welcomed:{user_id}", True)
    return True


async def send_welcome(context, target, user_id):
    """The welcome note now if it's ready; otherwise it is sent by itself as soon as it's made. True if sent now."""
    if await deliver_welcome(context, target, user_id):
        return True
    data = context.bot_data
    task = data.get("welcome_task")
    if not task or task.done():  # not being made (e.g. it failed): try again in the background
        task = data["welcome_task"] = asyncio.create_task(make_welcome(context.application))

    async def later():
        try:
            await asyncio.wait_for(asyncio.shield(task), 900)
        except Exception:  # failed or too slow: nothing to send
            return
        await deliver_welcome(context, target, user_id)

    background = asyncio.create_task(later())
    data.setdefault("welcome_waiters", set()).add(background)
    background.add_done_callback(data["welcome_waiters"].discard)
    return False


async def added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The bot was just added to a group: say hello there with the same round welcome video as after /start."""
    message = update.effective_message
    if not any(member.id == context.bot.id for member in message.new_chat_members or []):
        return
    try:
        await send_welcome(context, message, update.effective_user.id if update.effective_user else 0)
    except TelegramError as exc:  # no right to post video notes, etc.: just stay quiet
        LOG.warning("Group welcome failed: %s", type(exc).__name__)


async def welcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/welcome: show the welcome video note again."""
    if not await authorized(update, context):
        return
    if not await send_welcome(context, update.effective_message, update.effective_user.id):
        await send(update.effective_message, ui(update, "🎬 Кружок ещё готовится — пришлю, как только будет готов.",
                                                "🎬 The video note is still being made — I'll send it when it's ready."))


async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setwelcome in reply to a video note (owner only): new users get that note instead."""
    if not await authorized(update, context):
        return
    message, store = update.effective_message, context.bot_data["connections"]
    if store.get_meta("owner") != update.effective_user.id:
        await send(message, ui(update, "Менять приветствие может только владелец бота.", "Only the bot owner can change the welcome."))
        return
    note = message.reply_to_message.video_note if message.reply_to_message else None
    if not note:
        await send(message, ui(update, "Запиши кружок, а потом ответь на него командой /setwelcome.",
                               "Record a video note, then reply to it with /setwelcome."))
        return
    store.set_meta("welcome_note", {"file_id": note.file_id, "version": "custom"})
    await keep_welcome(context, note.file_id)
    await send(message, ui(update, "✅ Теперь новые пользователи будут получать этот кружок.", "✅ New users will get this video note."))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        await send(update.effective_message, ui(update, HELP_RU, HELP_EN))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        async with user_lock(context, update.effective_user.id):
            context.bot_data["memory"].reset(update.effective_user.id)
        await send(update.effective_message, ui(update, "🧹 Память очищена. Начнём новый диалог!", "🧹 Memory cleared. Let's start a new conversation!"))


async def wipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/wipe (owner only): forget everything — every private chat, the facts about people and the groups' history."""
    if not await authorized(update, context):
        return
    if context.bot_data["connections"].get_meta("owner") != update.effective_user.id:
        await send(update.effective_message, "Это может только владелец бота.")
        return
    await send(update.effective_message, "⚠️ Стереть ВСЮ память бота: переписки всех людей, факты о людях и историю групп? "
               "Это нельзя отменить.", InlineKeyboardMarkup([[InlineKeyboardButton("Да, стереть всё", callback_data="wipe:yes"),
                                                               InlineKeyboardButton("Отмена", callback_data="wipe:no")]]))


def wipe_all(data):
    memory, groups = data["memory"], data["groups"]
    with memory.db:
        memory.db.execute("DELETE FROM messages")
        memory.db.execute("DELETE FROM facts")
    with groups.db:
        groups.db.execute("DELETE FROM group_messages")
        groups.db.execute("DELETE FROM group_phrases")


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


async def emojiid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show IDs of custom emoji in the message, e.g. to put the logo into BRAND_EMOJI_ID."""
    if not await authorized(update, context):
        return
    message = update.effective_message
    found = [(text, entity.custom_emoji_id) for entity, text in message.parse_entities([MessageEntity.CUSTOM_EMOJI]).items()]
    if not found:
        await send(message, ui(update, "Отправьте /emojiid вместе с анимированным эмодзи, например: /emojiid ✨",
                               "Send /emojiid together with a custom emoji, e.g. /emojiid ✨"))
        return
    await send(message, "\n".join(f"{text}  BRAND_EMOJI={text}  BRAND_EMOJI_ID={emoji_id}" for text, emoji_id in found))


def app_url(context):
    base = context.bot_data.get("public_url")
    return base + "/app" if base else None


async def open_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update, context):
        return
    url = app_url(context)
    if not url:
        await send(update.effective_message, ui(update, "Mini App не настроен: задайте PUBLIC_URL (на Render он определяется сам).",
                                                "The Mini App is not configured: set PUBLIC_URL (Render sets it automatically)."))
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(ui(update, "✨ Открыть Secret AI", "✨ Open Secret AI"), web_app=WebAppInfo(url))]])
    await send(update.effective_message, ui(
        update, "Подключайте приложения одной кнопкой — Gmail, Notion, Slack, GitHub и тысячи других — и просто пишите, что сделать.",
        "Connect apps with one tap — Gmail, Notion, Slack, GitHub and thousands more — then just tell me what to do."), keyboard)


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


WHISPER_LANGS = {"ru", "en", "uk", "kk", "be", "uz", "de", "fr", "es", "it", "pt", "tr", "pl", "ar", "zh", "ja", "ko"}


async def media_text(media, client, language=None):
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
            # A hint keeps one-second clips from being "recognized" as Portuguese or Chinese.
            hint = {"language": language} if language in WHISPER_LANGS else {}
            result = await client.audio.transcriptions.create(model="whisper-1", file=audio, **hint)
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
            transcript = await media_text(media, client, (update.effective_user.language_code or "ru")[:2])
        except BaseException:
            await status.delete()
            raise
        if not transcript:
            await status.edit_text(ui(update, "Не удалось распознать речь. Запишите ещё раз.", "No speech recognized. Please try again."))
            return None
        await status.delete()  # the answer comes as a voice message; no transcript bubble in between
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


def user_model(bot_data, user_id):
    chosen = bot_data["memory"].model(user_id)
    return chosen if chosen in bot_data["models"] else bot_data.get("chat_default") or bot_data["model"]


async def app_tools(bot_data, user_id, taken):
    """MCP tools of apps the user connected in one tap; an outage of one source must not break the answer."""
    tools, names = [], []
    try:
        tools, names = await asyncio.to_thread(bot_data["direct"].mcp_tools, user_id, taken)
    except (MCPAuthError, httpx.HTTPError, KeyError, ValueError) as exc:
        LOG.warning("Directly connected apps unavailable: %s", type(exc).__name__)
    pipedream = bot_data["pipedream"]
    if pipedream.enabled:
        try:
            more, more_names = await asyncio.to_thread(pipedream.mcp_tools, user_id, list(taken) + [t["server_label"] for t in tools])
            tools, names = tools + more, names + more_names
        except (PipedreamError, httpx.HTTPError, KeyError, ValueError) as exc:
            LOG.warning("Connected apps unavailable: %s", type(exc).__name__)
    return tools, names


def fit_history(history):
    """The newest messages in full, older long ones shortened, all within HISTORY_CHARS (oldest dropped first)."""
    kept, total = [], 0
    for index, item in enumerate(reversed(history)):
        text = item["content"]
        if index >= 6 and len(text) > HISTORY_ITEM_CHARS:
            text = text[:HISTORY_ITEM_CHARS] + " …"
        if total + len(text) > HISTORY_CHARS:
            break
        kept.append({"role": item["role"], "content": text})
        total += len(text)
    kept.reverse()
    while kept and kept[0]["role"] != "user":  # start with a question, not an orphaned answer
        kept.pop(0)
    return kept


async def respond(update, context, target, content, saved_question, voice=False):
    """Answer in the chat; voice=True (the user sent a voice message) also sends the answer as a voice message."""
    user_id = update.effective_user.id
    memory = context.bot_data["memory"]
    persona = PERSONAS[memory.persona(user_id)]
    prune_pending(context.bot_data["pending"])
    # The logo shows at once (or "recording a voice message…" for voice); tokens and apps are prepared meanwhile.
    if voice and context.bot_data.get("tts_model"):
        live = SilentReply(target)
        await live.start()
    else:
        live = await open_reply(target, lang(update), context.bot_data)
    try:
        taken = [server["label"] for server in context.bot_data["connections"].public(user_id) if server["service"] == "mcp"]
        _, apps = await asyncio.gather(context.bot_data["github_apps"].fresh_token(context.bot_data["http"], user_id),
                                       app_tools(context.bot_data, user_id, taken))
    except BaseException:
        await live.abort(ui(update, "⚠️ Ответ прерван.", "⚠️ The answer was interrupted."))
        raise
    # Free and paid image/video options are always available.
    async def media(kind, prompt, fmt, seconds=5, voice=True):
        error = await start_generation(context.application, user_id, lang(update), kind, prompt, fmt, seconds, voice)
        if error == "no_credits":
            return ("Not started: the user has no free generations or credits left. Tell them to top up in the Mini App "
                    "(button Secret AI → Generation) with Telegram Stars.")
        if error:
            return f"Not started ({error}). Tell the user briefly that generation is unavailable right now."
        return "Started. The result will arrive in the chat by itself in a moment; tell the user briefly."
    toolbox = Toolbox(context.bot_data["http"], context.bot_data["connections"], context.bot_data["pending"], user_id, lang(update),
                      apps=apps, media=media, extra=[memory_tool(context, user_id), secretary_tool(context, user_id, lang(update) == "ru"),
                                                     edit_tool(context, user_id, lang(update))])
    instructions = (SYSTEM + TRANSLATION_RULES + CODE_RULES + (" " + persona[3] if persona[3] else "") + " Telegram language: "
                    + (update.effective_user.language_code or "ru"))
    speak_only = voice and bool(context.bot_data.get("tts_model"))
    if speak_only:
        instructions += (" The user sent a voice message and your answer will be read aloud as a voice message: talk like "
                         "in a live conversation, short (2-5 sentences), no lists, no markdown, no links, no emoji.")
    if toolbox.instructions():
        instructions += " " + toolbox.instructions()
    try:
        answer, incomplete = await generate(
            context, live, toolbox, model=user_model(context.bot_data, user_id), instructions=instructions,
            input=fit_history(memory.history(user_id)) + [{"role": "user", "content": content}],
            store=False, max_output_tokens=32000)
    except BaseException:
        await live.abort(ui(update, "⚠️ Ответ прерван.", "⚠️ The answer was interrupted."))
        raise
    answer = answer.strip()
    if not answer and toolbox.proposals:
        answer = ui(update, "Подготовил изменения — проверьте и подтвердите ниже.", "Changes are ready — review and confirm below.")
    if not answer:
        await live.finish(ui(update, "Модель не вернула текст. Попробуйте переформулировать вопрос.", "The model returned no text. Please rephrase your question."))
        return
    if incomplete:
        answer += ui(update, "\n\nОтвет достиг лимита. Напишите «продолжи».", "\n\nOutput limit reached. Ask me to continue.")
    memory.append_turn(user_id, saved_question, answer[:12000])
    text, files = extract_files(answer)
    if speak_only and not files and not toolbox.proposals:
        await live.stop()
        try:
            await asyncio.wait_for(speak(context, target, text), VOICE_TIMEOUT)
            return
        except Exception as exc:  # no voice: the answer still arrives, as text
            LOG.warning("Voice answer failed: %s", type(exc).__name__)
        voice = False
    last = await live.finish(text or ui(update, "📎 Готово — файлы ниже.", "📎 Done — the files are below."))
    context.bot_data["last_answer"][user_id] = last.message_id
    await send_files(update, context, target, files)
    answer = text
    if voice and context.bot_data.get("tts_model") and not speak_only:
        # Recorded in the background: the user's next message must not wait for the voice (it keeps the lock otherwise).
        task = asyncio.create_task(voice_reply(update, context, target, answer))
        tasks = context.bot_data.setdefault("voice_tasks", set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    await show_proposals(update, context, toolbox, target)


async def send_files(update, context, target, files):
    """Send code files from an answer; a website is also published with an "Open" button."""
    page = site_file(files)
    keyboard = None
    if page and context.bot_data["public_url"]:
        site_id = context.bot_data["connections"].save_site(page[1])
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(ui(update, "🌐 Открыть сайт", "🌐 Open the website"),
                                                               url=f"{context.bot_data['public_url']}/s/{site_id}")]])
    for name, data in files:
        markup = keyboard if page and name == page[0] else None
        await target.reply_document(document=io.BytesIO(data), filename=name, reply_markup=markup)


class GroupReply:
    """In groups: just the animated logo and "…" until the answer is ready, then the answer in its place
    (Telegram's see-through thinking block exists only in private chats)."""

    def __init__(self, target, config):
        self.target, self.config = target, config
        self.text, self.message, self.typing = "", None, None

    def status(self, note):
        pass

    async def start(self):
        frame = f"{self.config['logo']} …"
        self.message = await with_logo(self.config, lambda entities: reply(self.target, frame, entities=entities))
        self.typing = asyncio.create_task(typing_loop(self.target, ChatAction.TYPING))

    async def update(self, text):
        self.text = text

    async def _stop(self):
        if self.typing:
            self.typing.cancel()
            await asyncio.gather(self.typing, return_exceptions=True)
            self.typing = None

    async def finish(self, text, reply_markup=None):
        await self._stop()
        parts = list(chunks(text)) or ["…"]
        try:
            await self.message.edit_text(parts[0], parse_mode=None, reply_markup=reply_markup if len(parts) == 1 else None)
            last = self.message
        except BadRequest:
            await self.message.delete()
            last = await reply(self.target, parts[0], reply_markup if len(parts) == 1 else None)
        for index, part in enumerate(parts[1:], 2):
            last = await reply(self.target, part, reply_markup if index == len(parts) else None)
        return last

    async def abort(self, note):
        await self._stop()
        try:
            await self.message.delete()
        except TelegramError:
            pass


def speaker(user):
    return (user.full_name or user.username or "?")[:40]


def addressed_to_bot(message, bot, bot_data):
    """A reply to the bot, its @username (the current one) or a mention of the bot."""
    replied = message.reply_to_message
    if replied and replied.from_user and replied.from_user.id == bot.id:
        return True
    username = (bot_data.get("bot_name") or bot.username or "").lower()
    entities = message.parse_entities() if message.text else message.parse_caption_entities()
    for entity, text in entities.items():
        if entity.type == MessageEntity.MENTION and username and text.lower() == "@" + username:
            return True
        if entity.type == MessageEntity.TEXT_MENTION and entity.user and entity.user.id == bot.id:
            return True
    return False


def describe_message(message):
    text = message.text or message.caption or ""
    kind = ("фото" if message.photo else "голосовое" if message.voice else "кружок" if message.video_note else
            "стикер " + (message.sticker.emoji or "") if message.sticker else "видео" if message.video else
            "гифка" if message.animation else "файл" if message.document else "")
    return f"[{kind}] {text}".strip() if kind else text


def remember_group_message(store, message, name):
    """Keep the line for context, the chat's stickers/gifs/photos and its catchphrases."""
    chat_id = message.chat_id
    said = describe_message(message)
    if said:
        store.add_line(chat_id, f"{name}: {said}", message.message_id, message.from_user.id if message.from_user else None)
    if message.sticker:
        store.add_media(chat_id, "sticker", message.sticker.file_unique_id, message.sticker.file_id)
    elif message.animation:
        store.add_media(chat_id, "gif", message.animation.file_unique_id, message.animation.file_id)
    elif message.photo:
        store.add_media(chat_id, "photo", message.photo[-1].file_unique_id, message.photo[-1].file_id)
    if message.text and not message.text.startswith("/"):
        store.add_phrase(chat_id, message.text)


async def group_chat(update, context):
    """Group chats: remember the chat, answer when addressed, and sometimes do something uninvited."""
    message, user, data = update.effective_message, update.effective_user, context.bot_data
    if not user or user.is_bot or (data["allowed"] and user.id not in data["allowed"]):
        return
    chat_id, store, name = message.chat_id, data["groups"], speaker(user)
    store.touch(chat_id, message.chat.title, user.id)
    lock = data["locks"][chat_id]
    if not addressed_to_bot(message, context.bot, data):
        remember_group_message(store, message, name)
        span = GROUP_EVERY.get(store.level(chat_id))
        if not span:  # off
            return
        counters = data.setdefault("group_count", {})
        due = data.setdefault("group_due", {})
        counters[chat_id] = counters.get(chat_id, 0) + 1
        due.setdefault(chat_id, random.randint(*span))
        quiet = time.monotonic() - data.setdefault("group_surprised", {}).get(chat_id, float("-inf"))
        if counters[chat_id] < due[chat_id] or lock.locked() or quiet < GROUP_RANDOM_PAUSE:
            return
        counters[chat_id], due[chat_id] = 0, random.randint(*span)
        data["group_surprised"][chat_id] = data["group_spoke"][chat_id] = time.monotonic()
        async with lock:
            try:
                await group_surprise(context, message, user)
            except Exception as exc:  # an uninvited joke that failed is no reason to bother the chat
                LOG.warning("Group surprise failed: %s", type(exc).__name__)
        return
    async with lock:
        if await group_generation(update, context):
            return
        if await meme_request(update, context):
            return
        if game.GAME_ASK.search(message.text or "") and len(message.text or "") < 120:
            await quiz(context).open_lobby(context.bot, message, user, name)
            return
        readable = (message.text or message.caption or message.voice or message.audio or message.video_note or message.photo
                    or (message.document and (message.document.mime_type or "").startswith("image/")))
        if readable:
            parsed = await read_question(update, context)
            if not parsed:
                return
            question, content, _ = parsed
        else:  # a sticker, gif or video in reply to the bot
            question = content = describe_message(message) or "…"
        before = "\n".join(store.lines(chat_id))
        older = store.search(chat_id, question)  # from the 10 000 kept: the old messages this one is about
        store.add_line(chat_id, f"{name}: {question}", message.message_id, user.id)
        head = ((f"Older messages from this group that may be relevant (oldest first):\n" + "\n".join(older) + "\n\n"
                 if older else "")
                + (f"Recent messages in the group (oldest first):\n{before}\n\n" if before else "") + f"{name} writes to you:")
        content = ([{"type": "input_text", "text": head}] + content if isinstance(content, list) else f"{head}\n{content}")
        data["group_spoke"][chat_id] = time.monotonic()
        await group_respond(update, context, message, content, voice=bool(message.voice or message.video_note))


def group_instructions(update):
    return GROUP_STYLE + TRANSLATION_RULES + CODE_RULES + " Reply in the chat's language. Telegram language: " + (update.effective_user.language_code or "ru")


def humanize(text):
    """Strip the tells of a bot: long dashes and a full stop at the end of a one-liner."""
    text = text.replace(" — ", ", ").replace("—", "-").strip()
    if "\n" not in text and text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    return text


GROUP_MEDIA_TOOL = {"type": "function", "name": "make_media",
                    "description": ("Actually make a video or a picture and post it in this group. Call it whenever someone "
                                    "asks for a video/picture or agrees to one you offered (\"ну кинь\", \"давай\", \"го\"). "
                                    "You CAN make videos and pictures: never say you can't. The result arrives in the chat by "
                                    "itself in a few minutes; just say briefly that it's coming."),
                    "parameters": {"type": "object", "properties": {
                        "kind": {"type": "string", "enum": ["video", "photo"]},
                        "description": {"type": "string", "description": (
                            "what to show, detailed, as agreed in the chat (any language). Any words to appear on the "
                            "picture go in quotes exactly as they must be written, spelled and declined correctly "
                            "(e.g. «для Айлы»)")},
                        "seconds": {"type": "integer", "description": "video length asked for, 8 if not said"}},
                        "required": ["kind", "description"], "additionalProperties": False}}


MEDIA_TALK = re.compile(r"видео|видос|ролик|клип|картин|фотк|нарису|video|picture|image", re.I)


class GroupTools:
    """The only tool in a group: make a video or picture for the person who asked (they pay, the owner doesn't)."""

    def __init__(self, update, context, message):
        self.update, self.context, self.message = update, context, message

    def definitions(self):
        return [GROUP_MEDIA_TOOL, MEMORY_TOOL]

    def status(self, name, args):
        return None

    async def run(self, name, args):
        if name == "remember":  # about the person who wrote to the bot
            self.context.bot_data["memory"].remember(self.update.effective_user.id, args.get("add") or [],
                                                     args.get("forget") or [])
            return "Saved."
        if name != "make_media":
            return "Unknown tool."
        kind = "video" if args.get("kind") == "video" else "photo"
        seconds = next((s for s in sorted(VIDEO_PRICES) if s >= int(args.get("seconds") or SCENE_SECONDS)), max(VIDEO_PRICES))
        error = await start_generation(self.context.application, self.update.effective_user.id, lang(self.update), kind,
                                       str(args.get("description") or ""), "portrait", seconds if kind == "video" else 5, True,
                                       chat_id=self.message.chat_id, reply_id=self.message.message_id)
        if error == "no_credits":
            return ("Not started: this person has no free generations or credits left. Tell them to top up in the bot's "
                    "Mini App (Генерация → Пополнить).")
        if error:
            return f"Not started ({error}). Say briefly that it doesn't work right now."
        return "Started. It will be posted in reply in a few minutes; say so in a few words."


async def group_respond(update, context, message, content, voice=False):
    """Answer in the group; a voice message gets only a voice message back."""
    data = context.bot_data
    voice = voice and bool(data.get("tts_model"))
    live = SilentReply(message) if voice else GroupReply(message, data)
    await live.start()
    try:
        # Only the media tool in groups: someone else's apps, repos and money must not be reachable from a shared chat.
        model = group_model(data, message.chat_id)
        if is_abacus(model) and MEDIA_TALK.search(str(content)):
            model = data["model"]  # the media tool needs function calls, which the Abacus path doesn't do
        answer, _ = await generate(context, live, GroupTools(update, context, message), model=model,
                                   instructions=group_instructions(update)
                                   + memory_notes(data, update.effective_user.id, speaker(update.effective_user))
                                   + " Save lasting facts about the person writing with the remember tool." + (
                                       " This person sent a voice message: your answer will be read aloud, so talk like "
                                       "in a live chat, 1-3 short sentences, no lists, no links, no emoji." if voice else ""),
                                   input=[{"role": "user", "content": content}], store=False, max_output_tokens=32000)
    except BaseException:
        await live.abort(None)
        raise
    text, files = extract_files(answer.strip())
    if not files:
        text = humanize(text)
    if not text and not files:
        await live.abort(None)
        return
    if voice and text and not files:
        await live.stop()
        try:
            await asyncio.wait_for(speak(context, message, text), VOICE_TIMEOUT)
            data["groups"].add_line(message.chat_id, "Secret AI (голосом): " + text[:600])
            return
        except Exception as exc:  # no voice: the answer still arrives, as text
            LOG.warning("Group voice answer failed: %s", type(exc).__name__)
    await live.finish(text or "📎")
    data["groups"].add_line(message.chat_id, "Secret AI: " + (text or "[files]")[:600])
    await send_files(update, context, message, files)


# ----- uninvited: no logo, no "typing", nothing that gives it away -----

def reply_to(message_id):
    """Reply to a message if it still exists, otherwise just post."""
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None


def surprise_target(store, message):
    """Whom to answer: usually the fresh message, sometimes an earlier one, sometimes nobody (just post)."""
    roll = random.random()
    if roll < 0.5:
        return message.message_id, None
    if roll < 0.75:
        earlier = [row for row in store.recent_messages(message.chat_id, 15) if row[0] != message.message_id]
        if earlier:
            return random.choice(earlier)
    return None, None


async def group_surprise(context, message, user):
    """Do one thing on its own: a remark, the chat's sticker or gif, a catchphrase, a quote picture or a demotivator.

    Often as a reply to someone's message (the fresh one or an earlier one), sometimes as a plain post.
    """
    store, chat_id = context.bot_data["groups"], message.chat_id
    options = [("remark", 35)]
    if store.has_media(chat_id, "sticker") or store.has_media(chat_id, "gif"):
        options.append(("media", 25))
    if store.catchphrase(chat_id):
        options.append(("phrase", 12))
    if message.text and 3 <= len(message.text.split()) and len(message.text) <= 300:
        options.append(("quote", 18))
    if store.has_media(chat_id, "photo"):
        options.append(("demotivator", 10))
    options.append(("tiktok", 15))
    action = random.choices([o[0] for o in options], weights=[o[1] for o in options])[0]
    target, target_line = surprise_target(store, message)
    if action == "media":
        await send_chat_media(context, chat_id, ("sticker", "gif"), target)
    elif action == "phrase":
        phrase = store.catchphrase(chat_id)
        await context.bot.send_message(chat_id, phrase, reply_parameters=reply_to(target))
        store.add_line(chat_id, "Secret AI: " + phrase)
    elif action == "quote":
        await send_quote(context, chat_id, message.text, user, message.message_id if target else None)
    elif action == "demotivator":
        await send_demotivator(context, chat_id, None, reply_id=target)
    elif action == "tiktok" and await send_meme(context, chat_id, None, target):
        pass
    else:
        if target == message.message_id:
            target_line = f"{speaker(user)}: {describe_message(message)}"
        await group_remark(context, message.chat_id, target, target_line)


def group_model(data, chat_id):
    """The model a group talks with: its own choice from the Mini App if still offered, else the bot's default."""
    chosen = data["groups"].model(chat_id)
    return chosen if chosen and chosen in data.get("models", ()) else data.get("chat_default") or data["model"]


async def quick_answer(data, model, instructions, text, max_tokens=2000):
    """A short non-streamed answer from either an OpenAI model or a RouteLLM one."""
    provider = chat_provider(model)
    if provider and data.get(CHAT_PROVIDERS[provider]):
        response = await data[CHAT_PROVIDERS[provider]].chat.completions.create(
            model=model[len(provider):], max_tokens=max_tokens,
            messages=[{"role": "system", "content": instructions + model_identity(model)}, {"role": "user", "content": text}])
        return response.choices[0].message.content or ""
    response = await data["client"].responses.create(model=data["model"] if provider else model, store=False,
                                                     instructions=instructions, max_output_tokens=max_tokens, input=text)
    return response.output_text or ""


async def group_remark(context, chat_id, target=None, target_line=None):
    data = context.bot_data
    lines = data["groups"].lines(chat_id, 120)
    task = (f"You are replying directly to this message: «{target_line}». React to it (address its author by name). "
            if target_line else "") + INTERJECT
    text = humanize(await quick_answer(data, group_model(data, chat_id), GROUP_STYLE,
                                       "Recent messages in the group (oldest first):\n" + "\n".join(lines) + "\n\n" + task))
    if not text or text.upper().startswith("SKIP"):
        return
    await context.bot.send_message(chat_id, text[:1000], reply_parameters=reply_to(target))
    data["groups"].add_line(chat_id, "Secret AI: " + text[:600])


async def send_chat_media(context, chat_id, kinds, target=None):
    store = context.bot_data["groups"]
    for _ in range(3):  # a file Telegram no longer knows is forgotten and another one tried
        picked = store.random_media(chat_id, kinds)
        if not picked:
            return
        kind, file_id = picked
        try:
            if kind == "sticker":
                await context.bot.send_sticker(chat_id, file_id, reply_parameters=reply_to(target))
            elif kind == "gif":
                await context.bot.send_animation(chat_id, file_id, reply_parameters=reply_to(target))
            else:
                await context.bot.send_photo(chat_id, file_id, reply_parameters=reply_to(target))
            store.add_line(chat_id, f"Secret AI: [{'стикер' if kind == 'sticker' else 'гифка' if kind == 'gif' else 'фото'}]")
            return
        except BadRequest:
            store.forget_media(chat_id, file_id)


MEME_ASK = re.compile(r"(скин|кин|кида|дава|покаж|дай|отправ|хочу|го\b|send|show|give).{0,40}(мем|тик ?ток|tik ?tok|meme)"
                      r"|(мем|тик ?ток|tik ?tok|meme).{0,20}(скин|кин|дава|покаж|дай|плиз|пж)", re.I | re.S)
MEME_FILLER = re.compile(r"@\w+|\b(скинь|кинь|кидай|давай|покажи|дай|отправь|хочу|го|пж|плиз|пожалуйста|please|send|show|give|me|a|"
                         r"мемы|мемчик|мемас|мем|тикток|тик ток|tiktok|tik tok|видео|видос|из|с|в|про|какой|нибудь|-?то|meme|memes|"
                         r"about|from)\b", re.I)


def meme_query(text):
    """"скинь мем про котов" -> "мем коты"-like search words, or None for any meme."""
    rest = " ".join(MEME_FILLER.sub(" ", text or "").split()).strip(" ,.!?")
    return f"мем {rest}" if rest else None


async def send_meme(context, chat_id, query=None, reply_id=None):
    """A funny Russian-language TikTok video (or, if TikTok search is down, the chat's own gif). True if sent."""
    store, http = context.bot_data["groups"], context.bot_data["http"]
    try:
        video_id, video, title = await tiktok_meme(http, query, {k[3:] for k in store.sent_keys(chat_id) if k.startswith("tt:")})
        await context.bot.send_video(chat_id, InputFile(video, filename="meme.mp4"), supports_streaming=True,
                                     reply_parameters=reply_to(reply_id), write_timeout=120, read_timeout=120)
        store.mark_sent(chat_id, "tt:" + video_id)
        store.add_line(chat_id, f"Secret AI: [видео-мем из TikTok: {title}]")
        return True
    except (MemeError, TelegramError) as exc:
        LOG.warning("TikTok meme failed: %s", type(exc).__name__)
    # No English picture memes: the chat's own stickers and gifs are the fallback.
    if store.has_media(chat_id, "gif") or store.has_media(chat_id, "sticker"):
        await send_chat_media(context, chat_id, ("gif", "sticker"), reply_id)
        return True
    return False


def quiz(context):
    """The group quiz, created on first use (it shares the groups' database)."""
    data = context.bot_data
    if "quiz" not in data:
        async def ask(chat_id, instructions, text):
            return await quick_answer(data, group_model(data, chat_id), instructions, text, max_tokens=4000)
        data["quiz"] = game.Quiz(data["groups"].db, ask)
    return data["quiz"]


async def game_command(update, context):
    """/sgame: a quiz for the whole group."""
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    if message.chat.type == "private":
        await reply(message, ui(update, f"Викторина — игра для групп 🎮 Добавь меня в группу и напиши там /{game.START}",
                                f"The quiz is for groups 🎮 Add me to a group and send /{game.START} there"))
        return
    await quiz(context).open_lobby(context.bot, message, user, speaker(user))


async def stop_game_command(update, context):
    """/sstop: stop the group's quiz (its host or an admin)."""
    message, user = update.effective_message, update.effective_user
    if not message or not user or message.chat.type == "private":
        return
    try:
        member = await context.bot.get_chat_member(message.chat_id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except TelegramError:
        is_admin = False
    await message.reply_text(await quiz(context).stop(context.bot, message.chat_id, user.id, is_admin))


async def top_command(update, context):
    """/top: the group's all-time quiz leaderboard."""
    message = update.effective_message
    if not message:
        return
    if message.chat.type == "private":
        await reply(message, ui(update, f"Рейтинг есть только в группах — там, где играют в /{game.START}.",
                                f"The leaderboard lives in groups, where /{game.START} is played."))
        return
    await quiz(context).top(context.bot, message.chat_id, message.chat.title or "Группа")


async def meme_command(update, context):
    """/meme [about what]: a TikTok meme."""
    message = update.effective_message
    if not message:
        return
    await context.bot.send_chat_action(message.chat_id, ChatAction.UPLOAD_VIDEO)
    query = meme_query(" ".join(context.args)) if context.args else None
    if not await send_meme(context, message.chat_id, query, message.message_id):
        await reply(message, ui(update, "Мемы сейчас не грузятся, попробуй чуть позже.", "Memes aren't loading right now, try later."))


async def avatar_bytes(bot, user_id):
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.photos:
            remote = await photos.photos[0][0].get_file()
            return bytes(await remote.download_as_bytearray())
    except TelegramError:
        pass
    return None


async def send_quote(context, chat_id, text, user, reply_id=None):
    """A quote picture: the mascot says the member's message, signed with their name and avatar."""
    avatar = await avatar_bytes(context.bot, user.id)
    picture = await asyncio.to_thread(quote_card, speaker(user), text, avatar)
    await context.bot.send_photo(chat_id, picture, reply_parameters=reply_to(reply_id))
    context.bot_data["groups"].add_line(chat_id, f"Secret AI: [цитата {speaker(user)}: {text[:200]}]")


async def demotivator_caption(context, chat_id, hint):
    """A big title and a small subtitle for a demotivator, in the chat's spirit."""
    data = context.bot_data
    lines = data["groups"].lines(chat_id, 60)
    try:
        response = await data["client"].responses.create(
            model=data["model"], store=False, max_output_tokens=800, text={"format": {"type": "json_object"}},
            instructions=("Write a caption for a classic Russian demotivator poster in the language of the chat: 'title' is 1-3 "
                          "words, 'subtitle' is one short ironic line. Funny, based on the chat; no insults about "
                          'ethnicity, religion, orientation or looks. Return JSON {"title": "...", "subtitle": "..."}.'),
            input="Chat:\n" + "\n".join(lines) + (f"\n\nPhoto caption or request: {hint}" if hint else ""))
        caption = json.loads(response.output_text)
        return str(caption.get("title") or "").strip()[:60] or "Ну да", str(caption.get("subtitle") or "").strip()[:140]
    except Exception as exc:  # a catchphrase still makes a demotivator
        LOG.warning("Demotivator caption failed: %s", type(exc).__name__)
        return data["groups"].catchphrase(chat_id) or "Без комментариев", ""


async def send_demotivator(context, chat_id, photo, hint="", reply_id=None):
    """A demotivator from the given photo (a PhotoSize) or a random photo the chat posted."""
    store = context.bot_data["groups"]
    file_id = photo.file_id if photo else None
    if not file_id:
        picked = store.random_media(chat_id, ("photo",))
        if not picked:
            return False
        file_id = picked[1]
    try:
        remote = await context.bot.get_file(file_id)
        data = bytes(await remote.download_as_bytearray())
    except BadRequest:
        store.forget_media(chat_id, file_id)
        return False
    title, subtitle = await demotivator_caption(context, chat_id, hint)
    picture = await asyncio.to_thread(demotivator, data, title, subtitle)
    await context.bot.send_photo(chat_id, picture, reply_parameters=reply_to(reply_id))
    store.add_line(chat_id, f"Secret AI: [демотиватор «{title}. {subtitle}»]")
    return True


GEN_ASK = re.compile(r"\b(созда|сдела|сгенер|сним|запил|замут|нарису|генер|make|create|generate|draw)\w*\b.{0,40}?"
                     r"\b(?P<kind>видео|видос|ролик|клип|video|clip|фото|картин|изображ|рисун|арт|picture|image|photo|art)", re.I | re.S)
GEN_SECONDS = re.compile(r"(\d{1,3})\s*(с\b|сек|s\b|sec)|(минут|minute)", re.I)


def gen_seconds(text):
    """"на 20 секунд" -> 32, "минуту" -> 64: the shortest video length that fits, 8 s by default."""
    match = GEN_SECONDS.search(text or "")
    asked = 60 if match and match[3] else int(match[1]) if match else SCENE_SECONDS
    return next((s for s in sorted(VIDEO_PRICES) if s >= asked), max(VIDEO_PRICES))


async def group_generation(update, context):
    """"@bot создай видео …" or "сделай видео …" in reply to the bot: generate it in the group; the asker pays. True if handled."""
    message, user = update.effective_message, update.effective_user
    text = message.text or message.caption or ""
    match = GEN_ASK.search(text)
    if not match or message.photo:  # a photo with the request is an edit: the normal answer handles it
        return False
    kind = "video" if match["kind"].lower() in ("видео", "видос", "ролик", "клип", "video", "clip") else "photo"
    prompt = " ".join(re.sub(r"@\w+", " ", text).split())
    ru = lang(update) == "ru"
    error = await start_generation(context.application, user.id, lang(update), kind, prompt, "portrait",
                                   gen_seconds(text) if kind == "video" else 5, True, chat_id=message.chat_id,
                                   reply_id=message.message_id)
    if error == "no_credits":
        note = ("Бесплатные генерации на сегодня кончились — пополни кредиты в мини-аппе бота (Генерация → Пополнить)"
                if ru else "Out of free generations today: top up credits in the bot's Mini App (Create → Top up)")
    elif error in ("no_key", "no_video_key"):
        note = "Генерация видео пока не настроена" if ru else "Video generation isn't set up yet"
    elif error:
        note = "Не получилось запустить, попробуй описать по-другому" if ru else "Couldn't start it, try describing it differently"
    else:
        context.bot_data["groups"].add_line(message.chat_id, f"Secret AI: [{'видео' if kind == 'video' else 'фото'} по запросу]")
        return True
    await message.reply_text(note)
    return True


async def meme_request(update, context):
    """"@bot цитата" / "демотиватор" (in reply to a message): make the picture. True if handled."""
    message = update.effective_message
    text = (message.text or message.caption or "").lower()
    target = message.reply_to_message
    if "цитат" in text or "quote" in text:
        if target and target.from_user and target.from_user.id != context.bot.id and (target.text or target.caption):
            await send_quote(context, message.chat_id, target.text or target.caption, target.from_user, message.message_id)
            return True
    if MEME_ASK.search(text):
        await context.bot.send_chat_action(message.chat_id, ChatAction.UPLOAD_VIDEO)
        if await send_meme(context, message.chat_id, meme_query(text), message.message_id):
            return True
    if "демотиватор" in text or "demotivator" in text:
        photo = (message.photo or (target.photo if target else None) or [None])[-1]
        hint = (target.caption if target and target.caption else "") or message.caption or ""
        if await send_demotivator(context, message.chat_id, photo, hint, message.message_id):
            return True
    return False


async def quote_command(update, context):
    """/q in reply to a message: a quote picture of it."""
    message = update.effective_message
    if not message or update.effective_chat.type not in GROUP_TYPES:
        return
    target = message.reply_to_message
    if not target or not target.from_user or not (target.text or target.caption):
        await reply(message, "Ответь командой /q на сообщение, которое надо процитировать.")
        return
    await send_quote(context, message.chat_id, target.text or target.caption, target.from_user, message.message_id)


async def demotivator_command(update, context):
    """/dem (in reply to a photo, or a random photo from the chat): a demotivator."""
    message = update.effective_message
    if not message or update.effective_chat.type not in GROUP_TYPES:
        return
    target = message.reply_to_message
    photo = (message.photo or (target.photo if target else None) or [None])[-1]
    if not await send_demotivator(context, message.chat_id, photo, (target.caption if target else "") or "", message.message_id):
        await reply(message, "Пока нет фоток из чата. Ответь /dem на фото — сделаю демотиватор.")


async def watch_username(app):
    """Keep the bot's @username fresh: after a rename in BotFather, mentions of the new name work without a restart."""
    while True:
        await asyncio.sleep(USERNAME_REFRESH)
        try:
            me = await app.bot.get_me()
            app.bot_data["bot_name"] = me.username
        except TelegramError:
            pass


# ----- photo editing: the user's recent photos (also albums) changed or combined by Nano Banana / GPT Image -----

EDIT_TOOL = {"type": "function", "name": "edit_photos",
             "description": ("Edit or combine the photos the user sent in this chat (the most recent ones): remove people or "
                             "objects, put a person from one photo next to someone in another, change the background, "
                             "clothes, light or style, restore or enhance. Use it whenever the user asks to change their "
                             "photo(s). The result arrives in the chat by itself; just say briefly that you're on it."),
             "parameters": {"type": "object", "properties": {
                 "instruction": {"type": "string", "description": ("what to do, detailed, in English; refer to the photos "
                                                                   "as the first, second… image in the order they were sent")},
                 "photos": {"type": "integer", "minimum": 1, "maximum": 4, "description": "how many of the latest photos"},
                 "format": {"type": "string", "enum": ["square", "portrait", "landscape"]}},
                 "required": ["instruction", "photos"], "additionalProperties": False}}
RECENT_PHOTOS_SECONDS = 3600


def remember_photo(data, user_id, message):
    """Keep the user's latest photos (an hour) so "убери людей с этой фотки" knows which photos are meant."""
    photo = message.photo[-1].file_id if message.photo else (
        message.document.file_id if message.document and (message.document.mime_type or "").startswith("image/") else None)
    if not photo:
        return
    items = data.setdefault("recent_photos", {}).setdefault(user_id, [])
    items.append((photo, time.time()))
    del items[:-8]


def edit_tool(context, user_id, language):
    data = context.bot_data

    async def run(args):
        fresh = [photo for photo, at in data.get("recent_photos", {}).get(user_id, []) if time.time() - at < RECENT_PHOTOS_SECONDS]
        count = max(1, min(4, int(args.get("photos") or 1)))
        if not fresh:
            return "No recent photos. Ask the user to send the photo(s) first, then repeat the request."
        error = await start_generation(context.application, user_id, language, "photo", args.get("instruction") or "",
                                       args.get("format") or "portrait", images=fresh[-count:])
        if error == "no_credits":
            return "Not started: no free generations or credits left; tell the user to top up in the Mini App (Generation)."
        if error:
            return f"Not started ({error}). Tell the user briefly that photo editing is unavailable right now."
        return "Started. The edited photo will arrive in the chat by itself in about a minute; tell the user briefly."

    return {"definition": EDIT_TOOL, "run": run, "status": ("Редактирую фото", "Editing the photo"),
            "hint": "You can edit and combine the user's photos with edit_photos (Nano Banana / GPT Image)."}


async def answer_album(updates, context):
    """Several photos sent together (an album) with one caption: one answer that sees all of them."""
    first = updates[0]
    message, user_id = first.effective_message, first.effective_user.id
    caption = next((u.effective_message.caption for u in updates if u.effective_message.caption), "") or ""
    question = caption or ui(first, "Что на этих фото?", "What is in these photos?")
    content = [{"type": "input_text", "text": question}]
    for update in updates[:4]:
        msg = update.effective_message
        image = msg.photo[-1] if msg.photo else msg.document
        remote = await image.get_file()
        raw = await remote.download_as_bytearray()
        content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")})
    async with user_lock(context, user_id):
        try:
            await respond(first, context, message, content, f"[{len(updates)} фото] " + question)
        except Exception as exc:
            context.error = exc
            await on_error(first, context)


# ----- AI secretary (Telegram Business) -----

MEMORY_TOOL = {"type": "function", "name": "remember",
               "description": ("Your long-term memory about a person, shared between the private chat and all groups. "
                               "Save lasting facts the moment you learn them: name, age, city, job or school, family, "
                               "friends, pets, likes and dislikes, goals, plans and dates, how they want to be addressed. "
                               "Also drop facts that changed or that they ask you to forget. Don't save small talk."),
               "parameters": {"type": "object", "properties": {
                   "add": {"type": "array", "items": {"type": "string"},
                           "description": "short facts in the third person, e.g. 'Lives in Kazan', 'Birthday on May 3'"},
                   "forget": {"type": "array", "items": {"type": "string"},
                              "description": "words from facts to delete"}},
                   "required": ["add"], "additionalProperties": False}}


def memory_notes(data, user_id, name="", with_groups=False):
    """What the bot remembers about a person (and, for the private chat, what they said in groups lately)."""
    facts = data["memory"].facts(user_id) if data.get("memory") else []
    notes = (f" What you remember about {name or 'this person'} (use it naturally, don't list it): "
             + "; ".join(facts) + ".") if facts else ""
    if with_groups:
        lines = data["groups"].user_lines(user_id, 40)
        if lines:
            notes += (" What they said in group chats lately (you were there too): "
                      + " | ".join(f"[{title or 'group'}] {line}" for title, line in lines)[:6000])
    return notes


def memory_tool(context, user_id):
    """The remember tool for the private chat, with what the bot already knows about the user as a hint."""
    async def run(args):
        context.bot_data["memory"].remember(user_id, args.get("add") or [], args.get("forget") or [])
        return "Saved."

    return {"definition": MEMORY_TOOL, "run": run, "status": ("Запоминаю", "Remembering"),
            "hint": memory_notes(context.bot_data, user_id, with_groups=True).strip()}


def secretary_tool(context, user_id, ru):
    """The secretary_settings tool for the private chat: the owner turns the secretary on and tells it how to answer."""
    store = context.bot_data["connections"]

    async def run(args):
        current = sec.settings(store, user_id)
        current["enabled"] = bool(args.get("enabled"))
        if (args.get("rules") or "").strip():
            current["rules"] = args["rules"].strip()[:1000]
        if args.get("notify") is not None:
            current["notify"] = bool(args["notify"])
        if current["enabled"] and not current["rules"]:
            current["rules"] = ("Привет! Я сейчас занят и отвечу позже. Это ИИ-помощник." if ru
                                else "Hi! I'm busy right now and will answer later. This is an AI assistant.")
        sec.save_settings(store, user_id, current)
        if current["enabled"]:
            context.bot_data["biz_activity"].owner_seen.pop(user_id, None)
        return "Saved. " + sec.status_text(store, user_id, ru)

    return {"definition": sec.TOOL, "run": run, "status": ("Настраиваю секретаря", "Setting up the secretary"),
            "hint": sec.status_text(store, user_id, ru) + " Change it with secretary_settings when the user asks; tell them "
                    "briefly what is now set and, if the connection is not set up, how to connect."}


async def business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The owner connected or changed the bot in Telegram Business settings."""
    connection = update.business_connection
    store = context.bot_data["connections"]
    value = sec.save_connection(store, connection)
    ru = (connection.user.language_code or "ru").startswith("ru")
    current = sec.settings(store, value["user_id"])
    if not value["enabled"]:
        text = "Секретарь отключён от твоего аккаунта." if ru else "The secretary was disconnected from your account."
    elif not value["can_reply"]:
        text = ("Бот подключён, но ему не разрешено отвечать. Включи «Отвечать на сообщения» в настройках Telegram для бизнеса → "
                "Чат-боты." if ru else "Connected, but the bot may not reply. Allow replying in Telegram Business → Chatbots.")
    else:
        rules = current["rules"] or "—"
        text = (f"✅ Секретарь подключён. Сейчас он {'включён' if current['enabled'] else 'выключен'}, правила: «{rules}».\n\n"
                "Напиши мне, как отвечать, например: «отвечай всем: привет, я занят или сплю, отвечу позже, это ИИ». "
                "Выключить — «выключи секретаря». Как только ты сам кому-то пишешь в личке, я замолкаю во всех чатах на 10 минут." if ru else
                f"✅ The secretary is connected. It is {'on' if current['enabled'] else 'off'}, rules: «{rules}».\n\n"
                "Tell me how to answer, e.g. “reply to everyone: hi, I'm busy, I'll answer later, this is an AI”. "
                "Turn it off with “turn off the secretary”. As soon as you write to anyone yourself, I go quiet in all chats for 10 minutes.")
    try:
        await context.bot.send_message(value["chat_id"], text)
    except TelegramError:
        LOG.warning("Could not tell the owner about the business connection")


async def business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A message in the owner's personal chat: answer it for the owner when the secretary is on and the owner is away."""
    message = update.business_message
    if not message or not message.business_connection_id:
        return
    try:
        await secretary_answer(context, message)
    except Exception as exc:  # never show errors to the person the owner is talking to
        LOG.warning("Secretary failed: %s", type(exc).__name__)


async def secretary_answer(context, message):
    data, store = context.bot_data, context.bot_data["connections"]
    conn_id = message.business_connection_id
    conn = sec.connection(store, conn_id)
    if not conn:
        conn = sec.save_connection(store, await context.bot.get_business_connection(conn_id))
    chat_id, sender, activity = message.chat.id, message.from_user, data["biz_activity"]
    said = describe_message(message) or "…"
    if sender and sender.id == conn["user_id"]:
        activity.owner_wrote(sender.id)  # the owner is here: stay quiet in all chats for a while
        activity.log(chat_id, f"{conn.get('name', 'Owner')} (owner): {said}")
        return
    name = speaker(sender) if sender else "?"
    activity.log(chat_id, f"{name}: {said}")
    current = sec.settings(store, conn["user_id"])
    if (not current["enabled"] or not conn["enabled"] or not conn["can_reply"] or message.chat.type != "private"
            or (sender and sender.is_bot) or not activity.may_answer(conn["user_id"], chat_id)):
        return
    activity.answered_now(chat_id)
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING, business_connection_id=conn_id)
    except TelegramError:
        pass
    response = await data["client"].responses.create(
        model=data["model"], instructions=sec.instructions(conn.get("name", "the owner"), current["rules"]), store=False,
        max_output_tokens=1500,
        input="Chat (oldest first):\n" + "\n".join(activity.lines.get(chat_id, [])) + f"\n\nAnswer {name}'s last message.")
    text = (response.output_text or "").strip()
    if not text:
        return
    await send_signed(context, chat_id, text[:3500], conn_id, message.message_id)
    activity.log(chat_id, f"AI secretary: {text}")
    activity.remember(conn["user_id"], name, said, text)
    if current.get("notify", True):
        try:
            await context.bot.send_message(conn["chat_id"], f"🤖 {name}: «{said[:200]}»\n↳ {text[:500]}")
        except TelegramError:
            pass


async def send_signed(context, chat_id, text, conn_id, reply_id=None):
    """Send the secretary's answer with a signature: the animated logo and the bot's name, so nobody thinks it's the owner."""
    data = context.bot_data
    ru = any("а" <= c.lower() <= "я" for c in text)
    name = context.bot.first_name or "Secret AI"
    logo = data["logo"]
    signature = f"{logo} {name} · {'ИИ-ассистент' if ru else 'AI assistant'}"
    body = f"{text}\n\n"
    start = utf16_len(body)
    italic = MessageEntity(MessageEntity.ITALIC, start + utf16_len(logo) + 1, utf16_len(signature) - utf16_len(logo) - 1)

    async def post(entities):
        marks = [italic] + [MessageEntity(e.type, start, e.length, custom_emoji_id=e.custom_emoji_id) for e in entities or []]
        return await context.bot.send_message(chat_id, body + signature, entities=marks, business_connection_id=conn_id,
                                              reply_parameters=reply_to(reply_id))

    return await with_logo(data, post)


SECRETARY_OFF = re.compile(r"\b(выключ|отключ|стоп|останов|убери|turn off|disable|stop)\w*\b.{0,20}(секретар|автоответ|secretary)"
                           r"|(секретар|автоответ|secretary)\w*.{0,10}\b(выключ|отключ|выкл|стоп|off)", re.I | re.S)
SECRETARY_ON = re.compile(r"\b(включ|вруби|запусти|turn on|enable|start)\w*\b.{0,20}(секретар|автоответ|secretary)"
                          r"|(секретар|автоответ|secretary)\w*.{0,10}\b(включ\w*|вкл\w*|on)\b", re.I | re.S)


async def secretary_switch(update, context, text):
    """"выключи секретаря" / "включи секретаря": switched at once, without the model. True if handled."""
    if len(text) > 120:
        return False
    off, on = SECRETARY_OFF.search(text), SECRETARY_ON.search(text)
    if not off and not on:
        return False
    store, user_id, ru = context.bot_data["connections"], update.effective_user.id, lang(update) == "ru"
    current = sec.settings(store, user_id)
    current["enabled"] = not off
    if current["enabled"] and not current["rules"]:
        current["rules"] = ("Привет! Я сейчас занят и отвечу позже. Это ИИ-помощник." if ru
                            else "Hi! I'm busy right now and will answer later. This is an AI assistant.")
    sec.save_settings(store, user_id, current)
    if current["enabled"]:
        context.bot_data["biz_activity"].owner_seen.pop(user_id, None)  # start answering right away
        text = (f"✅ Секретарь включён. Отвечаю всем по правилам: «{current['rules']}»." if ru
                else f"✅ Secretary on. Answering everyone by the rules: «{current['rules']}».")
    else:
        text = "⏸ Секретарь выключен. Больше никому не отвечаю, пока не скажешь «включи секретаря»." if ru \
            else "⏸ Secretary off. I won't answer anyone until you say “turn on the secretary”."
    await send(update.effective_message, text)
    return True


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/voice: which AnyModel voice answers voice messages; /voice <name or id> picks one, /voice off — the usual voice."""
    if not await authorized(update, context):
        return
    data, message = context.bot_data, update.effective_message
    if data["connections"].get_meta("owner") != update.effective_user.id:
        await send(message, "Голос бота меняет только владелец.")
        return
    base, query = data.get("anymodel_api") or ANYMODEL_API, " ".join(context.args or []).strip()
    if query.lower().startswith("fish"):
        if not data.get("fish_key"):
            await send(message, "Нужен ключ Fish Audio: переменная FISH_API_KEY в Render.")
            return
        voice = fish_voice_id(query[4:].strip())
        if not voice:
            await send(message, "Пришли так: /voice fish <ссылка на голос с fish.audio или его ID>")
            return
        data["connections"].set_meta("fish_voice", voice)
        await send(message, "🎙 Голос Fish Audio включён: " + voice + ("" if voice == "off" else
                   "\nЧтобы не слетал после обновлений, пропиши в Render FISH_VOICE = " + voice))
        return
    if not data.get("anymodel_key"):
        await send(message, "Нужен ключ AnyModel: переменная ANYMODEL_API_KEY в Render.")
        return
    if query.lower() in ("all", "все", "всё"):
        voices = await anymodel_voices(data["http"], data["anymodel_key"], base, every_language=True)
        lines = [f"{model}" + (f" — {label}" if label else "") for model, label in voices]
        await send(message, f"🎙 Все голоса API ({len(voices)}):\n" + "\n".join(lines) if voices
                   else "AnyModel не отдал список голосов.")
        return
    voices = await anymodel_voices(data["http"], data["anymodel_key"], base)
    if query:
        found = await anymodel_voice_id(data["http"], data["anymodel_key"], query, base) if query.lower() != "off" else ""
        if query.lower() != "off" and not found:
            await send(message, f"Голос «{query}» через API не нашёлся. Напиши /voice — покажу, какие есть.")
            return
        data["connections"].set_meta("anymodel_voice", query)
        data["anymodel_voice_id"] = found
        await send(message, f"🎙 Теперь голосовые отвечаю голосом: {found}" if found else "🎙 Вернул обычный голос.")
        return
    current = data.get("anymodel_voice_id")
    head = f"🎙 Сейчас: {current or 'обычный голос (OpenAI)'}\n\n"
    if not voices:
        await send(message, head + "AnyModel не отдал список голосов — проверь ключ и баланс.")
        return
    lines = [f"• {model}" + (f" — {label}" if label else "") for model, label in voices[:60]]
    await send(message, head + f"Голоса, доступные через API ({len(voices)}):\n" + "\n".join(lines)
               + "\n\nВыбрать: /voice <имя или id>, например /voice " + voices[0][0] + "\nВсе голоса всех языков: /voice all"
               + "\nОбычный голос: /voice off")


async def secretary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/secretary: status and how to connect."""
    if not await authorized(update, context):
        return
    ru = lang(update) == "ru"
    store, user_id = context.bot_data["connections"], update.effective_user.id
    conn_id = store.get_meta(f"biz:{user_id}")
    conn = sec.connection(store, conn_id) if conn_id else None
    current = sec.settings(store, user_id)
    ready = bool(conn and conn["enabled"] and conn["can_reply"])
    if ru:
        text = (f"🤖 Секретарь: {'включён' if current['enabled'] else 'выключен'}\n"
                f"Подключение к аккаунту: {'есть' if ready else 'нет'}\n"
                f"Правила: «{current['rules'] or '—'}»\n"
                f"Уведомления: {'вкл' if current.get('notify', True) else 'выкл'}\n\n"
                + ("" if ready else sec.SETUP_RU + " Нужен Telegram Premium.\n\n") +
                "Просто напиши мне, как отвечать: «отвечай всем: привет, я занят, отвечу позже, это ИИ» или «выключи секретаря».")
    else:
        text = (f"🤖 Secretary: {'on' if current['enabled'] else 'off'}\nAccount connection: {'yes' if ready else 'no'}\n"
                f"Rules: «{current['rules'] or '—'}»\nNotifications: {'on' if current.get('notify', True) else 'off'}\n\n"
                + ("" if ready else sec.SETUP_EN + " Telegram Premium is required.\n\n") +
                "Just tell me how to answer, e.g. “reply to everyone: hi, I'm busy, I'll answer later” or “turn off the secretary”.")
    await send(update.effective_message, text)


MEDIA_ERRORS = {
    "no_key": ("Генерация пока не настроена: владелец бота ещё не добавил ключ.", "Generation isn't set up yet: the bot owner hasn't added a key."),
    "bad_key": ("Ключ fal.ai не работает или на нём закончился баланс — владельцу бота нужно проверить fal.ai.",
                "The fal.ai key doesn't work or its balance is empty — the bot owner should check fal.ai."),
    "model": ("Эта модель генерации недоступна — владельцу бота нужно сменить её в настройках Mini App.",
              "This generation model is unavailable — the bot owner should change it in the Mini App settings."),
    "timeout": ("Генерация заняла слишком много времени. Попробуйте ещё раз.", "Generation took too long. Please try again."),
    "blocked": ("Запрос отклонён фильтром безопасности. Попробуйте описать иначе.", "The request was blocked by the safety filter. Try describing it differently."),
    "failed": ("Не получилось сгенерировать. Попробуйте ещё раз.", "Generation failed. Please try again."),
    "no_video_key": ("Видео пока не настроено: владелец бота ещё не добавил ключ видео-сервиса (Higgsfield).",
                     "Video isn't set up yet: the bot owner hasn't added the video service key (Higgsfield)."),
    "bad_video_key": ("Ключ видео-сервиса не работает — владельцу бота нужно проверить его в настройках Mini App.",
                      "The video service key doesn't work — the bot owner should check it in the Mini App settings."),
    "video_billing": ("На балансе видео-сервиса закончились деньги — владельцу бота нужно пополнить его.",
                      "The video service balance is empty — the bot owner should top it up."),
    "busy": ("Видео-сервис сейчас перегружен. Попробуйте через пару минут.", "The video service is busy. Try again in a couple of minutes."),
}


SPACES_REST = 3 * 3600  # after the free GPU quota runs out, use pictures for a while before asking again


def kling_env_key():
    """KLING_API_KEY (the console's API Key), or the legacy KLING_ACCESS_KEY + KLING_SECRET_KEY pair."""
    key = os.environ.get("KLING_API_KEY", "").strip()
    if key:
        return "bearer:" + key.removeprefix("Bearer ").strip()
    access, secret = os.environ.get("KLING_ACCESS_KEY", "").strip(), os.environ.get("KLING_SECRET_KEY", "").strip()
    return f"{access}:{secret}" if access and secret else ""


def gen_settings(bot_data):
    """fal key (Mini App, or FAL_KEY env) and the photo/video models."""
    store = bot_data["connections"]
    key = store.get_meta("fal_key", secret=True) or bot_data.get("fal_env_key") or ""
    models = store.get_meta("fal_models") or {}
    if models.get("video") in OLD_VIDEO_DEFAULTS:
        models["video"] = ""  # an old default saved from the form: use the current one (free)
    if models.get("photo") in OLD_PHOTO_DEFAULTS:
        models["photo"] = ""
    defaults = dict(DEFAULT_MODELS, video=os.environ.get("VIDEO_MODEL", "").strip()
                    or (ANYMODEL_PREFIX + bot_data["anymodel_video"] if bot_data.get("anymodel_key")
                        else KLING_DIRECT if bot_data.get("kling_env_key") else KLING if key else "free"))
    return key, {kind: models.get(kind) or default for kind, default in defaults.items()}


def video_key(bot_data, model):
    """Key for the video model: none needed for free, Higgsfield, Google AI Studio for Veo, or fal."""
    provider = video_provider(model)
    if provider == "free":
        return "free"
    if provider == "veo":
        return bot_data["connections"].get_meta("gemini_key", secret=True) or bot_data.get("gemini_env_key") or ""
    if provider == "kling":
        return bot_data.get("kling_env_key") or ""
    if provider == "anymodel":
        return bot_data.get("anymodel_key") or ""
    if provider == "higgsfield":
        return bot_data["connections"].get_meta("hf_key", secret=True) or bot_data.get("hf_env_key") or ""
    return gen_settings(bot_data)[0]


def gen_state(bot_data, user_id):
    key, models = gen_settings(bot_data)
    free, credits = bot_data["memory"].gen_info(user_id)
    owner = bot_data["connections"].get_meta("owner") == user_id
    state = {"ready": bool(key) or models["photo"] in ("free", OPENAI_IMAGE), "video_ready": bool(video_key(bot_data, models["video"])), "video_on": models["video"] != "free" or bool(bot_data.get("hf_token")), "video_free": models["video"] == "free", "owner": owner, "unlimited": owner, "free_left": free, "free_per_day": FREE_PHOTOS_PER_DAY,
             "credits": credits, "cost": COST, "video_prices": VIDEO_PRICES, "packs": [{"id": pid, "credits": c, "stars": s} for pid, (c, s) in PACKS.items()]}
    if owner:
        state["models"] = models
        state["gemini"] = bool(bot_data["connections"].get_meta("gemini_key", secret=True))
        state["hf"] = bool(bot_data["connections"].get_meta("hf_key", secret=True))
    return state


async def start_generation(app, user_id, language, kind, prompt, fmt, seconds=5, voice=True, images=None, chat_id=None,
                           reply_id=None):
    """Check the key and quota, take the generation and run it in the background. Returns None or an error code.
    The user pays; the result goes to chat_id (a group) in reply to reply_id, or to the user's private chat."""
    data = app.bot_data
    prompt = (prompt or "").strip()[:MAX_PROMPT]
    if kind not in COST or not prompt or (kind == "video" and seconds not in VIDEO_PRICES):
        return "bad_request"
    key, models = gen_settings(data)
    if kind == "video":
        key = video_key(data, models["video"])
        if not key:
            return "no_key" if video_provider(models["video"]) == "fal" else "no_video_key"
    elif models["photo"] in ("free", OPENAI_IMAGE) or images:
        key = models["photo"]
    elif not key:
        return "no_key"
    cost = price(kind, seconds)
    in_group = bool(chat_id) and chat_id != user_id
    free_here = in_group and kind == "photo" and os.environ.get("GROUP_PHOTOS_FREE", "1").strip() != "0"
    how = ("owner" if data["connections"].get_meta("owner") == user_id or free_here
           else data["memory"].gen_charge(user_id, kind, cost))
    if not how:
        return "no_credits"
    job = {"user_id": user_id, "language": language, "kind": kind, "prompt": prompt, "fmt": fmt if fmt in FORMATS else "portrait",
           "seconds": seconds, "voice": bool(voice), "key": key, "model": models[kind], "how": how, "cost": cost,
           "images": list(images or [])[:4], "chat_id": chat_id or user_id, "reply_id": reply_id}
    task = asyncio.create_task(generation_job(app, job))
    jobs = data.setdefault("gen_tasks", set())
    jobs.add(task)
    task.add_done_callback(jobs.discard)
    return None


def waiting_text(job):
    ru = job["language"] == "ru"
    if job["kind"] == "photo" and job.get("images"):
        return "🪄 Редактирую фото… обычно это 20–60 секунд" if ru else "🪄 Editing the photo… usually 20–60 seconds"
    if job["kind"] == "photo":
        return "🎨 Рисую… обычно это 10–30 секунд" if ru else "🎨 Drawing… usually 10–30 seconds"
    seconds = job["seconds"]
    length = (f"{seconds} с" if seconds < 60 else "~1 мин") if ru else (f"{seconds} s" if seconds < 60 else "~1 min")
    if seconds <= SCENE_SECONDS:
        return f"🎬 Снимаю видео ({length})… это займёт 1–3 минуты" if ru else f"🎬 Making the video ({length})… 1–3 minutes"
    scenes = seconds // SCENE_SECONDS
    word = "сцены" if 2 <= scenes % 10 <= 4 and not 12 <= scenes % 100 <= 14 else "сцен"
    return (f"🎬 Снимаю видео ({length}): {scenes} {word} параллельно, потом монтаж. Это займёт 3–10 минут."
            if ru else f"🎬 Making the video ({length}): {scenes} scenes in parallel, then editing. 3–10 minutes.")


async def plan_scenes(data, job):
    """Split the idea into scenes: an English visual prompt and a voice-over line in the user's language for each."""
    seconds, ru = job["seconds"], job["language"] == "ru"
    count = max(1, seconds // SCENE_SECONDS)
    per_scene = FAL_SCENE_SECONDS
    words = max(6, int(per_scene * 1.8))
    instructions = (
        f"You plan a {seconds}-second AI video made of {count} consecutive scene(s), {per_scene} seconds each. "
        "For every scene write 'visual': a self-contained English prompt for a text-to-video model (repeat the main "
        "characters' look, setting and style in every scene so they stay consistent; camera movement; no on-screen text; "
        "nobody speaks). Write 'sound': a short English description of the sound effects heard in that scene "
        "(footsteps, wind, birds, rain, water, traffic, crowd murmur, doors...), no speech and no music. " +
        (f"Also write 'voice': a natural voice-over line in {'Russian' if ru else 'the language of the request'} for that "
         f"scene, at most {words} words, together telling the story. " if job["voice"] else "Set 'voice' to an empty string. ") +
        'Return JSON: {"scenes": [{"visual": "...", "sound": "...", "voice": "..."}]}.')
    try:
        response = await data["client"].responses.create(
            model=data["model"], instructions=instructions, input=job["prompt"], store=False, max_output_tokens=4000,
            text={"format": {"type": "json_object"}})
        scenes = json.loads(response.output_text)["scenes"][:count]
        scenes = [{"visual": str(s["visual"]).strip(), "sound": str(s.get("sound") or "").strip(),
                   "voice": str(s.get("voice") or "").strip()} for s in scenes if s.get("visual")]
    except Exception as exc:  # planning is a nicety: fall back to the raw idea
        LOG.warning("Scene planning failed: %s", type(exc).__name__)
        scenes = []
    if len(scenes) < count:
        scenes += [{"visual": f"{job['prompt']} (part {i + 1} of {count})", "sound": "", "voice": ""} for i in range(len(scenes), count)]
    return scenes


async def english_prompt(data, prompt):
    """Image models understand English best: translate and enrich a prompt written in another language."""
    if prompt.isascii():
        return prompt
    try:
        response = await data["client"].responses.create(
            model=data["model"], store=False, max_output_tokens=600, input=prompt,
            instructions=("Rewrite this image request as one detailed English prompt for a text-to-image model: subject, "
                          "style, lighting, composition. Keep every detail the user asked for; quote any text that must "
                          "appear in the image as is. Output only the prompt."))
        return (response.output_text or "").strip() or prompt
    except Exception as exc:  # the original prompt still works, just less precisely
        LOG.warning("Prompt translation failed: %s", type(exc).__name__)
        return prompt


async def plan_veo(data, job, count):
    """English prompts for consecutive 8-second Veo scenes; spoken lines in the user's language, quoted."""
    language = "Russian" if job["language"] == "ru" else "the language of the request"
    speech = (f"If people appear, they speak {language} on camera with natural lip sync: write their exact lines in "
              f"{language} in quotes (for example: The woman looks at the camera and says in Russian: \"...\"), at most "
              f"about 16 words per 8-second scene, and add 'The dialogue is in {language}.' A short narrator line in "
              f"{language} is fine when nobody is on screen." if job["voice"]
              else "Nobody speaks: no dialogue, no voice-over; music and ambient sound only.")
    instructions = (
        f"You write prompts for a text-to-video model with native sound (Veo / Seedance). The video is {count} consecutive 8-second scene(s) of one story. Each "
        "prompt is in English and self-contained: subject (repeat the characters' exact look every time), action, setting, "
        "camera, lighting, style, sound. No on-screen text. " + speech +
        ' Return JSON: {"prompts": ["...", ...]} with exactly ' + str(count) + " prompts.")
    try:
        response = await data["client"].responses.create(
            model=data["model"], instructions=instructions, input=job["prompt"], store=False, max_output_tokens=4000,
            text={"format": {"type": "json_object"}})
        prompts = [str(p).strip() for p in json.loads(response.output_text)["prompts"] if str(p).strip()][:count]
    except Exception as exc:  # planning is a nicety: fall back to the raw idea
        LOG.warning("Veo planning failed: %s", type(exc).__name__)
        prompts = []
    note = f" Any dialogue is spoken in {language}." if job["voice"] else " No dialogue."
    prompts += [job["prompt"] + note + (f" Part {i + 1} of {count}." if count > 1 else "") for i in range(len(prompts), count)]
    return prompts


async def make_veo_video(data, job):
    """Veo or Higgsfield scenes in parallel with their own sound and speech, joined into one MP4."""
    count = max(1, job["seconds"] // SCENE_SECONDS)
    prompts = await plan_veo(data, job, count)
    if video_provider(job["model"]) == "veo":
        calls = [veo_generate(data["http"], job["key"], job["model"], prompt, job["fmt"]) for prompt in prompts]
    else:
        calls = [hf_generate(data["http"], job["key"], job["model"], prompt, job["fmt"]) for prompt in prompts]
    clips = await asyncio.gather(*calls)
    return await montage(list(clips), job["fmt"], keep_sound=True)


async def voice_over(data, job, scenes):
    """The Russian (or the user's language) narrator for the scenes, MP3 bytes; None when off or failed."""
    narration = " ".join(scene["voice"] for scene in scenes if scene["voice"])
    if not (job["voice"] and narration and data.get("tts_model")):
        return None
    try:
        speech = await data["client"].audio.speech.create(model=data["tts_model"], voice=data["tts_voice"],
                                                          input=narration[:4000], response_format="mp3")
        return speech.content
    except Exception as exc:  # a video without voice is still a result
        LOG.warning("Voice-over failed: %s", type(exc).__name__)
        return None


async def make_kling_video(data, job):
    """Kling (its own API) or AnyModel (Grok Imagine...) scenes in parallel with their sound effects, plus the
    voice-over, in one MP4."""
    scenes = await plan_scenes(data, job)
    quiet = " No dialogue, nobody talks, no lip movement."
    if video_provider(job["model"]) == "anymodel":
        clips = await asyncio.gather(*[
            anymodel_generate(data["http"], job["key"], job["model"], scene["visual"].rstrip(". ") + "." + quiet,
                              job["fmt"], ANYMODEL_SECONDS, data.get("anymodel_api") or ANYMODEL_API)
            for scene in scenes])
    else:
        clips = await asyncio.gather(*[
            kling_generate(data["http"], job["key"], job["model"], scene["visual"].rstrip(". ") + "." + quiet, job["fmt"],
                           KLING_SECONDS, data.get("kling_api") or KLING_API)
            for scene in scenes])
    voice = await voice_over(data, job, scenes)
    return await montage(list(clips), job["fmt"], voice, keep_sound=True, sound_volume=0.5 if voice else 1.0)


async def make_free_video(data, job):
    """Free video: real AI clips from free Hugging Face spaces (Wan 2.2, LTX) while the daily free GPU quota lasts,
    otherwise a picture per scene (Cloudflare/Pollinations) brought to life; plus the voice-over in the user's language."""
    scenes = await plan_scenes(data, job)
    seconds = max(5, job["seconds"] // max(1, len(scenes)))
    clips = []
    for index, scene in enumerate(scenes):  # one by one: the free services take about one request at a time
        if data.get("hf_token") and time.monotonic() >= data.get("spaces_rest_until", float("-inf")):
            try:
                clip = await space_video(scene["visual"].rstrip(". ") + ". Cinematic, smooth motion, nobody talks.",
                                         job["fmt"], data["hf_token"], data.get("hf_spaces") or HF_SPACES)
                clips.append(await fit_clip(clip, seconds, job["fmt"]))
                continue
            except MediaError as exc:
                if exc.code == "video_billing":  # today's free GPU minutes are spent: pictures until they come back
                    data["spaces_rest_until"] = time.monotonic() + SPACES_REST
                LOG.warning("Free AI clip failed (%s); using a living picture", exc.code)
        image = await free_image(data["http"], scene["visual"] + ", cinematic still, highly detailed", job["fmt"],
                                 data.get("cloudflare"))
        clips.append(await living_picture(image, seconds, job["fmt"], index))
    voice = await voice_over(data, job, scenes)
    return await join_with_voice(clips, seconds * len(clips), voice)


async def make_video(data, job):
    """Generate the scenes in parallel, voice them and edit them into one MP4. Returns the video bytes."""
    scenes = await plan_scenes(data, job)
    quiet = " No dialogue, nobody talks, no lip movement."
    urls = await asyncio.gather(*[
        fal_generate(data["http"], job["key"], job["model"], "video", scene["visual"].rstrip(". ") + "." + quiet, job["fmt"],
                     FAL_SCENE_SECONDS)
        for scene in scenes])
    urls = await asyncio.gather(*[add_sound(data, job, url, scene) for url, scene in zip(urls, scenes)])
    clips = []
    for url in urls:
        response = await data["http"].get(url, timeout=180)
        response.raise_for_status()
        clips.append(response.content)
    voice = await voice_over(data, job, scenes)
    return await montage(clips, job["fmt"], voice, keep_sound=True, sound_volume=0.5 if voice else 1.0)


async def add_sound(data, job, url, scene):
    """Sound effects for a silent clip (MMAudio); on failure the clip stays silent."""
    prompt = scene["sound"] or "natural ambient sound effects matching the scene"
    try:
        return await fal_sound(data["http"], job["key"], url, prompt + ". No speech, no music.", FAL_SCENE_SECONDS)
    except Exception as exc:  # a video without effects is still a result
        LOG.warning("Sound effects failed: %s", type(exc).__name__)
        return url


async def paid_image(data, prompt, fmt, images=None):
    """Nano Banana on Abacus when it's set up, otherwise GPT Image on the OpenAI key."""
    if data.get("abacus") and data.get("abacus_image"):
        try:
            return await abacus_image(data["abacus"], data["http"], data["abacus_image"], prompt, fmt, images)
        except MediaError as exc:
            if exc.code == "blocked":
                raise
            LOG.warning("Nano Banana failed: %s; trying GPT Image", exc.code)
        except Exception as exc:  # RouteLLM down or the model id is different: OpenAI still can
            LOG.warning("Nano Banana failed: %s; trying GPT Image", type(exc).__name__)
    try:
        return await openai_image(data["client"], data.get("image_model") or "gpt-image-1", prompt, fmt, images)
    except BadRequestError as exc:
        raise MediaError("blocked" if "safety" in str(exc).lower() or "moderation" in str(exc).lower() else "failed",
                         type(exc).__name__) from None


async def photo_bytes(bot, photos):
    """Photos for an edit: bytes as they are, Telegram file ids downloaded."""
    result = []
    for photo in photos or []:
        if isinstance(photo, (bytes, bytearray)):
            result.append(bytes(photo))
        else:
            remote = await bot.get_file(photo)
            result.append(bytes(await remote.download_as_bytearray()))
    return result


async def generation_job(app, job):
    bot, data = app.bot, app.bot_data
    user_id, ru, kind = job["user_id"], job["language"] == "ru", job["kind"]
    chat, answer = job.get("chat_id") or user_id, {"reply_parameters": reply_to(job.get("reply_id"))}
    status = None
    try:
        status = await bot.send_message(chat, waiting_text(job), **answer)
    except TelegramError:
        pass
    try:
        # In groups no caption: the prompt is the model's own (often English) description, not something to show.
        caption = None if job.get("chat_id") not in (None, user_id) else job["prompt"][:900]
        if kind == "photo" and (job.get("images") or job["model"] == OPENAI_IMAGE):
            images = await photo_bytes(bot, job.get("images"))
            try:
                image = await paid_image(data, job["prompt"], job["fmt"], images)
            except MediaError:
                if images:
                    raise  # an edit can't fall back to drawing from scratch
                image = await free_image(data["http"], await english_prompt(data, job["prompt"]), job["fmt"], data.get("cloudflare"))
            await bot.send_photo(chat, InputFile(image, filename="secret-ai.jpg"), caption=caption, **answer)
        elif kind == "photo":
            prompt = await english_prompt(data, job["prompt"])
            if job["model"] == "free":
                image = await free_image(data["http"], prompt, job["fmt"], data.get("cloudflare"))
                await bot.send_photo(chat, InputFile(image, filename="secret-ai.jpg"), caption=caption, **answer)
            else:
                url = await fal_generate(data["http"], job["key"], job["model"], "photo", prompt, job["fmt"])
                await send_media(bot, data["http"], chat, "photo", url, caption)
        else:
            provider = video_provider(job["model"])
            video = await (make_free_video(data, job) if provider == "free" else make_video(data, job) if provider == "fal"
                           else make_kling_video(data, job) if provider in ("kling", "anymodel") else make_veo_video(data, job))
            await bot.send_video(chat, InputFile(video, filename="secret-ai.mp4"), caption=caption, supports_streaming=True,
                                 write_timeout=180, read_timeout=180, **answer)
    except Exception as exc:  # any failure: give the generation back and explain
        if job["how"] != "owner":
            data["memory"].gen_refund(user_id, job["how"], job["cost"])
        code = exc.code if isinstance(exc, MediaError) else "failed"
        # Only the error code: fal's details may echo the prompt, which is user content.
        LOG.warning("Generation failed: %s %s", code, type(exc).__name__)
        text = MEDIA_ERRORS[code][0 if ru else 1]
        if job["how"] in ("free", "credits"):
            text += " " + ("Генерация не списана." if ru else "Nothing was charged.")
        try:
            await bot.send_message(chat, "⚠️ " + text, **answer)
        except TelegramError:
            pass
    finally:
        if status:
            try:
                await status.delete()
            except TelegramError:
                pass


async def send_media(bot, http, chat_id, kind, url, caption):
    """Send by URL; if Telegram can't fetch it, download and upload the file."""
    send = bot.send_photo if kind == "photo" else bot.send_video
    field = "photo" if kind == "photo" else "video"
    try:
        await send(chat_id, **{field: url}, caption=caption)
        return
    except BadRequest:
        pass
    response = await http.get(url, timeout=120)
    response.raise_for_status()
    name = "secret-ai.png" if kind == "photo" else "secret-ai.mp4"
    await send(chat_id, **{field: InputFile(response.content, filename=name)}, caption=caption)


async def voice_reply(update, context, target, answer):
    try:
        await asyncio.wait_for(speak(context, target, answer), VOICE_TIMEOUT)
    except Exception as exc:  # the text answer is already there; a failed voice must not look like a failed reply
        # Error texts from OpenAI/Telegram carry no user content and tell the owner what to fix.
        LOG.warning("Voice reply failed: %s %s", type(exc).__name__, getattr(exc, "message", "") or str(exc)[:200])
        try:
            await send(target, ui(update, "🔇 Не получилось записать голосовой ответ — ответил текстом.",
                                  "🔇 Couldn't record a voice reply — answered in text."))
        except TelegramError:
            pass


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_chat.type in GROUP_TYPES:
        await group_chat(update, context)
        return
    if not await authorized(update, context):
        return
    message = update.effective_message
    if message.text and await secretary_switch(update, context, message.text):
        return
    remember_photo(context.bot_data, update.effective_user.id, message)
    if message.media_group_id and (message.photo or message.document):
        # An album arrives as separate messages: collect them for a moment and answer once.
        key = (update.effective_user.id, message.media_group_id)
        album = context.bot_data.setdefault("albums", {}).setdefault(key, [])
        album.append(update)
        if len(album) == 1:
            async def later():
                await asyncio.sleep(1.5)
                await answer_album(context.bot_data["albums"].pop(key), context)
            task = asyncio.create_task(later())
            context.bot_data.setdefault("album_tasks", set()).add(task)
            task.add_done_callback(context.bot_data["album_tasks"].discard)
        return
    lock = user_lock(context, update.effective_user.id)
    if lock.locked():
        await send(message, ui(update, "⏳ Ещё отвечаю на предыдущее сообщение — это обработаю следом.",
                               "⏳ Still answering your previous message — this one is next."))
    async with lock:
        parsed = await read_question(update, context)
        if parsed:
            _, content, saved_question = parsed
            # Voice in, voice out: a voice message or video note gets a spoken answer too.
            voice = bool(message.voice or message.video_note)
            await respond(update, context, message, content, saved_question, voice=voice)


async def speak(context, target, text):
    """Reply to target with text read aloud (OpenAI TTS)."""
    text = (text or "").replace(CURSOR, "").strip()
    if len(text) > VOICE_MAX_CHARS:  # long answers take minutes to voice; read the beginning, the full text is in the chat
        cut = text.rfind(". ", 0, VOICE_MAX_CHARS)
        text = text[:cut + 1 if cut > VOICE_MAX_CHARS // 2 else VOICE_MAX_CHARS]
    if not text:
        return
    await target.get_bot().send_chat_action(chat_id=target.chat_id, action=ChatAction.RECORD_VOICE)
    audio = await speech_audio(context.bot_data, text)
    try:
        await target.reply_voice(voice=audio, filename="secret-ai.ogg")
    except BadRequest as exc:
        # Premium users can forbid voice messages (VOICE_MESSAGES_FORBIDDEN); an audio file is still allowed.
        LOG.warning("Voice message refused by Telegram (%s); sending an audio file", exc.message)
        await target.reply_audio(audio=audio, filename="Secret AI.ogg", title="Secret AI")


async def speech_audio(data, text):
    """OGG Opus speech: a Fish Audio voice (FISH_API_KEY + FISH_VOICE or /voice fish …), else the AnyModel voice,
    else OpenAI TTS; each one falls back to the next."""
    fish = data["connections"].get_meta("fish_voice") or data.get("fish_voice") or ""
    if fish and fish != "off" and data.get("fish_key"):
        try:
            audio = await fish_speech(data["http"], data["fish_key"], fish, text, data.get("fish_model", ""))
            LOG.info("Voice reply: Fish Audio voice %s", fish)
            return audio
        except Exception as exc:
            LOG.warning("Fish Audio voice failed: %s %s", type(exc).__name__, str(exc)[:200])
    elif fish and fish != "off" and data.get("anymodel_key") and data.get("fish_via_anymodel", True):
        # No Fish Audio key: ask AnyModel's free Fish route for the same voice.
        try:
            audio = await anymodel_speech(data["http"], data["anymodel_key"], ANYMODEL_FISH, text,
                                          data.get("anymodel_api") or ANYMODEL_API, reference_id=fish)
            LOG.info("Voice reply: AnyModel %s asked for Fish voice %s", ANYMODEL_FISH, fish)
            return audio
        except Exception as exc:
            LOG.warning("Fish voice through AnyModel failed: %s %s", type(exc).__name__, str(exc)[:200])
    if data.get("anymodel_key") and data.get("anymodel_voice"):
        try:
            base = data.get("anymodel_api") or ANYMODEL_API
            if "anymodel_voice_id" not in data:  # looked up once per run; /voice changes it
                wanted = data["connections"].get_meta("anymodel_voice") or data["anymodel_voice"]
                data["anymodel_voice_id"] = await anymodel_voice_id(data["http"], data["anymodel_key"], wanted, base)
                LOG.info("AnyModel voice: %s", data["anymodel_voice_id"] or "not found, using the usual voice")
            if data["anymodel_voice_id"]:
                audio = await anymodel_speech(data["http"], data["anymodel_key"], data["anymodel_voice_id"], text, base)
                LOG.info("Voice reply: AnyModel voice %s", data["anymodel_voice_id"])
                return audio
        except Exception as exc:  # the usual voice still answers
            LOG.warning("AnyModel voice failed: %s %s", type(exc).__name__, str(exc)[:200])
    audio = await data["client"].audio.speech.create(model=data["tts_model"], voice=data["tts_voice"], input=text,
                                                     response_format="opus")
    LOG.info("Voice reply: OpenAI voice %s", data["tts_voice"])
    return audio.content


async def confirm_deletion(update, context, query, pid, change):
    """Second confirmation pressed: delete the repositories and report what happened."""
    if change.get("applying"):
        await query.answer()
        return
    change["applying"] = True
    await query.answer(ui(update, "Удаляю…", "Deleting…"))
    try:
        await context.bot_data["github_apps"].fresh_token(context.bot_data["http"], update.effective_user.id)
        deleted, failed = await delete_repos(context.bot_data["http"], context.bot_data["connections"], change)
    except (GitHubError, httpx.HTTPError) as exc:
        change["applying"] = False
        detail = str(exc) if isinstance(exc, GitHubError) else type(exc).__name__
        await send(query.message, ui(update, f"⚠️ Не удалось удалить: {detail}", f"⚠️ Couldn't delete: {detail}"))
        return
    context.bot_data["pending"].pop(pid, None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    lines = [ui(update, f"🗑 Удалено: {len(deleted)}", f"🗑 Deleted: {len(deleted)}")] + [f"• {repo}" for repo in deleted]
    if failed:
        lines.append(ui(update, f"\n⚠️ Не удалось: {len(failed)}", f"\n⚠️ Failed: {len(failed)}"))
        lines += [f"• {repo}: {reason}" for repo, reason in failed]
        if any("403" in reason for _, reason in failed):
            lines.append(ui(
                update,
                "\nУ подключения GitHub нет права удалять репозитории. Владелец бота: GitHub → Settings → Developer settings → "
                "GitHub Apps → приложение бота → Permissions → Administration: Read and write, сохраните и примите новые права. "
                "При входе по токену нужен токен с правом Administration — Read and write.",
                "\nThe GitHub connection may not delete repositories. Bot owner: GitHub → Settings → Developer settings → "
                "GitHub Apps → the bot's app → Permissions → Administration: Read and write, save and accept the new permissions. "
                "With a token, it needs Administration — Read and write."))
    await send(query.message, "\n".join(lines))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if (query.data or "").startswith("g:"):  # the group quiz: anyone in the group may press
        user = update.effective_user
        await quiz(context).on_button(query, user, speaker(user), context.bot)
        return
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

    if data.startswith("wipe:"):
        if context.bot_data["connections"].get_meta("owner") != update.effective_user.id:
            await query.answer()
            return
        if data == "wipe:yes":
            wipe_all(context.bot_data)
            await query.answer("Готово")
            await query.edit_message_text("🧹 Вся память бота стёрта. Начинаем с нуля.")
        else:
            await query.answer()
            await query.edit_message_text("Отменено, память на месте.")
        return
    if data == "reset":
        async with user_lock(context, user_id):
            memory.reset(user_id)
        await query.answer(ui(update, "Память очищена", "Memory cleared"))
        await send(query.message, ui(update, "🧹 Память очищена. Начнём новый диалог!", "🧹 Memory cleared. Let's start a new conversation!"))
        return

    if data.startswith(("apply:", "cancel:", "del1:", "del2:")):
        action, pid = data.split(":", 1)
        change = context.bot_data["pending"].get(pid)
        if not change or change["user_id"] != user_id:
            await query.answer(ui(update, "Изменение устарело — попросите ещё раз", "This change expired — please ask again"), show_alert=True)
            return
        deletion = change.get("kind") == "delete_repos"
        if action == "cancel":
            context.bot_data["pending"].pop(pid, None)
            await query.answer(ui(update, "Отменено", "Cancelled"))
            await query.edit_message_reply_markup(reply_markup=None)
            if deletion:
                await send(query.message, ui(update, "❌ Удаление отменено, репозитории не тронуты.", "❌ Deletion cancelled; nothing was deleted."))
            else:
                await send(query.message, ui(update, "❌ Изменения отменены, репозиторий не тронут.", "❌ Changes cancelled; the repository is untouched."))
            return
        if action == "del1" and deletion:
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(ui(update, "⚠️ Да, удалить навсегда", "⚠️ Yes, delete forever"), callback_data=f"del2:{pid}"),
                InlineKeyboardButton(ui(update, "❌ Отмена", "❌ Cancel"), callback_data=f"cancel:{pid}")]]))
            return
        if action == "del2" and deletion:
            await confirm_deletion(update, context, query, pid, change)
            return
        if deletion or action != "apply":
            await query.answer()
            return
        if change.get("applying"):
            await query.answer()
            return
        change["applying"] = True
        await query.answer(ui(update, "Применяю…", "Applying…"))
        try:
            await context.bot_data["github_apps"].fresh_token(context.bot_data["http"], user_id)
            commit_url, pull_url = await apply_change(context.bot_data["http"], context.bot_data["connections"], change)
        except (GitHubError, httpx.HTTPError) as exc:
            change["applying"] = False
            detail = str(exc) if isinstance(exc, GitHubError) else type(exc).__name__
            await send(query.message, ui(update, f"⚠️ Не удалось применить: {detail}", f"⚠️ Couldn't apply: {detail}"))
            return
        context.bot_data["pending"].pop(pid, None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        text = ui(update, f"✅ Готово! Коммит в {change['repo']} → {change['branch']}\n{commit_url}",
                  f"✅ Done! Commit to {change['repo']} → {change['branch']}\n{commit_url}")
        if pull_url:
            text += f"\n🔀 Pull request: {pull_url}"
        await send(query.message, text)
        return

    if data == "tts":
        await query.answer(ui(update, "Записываю голосовое…", "Recording a voice message…"))
        await speak(context, query.message, query.message.text)  # buttons under older answers
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


def parse_payload(payload):
    """'gen:<pack>:<user id>' -> (pack, user id) or None."""
    try:
        kind, pack, user_id = (payload or "").split(":")
        return (pack, int(user_id)) if kind == "gen" and pack in PACKS else None
    except ValueError:
        return None


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    parsed = parse_payload(query.invoice_payload)
    ok = bool(parsed) and parsed[1] == query.from_user.id and query.currency == "XTR" and query.total_amount == PACKS[parsed[0]][1]
    await query.answer(ok=ok, error_message=None if ok else "This invoice is no longer valid.")


async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.effective_message.successful_payment
    parsed = parse_payload(payment.invoice_payload)
    if not parsed or parsed[1] != update.effective_user.id:
        LOG.warning("Payment with an unknown payload")
        return
    credits = PACKS[parsed[0]][0]
    if context.bot_data["memory"].gen_paid(payment.telegram_payment_charge_id, parsed[1], credits, payment.total_amount):
        await send(update.effective_message, ui(update, f"✅ Готово! +{credits} генераций. Открывайте «Генерация» в Secret AI.",
                                                f"✅ Done! +{credits} generations. Open “Generation” in Secret AI."))


async def unsupported(update, context):
    if update.effective_chat and update.effective_chat.type in GROUP_TYPES:
        if update.effective_message and update.effective_user:
            await group_chat(update, context)  # stickers, videos, gifs: remembered as context, sometimes commented
        return
    if await authorized(update, context):
        await send(update.effective_message, ui(update, "Отправьте текст, голосовое, аудио или фото. Команды: /help", "Send text, voice, audio or a photo. Commands: /help"))


async def on_error(update, context):
    error = context.error
    # Never log exception bodies: they can contain tokens, URLs or user content.
    LOG.error("Handler error: %s", type(error).__name__)
    if not isinstance(update, Update) or not update.effective_message or not update.effective_user or update.business_message:
        return
    try:
        await send(update.effective_message, ui(update, *error_text(error)))
    except TelegramError:
        LOG.warning("Error notification could not be delivered")


def error_text(error):
    """(Russian, English) explanation of a failed answer."""
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
    return ru, en


COMMANDS = {
    "ru": [("app", "Подключения"), ("secretary", "ИИ-секретарь"), ("help", "Что я умею"), ("mode", "Сменить режим"), ("reset", "Очистить память"),
           ("export", "Скачать историю"), ("myid", "Мой Telegram ID")],
    "en": [("app", "Connections"), ("secretary", "AI secretary"), ("help", "What I can do"), ("mode", "Change mode"), ("reset", "Clear memory"),
           ("export", "Download history"), ("myid", "My Telegram ID")],
}


async def ensure_logo(app):
    """Create (once) a custom emoji pack with the animated logo and use it in the thinking bubble.

    The pack belongs to the bot owner. Telegram shows custom emoji from bots only if the owner has Premium;
    otherwise with_logo() falls back to the plain BRAND_EMOJI.
    """
    data = app.bot_data
    data["logo_tried"] = True
    if data.get("logo_id") or not LOGO_EMOJI_FILE.is_file():
        return
    store = data["connections"]
    saved = store.get_meta("logo_emoji")
    if saved and saved.get("version") == LOGO_EMOJI_VERSION:
        data["logo"], data["logo_id"] = saved["emoji"], saved["id"]
        return
    owner = store.get_meta("owner")
    if not owner:
        data["logo_tried"] = False  # try again once someone uses the bot
        return
    name = f"secretai_{LOGO_EMOJI_VERSION}_by_{app.bot.username}"
    try:
        try:
            pack = await app.bot.get_sticker_set(name)
        except BadRequest:
            # attach=True: the file is uploaded as attach://… inside the sticker list (otherwise "sticker" is null).
            upload = InputFile(LOGO_EMOJI_FILE.read_bytes(), filename="logo.webm", attach=True)
            sticker = InputSticker(upload, ["✨"], format="video")
            await app.bot.create_new_sticker_set(owner, name, "Secret AI", [sticker], sticker_type="custom_emoji")
            pack = await app.bot.get_sticker_set(name)
        emoji_id = pack.stickers[0].custom_emoji_id
    except (TelegramError, IndexError) as exc:
        LOG.warning("Could not create the logo emoji: %s %s", type(exc).__name__, getattr(exc, "message", ""))
        return
    store.set_meta("logo_emoji", {"version": LOGO_EMOJI_VERSION, "emoji": "✨", "id": emoji_id})
    data["logo"], data["logo_id"] = "✨", emoji_id


FISH_VOICE = "d432528907754a3aaaa6f2d90b7b9cfa"  # the owner's Fish Audio voice; FISH_VOICE env overrides
KEEP_AWAKE_EVERY = 600  # Render's free plan sleeps after 15 minutes without incoming HTTP requests


async def keep_awake(app):
    """Visit our own public address every 10 minutes so Render never puts the bot to sleep (a sleeping bot gets
    no Telegram messages and the Mini App shows Render's loading screen)."""
    url = app.bot_data["public_url"] + "/health"
    while True:
        await asyncio.sleep(KEEP_AWAKE_EVERY)
        try:
            await app.bot_data["http"].get(url, timeout=30)
        except httpx.HTTPError as exc:
            LOG.warning("Keep-awake ping failed: %s", type(exc).__name__)


async def post_init(app):
    app.bot_data["loop"] = asyncio.get_running_loop()
    app.bot_data["bot_name"] = app.bot.username
    app.bot_data["username_task"] = asyncio.create_task(watch_username(app))
    app.bot_data["welcome_task"] = asyncio.create_task(make_welcome(app))
    if app.bot_data.get("snapshots"):
        app.bot_data["snapshot_task"] = asyncio.create_task(app.bot_data["snapshots"].run())
    if app.bot_data.get("public_url") and os.environ.get("KEEP_AWAKE", "1").strip() != "0":
        app.bot_data["awake_task"] = asyncio.create_task(keep_awake(app))
    await resolve_abacus_model(app)
    await discover_models(app)
    if not app.bot_data.get("logo_id"):
        # The application is not running yet inside post_init, so start a plain task and keep a reference.
        app.bot_data["logo_task"] = asyncio.create_task(ensure_logo(app))
    try:
        await app.bot.set_my_commands([BotCommand(*c) for c in COMMANDS["en"]])
        await app.bot.set_my_commands([BotCommand(*c) for c in COMMANDS["ru"]], language_code="ru")
        group = [BotCommand(game.START, "🧠 Викторина Secret AI для всей группы"),
                 BotCommand(game.RATING, "🏆 Рейтинг викторины"), BotCommand(game.STOP, "⏹ Остановить викторину"),
                 BotCommand("q", "Цитата: ответь на сообщение"), BotCommand("dem", "Демотиватор из фото"),
                 BotCommand("meme", "Мем из TikTok")]
        await app.bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats(), language_code="ru")
        await app.bot.set_my_commands([BotCommand(game.START, "🧠 Secret AI quiz for the whole group"),
                                       BotCommand(game.RATING, "🏆 Quiz leaderboard"), BotCommand(game.STOP, "⏹ Stop the quiz"),
                                       BotCommand("q", "Quote: reply to a message"), BotCommand("dem", "Demotivator from a photo"),
                                       BotCommand("meme", "A TikTok meme")],
                                      scope=BotCommandScopeAllGroupChats())
        if app.bot_data["public_url"]:
            # The button next to the message field opens the Mini App, like in large AI bots.
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("Secret AI", WebAppInfo(app.bot_data["public_url"] + "/app")))
    except TelegramError:
        LOG.warning("Could not register the command menu")


async def shutdown(app):
    if app.bot_data.get("snapshots"):  # the last minute of memory before the deploy replaces this machine
        app.bot_data["snapshot_task"].cancel()
        try:
            await asyncio.wait_for(asyncio.to_thread(app.bot_data["snapshots"].save), 25)
        except Exception as exc:
            LOG.warning("Final database save failed: %s", type(exc).__name__)
    await app.bot_data["client"].close()
    await app.bot_data["http"].aclose()
    app.bot_data["pipedream"].http.close()
    app.bot_data["direct"].http.close()
    app.bot_data["memory"].close()
    app.bot_data["groups"].close()
    app.bot_data["connections"].close()


class MiniAppUpdate:
    """Just enough of an Update for respond(): a request started from the Mini App."""

    def __init__(self, user, message):
        self.effective_user, self.effective_message, self.effective_chat = user, message, message.chat


async def run_prompt(app, tg_user, text):
    """Answer a scenario picked in the Mini App as if the user had sent it."""
    user = User(id=tg_user["id"], first_name=tg_user.get("first_name") or "", is_bot=False,
                language_code=tg_user.get("language_code"))
    context = CallbackContext(app, chat_id=user.id, user_id=user.id)
    try:
        message = await app.bot.send_message(user.id, "💬 " + text)
    except TelegramError:
        LOG.warning("Could not start a Mini App request")
        return
    update = MiniAppUpdate(user, message)
    try:
        async with user_lock(context, user.id):
            await respond(update, context, message, text, text)
    except Exception as exc:  # the same user-facing explanations as for regular messages
        LOG.error("Mini App request error: %s", type(exc).__name__)
        try:
            await send(message, ui(update, *error_text(exc)))
        except TelegramError:
            pass


class Bridge:
    """Runs bot-side work for the Mini App HTTP thread on the bot's event loop (SQLite and Telegram live there)."""

    def __init__(self, app):
        self.app = app

    def _run(self, coro, wait=True):
        loop = self.app.bot_data.get("loop")
        if not loop:
            coro.close()
            raise RuntimeError("bot is not running yet")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=15) if wait else None

    def notify(self, user_id, text):
        self._run(self.app.bot.send_message(user_id, text), wait=False)

    def ask(self, tg_user, text):
        self._run(run_prompt(self.app, tg_user, text), wait=False)

    def settings(self, user_id):
        async def read():
            data, memory = self.app.bot_data, self.app.bot_data["memory"]
            return {"persona": memory.persona(user_id), "model": user_model(data, user_id),
                    "models": data["models"], "messages": memory.count(user_id),
                    "personas": [{"id": pid, "emoji": p[0], "ru": p[1], "en": p[2]} for pid, p in PERSONAS.items()]}
        return self._run(read())

    def update(self, user_id, persona=None, model=None):
        async def write():
            memory = self.app.bot_data["memory"]
            if persona in PERSONAS:
                memory.set_persona(user_id, persona)
            if model in self.app.bot_data["models"]:
                memory.set_model(user_id, model)
        self._run(write())

    def gen_state(self, user_id):
        async def read():
            return gen_state(self.app.bot_data, user_id)
        return self._run(read())

    def generate(self, tg_user, kind, prompt, fmt, seconds=5, voice=True):
        language = "ru" if (tg_user.get("language_code") or "ru").startswith("ru") else "en"
        return self._run(start_generation(self.app, tg_user["id"], language, kind, prompt, fmt, seconds, voice))

    def edit(self, tg_user, images, prompt, fmt="portrait"):
        """Mini App: edit/combine uploaded photos (bytes)."""
        language = "ru" if (tg_user.get("language_code") or "ru").startswith("ru") else "en"
        return self._run(start_generation(self.app, tg_user["id"], language, "photo", prompt, fmt, images=images))

    def invoice(self, tg_user, pack):
        """Telegram Stars invoice link for a credit pack."""
        credits, stars = PACKS[pack]
        ru = (tg_user.get("language_code") or "ru").startswith("ru")
        return self._run(self.app.bot.create_invoice_link(
            title=f"{credits} генераций" if ru else f"{credits} generations",
            description=(f"Кредиты Secret AI: фото — {COST['photo']}, видео — от {COST['video']}" if ru
                         else f"Secret AI credits: photo — {COST['photo']}, video — from {COST['video']}"),
            payload=f"gen:{pack}:{tg_user['id']}", currency="XTR",
            prices=[LabeledPrice(f"{credits} credits", stars)]))

    def secretary(self, user_id):
        async def read():
            data, store = self.app.bot_data, self.app.bot_data["connections"]
            conn_id = store.get_meta(f"biz:{user_id}")
            conn = sec.connection(store, conn_id) if conn_id else None
            current = sec.settings(store, user_id)
            return {"connected": bool(conn and conn["enabled"]), "can_reply": bool(conn and conn["can_reply"]),
                    "enabled": current["enabled"], "rules": current["rules"], "notify": current.get("notify", True),
                    "recent": data["biz_activity"].recent.get(user_id, [])}
        return self._run(read())

    def set_secretary(self, user_id, enabled=None, rules=None, notify=None):
        async def write():
            store = self.app.bot_data["connections"]
            current = sec.settings(store, user_id)
            if rules is not None:
                current["rules"] = str(rules).strip()[:1000]
            if notify is not None:
                current["notify"] = bool(notify)
            if enabled is not None:
                current["enabled"] = bool(enabled)
                if current["enabled"]:
                    self.app.bot_data["biz_activity"].owner_seen.pop(user_id, None)
            if current["enabled"] and not current["rules"]:
                current["rules"] = "Привет! Я сейчас занят и отвечу позже. Это ИИ-помощник."
            sec.save_settings(store, user_id, current)
        self._run(write())
        return self.secretary(user_id)

    def groups(self, user_id):
        async def read():
            return self.app.bot_data["groups"].groups_for(user_id)
        return self._run(read())

    def set_group_level(self, user_id, chat_id, level):
        """Only the group's admins change how often the bot speaks up there. Returns False if not allowed."""
        async def write():
            if level not in LEVELS:
                return False
            try:
                member = await self.app.bot.get_chat_member(chat_id, user_id)
            except TelegramError:
                return False
            if member.status not in ("administrator", "creator"):
                return False
            self.app.bot_data["groups"].set_level(chat_id, level)
            return True
        return self._run(write())

    def set_group_model(self, user_id, chat_id, model):
        """Admins choose the group's model ('' = default). Returns False if not allowed."""
        async def write():
            if model and model not in self.app.bot_data["models"]:
                return False
            try:
                member = await self.app.bot.get_chat_member(chat_id, user_id)
            except TelegramError:
                return False
            if member.status not in ("administrator", "creator"):
                return False
            self.app.bot_data["groups"].set_model(chat_id, model)
            return True
        return self._run(write())

    def reset(self, user_id):
        async def clear():
            async with self.app.bot_data["locks"][user_id]:
                self.app.bot_data["memory"].reset(user_id)
        self._run(clear())


def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in (LOG.name, "persist", "media"):  # our own notes (voice used, database restored…) are worth seeing
        logging.getLogger(name).setLevel(logging.INFO)
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
    database = os.environ.get("DATABASE_PATH", "memory.db")
    snapshots = persist.from_env(database)
    if snapshots:  # Render wipes the disk on every deploy: bring the database back from Postgres first
        try:
            snapshots.restore()
        except Exception as exc:  # better to start with an empty memory than not at all
            LOG.error("Could not restore the database from Postgres: %s %s", type(exc).__name__, str(exc)[:200])
    public_url = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if public_url and not public_url.startswith("https://"):
        raise SystemExit("PUBLIC_URL must start with https:// (Telegram Mini Apps require HTTPS).")
    # Service tokens are encrypted with ENCRYPTION_KEY; without it the key is derived from the bot token.
    owner = os.environ.get("OWNER_ID", "").strip()
    connections = Connections(database, os.environ.get("ENCRYPTION_KEY", "").strip() or token,
                              int(owner) if owner.isdigit() else None)
    model = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
    # Models offered in the Mini App picker; the first one stays the default for users who never chose.
    models = [model] + [m for m in dict.fromkeys(x.strip() for x in os.environ.get("OPENAI_MODELS", "").split(",")) if m and m != model]
    direct = DirectApps(connections, public_url)
    github_apps = GitHubApps(connections, os.environ.get("GITHUB_CLIENT_ID", "").strip(), os.environ.get("GITHUB_CLIENT_SECRET", "").strip())
    pipedream = Pipedream(os.environ.get("PIPEDREAM_CLIENT_ID", "").strip(), os.environ.get("PIPEDREAM_CLIENT_SECRET", "").strip(),
                          os.environ.get("PIPEDREAM_PROJECT_ID", "").strip(), os.environ.get("PIPEDREAM_ENVIRONMENT", "production").strip(),
                          os.environ.get("PIPEDREAM_MCP_URL", "").strip())
    app = Application.builder().token(token).concurrent_updates(True).post_init(post_init).post_shutdown(shutdown).build()
    app.bot_data.update(
        connections=connections, http=httpx.AsyncClient(), pending={}, public_url=public_url,
        client=AsyncOpenAI(api_key=key, timeout=120, max_retries=2),
        memory=Memory(database), allowed=allowed, snapshots=snapshots,
        model=model, models=models, pipedream=pipedream, github_apps=github_apps, direct=direct,
        stream=os.environ.get("STREAM_REPLIES", "1").strip() != "0",
        drafts=os.environ.get("DRAFT_REPLIES", "1").strip() != "0",
        clear_drafts=os.environ.get("CLEAR_DRAFTS", "1").strip() != "0",
        logo=os.environ.get("BRAND_EMOJI", "✨").strip() or "✨",
        logo_id=os.environ.get("BRAND_EMOJI_ID", "").strip(),
        tts_model=os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts").strip(),
        tts_voice=os.environ.get("OPENAI_TTS_VOICE", "alloy").strip() or "alloy",
        fal_env_key=os.environ.get("FAL_KEY", "").strip(), gemini_env_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        hf_env_key=os.environ.get("HF_KEY", "").strip(),
        kling_env_key=kling_env_key(),
        hf_token=os.environ.get("HF_TOKEN", "").strip(),
        anymodel_key=os.environ.get("ANYMODEL_API_KEY", "").strip().removeprefix("Bearer ").strip(),
        anymodel_api=os.environ.get("ANYMODEL_BASE", "").strip().rstrip("/") or ANYMODEL_API,
        anymodel_video=os.environ.get("ANYMODEL_VIDEO_MODEL", "").strip() or ANYMODEL_VIDEO,
        anymodel_voice=os.environ.get("ANYMODEL_VOICE", ANYMODEL_VOICE).strip(),
        fish_key=os.environ.get("FISH_API_KEY", "").strip(),
        fish_voice=fish_voice_id(os.environ.get("FISH_VOICE", "") or FISH_VOICE),
        fish_model=os.environ.get("FISH_MODEL", "").strip(),
        fish_via_anymodel=os.environ.get("FISH_VIA_ANYMODEL", "1").strip() != "0",
        chat_default=OPUS, chat_override=os.environ.get("CHAT_MODEL", "").strip(),
        hf_spaces=tuple(x.strip() for x in os.environ.get("HF_SPACES", "").split(",") if x.strip()) or HF_SPACES,
        kling_api=os.environ.get("KLING_API_BASE", "").strip().rstrip("/") or KLING_API,
        cloudflare=(os.environ.get("CF_ACCOUNT_ID", "").strip(), os.environ.get("CF_API_TOKEN", "").strip()),
        abacus=AsyncOpenAI(api_key=os.environ["ABACUS_API_KEY"].strip(), base_url=ABACUS_URL, timeout=120, max_retries=2)
        if os.environ.get("ABACUS_API_KEY", "").strip() else None,
        anymodel_chat=AsyncOpenAI(api_key=os.environ["ANYMODEL_API_KEY"].strip().removeprefix("Bearer ").strip(),
                                  base_url=os.environ.get("ANYMODEL_BASE", "").strip().rstrip("/") or ANYMODEL_API,
                                  timeout=180, max_retries=2) if os.environ.get("ANYMODEL_API_KEY", "").strip() else None,
        abacus_model=os.environ.get("ABACUS_MODEL", "").strip(),
        abacus_image=os.environ.get("ABACUS_IMAGE_MODEL", "").strip(),
        image_model=os.environ.get("OPENAI_IMAGE_MODEL", "").strip(),
        web_search=os.environ.get("WEB_SEARCH", "1").strip() != "0",
        # Low effort: the first words come much sooner; set REASONING_EFFORT=medium/high for deeper answers.
        reasoning=os.environ.get("REASONING_EFFORT", "low").strip(),
        locks=defaultdict(asyncio.Lock), last_answer={}, groups=GroupStore(database), group_spoke={},
        biz_activity=sec.Activity())
    # Business updates first: their messages must not reach the regular chat handlers.
    app.add_handler(BusinessConnectionHandler(business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGES, business_message))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["secretary", "sekretar"], secretary_command))
    app.add_handler(CommandHandler(["voice", "golos"], voice_command))
    app.add_handler(CommandHandler("wipe", wipe))
    app.add_handler(CommandHandler("welcome", welcome_command))
    app.add_handler(CommandHandler("setwelcome", set_welcome))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mode", mode))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("emojiid", emojiid))
    app.add_handler(CommandHandler(["app", "connect"], open_app))
    app.add_handler(CommandHandler(["q", "quote"], quote_command))
    app.add_handler(CommandHandler(["dem", "demotivator"], demotivator_command))
    app.add_handler(CommandHandler(["meme", "mem"], meme_command))
    # Unusual names on purpose: other bots in a group react to /game, /quiz and /top too.
    app.add_handler(CommandHandler([game.START, "svik"], game_command))
    app.add_handler(CommandHandler([game.RATING, "stop10"], top_command))
    app.add_handler(CommandHandler(game.STOP, stop_game_command))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, added_to_group))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.PHOTO | filters.Document.IMAGE,
        chat))
    app.add_handler(MessageHandler(filters.ALL, unsupported))
    app.add_error_handler(on_error)
    handler = miniapp.make_handler({
        "token": token, "allowed": allowed, "store": connections, "public_url": public_url,
        "github": github_apps, "direct": direct, "bot_name": lambda: app.bot_data.get("bot_name") or "bot",
        "bridge": Bridge(app), "pipedream": pipedream})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app.run_polling(allowed_updates=["message", "callback_query", "pre_checkout_query", "business_connection", "business_message"], drop_pending_updates=False, bootstrap_retries=3)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
