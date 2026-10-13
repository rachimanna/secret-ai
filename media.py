"""Photo and video generation through fal.ai (one key, many models, pay per generation).

Requests go through fal's queue API: submit, poll the status, fetch the result.
The bot owner pastes the fal key in the Mini App; it is stored encrypted.
"""
import asyncio
import base64
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import imageio_ffmpeg

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
    """'free', 'veo', 'fal' or 'higgsfield' (anything else, e.g. bytedance/seedance-2.5/text-to-video)."""
    return "free" if model == "free" else "veo" if is_veo(model) else "fal" if is_fal(model) else "higgsfield"


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
