"""Photo and video generation through fal.ai (one key, many models, pay per generation).

Requests go through fal's queue API: submit, poll the status, fetch the result.
The bot owner pastes the fal key in the Mini App; it is stored encrypted.
"""
import asyncio
import time

import httpx

FAL_QUEUE = "https://queue.fal.run"
DEFAULT_MODELS = {"photo": "fal-ai/flux/schnell", "video": "fal-ai/kling-video/v2.6/pro/text-to-video"}
TIMEOUTS = {"photo": 180, "video": 900}
POLL_SECONDS = 3

# format id -> (fal image_size, video aspect_ratio)
FORMATS = {"square": ("square_hd", "1:1"), "portrait": ("portrait_16_9", "9:16"), "landscape": ("landscape_16_9", "16:9")}

FREE_PHOTOS_PER_DAY = 5
COST = {"photo": 1, "video": 15}  # in credits
# Credit packs sold for Telegram Stars.
PACKS = {"s": (20, 25), "m": (100, 100), "l": (300, 250)}  # id: (credits, stars)
MAX_PROMPT = 1500


class MediaError(Exception):
    """A user-facing failure: code is one of no_key, bad_key, model, timeout, failed, blocked."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def build_input(kind, prompt, fmt):
    image_size, aspect = FORMATS.get(fmt, FORMATS["square"])
    if kind == "photo":
        return {"prompt": prompt, "image_size": image_size, "num_images": 1, "enable_safety_checker": True}
    return {"prompt": prompt, "aspect_ratio": aspect, "duration": "5"}


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


async def fal_generate(http, key, model, kind, prompt, fmt):
    """Run one generation and return the URL of the result."""
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    try:
        response = await http.post(f"{FAL_QUEUE}/{model}", headers=headers, json=build_input(kind, prompt, fmt), timeout=30)
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
