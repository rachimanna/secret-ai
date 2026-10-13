"""Render assets/welcome-board.mp4: the mascot on little legs next to a board where the welcome is written by hand.

The bot only lays the voice over this video at runtime, so the tiny server never draws frames itself.
Usage: python tools/render_welcome.py CAVEAT_CYRILLIC.woff CAVEAT_LATIN.woff
(fonts: npm package @fontsource/caveat, files caveat-{cyrillic,latin}-700-normal.woff, SIL Open Font License)
"""
import math
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SIZE, FPS, SECONDS = 640, 30, 18
WRITE_FROM, WRITE_TO = 0.6, 8.0  # the text is finished before the voice ends (~9-11 s)
LINES = ["Привет! Я Secret AI", "Пиши что хочешь:", "вопросы · код · сайты", "картинки и видео", "и можно голосом!"]
INK = [(40, 44, 92), (40, 44, 92), (109, 93, 252), (109, 93, 252), (40, 44, 92)]


def ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def background():
    """Deep violet with a soft glow, drawn once."""
    bg = Image.new("RGB", (SIZE, SIZE), (10, 8, 26))
    glow = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
    d = ImageDraw.Draw(glow)
    d.ellipse((60, 40, 580, 560), fill=(70, 50, 170))
    d.ellipse((330, 330, 700, 700), fill=(120, 40, 140))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    return Image.blend(bg, glow, 0.55)


def board_image():
    """A whiteboard with a soft shadow, drawn once."""
    w, h = 360, 214
    layer = Image.new("RGBA", (w + 60, h + 60), (0, 0, 0, 0))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((30, 40, 30 + w, 40 + h), 22, fill=(0, 0, 0, 150))
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((30, 30, 30 + w, 30 + h), 22, fill=(246, 246, 252), outline=(205, 205, 228), width=4)
    d.rounded_rectangle((30 + w // 2 - 70, 30 + h - 6, 30 + w // 2 + 70, 30 + h + 6), 5, fill=(176, 176, 200))  # marker tray
    return layer


class Ink:
    """Draws mixed Cyrillic/Latin text with the two Caveat subsets."""

    def __init__(self, cyr, lat, size):
        self.cyr, self.lat = ImageFont.truetype(cyr, size), ImageFont.truetype(lat, size)
        self.emoji = ImageFont.truetype(str(ROOT / "assets" / "DejaVuSans.ttf"), int(size * 0.8))

    def font(self, ch):
        if "Ѐ" <= ch <= "ӿ":
            return self.cyr
        if ord(ch) > 0x2000 and ch not in "·":
            return self.emoji
        return self.lat

    def width(self, text):
        return sum(self.font(c).getlength(c) for c in text)

    def draw(self, draw, x, y, text, color):
        for ch in text:
            f = self.font(ch)
            draw.text((x, y), ch, font=f, fill=color)
            x += f.getlength(ch)
        return x


def mascot_frames():
    star = Image.open(ROOT / "assets" / "mascot.png").convert("RGBA")
    box = star.getbbox()
    return star.crop(box).resize((196, 196), Image.LANCZOS)


def legs(draw, cx, top, t, walking):
    """Two little legs with sneakers; they step in place, one after the other."""
    for side, phase in ((-1, 0.0), (1, math.pi)):
        lift = 9 * max(0.0, math.sin(2 * math.pi * 1.6 * t + phase)) if walking else 0
        x = cx + side * 22
        foot_y = top + 52 - lift
        draw.rounded_rectangle((x - 6, top, x + 6, foot_y), 6, fill=(196, 188, 255))
        draw.ellipse((x - 16 + side * 4, foot_y - 7, x + 16 + side * 4, foot_y + 9), fill=(250, 250, 255),
                     outline=(150, 140, 230), width=2)


def main(cyr, lat):
    bg, board, star = background(), board_image(), mascot_frames()
    ink = Ink(cyr, lat, 35)
    total = sum(len(line) for line in LINES)
    out = ROOT / "assets" / "welcome-board.mp4"
    ffmpeg = subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                               "-s", f"{SIZE}x{SIZE}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "slow",
                               "-crf", "22", "-pix_fmt", "yuv420p", "-g", str(FPS), "-movflags", "+faststart", str(out)],
                              stdin=subprocess.PIPE)
    for n in range(SECONDS * FPS):
        t = n / FPS
        frame = bg.copy().convert("RGBA")
        # board slides in at the start
        appear = ease(t / 0.6)
        bx, by = 150, int(102 - (1 - appear) * 40)
        frame.alpha_composite(board, (bx - 30, by - 30))
        d = ImageDraw.Draw(frame)
        # handwriting: characters revealed over the speech, with a marker dot at the pen
        shown = int(total * ease((t - WRITE_FROM) / (WRITE_TO - WRITE_FROM)) if t > WRITE_FROM else 0)
        y, pen = by + 18, None
        for line, color in zip(LINES, INK):
            part = line[:max(0, min(len(line), shown))]
            shown -= len(line)
            if part:
                x0 = bx + (360 - ink.width(line)) / 2
                end = ink.draw(d, x0, y, part, color)
                pen = (end, y + 26)
            y += 37
        writing = WRITE_FROM < t < WRITE_TO + 0.2
        # the star: walks in, then stands by the board bobbing; a little lean toward the text while writing
        walk_in = ease((t - 0.2) / 1.4)
        cx = int(-120 + walk_in * (190 + 120))
        bob = 6 * math.sin(2 * math.pi * 1.1 * t)
        talk = 1 + 0.03 * math.sin(2 * math.pi * 3.2 * t) if t < WRITE_TO + 0.5 else 1
        lean = 5 * math.sin(2 * math.pi * 0.35 * t) + (4 if writing else 0)
        body = star.resize((int(196 * talk), int(196 * talk)), Image.BICUBIC).rotate(-lean, resample=Image.BICUBIC, expand=True)
        top = int(432 + bob)
        legs(d, cx, top + 62, t, walking=t < 1.8 or abs(math.sin(2 * math.pi * 0.25 * t)) > 0.92)
        frame.alpha_composite(body, (cx - body.width // 2, top - body.height // 2))
        # a marker line from the star to the pen while writing
        if writing and pen:  # a marker writing the text
            wiggle = 3 * math.sin(2 * math.pi * 6 * t)
            px, py = pen[0] + 4, pen[1] - 6 + wiggle
            d.line((px, py, px + 26, py - 34), fill=(109, 93, 252), width=11)
            d.line((px + 20, py - 26, px + 30, py - 40), fill=(60, 52, 140), width=11)
            d.ellipse((px - 4, py - 4, px + 4, py + 4), fill=(40, 44, 92))
        ffmpeg.stdin.write(frame.convert("RGB").tobytes())
    ffmpeg.stdin.close()
    ffmpeg.wait()
    print("written", out, out.stat().st_size)


if __name__ == "__main__":
    main(*sys.argv[1:3])
