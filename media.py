"""Photo and video generation through fal.ai (one key, many models, pay per generation).

Requests go through fal's queue API: submit, poll the status, fetch the result.
The bot owner pastes the fal key in the Mini App; it is stored encrypted.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
import imageio_ffmpeg

LOG = logging.getLogger(__name__)

FAL_QUEUE = "https://queue.fal.run"
# Photos and videos: fal.ai, one key. Video scenes come from Kling without sound; MMAudio then adds real sound effects
# (steps, nature, city) and the bot lays a Russian voice-over on top. A Higgsfield id ("bytedance/...") or a Google Veo id
# ("veo-...") can be set instead; they need their own keys.
# "free": photos from Cloudflare Workers AI (if CF_ACCOUNT_ID/CF_API_TOKEN are set) or Pollinations (no key), and videos
# made of those pictures brought to life (slow zoom and pan) with a voice-over. Paid models stay available by id.
DEFAULT_MODELS = {"photo": "openai-image", "video": "free"}  # photos: GPT Image on the OpenAI key (free Flux as fallback)
OLD_VIDEO_DEFAULTS = ("veo-3.1-fast-generate-preview", "bytedance/seedance-2.5/text-to-video",
                      "fal-ai/kling-video/v2.6/pro/text-to-video")
OLD_PHOTO_DEFAULTS = ("fal-ai/flux/schnell",)
KLING = "fal-ai/kling-video/v2.6/pro/text-to-video"  # the video model once a fal key is set (VIDEO_MODEL env overrides)
SOUND_MODEL = "fal-ai/mmaudio-v2"  # video-to-audio: sound effects that match the picture, about $0.001 per second
FAL_SCENE_SECONDS = 10  # Kling makes 5- or 10-second clips
TIMEOUTS = {"photo": 180, "video": 1800, "sound": 300}
POLL_SECONDS = 3

# format id -> (fal image_size, video aspect_ratio)
FORMATS = {"square": ("square_hd", "1:1"), "portrait": ("portrait_16_9", "9:16"), "landscape": ("landscape_16_9", "16:9")}

FREE_PHOTOS_PER_DAY = 5
COST = {"photo": 1, "video": 60}  # in credits; video is the shortest video's price, see VIDEO_PRICES
# Video length (s) -> credits. Veo makes 8 s per clip (about $0.10/s on Veo 3.1 Fast); longer videos are 8-second
# scenes joined together. 1 credit is sold for about 1 Telegram Star.
VIDEO_PRICES = {8: 60, 16: 120, 32: 240, 64: 480}
SCENE_SECONDS = 8
FRAME = {"square": (720, 720), "portrait": (720, 1280), "landscape": (1280, 720)}


def price(kind, seconds=5):
    return COST["photo"] if kind == "photo" else VIDEO_PRICES.get(seconds, COST["video"])


# Credit packs sold for Telegram Stars.
PACKS = {"s": (60, 60), "m": (250, 230), "l": (1000, 880)}  # id: (credits, stars)
MAX_PROMPT = 1500


class MediaError(Exception):
    """A user-facing failure: code is one of no_key, bad_key, model, timeout, failed, blocked."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# Models with native audio invent speech in English or Chinese whatever the prompt says, so their sound is turned off;
# the voice-over is added by the bot. Other models get no such parameter.
NATIVE_AUDIO = ("kling-video/v2.6", "kling-video/v3", "kling-video/o3", "veo3", "seedance")


def build_input(kind, prompt, fmt, seconds=5, model=""):
    image_size, aspect = FORMATS.get(fmt, FORMATS["square"])
    if kind == "photo":
        return {"prompt": prompt, "image_size": image_size, "num_images": 1, "enable_safety_checker": True}
    data = {"prompt": prompt, "aspect_ratio": aspect, "duration": "10" if seconds >= 8 else "5"}
    if any(name in model for name in NATIVE_AUDIO):
        data["generate_audio"] = False
    return data


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
    return await fal_run(http, key, model, build_input(kind, prompt, fmt, seconds, model), TIMEOUTS[kind])


async def fal_sound(http, key, video_url, prompt, seconds):
    """Add sound effects to a silent clip with MMAudio; returns the URL of the clip with sound."""
    payload = {"video_url": video_url, "prompt": prompt, "duration": seconds, "num_steps": 25, "cfg_strength": 4.5}
    return await fal_run(http, key, SOUND_MODEL, payload, TIMEOUTS["sound"])


async def fal_run(http, key, model, payload, timeout):
    """Submit to fal's queue, wait and return the URL of the result."""
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    try:
        response = await http.post(f"{FAL_QUEUE}/{model}", headers=headers, json=payload, timeout=30)
        if response.status_code == 422 and "generate_audio" in payload:
            # This model doesn't know the parameter: ask again without it (its sound is dropped in the montage anyway).
            payload.pop("generate_audio")
            response = await http.post(f"{FAL_QUEUE}/{model}", headers=headers, json=payload, timeout=30)
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
    deadline = time.monotonic() + timeout
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


async def montage(clips, fmt, voice=None, keep_sound=False, sound_volume=0.22):
    """Join video clips (bytes) into one MP4 with the same frame and lay an optional voice-over (bytes) on top.

    The clips' own sound is dropped unless keep_sound: video models put foreign-language speech there. Returns MP4 bytes.
    """
    width, height = FRAME.get(fmt, FRAME["square"])
    with tempfile.TemporaryDirectory(prefix="secret-ai-video-") as folder:
        folder = Path(folder)
        parts = []
        for index, data in enumerate(clips):
            source, target = folder / f"clip{index}.mp4", folder / f"part{index}.mp4"
            source.write_bytes(data)
            # Every part gets the same size, frame rate and an audio track, so they can be joined without re-encoding.
            audio = ["-map", "0:a:0"] if keep_sound and await _has_audio(source) else ["-map", "1:a:0"]
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
                      f"[0:a]volume={sound_volume}[bg];[1:a]apad[vo];[bg][vo]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
                      "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                      str(final))
        return final.read_bytes()


# ---------- Google Veo (Gemini API): video with sound and speech ----------

GEMINI = "https://generativelanguage.googleapis.com/v1beta"


def is_veo(model):
    return model.startswith("veo")


def is_fal(model):
    return model.startswith("fal-ai/")


def video_provider(model):
    """'free', 'veo', 'fal', 'kling' (kling:<model_name>) or 'higgsfield' (anything else, e.g. bytedance/seedance-2.5/text-to-video)."""
    return ("free" if model == "free" else "veo" if is_veo(model) else "fal" if is_fal(model)
            else "kling" if model.startswith(KLING_PREFIX) else "anymodel" if model.startswith(ANYMODEL_PREFIX) else "higgsfield")


# ---------- Kling's own API: Access Key + Secret Key from the Kling developer console ----------

KLING_PREFIX = "kling:"
KLING_DIRECT = KLING_PREFIX + "kling-v2-6"  # the video model when KLING_ACCESS_KEY/KLING_SECRET_KEY are set
KLING_API = "https://api-singapore.klingai.com"  # KLING_API_BASE env overrides
KLING_SECONDS = 10  # Kling makes 5- or 10-second clips


def kling_token(key, now=None):
    """The Bearer token: a new-style API key ("bearer:KEY") as is, or a short-lived HS256 JWT from the legacy
    "ACCESS_KEY:SECRET_KEY" pair."""
    if key.startswith("bearer:"):
        return key[len("bearer:"):]
    access, _, secret = key.partition(":")
    now = int(now or time.time())

    def part(value):
        raw = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    signing = part({"alg": "HS256", "typ": "JWT"}) + "." + part({"iss": access, "exp": now + 1800, "nbf": now - 5})
    return signing + "." + part(hmac.new(secret.encode(), signing.encode(), hashlib.sha256).digest())


async def kling_generate(http, key, model, prompt, fmt, seconds=KLING_SECONDS, base=KLING_API):
    """One Kling clip with its own sound effects (no speech); returns the MP4 bytes."""
    name = model[len(KLING_PREFIX):] if model.startswith(KLING_PREFIX) else model
    body = {"model_name": name, "prompt": prompt[:2500], "mode": "pro", "duration": str(seconds),
            "aspect_ratio": {"square": "1:1", "portrait": "9:16"}.get(fmt, "16:9"), "sound": "on"}

    async def call(method, path, **kwargs):
        headers = {"Authorization": f"Bearer {kling_token(key)}", "Content-Type": "application/json"}
        try:
            return await http.request(method, base + path, headers=headers, timeout=60, **kwargs)
        except httpx.HTTPError as exc:
            raise MediaError("failed", type(exc).__name__) from None

    response = await call("POST", "/v1/videos/text2video", json=body)
    if response.status_code == 400 and "sound" in _kling_error(response)[1].lower():
        body.pop("sound")  # older models have no sound
        response = await call("POST", "/v1/videos/text2video", json=body)
    code, detail = _kling_error(response)
    if response.status_code == 401 or 1000 <= code < 1100:
        raise MediaError("bad_video_key", detail)
    if code in (1101, 1102) or "balance" in detail.lower() or "resource pack" in detail.lower():
        raise MediaError("video_billing", detail)
    if response.status_code == 429 or code in (1302, 1303):
        raise MediaError("busy", detail)
    if code == 1301:
        raise MediaError("blocked", detail)
    if response.status_code >= 400 or code:
        raise MediaError("failed", f"submit {response.status_code} {code} {detail}")
    task = (response.json().get("data") or {}).get("task_id")
    if not task:
        raise MediaError("failed", "no task id")
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS * 3)
        try:
            data = (await call("GET", f"/v1/videos/text2video/{task}")).json().get("data") or {}
        except (MediaError, ValueError):
            continue
        status = data.get("task_status")
        if status == "succeed":
            videos = (data.get("task_result") or {}).get("videos") or []
            if not videos or not videos[0].get("url"):
                raise MediaError("failed", "no video url")
            video = await http.get(videos[0]["url"], timeout=180, follow_redirects=True)
            if video.status_code >= 400:
                raise MediaError("failed", f"download {video.status_code}")
            return video.content
        if status == "failed":
            message = str(data.get("task_status_msg", ""))
            raise MediaError("blocked" if "risk" in message.lower() or "content" in message.lower() else "failed", message[:200])
    raise MediaError("timeout")


# ---------- AnyModel (OpenAI-compatible gateway): Grok Imagine, Kling, Veo video on one key ----------

ANYMODEL_PREFIX = "anymodel:"
ANYMODEL_API = "https://anymodel.org/v1"  # ANYMODEL_BASE env overrides
ANYMODEL_VIDEO = "xai/grok-imagine-video"  # ANYMODEL_VIDEO_MODEL env overrides
ANYMODEL_SECONDS = 10  # 1-15 s per clip; billed per second, so always sent explicitly
ANYMODEL_RESOLUTION = "720p"  # sent explicitly too: an omitted one is billed at the dearest rate
ANYMODEL_VOICE = "edge-tts/ru-RU-DmitryNeural"  # ANYMODEL_VOICE env: an exact voice model id, names to look up (a|b), or off
DONE = ("done", "completed", "succeeded", "success", "succeed", "finished")
FAILED = ("failed", "error", "expired", "cancelled", "canceled", "rejected")


def _anymodel_failure(response, what):
    detail = _gateway_error(response)
    status = response.status_code
    if status in (401, 403):  # 403: the account behind the key isn't active
        return MediaError("bad_video_key", detail)
    if status == 402:
        return MediaError("video_billing", detail)
    if status == 429:
        return MediaError("busy", detail)
    if status in (404, 406):
        return MediaError("model", detail)
    blocked = any(w in detail.lower() for w in ("moderation", "safety", "policy", "nsfw"))
    return MediaError("blocked" if blocked else "failed", f"{what} {status} {detail}")


async def anymodel_generate(http, key, model, prompt, fmt, seconds=ANYMODEL_SECONDS, base=ANYMODEL_API):
    """One clip with its own sound (AnyModel's xAI-shaped video API): create the job, poll it with the connection
    ticket, download it through the gateway. Returns the MP4 bytes."""
    name = model[len(ANYMODEL_PREFIX):] if model.startswith(ANYMODEL_PREFIX) else model
    headers = {"Authorization": f"Bearer {key}"}
    body = {"model": name, "prompt": prompt[:4000], "duration": max(1, min(15, int(seconds))),
            "resolution": ANYMODEL_RESOLUTION, "aspect_ratio": {"square": "1:1", "portrait": "9:16"}.get(fmt, "16:9")}

    async def call(method, path, **kwargs):
        try:
            return await http.request(method, base + path, headers=headers | kwargs.pop("extra", {}),
                                      timeout=kwargs.pop("timeout", 60), follow_redirects=True, **kwargs)
        except httpx.HTTPError as exc:
            raise MediaError("failed", type(exc).__name__) from None

    response = await call("POST", "/videos/generations", json=body, extra={"Idempotency-Key": str(uuid.uuid4())})
    if response.status_code >= 400:
        raise _anymodel_failure(response, "submit")
    job = _json(response)
    task = job.get("request_id") or job.get("id")
    if not task:
        raise MediaError("failed", "no request id")
    ticket = {"x-9router-connection-id": response.headers["x-9router-connection-id"]} \
        if response.headers.get("x-9router-connection-id") else {}
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS * 2)
        try:
            response = await call("GET", f"/videos/{task}", extra=ticket)
        except MediaError:
            continue
        if response.status_code >= 500 or response.status_code == 429:
            continue
        if response.status_code >= 400:
            raise _anymodel_failure(response, "poll")
        data = _json(response)
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        status = str(data.get("status") or inner.get("status") or "").lower()
        if status in DONE or (_video_url(data) and status not in FAILED):
            # Through the gateway: works where the provider's own link needs cookies.
            content = await call("GET", f"/videos/{task}/content", extra=ticket, timeout=180)
            if content.status_code < 400 and b"ftyp" in content.content[:16]:
                return content.content
            url = _video_url(data)
            if url:
                return await _download(http, url, None)
            raise MediaError("failed", f"download {content.status_code}")
        if status in FAILED:
            message = _gateway_error(response) or str(data.get("error") or status)
            blocked = any(w in message.lower() for w in ("moderation", "safety", "policy", "nsfw"))
            raise MediaError("blocked" if blocked else "failed", message[:200])
    raise MediaError("timeout")


async def anymodel_voices(http, key, base=ANYMODEL_API, every_language=False):
    """Voices the API can speak with: (model id, label) from the TTS catalog, then the edge-tts voices (Russian ones, or
    every language)."""
    voices = []
    for path in ("/models/tts", "/audio/voices?provider=edge-tts" + ("" if every_language else "&lang=ru")):
        try:
            response = await http.get(base + path, headers={"Authorization": f"Bearer {key}"}, timeout=30)
            entries = (response.json().get("data") or []) if response.status_code < 400 else []
        except (httpx.HTTPError, ValueError, AttributeError):
            continue
        for entry in entries:
            if isinstance(entry, dict) and (entry.get("model") or entry.get("id")):
                label = " · ".join(str(entry[k]) for k in ("name", "title", "gender") if entry.get(k))
                voices.append((str(entry.get("model") or entry.get("id")), label))
    return voices


async def anymodel_voice_id(http, key, wanted, base=ANYMODEL_API):
    """The speech model id for a voice: an id as is ("fish-farm/…", "edge-tts/ru-RU-…"), or the first voice whose id or
    name holds one of the names in "a|b|c". "" if none."""
    wanted = (wanted or "").strip()
    if not wanted or wanted.lower() in ("off", "none", "no"):
        return ""
    if "/" in wanted and "|" not in wanted:
        return wanted
    names = [n.strip().casefold() for n in wanted.split("|") if n.strip()]
    for model, label in await anymodel_voices(http, key, base):
        if any(name in f"{model} {label}".casefold() for name in names):
            return model
    return ""


ANYMODEL_FISH = "fish-farm/s2.1-pro-free"  # Fish Audio's engine on the AnyModel key


async def anymodel_speech(http, key, voice_model, text, base=ANYMODEL_API, reference_id=""):
    """Speech in an AnyModel voice (the voice is the model id) as an OGG Opus voice message. reference_id asks the
    Fish Audio route for a voice from fish.audio (not documented by AnyModel: tried, and judged by ear)."""
    body = {"model": voice_model, "input": text}
    if reference_id:
        body["reference_id"] = reference_id
    try:
        response = await http.post(base + "/audio/speech", json=body, timeout=120,
                                   headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        raise MediaError("failed", type(exc).__name__) from None
    if response.status_code >= 400 or response.headers.get("content-type", "").startswith("application/json"):
        raise _anymodel_failure(response, "speech")
    return await voice_message(response.content)


async def voice_message(audio):
    """Any audio (MP3…) as an OGG Opus file that Telegram shows as a voice message."""
    with tempfile.TemporaryDirectory(prefix="secret-ai-tts-") as folder:
        folder = Path(folder)
        (folder / "in.audio").write_bytes(audio)
        await _ffmpeg("-i", str(folder / "in.audio"), "-vn", "-c:a", "libopus", "-b:a", "48k", "-ac", "1",
                      str(folder / "voice.ogg"), timeout=120)
        return (folder / "voice.ogg").read_bytes()


# ---------- Fish Audio: thousands of character voices, picked by the voice's id from fish.audio ----------

FISH_API = "https://api.fish.audio/v1/tts"
FISH_MODELS = ("s2-pro", "s1")  # FISH_MODEL env picks one; the next is tried if the first is refused


def fish_voice_id(text):
    """The voice id from what the owner pasted: the id itself or a fish.audio link to the voice (…/m/<id>)."""
    found = re.search(r"([0-9a-f]{32})", text or "")
    return found[1] if found else (text or "").strip()


async def fish_speech(http, key, voice, text, model=""):
    """Speech in a Fish Audio voice as an OGG Opus voice message."""
    for name in ([model] if model else list(FISH_MODELS)):
        try:
            response = await http.post(FISH_API, timeout=120, json={"text": text, "reference_id": voice, "format": "mp3"},
                                       headers={"Authorization": f"Bearer {key}", "model": name})
        except httpx.HTTPError as exc:
            raise MediaError("failed", type(exc).__name__) from None
        if response.status_code < 400 and not response.headers.get("content-type", "").startswith("application/json"):
            return await voice_message(response.content)
        detail = response.text[:200]
        if response.status_code in (401, 403):
            raise MediaError("bad_video_key", detail)
        if response.status_code == 402:
            raise MediaError("video_billing", detail)
        if "model" not in detail.lower():  # only an unknown model is worth another try
            break
    raise MediaError("failed", f"fish {response.status_code} {detail}")


def _json(response):
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {}


def _video_url(data):
    """The finished video's URL in the common response shapes (video.url, url, output, data[0].url, result...)."""
    if isinstance(data, str):
        return data if data.startswith("http") else ""
    if isinstance(data, list):
        return next((u for u in map(_video_url, data) if u), "")
    if not isinstance(data, dict):
        return ""
    for key in ("video", "video_url", "url", "output", "result", "data", "videos", "generations"):
        if key in data:
            found = _video_url(data[key])
            if found:
                return found
    return ""


def _gateway_error(response):
    data = _json(response)
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    return str(error or data.get("message") or data.get("detail") or "")[:200]


async def _download(http, url, headers):
    video = await http.get(url, timeout=180, follow_redirects=True, headers=headers)
    if video.status_code >= 400:
        raise MediaError("failed", f"download {video.status_code}")
    return video.content


def _kling_error(response):
    try:
        data = response.json()
        return int(data.get("code") or 0), str(data.get("message", ""))[:200]
    except (ValueError, AttributeError, TypeError):
        return 0, ""


async def veo_generate(http, key, model, prompt, fmt):
    """One 8-second Veo clip with sound; returns the MP4 bytes."""
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    body = {"instances": [{"prompt": prompt}],
            "parameters": {"aspectRatio": "16:9" if fmt == "landscape" else "9:16", "durationSeconds": 8}}
    try:
        response = await http.post(f"{GEMINI}/models/{model}:predictLongRunning", headers=headers, json=body, timeout=60)
    except httpx.HTTPError as exc:
        raise MediaError("failed", type(exc).__name__) from None
    detail = _google_error(response)
    if response.status_code in (401, 403) or "api key" in detail.lower():
        raise MediaError("bad_video_key", detail)
    if response.status_code == 429:
        raise MediaError("video_billing", detail)
    if response.status_code == 404:
        raise MediaError("model", model)
    if response.status_code >= 400:
        raise MediaError("failed", f"submit {response.status_code} {detail}")
    operation = response.json().get("name")
    if not operation:
        raise MediaError("failed", "no operation")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS * 3)
        try:
            data = (await http.get(f"{GEMINI}/{operation}", headers=headers, timeout=30)).json()
        except (httpx.HTTPError, ValueError):
            continue
        if not data.get("done"):
            continue
        if data.get("error"):
            message = str(data["error"].get("message", ""))
            raise MediaError("blocked" if "safety" in message.lower() or "policy" in message.lower() else "failed", message[:200])
        result = (data.get("response") or {}).get("generateVideoResponse") or {}
        samples = result.get("generatedSamples") or []
        if not samples:
            raise MediaError("blocked" if result.get("raiMediaFilteredCount") else "failed", "no video")
        uri = (samples[0].get("video") or {}).get("uri")
        if not uri:
            raise MediaError("failed", "no video uri")
        video = await http.get(uri, headers={"x-goog-api-key": key}, timeout=180, follow_redirects=True)
        if video.status_code >= 400:
            raise MediaError("failed", f"download {video.status_code}")
        return video.content
    raise MediaError("timeout")


def _google_error(response):
    try:
        return str(response.json().get("error", {}).get("message", ""))[:200]
    except (ValueError, AttributeError):
        return ""


# ---------- Higgsfield: Seedance and other video models with native sound ----------

HIGGSFIELD = "https://api.higgsfield.ai"


async def hf_generate(http, key, model, prompt, fmt, seconds=SCENE_SECONDS, audio=True):
    """One Higgsfield clip with sound; key is "KEY_ID:KEY_SECRET". Returns the MP4 bytes."""
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    body = {"prompt": prompt, "duration": seconds, "aspect_ratio": "16:9" if fmt == "landscape" else "9:16",
            "resolution": "720p", "generate_audio": audio}
    try:
        response = await http.post(f"{HIGGSFIELD}/{model}", headers=headers, json=body, timeout=60)
    except httpx.HTTPError as exc:
        raise MediaError("failed", type(exc).__name__) from None
    detail = _hf_error(response)
    if response.status_code in (401, 403):
        raise MediaError("bad_video_key", detail)
    if response.status_code in (402, 429) or "credit" in detail.lower() or "balance" in detail.lower():
        raise MediaError("video_billing", detail)
    if response.status_code == 404:
        raise MediaError("model", model)
    if response.status_code >= 400:
        raise MediaError("failed", f"submit {response.status_code} {detail}")
    job = response.json()
    status_url = job.get("status_url") or (f"{HIGGSFIELD}/requests/{job['request_id']}/status" if job.get("request_id") else "")
    if not status_url:
        raise MediaError("failed", "no request id")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS * 2)
        try:
            data = (await http.get(status_url, headers=headers, timeout=30)).json()
        except (httpx.HTTPError, ValueError):
            continue
        status = str(data.get("status", "")).lower()
        if status == "completed":
            url = result_url(data)
            if not url:
                raise MediaError("failed", "no video url")
            video = await http.get(url, timeout=180, follow_redirects=True)
            if video.status_code >= 400:
                raise MediaError("failed", f"download {video.status_code}")
            return video.content
        if status == "nsfw":
            raise MediaError("blocked")
        if status in ("failed", "canceled", "cancelled"):
            raise MediaError("failed", status)
    raise MediaError("timeout")


def _hf_error(response):
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("detail") or data.get("message") or data.get("error") or "")[:200]


# ---------- free: Cloudflare Workers AI / Pollinations pictures, videos from pictures ----------

POLLINATIONS = "https://image.pollinations.ai/prompt/"
FREE_SIZES = {"square": (1024, 1024), "portrait": (768, 1344), "landscape": (1344, 768)}


async def free_image(http, prompt, fmt, cloudflare=None):
    """A Flux picture for free: Cloudflare Workers AI when its account id and token are given, else Pollinations."""
    if cloudflare and all(cloudflare):
        account, token = cloudflare
        try:
            response = await http.post(
                f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/black-forest-labs/flux-1-schnell",
                headers={"Authorization": f"Bearer {token}"}, json={"prompt": prompt[:2000], "steps": 6}, timeout=90)
            image = (response.json().get("result") or {}).get("image") if response.status_code == 200 else None
            if image:
                return base64.b64decode(image)
        except (httpx.HTTPError, ValueError):
            pass  # Pollinations below
    width, height = FREE_SIZES.get(fmt, FREE_SIZES["square"])
    params = {"width": width, "height": height, "model": "flux", "nologo": "true", "private": "true",
              "seed": str(int(time.time() * 1000) % 1_000_000)}
    for attempt in range(4):
        try:
            response = await http.get(POLLINATIONS + quote(prompt[:1500], safe=""), params=params, timeout=150,
                                      follow_redirects=True)
        except httpx.HTTPError as exc:
            if attempt == 3:
                raise MediaError("failed", type(exc).__name__) from None
            await asyncio.sleep(5)
            continue
        if response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"):
            return response.content
        if response.status_code in (429, 502, 503, 504) and attempt < 3:
            await asyncio.sleep(16)  # anonymous use: about one picture per 15 seconds
            continue
        raise MediaError("failed", f"pollinations {response.status_code}")
    raise MediaError("failed", "pollinations busy")


async def living_picture(image, seconds, fmt, index=0):
    """A picture brought to life: a slow zoom with a gentle pan (alternating direction). Returns silent MP4 bytes."""
    width, height = FRAME.get(fmt, FRAME["square"])
    frames = int(seconds * 30)
    moves = ["x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
             "x='(iw-iw/zoom)*on/{f}':y='ih/2-(ih/zoom/2)'",
             "x='(iw-iw/zoom)*(1-on/{f})':y='ih/2-(ih/zoom/2)'",
             "x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*on/{f}'"]
    move = moves[index % len(moves)].format(f=frames)
    with tempfile.TemporaryDirectory(prefix="secret-ai-pic-") as folder:
        folder = Path(folder)
        (folder / "in.img").write_bytes(image)
        out = folder / "clip.mp4"
        # Light enough for a tiny server: a 1.3x working canvas and the fastest x264 preset.
        work_w, work_h = int(width * 1.3) // 2 * 2, int(height * 1.3) // 2 * 2
        await _ffmpeg("-i", str(folder / "in.img"), "-vf",
                      f"scale={work_w}:{work_h}:force_original_aspect_ratio=increase,crop={work_w}:{work_h},"
                      f"zoompan=z='min(zoom+0.0009,1.18)':{move}:d={frames}:s={width}x{height}:fps=30,format=yuv420p",
                      "-frames:v", str(frames), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-g", "60",
                      str(out), timeout=900)
        return out.read_bytes()


# ---------- Free real video: open models (Wan 2.2, LTX) on Hugging Face ZeroGPU spaces, with a free HF token ----------

HF_SPACES = ("Wan-AI/Wan-2.2-5B", "Lightricks/ltx-video-distilled")  # tried in order; HF_SPACES env overrides
SPACE_QUOTA = ("quota", "exceeded", "limit", "too many", "zerogpu", "gpu task aborted", "pro ")


def _space_call(space, token, prompt, fmt):
    """Blocking: one clip from a Gradio space; finds its text-to-video endpoint and fills only the prompt and orientation."""
    from gradio_client import Client  # imported here: only the free video path needs it

    client = Client(space, token=token or None, verbose=False, download_files=tempfile.gettempdir())
    endpoints = (client.view_api(print_info=False, return_format="dict") or {}).get("named_endpoints") or {}

    def score(item):
        name, info = item
        params = [p.get("parameter_name") or "" for p in info.get("parameters", [])]
        returns = " ".join(str(r.get("component", "")) for r in info.get("returns", []))
        if not any("prompt" == p or p.endswith("prompt") and "negative" not in p for p in params) or "Video" not in returns:
            return -1
        return 3 if "text_to_video" in name or "t2v" in name else 2 if "generate" in name else 1

    ranked = sorted(endpoints.items(), key=score, reverse=True)
    if not ranked or score(ranked[0]) < 0:
        raise MediaError("model", space)
    name, info = ranked[0]
    kwargs, sizes = {}, {}
    for param in info.get("parameters", []):
        key = param.get("parameter_name")
        if not key:
            continue
        if key == "prompt" or key.endswith("prompt") and "negative" not in key:
            kwargs[key] = prompt
        elif key in ("mode", "generation_mode") and "text" in str(param.get("parameter_default", "")).lower() + str(param.get("type", "")).lower():
            kwargs[key] = "text-to-video"
        elif any(word in key for word in ("height", "width")) and isinstance(param.get("parameter_default"), (int, float)):
            sizes[key] = param["parameter_default"]
        elif not param.get("parameter_has_default"):
            kwargs[key] = None  # an optional image or video input left empty
    heights = [k for k in sizes if "height" in k]
    widths = [k for k in sizes if "width" in k]
    if heights and widths:  # keep the space's size, turned to the asked orientation
        h, w = sizes[heights[0]], sizes[widths[0]]
        tall = fmt == "portrait"
        if (h > w) != tall and fmt != "square":
            h, w = w, h
        if fmt == "square":
            h = w = min(h, w)
        kwargs[heights[0]], kwargs[widths[0]] = h, w
    result = client.predict(api_name=name, **kwargs)
    path = _video_path(result)
    if not path:
        raise MediaError("failed", "no video in the result")
    return Path(path).read_bytes()


def _video_path(result):
    if isinstance(result, str):
        return result if Path(result).suffix.lower() in (".mp4", ".webm", ".mov", ".mkv") and Path(result).exists() else ""
    if isinstance(result, dict):
        return _video_path(result.get("video")) or _video_path(result.get("path")) or _video_path(result.get("value"))
    if isinstance(result, (list, tuple)):
        return next((p for p in map(_video_path, result) if p), "")
    return ""


async def space_video(prompt, fmt, token, spaces=HF_SPACES):
    """A real AI clip (about 5 s) from the first free space that answers; MediaError("video_billing") when the daily
    free GPU quota is used up everywhere."""
    quota = False
    for space in spaces:
        try:
            return await asyncio.wait_for(asyncio.to_thread(_space_call, space, token, prompt, fmt), 420)
        except Exception as exc:  # next space: this one is busy, out of quota, changed or down
            text = str(exc).lower()
            quota = quota or any(word in text for word in SPACE_QUOTA)
            LOG.warning("Free video space %s failed: %s %s", space, type(exc).__name__, str(exc)[:160])
    raise MediaError("video_billing" if quota else "failed", "free spaces")


async def fit_clip(video, seconds, fmt):
    """Re-encode a clip to the living_picture format (size, 30 fps, no sound) and exactly seconds long: a short clip
    plays in gentle slow motion (up to 2x) and holds its last frame for the rest."""
    width, height = FRAME.get(fmt, FRAME["square"])
    with tempfile.TemporaryDirectory(prefix="secret-ai-fit-") as folder:
        folder = Path(folder)
        (folder / "in.mp4").write_bytes(video)
        length = await _duration(folder / "in.mp4") or seconds
        slow = min(2.0, max(1.0, seconds / length))
        out = folder / "clip.mp4"
        await _ffmpeg("-i", str(folder / "in.mp4"), "-an", "-vf",
                      f"setpts={slow:.3f}*PTS,fps=30,scale={width}:{height}:force_original_aspect_ratio=increase,"
                      f"crop={width}:{height},tpad=stop_mode=clone:stop_duration={seconds},format=yuv420p",
                      "-t", f"{seconds:.2f}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-g", "60",
                      str(out), timeout=600)
        return out.read_bytes()


async def _duration(path):
    """Seconds of a media file (from ffmpeg's banner), 0 when unknown."""
    process = await asyncio.create_subprocess_exec(imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path),
                                                   stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await process.communicate()
    match = re.search(rb"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err or b"")
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3]) if match else 0


async def join_with_voice(clips, seconds, voice=None):
    """Join same-format silent clips (seconds long in total) without re-encoding the picture; voice (or silence) over them."""
    with tempfile.TemporaryDirectory(prefix="secret-ai-join-") as folder:
        folder = Path(folder)
        names = []
        for index, data in enumerate(clips):
            (folder / f"c{index}.mp4").write_bytes(data)
            names.append(f"c{index}.mp4")
        (folder / "list.txt").write_text("".join(f"file '{name}'\n" for name in names))
        audio = ["-i", str(folder / "voice.mp3")] if voice else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        if voice:
            (folder / "voice.mp3").write_bytes(voice)
        out = folder / "video.mp4"
        await _ffmpeg("-f", "concat", "-safe", "0", "-i", str(folder / "list.txt"), *audio,
                      "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                      "-af", "apad", "-t", f"{seconds:.2f}", "-movflags", "+faststart", str(out), timeout=300)
        return out.read_bytes()


# ---------- OpenAI GPT Image: pictures and photo editing (several photos at once, faces kept) ----------

OPENAI_IMAGE = "openai-image"
OPENAI_SIZES = {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"}
KEEP_FACES = ("Use the provided photos as the source. Keep every person's face, identity, skin tone, hair, body shape and "
              "clothes exactly as in the photos unless the request says otherwise; match lighting, perspective and scale so "
              "the result looks like one real, natural photograph. Change only what is asked.")


async def openai_image(client, model, prompt, fmt, images=None):
    """A picture from GPT Image: generated from the prompt, or an edit/combination of the given photos (bytes)."""
    if images:
        files = [(f"photo{index}.jpg", data, "image/jpeg") for index, data in enumerate(images)]
        request = {"model": model, "image": files, "prompt": f"{prompt}\n\n{KEEP_FACES}", "size": "auto", "quality": "high"}
        try:
            response = await client.images.edit(**request, input_fidelity="high")
        except Exception as exc:  # older image models don't know input_fidelity
            if "input_fidelity" not in str(exc):
                raise
            response = await client.images.edit(**request)
    else:
        response = await client.images.generate(model=model, prompt=prompt, size=OPENAI_SIZES.get(fmt, "1024x1024"),
                                                quality="medium")
    image = response.data[0].b64_json if response.data else None
    if not image:
        raise MediaError("failed", "no image")
    return base64.b64decode(image)


# ---------- Nano Banana (Google) through Abacus.AI RouteLLM: chat completions with image output ----------

ASPECTS = {"square": "1:1", "portrait": "9:16", "landscape": "16:9"}


async def abacus_image(client, http, model, prompt, fmt, images=None):
    """A picture from an image model on RouteLLM (e.g. Nano Banana Pro); input photos are sent as data URIs."""
    content = [{"type": "text", "text": f"{prompt}\n\n{KEEP_FACES}" if images else prompt}]
    for data in images or []:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()}})
    response = await client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": content}],
        extra_body={"modalities": ["image", "text"], "image_config": {"num_images": 1, "aspect_ratio": ASPECTS.get(fmt, "1:1")}})
    message = response.choices[0].message if response.choices else None
    if message is None:
        raise MediaError("failed", "no answer")
    urls = []
    extra = getattr(message, "model_extra", None) or {}
    for item in extra.get("images") or []:
        url = (item.get("image_url") or {}).get("url") if isinstance(item, dict) else None
        if url:
            urls.append(url)
    if isinstance(message.content, list):  # some models put the picture into the content parts
        urls += [part.get("image_url", {}).get("url") for part in message.content
                 if isinstance(part, dict) and part.get("type") == "image_url"]
    url = next((u for u in urls if u), None)
    if not url:
        raise MediaError("blocked" if message.content else "failed", str(message.content or "no image")[:200])
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    picture = await http.get(url, timeout=120, follow_redirects=True)
    if picture.status_code >= 400:
        raise MediaError("failed", f"download {picture.status_code}")
    return picture.content
