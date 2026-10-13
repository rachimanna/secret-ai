"""Welcome video note (a round "кружок") for new users: an AI host says hi in a voice.

Made once and reused by its Telegram file_id: an AI presenter (fal: Flux portrait + SadTalker lip sync) when a fal key is
set, otherwise the mascot "speaking" over the voice. The owner can replace it with their own recorded video note.
"""
import asyncio
import base64
import tempfile
from pathlib import Path

import httpx
import imageio_ffmpeg

from media import MediaError, fal_run

ASSETS = Path(__file__).with_name("assets")
VERSION = "v2"
TEXT = {
    "ru": "Привет! Я Secret AI, твой личный ИИ. Пиши что хочешь: вопросы, тексты, код, сайты, картинки и видео. "
          "Можно голосом, отвечу голосом. Погнали!",
    "en": "Hi! I'm Secret AI, your personal AI. Ask me anything: questions, texts, code, websites, pictures and videos. "
          "You can talk to me by voice too. Let's go!",
}
PRESENTER = ("friendly young man in his twenties, modern casual black hoodie, warm smile, looking straight into the camera, "
             "head and shoulders, centered face, soft studio light, dark purple background, photorealistic portrait, 85mm")
TALKING_MODEL = "fal-ai/sadtalker"
SIZE = 640


async def speech(client, model, voice, text):
    response = await client.audio.speech.create(model=model, voice=voice, input=text, response_format="mp3")
    return response.content


async def _ffmpeg(*args, timeout=180):
    process = await asyncio.create_subprocess_exec(imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-y", "-v", "error", *args,
                                                   stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(process.communicate(), timeout)
    if process.returncode:
        raise MediaError("failed", "ffmpeg: " + err.decode(errors="replace")[-200:])


async def mascot_note(voice_mp3):
    """The mascot on little legs writing the welcome on a board (assets/welcome-board.mp4, 18 s) with the voice over it.

    The picture is pre-rendered (tools/render_welcome.py) and copied as is, so this takes a fraction of a second even
    on a tiny server; the video ends with the voice.
    """
    with tempfile.TemporaryDirectory(prefix="secret-ai-welcome-") as folder:
        folder = Path(folder)
        (folder / "voice.mp3").write_bytes(voice_mp3)
        out = folder / "note.mp4"
        await _ffmpeg("-i", str(ASSETS / "welcome-board.mp4"), "-i", str(folder / "voice.mp3"),
                      "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-shortest",
                      "-movflags", "+faststart", str(out), timeout=120)
        return out.read_bytes()


async def presenter_note(http, fal_key, voice_mp3):
    """An AI presenter saying the welcome (Flux portrait + SadTalker), cropped square. Raises MediaError."""
    portrait = await fal_run(http, fal_key, "fal-ai/flux/schnell",
                             {"prompt": PRESENTER, "image_size": "square_hd", "num_images": 1}, 120)
    audio = "data:audio/mpeg;base64," + base64.b64encode(voice_mp3).decode()
    video_url = await fal_run(http, fal_key, TALKING_MODEL, {"source_image_url": portrait, "driven_audio_url": audio,
                                                            "face_model_resolution": "512", "preprocess": "full",
                                                            "expression_scale": 1.0}, 600)
    try:
        video = await http.get(video_url, timeout=120, follow_redirects=True)
        video.raise_for_status()
    except httpx.HTTPError as exc:
        raise MediaError("failed", type(exc).__name__) from None
    with tempfile.TemporaryDirectory(prefix="secret-ai-welcome-") as folder:
        folder = Path(folder)
        (folder / "in.mp4").write_bytes(video.content)
        out = folder / "note.mp4"
        await _ffmpeg("-i", str(folder / "in.mp4"), "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale={SIZE}:{SIZE},fps=30,format=yuv420p",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-c:a", "aac", "-b:a", "96k",
                      "-movflags", "+faststart", str(out))
        return out.read_bytes()
