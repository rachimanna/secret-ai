"""Group fun: the chat's memory (messages, stickers, gifs, photos, catchphrases) and picture memes.

Everything a group posts is remembered per chat in SQLite, so the bot knows the conversation and can throw the
chat's own stickers, gifs and favourite phrases back, make quote pictures (the mascot "says" a member's message)
and demotivators from the chat's photos.
"""
import io
import random
import re
import sqlite3
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ASSETS = Path(__file__).with_name("assets")
GROUP_MESSAGES = 300_000  # kept per chat ("forever"); the model sees the last GROUP_CONTEXT and searches the rest
GROUP_CONTEXT = 600
GROUP_CONTEXT_CHARS = 120_000  # and at most this much text of them, newest first
MEDIA_PER_KIND = 150
PHRASES_PER_CHAT = 400
# How often the bot does something on its own in a group ("Сглыпа-режим"): chance per message nobody addressed to it.
LEVELS = {"off": 0.0, "rare": 0.04, "normal": 0.1, "often": 0.2}


def ago(created):
    """How long ago, in words: "сегодня", "вчера", "5 дн. назад", "2 мес. назад"."""
    if not created:
        return "давно"
    days = (time.time() - created) / 86400
    if days < 1:
        return "сегодня"
    if days < 2:
        return "вчера"
    if days < 30:
        return f"{int(days)} дн. назад"
    if days < 365:
        return f"{int(days // 30)} мес. назад"
    return f"{int(days // 365)} г. назад"


class GroupStore:
    """Per-chat memory of a group. Used only from the bot's event loop."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.create_function("ulower", 1, lambda text: text.lower() if isinstance(text, str) else text, deterministic=True)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS group_messages (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, "
                            "line TEXT NOT NULL, created INTEGER NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS group_messages_chat ON group_messages(chat_id, id)")
            columns = [row[1] for row in self.db.execute("PRAGMA table_info(group_messages)")]
            if "message_id" not in columns:
                self.db.execute("ALTER TABLE group_messages ADD COLUMN message_id INTEGER")
            if "user_id" not in columns:  # who said it: the private chat remembers what a person said in groups
                self.db.execute("ALTER TABLE group_messages ADD COLUMN user_id INTEGER")
            self.db.execute("CREATE INDEX IF NOT EXISTS group_messages_user ON group_messages(user_id, id)")
            self.db.execute("CREATE TABLE IF NOT EXISTS group_media (chat_id INTEGER NOT NULL, kind TEXT NOT NULL, "
                            "unique_id TEXT NOT NULL, file_id TEXT NOT NULL, seen INTEGER NOT NULL, added INTEGER NOT NULL, "
                            "PRIMARY KEY (chat_id, unique_id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS group_info (chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '', "
                            "level TEXT NOT NULL DEFAULT 'normal')")
            if "model" not in [row[1] for row in self.db.execute("PRAGMA table_info(group_info)")]:
                self.db.execute("ALTER TABLE group_info ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            self.db.execute("CREATE TABLE IF NOT EXISTS group_members (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                            "last INTEGER NOT NULL, PRIMARY KEY (chat_id, user_id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS group_sent (chat_id INTEGER NOT NULL, key TEXT NOT NULL, "
                            "created INTEGER NOT NULL, PRIMARY KEY (chat_id, key))")
            self.db.execute("CREATE TABLE IF NOT EXISTS group_phrases (chat_id INTEGER NOT NULL, key TEXT NOT NULL, "
                            "phrase TEXT NOT NULL, count INTEGER NOT NULL, last INTEGER NOT NULL, PRIMARY KEY (chat_id, key))")

    # ----- conversation -----

    def add_line(self, chat_id, line, message_id=None, user_id=None):
        with self.db:
            self.db.execute("INSERT INTO group_messages(chat_id, line, created, message_id, user_id) VALUES (?, ?, ?, ?, ?)",
                            (chat_id, line[:1500], int(time.time()), message_id, user_id))
            # Everything older than the newest GROUP_MESSAGES goes (one index lookup, cheap however many are kept).
            self.db.execute("DELETE FROM group_messages WHERE chat_id=? AND id <= (SELECT id FROM group_messages "
                            "WHERE chat_id=? ORDER BY id DESC LIMIT 1 OFFSET ?)", (chat_id, chat_id, GROUP_MESSAGES))

    def lines(self, chat_id, limit=GROUP_CONTEXT, chars=GROUP_CONTEXT_CHARS):
        rows = self.db.execute("SELECT line FROM group_messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                               (chat_id, limit)).fetchall()
        kept, total = [], 0
        for (line,) in rows:  # newest first, until the text budget is spent
            total += len(line) + 1
            if total > chars:
                break
            kept.append(line)
        return kept[::-1]

    def user_lines(self, user_id, limit=40):
        """What this person said in groups lately, oldest first: [(group title, line)]."""
        rows = self.db.execute("SELECT COALESCE(i.title, ''), m.line FROM group_messages m LEFT JOIN group_info i "
                               "ON i.chat_id = m.chat_id WHERE m.user_id=? ORDER BY m.id DESC LIMIT ?",
                               (user_id, limit)).fetchall()
        return rows[::-1]

    def search(self, chat_id, text, skip_recent=GROUP_CONTEXT, limit=20):
        """Older lines (beyond the recent window) sharing words with text: the best matches, each with the message
        before and after it for context, oldest first and marked with how long ago ("[2 мес. назад] Name: …")."""
        words = list(dict.fromkeys(w for w in re.findall(r"\w{4,}", (text or "").lower()) if not w.isdigit()))[:8]
        if not words:
            return []
        stems = [w[:max(4, len(w) - 2)] for w in words]  # "айлы", "айле" -> "айл…": Russian endings vary
        # SQLite's own LIKE (fast, no Python per row) ignores case only for Latin letters: try "ахмед" and "Ахмед".
        variants = list(dict.fromkeys(v for s in stems for v in (s, s.capitalize())))
        rows = self.db.execute("SELECT id, line FROM group_messages WHERE chat_id=? AND id < (SELECT COALESCE(MIN(id), 0) "
                               "FROM (SELECT id FROM group_messages WHERE chat_id=? ORDER BY id DESC LIMIT ?)) AND ("
                               + " OR ".join("line LIKE ?" for _ in variants) + ") ORDER BY id DESC LIMIT 2000",
                               (chat_id, chat_id, skip_recent, *[f"%{v}%" for v in variants])).fetchall()
        best = sorted(rows, key=lambda r: (-sum(s in r[1].lower() for s in stems), -r[0]))[:limit]
        if not best:
            return []
        oldest_recent = self.db.execute("SELECT COALESCE(MIN(id), 0) FROM (SELECT id FROM group_messages WHERE chat_id=? "
                                        "ORDER BY id DESC LIMIT ?)", (chat_id, skip_recent)).fetchone()[0]
        ids = set()
        for id_, _ in best:
            ids.add(id_)
            for row in self.db.execute("SELECT id FROM (SELECT id FROM group_messages WHERE chat_id=? AND id < ? ORDER BY id "
                                       "DESC LIMIT 1) UNION SELECT id FROM (SELECT id FROM group_messages WHERE chat_id=? "
                                       "AND id > ? AND id < ? ORDER BY id LIMIT 1)", (chat_id, id_, chat_id, id_, oldest_recent)):
                ids.add(row[0])
        found = self.db.execute(f"SELECT created, line FROM group_messages WHERE id IN ({','.join('?' * len(ids))}) "
                                "ORDER BY id", tuple(ids)).fetchall()
        return [f"[{ago(created)}] {line}" for created, line in found]

    def recent_messages(self, chat_id, limit=15):
        """Recent members' messages that can be replied to: [(message_id, line)], newest first."""
        return self.db.execute("SELECT message_id, line FROM group_messages WHERE chat_id=? AND message_id IS NOT NULL "
                               "AND line NOT LIKE 'Secret AI:%' ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()

    # ----- stickers, gifs, photos -----

    def add_media(self, chat_id, kind, unique_id, file_id):
        now = int(time.time())
        with self.db:
            self.db.execute("INSERT INTO group_media(chat_id, kind, unique_id, file_id, seen, added) VALUES (?, ?, ?, ?, 1, ?) "
                            "ON CONFLICT(chat_id, unique_id) DO UPDATE SET seen=seen+1, file_id=excluded.file_id, "
                            "added=excluded.added", (chat_id, kind, unique_id, file_id, now))
            self.db.execute("DELETE FROM group_media WHERE chat_id=? AND kind=? AND unique_id NOT IN "
                            "(SELECT unique_id FROM group_media WHERE chat_id=? AND kind=? ORDER BY added DESC LIMIT ?)",
                            (chat_id, kind, chat_id, kind, MEDIA_PER_KIND))

    def random_media(self, chat_id, kinds):
        """A sticker/gif/photo the chat posted; popular ones come up more often."""
        marks = ",".join("?" * len(kinds))
        rows = self.db.execute(f"SELECT kind, file_id, seen FROM group_media WHERE chat_id=? AND kind IN ({marks})",
                               (chat_id, *kinds)).fetchall()
        if not rows:
            return None
        kind, file_id, _ = random.choices(rows, weights=[1 + min(seen, 5) for *_, seen in rows])[0]
        return kind, file_id

    def has_media(self, chat_id, kind):
        return bool(self.db.execute("SELECT 1 FROM group_media WHERE chat_id=? AND kind=? LIMIT 1", (chat_id, kind)).fetchone())

    def forget_media(self, chat_id, file_id):
        with self.db:
            self.db.execute("DELETE FROM group_media WHERE chat_id=? AND file_id=?", (chat_id, file_id))

    # ----- catchphrases -----

    def add_phrase(self, chat_id, text):
        phrase = " ".join(text.split())
        if not 2 <= len(phrase) <= 80 or phrase.startswith("/") or "http" in phrase:
            return
        key = phrase.lower().strip(" .!?)(")
        if not key:
            return
        with self.db:
            self.db.execute("INSERT INTO group_phrases(chat_id, key, phrase, count, last) VALUES (?, ?, ?, 1, ?) "
                            "ON CONFLICT(chat_id, key) DO UPDATE SET count=count+1, phrase=excluded.phrase, last=excluded.last",
                            (chat_id, key, phrase, int(time.time())))
            self.db.execute("DELETE FROM group_phrases WHERE chat_id=? AND key NOT IN (SELECT key FROM group_phrases "
                            "WHERE chat_id=? ORDER BY count DESC, last DESC LIMIT ?)", (chat_id, chat_id, PHRASES_PER_CHAT))

    def catchphrase(self, chat_id):
        """A phrase the chat says often (at least twice), or None."""
        rows = self.db.execute("SELECT phrase, count FROM group_phrases WHERE chat_id=? AND count >= 2 "
                               "ORDER BY count DESC LIMIT 40", (chat_id,)).fetchall()
        if not rows:
            return None
        return random.choices([r[0] for r in rows], weights=[r[1] for r in rows])[0]

    def was_sent(self, chat_id, key):
        return bool(self.db.execute("SELECT 1 FROM group_sent WHERE chat_id=? AND key=?", (chat_id, key)).fetchone())

    def sent_keys(self, chat_id):
        return {row[0] for row in self.db.execute("SELECT key FROM group_sent WHERE chat_id=?", (chat_id,))}

    def mark_sent(self, chat_id, key):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO group_sent(chat_id, key, created) VALUES (?, ?, ?)", (chat_id, key, int(time.time())))
            self.db.execute("DELETE FROM group_sent WHERE chat_id=? AND key NOT IN (SELECT key FROM group_sent WHERE chat_id=? "
                            "ORDER BY created DESC LIMIT 500)", (chat_id, chat_id))

    # ----- groups in the Mini App -----

    def touch(self, chat_id, title, user_id):
        """Remember the group's title and that this member writes there (the Mini App lists a member's groups)."""
        now = int(time.time())
        with self.db:
            self.db.execute("INSERT INTO group_info(chat_id, title) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                            "title=excluded.title", (chat_id, (title or "")[:120]))
            if user_id:
                self.db.execute("INSERT INTO group_members(chat_id, user_id, last) VALUES (?, ?, ?) "
                                "ON CONFLICT(chat_id, user_id) DO UPDATE SET last=excluded.last", (chat_id, user_id, now))

    def level(self, chat_id):
        row = self.db.execute("SELECT level FROM group_info WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row and row[0] in LEVELS else "normal"

    def set_level(self, chat_id, level):
        with self.db:
            self.db.execute("INSERT INTO group_info(chat_id, level) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                            "level=excluded.level", (chat_id, level))

    def model(self, chat_id):
        """The model this group talks with ('' = the bot's default)."""
        row = self.db.execute("SELECT model FROM group_info WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else ""

    def set_model(self, chat_id, model):
        with self.db:
            self.db.execute("INSERT INTO group_info(chat_id, model) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                            "model=excluded.model", (chat_id, model))

    def forget(self, chat_id):
        """Everything the bot keeps about a group: messages, stickers, settings, games, summaries."""
        tables = [row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        with self.db:
            for table in ("group_messages", "group_media", "group_info", "group_members", "group_sent", "group_phrases",
                          "game_scores", "game_asked", "group_digest"):
                if table in tables:
                    self.db.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))

    def groups_for(self, user_id):
        rows = self.db.execute("SELECT i.chat_id, i.title, i.level, i.model FROM group_members m JOIN group_info i "
                               "USING(chat_id) WHERE m.user_id=? ORDER BY m.last DESC LIMIT 30", (user_id,)).fetchall()
        groups = []
        for chat_id, title, level, model in rows:
            count = lambda sql: self.db.execute(sql, (chat_id,)).fetchone()[0]
            groups.append({
                "id": chat_id, "title": title or "Группа", "level": level if level in LEVELS else "normal", "model": model,
                "messages": count("SELECT COUNT(*) FROM group_messages WHERE chat_id=?"),
                "stickers": count("SELECT COUNT(*) FROM group_media WHERE chat_id=? AND kind IN ('sticker', 'gif')"),
                "photos": count("SELECT COUNT(*) FROM group_media WHERE chat_id=? AND kind='photo'"),
                "memes": count("SELECT COUNT(*) FROM group_sent WHERE chat_id=?"),
                "phrases": [r[0] for r in self.db.execute("SELECT phrase FROM group_phrases WHERE chat_id=? AND count >= 2 "
                                                          "ORDER BY count DESC LIMIT 6", (chat_id,))]})
        return groups

    def close(self):
        self.db.close()


# ---------- pictures ----------

def _font(name, size):
    return ImageFont.truetype(str(ASSETS / name), size)


def _wrap(draw, text, font, width):
    """Wrap text to fit width pixels."""
    lines = []
    for paragraph in text.split("\n"):
        words, line = paragraph.split(), ""
        for word in words:
            test = f"{line} {word}".strip()
            if draw.textlength(test, font=font) <= width:
                line = test
                continue
            if line:
                lines.append(line)
            while draw.textlength(word, font=font) > width and len(word) > 1:  # a very long word: hard split
                cut = max(1, int(len(word) * width / draw.textlength(word, font=font)))
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return [line for line in lines if line is not None]


def _fit(draw, text, font_name, width, max_height, sizes=(64, 56, 48, 42, 36, 32, 28, 24)):
    for size in sizes:
        font = _font(font_name, size)
        lines = _wrap(draw, text, font, width)
        if len(lines) * int(size * 1.3) <= max_height:
            return font, lines
    font = _font(font_name, sizes[-1])
    lines = _wrap(draw, text, font, width)
    keep = max(1, max_height // int(sizes[-1] * 1.3))
    return font, lines[:keep - 1] + [lines[keep - 1][:60] + "…"] if len(lines) > keep else lines


def _jpeg(image):
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


PALETTES = [((75, 85, 255), (217, 70, 239)), ((20, 184, 166), (59, 130, 246)), ((249, 115, 22), (236, 72, 153)),
            ((139, 92, 246), (14, 165, 233)), ((34, 197, 94), (16, 185, 129)), ((244, 63, 94), (168, 85, 247))]


def _gradient(size, top, bottom):
    width, height = size
    base = Image.new("RGB", (1, 2))
    base.putpixel((0, 0), top)
    base.putpixel((0, 1), bottom)
    return base.resize((width, height), Image.BICUBIC)


def _avatar(avatar, name, size, color):
    if avatar:
        try:
            image = ImageOps.fit(Image.open(io.BytesIO(avatar)).convert("RGB"), (size, size))
        except OSError:
            image = None
    else:
        image = None
    if image is None:
        image = Image.new("RGB", (size, size), color)
        draw = ImageDraw.Draw(image)
        letter = (name.strip() or "?")[0].upper()
        font = _font("DejaVuSans-Bold.ttf", size // 2)
        draw.text((size / 2, size / 2), letter, font=font, fill="white", anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    return image, mask


def quote_card(name, text, avatar=None):
    """The mascot with a speech bubble: a member's message, signed with their name and avatar. Returns JPEG bytes."""
    width, height = 1080, 1080
    top, bottom = random.choice(PALETTES)
    card = _gradient((width, height), tuple(c // 3 for c in top), (8, 8, 16)).convert("RGBA")
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((-200, -260, 700, 520), fill=(*top, 110))
    ImageDraw.Draw(glow).ellipse((560, 640, 1340, 1380), fill=(*bottom, 90))
    card.alpha_composite(glow.filter(ImageFilter.GaussianBlur(120)))
    draw = ImageDraw.Draw(card)

    # Speech bubble with the message.
    box = (70, 90, width - 70, 700)
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((box[0], box[1] + 16, box[2], box[3] + 16), 56, fill=(0, 0, 0, 120))
    card.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(24)))
    draw.rounded_rectangle(box, 56, fill=(250, 250, 255))
    draw.polygon([(300, box[3] - 2), (390, box[3] - 2), (260, box[3] + 90)], fill=(250, 250, 255))
    quote_font = _font("DejaVuSerif-Bold.ttf", 120)
    draw.text((box[0] + 34, box[1] - 6), "“", font=quote_font, fill=(*top, 255))
    font, lines = _fit(draw, text, "DejaVuSans-Bold.ttf", box[2] - box[0] - 120, box[3] - box[1] - 150)
    line_height = int(font.size * 1.3)
    y = box[1] + 100 + (box[3] - box[1] - 150 - len(lines) * line_height) // 2
    for line in lines:
        draw.text((box[0] + 60, y), line, font=font, fill=(18, 18, 30))
        y += line_height

    # The mascot "says" it.
    mascot = Image.open(ASSETS / "mascot.png").convert("RGBA").resize((330, 330), Image.LANCZOS)
    card.alpha_composite(mascot.rotate(random.uniform(-10, 10), resample=Image.BICUBIC), (90, 715))

    # Signature: avatar and name.
    face, mask = _avatar(avatar, name, 120, top)
    ring = Image.new("RGBA", (132, 132), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((0, 0, 131, 131), fill=(*bottom, 255))
    card.alpha_composite(ring, (474, 830))
    card.paste(face, (480, 836), mask)
    draw.text((630, 868), name[:22], font=_font("DejaVuSans-Bold.ttf", 46), fill="white")
    draw.text((632, 928), "© " + time.strftime("%Y"), font=_font("DejaVuSans.ttf", 30), fill=(210, 210, 230))
    return _jpeg(card)


def demotivator(photo, title, subtitle=""):
    """Classic demotivator: the photo in a white frame on black, a big caption and a small one. Returns JPEG bytes."""
    picture = Image.open(io.BytesIO(photo)).convert("RGB")
    picture.thumbnail((900, 700))
    pw, ph = picture.size
    width = max(pw + 160, 900)
    title_font, title_lines = None, []
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    title_font, title_lines = _fit(probe, title.upper() if len(title) < 40 else title, "DejaVuSerif.ttf", width - 120, 160,
                                   sizes=(64, 56, 48, 42, 36))
    sub_font = _font("DejaVuSans.ttf", 30)
    sub_lines = _wrap(probe, subtitle, sub_font, width - 140)[:3] if subtitle else []
    height = 60 + ph + 40 + len(title_lines) * int(title_font.size * 1.25) + len(sub_lines) * 42 + 70
    card = Image.new("RGB", (width, height), "black")
    draw = ImageDraw.Draw(card)
    x, y = (width - pw) // 2, 60
    draw.rectangle((x - 10, y - 10, x + pw + 9, y + ph + 9), outline="white", width=3)
    card.paste(picture, (x, y))
    y += ph + 50
    for line in title_lines:
        draw.text((width / 2, y), line, font=title_font, fill="white", anchor="ma")
        y += int(title_font.size * 1.25)
    for line in sub_lines:
        draw.text((width / 2, y + 6), line, font=sub_font, fill=(220, 220, 220), anchor="ma")
        y += 42
    return _jpeg(card)


# ---------- TikTok memes ----------

TIKWM = "https://www.tikwm.com"  # unofficial TikTok search with watermark-free video links; may change or rate-limit
MEME_QUERIES = ["русские мемы", "мемы на русском", "приколы на русском", "русские приколы", "смешные видео на русском",
                "мемы россия", "ржака до слёз", "угарные видео", "мемы про школу", "мемы про работу", "мемы про друзей",
                "жиза мем", "приколы с котами", "приколы с собаками", "мемы тикток россия", "смешные моменты стрим"]
RUSSIAN_REGIONS = {"RU", "BY", "KZ", "UA", "KG", "UZ", "AM", "AZ", "GE", "MD", "TJ", "TM"}
MAX_VIDEO_BYTES = 45_000_000


class MemeError(Exception):
    pass


def _cyrillic(text):
    return sum(1 for c in text if "а" <= c.lower() <= "я" or c in "ёЁ")


def _russian(video):
    """Russian-speaking video: a Russian title or an author from a Russian-speaking country."""
    title = str(video.get("title") or "")
    region = str(video.get("region") or (video.get("author") or {}).get("region") or "").upper()
    return _cyrillic(title) >= 3 or region in RUSSIAN_REGIONS


async def _search(http, query):
    response = await http.post(f"{TIKWM}/api/feed/search", timeout=20,
                               data={"keywords": query, "count": 30, "cursor": random.choice([0, 0, 10, 20]), "HD": 0,
                                     "region": "RU"},
                               headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9"})
    return ((response.json() or {}).get("data") or {}).get("videos") or []


async def tiktok_meme(http, query=None, skip=()):
    """A funny Russian-language TikTok video: (video id, mp4 bytes, title). Raises MemeError."""
    queries = [query] if query else []
    queries += random.sample(MEME_QUERIES, 2)
    videos = []
    for words in queries:
        if " " not in words and not _cyrillic(words):
            words += " на русском"
        try:
            found = await _search(http, words)
        except Exception as exc:  # network, JSON or a changed format
            raise MemeError(type(exc).__name__) from None
        videos = [v for v in found if v.get("play") and str(v.get("video_id")) not in skip and _russian(v)
                  and 0 < (v.get("duration") or 0) <= 90 and (v.get("size") or 0) <= MAX_VIDEO_BYTES]
        if videos:
            break
    random.shuffle(videos)
    for video in videos[:3]:
        url = video["play"] if video["play"].startswith("http") else TIKWM + video["play"]
        try:
            data = await http.get(url, timeout=60, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        except Exception:  # try the next one
            continue
        if data.status_code == 200 and 10_000 < len(data.content) <= MAX_VIDEO_BYTES:
            return str(video["video_id"]), data.content, str(video.get("title") or "")[:200]
    raise MemeError("no video")
