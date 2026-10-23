"""Pictures and short animations for the group quiz.

Animations are MP4s (what Telegram calls a GIF): words of the question fly in one by one, answer tiles pop, a countdown
runs, the right answer lights up, the podium grows. Frames are drawn with Pillow and encoded with variable frame
rate — the smooth intro at 25 fps, then one frame per half second for the countdown and a single long frame to hold —
so a clip is ~100 frames and renders in a second or two even on a small server.
"""
import functools
import itertools
import io
import re
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFilter

from fun import ASSETS

FPS = 25
LETTERS = ["A", "B", "C", "D"]
TILE_COLORS = [(139, 184, 255), (104, 150, 212), (96, 178, 182), (155, 170, 204)]
GREEN, DIM = (93, 196, 160), (45, 53, 66)
MEDALS = [(255, 204, 77), (205, 214, 230), (230, 150, 90)]
NO_EMOJI = re.compile("[\U00010000-\U0010FFFF☀-➿️‍]")


@functools.lru_cache(maxsize=64)
def font(size, bold=True):
    from PIL import ImageFont
    return ImageFont.truetype(str(ASSETS / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


def clean(text):
    """DejaVu has no emoji: they would be boxes on the card (the caption still has them)."""
    return re.sub(r"\s{2,}", " ", NO_EMOJI.sub("", str(text or ""))).strip()


def clamp(value):
    return max(0.0, min(1.0, value))


def ease_out(t):
    return 1 - (1 - clamp(t)) ** 3


def ease_back(t):
    t = clamp(t) - 1
    return 1 + 2.70158 * t ** 3 + 1.70158 * t ** 2


def mix(a, b, k):
    return tuple(int(x + (y - x) * k) for x, y in zip(a, b))


@functools.lru_cache(maxsize=8)
def _backdrop(width, height):
    top = Image.new("RGB", (1, 2))
    top.putpixel((0, 0), (27, 33, 43))
    top.putpixel((0, 1), (16, 19, 24))
    card = top.resize((width, height), Image.BICUBIC).convert("RGBA")
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    draw.ellipse((-width * 0.2, -height * 0.45, width * 0.75, height * 0.55), fill=(139, 184, 255, 22))
    draw.ellipse((width * 0.45, height * 0.35, width * 1.25, height * 1.45), fill=(89, 124, 170, 14))
    card.alpha_composite(glow.filter(ImageFilter.GaussianBlur(max(width, height) // 9)))
    return card


def backdrop(width, height):
    return _backdrop(width, height).copy()


def faded(image, alpha):
    """The image with its opacity multiplied by alpha (0..1)."""
    if alpha >= 0.999:
        return image
    copy = image.copy()
    copy.putalpha(copy.getchannel("A").point([int(v * alpha) for v in range(256)]))
    return copy


def put(card, image, x, y, alpha=1.0, scale=1.0):
    """Paste an RGBA image centred-scaled at its top-left (x, y); parts outside the card are cut off."""
    if alpha <= 0.01 or scale <= 0.01:
        return
    if abs(scale - 1) > 0.01:
        w, h = image.size
        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
        x, y = x + (w - image.width) / 2, y + (h - image.height) / 2
    image = faded(image, alpha)
    x, y = int(x), int(y)
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(image.width, card.width - x), min(image.height, card.height - y)
    if right > left and bottom > top:
        card.alpha_composite(image.crop((left, top, right, bottom)), (x + left, y + top))


def text_image(text, size, color=(255, 255, 255), bold=True, shadow=True):
    face = font(size, bold)
    box = face.getbbox(text)
    image = Image.new("RGBA", (box[2] + 8, box[3] + 10), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if shadow:
        draw.text((2, 4), text, font=face, fill=(0, 0, 0, 110))
    draw.text((1, 1), text, font=face, fill=(*color, 255))
    return image


def label(card, text, size, color, x, y, anchor="mm", alpha=1.0, bold=True, shadow=False):
    """Text placed by its middle-left / middle / middle-right point, with real fading (ImageDraw can't blend alpha)."""
    image = text_image(text, size, color, bold, shadow)
    width = font(size, bold).getlength(text)
    left = x - (width / 2 if anchor == "mm" else width if anchor == "rm" else 0)
    put(card, image, left - 1, y - size * 0.62, alpha=alpha)


def wrap(text, face, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if face.getlength(trial) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def panel(card, box, radius, color, alpha):
    layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius, fill=(*color, int(255 * alpha)))
    card.alpha_composite(layer)


COVER_SECONDS = 0.5


def encode(frames, cover=None):
    """(image, seconds) pairs, possibly a generator -> MP4 bytes. Frames go to disk as they come (a server has little
    memory); the same image object twice in a row is one frame shown longer.

    cover: the finished card as the very first frame. Telegram makes the preview from the first frame (it ignores a
    thumbnail we send), and whoever has autoplay off — or Low Power Mode on an iPhone — sees only that preview."""
    with tempfile.TemporaryDirectory(prefix="quiz-") as folder:
        folder = Path(folder)
        lines, count, previous = [], 0, None
        if cover is not None:
            frames = itertools.chain([(cover, COVER_SECONDS)], frames)
        for image, seconds in frames:
            if image is previous:
                lines[-1] = f"duration {float(lines[-1].split()[1]) + seconds:.3f}"
                continue
            name = f"f{count:04d}.jpg"
            image.convert("RGB").save(folder / name, "JPEG", quality=90)
            lines += [f"file '{name}'", f"duration {seconds:.3f}"]
            count, previous = count + 1, image
        # MP4 keeps no length for the last frame: close the hold with a short copy, plus the line the demuxer wants last.
        last = f"file 'f{count - 1:04d}.jpg'"
        lines += [last, "duration 0.040", last]
        (folder / "list.txt").write_text("\n".join(lines) + "\n")
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", str(folder / "list.txt"), "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "24", "-bf", "0", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(folder / "out.mp4")],
                       check=True, timeout=120, capture_output=True)
        return (folder / "out.mp4").read_bytes()


def thumb(image):
    """The preview Telegram shows before (or instead of) playing: the finished card, at most 320 px."""
    small = image.convert("RGB")
    small.thumbnail((320, 320), Image.LANCZOS)
    out = io.BytesIO()
    small.save(out, "JPEG", quality=85)
    return out.getvalue()


def jpeg(image):
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


# ---------- a question ----------

W, H = 800, 500
TILE_W, TILE_H, GAP, MARGIN = 352, 86, 16, 40
TILE_Y = (276, 378)


class QuestionArt:
    """Everything about one question that doesn't change from frame to frame."""

    def __init__(self, number, total, topic, question, options, seconds):
        self.number, self.total, self.topic, self.seconds = number, total, clean(topic), seconds
        self.options = [clean(o) for o in options]
        question = clean(question)
        for size in (40, 36, 32, 28, 25):
            lines = wrap(question, font(size), W - 2 * MARGIN)
            if len(lines) <= 3:
                break
        self.words = []  # (image, x, y) for each word, centred line by line
        line_height = int(size * 1.28)
        top = 172 - line_height * len(lines) / 2
        space = font(size).getlength(" ")
        for row, line in enumerate(lines):
            x = (W - font(size).getlength(line)) / 2
            for word in line.split():
                image = text_image(word, size)
                self.words.append((image, x, top + row * line_height))
                x += font(size).getlength(word) + space
        self.chip = self._chip()
        self.tiles = [self._tile(i, TILE_COLORS[i]) for i in range(4)]

    def _chip(self):
        label = f"ВОПРОС {self.number}/{self.total}"
        width = int(font(19).getlength(label)) + 34
        topic = font(19, False)
        image = Image.new("RGBA", (width + 20 + int(topic.getlength(self.topic)) + 10, 40), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, width, 39), 20, fill=(255, 255, 255, 235))
        draw.text((width / 2, 20), label, font=font(19), fill=(27, 33, 43), anchor="mm")
        draw.text((width + 16, 20), self.topic, font=topic, fill=(205, 205, 255), anchor="lm")
        return image

    def _tile(self, index, color, mark=None):
        image = Image.new("RGBA", (TILE_W, TILE_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, TILE_W - 1, TILE_H - 1), 22, fill=(*color, 245))
        shine = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(shine).rounded_rectangle((3, 3, TILE_W - 4, TILE_H // 2), 19, fill=(255, 255, 255, 26))
        image.alpha_composite(shine)
        draw.ellipse((14, TILE_H / 2 - 22, 58, TILE_H / 2 + 22), fill=(255, 255, 255, 240))
        if mark == "right":
            draw.line((25, TILE_H / 2, 33, TILE_H / 2 + 9, 48, TILE_H / 2 - 9), fill=GREEN, width=6, joint="curve")
        else:
            draw.text((36, TILE_H / 2 + 1), LETTERS[index], font=font(24), fill=color, anchor="mm")
        text = self.options[index]
        for size in (24, 22, 20, 18):
            lines = wrap(text, font(size), TILE_W - 90)
            if len(lines) <= 2 and all(font(size).getlength(l) <= TILE_W - 90 for l in lines):
                break
        lines = lines[:2]
        step = size * 1.2
        y = TILE_H / 2 - step * (len(lines) - 1) / 2
        for line in lines:
            draw.text((74, y), line, font=font(size), fill="white", anchor="lm")
            y += step
        return image

    def tile_spot(self, index):
        return MARGIN + (index % 2) * (TILE_W + GAP), TILE_Y[index // 2]

    def _scene(self, t, words=True, tiles=None):
        """The question at time t of the intro (tiles: images to use instead of the coloured ones)."""
        card = backdrop(W, H)
        put(card, self.chip, MARGIN - 40 * (1 - ease_out(t / 0.35)), 26, alpha=ease_out(t / 0.35))
        for i, (image, x, y) in enumerate(self.words):
            k = (t - 0.25 - i * 0.05) / 0.32
            if words and k > 0:
                put(card, image, x, y + 26 * (1 - ease_back(k)), alpha=ease_out(k))
        start = 0.35 + len(self.words) * 0.05
        for i in range(4):
            k = (t - start - i * 0.09) / 0.38
            x, y = self.tile_spot(i)
            put(card, (tiles or self.tiles)[i], x, y, alpha=ease_out(k * 1.6), scale=ease_back(k) if k < 1 else 1)
        return card

    def intro_length(self):
        return 0.35 + len(self.words) * 0.05 + 3 * 0.09 + 0.45

    def _timer(self, card, left):
        layer = Image.new("RGBA", card.size, (0, 0, 0, 0))  # drawn apart and blended: ImageDraw would overwrite
        draw = ImageDraw.Draw(layer)
        share = left / self.seconds
        color = mix((239, 68, 68), (45, 212, 191), clamp((share - 0.15) / 0.5))
        draw.rounded_rectangle((MARGIN, H - 22, W - MARGIN, H - 14), 4, fill=(255, 255, 255, 40))
        if share > 0:
            draw.rounded_rectangle((MARGIN, H - 22, MARGIN + (W - 2 * MARGIN) * share, H - 14), 4, fill=(*color, 255))
        cx, cy, r = W - MARGIN - 24, 46, 26
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 30))
        draw.arc((cx - r, cy - r, cx + r, cy + r), -90, -90 + 360 * share, fill=(*color, 255), width=6)
        draw.text((cx, cy + 1), str(int(round(left))), font=font(20), fill="white", anchor="mm")
        card.alpha_composite(layer)
        return card

    def question_clip(self):
        return encode(self._question_frames(), cover=self._timer(self._scene(99), self.seconds))

    def _question_frames(self):
        length = self.intro_length()
        for f in range(int(length * FPS) + 1):
            yield self._timer(self._scene(f / FPS), self.seconds), 1 / FPS
        still = self._scene(length + 1)
        for s in range(self.seconds * 2):
            yield self._timer(still.copy(), self.seconds - s / 2), 0.5
        yield self._timer(still.copy(), 0), 2.0

    def reveal_clip(self, correct):
        return encode(self._reveal_frames(correct), cover=self._revealed(correct))

    def _revealed(self, correct):
        *_, (last, _) = self._reveal_frames(correct)
        return last

    def question_thumb(self):
        return thumb(self._timer(self._scene(99), self.seconds))

    def reveal_thumb(self, correct):
        return thumb(self._revealed(correct))

    def _reveal_frames(self, correct):
        right = self._tile(correct, GREEN, mark="right")
        grey = [self._tile(i, DIM) for i in range(4)]
        card = None
        for f in range(int(0.9 * FPS) + 1):
            t = f / FPS
            card = self._scene(99)
            for i in range(4):
                x, y = self.tile_spot(i)
                if i == correct:
                    pop = 1 + 0.07 * (1 - abs(1 - 2 * clamp(t / 0.5)))  # swell and settle
                    put(card, self.tiles[i], x, y)
                    put(card, right, x, y, alpha=ease_out(t / 0.35), scale=pop)
                else:
                    put(card, grey[i], x, y, alpha=ease_out(t / 0.4))
            yield card, 1 / FPS
        yield card, 3.0


# ---------- the lobby ----------

def banner_frames():
    width, height = 800, 400
    title = [text_image(letter, 58) for letter in "ВИКТОРИНА"]
    sub = text_image("Secret AI · игра для группы", 27, (177, 194, 216), bold=False)
    mascot = Image.open(ASSETS / "mascot.png").convert("RGBA").resize((180, 180), Image.LANCZOS).rotate(-8, resample=Image.BICUBIC)
    chips = []
    for label, color in (("3 режима", (66, 100, 150)), ("разные темы", (58, 86, 122)), ("играйте вместе", (50, 100, 108))):
        w = int(font(22).getlength(label)) + 36
        chip = Image.new("RGBA", (w, 46), (0, 0, 0, 0))
        d = ImageDraw.Draw(chip)
        d.rounded_rectangle((0, 0, w - 1, 45), 23, fill=(*color, 255))
        d.text((w / 2, 23), label, font=font(22), fill="white", anchor="mm")
        chips.append(chip)

    def frame(t):
        card = backdrop(width, height)
        put(card, mascot, 590 + 120 * (1 - ease_back((t - 0.15) / 0.6)), 75, alpha=ease_out((t - 0.15) / 0.4))
        x = 58
        for i, letter in enumerate(title):
            k = (t - i * 0.06) / 0.45
            put(card, letter, x, 92 - 70 * (1 - ease_back(k)), alpha=ease_out(k * 2))
            x += font(58).getlength("ВИКТОРИНА"[i])
        put(card, sub, 60, 190 + 10 * (1 - ease_out((t - 0.6) / 0.4)), alpha=ease_out((t - 0.6) / 0.4))
        x = 60
        for i, chip in enumerate(chips):
            k = (t - 0.85 - i * 0.12) / 0.35
            put(card, chip, x, 262, alpha=ease_out(k * 2), scale=ease_back(k) if k < 1 else 1)
            x += chip.width + 14
        return card

    for f in range(int(1.6 * FPS) + 1):
        yield frame(f / FPS), 1 / FPS
    yield frame(9), 4.0


def banner_clip():
    *_, (last, _) = banner_frames()
    return encode(banner_frames(), cover=last)


def banner():
    *_, (last, _) = banner_frames()
    return jpeg(last)


def banner_thumb():
    *_, (last, _) = banner_frames()
    return thumb(last)


# ---------- the end ----------

def podium_frame(rows, questions, topic, t):
    width, height = 720, 900
    s = width / 1080
    card = backdrop(width, height)
    k = ease_out(t / 0.4)
    put(card, text_image("ИТОГИ", int(96 * s)), width / 2 - font(int(96 * s)).getlength("ИТОГИ") / 2, (60 - 30 * (1 - k)) * s, alpha=k)
    sub = clean(f"Викторина · {topic}")
    put(card, text_image(sub, int(38 * s), (177, 194, 216), bold=False, shadow=False),
        width / 2 - font(int(38 * s), False).getlength(sub) / 2, 170 * s, alpha=k)
    spots = [(540, 460, 350), (220, 540, 270), (860, 600, 210)]  # 1st, 2nd, 3rd in the 1080-wide layout
    order = {2: 0.25, 1: 0.5, 0: 0.8}  # third rises first, the winner last
    draw = ImageDraw.Draw(card)
    for place in range(min(3, len(rows))):
        name, points, right = rows[place]
        cx, top, column = spots[place]
        start = order[place]
        grow = ease_back((t - start) / 0.55) if t > start else 0
        base = top + 150 + column
        if grow > 0:
            panel(card, ((cx - 145) * s, (base - column * grow) * s, (cx + 145) * s, base * s), int(28 * s),
                  (255, 255, 255), 0.24 if place == 0 else 0.16)
        shift = column * (1 - min(1, grow))
        medal_k = ease_back((t - start - 0.35) / 0.4)
        if medal_k > 0:
            r = 70 * s * medal_k
            mx, my = cx * s, (top + 60 + shift) * s
            draw.ellipse((mx - r, my - r, mx + r, my + r), fill=(*MEDALS[place], 255))
            if medal_k > 0.6:
                draw.text((mx, my), str(place + 1), font=font(int(76 * s * min(1, medal_k))), fill=(30, 26, 60), anchor="mm")
        text_k = clamp((t - start - 0.45) / 0.4)
        if text_k > 0:
            alpha = ease_out(text_k)
            short = clean(name) or "?"
            while font(int(40 * s)).getlength(short) > 260 * s and len(short) > 2:
                short = short.rstrip("…")[:-1].rstrip() + "…"
            label(card, short, int(40 * s), (255, 255, 255), cx * s, (top + 195 + shift) * s, alpha=alpha)
            counted = int(points * ease_out((t - start - 0.45) / 0.9)) // 10 * 10 if t - start < 1.35 else points
            label(card, str(counted), int(52 * s), MEDALS[place], cx * s, (top + 250 + shift) * s, alpha=alpha)
            label(card, f"{right}/{questions} верно", int(30 * s), (210, 210, 235), cx * s, (top + 300 + shift) * s,
                  alpha=alpha, bold=False)
    y = 990
    for i, (name, points, right) in enumerate(rows[3:7]):
        k = ease_out((t - 1.5 - i * 0.1) / 0.4)
        if k > 0:
            dx = -60 * (1 - k)
            panel(card, ((90 + dx) * s, y * s, (990 + dx) * s, (y + 70) * s), int(22 * s), (255, 255, 255), 0.12 * k)
            label(card, str(i + 4), int(34 * s), (177, 194, 216), (130 + dx) * s, (y + 35) * s, "lm", k)
            label(card, clean(name)[:22] or "?", int(34 * s), (255, 255, 255), (200 + dx) * s, (y + 35) * s, "lm", k, bold=False)
            label(card, str(points), int(34 * s), (255, 255, 255), (950 + dx) * s, (y + 35) * s, "rm", k)
        y += 82
    draw.text((width / 2, height - 40), "Secret AI", font=font(int(30 * s)), fill=(170, 170, 220), anchor="mm")
    return card


def podium_clip(rows, questions, topic):
    def frames():
        for f in range(int(2.4 * FPS) + 1):
            yield podium_frame(rows, questions, topic, f / FPS), 1 / FPS
        yield podium_frame(rows, questions, topic, 9), 5.0
    return encode(frames(), cover=podium_frame(rows, questions, topic, 9))


def podium_thumb(rows, questions, topic):
    return thumb(podium_frame(rows, questions, topic, 10))


def podium(rows, questions, topic):
    return jpeg(podium_frame(rows, questions, topic, 10))


def leaderboard(rows, title):
    width, height = 1080, 300 + 96 * len(rows)
    card = backdrop(width, height)
    draw = ImageDraw.Draw(card)
    draw.text((width / 2, 100), "РЕЙТИНГ", font=font(84), fill="white", anchor="mm")
    sub = clean(title)
    while font(36, False).getlength(sub) > 900 and len(sub) > 2:
        sub = sub.rstrip("…")[:-1] + "…"
    draw.text((width / 2, 175), sub, font=font(36, False), fill=(177, 194, 216), anchor="mm")
    y = 240
    for place, (name, points, wins, games) in enumerate(rows, start=1):
        panel(card, (70, y, width - 70, y + 80), 24, (255, 255, 255), 0.13 if place > 3 else 0.22)
        color = MEDALS[place - 1] if place <= 3 else (177, 194, 216)
        draw.ellipse((95, y + 14, 147, y + 66), fill=(*color, 255))
        draw.text((121, y + 40), str(place), font=font(30), fill=(30, 26, 60), anchor="mm")
        name = clean(name) or "?"
        while font(36).getlength(name) > 520 and len(name) > 2:
            name = name.rstrip("…")[:-1] + "…"
        draw.text((175, y + 40), name, font=font(36), fill="white", anchor="lm")
        draw.text((width - 100, y + 28), f"{points}", font=font(34), fill="white", anchor="rm")
        draw.text((width - 100, y + 60), f"побед {wins} · игр {games}", font=font(22, False), fill=(200, 200, 235), anchor="rm")
        y += 96
    return jpeg(card)
