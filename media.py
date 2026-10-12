"""Photo and video generation through fal.ai (one key, many models, pay per generation).

Requests go through fal's queue API: submit, poll the status, fetch the result.
The bot owner pastes the fal key in the Mini App; it is stored encrypted.
"""
import asyncio
import tempfile
import time
from pathlib import Path

import httpx
import imageio_ffmpeg

FAL_QUEUE = "https://queue.fal.run"
DEFAULT_MODELS = {"photo": "fal-ai/flux/schnell", "video": "fal-ai/kling-video/v2.6/pro/text-to-video"}
TIMEOUTS = {"photo": 180, "video": 1800}
POLL_SECONDS = 3

# format id -> (fal image_size, video aspect_ratio)
FORMATS = {"square": ("square_hd", "1:1"), "portrait": ("portrait_16_9", "9:16"), "landscape": ("landscape_16_9", "16:9")}

FREE_PHOTOS_PER_DAY = 5
COST = {"photo": 1, "video": 15}  # in credits; video is the 5-second price, longer videos cost VIDEO_PRICES
# Video length (s) -> credits. Models make 5-10 s per clip; longer videos are 10-second scenes joined together.
VIDEO_PRICES = {5: 15, 10: 25, 30: 75, 60: 150, 120: 300}
SCENE_SECONDS = 10
FRAME = {"square": (720, 720), "portrait": (720, 1280), "landscape": (1280, 720)}


def price(kind, seconds=5):
    return COST["photo"] if kind == "photo" else VIDEO_PRICES.get(seconds, VIDEO_PRICES[5])


# Credit packs sold for Telegram Stars.
PACKS = {"s": (20, 25), "m": (100, 100), "l": (300, 250)}  # id: (credits, stars)
MAX_PROMPT = 1500


class MediaError(Exception):
    """A user-facing failure: code is one of no_key, bad_key, model, timeout, failed, blocked."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def build_input(kind, prompt, fmt, seconds=5):
    image_size, aspect = FORMATS.get(fmt, FORMATS["square"])
    if kind == "photo":
        return {"prompt": prompt, "image_size": image_size, "num_images": 1, "enable_safety_checker": True}
    return {"prompt": prompt, "aspect_ratio": aspect, "duration": "10" if seconds >= 10 else "5"}


def result_url(data):
    """URL of the generated file in a fal response (images[0], image or video)."""
    if not isinstance(data, dict):
        return None
    for key in ("images", "videos"):
        items = data.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict) and items[0].get("url"):
            return items[0]["url"]
    for key in ("image", "video"):
        item = data.get(key)
        if isinstance(item, dict) and item.get("url"):
            return item["url"]
    return None


async def fal_generate(http, key, model, kind, prompt, fmt, seconds=5):
    """Run one generation and return the URL of the result."""
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    try:
        response = await http.post(f"{FAL_QUEUE}/{model}", headers=headers, json=build_input(kind, prompt, fmt, seconds), timeout=30)
    except httpx.HTTPError as exc:
        raise MediaError("failed", type(exc).__name__) from None
    if response.status_code in (401, 403):
        raise MediaError("bad_key", str(response.status_code))
    if response.status_code == 404:
        raise MediaError("model", model)
    if response.status_code >= 400:
        raise MediaError("failed", f"submit {response.status_code} {_detail(response)}")
    job = response.json()
    status_url, response_url = job.get("status_url"), job.get("response_url")
    if not status_url or not response_url:
        raise MediaError("failed", "no request id")
    deadline = time.monotonic() + TIMEOUTS[kind]
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        try:
            status = (await http.get(status_url, headers=headers, timeout=20)).json().get("status")
        except (httpx.HTTPError, ValueError):
            continue  # a hiccup while polling is not a failed generation
        if status == "COMPLETED":
            break
    else:
        raise MediaError("timeout")
    result = await http.get(response_url, headers=headers, timeout=30)
    if result.status_code == 422:
        raise MediaError("blocked", _detail(result))  # usually the safety checker or an invalid prompt
    if result.status_code >= 400:
        raise MediaError("failed", f"result {result.status_code} {_detail(result)}")
    url = result_url(result.json())
    if not url:
        raise MediaError("failed", "no file in the result")
    return url


def _detail(response):
    try:
        data = response.json()
    except ValueError:
        return ""
    detail = data.get("detail") if isinstance(data, dict) else ""
    return str(detail)[:200]


# ---------- montage: join scenes and lay a voice-over on top ----------

async def _ffmpeg(*args, timeout=300):
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-y", "-v", "error", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise MediaError("failed", "ffmpeg: " + err.decode(errors="replace")[-200:])


async def _has_audio(path):
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", str(path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await process.communicate()
    return b"Audio:" in err


async def montage(clips, fmt, voice=None):
    """Join video clips (bytes) into one MP4 with the same frame; mix an optional voice-over (bytes) on top.

    The clips' own sound stays as quiet background under the voice. Returns the MP4 bytes.
    """
    width, height = FRAME.get(fmt, FRAME["square"])
    with tempfile.TemporaryDirectory(prefix="secret-ai-video-") as folder:
        folder = Path(folder)
        parts = []
        for index, data in enumerate(clips):
            source, target = folder / f"clip{index}.mp4", folder / f"part{index}.mp4"
            source.write_bytes(data)
            # Every part gets the same size, frame rate and an audio track, so they can be joined without re-encoding.
            audio = ["-map", "0:a:0"] if await _has_audio(source) else ["-map", "1:a:0"]
            await _ffmpeg("-i", str(source), "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                          "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                                 f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
                          "-map", "0:v:0", *audio, "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                          "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest", str(target))
            parts.append(target)
        listing = folder / "list.txt"
        listing.write_text("".join(f"file '{part.name}'\n" for part in parts))
        joined = folder / "joined.mp4"
        await _ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(joined))
        if not voice:
            return joined.read_bytes()
        narration, final = folder / "voice.mp3", folder / "final.mp4"
        narration.write_bytes(voice)
        await _ffmpeg("-i", str(joined), "-i", str(narration), "-filter_complex",
                      "[0:a]volume=0.22[bg];[1:a]apad[vo];[bg][vo]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
                      "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                      str(final))
        return final.read_bytes()
