"""Turn a logo image into an animated Telegram custom emoji (WEBM, 100x100, VP9).

Usage: python tools/make_emoji.py logo.png [emoji.webm] [--style pulse|spin|bounce]

Upload the result in @Stickers: /newemojipack -> Video emoji -> send the file.
"""
import argparse
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

SIZE = 100
DURATION = 3  # seconds; Telegram allows up to 3
FPS = 30
MAX_BYTES = 64 * 1024  # Telegram's limit for video emoji

# Each style is an ffmpeg filter chain applied to a transparent 100x100 frame; one loop lasts DURATION.
STYLES = {
    # Gentle "breathing" like an assistant that is thinking.
    "pulse": "scale=w='2*trunc((84+12*sin(2*PI*t/1.5))/2)':h=-2:eval=frame,"
             "pad=100:100:(ow-iw)/2:(oh-ih)/2:color=0x00000000",
    "spin": "scale=88:88,pad=100:100:6:6:color=0x00000000,rotate='2*PI*t/3':c=none:ow=100:oh=100",
    "bounce": "scale=80:80,pad=100:120:10:20:color=0x00000000,crop=100:100:0:'12+8*abs(sin(2*PI*t/1.5))'",
}


def build(source, target, style, crf):
    chain = f"format=rgba,scale={SIZE}:{SIZE}:force_original_aspect_ratio=decrease," \
            f"pad={SIZE}:{SIZE}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,{STYLES[style]},format=yuva420p"
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-y",
        "-loop", "1", "-i", str(source), "-t", str(DURATION), "-r", str(FPS), "-vf", chain,
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", str(crf),
        "-auto-alt-ref", "0", "-an", str(target),
    ], check=True)
    return target.stat().st_size


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logo", type=Path, help="PNG with a transparent background works best")
    parser.add_argument("output", type=Path, nargs="?", default=Path("emoji.webm"))
    parser.add_argument("--style", choices=STYLES, default="pulse")
    args = parser.parse_args()
    if not args.logo.is_file():
        sys.exit(f"File not found: {args.logo}")
    # Lower quality step by step until the file fits Telegram's size limit.
    for crf in (30, 36, 42, 48, 54, 63):
        size = build(args.logo, args.output, args.style, crf)
        if size <= MAX_BYTES:
            print(f"Done: {args.output} ({size // 1024} KB). Upload it via @Stickers -> /newemojipack.")
            return
    sys.exit("The logo is too detailed to fit 64 KB; try a simpler image.")


if __name__ == "__main__":
    main()
