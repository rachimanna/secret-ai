"""Group fun: the chat's memory (messages, stickers, gifs, photos, catchphrases) and picture memes.

Everything a group posts is remembered per chat in SQLite, so the bot knows the conversation and can throw the
chat's own stickers, gifs and favourite phrases back, make quote pictures (the mascot "says" a member's message)
and demotivators from the chat's photos.
"""
import io
import random
import sqlite3
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ASSETS = Path(__file__).with_name("assets")
GROUP_MESSAGES = 500  # kept per chat; the model sees the last GROUP_CONTEXT of them
GROUP_CONTEXT = 300
MEDIA_PER_KIND = 150
PHRASES_PER_CHAT = 400


class GroupStore:
    """Per-chat memory of a group. Used only from the bot's event loop."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS group_messages (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, "
                            "line TEXT NOT NULL, created INTEGER NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS group_messages_chat ON group_messages(chat_id, id)")
            if "message_id" not in [row[1] for row in self.db.execute("PRAGMA table_info(group_messages)")]:
                self.db.execute("ALTER TABLE group_messages ADD COLUMN message_id INTEGER")
            self.db.execute("CREATE TABLE IF NOT EXISTS group_media (chat_id INTEGER NOT NULL, kind TEXT NOT NULL, "
                            "unique_id TEXT NOT NULL, file_id TEXT NOT NULL, seen INTEGER NOT NULL, added INTEGER NOT NULL, "
                            "PRIMARY KEY (chat_id, unique_id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS group_sent (chat_id INTEGER NOT NULL, key TEXT NOT NULL, "
                            "created INTEGER NOT NULL, PRIMARY KEY (chat_id, key))")
            self.db.execute("CREATE TABLE IF NOT EXISTS group_phrases (chat_id INTEGER NOT NULL, key TEXT NOT NULL, "
                            "phrase TEXT NOT NULL, count INTEGER NOT NULL, last INTEGER NOT NULL, PRIMARY KEY (chat_id, key))")

    # ----- conversation -----

    def add_line(self, chat_id, line, message_id=None):
        with self.db:
            self.db.execute("INSERT INTO group_messages(chat_id, line, created, message_id) VALUES (?, ?, ?, ?)",
                            (chat_id, line[:1500], int(time.time()), message_id))
            self.db.execute("DELETE FROM group_messages WHERE chat_id=? AND id NOT IN "
                            "(SELECT id FROM group_messages WHERE chat_id=? ORDER BY id DESC LIMIT ?)",
                            (chat_id, chat_id, GROUP_MESSAGES))

    def lines(self, chat_id, limit=GROUP_CONTEXT):
        rows = self.db.execute("SELECT line FROM (SELECT id, line FROM group_messages WHERE chat_id=? ORDER BY id DESC "
                               "LIMIT ?) ORDER BY id", (chat_id, limit)).fetchall()
        return [row[0] for row in rows]

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
MEME_QUERIES = ["мемы", "мем", "приколы", "смешные видео", "мемы 2026", "ржака", "смешно до слёз", "мемы про школу",
                "мемы про работу", "мемы про друзей", "кот мем", "собака прикол", "жиза мем", "угар", "приколы с животными"]
MAX_VIDEO_BYTES = 45_000_000


class MemeError(Exception):
    pass


async def tiktok_meme(http, query=None, skip=()):
    """A funny TikTok video for the query: (video id, mp4 bytes, title). Raises MemeError."""
    query = query or random.choice(MEME_QUERIES)
    try:
        response = await http.post(f"{TIKWM}/api/feed/search", timeout=20,
                                   data={"keywords": query, "count": 20, "cursor": random.choice([0, 0, 10, 20]), "HD": 0},
                                   headers={"User-Agent": "Mozilla/5.0"})
        videos = ((response.json() or {}).get("data") or {}).get("videos") or []
    except Exception as exc:  # network, JSON or a changed format
        raise MemeError(type(exc).__name__) from None
    videos = [v for v in videos if v.get("play") and str(v.get("video_id")) not in skip and 0 < (v.get("duration") or 0) <= 90
              and (v.get("size") or 0) <= MAX_VIDEO_BYTES]
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


async def picture_meme(http):
    """A meme picture (meme-api.com, from Reddit) as a fallback: (url, title). Raises MemeError."""
    try:
        data = (await http.get("https://meme-api.com/gimme", timeout=15)).json()
    except Exception as exc:
        raise MemeError(type(exc).__name__) from None
    if data.get("nsfw") or not data.get("url"):
        raise MemeError("no meme")
    return data["url"], str(data.get("title") or "")[:200]
