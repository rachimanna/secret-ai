"""TikTok links: the bot "watches" a shared video — its description, sound, a few frames and what is said or sung
in it — so the model can react to it or name the song.

The video comes from tikwm.com (unofficial, no key; the same service the meme feature uses). The song comes from the
TikTok sound info; when that is just "original sound", AudD recognizes the audio if AUDD_API_TOKEN is set, and the
heard lyrics are left for the model to recognize otherwise.
"""
import asyncio
import base64
import json
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


BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
PAGE_DATA = re.compile(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)


async def _resolve(http, url):
    """vm.tiktok.com / vt.tiktok.com short links -> the full video address."""
    if re.search(r"/(?:video|photo)/\d+", url):
        return url
    try:
        response = await http.get(url, timeout=20, follow_redirects=True, headers=BROWSER)
        return str(response.url)
    except Exception as exc:
        LOG.info("TikTok short link not resolved: %s", type(exc).__name__)
        return url


async def _tikwm(http, url):
    response = await http.get(f"{TIKWM}/api/", params={"url": url, "hd": 0}, timeout=25, headers=BROWSER)
    body = response.json() or {}
    if body.get("code") != 0 or not body.get("data"):
        raise LinkError(f"tikwm: {str(body.get('msg') or response.status_code)[:80]}")
    return body["data"], None


async def _page(http, url):
    """TikTok's own page: its embedded JSON has the description, sound and a play address (valid with its cookies)."""
    import httpx

    session = httpx.AsyncClient(headers=BROWSER, follow_redirects=True, timeout=30)
    try:
        response = await session.get(url)
        found = PAGE_DATA.search(response.text)
        if not found:
            raise LinkError(f"page: no data ({response.status_code})")
        scope = json.loads(found.group(1)).get("__DEFAULT_SCOPE__", {})
        item = ((scope.get("webapp.video-detail") or {}).get("itemInfo") or {}).get("itemStruct") or {}
        if not item:
            raise LinkError("page: no item")
        video, music, author = item.get("video") or {}, item.get("music") or {}, item.get("author") or {}
        stats = item.get("stats") or {}
        data = {"title": item.get("desc") or "", "duration": video.get("duration") or 0,
                "play": video.get("playAddr") or video.get("downloadAddr") or "",
                "music_info": {"title": music.get("title"), "author": music.get("authorName"), "original": music.get("original")},
                "author": {"nickname": author.get("nickname"), "unique_id": author.get("uniqueId")},
                "play_count": stats.get("playCount"), "digg_count": stats.get("diggCount"),
                "images": [((i.get("imageURL") or {}).get("urlList") or [None])[0]
                           for i in ((item.get("imagePost") or {}).get("images") or [])],
                "cover": video.get("cover") or ""}
        return data, session  # the session's cookies are needed to download "play"
    except BaseException:
        await session.aclose()
        raise


async def _oembed(http, url):
    """The official oEmbed: only the description, author and cover — no video, no sound."""
    response = await http.get("https://www.tiktok.com/oembed", params={"url": url}, timeout=20, headers=BROWSER)
    body = response.json() if response.status_code == 200 else {}
    if not body.get("title") and not body.get("thumbnail_url"):
        raise LinkError(f"oembed: {response.status_code}")
    return {"title": body.get("title") or "", "author": {"nickname": body.get("author_name"),
                                                          "unique_id": body.get("author_unique_id")},
            "cover": body.get("thumbnail_url") or "", "only_cover": True}, None


async def _info(http, url):
    """Video data from the first source that works: tikwm, TikTok's page, oEmbed. (data, session to download with)."""
    full = await _resolve(http, url)
    problems = []
    for source in (_tikwm, _page, _oembed):
        try:
            return await source(http, full)
        except Exception as exc:
            problems.append(f"{source.__name__}: {type(exc).__name__} {str(exc)[:80]}")
    raise LinkError("; ".join(problems))


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
    data, session = await _info(http, url)
    try:
        return await _watch(http, session or http, client, url, data, language)
    finally:
        if session:
            await session.aclose()


async def _watch(http, download, client, url, data, language):
    author = data.get("author") or {}
    music = data.get("music_info") or {}
    duration = data.get("duration") or 0
    frames, transcript, recognized = [], "", ""
    images = [i for i in (data.get("images") or []) if isinstance(i, str)]
    play = _absolute(data.get("play") or "")
    video = None
    if not images and play and (data.get("size") or 0) <= MAX_BYTES:
        try:
            response = await download.get(play, timeout=90, follow_redirects=True,
                                          headers={**BROWSER, "Referer": "https://www.tiktok.com/"})
            if response.status_code == 200 and 10_000 < len(response.content) <= MAX_BYTES:
                video = response.content
            else:
                LOG.info("TikTok video download: %s, %d bytes", response.status_code, len(response.content))
        except Exception as exc:
            LOG.info("TikTok video download failed: %s", type(exc).__name__)
    if not video:  # a photo slideshow, or only the cover could be had
        for image in (images or [data.get("cover")])[:4]:
            if not image:
                continue
            try:
                response = await download.get(_absolute(image), timeout=30, follow_redirects=True, headers=BROWSER)
                if response.status_code == 200 and response.content:
                    frames.append(response.content)
            except Exception:
                pass
    if video:
        with tempfile.TemporaryDirectory(prefix="secret-ai-tt-") as folder:
            frames, audio = await _frames_and_audio(video, duration, Path(folder))
            if audio:
                transcript, recognized = await asyncio.gather(_transcribe(client, audio, language), _recognize(http, audio))
    sound = " — ".join(x for x in (str(music.get("title") or "").strip(), str(music.get("author") or "").strip()) if x)
    original = bool(music.get("original")) or bool(ORIGINAL.search(sound))
    kind = "photo post" if images else "video" if video else "video (only its cover and description could be loaded — no sound)"
    lines = [f"[The user shared a TikTok {kind}: {url}",
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
    shown = "frames from it in order" if video else "pictures from it"
    lines.append(f"Below: {len(frames)} {shown}.]" if frames else "No pictures could be taken.]")
    lines.append(("You have watched it. " if video else "You only saw its cover and description; don't pretend you heard it. ") +
                 "If asked what the song is, name it from the recognized song or the sound info; "
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
