"""TikTok links: the bot "watches" a shared video — its description, sound, a few frames and what is said or sung
in it — so the model can react to it or name the song.

The video comes from tikwm.com (unofficial, no key; the same service the meme feature uses). The song comes from the
TikTok sound info; when that is just "original sound", AudD recognizes the audio if AUDD_API_TOKEN is set, and the
heard lyrics are left for the model to recognize otherwise.
"""
import asyncio
import base64
import logging
import os
import re
import tempfile
from pathlib import Path

import imageio_ffmpeg

LOG = logging.getLogger(__name__)

TIKWM = "https://www.tikwm.com"
TIKTOK_URL = re.compile(r"https?://(?:[a-z]+\.)?tiktok\.com/[^\s<>\"']+", re.I)
MAX_BYTES = 45_000_000
FRAMES = 6
WHISPER_LANGS = {"ru", "en", "uk", "kk", "hy", "ka", "az", "tr", "de", "fr", "es", "it"}
ORIGINAL = re.compile(r"(original sound|оригинальный звук|son original|sonido original|suono originale)", re.I)


class LinkError(Exception):
    pass


def find(text):
    found = TIKTOK_URL.search(text or "")
    return found.group(0).rstrip(".,!?)»") if found else None


async def _info(http, url):
    response = await http.post(f"{TIKWM}/api/", data={"url": url, "hd": 0}, timeout=25,
                               headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9"})
    body = response.json() or {}
    if body.get("code") != 0 or not body.get("data"):
        raise LinkError(str(body.get("msg") or "no data")[:80])
    return body["data"]


def _absolute(url):
    return url if not url or url.startswith("http") else TIKWM + url


async def _run(*args, timeout=90):
    process = await asyncio.create_subprocess_exec(imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-y", "-v", "error",
                                                   *args, stdout=asyncio.subprocess.DEVNULL,
                                                   stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return process.returncode == 0


async def _frames_and_audio(video, duration, folder):
    """Evenly spaced frames (JPEG bytes) and the soundtrack as 16 kHz mono WAV (path or None)."""
    source = folder / "video.mp4"
    source.write_bytes(video)
    rate = FRAMES / max(1.0, float(duration or 15))
    await _run("-i", str(source), "-vf", f"fps={rate:.4f},scale=512:-2", "-frames:v", str(FRAMES), "-q:v", "4",
               str(folder / "frame%02d.jpg"))
    frames = [p.read_bytes() for p in sorted(folder.glob("frame*.jpg"))]
    audio = folder / "audio.wav"
    ok = await _run("-i", str(source), "-vn", "-t", "180", "-ac", "1", "-ar", "16000", str(audio))
    return frames, (audio if ok and audio.exists() and audio.stat().st_size > 4000 else None)


async def _transcribe(client, audio, language):
    if not client:
        return ""
    hint = {"language": language} if language in WHISPER_LANGS else {}
    try:
        with audio.open("rb") as file:
            result = await client.audio.transcriptions.create(model="whisper-1", file=file, **hint)
        return (result.text or "").strip()
    except Exception as exc:  # the frames and the description still help
        LOG.warning("TikTok transcription failed: %s", type(exc).__name__)
        return ""


async def _recognize(http, audio):
    """AudD song recognition: "Artist — Title" or ""."""
    token = os.environ.get("AUDD_API_TOKEN", "").strip()
    if not token:
        return ""
    clip = audio.with_name("clip.mp3")
    if not await _run("-i", str(audio), "-t", "25", "-b:a", "96k", str(clip)):
        return ""
    try:
        response = await http.post("https://api.audd.io/", data={"api_token": token}, timeout=40,
                                   files={"file": ("clip.mp3", clip.read_bytes(), "audio/mpeg")})
        song = (response.json() or {}).get("result") or {}
    except Exception as exc:
        LOG.warning("Song recognition failed: %s", type(exc).__name__)
        return ""
    return f"{song.get('artist', '')} — {song.get('title', '')}".strip(" —") if song else ""


async def watch(http, client, url, language="ru"):
    """What the bot needs to know about a TikTok post: (text for the model, [JPEG frames], short label)."""
    data = await _info(http, url)
    author = data.get("author") or {}
    music = data.get("music_info") or {}
    duration = data.get("duration") or 0
    frames, transcript, recognized = [], "", ""
    images = [i for i in (data.get("images") or []) if isinstance(i, str)]
    if images:  # a photo slideshow
        for image in images[:4]:
            try:
                response = await http.get(_absolute(image), timeout=30, follow_redirects=True)
                if response.status_code == 200:
                    frames.append(response.content)
            except Exception:
                pass
    else:
        play = _absolute(data.get("play") or "")
        if not play or (data.get("size") or 0) > MAX_BYTES:
            raise LinkError("no video")
        response = await http.get(play, timeout=90, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code != 200 or len(response.content) < 10_000:
            raise LinkError(f"download {response.status_code}")
        with tempfile.TemporaryDirectory(prefix="secret-ai-tt-") as folder:
            frames, audio = await _frames_and_audio(response.content, duration, Path(folder))
            if audio:
                transcript, recognized = await asyncio.gather(_transcribe(client, audio, language), _recognize(http, audio))
    sound = " — ".join(x for x in (str(music.get("title") or "").strip(), str(music.get("author") or "").strip()) if x)
    original = bool(music.get("original")) or bool(ORIGINAL.search(sound))
    lines = [f"[The user shared a TikTok {'photo post' if images else 'video'}: {url}",
             f"Author: {author.get('nickname') or ''} (@{author.get('unique_id') or ''})"]
    if duration:
        lines.append(f"Length: {duration} s")
    if data.get("title"):
        lines.append(f"Description: {str(data['title'])[:600]}")
    if data.get("play_count"):
        lines.append(f"Views: {data.get('play_count')}, likes: {data.get('digg_count') or 0}")
    if sound:
        lines.append(f"Sound used: {sound}" + (" (marked as the creator's original sound — not a song name)" if original else ""))
    if recognized:
        lines.append(f"Song recognized from the audio: {recognized}")
    if transcript:
        lines.append(f"Speech or lyrics heard in it (automatic transcript, may have mistakes): {transcript[:2500]}")
    lines.append(f"Below: {len(frames)} frames from it in order.]" if frames else "No frames could be taken.]")
    lines.append("You have watched it. If asked what the song is, name it from the recognized song or the sound info; "
                 "if it is only an original sound, try the lyrics, and say honestly if you can't tell. If the user just "
                 "sent the link, react to it like a friend who watched it — short; joke along if it is funny. "
                 "Don't mention frames or transcripts.")
    label = f"[TikTok: {str(data.get('title') or sound or url)[:120]}]"
    return "\n".join(lines), frames, label


def content(text, question, frames):
    """Model input in the bot's format: the user's words, what was watched, the frames."""
    parts = [{"type": "input_text", "text": f"{question}\n\n{text}"}]
    for frame in frames:
        parts.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")})
    return parts
