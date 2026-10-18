"""Secret AI: Telegram polling bot with a Render HTTP health endpoint."""
import asyncio
import base64
import hmac
import html
import io
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import tempfile
import time
import traceback
from types import SimpleNamespace
from collections import defaultdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import emoji as emoji_lib
import imageio_ffmpeg

from media import _ffmpeg as media_ffmpeg
from openai import (
    APIConnectionError,
    APIError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)
from telegram import (BotCommand, BotCommandScopeAllGroupChats, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputSticker, LabeledPrice, MenuButtonWebApp,
                      MessageEntity, ReplyParameters, Update, User, WebAppInfo)
from codefiles import extract_files, site_file
from fun import LEVELS, GroupStore, ago, MemeError, demotivator, quote_card, tiktok_meme
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (Application, BusinessConnectionHandler, CallbackContext, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          PreCheckoutQueryHandler, TypeHandler, filters)

import codex
import cursor
import gentemplates
import digest
import chatserial
import game
import hosting
import links
import mcpbridge
import miniapp
import persist
import secretary as sec
import speech
import welcome
from apps import Pipedream, PipedreamError
from github_app import GitHubApps
from mcp_oauth import DirectApps, MCPAuthError
from media import (COST, DEFAULT_MODELS, FAL_SCENE_SECONDS, FORMATS, KLING, KLING_API, KLING_DIRECT, KLING_SECONDS, kling_generate, ANYMODEL_API, ANYMODEL_PREFIX, ANYMODEL_SECONDS, ANYMODEL_VIDEO, anymodel_generate, anymodel_speech, anymodel_voice_id, anymodel_voices, ANYMODEL_VOICE, ANYMODEL_FISH, fish_speech, fish_voice_id, HF_SPACES, space_video, fit_clip, FREE_PHOTOS_PER_DAY, MAX_PROMPT, OLD_PHOTO_DEFAULTS, OLD_VIDEO_DEFAULTS, PACKS, SCENE_SECONDS,
                   VIDEO_PRICES, MediaError, OPENAI_IMAGE, voice_message, abacus_image, openai_image, free_image, join_with_voice, living_picture, fal_generate, fal_sound, hf_generate, montage, price, veo_generate, video_provider)
from integrations import (GITHUB_TOOLS, Connections, GitHub, GitHubError, Toolbox, apply_change, delete_repos, describe,
                          describe_deletion, prune_pending)

LOG = logging.getLogger("secret_ai")
SYSTEM = (
    "You are Secret AI, a brilliant, knowledgeable and friendly assistant. "
    "Your name here is Secret AI (say so when asked your name). Reply in the user's language. "
    "Use the language of their latest message, caption or voice transcription; "
    "if none is available, use their Telegram language. Short greetings and words shared by many languages (салам, "
    "привет, ок, хай, рахмат) are not a reason to switch: keep the language of the conversation so far, Russian when unsure. "
    "Never drift into another language or script on your own (no Korean, Chinese, Japanese, Arabic… phrases, not even as a "
    "joke) unless the user writes in it or asks for it. "
    "Get facts, numbers and code right. Give complete, practical answers: concrete steps, examples and working "
    "solutions instead of generic advice; for simple questions and small talk be brief and quick. "
    "Search the web only when the answer depends on recent or changing facts (news, prices, versions, events). "
    "Admit uncertainty instead of guessing. "
    "Read every short reply in the light of your own last message (also when that message was a voice answer): if you "
    "just offered ideas, options or a list and the user answers «скучно», «не то», «фигня», «ну такое», «ещё», «другое», "
    "they are judging YOUR offer, so give fresher, bolder options on the same topic instead of starting a new one; "
    "only treat it as a new topic when it clearly is one, and if it could mean both, ask in a few words. "
    "Reply in plain text, without Markdown formatting (no **bold**, no # headings). "
    "For ordinary replies, use no emoji by default. If one adds meaning, use at most one per reply, "
    "never in every sentence or between words. Do not copy emoji density from conversation history or emoji catalogs. "
    "When the user explicitly requests emoji content, provide it as requested. "
    "Previous photo markers are not images; do not invent unseen visual details."
)
# What the models can't know from their training: today's date and the AI models that exist now (their knowledge
# stops months or years earlier, so asked "what are the newest Claude models" they listed Claude 3/4).
AI_MODELS_NOW = (
    "Current AI models (as of October 2026; newer than your training data — trust this list over your memory): "
    "Anthropic Claude — Claude Fable 5.1 (the most capable), Claude Opus 5.5, Claude Opus 5, Claude Sonnet 5.5, Claude "
    "Sonnet 5, Claude Haiku 5.5 (the newest generation), and the previous ones still served: Claude Opus 4.8, 4.7, 4.6, "
    "Claude Sonnet 4.6, Claude Haiku 4.5; Claude 3.x and older are retired. Other labs have newer models than you "
    "remember too (GPT-6, Gemini 4, Grok 4.7…); for anything about very recent releases, prices or news, say what you "
    "know may be outdated and use web search when it's available. "
)


def now_note():
    """Today's date (models don't know it) and the current model lineup, added to every system prompt."""
    return f" Today is {time.strftime('%Y-%m-%d')}. " + AI_MODELS_NOW


# The bot's built-in voice: plain, friendly, no accent.
ACCENT = (" Speak plain, natural Russian (or the user's language) with no accent or ethnic stylisation: never use «вах», "
          "«брат», «валлах», «э», «дорогой», «клянусь», «слушай» as filler, even if your own earlier messages in the history "
          "did; that style is switched off.")
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
    " No accent and no ethnic stylisation: never write «вах», «брат», «валлах», «э», «дорогой», «клянусь», «мамой клянусь», "
    "«слушай» as filler, even if your own earlier messages in this chat did; that style is switched off, and if someone "
    "asks you to stop saying «вах», just stop."
    " Stickers come as [стикер: what's on it] — react to that picture like a person who saw it. When it says «картинки "
    "не видно», don't invent what's on it; a pack's emoji tag is never a message (a sticker is not a «минус»)."
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
USERNAME_REFRESH = 300
GROUP_PARALLEL = 3  # answers being written at once in one group (others wait); uninvited remarks still go one by one  # the bot's @username is re-read so a renamed bot still reacts to mentions
PHOTO_MARKER = "[Photo attached; image is not retained] "
HISTORY_LIMIT = 1000  # the newest messages the model reads in full
KEEP_MESSAGES = 200_000  # kept per person ("forever"); older ones come back by search when the talk touches them
RECALL_MATCHES = 12
RECALL_CHARS = 600
# What the model sees of the history: older long answers are shortened and the whole history fits a budget,
# so 300 messages stay affordable and inside the model's context.
HISTORY_CHARS = 24_000  # a big prompt makes every answer slow on the gateways; older messages come back by search
MAX_FACTS = 80  # lasting facts kept per person
HISTORY_ITEM_CHARS = 2_500

# id: (emoji, Russian name, English name, extra instructions)
PERSONAS = {
    "street": ("😎", "Свой пацан", "Homie",
               "Style: you talk like a cool guy from the user's own chat, not like an assistant. Small talk gets one short "
               "line (often lowercase, no full stop at the end), street slang and easy humour: «ну чё», «го», «хз», «изи», "
               "«жиза», «база», «кринж», «бро» — a touch, not in every line, and never «вах»/«брат»-style accent filler. "
               "No assistant phrases («чем могу помочь», «рад помочь», «отличный вопрос»), no lists or headings for small "
               "talk, at most one emoji. Swear only if the user swears first. When they need something real (code, facts, "
               "advice, a task), do it properly and completely, just in the same relaxed tone. Asked who you are or which "
               "model: answer casually and truthfully — you're Secret AI, an AI (name the model if asked); never claim to "
               "be a human."),
    "assistant": ("✨", "Ассистент", "Assistant", ""),
    "coder": ("💻", "Программист", "Coder",
              "Act as a senior software engineer: give complete working code, explain decisions briefly and point out pitfalls."),
    "translator": ("🌍", "Переводчик", "Translator",
                   "Act as a translator: translate Russian text into English and any other language into Russian. "
                   "Preserve tone and line breaks. Output only the translation."),
    "creative": ("🎨", "Креатив", "Creative",
                 "Be bold and original: suggest unexpected ideas and write vividly."),
}
DEFAULT_PERSONA = "street"  # the relaxed "one of us" tone; /mode switches to the assistant

# The thinking bubble starts with the logo: an animated custom emoji made from webapp/logo-emoji.webm
# (the bot creates the emoji pack itself) or the one from BRAND_EMOJI_ID.
LOGO_EMOJI_FILE = Path(__file__).with_name("webapp") / "logo-emoji.webm"
LOGO_EMOJI_VERSION = "v2"
CURSOR = ""  # no typing bar after the streamed text: it flashed like a glitch
# Telegram allows roughly one edit per second per chat; faster edits get RetryAfter.
ANIMATION_INTERVAL = 1.5
STREAM_EDIT_INTERVAL = 1.2
# Groups: Telegram allows about 20 messages a minute per group and edits count, so an answer is edited at most
# GROUP_EDITS times, GROUP_EDIT_INTERVAL seconds apart (private chats use drafts, which aren't limited like that).
GROUP_EDITS = 3
GROUP_EDIT_INTERVAL = 4.0


ACTIVE = {"answers": 0}  # answers being written right now: a self-update waits for them before restarting


def counted(handler):
    """Count a running answer so a restart never cuts it off."""
    async def run(*args, **kwargs):
        ACTIVE["answers"] += 1
        try:
            return await handler(*args, **kwargs)
        finally:
            ACTIVE["answers"] -= 1
    run.__name__, run.__doc__ = handler.__name__, handler.__doc__
    return run

TYPING_INTERVAL = 4.5
# Native drafts (sendMessageDraft) animate smoothly and are not limited like edits.
DRAFT_INTERVAL = 0.3
# A draft disappears after 30 seconds without updates.
DRAFT_KEEPALIVE = 10
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
        self.db.create_function("ulower", 1, lambda text: text.lower() if isinstance(text, str) else text, deterministic=True)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS by_user ON messages(user_id, id)")
        if "created" not in [row[1] for row in self.db.execute("PRAGMA table_info(messages)")]:
            self.db.execute("ALTER TABLE messages ADD COLUMN created INTEGER")
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
        # Promo codes: credits for a code, a limited number of people, each person once; an optional end date.
        self.db.execute("CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY, credits INTEGER NOT NULL, "
                        "max_uses INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, expires INTEGER, created INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS promo_uses (code TEXT NOT NULL, user_id INTEGER NOT NULL, at INTEGER NOT NULL, "
                        "PRIMARY KEY (code, user_id))")
        self.db.commit()

    def history(self, user_id, limit=HISTORY_LIMIT):
        rows = self.db.execute("SELECT id, role, content FROM (SELECT id, role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?) ORDER BY id", (user_id, limit)).fetchall()
        return [{"id": id_, "role": role, "content": content} for id_, role, content in rows]

    def append_turn(self, user_id, question, answer):
        now = int(time.time())
        with self.db:
            self.db.executemany("INSERT INTO messages(user_id, role, content, created) VALUES (?, ?, ?, ?)",
                                [(user_id, "user", question, now), (user_id, "assistant", answer, now)])
            self.db.execute("DELETE FROM messages WHERE user_id=? AND id <= (SELECT id FROM messages WHERE user_id=? "
                            "ORDER BY id DESC LIMIT 1 OFFSET ?)", (user_id, user_id, KEEP_MESSAGES))

    def recall(self, user_id, text, before_id):
        """Old messages (older than before_id, i.e. no longer in the model's view) about the same things as text:
        the matching messages with their question/answer partner, oldest first: [(created, role, content)]."""
        words = list(dict.fromkeys(w for w in re.findall(r"\w{4,}", (text or "").lower()) if not w.isdigit()))[:8]
        if not words or not before_id:
            return []
        stems = [w[:max(4, len(w) - 2)] for w in words]  # Russian endings vary: "айлы", "айле" -> "айл"
        variants = list(dict.fromkeys(v for s in stems for v in (s, s.capitalize())))  # fast LIKE: case only for Latin
        rows = self.db.execute("SELECT id, role, content FROM messages WHERE user_id=? AND id < ? AND ("
                               + " OR ".join("content LIKE ?" for _ in variants) + ") ORDER BY id DESC LIMIT 3000",
                               (user_id, before_id, *[f"%{v}%" for v in variants])).fetchall()
        best = sorted(rows, key=lambda r: (-sum(s in r[2].lower() for s in stems), -r[0]))[:RECALL_MATCHES]
        ids = sorted({i for id_, role, _ in best for i in (id_, id_ + 1 if role == "user" else id_ - 1)})
        if not ids:
            return []
        found = self.db.execute(f"SELECT id, created, role, content FROM messages WHERE user_id=? AND id < ? AND id IN "
                                f"({','.join('?' * len(ids))}) ORDER BY id", (user_id, before_id, *ids)).fetchall()
        return [(created, role, content) for _, created, role, content in found]

    def annotate(self, user_id, note):
        """Add a note to the bot's latest answer (e.g. that a video was sent): the history then remembers it for good,
        restarts included, and the roles still alternate."""
        with self.db:
            self.db.execute("UPDATE messages SET content = content || ? WHERE id = (SELECT MAX(id) FROM messages "
                            "WHERE user_id=? AND role='assistant')", ("\n" + note, user_id))

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
        def fold(text):  # in Python: SQLite's lower() leaves Cyrillic as it is ("Берлине" never matched "берлине")
            return text.casefold().replace("ё", "е")
        with self.db:
            phrases = [fold(p.strip()) for p in forget if p.strip()]
            if phrases:
                rows = self.db.execute("SELECT id, fact FROM facts WHERE user_id=?", (user_id,)).fetchall()
                gone = [(row_id,) for row_id, fact in rows if any(p in fold(fact) for p in phrases)]
                self.db.executemany("DELETE FROM facts WHERE id=?", gone)
            known = {f.casefold() for f in self.facts(user_id)}
            for fact in add:
                fact = " ".join(fact.split())[:300]
                if fact and fact.casefold() not in known:
                    self.db.execute("INSERT INTO facts(user_id, fact) VALUES (?, ?)", (user_id, fact))
                    known.add(fact.casefold())
            self.db.execute("DELETE FROM facts WHERE user_id=? AND id NOT IN (SELECT id FROM facts WHERE user_id=? "
                            "ORDER BY id DESC LIMIT ?)", (user_id, user_id, MAX_FACTS))

    def reset(self, user_id):
        """Forget this person: the conversation and the facts remembered about them (name, city…)."""
        with self.db:
            self.db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM facts WHERE user_id=?", (user_id,))

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

    def promo_create(self, code, credits, max_uses, days=None):
        """A new code (or the same code replaced); max_uses 0 = no limit."""
        now = int(time.time())
        with self.db:
            self.db.execute("INSERT INTO promo_codes(code, credits, max_uses, used, expires, created) VALUES (?, ?, ?, 0, ?, ?) "
                            "ON CONFLICT(code) DO UPDATE SET credits=excluded.credits, max_uses=excluded.max_uses, "
                            "expires=excluded.expires", (code, credits, max_uses, now + days * 86400 if days else None, now))

    def promo_redeem(self, user_id, code):
        """('ok', credits) or (reason, 0): 'unknown', 'expired', 'empty', 'used'. Atomic: never twice, never over the limit."""
        now = int(time.time())
        with self.db:
            row = self.db.execute("SELECT credits, max_uses, used, expires FROM promo_codes WHERE code=?", (code,)).fetchone()
            if not row:
                return "unknown", 0
            credits, max_uses, used, expires = row
            if expires and expires < now:
                return "expired", 0
            if self.db.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, user_id)).fetchone():
                return "used", 0
            taken = self.db.execute("UPDATE promo_codes SET used=used+1 WHERE code=? AND (max_uses=0 OR used<max_uses)", (code,))
            if not taken.rowcount:
                return "empty", 0
            self.db.execute("INSERT INTO promo_uses(code, user_id, at) VALUES (?, ?, ?)", (code, user_id, now))
            self._add_credits(user_id, credits)
        return "ok", credits

    def promo_list(self):
        return self.db.execute("SELECT code, credits, max_uses, used, expires FROM promo_codes ORDER BY created DESC").fetchall()

    def promo_delete(self, code):
        with self.db:
            return self.db.execute("DELETE FROM promo_codes WHERE code=?", (code,)).rowcount > 0

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


# ---------- premium emoji: the owner sends the bot custom emoji once, the bot puts them in its texts and buttons ----------
PREMIUM_EMOJI = {}  # the plain emoji (😀) -> the id of the owner's chosen custom emoji in its place
PREMIUM_MAX = 90  # Telegram takes at most ~100 custom emoji in one message


def emoji_key(char):
    """The emoji without the variation selector, so ❤ and ❤️ are the same."""
    return char.replace("\ufe0f", "")


def valid_emoji_key(text):
    """A custom emoji may replace an emoji, never a space, letter or punctuation mark."""
    return isinstance(text, str) and bool(text) and emoji_lib.is_emoji(text)


# Keep code (including an unfinished streamed code fence) and URLs byte-for-byte.
CHAT_LITERAL = re.compile(r"(```[\s\S]*?(?:```|\Z)|`[^`\n]*(?:`|\Z)|https?://[^\s<>]+)")


def clean_chat_text(text):
    """Remove decorative emoji from generated prose before streaming, sending and saving it."""
    parts = CHAT_LITERAL.split(text)
    for index in range(0, len(parts), 2):
        original = parts[index]
        cleaned = emoji_lib.replace_emoji(original, replace=" ")
        if cleaned != original:
            cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
            cleaned = re.sub(r" +([,.!?;:])", r"\1", cleaned)
            cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
            cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
        parts[index] = cleaned
    return "".join(parts).strip()


class CleanChatReply:
    """Filter each cumulative preview as well as the final reply; status UI stays intact."""

    def __init__(self, reply):
        self.reply = reply

    def __getattr__(self, name):
        return getattr(self.reply, name)

    async def update(self, text):
        await self.reply.update(clean_chat_text(text))

    async def finish(self, text, reply_markup=None):
        return await self.reply.finish(clean_chat_text(text), reply_markup=reply_markup)


def premium_entities(text, start=0):
    """MessageEntity list that turns the known plain emoji in text into the owner's custom ones."""
    if not PREMIUM_EMOJI or not text:
        return []
    found, offset, index = [], 0, 0
    keys = sorted((key for key in PREMIUM_EMOJI if valid_emoji_key(key)), key=len, reverse=True)
    while index < len(text) and len(found) < PREMIUM_MAX:
        for key in keys:
            plain = text[index:index + len(key) + 1]
            matched = key if text.startswith(key, index) else None
            if matched:
                length = len(key) + (1 if text[index + len(key):index + len(key) + 1] == "\ufe0f" else 0)
                piece = text[index:index + length]
                found.append(MessageEntity(MessageEntity.CUSTOM_EMOJI, start + offset, utf16_len(piece),
                                           custom_emoji_id=PREMIUM_EMOJI[key]))
                offset += utf16_len(piece)
                index += length
                break
        else:
            offset += utf16_len(text[index])
            index += 1
    return found


def premium_button(button):
    """A button whose text starts with a known emoji gets the custom one as its icon (the plain one leaves the text)."""
    text = button.text or ""
    for key, emoji_id in PREMIUM_EMOJI.items():
        if valid_emoji_key(key) and emoji_key(text).startswith(key) and not button.icon_custom_emoji_id:
            rest = text[len(key):].lstrip("\ufe0f").strip()
            fields = {name: getattr(button, name) for name in ("url", "callback_data", "web_app", "login_url", "copy_text",
                                                                 "switch_inline_query", "switch_inline_query_current_chat",
                                                                 "switch_inline_query_chosen_chat", "pay", "style")
                      if getattr(button, name, None) is not None}
            return InlineKeyboardButton(rest or text, icon_custom_emoji_id=emoji_id, **fields)
    return button


def premium_markup(markup):
    if not PREMIUM_EMOJI or not isinstance(markup, InlineKeyboardMarkup):
        return markup
    return InlineKeyboardMarkup([[premium_button(b) for b in row] for row in markup.inline_keyboard])


def premium_off(exc):
    """Telegram refused custom emoji (the owner has no Premium any more): plain ones from now on."""
    if PREMIUM_EMOJI and any(word in str(exc).lower() for word in ("emoji", "entit", "document", "icon")):
        LOG.warning("Premium emoji refused by Telegram (%s); using plain ones", getattr(exc, "message", exc))
        PREMIUM_EMOJI.clear()
        return True
    return False


async def rich_edit(query, text, reply_markup=None):
    """Edit a message (a button press) with the premium emoji in its text and buttons."""
    try:
        return await query.edit_message_text(text, reply_markup=premium_markup(reply_markup),
                                              entities=premium_entities(text) or None)
    except BadRequest as exc:
        if not premium_off(exc):
            raise
        return await query.edit_message_text(text, reply_markup=reply_markup)


async def reply(message, text, reply_markup=None, entities=None):
    if entities is None and PREMIUM_EMOJI:
        entities = premium_entities(text) or None
    if PREMIUM_EMOJI:
        reply_markup = premium_markup(reply_markup)
    for attempt in range(3):
        try:
            return await message.reply_text(text, parse_mode=None, reply_markup=reply_markup, entities=entities)
        except BadRequest as exc:
            if attempt == 0 and premium_off(exc):
                entities, reply_markup = None, reply_markup and InlineKeyboardMarkup(
                    [[InlineKeyboardButton(b.text, **{k: getattr(b, k) for k in ("url", "callback_data", "web_app") if getattr(b, k, None)})
                      for b in row] for row in reply_markup.inline_keyboard]) if isinstance(reply_markup, InlineKeyboardMarkup) else reply_markup
                continue
            raise
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
        self.halted = asyncio.Event()  # the user pressed Telegram's Stop button on this draft

    def status(self, note):
        """Show what the bot is doing (e.g. searching the web) with the thinking timer or under the text."""
        track_step(self, note)

    async def _push(self, text, entities=None):
        # An empty draft is Telegram's own see-through "Thinking… 5s" placeholder (Bot API 10).
        await self.bot.send_message_draft(self.chat_id, self.draft_id, text or None, entities=entities or None,
                                          api_kwargs=STOP_BUTTON)
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
            "rich_message": {"blocks": [{"type": "thinking", "text": text}], "skip_entity_detection": True}, **STOP_BUTTON})

    async def start(self):
        LIVE_DRAFTS[(self.chat_id, self.draft_id)] = self
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
        # Display all available tokens immediately; the provider already streams them.
        self.revealed = len(pending)
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
        LIVE_DRAFTS.pop((self.chat_id, self.draft_id), None)
        self.closed = True
        self.wake.set()
        # Let the draft loop finish its current step so a part is never sent twice.
        for task in self.tasks[1:]:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def finish(self, text, reply_markup=None):
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


LIVE_DRAFTS = {}  # (chat id, draft id) -> the DraftReply still streaming there
# Telegram's own Stop button on a streaming draft (Bot API: can_stop); the preview stays until we send the message.
STOP_BUTTON = {"can_stop": True, "keep_on_stop": True}


class Stopped(Exception):
    """The user pressed Stop while the answer was being written."""


async def stop_generation(update, context):
    """Telegram's stopped_message_generation update: halt the answer behind that draft."""
    stop = (update.api_kwargs or {}).get("stopped_message_generation")
    if not isinstance(stop, dict):
        return
    try:
        key = (int((stop.get("chat") or {}).get("id")), int(stop.get("draft_id")))
    except (TypeError, ValueError):
        return
    draft = LIVE_DRAFTS.get(key)
    if draft:
        LOG.info("The user stopped an answer")
        draft.halted.set()


async def unless_stopped(live, coro):
    """Await the answer; raises Stopped (and cancels the work) when the user presses Stop on the draft."""
    halted = getattr(live, "halted", None)
    if halted is None:
        return await coro
    work, waiter = asyncio.ensure_future(coro), asyncio.ensure_future(halted.wait())
    try:
        done, _ = await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        work.cancel()
        waiter.cancel()
        raise
    waiter.cancel()
    if work in done:
        return work.result()
    work.cancel()
    await asyncio.gather(work, return_exceptions=True)
    raise Stopped


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
# More gateways for free models (the key is <NAME>_API_KEY): name -> (base URL, [(how to find a model, id to try if the
# list is closed)], what an id must contain). Each found model goes into the menu as a free one.
EXTRA_PROVIDERS = {
    "nvidia": ("https://integrate.api.nvidia.com/v1",       # build.nvidia.com: DeepSeek, Kimi, Qwen for free
               [(r"deepseek-(v\d|chat)", None), (r"qwen-?3", None)], ""),
    "openrouter": ("https://openrouter.ai/api/v1",           # openrouter.ai: only its ":free" models
                   [(r"deepseek", None), (r"qwen", None)], ":free"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1",   # Alibaba DashScope (DASHSCOPE_API_KEY): DeepSeek V4 Flash
                  [(r"deepseek-?v4-?flash(?!.*vision)", "deepseek-v4-flash")], ""),
    "bai": ("https://api.b.ai/v1",                           # b.ai: its free DeepSeek V4 Flash and Tencent Hy3 (BAI_BASE: the
            [(r"deepseek-?v4-?flash(?!.*vision)", "deepseek-v4-flash"), (r"\bhy-?3", "Hy3")], ""),   # Base URL from its key page)
    "experiential": ("https://api.experientiallabs.ai/v1",  # platform.experientiallabs.ai (EXPERIENTIAL_API_KEY, xpl_…): the owner's
                     [], ""),                                 # subscription; its models are picked in MODEL_CATALOG
    "tokenharbor": ("https://tokenharbor.ai/v1",         # tokenharbor.ai: its ":free" models (TOKENHARBOR_BASE if the URL differs)
                    [(r"deepseek", "deepseek-v4.1-flash:free"), (r"mimo", "mimo-v2.6-flash:free")], ":free"),
}
CHAT_PROVIDERS = {ABACUS_PREFIX: "abacus", ANYMODEL_PREFIX: "anymodel_chat", "groq:": "groq", "cerebras:": "cerebras",
                  **{f"{name}:": name for name in EXTRA_PROVIDERS}}
# Very fast inference on special chips, free keys: GROQ_API_KEY (console.groq.com), CEREBRAS_API_KEY (cloud.cerebras.ai).
FAST_PROVIDERS = {"groq": "https://api.groq.com/openai/v1", "cerebras": "https://api.cerebras.ai/v1"}
FAST_PICK = (r"gpt-oss-120b", r"qwen-?3.*(235|480)", r"llama-?4.*maverick", r"llama-?3\.3-70b", r"gpt-oss-20b")
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


def pick_grok(ids):
    """The newest Grok chat model in a catalog (not mini, vision, image or code ones), its provider prefix kept."""
    found = [i for i in ids if re.search(r"grok[-._ ]?\d", i, re.I) and not re.search(r"mini|vision|image|imagine|code|embed", i, re.I)]
    def version(i):
        return ([int(n) for n in re.findall(r"\d+", re.split(r"grok", i, flags=re.I)[1])] + [0, 0])[:2]
    found.sort(key=lambda i: ([-n for n in version(i)], "fast" in i.lower(), "reason" in i.lower(), len(i)))
    return found[0] if found else None


THINKERS = re.compile(r"gpt-?[5-9]|grok|\bo[1-9]|deepseek-?r|reason|thinking|gemini|claude", re.I)


def quick_thinking(data, model):
    """Request less thinking; remember gateways that reject this optional parameter."""
    effort = (data.get("chat_reasoning") or "low").strip()
    if not THINKERS.search(model) or effort in ("default", "off") or model in data.setdefault("no_effort", set()):
        return {}
    if effort == "low" and re.search(r"gemini[-._ ]3(?:[-._ ]0)?[-._ ]flash", model, re.I):
        effort = "low" if model in data.get("low_effort", set()) else "minimal"
    return {"reasoning_effort": effort}


# A gateway (AnyModel…) refuses requests beyond its key's limit with 429 "overloaded". Requests to one gateway queue
# here instead (GATEWAY_PARALLEL at a time), and a 429 is retried after a pause before anyone sees an error.
GATEWAY_PARALLEL = max(1, int(os.environ.get("GATEWAY_PARALLEL", "5") or 5))
GATEWAY_SLOTS = {}
RATE_RETRIES = (2, 5, 10)  # seconds to wait before each retry of a 429 (unless the gateway says how long)
OUT_OF_CREDITS = re.compile(r"no remaining credits|insufficient[_ ]?(quota|balance|credits)|out of credits", re.I)


def gateway_slot(model):
    return GATEWAY_SLOTS.setdefault(chat_provider(model) or "openai", asyncio.Semaphore(GATEWAY_PARALLEL))


def gateway_busy(model):
    """Every slot of this model's gateway is taken: optional extras (uninvited group remarks) skip their turn."""
    return gateway_slot(model).locked()


async def chat_completion(data, model_id, client, **options):
    """Adapt optional effort once per model; wait and retry when the gateway is momentarily overloaded (429)."""
    options.update(quick_thinking(data, model_id))
    waits = list(RATE_RETRIES)
    while True:
        try:
            return await client.chat.completions.create(**options)
        except RateLimitError as exc:
            if not waits or OUT_OF_CREDITS.search(str(exc)):
                raise
            header = (getattr(getattr(exc, "response", None), "headers", None) or {}).get("retry-after")
            delay = waits.pop(0)
            try:
                delay = min(20.0, max(delay, float(header))) if header else delay
            except ValueError:
                pass
            LOG.warning("%s is overloaded (429); retrying in %.0f s", model_id, delay)
            await asyncio.sleep(delay)
        except BadRequestError as exc:
            if "reasoning_effort" not in options or not re.search(r"reason|effort|thinking", str(exc), re.I):
                raise
            if options["reasoning_effort"] == "minimal":
                data.setdefault("low_effort", set()).add(model_id)
                options["reasoning_effort"] = "low"
            else:
                data.setdefault("no_effort", set()).add(model_id)
                options.pop("reasoning_effort")
            LOG.info("Adjusted effort compatibility for %s", model_id)


async def chat_stream(data, model_id, client, *, retries=0, on_retry=None, **options):
    """Retry a stalled model round only before it has emitted text or a tool call.

    Tool results stay in the caller's messages; already executed tools are never replayed.
    """
    def field(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    for attempt in range(retries + 1):
        stream, emitted = None, False
        slot = gateway_slot(model_id)
        await slot.acquire()  # held while the answer streams: the gateway counts open requests
        try:
            try:
                stream = await chat_completion(data, model_id, client, **options)
            except BadRequestError as exc:
                if not options.get("tools") or "tool" not in str(exc).lower():
                    raise
                provider = chat_provider(model_id)
                data.setdefault("no_tools", set()).add(provider)
                LOG.warning("%s refuses tools; answering without them", provider)
                options.pop("tools")
                stream = await chat_completion(data, model_id, client, **options)
            async for chunk in stream:
                for choice in field(chunk, "choices") or []:
                    delta = field(choice, "delta")
                    if delta and (field(delta, "content") or field(delta, "tool_calls")):
                        emitted = True
                yield chunk
            return
        except APIConnectionError:
            if emitted or attempt >= retries:
                raise
            LOG.warning("%s connection stalled before output; retrying model round once", model_id)
            if on_retry:
                on_retry()
        finally:
            slot.release()
            close = (getattr(stream, "close", None) or getattr(stream, "aclose", None)) if stream is not None else None
            if close:
                try:
                    await close()
                except Exception as exc:
                    LOG.warning("Closing model stream failed: %s", type(exc).__name__)


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
    if not data.get("openai"):
        try:
            return await quick_answer(data, data["search_model"], "Today is " + time.strftime("%Y-%m-%d") + ". Search the "
                                      "web and report the facts that answer the query: exact numbers with units and dates, "
                                      "and the source links. No small talk.", query, max_tokens=1500) or "Nothing found."
        except Exception as exc:
            LOG.warning("Web search with %s failed: %s", data.get("search_model"), type(exc).__name__)
            return "Web search is unavailable right now; answer from what you know and say the data may be outdated."
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
    if re.search(r"route-?llm", name):
        return (" You run on RouteLLM by Abacus.AI: a router that picks a suitable model (GPT, Claude, Gemini…) for each "
                "message. If someone asks which model you are, say so plainly, working inside the Secret AI Telegram bot.")
    if "claude" in name:
        pretty = "Claude " + " ".join(p.capitalize() for p in re.sub(r"^claude-", "", name).split("-") if not p.isdigit())
        version = ".".join(re.findall(r"-(\d+)(?=-|$)", name)[:2])
        return (f" You run on {pretty} {version}".rstrip() + ", a model made by Anthropic. If someone asks which model you "
                "are or who created you, say plainly that you are " + f"{pretty} {version}".strip() + " by Anthropic, "
                "working inside the Secret AI Telegram bot.")
    if "deepseek" in name:
        pretty = model_title(model)
        return (f" You run on {pretty.strip()}, a model made by DeepSeek. If someone asks which model you are or who created "
                f"you, say plainly that you are {pretty.strip()} by DeepSeek, working inside the Secret AI Telegram bot.")
    if "deepseek" in name and "free" in name:
        name = name.replace(":free", "")
    if "mimo" in name:
        pretty = "MiMo " + name.split(":")[0].split("mimo-", 1)[-1].replace("-", " ").upper().replace("FLASH", "Flash")
        return (f" You run on {pretty}, a model made by Xiaomi. If someone asks which model you are or who created you, "
                f"say plainly that you are {pretty} by Xiaomi, working inside the Secret AI Telegram bot.")
    if "kimi" in name:
        pretty = "Kimi " + "-".join(name.split("kimi-", 1)[-1].split("-")).upper().replace("-", " ")
        return (f" You run on {pretty}, a model made by Moonshot AI. If someone asks which model you are or who created you, "
                f"say plainly that you are {pretty} by Moonshot AI, working inside the Secret AI Telegram bot.")
    if "grok" in name:
        pretty = "Grok " + ".".join(re.findall(r"\d+", name.split("grok", 1)[1])[:2])
        return (f" You run on {pretty}, a model made by xAI. If someone asks which model you are or who created you, say "
                f"plainly that you are {pretty} by xAI, working inside the Secret AI Telegram bot.")
    if "gemini" in name:
        pretty = " ".join(p.capitalize() if not p[0].isdigit() else p for p in name.replace("gemini-", "gemini ").replace("-", " ").split())
        return (f" You run on {pretty}, a model made by Google. If someone asks which model you are or who created you, "
                f"say plainly that you are {pretty} by Google, working inside the Secret AI Telegram bot.")
    if name.startswith("gpt") or name.startswith("o"):
        pretty = name.upper().replace("GPT-", "GPT-").replace("-SOL", " Sol").replace("-LUNA", " Luna").replace("-TERRA", " Terra").replace("-ASTRA", " Astra")
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
    no_web = request.pop("no_web", False)
    if provider not in no_tools and not no_web and (context.bot_data.get("web_search") or context.bot_data.get("search_model")):
        tools.append(WEB_TOOL)
    # Connected apps are remote MCP servers, which only OpenAI calls by itself: here the bot does it for the model.
    servers = [d for d in toolbox.definitions() if d.get("type") == "mcp"] if toolbox and provider not in no_tools else []
    apps = await mcpbridge.load(context.bot_data["http"], servers) if servers else None
    if apps:
        tools += apps.functions
    text, incomplete = "", False
    started, first = time.monotonic(), None
    size = sum(len(str(m.get("content") or "")) for m in messages)
    for _ in range(request.pop("rounds", MAX_TOOL_ROUNDS)):
        options = {"model": model[len(provider):], "stream": True, "messages": messages,
                   "max_tokens": min(request.get("max_output_tokens", 8000), 32000)}
        if request.get("api_timeout"):
            options["timeout"] = httpx.Timeout(request["api_timeout"], connect=8)
        if tools and provider not in no_tools:
            options["tools"] = tools
        def retry_note():
            ru = getattr(live, "language", "ru") == "ru"
            live.status("Модель задерживается, повторяю запрос" if ru else "Model is taking longer; retrying request")
        stream = chat_stream(context.bot_data, model, client,
                             retries=request.get("stream_retries", 0), on_retry=retry_note, **options)
        said, calls, finish = "", {}, None
        field = lambda obj, key: obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)  # objects or raw dicts
        try:
            async for chunk in stream:
                if not field(chunk, "choices"):
                    continue
                choice = field(chunk, "choices")[0]
                delta = field(choice, "delta")  # some gateways end the stream with a chunk without a delta
                content = field(delta, "content") if delta is not None else None
                if isinstance(content, list):  # or send content as parts
                    content = "".join(str(field(p, "text") or "") for p in content)
                if content:
                    if first is None:
                        first = time.monotonic() - started
                    said += content
                    await live.update(prefix + text + said)
                for call in (field(delta, "tool_calls") if delta is not None else None) or []:
                    slot = calls.setdefault(field(call, "index") or 0, {"id": "", "name": "", "arguments": ""})
                    slot["id"] = field(call, "id") or slot["id"]
                    function = field(call, "function")
                    if function:
                        slot["name"] += field(function, "name") or ""
                        slot["arguments"] += field(function, "arguments") or ""
                finish = field(choice, "finish_reason") or finish
        except Exception as exc:
            # The gateway broke off after the answer was written (an error event at the end of the stream): keep the text.
            if not said.strip() or calls:
                raise
            LOG.warning("%s stream ended with %s after the answer: %s", model, type(exc).__name__, str(exc)[:200])
        finally:
            await stream.aclose()  # frees the gateway slot at once, even when the answer was stopped mid-way
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
            elif apps and apps.handles(call["name"]):
                live.status(f"Работаю с {apps.app(call['name'])}")
                try:
                    output = await apps.call(call["name"], args)
                except Exception as exc:
                    LOG.warning("App tool %s failed: %s %s", call["name"], type(exc).__name__, str(exc)[:200])
                    output = f"The app didn't answer ({type(exc).__name__}). Tell the user briefly."
            elif toolbox:
                live.status(toolbox.status(call["name"], args))
                output = await toolbox.run(call["name"], args)
            else:
                output = "This tool is not available here."
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(output)})
        if said.strip():
            text += "\n\n"
    live.status(None)
    LOG.info("%s: first text after %.1f s, done in %.1f s (prompt %d chars, %d tools)", model,
             first if first is not None else -1, time.monotonic() - started, size, len(tools))
    return (text if prefix else text.strip()), incomplete


async def discover_models(app):
    """Only the owner's catalog, using IDs confirmed by a configured gateway."""
    data = app.bot_data
    sources = ((ANYMODEL_PREFIX, "anymodel_chat"), (ABACUS_PREFIX, "abacus"), ("experiential:", "experiential"))

    async def catalog(prefix, name):
        if not data.get(name):
            return prefix, []
        try:
            result = await asyncio.wait_for(data[name].models.list(), 20)
            return prefix, [m.id for m in result.data]
        except Exception as exc:
            LOG.warning("%s model list unavailable: %s", name, type(exc).__name__)
            return prefix, []

    catalogs = await asyncio.gather(*(catalog(*source) for source in sources))
    models, backups = [], []
    for label, pattern, *first in MODEL_CATALOG:
        routes = []
        for prefix, ids in catalogs:
            found = pick_catalog(ids, pattern, cc_first=False)
            if found:
                routes.append(prefix + found)
        if first:  # this model's preferred gateway first (the others back it up)
            routes.sort(key=lambda route: not route.startswith(first[0]))
        if routes:
            models.append(routes[0])
            backups.extend(routes[1:])
            LOG.info("Chat model %s: %s", label, routes[0])
        else:
            LOG.warning("%s is unavailable in the configured catalogs; not offered", label)
    default = next((m for m in models if re.search(DEFAULT_PICK, m, re.I)), None)
    if not default and models:
        default = models[0]
        LOG.warning("%s unavailable; temporarily using %s", MODEL_CATALOG[0][0], default)
    # Migrate the previous global default once. Explicit per-user choices remain valid
    # only if they belong to this catalog. Old env flags cannot activate other models.
    store = data.get("connections")
    if store and default and store.get_meta("default_pick") != DEFAULT_PICK:
        store.set_meta("default_model", default)
        if re.search(DEFAULT_PICK, default, re.I):
            store.set_meta("default_pick", DEFAULT_PICK)
    saved = store.get_meta("default_model") if store else None
    if saved in models:
        default = saved
    fallbacks = list(dict.fromkeys(([default] if default else []) + models + backups))
    data.update(models=models, premium={}, fallbacks=fallbacks, cursor_models=[],
                model=default, chat_default=default, group_fast=None,
                vision_model=None, search_model=None, web_search=False)
    if not models:
        LOG.warning("No requested chat models available; check gateway keys and catalog access")


# Opus 4.6, Sonnet 4.6, Haiku 5.5 and Opus 5.5: no auto-router or guessed IDs.
MODEL_CATALOG = [  # the first one is the default for everyone
    ("Claude Opus 4.6", r"(?:^|[/:])claude[-._ ]opus[-._ ]4[-._ ]6(?:[-._ ]\d{6,})?$", "anymodel:"),  # the default, from AnyModel
    ("Claude Sonnet 4.6", r"(?:^|[/:])claude[-._ ]sonnet[-._ ]4[-._ ]6(?:[-._ ]\d{6,})?$"),
    ("Claude Haiku 5.5", r"(?:^|[/:])claude[-._ ]haiku[-._ ]5[-._ ]5(?:[-._ ]\d{6,})?$"),
    ("Claude Opus 5.5", r"(?:^|[/:])claude[-._ ]opus[-._ ]5[-._ ]5(?:[-._ ]\d{6,})?$"),
]
DEFAULT_PICK = MODEL_CATALOG[0][1]
ENABLED_MODELS = []  # Cursor must not add other models to the picker.


def pick_catalog(ids, pattern, cc_first=True):
    """The best id in a catalog for a pattern: a plain one before fast/thinking/preview/dated variants, cc/ routes first."""
    found = [i for i in ids if re.search(pattern, i, re.I) and not re.search(r"image|audio|tts|embed|vision", i, re.I)]
    found.sort(key=lambda i: (bool(re.search(r"fast|mini|lite|flash", i, re.I)), "thinking" in i.lower(), "preview" in i.lower(),
                              i.startswith("cc/") != cc_first, bool(re.search(r"\d{6,}", i)), len(i)))  # cc/ first, or last
    return found[0] if found else None


class NoModels(Exception):
    """No model is switched on."""


# Paid models, like Mira's: each answer costs credits, and credits are only bought with Telegram Stars.
# key: (how to find it in the catalogs, credits per answer). PREMIUM_PRICES="fable=6,opus=5,astra=4" overrides prices.
PREMIUM = {}  # No paid chat models until the owner adds them again.


def premium_prices():
    prices = {key: price for key, (_, price) in PREMIUM.items()}
    for pair in os.environ.get("PREMIUM_PRICES", "").split(","):
        key, _, value = pair.partition("=")
        if key.strip().lower() in prices and value.strip().isdigit():
            prices[key.strip().lower()] = max(1, int(value))
    return prices


def find_premium(openai_ids, abacus_ids, any_ids):
    """{model id: credits per answer} for the paid models this account really has: Claude from Abacus/AnyModel first, GPT from
    OpenAI first."""
    found, prices = {}, premium_prices()
    for key, (pattern, _) in PREMIUM.items():
        # Opus only through Abacus; Fable and Astra through Abacus, then AnyModel; OpenAI only when it's switched on.
        sources = {"opus": ((ABACUS_PREFIX, abacus_ids),)}.get(key, ((ABACUS_PREFIX, abacus_ids), (ANYMODEL_PREFIX, any_ids), ("", openai_ids)))
        for prefix, ids in sources:
            hits = sorted((i for i in ids if pattern.search(i) and not re.search(r"mini|nano|image|audio|\d{4}-\d\d-\d\d", i)),
                          key=lambda i: ("thinking" in i.lower(), "preview" in i.lower(), len(i)))
            if hits:
                found[prefix + hits[0]] = prices[key]
                LOG.info("Paid model %s: %s (%s credits an answer)", key, prefix + hits[0], prices[key])
                break
        else:
            LOG.warning("Paid model %s is in none of the catalogs; not offered", key)
    return found


def free_models(data):
    return [m for m in data.get("models", ()) if m not in data.get("premium", {})]


NANO = re.compile(r"nano[-_ ]?banana", re.I)


def pick_nano(ids):
    """The best Nano Banana the account has: a Pro one first, otherwise the newest."""
    nano = sorted(i for i in ids if NANO.search(i))
    pro = [i for i in nano if "pro" in i.lower()]
    return (pro or nano or ["nano_banana_pro"])[-1]


async def resolve_abacus_model(app):
    """Find Nano Banana (pictures) in RouteLLM's model list; chat discovery is separate."""
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
    if not request.get("model"):
        raise NoModels()
    if request.pop("light", False):  # small talk on a slow gateway: no tool definitions to read, a faster first word
        toolbox, request["no_web"] = None, True
    if request["model"].startswith(cursor.PREFIX):  # the Cursor CLI: text in, streamed text out, no tools
        async def on_text(text):
            await live.update(text)
        live.status("Cursor")
        text = await cursor.answer(request["model"], (request.get("instructions") or "") + model_identity(request["model"]),
                                   request["input"], on_text)
        live.status(None)
        return text, False
    if not chat_provider(request["model"]) and not context.bot_data.get("openai"):
        request["model"] = context.bot_data.get("chat_default") or request["model"]  # OpenAI is off: never call it
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
    for _ in range(request.pop("rounds", MAX_TOOL_ROUNDS)):
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
        voice = (await welcome.speech(data["client"], data["tts_model"], data["tts_voice"], welcome.TEXT["ru"]) if data.get("openai")
                 else await speech.edge_speech(welcome.TEXT["ru"]))
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


PROMO_TRIES = 8  # wrong codes a person may try per hour (no guessing codes by brute force)


def promo_clean(code):
    return re.sub(r"[^A-Z0-9_-]", "", (code or "").upper())[:32]


def redeem_promo(data, user_id, code, ru):
    """Apply a code for a person; returns (ok, text)."""
    code = promo_clean(code)
    tries = [t for t in data.setdefault("promo_tries", {}).get(user_id, []) if time.monotonic() - t < 3600]
    data["promo_tries"][user_id] = tries
    if len(tries) >= PROMO_TRIES:
        return False, ("Слишком много попыток. Попробуй через час." if ru else "Too many tries. Try again in an hour.")
    if not code:
        return False, ("Напиши промокод: /promo КОД" if ru else "Send the code: /promo CODE")
    result, credits = data["memory"].promo_redeem(user_id, code)
    if result == "ok":
        LOG.info("Promo code %s redeemed: +%s credits", code, credits)
        total = data["memory"].gen_info(user_id)[1]
        return True, (f"🎁 Промокод активирован: +{credits} кредитов. Теперь у тебя {total}." if ru
                      else f"🎁 Code activated: +{credits} credits. You now have {total}.")
    if result in ("unknown", "expired"):
        tries.append(time.monotonic())
    return False, {"unknown": ("Такого промокода нет." if ru else "There's no such code."),
                   "expired": ("Срок действия промокода закончился." if ru else "This code has expired."),
                   "empty": ("Промокод уже закончился." if ru else "This code has run out."),
                   "used": ("Ты уже активировал этот промокод." if ru else "You've already used this code.")}[result]


def promo_button(text, data, style=None):
    """A coloured inline button: style "primary" is blue, "success" green, "danger" red."""
    return InlineKeyboardButton(text, callback_data=data, **({"style": style} if style else {}))


PROMO_CREDITS = (10, 25, 50, 100, 250, 500)
PROMO_PEOPLE = (10, 50, 100, 500, 1000)
PROMO_DAYS = (1, 3, 7, 30)


def promo_menu(owner, ru=True):
    rows = []
    if owner:
        rows.append([promo_button("➕ Создать промокод" if ru else "➕ Create a code", "pr:new", "success")])
        rows.append([promo_button("📋 Мои промокоды" if ru else "📋 My codes", "pr:list", "primary")])
    rows.append([promo_button("🎟 Ввести промокод" if ru else "🎟 Enter a code", "pr:enter", "primary")])
    return InlineKeyboardMarkup(rows)


def promo_choices(values, prefix, extra, per_row=3):
    """Number buttons (blue), the extra ones (blue), then a red Cancel."""
    buttons = [promo_button(str(v), f"{prefix}{v}", "primary") for v in values]
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    rows += [[promo_button(text, data, "primary")] for text, data in extra]
    rows.append([promo_button("❌ Отмена", "pr:cancel", "danger")])
    return InlineKeyboardMarkup(rows)


def promo_summary(w):
    return (f"🎁 Промокод {w['code']}\n"
            f"💎 Кредитов: {w['credits']}\n"
            f"👥 Людей: {w['people'] or 'без ограничений'}\n"
            f"📅 Срок: {str(w['days']) + ' дн.' if w['days'] else 'бессрочно'}")


async def promo_step(target, w, edit=False):
    """Show the wizard's current step (edit the message after a button, or send a new one after typed text)."""
    step = w["step"]
    if step == "code":
        text = ("🎁 Новый промокод\n\nШаг 1 из 4. Напиши название промокода сообщением "
                "(буквы и цифры, например START), или нажми «Придумать за меня».")
        markup = InlineKeyboardMarkup([[promo_button("🎲 Придумать за меня", "pr:rnd", "primary")],
                                       [promo_button("❌ Отмена", "pr:cancel", "danger")]])
    elif step == "credits":
        text = f"🎁 {w['code']}\n\nШаг 2 из 4. Сколько кредитов даёт промокод?"
        markup = promo_choices(PROMO_CREDITS, "pr:c:", [("✏️ Своё число", "pr:type")])
    elif step == "people":
        text = f"🎁 {w['code']} · {w['credits']} кр.\n\nШаг 3 из 4. Сколько человек могут его активировать?"
        markup = promo_choices(PROMO_PEOPLE, "pr:p:", [("♾ Без ограничений", "pr:p:0"), ("✏️ Своё число", "pr:type")])
    elif step == "days":
        text = (f"🎁 {w['code']} · {w['credits']} кр. · {w['people'] or '∞'} чел.\n\n"
                "Шаг 4 из 4. Сколько дней он действует?")
        markup = promo_choices(PROMO_DAYS, "pr:d:", [("♾ Бессрочно", "pr:d:0"), ("✏️ Своё число", "pr:type")], per_row=4)
    else:  # confirm
        text = promo_summary(w) + "\n\nВсё верно?"
        markup = InlineKeyboardMarkup([[promo_button("✅ Создать", "pr:ok", "success")],
                                       [promo_button("❌ Отмена", "pr:cancel", "danger")]])
    if w.get("typing"):
        text += "\n\n✏️ Напиши число сообщением."
    if edit:
        await rich_edit(target, text, markup)
    else:
        await reply(target, text, markup)


def promo_list_view(memory):
    rows = memory.promo_list()
    if not rows:
        return "Промокодов пока нет.", InlineKeyboardMarkup([[promo_button("➕ Создать промокод", "pr:new", "success")]])
    lines, buttons = [], []
    for code, credits, limit, used, expires in rows[:20]:
        state = (" · истёк" if expires and expires < time.time() else
                 f" · до {time.strftime('%d.%m', time.localtime(expires))}" if expires else "")
        lines.append(f"• {code} — {credits} кр., активаций {used}" + (f"/{limit}" if limit else "") + state)
        buttons.append([promo_button(f"🗑 Удалить {code}", f"pr:del:{code}", "danger")])
    buttons.append([promo_button("➕ Создать промокод", "pr:new", "success")])
    return "📋 Твои промокоды:\n\n" + "\n".join(lines), InlineKeyboardMarkup(buttons)


async def promo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/promo: a menu with buttons (the owner creates codes step by step). /promo CODE applies a code at once."""
    if not await authorized(update, context):
        return
    data, user_id, args = context.bot_data, update.effective_user.id, context.args or []
    ru = lang(update) == "ru"
    if args:
        _, text = redeem_promo(data, user_id, args[0], ru)
        await send(update.effective_message, text)
        return
    owner = data["connections"].get_meta("owner") == user_id
    data.setdefault("promo_wizard", {}).pop(user_id, None)
    await reply(update.effective_message,
        ("🎁 Промокоды" + ("\n\nСоздавай коды на кредиты для своих пользователей." if owner else
                          "\n\nЕсть промокод? Нажми кнопку и отправь его.")) if ru else
        "🎁 Promo codes", promo_menu(owner, ru))


async def promo_button_press(update, context, query, action):
    """The pr:… buttons. Returns nothing; always answers the query."""
    data, user_id, ru = context.bot_data, update.effective_user.id, lang(update) == "ru"
    owner = data["connections"].get_meta("owner") == user_id
    wizards = data.setdefault("promo_wizard", {})
    w = wizards.get(user_id)
    if action == "enter":
        wizards[user_id] = {"step": "redeem"}
        await query.answer()
        await rich_edit(query, "🎟 Отправь промокод сообщением." if ru else "🎟 Send the code as a message.",
                                      reply_markup=InlineKeyboardMarkup([[promo_button("❌ Отмена", "pr:cancel", "danger")]]))
        return
    if action == "cancel":
        wizards.pop(user_id, None)
        await query.answer()
        await rich_edit(query, "Отменено." if ru else "Cancelled.")
        return
    if not owner:
        await query.answer()
        return
    if action == "new":
        wizards[user_id] = w = {"step": "code", "credits": 0, "people": 0, "days": 0}
        await query.answer()
        await promo_step(query, w, edit=True)
        return
    if action == "list":
        await query.answer()
        text, markup = promo_list_view(data["memory"])
        await rich_edit(query, text, reply_markup=markup)
        return
    if action.startswith("del:"):
        code = promo_clean(action[4:])
        data["memory"].promo_delete(code)
        await query.answer(f"Удалён {code}")
        text, markup = promo_list_view(data["memory"])
        await rich_edit(query, text, reply_markup=markup)
        return
    if not w or w["step"] == "redeem":
        await query.answer("Начни заново: /promo")
        return
    if action == "rnd" and w["step"] == "code":
        w.update(code=secrets.token_hex(3).upper(), step="credits")
    elif action == "type":
        w["typing"] = True
    elif action.startswith(("c:", "p:", "d:")) and action[2:].isdigit():
        field, value = {"c": "credits", "p": "people", "d": "days"}[action[0]], int(action[2:])
        if field == "credits" and value <= 0:
            await query.answer()
            return
        w[field] = value
        w["step"] = {"credits": "people", "people": "days", "days": "confirm"}[field]
        w.pop("typing", None)
    elif action == "ok" and w["step"] == "confirm":
        data["memory"].promo_create(w["code"], w["credits"], w["people"], w["days"] or None)
        wizards.pop(user_id, None)
        await query.answer("Создан ✅")
        await rich_edit(query, f"✅ Промокод создан!\n\n{promo_summary(w)}\n\n"
                                      f"Как активировать: /promo {w['code']} или Mini App → Генерация → Промокод.",
                                      reply_markup=InlineKeyboardMarkup([[promo_button("📋 Мои промокоды", "pr:list", "primary")],
                                                                         [promo_button("➕ Ещё один", "pr:new", "success")]]))
        return
    await query.answer()
    await promo_step(query, w, edit=True)


async def promo_typed(update, context):
    """A typed message while the promo menu waits for text (a code to apply, or the owner's code name/number).
    True if it was taken."""
    data, user_id = context.bot_data, update.effective_user.id
    w = data.get("promo_wizard", {}).get(user_id)
    message = update.effective_message
    if not w or not message.text or message.text.startswith("/"):
        return False
    text, ru = message.text.strip(), lang(update) == "ru"
    if w["step"] == "redeem":
        ok, answer = redeem_promo(data, user_id, text, ru)
        if ok or len(text) > 32:
            data["promo_wizard"].pop(user_id, None)
        await send(message, answer)
        return True
    if w["step"] == "code":
        code = promo_clean(text)
        if len(code) < 3:
            await send(message, "Название — от 3 букв или цифр, латиницей. Например START")
            return True
        w.update(code=code, step="credits")
    elif w.get("typing") and w["step"] in ("credits", "people", "days"):
        if not text.isdigit() or (w["step"] == "credits" and int(text) <= 0) or int(text) > 1_000_000:
            await send(message, "Нужно целое число, например 75.")
            return True
        field = w["step"]
        w[field] = int(text)
        w["step"] = {"credits": "people", "people": "days", "days": "confirm"}[field]
        w.pop("typing", None)
    else:
        return False  # a button is expected: the message goes to the chat as usual
    await promo_step(message, w)
    return True


# ---------- Broadcast: the owner sends one message to every private chat and every group the bot is in ----------
BROADCAST_RATE = 0.05  # 20 messages a second: under Telegram's ~30/s limit for bots


def broadcast_targets(data, who):
    """Chat ids: people who wrote the bot privately, and the groups it has been in."""
    users, groups = [], []
    if who in ("all", "users"):
        memory = data["memory"]
        rows = memory.db.execute("SELECT DISTINCT user_id FROM messages UNION SELECT user_id FROM settings").fetchall()
        users = sorted({row[0] for row in rows if row[0] and row[0] > 0})
    if who in ("all", "groups"):
        groups = sorted({row[0] for row in data["groups"].db.execute("SELECT chat_id FROM group_info").fetchall() if row[0] < 0})
    return users, groups


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast: the owner's next message (text, photo, video…) goes to everyone. /broadcast TEXT sends that text."""
    if not await authorized(update, context):
        return
    data, user_id = context.bot_data, update.effective_user.id
    if data["connections"].get_meta("owner") != user_id:
        await send(update.effective_message, "Это может только владелец бота.")
        return
    text = update.effective_message.text.partition(" ")[2].strip() if update.effective_message.text else ""
    if text:
        data.setdefault("broadcast", {})[user_id] = {"text": text}
        await broadcast_preview(update.effective_message, data, user_id)
        return
    data.setdefault("broadcast", {})[user_id] = {"step": "wait"}
    await reply(update.effective_message, "📣 Рассылка\n\nОтправь сообщение, которое нужно разослать: текст, фото, видео, "
                                          "голосовое — что угодно, оно уйдёт как есть.",
                InlineKeyboardMarkup([[promo_button("❌ Отмена", "bc:cancel", "danger")]]))


async def broadcast_preview(message, data, user_id):
    users, groups = broadcast_targets(data, "all")
    await reply(message, f"📣 Кому отправить?\n\n👤 В личку: {len(users)} чел.\n👥 В группы: {len(groups)}",
                InlineKeyboardMarkup([
                    [promo_button(f"✅ Всем ({len(users) + len(groups)})", "bc:all", "success")],
                    [promo_button(f"👤 Только в личку ({len(users)})", "bc:users", "primary"),
                     promo_button(f"👥 Только в группы ({len(groups)})", "bc:groups", "primary")],
                    [promo_button("❌ Отмена", "bc:cancel", "danger")]]))


async def broadcast_typed(update, context):
    """The owner's message after /broadcast becomes the broadcast. True if taken."""
    data, user_id = context.bot_data, update.effective_user.id
    pending = data.get("broadcast", {}).get(user_id)
    message = update.effective_message
    if not pending or pending.get("step") != "wait" or (message.text or "").startswith("/"):
        return False
    data["broadcast"][user_id] = {"copy": (message.chat_id, message.message_id)}
    await broadcast_preview(message, data, user_id)
    return True


async def broadcast_button(update, context, query, action):
    data, user_id = context.bot_data, update.effective_user.id
    if data["connections"].get_meta("owner") != user_id:
        await query.answer()
        return
    pending = data.get("broadcast", {}).pop(user_id, None)
    if action == "cancel" or not pending or pending.get("step") == "wait":
        await query.answer()
        await rich_edit(query, "Рассылка отменена.")
        return
    users, groups = broadcast_targets(data, action)
    await query.answer("Отправляю…")
    await rich_edit(query, f"📣 Рассылка началась: {len(users) + len(groups)} чатов. Пришлю отчёт, когда закончу.")
    task = asyncio.create_task(run_broadcast(context.bot, data, user_id, pending, users, groups))
    data.setdefault("broadcast_tasks", set()).add(task)
    task.add_done_callback(data["broadcast_tasks"].discard)


async def run_broadcast(bot, data, owner, pending, users, groups):
    sent, failed = {"users": 0, "groups": 0}, {"users": 0, "groups": 0}
    for kind, chats in (("users", users), ("groups", groups)):
        for chat_id in chats:
            for attempt in range(2):
                try:
                    if "copy" in pending:
                        await bot.copy_message(chat_id, pending["copy"][0], pending["copy"][1])
                    else:
                        await bot.send_message(chat_id, pending["text"])
                    sent[kind] += 1
                    break
                except RetryAfter as exc:  # Telegram asks to slow down: wait and try this chat once more
                    await asyncio.sleep(seconds(exc.retry_after) + 1)
                    if attempt:
                        failed[kind] += 1
                except (Forbidden, BadRequest):  # blocked the bot, left the group, chat gone
                    failed[kind] += 1
                    break
                except TelegramError as exc:
                    LOG.warning("Broadcast to a chat failed: %s", type(exc).__name__)
                    failed[kind] += 1
                    break
            await asyncio.sleep(BROADCAST_RATE)
    LOG.info("Broadcast done: %s sent, %s failed", sent, failed)
    try:
        await bot.send_message(owner, "📣 Рассылка готова\n\n"
                               f"👤 В личку: доставлено {sent['users']}, не дошло {failed['users']}\n"
                               f"👥 В группы: доставлено {sent['groups']}, не дошло {failed['groups']}\n\n"
                               "«Не дошло» — человек заблокировал бота или бота убрали из группы.")
    except TelegramError:
        pass


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
        if groups.db.execute("SELECT 1 FROM sqlite_master WHERE name='group_serial'").fetchone():
            groups.db.execute("DELETE FROM group_serial")


def owner_code(data):
    """With no owner yet: a one-time code, printed only in the server console, that /owner exchanges for ownership."""
    if data["connections"].get_meta("owner"):
        data.pop("owner_code", None)
        return None
    if not data.get("owner_code"):
        data["owner_code"] = secrets.token_hex(5)
        LOG.warning("No bot owner yet. To become the owner, send the bot in a private chat: /owner %s "
                    "(or set OWNER_ID in .env)", data["owner_code"])
    return data["owner_code"]


async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/owner <code>: become the owner with the code from the server console (only while there is no owner)."""
    if not await authorized(update, context):
        return
    data, user_id = context.bot_data, update.effective_user.id
    if data["connections"].get_meta("owner"):
        text = ui(update, "Владелец уже назначен." if data["connections"].get_meta("owner") != user_id else "Ты уже владелец 👑",
                  "The bot already has an owner." if data["connections"].get_meta("owner") != user_id else "You're already the owner 👑")
        await send(update.effective_message, text)
        return
    expected, given = owner_code(data), (context.args[0] if context.args else "").strip()
    if not expected or not given or not hmac.compare_digest(given, expected):
        await send(update.effective_message, ui(update, "Неверный код. Код владельца есть только в консоли сервера.",
                                                "Wrong code. The owner code is only in the server console."))
        return
    data["connections"].set_meta("owner", user_id)
    data.pop("owner_code", None)
    LOG.info("Bot owner set: %s", user_id)
    await send(update.effective_message, ui(update, "👑 Готово, теперь ты владелец бота.", "👑 Done, you're the bot's owner now."))


def learn_premium(context, message):
    """The owner's custom emoji in a message: remembered as the bot's own for those plain emoji. Returns what was learned."""
    learned = []
    for entity in (*(message.entities or ()), *(message.caption_entities or ())):
        if entity.type != MessageEntity.CUSTOM_EMOJI or not entity.custom_emoji_id:
            continue
        plain = emoji_key(message.parse_entity(entity) if message.text else message.parse_caption_entity(entity))
        if valid_emoji_key(plain):
            PREMIUM_EMOJI[plain] = entity.custom_emoji_id
            learned.append(plain)
    if learned:
        context.bot_data["connections"].set_meta("premium_emoji", PREMIUM_EMOJI)
    return learned


async def emoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/emoji: how to give the bot premium emoji, and the ones it uses. /emoji reset forgets them."""
    if not await authorized(update, context):
        return
    store = context.bot_data["connections"]
    if store.get_meta("owner") != update.effective_user.id:
        await send(update.effective_message, "Это может только владелец бота.")
        return
    if context.args and context.args[0].lower() in ("reset", "сброс", "clear"):
        PREMIUM_EMOJI.clear()
        store.set_meta("premium_emoji", {})
        await send(update.effective_message, "Забыл все премиум-эмодзи, теперь обычные.")
        return
    known = " ".join(PREMIUM_EMOJI) or "пока нет"
    # Emoji configuration is UI state, not a conversation turn for the model.
    await send(update.effective_message,
               "✨ Премиум-эмодзи бота\n\nОтправь мне сообщение с премиум-эмодзи — можно сразу много, например "
               "✅ ❌ 🎁 💎 ⭐ 🔥 🎨 🎬 👑 ⚡ 📋 🗑 ➕ 🎲 🎟 ⏱ 🔖 📝 🌐 🧹 ⚠️ 🤖 — и я буду ставить их вместо обычных во всех "
               "своих сообщениях и на кнопках.\n\nУже знаю: " + known + "\n\n/emoji reset — вернуть обычные.")


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await authorized(update, context):
        await send(update.effective_message, f"Telegram ID: {update.effective_user.id}")


async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/version: which update is running and for how long — to see that a new version arrived."""
    if not await authorized(update, context):
        return
    sha, subject, date = await asyncio.to_thread(hosting.version)
    minutes = int((time.time() - hosting.STARTED) // 60)
    uptime = f"{minutes // 60} ч {minutes % 60} мин" if minutes >= 60 else f"{minutes} мин"
    branch = os.environ.get("UPDATE_BRANCH", "main").strip() or "main"
    text = (("🧪 <b>Тестовый бот</b> — тут всё новое до выпуска\n" if hosting.is_test_bot() else "")
            + f"🔖 Версия: <code>{sha or '?'}</code>" + (f" от {date}" if date else "")
            + (f"\n📝 {html.escape(subject[:150])}" if subject else "") + f"\n⏱ Работает: {uptime}"
            + (f"\n🔄 Обновляется с GitHub сам (ветка {branch}), раз в 5 минут" if hosting.updates_enabled() else "")
            + (f"\n🌐 Сайт: {context.bot_data['public_url']}" if context.bot_data.get("public_url") else ""))
    await update.effective_message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


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
    client = context.bot_data["stt"]
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
            shown = ui(update, "Не удалось распознать речь. Запишите ещё раз.", "No speech recognized. Please try again.")
            await status.edit_text(shown)
            remember_reply(context, update, shown)
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
    url = links.find(question)
    if url:  # a TikTok link: watch it first, then answer about it
        return await read_tiktok(update, context, question, url)
    return question, question, question


async def read_tiktok(update, context, question, url):
    message = update.effective_message
    status = await reply(message, ui(update, "🎬 Смотрю видео…", "🎬 Watching the video…"))
    try:
        text, frames, label = await links.watch(context.bot_data["http"], context.bot_data["stt"], url,
                                                 (update.effective_user.language_code or "ru")[:2])
    except Exception as exc:  # a removed video, a private account or tikwm being down
        LOG.warning("TikTok link failed: %s %s", type(exc).__name__, str(exc)[:400])
        note = (f"{question}\n\n[The bot could not open this TikTok link (deleted, private or the service is down). "
                "Say so briefly and ask to send the video itself as a file instead.]")
        return question, note, question
    finally:
        try:
            await status.delete()
        except TelegramError:
            pass
    LOG.info("Watched a TikTok: %d frames, %d chars", len(frames), len(text))
    return question, links.content(text, question, frames), f"{question} {label}"


def model_title(model):
    if re.search(r"route-?llm", model, re.I):
        return "RouteLLM"
    return _model_title(model)


def _model_title(model):
    """claude-opus-5-5 -> Claude Opus 5.5, gpt-6-sol -> GPT-6 Sol, gemini-4-argon -> Gemini 4 Argon."""
    name = re.sub(r"-(\d{4}|\d{6,8}|latest|preview)$", "", model.split(":", 1)[-1].split("/")[-1].removesuffix(":free"))
    gpt = re.match(r"gpt-([\d.]+)", name, re.I)
    words = name.split("-")
    out = []
    for word in words:
        if out and word.isdigit() and out[-1].replace(".", "").isdigit():
            out[-1] += "." + word  # 5-5 -> 5.5
        else:
            out.append(word)
    text = " ".join(w if w[:1].isdigit() else w.capitalize() for w in out)
    text = text.replace("Deepseek", "DeepSeek").replace("Mimo", "MiMo")
    return re.sub(r"^Gpt ([\d.]+)", r"GPT-\1", text) if gpt else text


async def premium_offer(context, update, model, price):
    """No credits for a paid model: the price and Stars packs to buy right here (credits are only sold for Stars)."""
    user_id, ru = update.effective_user.id, lang(update) == "ru"
    credits = context.bot_data["memory"].gen_info(user_id)[1]
    rows = []
    for pack, (amount, stars) in PACKS.items():
        try:
            url = await context.bot.create_invoice_link(
                title=f"{amount} кредитов" if ru else f"{amount} credits",
                description=(f"Кредиты Secret AI: платные модели, фото и видео" if ru else "Secret AI credits: paid models, photos and videos"),
                payload=f"gen:{pack}:{user_id}", currency="XTR", prices=[LabeledPrice(f"{amount} credits", stars)])
        except TelegramError as exc:
            LOG.warning("Invoice link failed: %s", type(exc).__name__)
            continue
        rows.append([InlineKeyboardButton(f"⭐ {stars} → {amount} " + ("кредитов" if ru else "credits") + f" (~{amount // price})", url=url)])
    title = model_title(model)
    text = (f"💎 {title} — платная модель: {price} кредитов за ответ. У тебя {credits}.\n\nКредиты покупаются только за звёзды "
            "Telegram. Или выбери бесплатную модель в Secret AI (вверху Mini App)." if ru else
            f"💎 {title} is a paid model: {price} credits an answer. You have {credits}.\n\nCredits are bought with Telegram "
            "Stars only. Or pick a free model in Secret AI (top of the Mini App).")
    return text, InlineKeyboardMarkup(rows) if rows else None


def drop_model(data, model, exc):
    """A model the gateway won't serve at all (no access, no such model, no credits left): out of the menu and the
    fallbacks until restart, so nobody waits for its refusal before every answer."""
    out_of_credits = isinstance(exc, (RateLimitError, APIError)) and re.search(
        r"no remaining credits|insufficient[_ ]?(quota|balance|credits)|out of credits|balance", str(exc), re.I)
    if (not isinstance(exc, (PermissionDeniedError, AuthenticationError, NotFoundError)) and not out_of_credits) \
            or model not in data.get("models", []):
        return
    data["models"] = [m for m in data["models"] if m != model]
    data["fallbacks"] = [m for m in data.get("fallbacks", []) if m != model]
    data.get("premium", {}).pop(model, None)
    if not data["models"] and data["fallbacks"]:  # a backup copy of the same model (another gateway) takes its place
        data["models"] = data["fallbacks"][:1]
    if data.get("chat_default") == model:
        data["chat_default"] = data["model"] = (data["fallbacks"] or data["models"] or [None])[0]
    LOG.warning("%s is not available (%s): removed from the menu until restart", model, type(exc).__name__)


def models_for(bot_data, user_id):
    """The models this person may pick: Cursor ones (a personal subscription) only for the owner unless CURSOR_PUBLIC=1."""
    extra = bot_data.get("cursor_models") or []
    if extra and not cursor.public() and bot_data["connections"].get_meta("owner") != user_id:
        extra = []
    return list(bot_data["models"]) + extra


def user_model(bot_data, user_id):
    chosen = bot_data["memory"].model(user_id)
    if chosen and chosen.startswith(cursor.PREFIX) and chosen in models_for(bot_data, user_id):
        return chosen
    if chosen and chosen not in bot_data["models"]:  # the same model now served by another gateway (e.g. Opus moved off OpenAI)
        bare = chosen.split(":", 1)[-1].split("/")[-1]
        chosen = next((m for m in bot_data["models"] if m.split(":", 1)[-1].split("/")[-1] == bare), None)
    if chosen in bot_data["models"]:
        return chosen
    # Nothing chosen (or gone): the default model, or the first one this person may use (e.g. the owner's Cursor model).
    return bot_data.get("chat_default") or next(iter(models_for(bot_data, user_id)), None) or bot_data["model"]


# Connected apps cost seconds before every answer (Pipedream lookups, MCP handshakes): they load only when the message is
# about them. Small talk goes straight to the model.
APP_WORDS = re.compile(
    r"почт|письм|имейл|e-?mail|gmail|inbox|ящик|календар|calendar|встреч|событи|созвон|notion|ноушн|заметк|drive|диск|"
    r"документ|docs|таблиц|sheet|excel|youtube|ютуб|spotify|спотиф|плейлист|github|гитхаб|репозитор|todoist|задач|"
    r"trello|slack|linear|jira|asana|figma|dropbox|контакт|crm|сделк|клиент|прилож|подключ|интеграц|mcp", re.I)


ANYMODEL_HISTORY_CHARS = 9000  # AnyModel models read a shorter history: every 1000 characters cost them time
# Messages that need a tool (memory, the secretary, edits, search, media, links); anything else is small talk.
TOOL_WORDS = re.compile(
    r"запомн|забуд|помнишь|секретар|автоответ|отмени|останов|измени|отредакт|исправ|найди|поищи|загугл|поиск|гугл|"
    r"новост|погод|курс|сегодня|сейчас|вчера|завтра|цен[аыу]|стоит|сколько стоит|актуальн|последн|2026|2027|"
    r"нарису|картин|фото|видео|сгенер|озвуч|голос|ссылк|http|www\.|\.com|\.ru|github|репо|код|сайт|"
    r"remember|forget|search|find|news|weather|price|today|draw|picture|video|link|code|website", re.I)


def needs_tools(text):
    text = text or ""
    return bool(TOOL_WORDS.search(text)) or asks_for_media(text)


APPS_FOLLOW_UP = 600  # seconds after an apps request when short follow-ups ("теперь удали второй") keep the apps


def wants_apps(bot_data, user_id, text):
    """Does this message need the connected apps? Their names, words about mail, calendars, files, tasks… or it comes
    soon after a message that did: a follow-up names no app, but it continues the same work."""
    return apps_named(bot_data, user_id, text) or bot_data.setdefault("apps_until", {}).get(user_id, 0) > time.monotonic()


def apps_named(bot_data, user_id, text):
    """The message itself is about the apps (only this opens the follow-up window, so small talk lets it close)."""
    text = text or ""
    names = bot_data.setdefault("app_names", {}).get(user_id) or []
    return bool(APP_WORDS.search(text)) or any(name.lower()[:5] in text.lower() for name in names if len(name) >= 4)


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


def recall_note(found):
    """Old parts of the conversation, for the instructions: the bot remembers them like a friend would."""
    if not found:
        return ""
    lines = []
    for created, role, content in found:
        text = " ".join(str(content).split())
        if len(text) > RECALL_CHARS:
            text = text[:RECALL_CHARS] + " …"
        lines.append(f"[{ago(created)}] {'User' if role == 'user' else 'You'}: {text}")
    return ("\n\nFrom your earlier conversations with this user (older than the chat history you see; you remember "
            "them — bring them up naturally when they matter, never say you searched):\n" + "\n".join(lines))


def fit_history(history, limit=None):
    """The newest messages in full, older long ones shortened, all within HISTORY_CHARS (oldest dropped first)."""
    kept, total, limit = [], 0, limit or HISTORY_CHARS
    for index, item in enumerate(reversed(history)):
        text = item["content"]
        if index >= 6 and len(text) > HISTORY_ITEM_CHARS:
            text = text[:HISTORY_ITEM_CHARS] + " …"
        if total + len(text) > limit:
            break
        kept.append({"role": item["role"], "content": text})
        total += len(text)
    kept.reverse()
    while kept and kept[0]["role"] != "user":  # start with a question, not an orphaned answer
        kept.pop(0)
    return kept


GREETING = re.compile(
    r"(?:(?:привет(?:ик)?|здравствуй(?:те)?|салам|салям|хай|здарова|доброе утро|добрый (?:день|вечер)|hello|hi|hey)"
    r"(?: (?:как (?:ты|дела|сам)|как у тебя дела|how are you|how s it going))?"
    r"|как (?:ты|дела|сам)|как у тебя дела|how are you)", re.I)


def is_greeting(text):
    """Only standalone greetings, never a substantive question prefaced with hello."""
    normalized = emoji_lib.replace_emoji(text or "", replace=" ")
    normalized = re.sub(r"[,.!?…:;'’—–-]", " ", normalized)
    return bool(GREETING.fullmatch(" ".join(normalized.split())))


@counted
async def respond(update, context, target, content, saved_question, voice=False):
    """Answer in the chat; voice=True (the user sent a voice message) also sends the answer as a voice message."""
    user_id = update.effective_user.id
    if not models_for(context.bot_data, user_id):
        await send(target, ui(update, *error_text(NoModels())))
        return
    memory = context.bot_data["memory"]
    persona = PERSONAS[memory.persona(user_id)]
    prune_pending(context.bot_data["pending"])
    # The logo shows at once (or "recording a voice message…" for voice); tokens and apps are prepared meanwhile.
    if voice and context.bot_data.get("tts_model"):
        live = SilentReply(target)
        await live.start()
    else:
        live = await open_reply(target, lang(update), context.bot_data)
    live = CleanChatReply(live)
    try:
        prepared = time.monotonic()
        use_apps = wants_apps(context.bot_data, user_id, saved_question)
        apps = ([], [])
        if use_apps:
            taken = [server["label"] for server in context.bot_data["connections"].public(user_id) if server["service"] == "mcp"]
            _, apps = await asyncio.gather(context.bot_data["github_apps"].fresh_token(context.bot_data["http"], user_id),
                                           app_tools(context.bot_data, user_id, taken))
            context.bot_data.setdefault("app_names", {})[user_id] = list(apps[1])
        if apps_named(context.bot_data, user_id, saved_question):
            context.bot_data.setdefault("apps_until", {})[user_id] = time.monotonic() + APPS_FOLLOW_UP
        LOG.info("Prepared in %.1f s (apps %s)", time.monotonic() - prepared, "loaded" if use_apps else "skipped")
    except BaseException:
        await live.abort(ui(update, "⚠️ Ответ прерван.", "⚠️ The answer was interrupted."))
        raise
    # Free and paid image/video options are always available.
    async def media(kind, prompt, fmt, seconds=5, voice=True):
        if not asks_for_media(saved_question):
            return ("Not started: the user's message doesn't ask for a picture or video. Answer what they actually "
                    "said instead; offer one only if it really fits.")
        error = await start_generation(context.application, user_id, lang(update), kind, prompt, fmt, seconds, voice)
        if error == "no_credits":
            return ("Not started: the user has no free generations or credits left. Tell them to top up in the Mini App "
                    "(button Secret AI → Generation) with Telegram Stars.")
        if error == "duplicate":
            return "Not started: the same thing is already being made. Tell the user it's on the way."
        if error:
            return f"Not started ({error}). Tell the user briefly that generation is unavailable right now."
        return "Started. The result will arrive in the chat by itself in a moment; tell the user briefly."
    toolbox = Toolbox(context.bot_data["http"], context.bot_data["connections"], context.bot_data["pending"], user_id, lang(update),
                      apps=apps, media=media, extra=[memory_tool(context, user_id), secretary_tool(context, user_id, lang(update) == "ru"),
                                                     edit_tool(context, user_id, lang(update)), cancel_tool(context, user_id)])
    if not use_apps:  # the person's own MCP servers too: handshakes cost seconds, small talk doesn't need them
        toolbox.mcp = []
    greeting = persona in (PERSONAS["assistant"], PERSONAS["street"]) and not use_apps and is_greeting(saved_question)
    accent = "" if persona is PERSONAS["translator"] else ACCENT
    rules = "" if greeting else TRANSLATION_RULES + CODE_RULES
    instructions = (SYSTEM + now_note() + accent + rules + (" " + persona[3] if persona[3] else "") + " Telegram language: "
                    + (update.effective_user.language_code or "ru"))
    if greeting:
        instructions += " This is a simple greeting: reply naturally in one or two short sentences."
    speak_only = voice and bool(context.bot_data.get("tts_model"))
    if speak_only:
        instructions += (" The user sent a voice message and your answer will be read aloud as a voice message: talk like "
                         "in a live conversation, short (2-5 sentences), no lists, no markdown, no links, no emoji.")
    model = user_model(context.bot_data, user_id)
    # Gateway small talk skips tool definitions and unnecessary app handshakes.
    slow_gateway = bool(model) and bool(chat_provider(model))
    light = slow_gateway and not use_apps and not needs_tools(saved_question)
    if not light and toolbox.instructions():
        instructions += " " + toolbox.instructions()
    history = memory.history(user_id, limit=6) if greeting else memory.history(user_id)
    history_limit = 2400 if greeting else ANYMODEL_HISTORY_CHARS if model.startswith(ANYMODEL_PREFIX) else None
    shown = fit_history(history, history_limit)
    if shown:  # everything older than what the model reads comes back by search when the talk touches it
        oldest = history[len(history) - len(shown)]["id"]
    else:
        oldest = history[-1]["id"] + 1 if history else None
    if not greeting:
        instructions += recall_note(memory.recall(user_id, saved_question, oldest))
    price = context.bot_data.get("premium", {}).get(model)
    if price and context.bot_data["connections"].get_meta("owner") == user_id:
        price = None  # the owner talks to every model without limits
    if price and not memory.gen_charge(user_id, "chat", price):
        await live.finish(*await premium_offer(context, update, model, price))
        return
    paid = bool(price)
    chain = [m for m in context.bot_data.get("fallbacks") or [context.bot_data.get("chat_default")] if m and m != model]
    swapped, attempt = None, model
    try:
        while True:
            try:
                answer, incomplete = await unless_stopped(live, generate(
                    context, live, toolbox, model=attempt, instructions=instructions,
                    input=shown + [{"role": "user", "content": content}],
                    store=False, max_output_tokens=1024 if greeting else 32000,
                    light=light and bool(chat_provider(attempt))))
                break
            except MODEL_ERRORS as exc:
                LOG.warning("Model %s failed: %s %s", attempt, type(exc).__name__, str(exc)[:300])
                drop_model(context.bot_data, attempt, exc)
                if not chain or live.text.strip():
                    raise
                if paid:  # the paid model didn't answer: nothing is charged, a free one answers instead
                    memory.gen_refund(user_id, "credits", price)
                    paid = False
                attempt = chain.pop(0)
                swapped = (model_title(model), model_title(attempt))
    except Stopped:  # what was written so far stays as a message, and in the history
        partial = live.text.strip()
        note = ui(update, "⏹ Остановлено", "⏹ Stopped")
        await live.abort(note)
        if not partial[live.offset:].strip():
            await send(target, note)
        memory.append_turn(user_id, saved_question, (partial[:12000] + " [stopped by the user]").strip())
        return
    except BaseException:
        if paid:
            memory.gen_refund(user_id, "credits", price)
        await live.abort(ui(update, "⚠️ Ответ прерван.", "⚠️ The answer was interrupted."))
        raise
    answer = keep_language(clean_chat_text(drop_accent(answer.strip())), saved_question)
    if not answer and toolbox.proposals:
        answer = ui(update, "Подготовил изменения — проверьте и подтвердите ниже.", "Changes are ready — review and confirm below.")
    if not answer:
        if paid:
            memory.gen_refund(user_id, "credits", price)
        await live.finish(ui(update, "Модель не вернула текст. Попробуйте переформулировать вопрос.", "The model returned no text. Please rephrase your question."))
        return
    if incomplete:
        answer += ui(update, "\n\nОтвет достиг лимита. Напишите «продолжи».", "\n\nOutput limit reached. Ask me to continue.")
    memory.append_turn(user_id, saved_question, answer[:12000])
    if swapped:
        free = ui(update, " Кредиты не списаны.", " No credits charged.") if price else ""
        answer = ui(update, f"⚠️ {swapped[0]} сейчас не отвечает, ответил {swapped[1]}.{free}\n\n",
                    f"⚠️ {swapped[0]} isn't answering right now, {swapped[1]} answered.{free}\n\n") + answer
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
        self.text, self.message, self.typing, self.edited, self.edits = "", None, None, 0.0, 0

    def status(self, note):
        pass

    async def start(self):
        # No "typing…" loop here: in a group every call counts towards Telegram's ~20 per minute, and the "…" message
        # already shows the bot is on it. Several busy groups used to hit flood waits and the bot looked frozen.
        frame = f"{self.config['logo']} …"
        self.started = time.monotonic()
        self.message = await with_logo(self.config, lambda entities: reply(self.target, frame, entities=entities))

    async def update(self, text):
        """The answer shows up as it's written, but sparingly: a few edits, seconds apart (code waits for the end)."""
        self.text = text
        now = time.monotonic()
        if (not self.message or not text.strip() or "```" in text or len(text) > 3500 or self.edits >= GROUP_EDITS
                or now - max(self.edited, getattr(self, "started", 0)) < GROUP_EDIT_INTERVAL):
            return
        self.edited, self.edits = now, self.edits + 1
        try:
            await self.message.edit_text(humanize(text) + CURSOR, parse_mode=None)
        except RetryAfter:  # Telegram asks this group to slow down: no more edits, just the final answer
            self.edits = GROUP_EDITS
        except TelegramError:
            pass

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


# Free models that see pictures, to understand stickers: gateway -> (how to find one, the id to try if the list is closed).
VISION_PICK = (("dashscope", r"qwen[\d.]*-?vl-?(max|plus)", "qwen-vl-plus"),
               ("nvidia", r"(qwen.*vl|llama-?3\.2.*vision|vision)", None),
               ("openrouter", r"(vl|vision).*:free|:free.*(vl|vision)", None))
STICKER_CAPTIONS = {}  # sticker file_unique_id -> what's on it (stickers repeat, so each is looked at once)


async def find_vision_model(data):
    """A free model that can look at a picture, or None."""
    for name, pattern, usual in VISION_PICK:
        if not data.get(name):
            continue
        try:
            ids = [m.id for m in (await data[name].models.list()).data]
        except Exception:
            ids = []
        hits = sorted((i for i in ids if re.search(pattern, i, re.I) and not re.search(r"ocr|embed|audio|video", i, re.I)),
                      key=lambda i: ("max" not in i.lower(), len(i)))
        found = hits[0] if hits else (usual if not ids else None)
        if found:
            LOG.info("Stickers are seen by %s:%s", name, found)
            return f"{name}:{found}"
    LOG.info("No free picture model: stickers are known only by their pack")
    return None


async def see_sticker(context, sticker):
    """What's on a sticker, in a few Russian words (cached); None if it can't be seen."""
    data = context.bot_data
    if sticker.file_unique_id in STICKER_CAPTIONS:
        return STICKER_CAPTIONS[sticker.file_unique_id]
    model = data.get("vision_model")
    picture = sticker if not (sticker.is_animated or sticker.is_video) else sticker.thumbnail
    if not model or not picture:
        return None
    name, model_id = model.split(":", 1)
    try:
        raw = bytes(await (await context.bot.get_file(picture.file_id)).download_as_bytearray())

        def to_png():
            from PIL import Image
            out = io.BytesIO()
            image = Image.open(io.BytesIO(raw)).convert("RGBA")
            image.thumbnail((384, 384))
            image.save(out, "PNG")
            return out.getvalue()

        png = await asyncio.to_thread(to_png)
        answer = await asyncio.wait_for(data[name].chat.completions.create(model=model_id, max_tokens=80, messages=[{
            "role": "user", "content": [
                {"type": "text", "text": "Это стикер из Telegram. Опиши по-русски одной короткой фразой (до 12 слов): кто или что "
                                         "на нём, какая эмоция или действие, и надпись, если есть. Без вступлений."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}]), 25)
        caption = " ".join((answer.choices[0].message.content or "").split())[:160].rstrip(".")
    except Exception as exc:
        LOG.warning("Sticker not seen: %s %s", type(exc).__name__, str(exc)[:150])
        return None
    if caption:
        if len(STICKER_CAPTIONS) > 3000:
            STICKER_CAPTIONS.clear()
        STICKER_CAPTIONS[sticker.file_unique_id] = caption
    return caption or None


def describe_sticker(sticker):
    """A sticker for the model. Its emoji is only the tag the pack's author picked (a cat picture may be tagged ➖), so
    it's given as a loose hint, never as words the person said."""
    seen = STICKER_CAPTIONS.get(sticker.file_unique_id)
    if seen:
        return f"стикер: {seen}"
    pack = re.sub(r"_by_\w+$", "", sticker.set_name or "").replace("_", " ").strip()
    hint = f", метка набора {sticker.emoji}" if sticker.emoji and any(ch.isalpha() or ord(ch) > 0x1F000 for ch in sticker.emoji) else ""
    return "стикер" + (f" из набора «{pack}»" if pack else "") + hint + ", картинки не видно"


def describe_message(message):
    text = message.text or message.caption or ""
    kind = ("фото" if message.photo else "голосовое" if message.voice else "кружок" if message.video_note else
            describe_sticker(message.sticker) if message.sticker else "видео" if message.video else
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
    if message.sticker and data.get("vision_model"):
        # Looked at once (updates run in parallel, so nobody waits): the answer and the chat's history know what it is.
        await see_sticker(context, message.sticker)
    if not data.get("models"):  # no models: the chat is remembered, nothing is said
        remember_group_message(store, message, name)
        return
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
        if gateway_busy(group_model(data, chat_id) or ""):
            return  # the model is busy answering people: an uninvited remark can wait for the next time
        counters[chat_id], due[chat_id] = 0, random.randint(*span)
        data["group_surprised"][chat_id] = data["group_spoke"][chat_id] = time.monotonic()
        async with lock:
            try:
                await group_surprise(context, message, user)
            except Exception as exc:  # an uninvited joke that failed is no reason to bother the chat
                LOG.warning("Group surprise failed: %s", type(exc).__name__)
        return
    async with data.setdefault("group_slots", {}).setdefault(chat_id, asyncio.Semaphore(GROUP_PARALLEL)):
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
        older = store.search(chat_id, question)  # from everything kept: the old messages this one is about
        store.add_line(chat_id, f"{name}: {question}", message.message_id, user.id)
        head = ((f"From this group's older history (beyond the recent messages; you remember it — bring it up naturally when it matters, never say you searched), oldest first:\n" + "\n".join(older) + "\n\n"
                 if older else "")
                + (f"Recent messages in the group (oldest first):\n{before}\n\n" if before else "") + f"{name} writes to you:")
        content = ([{"type": "input_text", "text": head}] + content if isinstance(content, list) else f"{head}\n{content}")
        data["group_spoke"][chat_id] = time.monotonic()
        await group_respond(update, context, message, content, voice=bool(message.voice or message.video_note))


def group_instructions(update):
    return GROUP_STYLE + now_note() + TRANSLATION_RULES + CODE_RULES + " Reply in the chat's language. Telegram language: " + (update.effective_user.language_code or "ru")


# The retired accent's filler words, cut out even if the model copies them from its old messages in the chat.
LEADING_ACCENT = re.compile(r"(?i)^(?:вах|брат|братан|дорогой|слушай,\s*брат)[,!.]*\s+")


# Scripts a model sometimes drifts into ("점심시간도 아닌데…" in a Russian chat): name -> character class.
STRAY_SCRIPTS = {"hangul": "\uac00-\ud7af\u1100-\u11ff\u3130-\u318f", "han": "\u4e00-\u9fff\u3400-\u4dbf",
                 "kana": "\u3040-\u30ff", "thai": "\u0e00-\u0e7f", "arabic": "\u0600-\u06ff", "hebrew": "\u0590-\u05ff",
                 "devanagari": "\u0900-\u097f"}
WANTS_LANGUAGE = re.compile(r"перев|translat|по-?(корейск|китайск|японск|арабск|тайск|иврит|хинди)|корейск|китайск|японск|"
                            r"арабск|иероглиф|korean|chinese|japanese|arabic|hebrew|hindi|thai", re.I)


def keep_language(answer, question):
    """Cut sentences written in a script the user never used (and didn't ask for): the model's random language drift."""
    question = question if isinstance(question, str) else str(question or "")
    if not answer or WANTS_LANGUAGE.search(question):
        return answer
    stray = [chars for chars in STRAY_SCRIPTS.values()
             if re.search(f"[{chars}]", answer) and not re.search(f"[{chars}]", question)]
    if not stray:
        return answer
    foreign = re.compile("[" + "".join(stray) + "]")

    def alien(piece):
        letters = [c for c in piece if c.isalpha()]
        return letters and sum(1 for c in letters if foreign.match(c)) / len(letters) > 0.3

    kept = []
    for line in answer.split("\n"):
        parts = re.split(r"(?<=[.!?…])\s+", line)
        kept.append(" ".join(part for part in parts if not alien(part)).strip() if any(alien(x) for x in parts) else line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return cleaned or answer


def drop_accent(text):
    """Private answers: the retired accent's filler cut out ("Брат, я не понял" -> "Я не понял")."""
    cleaned = LEADING_ACCENT.sub("", ACCENT_WORDS.sub("", text))
    return cleaned[:1].upper() + cleaned[1:] if cleaned and text[:1].isupper() else cleaned


ACCENT_WORDS = re.compile(r"(?i)(?:^|(?<=[\s,.!?]))(?:вах|валлах|мамой клянусь)\b[,!.]*\s*|,\s*(?:брат|дорогой|э)\b(?=\s*[.,!?)]|\s*$|\s+\W)")


def humanize(text):
    """Strip the tells of a bot: long dashes and a full stop at the end of a one-liner."""
    text = ACCENT_WORDS.sub("", text)
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
# What makes a message a request for a picture/video: it names one, or it briefly agrees to the one the bot offered.
MEDIA_ASK = re.compile(r"видео|видое|видо|видяш|видик|вдео|видос|ролик|клип|мульт|аним|гиф|картин|фот|рисун|нарису|рисуй|изображ|постер|арт\b|"
                       r"обо[ий]|аватар|логотип|сними|снять|video|picture|image|photo|draw|paint|animate", re.I)
MEDIA_AGREE = re.compile(r"\b(давай|го+|да+|ок|окей|ага|угу|кинь|делай|сделай|хочу|можно|погнали|ещё|еще|заново|снова|"
                         r"другой|другую|повтори|yes|ok|go|sure|another|again)\b", re.I)


def asks_for_media(text):
    """Does this user message ask for a picture or a video? Stops the model starting one nobody asked for."""
    text = (text or "").strip()
    return bool(MEDIA_ASK.search(text)) or (len(text.split()) <= 5 and bool(MEDIA_AGREE.search(text)))


class GroupTools:
    """The only tool in a group: make a video or picture for the person who asked (they pay, the owner doesn't)."""

    def __init__(self, update, context, message):
        self.update, self.context, self.message = update, context, message

    def definitions(self):
        return [GROUP_MEDIA_TOOL, MEMORY_TOOL, CANCEL_TOOL]

    def status(self, name, args):
        return None

    async def run(self, name, args):
        if name == "remember":  # about the person who wrote to the bot
            self.context.bot_data["memory"].remember(self.update.effective_user.id, args.get("add") or [],
                                                     args.get("forget") or [])
            return "Saved."
        if name == "cancel_generation":
            return await cancel_generations(self.context.bot_data, self.message.chat_id)
        if name != "make_media":
            return "Unknown tool."
        said = self.message.text or self.message.caption or ""
        if said and not asks_for_media(said):
            return "Not started: this message doesn't ask for a picture or video. Just answer what was said."
        kind = "video" if args.get("kind") == "video" else "photo"
        seconds = next((s for s in sorted(VIDEO_PRICES) if s >= int(args.get("seconds") or SCENE_SECONDS)), max(VIDEO_PRICES))
        error = await start_generation(self.context.application, self.update.effective_user.id, lang(self.update), kind,
                                       str(args.get("description") or ""), "portrait", seconds if kind == "video" else 5, True,
                                       chat_id=self.message.chat_id, reply_id=self.message.message_id)
        if error == "no_credits":
            return ("Not started: this person has no free generations or credits left. Tell them to top up in the bot's "
                    "Mini App (Генерация → Пополнить).")
        if error == "duplicate":
            return "Not started: the same thing is already being made. Say it's on the way."
        if error:
            return f"Not started ({error}). Say briefly that it doesn't work right now."
        return "Started. It will be posted in reply in a few minutes; say so in a few words."


@counted
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
                                   instructions=group_instructions(update) + gen_status(data, message.chat_id)
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
    if chosen and chosen in free_models(data):
        return chosen
    return data.get("group_fast") or data.get("chat_default") or data["model"]  # groups want speed: the fast model first


async def quick_answer(data, model, instructions, text, max_tokens=2000):
    """A short non-streamed answer; when the model fails, the next working one (data["fallbacks"]) answers."""
    chain = [m for m in [model] + [m for m in data.get("fallbacks") or [] if m != model] if m]
    if not chain:
        raise NoModels()
    for index, candidate in enumerate(chain):
        try:
            return await quick_answer_once(data, candidate, instructions, text, max_tokens)
        except MODEL_ERRORS as exc:
            drop_model(data, candidate, exc)
            if index == len(chain) - 1:
                raise
            LOG.warning("Model %s failed (%s); trying %s", candidate, type(exc).__name__, chain[index + 1])


async def quick_answer_once(data, model, instructions, text, max_tokens=2000):
    """A short non-streamed answer from either an OpenAI model or a RouteLLM one."""
    if not data.get("openai") and not chat_provider(model):  # OpenAI is off: the default chat model does it
        model = data.get("chat_default") or model
    provider = chat_provider(model)
    if provider and data.get(CHAT_PROVIDERS[provider]):
        async with gateway_slot(model):
            response = await chat_completion(data, model, data[CHAT_PROVIDERS[provider]],
                model=model[len(provider):], max_tokens=max_tokens,
                messages=[{"role": "system", "content": instructions + model_identity(model)}, {"role": "user", "content": text}])
        message = response.choices[0].message if response.choices else None
        content = getattr(message, "content", None)
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(getattr(p, "text", "") or "") for p in content)
        return content or ""
    if not data.get("openai"):
        raise RuntimeError(f"no gateway for {model}")
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


def day_digest(data):
    """The groups' daily summary, created on first use (it shares the groups' database)."""
    if "digest" not in data:
        async def ask(chat_id, instructions, text):
            return await quick_answer(data, group_model(data, chat_id), instructions, text, max_tokens=1500)
        data["digest"] = digest.Digest(data["groups"], ask)
    return data["digest"]


def chat_serial(data):
    if "serial" not in data:
        async def ask(chat_id, instructions, text):
            return await quick_answer(data, group_model(data, chat_id), instructions, text, max_tokens=2400)

        async def image(prompt):
            return await free_image(data["http"], prompt, "portrait", data.get("cloudflare"))

        data["serial"] = chatserial.Serial(data["groups"], ask, speech.edge_speech, image)
    return data["serial"]


async def serial_command(update, context):
    message, user, data = update.effective_message, update.effective_user, context.bot_data
    if not message or not user or (data["allowed"] and user.id not in data["allowed"]):
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.reply_text("Добавьте меня в группу и напишите там /serial.\n\n" + chatserial.HELP)
        return
    show = chat_serial(data)
    arg = context.args[0].lower() if context.args else ""
    if arg in ("help", "помощь"):
        await message.reply_text(chatserial.HELP)
        return
    if arg == "status":
        state = show.state(message.chat_id)
        lines, _ = show.history(message.chat_id)
        next_at = (time.strftime("%d.%m %H:%M UTC", time.gmtime(state["next_at"]))
                   if state["enabled"] else "автовыпуск выключен")
        await message.reply_text(f"Вышло серий: {state['episode']}\nСообщений за неделю: {len(lines)}\n"
                                 f"Следующая серия: {next_at}\n\n/serial help — команды")
        return
    if arg not in ("", "on", "off", "вкл", "выкл"):
        await message.reply_text(chatserial.HELP)
        return
    try:
        member = await context.bot.get_chat_member(message.chat_id, user.id)
    except TelegramError:
        await message.reply_text("Не удалось проверить права. Повторите чуть позже.")
        return
    if user.is_bot or member.status not in ("administrator", "creator"):
        await message.reply_text("Снимать серии и менять расписание может администратор группы.")
        return
    data["groups"].touch(message.chat_id, message.chat.title, user.id)
    if arg:
        on = arg in ("on", "вкл")
        show.enable(message.chat_id, on)
        next_at = time.strftime("%d.%m %H:%M UTC", time.gmtime(show.state(message.chat_id)["next_at"]))
        await message.reply_text(f"Автовыпуск включён: раз в 7 дней. Следующая серия: {next_at}. Снять сейчас: /serial"
                                 if on else "Автовыпуск сериала выключен.")
        return
    if not data.get("models"):
        await message.reply_text("Сначала подключите модель для сценария в настройках Secret AI.")
        return
    # Track the task so automatic code updates wait for the render to finish.
    task = asyncio.create_task(show.post(context.bot, message.chat_id, message.chat.title or "Группа", manual=True))
    jobs = data.setdefault("gen_tasks", set())
    jobs.add(task)
    task.add_done_callback(jobs.discard)
    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())


async def day_command(update, context):
    """/sday: the day's summary now; "/sday off" / "/sday on" (admins) turn the evening post off and on."""
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    if message.chat.type == "private":
        await reply(message, "«Итоги дня» — для групп: добавь меня в группу и напиши там /sday")
        return
    summary, arg = day_digest(context.bot_data), (context.args[0].lower() if context.args else "")
    if arg in ("off", "on", "выкл", "вкл"):
        try:
            member = await context.bot.get_chat_member(message.chat_id, user.id)
            is_admin = member.status in ("administrator", "creator")
        except TelegramError:
            is_admin = False
        if not is_admin:
            await message.reply_text("Включать и выключать итоги дня может только админ группы")
            return
        on = arg in ("on", "вкл")
        summary.set_enabled(message.chat_id, on)
        await message.reply_text(f"Итоги дня по вечерам ({digest.HOUR}:00 мск) " + ("включены ✅" if on else "выключены ⏸")
                                 + f". Получить сейчас: /{digest.COMMAND}")
        return
    await context.bot.send_chat_action(message.chat_id, ChatAction.UPLOAD_PHOTO)
    await summary.post(context.bot, message.chat_id, message.chat.title, manual=True)


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
        text = await quick_answer(
            data, data.get("chat_default") or data["model"], max_tokens=800,
            instructions=("Write a caption for a classic Russian demotivator poster in the language of the chat: 'title' is 1-3 "
                          "words, 'subtitle' is one short ironic line. Funny, based on the chat; no insults about "
                          'ethnicity, religion, orientation or looks. Return only JSON {"title": "...", "subtitle": "..."}.'),
            text="Chat:\n" + "\n".join(lines) + (f"\n\nPhoto caption or request: {hint}" if hint else ""))
        caption = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
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


def cancel_tool(context, chat_id):
    """cancel_generation for the private chat; its hint tells the model what is being made or was just sent."""
    async def run(args):
        return await cancel_generations(context.bot_data, chat_id)

    return {"definition": CANCEL_TOOL, "run": run, "status": ("Отменяю", "Cancelling"),
            "hint": gen_status(context.bot_data, chat_id).strip()}


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
        LOG.warning("Secretary failed: %s %s", type(exc).__name__, str(exc)[:300])


async def secretary_answer(context, message):
    data, store = context.bot_data, context.bot_data["connections"]
    conn_id = message.business_connection_id
    conn = sec.connection(store, conn_id)
    if not conn:
        conn = sec.save_connection(store, await context.bot.get_business_connection(conn_id))
    chat_id, sender, activity = message.chat.id, message.from_user, data["biz_activity"]
    # One person may write to several owners: each owner's chat with them has its own history and timing.
    key = (conn["user_id"], chat_id)
    said = describe_message(message) or "…"
    if sender and sender.id == conn["user_id"]:
        activity.owner_wrote(sender.id)  # the owner is here: stay quiet in all chats for a while
        activity.log(key, f"{conn.get('name', 'Owner')} (owner): {said}")
        return
    name = speaker(sender) if sender else "?"
    if name.strip(" ?") == "":  # no name we can show: the chat's own title or @username
        name = (message.chat.full_name or (f"@{message.chat.username}" if message.chat.username else "")
                or f"id {chat_id}")[:40]
    activity.log(key, f"{name}: {said}")
    current = sec.settings(store, conn["user_id"])
    if (not current["enabled"] or not conn["enabled"] or not conn["can_reply"] or message.chat.type != "private"
            or (sender and sender.is_bot) or not activity.may_answer(conn["user_id"], key)):
        return
    activity.answered_now(key)
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING, business_connection_id=conn_id)
    except TelegramError:
        pass
    instructions = sec.instructions(conn.get("name", "the owner"), current["rules"])
    chat = "Chat (oldest first):\n" + "\n".join(activity.lines.get(key, [])) + f"\n\nAnswer {name}'s last message."
    text = ""
    # The bot's main chat model first (what answers in the private chat), plain OpenAI as the fallback.
    for model in dict.fromkeys([data.get("chat_default") or data["model"], data["model"]]):
        try:
            text = (await quick_answer(data, model, instructions, chat, max_tokens=1500)).strip()
        except Exception as exc:
            LOG.warning("Secretary model %s failed: %s %s", model, type(exc).__name__, str(exc)[:200])
            continue
        if text:
            break
    if not text:
        activity.answered.pop(key, None)  # nothing was sent: the next message may try again
        return
    # The answer took a while: send it only if the secretary is still on and the owner hasn't stepped in meanwhile.
    now, conn_now = sec.settings(store, conn["user_id"]), sec.connection(store, conn_id)
    if (not now["enabled"] or not conn_now or not conn_now["enabled"] or not conn_now["can_reply"]
            or activity.owner_online(conn["user_id"])):
        activity.answered.pop(key, None)
        return
    await send_signed(context, chat_id, text[:3500], conn_id, message.message_id)
    activity.log(key, f"AI secretary: {text}")
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


async def probe_model(data, model):
    """(seconds to the first word, seconds in total) for a tiny question, or (None, error text)."""
    provider = chat_provider(model)
    client = data.get(CHAT_PROVIDERS[provider]) if provider else None
    if not client:
        return None, "нет ключа"
    started, first = time.monotonic(), None
    try:
        async def run():
            nonlocal first
            stream = await chat_completion(data, model, client, model=model[len(provider):], stream=True, max_tokens=200,
                                                          messages=[{"role": "user", "content": "Скажи «привет» одним словом."}])
            async for chunk in stream:
                choice = (getattr(chunk, "choices", None) or [None])[0]
                if first is None and getattr(getattr(choice, "delta", None), "content", None):
                    first = time.monotonic() - started
        await asyncio.wait_for(run(), 40)
    except asyncio.TimeoutError:
        return None, "не ответила за 40 с"
    except Exception as exc:
        drop_model(data, model, exc)
        said = ""
        body = getattr(exc, "body", None)
        if isinstance(body, dict):  # the provider's own words: "insufficient balance", "verify your account"…
            said = str((body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error") or body.get("message") or "")
        said = (said or str(exc)).strip().replace("\n", " ")[:140]
        return None, f"ошибка {type(exc).__name__}" + (f": «{said}»" if said else "") + (
            " — убрал из меню" if model not in data.get("models", []) else "")
    return first if first is not None else time.monotonic() - started, time.monotonic() - started


async def speed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/speed: how fast each model answers right now (owner); /speed set makes the fastest free one the default."""
    if not await authorized(update, context):
        return
    data, message = context.bot_data, update.effective_message
    if data["connections"].get_meta("owner") != update.effective_user.id:
        await send(message, "Скорость моделей проверяет только владелец.")
        return
    models = list(data.get("models") or [])
    if not models:
        await send(message, "Моделей нет.")
        return
    status = await reply(message, f"⏱ Проверяю {len(models)} моделей…")
    results = await asyncio.gather(*(probe_model(data, m) for m in models))
    rows = sorted(zip(models, results), key=lambda r: (r[1][0] is None, r[1][0] or 0))
    paid = data.get("premium", {})
    lines = []
    for model, (first, total) in rows:
        name = model_title(model) + (" 💎" if model in paid else "") + (" ⭐ по умолчанию" if model == data.get("chat_default") else "")
        lines.append(f"• {name}: " + (f"начала через {first:.1f} с, всего {total:.1f} с" if first is not None else f"❌ {total}"))
    free_ok = [m for m, (first, _) in rows if first is not None and m not in paid]
    tip = ""
    if context.args and context.args[0].lower() in ("set", "поставь", "default") and free_ok:
        data["connections"].set_meta("default_model", free_ok[0])
        data["chat_default"] = data["model"] = free_ok[0]
        data["fallbacks"] = free_ok + [m for m in data.get("fallbacks", []) if m not in free_ok]
        tip = f"\n\n✅ По умолчанию теперь {model_title(free_ok[0])} — самая быстрая бесплатная."
    elif free_ok and free_ok[0] != data.get("chat_default"):
        tip = f"\n\nСамая быстрая бесплатная — {model_title(free_ok[0])}. Сделать её моделью по умолчанию: /speed set"
    await status.edit_text("⏱ Скорость моделей сейчас (от быстрой к медленной):\n\n" + "\n".join(lines) + tip)


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/voice: which free Microsoft voice reads the answers; /voice <name> picks one (owner only)."""
    if not await authorized(update, context):
        return
    data, message = context.bot_data, update.effective_message
    if data["connections"].get_meta("owner") != update.effective_user.id:
        await send(message, "Голос бота меняет только владелец.")
        return
    query = " ".join(context.args or []).strip()
    if query.lower().startswith("fish") or query.lower() in ("мой", "my", "реалистичный"):
        rest = query[4:].strip() if query.lower().startswith("fish") else ""
        voice = fish_voice_id(rest) if rest else (data.get("fish_voice") or "")
        if not voice:
            await send(message, "Пришли так: /voice fish <ссылка на голос с fish.audio или его ID>")
            return
        if rest:
            data["connections"].set_meta("fish_voice", voice)
        data["connections"].set_meta("reply_voice", "fish")
        try:
            await speak(context, message, "Вот так я теперь звучу. Как тебе, нормально?")
        except Exception as exc:
            await send(message, f"Голос выбран, но озвучить не вышло: {type(exc).__name__}")
        return
    if query:
        voice = speech.RU_VOICES.get(query.lower()) or (query if re.fullmatch(r"[a-z]{2}-[A-Z]{2}-\w+Neural", query) else None)
        if not voice:
            await send(message, "Такого голоса нет. Напиши /voice — покажу список.")
            return
        data["connections"].set_meta("edge_voice", voice)
        data["connections"].set_meta("reply_voice", "edge")
        try:
            await speak(context, message, "Теперь я говорю этим голосом. Как тебе?")
        except Exception as exc:
            await send(message, f"Голос выбран: {voice}, но озвучить не вышло: {type(exc).__name__}")
        return
    reply = data["connections"].get_meta("reply_voice") or "fish"
    fish = data["connections"].get_meta("fish_voice") or data.get("fish_voice") or ""
    edge = data["connections"].get_meta("edge_voice") or speech.EDGE_VOICE
    names = {v: k for k, v in speech.RU_VOICES.items()}
    current = (f"реалистичный Fish Audio ({'твой голос' if fish == data.get('fish_voice') else fish})" if reply == "fish"
               else f"Microsoft {names.get(edge, edge)}")
    await send(message, f"🎙 Сейчас голос: {current}\n\n"
               "Реалистичные (Fish Audio):\n• /voice мой — твой голос\n"
               "• /voice fish <ссылка> — любой голос из библиотеки fish.audio (открой fish.audio, выбери голос, скопируй ссылку)\n\n"
               "Простые (Microsoft, звучат роботнее):\n"
               + "\n".join(f"• /voice {name} — {label}" for name, label in speech.RU_LABELS.items()))


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


GEN_RECENT = 900  # seconds a finished picture/video stays in the bot's mind
CANCEL_TOOL = {"type": "function", "name": "cancel_generation",
               "description": ("Stop the picture or video still being made in this chat, when the user says not to make it "
                               "(\"не рисуй\", \"не надо\", \"отмена\", \"стоп\"). A stopped one is not charged."),
               "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}


def gen_log(data, chat_id):
    """This chat's pictures and videos: [{kind, prompt, state: running/done/failed/cancelled, at, task}]."""
    return data.setdefault("gen_log", {}).setdefault(chat_id, [])


def _words(text):
    return {w[:6] for w in re.findall(r"\w{4,}", (text or "").lower())}


def similar_prompt(a, b):
    first, second = _words(a), _words(b)
    return bool(first and second) and len(first & second) / min(len(first), len(second)) >= 0.5


def gen_status(data, chat_id):
    """For the instructions: what was made in this chat lately, so the bot doesn't start it again or forget it."""
    now = time.monotonic()
    recent = [e for e in gen_log(data, chat_id) if e["state"] == "running" or now - e["at"] < GEN_RECENT]
    if not recent:
        return ""
    lines = []
    for entry in recent:
        minutes = max(1, int((now - entry["at"]) // 60))
        state = {"running": f"being made right now (started {minutes} min ago)",
                 "done": f"ALREADY DELIVERED to the chat {minutes} min ago",
                 "failed": "failed, the user was told", "cancelled": "cancelled"}[entry["state"]]
        lines.append(f"- {'picture' if entry['kind'] == 'photo' else 'video'} «{entry['prompt'][:150]}»: {state}")
    return ("\n\nPictures and videos in this chat (each arrives in the chat by itself when ready):\n" + "\n".join(lines) +
            "\nNever start one of these again unless the user clearly asks for another one or a new version — short "
            "replies like \"э\", \"ну\", \"что делаешь\", \"где?\" are not requests. If they ask what you're doing or "
            "where it is, answer from this list. If they say not to make it, call cancel_generation.")


async def cancel_generations(data, chat_id):
    running = [e for e in gen_log(data, chat_id) if e["state"] == "running" and e.get("task")]
    for entry in running:
        entry["task"].cancel()
    if running:
        return f"Stopped {len(running)}; nothing is charged for it. Tell the user briefly."
    return "Nothing is being made now (it was already delivered or finished). Tell the user briefly."


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
    log = gen_log(data, chat_id or user_id)
    if any(e["state"] == "running" and e["kind"] == kind and similar_prompt(e["prompt"], prompt) for e in log):
        return "duplicate"
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
    entry = {"kind": kind, "prompt": prompt, "state": "running", "at": time.monotonic()}
    log.append(entry)
    # Keep the last 10 finished ones, but never drop a running job: it must stay findable for "cancel".
    finished = [e for e in log if e["state"] != "running"]
    for old in finished[:-10]:
        log.remove(old)
    job["entry"] = entry
    task = asyncio.create_task(generation_job(app, job))
    entry["task"] = task
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
    scenes = []
    # The main chat model first, plain OpenAI as the fallback; JSON is cut out of the answer, no special format needed.
    for model in dict.fromkeys([data.get("chat_default") or data["model"], data["model"]]):
        try:
            text = await quick_answer(data, model, instructions + " Output only the JSON.", job["prompt"], max_tokens=4000)
            found = re.search(r"\{.*\}", text or "", re.S)
            scenes = json.loads(found.group(0))["scenes"][:count] if found else []
            scenes = [{"visual": str(s["visual"]).strip(), "sound": str(s.get("sound") or "").strip(),
                       "voice": str(s.get("voice") or "").strip()} for s in scenes if s.get("visual")]
        except Exception as exc:  # planning is a nicety: fall back to the raw idea
            LOG.warning("Scene planning with %s failed: %s %s", model, type(exc).__name__, str(exc)[:200])
            scenes = []
        if scenes:
            break
    if len(scenes) < count:
        scenes += [{"visual": f"{job['prompt']} (part {i + 1} of {count})", "sound": "", "voice": ""} for i in range(len(scenes), count)]
    return scenes


async def english_prompt(data, prompt):
    """Image models understand English best: translate and enrich a prompt written in another language."""
    if prompt.isascii():
        return prompt
    try:
        text = await quick_answer(
            data, data.get("chat_default") or data["model"], max_tokens=600, text=prompt,
            instructions=("Rewrite this image request as one detailed English prompt for a text-to-image model: subject, "
                          "style, lighting, composition. Keep every detail the user asked for; quote any text that must "
                          "appear in the image as is. Output only the prompt."))
        return (text or "").strip() or prompt
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
        text = await quick_answer(data, data.get("chat_default") or data["model"], instructions + " Return only the JSON.",
                                  job["prompt"], max_tokens=4000)
        found = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        prompts = [str(p).strip() for p in found["prompts"] if str(p).strip()][:count]
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
    return await montage(list(clips), job["fmt"], keep_sound=True, seconds=job["seconds"])


async def voice_over(data, job, scenes):
    """The Russian (or the user's language) narrator for the scenes, MP3 bytes; None when off or failed."""
    narration = " ".join(scene["voice"] for scene in scenes if scene["voice"])
    if not (job["voice"] and narration and data.get("tts_model")):
        return None
    try:
        return await owner_voice(data, narration[:4000])
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
    return await montage(list(clips), job["fmt"], voice, keep_sound=True, sound_volume=0.5 if voice else 1.0,
                         seconds=job["seconds"])


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
    return await montage(clips, job["fmt"], voice, keep_sound=True, sound_volume=0.5 if voice else 1.0, seconds=job["seconds"])


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
    if not data.get("openai"):
        raise MediaError("failed", "no OpenAI")
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


def set_state(job, state, data=None):
    if job.get("entry"):
        job["entry"].update(state=state, at=time.monotonic(), task=None)
    if data is not None and job.get("chat_id") in (None, job["user_id"]):  # the private chat's history keeps it
        what = "picture" if job["kind"] == "photo" else "video"
        note = {"done": f"[{what} «{job['prompt'][:120]}» was delivered to the chat]",
                "failed": f"[{what} «{job['prompt'][:120]}» failed; the user was told]",
                "cancelled": f"[{what} «{job['prompt'][:120]}» was cancelled]"}[state]
        try:
            data["memory"].annotate(job["user_id"], note)
        except Exception as exc:
            LOG.warning("Could not note the generation: %s", type(exc).__name__)


async def generation_job(app, job):
    bot, data = app.bot, app.bot_data
    user_id, ru, kind = job["user_id"], job["language"] == "ru", job["kind"]
    chat, answer = job.get("chat_id") or user_id, {"reply_parameters": reply_to(job.get("reply_id"))}
    status = None
    try:  # from the very first step: a cancel while "Рисую…" is being sent still refunds and ends the job
        try:
            status = await bot.send_message(chat, waiting_text(job), **answer)
        except TelegramError:
            pass
        # No caption: the prompt is the model's own (often English) description, not something to show.
        caption = None
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
        set_state(job, "done", data)
    except asyncio.CancelledError:  # the user said not to make it
        set_state(job, "cancelled", data)
        if job["how"] != "owner":
            data["memory"].gen_refund(user_id, job["how"], job["cost"])
    except Exception as exc:  # any failure: give the generation back and explain
        set_state(job, "failed", data)
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
    if (message.entities or message.caption_entities) and context.bot_data["connections"].get_meta("owner") == update.effective_user.id:
        learned = learn_premium(context, message)
        rest = re.sub(r"[\s\W_]", "", message.text or message.caption or "", flags=re.UNICODE)
        if learned and not rest:  # only emoji: a lesson, not a question
            shown = (f"✨ Запомнил премиум-эмодзи: {' '.join(learned)}\nТеперь они стоят вместо обычных везде, где я их пишу: "
                     f"в моих ответах, в меню (/promo), уведомлениях и на кнопках. Всего: {len(PREMIUM_EMOJI)}. Список — /emoji")
            await send(message, shown)
            # Keep the catalog out of model history: it is not a sample answer style.
            return
    if context.bot_data.get("broadcast", {}).get(update.effective_user.id) and await broadcast_typed(update, context):
        return
    if message.text and await promo_typed(update, context):
        return
    if message.text and await secretary_switch(update, context, message.text):
        return
    if not models_for(context.bot_data, update.effective_user.id):
        shown = ui(update, "🛠 Модели пока не подключены — скоро вернусь.", "🛠 No models are connected yet — back soon.")
        await send(message, shown)
        remember_reply(context, update, shown)
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


async def owner_voice(data, text):
    """Speech in the owner's own voice (the Fish Audio voice FISH_VOICE): Fish Audio with FISH_API_KEY, otherwise the
    free Fish route on the AnyModel key; the Microsoft voice if neither answers. Used for video voice-overs."""
    voice = data["connections"].get_meta("fish_voice") or data.get("fish_voice") or ""
    if voice and voice != "off":
        if data.get("fish_key"):
            try:
                return await fish_speech(data["http"], data["fish_key"], voice, text, data.get("fish_model", ""))
            except Exception as exc:
                LOG.warning("Owner's voice on Fish Audio failed: %s %s", type(exc).__name__, str(exc)[:200])
        if data.get("anymodel_key"):
            try:
                audio = await anymodel_speech(data["http"], data["anymodel_key"], ANYMODEL_FISH, text,
                                              data.get("anymodel_api") or ANYMODEL_API, reference_id=voice)
                LOG.info("Voice-over in the owner's voice %s", voice)
                return audio
            except Exception as exc:
                LOG.warning("Owner's voice through the Fish route failed: %s %s", type(exc).__name__, str(exc)[:200])
    return await speech_audio(data, text)


async def speech_audio(data, text):
    """OGG Opus speech for voice replies. A realistic Fish Audio voice first (the owner's own voice unless /voice fish
    picked another from fish.audio; /voice дмитрий switches to Microsoft), through FISH_API_KEY or the free Fish route on
    the AnyModel key; the free Microsoft voice if Fish doesn't answer."""
    chosen = data["connections"].get_meta("reply_voice") or "fish"
    fish = data["connections"].get_meta("fish_voice") or data.get("fish_voice") or ""
    if chosen == "fish" and fish and fish != "off":
        if data.get("fish_key"):
            try:
                audio = await fish_speech(data["http"], data["fish_key"], fish, text, data.get("fish_model", ""))
                LOG.info("Voice reply: Fish Audio voice %s", fish)
                return audio
            except Exception as exc:
                LOG.warning("Fish Audio voice failed: %s %s", type(exc).__name__, str(exc)[:200])
        if data.get("anymodel_key"):
            try:
                audio = await anymodel_speech(data["http"], data["anymodel_key"], ANYMODEL_FISH, text,
                                              data.get("anymodel_api") or ANYMODEL_API, reference_id=fish)
                LOG.info("Voice reply: Fish voice %s (free route)", fish)
                return audio
            except Exception as exc:
                LOG.warning("Fish voice (free route) failed: %s %s", type(exc).__name__, str(exc)[:200])
    voice = data["connections"].get_meta("edge_voice") or None
    if voice and speech.voice_for(text) != speech.EDGE_VOICE and voice.startswith("ru-"):
        voice = None  # a Russian voice can't read English: the matching voice reads it
    audio = await voice_message(await speech.edge_speech(text, voice))
    LOG.info("Voice reply: Microsoft voice %s", voice or speech.voice_for(text))
    return audio


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

    if data.startswith("bc:"):
        await broadcast_button(update, context, query, data[3:])
        return

    if data.startswith("pr:"):
        await promo_button_press(update, context, query, data[3:])
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
        await send(update.effective_message, ui(update, f"✅ Готово! +{credits} кредитов: платные модели, фото и видео.",
                                                f"✅ Done! +{credits} credits: paid models, photos and videos."))


async def unsupported(update, context):
    if update.effective_chat and update.effective_chat.type in GROUP_TYPES:
        if update.effective_message and update.effective_user:
            await group_chat(update, context)  # stickers, videos, gifs: remembered as context, sometimes commented
        return
    if await authorized(update, context):
        if context.bot_data.get("broadcast", {}).get(update.effective_user.id) and await broadcast_typed(update, context):
            return  # a video, sticker or gif to broadcast
        await send(update.effective_message, ui(update, "Отправьте текст, голосовое, аудио или фото. Команды: /help", "Send text, voice, audio or a photo. Commands: /help"))


def remember_reply(context, update, shown):
    """A service reply (an error, "no models", "couldn't hear the voice message") goes into the private chat's history,
    so the model knows what the person saw and understands "ok, let's add it then" afterwards."""
    message, chat = update.effective_message, update.effective_chat
    if not message or not chat or chat.type != "private" or not update.effective_user:
        return
    asked = (message.text or message.caption or ("[голосовое сообщение]" if message.voice or message.audio or message.video_note
             else "[фото]" if message.photo else "[сообщение]"))
    memory, user_id = context.bot_data["memory"], update.effective_user.id
    try:
        last = memory._last_turn(user_id)
        if last and last[1][2] == asked:  # the question is already saved (the error came after the answer): note it there
            memory.annotate(user_id, f"[Then the bot showed: {shown}]")
        else:
            memory.append_turn(user_id, asked, f"[Service message the bot showed instead of an answer: {shown}]")
    except sqlite3.Error as exc:
        LOG.warning("Could not remember a service reply: %s", type(exc).__name__)


async def on_error(update, context):
    error = context.error
    # Never log exception bodies: they can contain tokens, URLs or user content.
    # Where it broke (file:line function) and, for model errors, what the provider said: enough to find a bug.
    frames = traceback.extract_tb(error.__traceback__) if error else []
    place = " ← ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in frames[-3:][::-1])
    LOG.error("Handler error: %s at %s %s", type(error).__name__, place, str(error)[:300] if isinstance(error, MODEL_ERRORS) else "")
    if not isinstance(update, Update) or not update.effective_message or not update.effective_user or update.business_message:
        return
    shown = ui(update, *error_text(error))
    try:
        await send(update.effective_message, shown)
    except TelegramError:
        LOG.warning("Error notification could not be delivered")
    remember_reply(context, update, shown)


MODEL_ERRORS = (APIError, cursor.CursorError)  # any provider failure


def error_text(error):
    """(Russian, English) explanation of a failed answer."""
    ru, en = "Не удалось обработать сообщение. Попробуйте ещё раз чуть позже.", "Couldn't process your message. Please try again later."
    if isinstance(error, NoModels):
        ru, en = "🛠 Модели пока не подключены — скоро вернусь.", "🛠 No models are connected yet — back soon."
    elif isinstance(error, cursor.CursorError):
        ru, en = f"Cursor не ответил: {str(error)[:200]}", f"Cursor didn't answer: {str(error)[:200]}"
    elif isinstance(error, RateLimitError):
        ru, en = "Модель сейчас перегружена или у провайдера закончился лимит. Попробуй чуть позже или выбери другую модель в Secret AI.", "The model is overloaded or its provider's limit is reached. Try later or pick another model in Secret AI."
    elif isinstance(error, (AuthenticationError, NotFoundError, PermissionDeniedError)):
        ru, en = "Выбранная модель недоступна на ключе бота. Выбери другую модель в Secret AI.", "The chosen model isn't available on the bot's key. Pick another model in Secret AI."
    elif isinstance(error, BadRequestError):
        ru, en = "Модель отклонила запрос. Попробуй написать текстом или прислать другое фото/голосовое.", "The model rejected the request. Try text or different media."
    elif isinstance(error, APIConnectionError):
        ru, en = "Модель не ответила вовремя или недоступна сеть. Попробуй ещё раз через минуту.", "The model timed out or the network is unavailable. Please try again in a minute."
    elif isinstance(error, TimeoutError):
        ru, en = "Файл обрабатывался слишком долго. Попробуйте запись покороче.", "Processing the file took too long. Try a shorter recording."
    elif isinstance(error, ValueError):
        ru, en = "Не удалось обработать файл. Голосовое — до 10 минут и 19 МБ, фото — до 19 МБ.", "Couldn't process this file. Voice limit: 10 minutes and 19 MB; photo limit: 19 MB."
    return ru, en


COMMANDS = {
    "ru": [("app", "Подключения"), ("secretary", "ИИ-секретарь"), ("help", "Что я умею"), ("mode", "Сменить режим"), ("reset", "Очистить память"),
           ("export", "Скачать историю"), ("promo", "Ввести промокод"), ("emoji", "Премиум-эмодзи бота"), ("myid", "Мой Telegram ID"), ("version", "Версия бота")],
    "en": [("app", "Connections"), ("secretary", "AI secretary"), ("help", "What I can do"), ("mode", "Change mode"), ("reset", "Clear memory"),
           ("export", "Download history"), ("promo", "Enter a promo code"), ("emoji", "The bot's premium emoji"), ("myid", "My Telegram ID"), ("version", "Bot version")],
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
    app.bot_data["digest_task"] = asyncio.create_task(day_digest(app.bot_data).run(app))  # 22:00 Moscow, every group
    if app.bot_data.get("snapshots"):
        app.bot_data["snapshot_task"] = asyncio.create_task(app.bot_data["snapshots"].run())
    if hosting.updates_enabled():  # not Render: pull new commits from GitHub and restart by itself
        async def before_restart():
            # Answers get 90 s; a serial render can need its full 15-minute budget.
            for elapsed in range(900):
                busy = (ACTIVE["answers"] + len(LIVE_DRAFTS) + len(app.bot_data.get("gen_tasks") or ())
                        + len(chat_serial(app.bot_data).busy))
                if not busy or (elapsed >= 90 and not chat_serial(app.bot_data).busy):
                    break
                await asyncio.sleep(1)
            if app.bot_data.get("snapshots"):
                await asyncio.wait_for(asyncio.to_thread(app.bot_data["snapshots"].save), 25)
        app.bot_data["update_task"] = asyncio.create_task(hosting.self_update(before_restart))
    if (app.bot_data.get("public_url") and hosting.on_render()
            and os.environ.get("KEEP_AWAKE", "1").strip() != "0"):
        app.bot_data["awake_task"] = asyncio.create_task(keep_awake(app))
    owner_code(app.bot_data)  # no owner yet: the console shows the code for /owner
    store = app.bot_data["connections"]
    if store.get_meta("persona_default") != DEFAULT_PERSONA:  # the default style changed: everyone on the old one moves
        memory = app.bot_data["memory"]
        with memory.db:
            memory.db.execute("UPDATE settings SET persona=? WHERE persona='assistant'", (DEFAULT_PERSONA,))
        store.set_meta("persona_default", DEFAULT_PERSONA)
    PREMIUM_EMOJI.update({key: value for key, value in
                          (app.bot_data["connections"].get_meta("premium_emoji") or {}).items() if valid_emoji_key(key)})
    await resolve_abacus_model(app)
    await discover_models(app)
    app.bot_data["serial_task"] = asyncio.create_task(chat_serial(app.bot_data).run(app))
    app.bot_data["previews_task"] = asyncio.create_task(gentemplates.draw_previews(app.bot_data, free_image))
    app.bot_data["cursor_models"] = cursor.models() if "cursor" in ENABLED_MODELS else []
    if app.bot_data["cursor_models"]:
        app.bot_data["cursor_task"] = asyncio.create_task(cursor.install())
    if not app.bot_data.get("logo_id"):
        # The application is not running yet inside post_init, so start a plain task and keep a reference.
        app.bot_data["logo_task"] = asyncio.create_task(ensure_logo(app))
    try:
        await app.bot.set_my_commands([BotCommand(*c) for c in COMMANDS["en"]])
        await app.bot.set_my_commands([BotCommand(*c) for c in COMMANDS["ru"]], language_code="ru")
        group = [BotCommand(game.START, "🧠 Викторина Secret AI для всей группы"),
                 BotCommand(game.RATING, "🏆 Рейтинг викторины"), BotCommand(game.STOP, "⏹ Остановить викторину"),
                 BotCommand(digest.COMMAND, "📊 Итоги дня"),
                 BotCommand("serial", "🎬 Сериал про ваш чат"),
                 BotCommand("q", "Цитата: ответь на сообщение"), BotCommand("dem", "Демотиватор из фото"),
                 BotCommand("meme", "Мем из TikTok")]
        await app.bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats(), language_code="ru")
        await app.bot.set_my_commands([BotCommand(game.START, "🧠 Secret AI quiz for the whole group"),
                                       BotCommand(game.RATING, "🏆 Quiz leaderboard"), BotCommand(game.STOP, "⏹ Stop the quiz"),
                                       BotCommand(digest.COMMAND, "📊 Day summary"),
                                       BotCommand("serial", "🎬 Your group sitcom"),
                                       BotCommand("q", "Quote: reply to a message"), BotCommand("dem", "Demotivator from a photo"),
                                       BotCommand("meme", "A TikTok meme")],
                                      scope=BotCommandScopeAllGroupChats())
        if app.bot_data["public_url"]:
            # The button next to the message field opens the Mini App, like in large AI bots.
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("Secret AI", WebAppInfo(app.bot_data["public_url"] + "/app")))
    except TelegramError:
        LOG.warning("Could not register the command menu")


async def shutdown(app):
    # Stop workers before saving and closing their DB and HTTP clients.
    tasks = {value for key, value in app.bot_data.items()
             if key.endswith("_task") and isinstance(value, asyncio.Task)}
    tasks.update(app.bot_data.get("gen_tasks") or ())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if app.bot_data.get("snapshots"):  # the last minute of memory before the deploy replaces this machine
        try:
            await asyncio.wait_for(asyncio.to_thread(app.bot_data["snapshots"].save), 25)
        except Exception as exc:
            LOG.warning("Final database save failed: %s", type(exc).__name__)
    await app.bot_data["client"].close()
    if hasattr(app.bot_data.get("stt"), "close"):
        await app.bot_data["stt"].close()
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


# ---------- Secret AI Live: a spoken conversation in the Mini App (microphone, camera, interruptions, a summary) ----------
LIVE_IDLE = 1800  # a pause this long starts a new conversation
LIVE_TURNS = 24  # the last messages the model hears in a live talk
LIVE_LOCALES = {"ru": "ru", "de": "de", "en": "en"}
LIVE_VOICES = {"de": "de-DE-KatjaNeural", "en": "en-US-AndrewNeural"}
LIVE_RULES = (
    " This is Secret AI Live: a real-time VOICE call inside the Telegram Mini App. Your words are read aloud, so talk like "
    "a person on a call: short (1-3 sentences, more only when asked to explain), natural, no markdown, no lists, no "
    "emoji, no links read out (say a link will be in the chat). The user may interrupt you; just go on from what they "
    "say. When a [camera] picture comes with the message, it is what the user is showing you right now through the "
    "camera: look at it and answer about it. You can use your tools during the call (mail, calendar, connected apps, "
    "GitHub): changes to a repository are only prepared and the user confirms them in the Telegram chat, say so. "
    "Never claim you did something you didn't.")
LIVE_GERMAN = (
    " Mode: German practice. Speak German at the user's level, keep it a real conversation (ask questions back). After "
    "your German reply, if the user made mistakes, add one short line in Russian starting with «Поправка:» with the "
    "correct phrase and why; for words the speech recognition got oddly (a sign of pronunciation), say briefly how to "
    "pronounce them. If they ask in Russian, explain in Russian, then continue in German.")


class LiveSink:
    """The model's streaming target during a live turn: nothing is shown, the text is just collected."""

    def __init__(self):
        self.text, self.offset = "", 0

    def status(self, note):
        pass

    async def update(self, text):
        self.text = text


def live_session(data, user_id, mode=None):
    sessions = data.setdefault("live", {})
    session = sessions.get(user_id)
    if not session or time.monotonic() - session["last"] > LIVE_IDLE:
        session = sessions[user_id] = {"items": [], "mode": "talk", "last": time.monotonic(), "turns": 0}
    if mode in ("talk", "de"):
        session["mode"] = mode
    session["last"] = time.monotonic()
    return session


def live_clean(text):
    """Spoken text: no markdown marks, emoji or the retired accent."""
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    text = re.sub(r"[*_#`>|]+", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", text)
    return drop_accent(" ".join(text.split()))


async def live_mp3(audio):
    """The reply voice as MP3: every phone's browser plays it (OGG Opus doesn't play on iPhones)."""
    with tempfile.TemporaryDirectory(prefix="secret-ai-live-") as folder:
        folder = Path(folder)
        (folder / "in.audio").write_bytes(audio)
        await media_ffmpeg("-i", str(folder / "in.audio"), "-vn", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k",
                           str(folder / "out.mp3"), timeout=60)
        return (folder / "out.mp3").read_bytes()


async def live_voice(data, text, mode):
    if mode == "de":
        return await speech.edge_speech(text, LIVE_VOICES["de"])  # MP3 already
    return await live_mp3(await speech_audio(data, text))


async def live_turn(app, tg_user, audio, image=None, mode=None):
    """One spoken turn: recognise what was said, answer it (with the camera frame and the user's tools), speak it."""
    data, user_id = app.bot_data, tg_user["id"]
    language = "ru" if (tg_user.get("language_code") or "ru").startswith("ru") else "en"
    session = live_session(data, user_id, mode)
    mode = session["mode"]
    stt_lang = "de" if mode == "de" else language
    try:
        heard = (await data["stt"].audio.transcriptions.create(model="whisper-1", file=io.BytesIO(audio), language=stt_lang)).text
    except Exception as exc:
        LOG.warning("Live: speech not recognised: %s %s", type(exc).__name__, str(exc)[:150])
        heard = ""
    heard = (heard or "").strip()
    if mode == "de" and not heard:  # they may have answered in Russian
        try:
            heard = ((await data["stt"].audio.transcriptions.create(model="whisper-1", file=io.BytesIO(audio), language="ru")).text or "").strip()
        except Exception:
            heard = ""
    if not heard and not image:
        return {"heard": "", "text": "", "audio": None}
    model = user_model(data, user_id)
    if not model:
        return {"error": "models"}
    price = data.get("premium", {}).get(model)
    owner = data["connections"].get_meta("owner") == user_id
    if price and not owner and not data["memory"].gen_charge(user_id, "chat", price):
        return {"error": "credits"}
    context = SimpleNamespace(bot_data=data, bot=app.bot, application=app)
    use_apps = wants_apps(data, user_id, heard)
    taken = [server["label"] for server in data["connections"].public(user_id) if server["service"] == "mcp"]
    apps = await app_tools(data, user_id, taken) if use_apps else ([], [])
    toolbox = Toolbox(data["http"], data["connections"], data["pending"], user_id, language, apps=apps,
                      extra=[memory_tool(context, user_id)])
    if not use_apps:
        toolbox.mcp = []
    instructions = (SYSTEM + now_note() + ACCENT + LIVE_RULES + (LIVE_GERMAN if mode == "de" else "") + model_identity(model)
                    + " Telegram language: " + (tg_user.get("language_code") or "ru"))
    if toolbox.instructions():
        instructions += " " + toolbox.instructions()
    said = heard or ("Что ты видишь?" if language == "ru" else "What do you see?")
    content = said if not image else [{"type": "input_text", "text": "[camera] " + said},
                                      {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}]
    history = session["items"][-LIVE_TURNS:]
    answer = ""
    for attempt in dict.fromkeys([model] + [m for m in (data.get("fallbacks") or []) if m != model][:2]):
        try:
            answer, _ = await generate(context, LiveSink(), toolbox, model=attempt, instructions=instructions,
                                       input=history + [{"role": "user", "content": content}], store=False,
                                       max_output_tokens=900)
            break
        except MODEL_ERRORS as exc:
            LOG.warning("Live: model %s failed: %s %s", attempt, type(exc).__name__, str(exc)[:200])
            drop_model(data, attempt, exc)
    answer = keep_language(live_clean(answer), said)
    if not answer:
        if price and not owner:
            data["memory"].gen_refund(user_id, "credits", price)
        return {"error": "model", "heard": heard}
    session["items"] += [{"role": "user", "content": ("[showed the camera] " if image else "") + said},
                         {"role": "assistant", "content": answer}]
    session["turns"] += 1
    if toolbox.proposals:  # repository changes wait for Apply in the chat, as always
        try:
            ru = language == "ru"
            target = await app.bot.send_message(user_id, "🎙 Secret AI Live: " + ("изменение готово, подтверди ниже ↓" if ru
                                                                                 else "a change is ready, confirm below ↓"))
            user = User(id=user_id, first_name=tg_user.get("first_name") or "", is_bot=False, language_code=tg_user.get("language_code"))
            await show_proposals(MiniAppUpdate(user, target), context, toolbox, target)
        except TelegramError as exc:
            LOG.warning("Live: could not post the change: %s", type(exc).__name__)
    try:
        voice = await live_voice(data, answer, mode)
    except Exception as exc:  # the text still shows on the screen
        LOG.warning("Live: no voice: %s %s", type(exc).__name__, str(exc)[:150])
        voice = None
    return {"heard": heard, "text": answer, "audio": base64.b64encode(voice).decode() if voice else None}


async def live_end(app, tg_user):
    """The call is over: a short summary (notes, links, agreements) goes to the chat and into its history."""
    data, user_id = app.bot_data, tg_user["id"]
    session = data.get("live", {}).pop(user_id, None)
    if not session or not session["items"]:
        return {"ok": True, "sent": False}
    ru = (tg_user.get("language_code") or "ru").startswith("ru")
    talk = "\n".join(f"{'User' if i['role'] == 'user' else 'Secret AI'}: {i['content']}" for i in session["items"])[-14000:]
    ask = ("Сделай итог этого голосового разговора для чата Telegram. Коротко: о чём говорили (1-2 предложения), затем "
           "разделы «Заметки», «Ссылки», «Договорённости и задачи» — каждый пункт с «• », пустые разделы пропусти. "
           "Без markdown-звёздочек. Пиши на языке разговора." if ru else
           "Summarise this voice conversation for a Telegram chat: what it was about (1-2 sentences), then «Notes», "
           "«Links», «Agreements and tasks» with «• » bullets, skipping empty ones. No markdown asterisks.")
    summary = ""
    for model in dict.fromkeys([data.get("chat_default"), user_model(data, user_id)]):
        if not model:
            continue
        try:
            summary = (await quick_answer(data, model, ask, talk, max_tokens=1200)).strip()
        except Exception as exc:
            LOG.warning("Live summary with %s failed: %s", model, type(exc).__name__)
            continue
        if summary:
            break
    if not summary:
        summary = talk[-3500:]
    title = "🎙 Итог разговора в Secret AI Live" if ru else "🎙 Secret AI Live summary"
    try:
        await app.bot.send_message(user_id, f"{title}\n\n{summary}"[:4000])
        data["memory"].append_turn(user_id, "[a Secret AI Live voice call in the Mini App]", f"[Summary of the call]\n{summary}"[:12000])
    except Exception as exc:
        LOG.warning("Live summary not sent: %s", type(exc).__name__)
        return {"ok": True, "sent": False}
    return {"ok": True, "sent": True}


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

    def _run_long(self, coro, seconds):
        loop = self.app.bot_data.get("loop")
        if not loop:
            coro.close()
            raise RuntimeError("bot is not running yet")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=seconds)

    def live_turn(self, tg_user, audio, image=None, mode=None):
        return self._run_long(live_turn(self.app, tg_user, audio, image, mode), 150)

    def live_end(self, tg_user):
        return self._run_long(live_end(self.app, tg_user), 90)

    def ask(self, tg_user, text):
        self._run(run_prompt(self.app, tg_user, text), wait=False)

    def settings(self, user_id):
        async def read():
            data, memory = self.app.bot_data, self.app.bot_data["memory"]
            return {"persona": memory.persona(user_id), "model": user_model(data, user_id),
                    "models": models_for(data, user_id),
                    "premium": {} if data["connections"].get_meta("owner") == user_id else data.get("premium", {}), "credits": memory.gen_info(user_id)[1],
                    "messages": memory.count(user_id),
                    "personas": [{"id": pid, "emoji": p[0], "ru": p[1], "en": p[2]} for pid, p in PERSONAS.items()]}
        return self._run(read())

    def update(self, user_id, persona=None, model=None):
        async def write():
            memory = self.app.bot_data["memory"]
            if persona in PERSONAS:
                memory.set_persona(user_id, persona)
            if model in models_for(self.app.bot_data, user_id):
                memory.set_model(user_id, model)
        self._run(write())

    def gen_state(self, user_id):
        async def read():
            return gen_state(self.app.bot_data, user_id)
        return self._run(read())

    def generate(self, tg_user, kind, prompt, fmt, seconds=5, voice=True):
        language = "ru" if (tg_user.get("language_code") or "ru").startswith("ru") else "en"
        return self._run(start_generation(self.app, tg_user["id"], language, kind, prompt, fmt, seconds, voice))

    def promo(self, tg_user, code):
        """Mini App: apply a promo code; (ok, text)."""
        ru = (tg_user.get("language_code") or "ru").startswith("ru")
        async def apply():
            return redeem_promo(self.app.bot_data, tg_user["id"], code, ru)
        return self._run(apply())

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

    def delete_group(self, user_id, chat_id):
        """A group's admin removes it: the bot leaves the group and forgets it. Returns False if not allowed."""
        async def remove():
            try:
                member = await self.app.bot.get_chat_member(chat_id, user_id)
            except TelegramError:
                return False
            if member.status not in ("administrator", "creator"):
                return False
            try:
                await self.app.bot.leave_chat(chat_id)
            except TelegramError as exc:  # already removed: forgetting is still right
                LOG.info("Leaving group %s: %s", chat_id, type(exc).__name__)
            self.app.bot_data["groups"].forget(chat_id)
            for key in ("group_count", "group_due", "group_surprised", "group_spoke"):
                self.app.bot_data.get(key, {}).pop(chat_id, None)
            return True
        return self._run(remove())

    def set_group_model(self, user_id, chat_id, model):
        """Admins choose the group's model ('' = default). Returns False if not allowed."""
        async def write():
            if model and model not in free_models(self.app.bot_data):  # paid models are for private chats
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

    def codex_start(self, tg_user, mode, prompt, repo="", thread=None):
        async def start():
            return codex_start(self.app, tg_user, mode, prompt, repo, thread)
        return self._run(start())

    def codex_stop(self, user_id, task_id):
        async def stop():
            task = self.app.bot_data["codex"].get(task_id, user_id)
            job = self.app.bot_data["codex_jobs"].get(task_id)
            if task and job:
                job.cancel()
            return bool(task and job)
        return self._run(stop())

    def codex_apply(self, user_id, task_id):
        return self._run(codex_apply(self.app, user_id, task_id))

    def codex_repos(self, user_id):
        return self._run(codex_repos(self.app, user_id))

    def codex_send(self, user_id, task_id):
        return self._run(codex_send(self.app, user_id, task_id))

    def codex_site(self, user_id, task_id):
        """Publish a project task's index.html and return its link."""
        task = self.app.bot_data["codex"].get(task_id, user_id)
        files = (task or {}).get("files") or {}
        page = files.get("index.html") or next((t for p, t in files.items() if p.endswith(".html")), None)
        if not page or not self.app.bot_data["public_url"]:
            return None
        site_id = self.app.bot_data["connections"].save_site(page.encode())
        return f"{self.app.bot_data['public_url']}/s/{site_id}"

    def reset(self, user_id):
        async def clear():
            async with self.app.bot_data["locks"][user_id]:
                self.app.bot_data["memory"].reset(user_id)
        self._run(clear())


# ----- Codex mode (Mini App): an agent that works on a task by itself -----
CODEX_PARALLEL = 1  # running tasks per person (the owner: 3)
CODEX_PRICE = 3  # a task on a paid model costs this many answers


class RepoTools:
    """The GitHub tools of a Toolbox for one repository task: read, list, prepare one commit (nothing destructive)."""

    def __init__(self, toolbox):
        self.toolbox, self.proposals = toolbox, toolbox.proposals

    def definitions(self):
        return [t for t in GITHUB_TOOLS if t["name"] in ("github_list_files", "github_read_file", "github_propose_changes")]

    def status(self, name, args):
        return self.toolbox.status(name, args)

    async def run(self, name, args):
        return await self.toolbox.run(name, args)


def codex_owner(data, user_id):
    return data["connections"].get_meta("owner") == user_id


def codex_model(data, model):
    """Old tasks also respect the current allowlist after the model catalog changes."""
    return model if model in data.get("models", []) else data.get("chat_default")


def codex_can_run(data, user_id):
    """The run tool: the owner only, and only where the sandbox works."""
    return codex_owner(data, user_id) and codex.sandbox_ready()


async def run_codex(app, task_id, language, charged=0):
    data, store = app.bot_data, app.bot_data["codex"]
    task = store.get(task_id)
    user_id = task["user_id"]
    ru = language == "ru"
    branch = task.get("branch") or f"codex/{codex.slug(task['prompt'])}-{task_id[:4]}"
    try:  # everything, the GitHub setup included: a failure always ends the task and gives the credits back
        context = SimpleNamespace(bot_data=data, bot=app.bot, application=app)
        live = codex.TaskLive(store, task_id, language)
        earlier = [t for t in store.thread(task["thread"], user_id) if t["id"] != task_id and t["created"] <= task["created"]]
        can_run = await asyncio.to_thread(codex_can_run, data, user_id)
        if task["mode"] == "repo":
            await data["github_apps"].fresh_token(data["http"], user_id)
            tools = RepoTools(Toolbox(data["http"], data["connections"], data["pending"], user_id, language))
        else:
            tools = codex.Workspace(store, task_id, can_run=can_run, language=language)
        live.status("Изучаю задачу" if ru else "Reading the task")
        answer, _ = await generate(
            context, live, tools, model=codex_model(data, task["model"]), store=False, max_output_tokens=32000, rounds=codex.ROUNDS,
            api_timeout=180, stream_retries=1,
            instructions=codex.instructions(task["mode"], task["repo"], branch, can_run, language),
            input=codex.history(earlier) + [{"role": "user", "content": (f"Repository: {task['repo']}\n\n" if task["repo"] and not earlier
                                                                          else "") + task["prompt"]}])
    except asyncio.CancelledError:  # stopped by the user: nothing was delivered, so the credits come back
        if charged:
            data["memory"].gen_refund(user_id, "credits", charged)
        store.update(task_id, status=codex.STATUS_STOPPED, step="")
        raise
    except Exception as exc:
        LOG.warning("Codex task failed: %s %s", type(exc).__name__, str(exc)[:200])
        if charged:
            data["memory"].gen_refund(user_id, "credits", charged)
        if isinstance(exc, APITimeoutError):
            error = ("Модель не ответила вовремя. Повторите запрос или выберите другую модель." if ru else
                     "The model did not respond in time. Retry the request or choose another model.")
        else:
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
        store.update(task_id, status=codex.STATUS_FAILED, step="", error=error)
        return
    finally:
        data["codex_jobs"].pop(task_id, None)
    change = None
    if task["mode"] == "repo" and tools.proposals:
        change = data["pending"].pop(tools.proposals[-1], None)
        if change and change["branch"] == change["default"]:  # Codex never writes to the main branch: a branch and a PR
            change.update(branch=branch, branch_exists=False, pull_request=True)
    store.update(task_id, status=codex.STATUS_DONE, step="", summary=(answer or "").strip()[:8000], change=change)
    try:
        title = task["prompt"][:60] + ("…" if len(task["prompt"]) > 60 else "")
        await app.bot.send_message(user_id, (f"✅ Codex: «{title}» готово. Открой Secret AI → Codex." if ru else
                                             f"✅ Codex: “{title}” is done. Open Secret AI → Codex."))
    except TelegramError:
        pass


def codex_start(app, tg_user, mode, prompt, repo, thread=None):
    """Start a task (a new chat, or the next message of `thread`); returns (task, None) or (None, error code)."""
    data, user_id = app.bot_data, tg_user["id"]
    language = "ru" if (tg_user.get("language_code") or "ru").startswith("ru") else "en"
    store = data["codex"]
    running = [t for t in store.list(user_id) if t["status"] == codex.STATUS_RUNNING]
    if len(running) >= (3 if codex_owner(data, user_id) else CODEX_PARALLEL):
        return None, "busy"
    files, branch = {}, ""
    if thread:
        chat = store.thread(thread, user_id)
        if not chat:
            return None, "thread"
        if chat[-1]["status"] == codex.STATUS_RUNNING:
            return None, "busy"
        mode, repo = chat[0]["mode"], chat[0]["repo"]
        files = chat[-1].get("files") or {}  # a project goes on from its latest files
        applied = [t for t in chat if t["status"] == codex.STATUS_APPLIED and t.get("change")]
        branch = applied[-1]["change"]["branch"] if applied else (chat[-1].get("branch") or "")
    if mode == "repo":
        if not data["connections"].get(user_id, "github"):
            return None, "github"
        repo = repo.strip()
        if not repo:
            return None, "repo"
    model = codex_model(data, user_model(data, user_id))
    if not model:
        return None, "models"
    price = data.get("premium", {}).get(model)
    charged = 0
    if price and not codex_owner(data, user_id):
        charged = price * CODEX_PRICE
        if not data["memory"].gen_charge(user_id, "chat", charged):
            return None, "credits"
    task = store.create(user_id, mode, prompt, repo if mode == "repo" else "", model, thread=thread, files=files, branch=branch)
    if mode == "repo" and not branch:
        store.update(task["id"], branch=f"codex/{codex.slug(prompt)}-{task['id'][:4]}")
    job = asyncio.create_task(run_codex(app, task["id"], language, charged))
    data["codex_jobs"][task["id"]] = job
    return task, None


async def codex_apply(app, user_id, task_id):
    """Commit a repository task's change on its branch and open the pull request."""
    data, store = app.bot_data, app.bot_data["codex"]
    task = store.get(task_id, user_id)
    if not task or not task.get("change") or task["status"] != codex.STATUS_DONE:
        raise GitHubError("nothing to apply")
    await data["github_apps"].fresh_token(data["http"], user_id)
    commit, pull = await apply_change(data["http"], data["connections"], task["change"])
    store.update(task_id, status=codex.STATUS_APPLIED, result={"commit": commit, "pull": pull})
    return store.get(task_id)


async def codex_repos(app, user_id):
    data = app.bot_data
    github = data["connections"].get(user_id, "github")
    if not github:
        return []
    await data["github_apps"].fresh_token(data["http"], user_id)
    github = data["connections"].get(user_id, "github")
    repos = await GitHub(data["http"], github[0]["token"]).call("GET", "/user/repos", params={"sort": "updated", "per_page": 50})
    return [{"name": r["full_name"], "private": r["private"], "branch": r["default_branch"]} for r in repos]


async def codex_send(app, user_id, task_id):
    """A project task's files into the chat: one file as is, several as a zip."""
    import zipfile
    task = app.bot_data["codex"].get(task_id, user_id)
    files = (task or {}).get("files") or {}
    if not files:
        return False
    if len(files) == 1:
        path, text = next(iter(files.items()))
        await app.bot.send_document(user_id, InputFile(text.encode(), filename=path.rsplit("/", 1)[-1]), caption="Codex · " + path)
        return True
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    name = codex.slug(task["prompt"]) + ".zip"
    await app.bot.send_document(user_id, InputFile(buffer.getvalue(), filename=name),
                                caption=f"📦 Codex · {len(files)}")
    return True


def main():
    logging.basicConfig(level=logging.WARNING,
                        format=os.environ.get("LOG_PREFIX", "") + "%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in (LOG.name, "persist", "media"):  # our own notes (voice used, database restored…) are worth seeing
        logging.getLogger(name).setLevel(logging.INFO)
    hosting.load_env()  # bot hosting panels (h1cloud…) have no environment settings: a .env file instead
    token, key = os.environ.get("TELEGRAM_TOKEN", "").strip(), os.environ.get("OPENAI_API_KEY", "").strip()
    if not token:
        raise SystemExit("Set the TELEGRAM_TOKEN environment variable.")
    # OpenAI is off unless asked for (OPENAI_ENABLED=1 with a key): chat goes through Abacus and AnyModel, voices are
    # recognized by a local Whisper and spoken by AnyModel or Microsoft's free voices.
    use_openai = bool(key) and os.environ.get("OPENAI_ENABLED", "0").strip() == "1"
    try:
        allowed = parse_allowed(os.environ.get("ALLOWED_USERS", ""))
        port = int(hosting.default_port())
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
    hosting.start_test_bot(port)  # TEST_TELEGRAM_TOKEN in .env: a test bot on the newest code runs next to this one
    tunnel = hosting.start_tunnel(port)
    if tunnel:  # the tunnel's address changes on every start: a Cloudflare Worker gives a permanent one in front of it
        tunnel = hosting.stable_address(tunnel) or tunnel
    public_url = (tunnel or os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
                  or "").strip().rstrip("/")
    if public_url and not public_url.startswith("https://"):
        raise SystemExit("PUBLIC_URL must start with https:// (Telegram Mini Apps require HTTPS).")
    # Service tokens are encrypted with ENCRYPTION_KEY; without it the key is derived from the bot token.
    owner = os.environ.get("OWNER_ID", "").strip()
    connections = Connections(database, os.environ.get("ENCRYPTION_KEY", "").strip() or token,
                              int(owner) if owner.isdigit() else None)
    # Discovery fills the chat catalog only after the owner explicitly adds models.
    model, models = None, []
    direct = DirectApps(connections, public_url)
    github_apps = GitHubApps(connections, os.environ.get("GITHUB_CLIENT_ID", "").strip(), os.environ.get("GITHUB_CLIENT_SECRET", "").strip())
    pipedream = Pipedream(os.environ.get("PIPEDREAM_CLIENT_ID", "").strip(), os.environ.get("PIPEDREAM_CLIENT_SECRET", "").strip(),
                          os.environ.get("PIPEDREAM_PROJECT_ID", "").strip(), os.environ.get("PIPEDREAM_ENVIRONMENT", "production").strip(),
                          os.environ.get("PIPEDREAM_MCP_URL", "").strip())
    app = Application.builder().token(token).concurrent_updates(True).post_init(post_init).post_shutdown(shutdown).build()
    app.bot_data.update(
        connections=connections, http=httpx.AsyncClient(), pending={}, public_url=public_url,
        client=AsyncOpenAI(api_key=key or "off", timeout=120, max_retries=2), openai=use_openai,
        stt=AsyncOpenAI(api_key=key, timeout=120, max_retries=2) if use_openai else speech.GoogleSpeech(),
        memory=Memory(database), allowed=allowed, snapshots=snapshots, codex=codex.CodexStore(connections), codex_jobs={},
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
        chat_default=None, chat_override="",
        hf_spaces=tuple(x.strip() for x in os.environ.get("HF_SPACES", "").split(",") if x.strip()) or HF_SPACES,
        kling_api=os.environ.get("KLING_API_BASE", "").strip().rstrip("/") or KLING_API,
        cloudflare=(os.environ.get("CF_ACCOUNT_ID", "").strip(), os.environ.get("CF_API_TOKEN", "").strip()),
        abacus=AsyncOpenAI(api_key=os.environ["ABACUS_API_KEY"].strip(), base_url=ABACUS_URL,
                           timeout=httpx.Timeout(120, connect=8, read=45), max_retries=0)
        if os.environ.get("ABACUS_API_KEY", "").strip() else None,
        **{name: AsyncOpenAI(api_key=os.environ[f"{name.upper()}_API_KEY"].strip(), base_url=url, timeout=60, max_retries=1)
           for name, url in FAST_PROVIDERS.items() if os.environ.get(f"{name.upper()}_API_KEY", "").strip()},
        **{name: AsyncOpenAI(api_key=os.environ[f"{name.upper()}_API_KEY"].strip(), timeout=120, max_retries=1,
                             base_url=os.environ.get(f"{name.upper()}_BASE", "").strip().rstrip("/") or spec[0])
           for name, spec in EXTRA_PROVIDERS.items() if os.environ.get(f"{name.upper()}_API_KEY", "").strip()},
        anymodel_chat=AsyncOpenAI(api_key=os.environ["ANYMODEL_API_KEY"].strip().removeprefix("Bearer ").strip(),
                                  base_url=os.environ.get("ANYMODEL_BASE", "").strip().rstrip("/") or ANYMODEL_API,
                                  timeout=httpx.Timeout(120, connect=8, read=45), max_retries=0) if os.environ.get("ANYMODEL_API_KEY", "").strip() else None,
        abacus_model=os.environ.get("ABACUS_MODEL", "").strip(),
        abacus_image=os.environ.get("ABACUS_IMAGE_MODEL", "").strip(),
        image_model=os.environ.get("OPENAI_IMAGE_MODEL", "").strip(),
        web_search=os.environ.get("WEB_SEARCH", "1").strip() != "0" and use_openai,
        web_wanted=os.environ.get("WEB_SEARCH", "1").strip() != "0",
        # Low effort: the first words come much sooner; set REASONING_EFFORT=medium/high for deeper answers.
        reasoning=os.environ.get("REASONING_EFFORT", "low").strip(),
        chat_reasoning=os.environ.get("CHAT_REASONING_EFFORT", "low").strip(),
        locks=defaultdict(asyncio.Lock), last_answer={}, groups=GroupStore(database), group_spoke={},
        biz_activity=sec.Activity())
    # Business updates first: their messages must not reach the regular chat handlers.
    app.add_handler(TypeHandler(Update, stop_generation, block=False), group=-2)
    app.add_handler(BusinessConnectionHandler(business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGES, business_message))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["secretary", "sekretar"], secretary_command))
    app.add_handler(CommandHandler(["voice", "golos"], voice_command))
    app.add_handler(CommandHandler(["speed", "skorost"], speed_command))
    app.add_handler(CommandHandler("wipe", wipe))
    app.add_handler(CommandHandler("welcome", welcome_command))
    app.add_handler(CommandHandler("setwelcome", set_welcome))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mode", mode))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("emoji", emoji_command))
    app.add_handler(CommandHandler("promo", promo_command))
    app.add_handler(CommandHandler("owner", owner_command))
    app.add_handler(CommandHandler(["version", "v"], version_command))
    # "/version" pasted as formatted text isn't a command for Telegram: catch it as text too.
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\s*/(version|v)(@\w+)?\s*$"), version_command))
    app.add_handler(CommandHandler("emojiid", emojiid))
    app.add_handler(CommandHandler(["app", "connect"], open_app))
    app.add_handler(CommandHandler(["q", "quote"], quote_command))
    app.add_handler(CommandHandler(["dem", "demotivator"], demotivator_command))
    app.add_handler(CommandHandler(["meme", "mem"], meme_command))
    # Unusual names on purpose: other bots in a group react to /game, /quiz and /top too.
    app.add_handler(CommandHandler([game.START, "svik"], game_command))
    app.add_handler(CommandHandler([game.RATING, "stop10"], top_command))
    app.add_handler(CommandHandler(game.STOP, stop_game_command))
    app.add_handler(CommandHandler([digest.COMMAND, "sitogi"], day_command))
    app.add_handler(CommandHandler("serial", serial_command))
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
        "bridge": Bridge(app), "pipedream": pipedream, "codex": lambda: app.bot_data["codex"]})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app.run_polling(allowed_updates=["message", "callback_query", "pre_checkout_query", "business_connection", "business_message",
                                        "stopped_message_generation"], drop_pending_updates=False, bootstrap_retries=3)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        hosting.stop_tunnel()


if __name__ == "__main__":
    main()
