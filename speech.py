"""Speech without OpenAI: recognition by a local Whisper model (if faster-whisper is installed; it doesn't fit a 4 GB disk,
so usually AnyModel's whisper-1 does it), voices by Microsoft's free neural voices.

LocalWhisper answers client.audio.transcriptions.create(...) like the OpenAI client, so the code that reads voice messages
and TikTok sound works with either. The model (WHISPER_MODEL, "base" by default) is downloaded once into models/ next to
the bot and runs on the CPU; a 30-second voice message takes a few seconds.
"""
import asyncio
import logging
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

LOG = logging.getLogger("secret_ai.speech")
HERE = Path(__file__).resolve().parent
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base").strip() or "base"
EDGE_VOICE = os.environ.get("EDGE_VOICE", "ru-RU-DmitryNeural").strip() or "ru-RU-DmitryNeural"  # a warm male voice
# Russian voices for /voice: name -> Microsoft voice id, and what each sounds like.
RU_VOICES = {"дмитрий": "ru-RU-DmitryNeural", "dmitry": "ru-RU-DmitryNeural",
             "светлана": "ru-RU-SvetlanaNeural", "svetlana": "ru-RU-SvetlanaNeural",
             "дария": "ru-RU-DariyaNeural", "dariya": "ru-RU-DariyaNeural"}
RU_LABELS = {"дмитрий": "мужской, тёплый (по умолчанию)", "светлана": "женский, спокойный", "дария": "женский, живой"}
EDGE_VOICES = {"ru": EDGE_VOICE, "en": "en-US-AndrewNeural", "uk": "uk-UA-OstapNeural", "kk": "kk-KZ-DauletNeural",
               "uz": "uz-UZ-SardorNeural", "az": "az-AZ-BabekNeural", "hy": "hy-AM-HaykNeural", "ka": "ka-GE-GiorgiNeural",
               "tr": "tr-TR-AhmetNeural", "de": "de-DE-ConradNeural", "es": "es-ES-AlvaroNeural", "fr": "fr-FR-HenriNeural"}


class LocalWhisper:
    """client.audio.transcriptions.create(model=..., file=..., language=...) -> .text, on the server's CPU."""

    def __init__(self, fallback=None):
        # fallback: an OpenAI-compatible gateway client (AnyModel) asked for whisper-1 if the local model can't run.
        self.model, self.lock, self.fallback = None, asyncio.Lock(), fallback
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))

    def _load(self):
        from faster_whisper import WhisperModel  # imported here: only voice messages need it
        LOG.info("Loading Whisper %s for voice messages", WHISPER_MODEL)
        return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=str(HERE / "models"))

    def _run(self, file, language):
        segments, _ = self.model.transcribe(file, language=language or None, beam_size=1, vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def create(self, model=None, file=None, language=None, **_):
        try:
            async with self.lock:  # one recognition at a time keeps the memory of a small server in check
                if self.model is None:
                    self.model = await asyncio.to_thread(self._load)
                text = await asyncio.to_thread(self._run, file, language)
            return SimpleNamespace(text=text)
        except Exception as exc:
            if not self.fallback:
                raise
            LOG.warning("Local Whisper failed (%s %s); asking the gateway", type(exc).__name__, str(exc)[:200])
            file.seek(0)
            hint = {"language": language} if language else {}
            return await self.fallback.audio.transcriptions.create(model="whisper-1", file=file, **hint)


GOOGLE_SPEECH = "http://www.google.com/speech-api/v2/recognize"
GOOGLE_KEY = os.environ.get("GOOGLE_SPEECH_KEY", "AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw")  # Chrome's public key
LOCALES = {"ru": "ru-RU", "en": "en-US", "uk": "uk-UA", "kk": "kk-KZ", "uz": "uz-UZ", "az": "az-AZ", "hy": "hy-AM",
           "ka": "ka-GE", "tr": "tr-TR", "de": "de-DE", "es": "es-ES", "fr": "fr-FR", "ky": "ky-KG", "tg": "tg-TJ"}
CHUNK_SECONDS = 25  # the free endpoint takes short clips: longer audio goes in pieces


class GoogleSpeech:
    """Free speech recognition through the endpoint Chrome uses (no key of ours, nothing on disk).
    Answers client.audio.transcriptions.create(model=..., file=..., language=...) like the OpenAI client."""

    def __init__(self):
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))

    async def create(self, model=None, file=None, language=None, **_):
        import httpx
        import imageio_ffmpeg
        locale = LOCALES.get((language or "ru")[:2], "ru-RU")
        with tempfile.TemporaryDirectory(prefix="stt-") as folder:
            source = Path(folder) / "in.audio"
            source.write_bytes(file.read())
            process = await asyncio.create_subprocess_exec(
                imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-y", "-v", "error", "-i", str(source), "-vn", "-ac", "1",
                "-ar", "16000", "-c:a", "flac", "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
                str(Path(folder) / "part%03d.flac"), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(process.wait(), 120)
            parts = sorted(Path(folder).glob("part*.flac"))
            if not parts:
                raise RuntimeError("the audio couldn't be read")
            texts = []
            async with httpx.AsyncClient(timeout=60) as http:
                for part in parts:
                    response = await http.post(GOOGLE_SPEECH, params={"client": "chromium", "lang": locale, "key": GOOGLE_KEY},
                                               content=part.read_bytes(), headers={"Content-Type": "audio/x-flac; rate=16000"})
                    if response.status_code >= 400:
                        raise RuntimeError(f"speech service {response.status_code}")
                    texts.append(_transcript(response.text))
        return SimpleNamespace(text=" ".join(t for t in texts if t).strip())


def _transcript(body):
    """The best transcript from the endpoint's answer: one JSON object per line, the first is often empty."""
    import json
    for line in body.splitlines():
        try:
            results = json.loads(line).get("result") or []
        except ValueError:
            continue
        for result in results:
            alternatives = result.get("alternative") or []
            if alternatives and alternatives[0].get("transcript"):
                return alternatives[0]["transcript"].strip()
    return ""


def voice_for(text):
    """A voice in the language of the text: Cyrillic is Russian, Latin is English unless told otherwise."""
    cyrillic = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    return EDGE_VOICE if cyrillic * 3 >= len([ch for ch in text if ch.isalpha()]) else EDGE_VOICES["en"]


async def edge_speech(text, voice=None):
    """MP3 speech from Microsoft Edge's free neural voices."""
    import edge_tts  # imported here: the dependency is only needed for voices
    voice = voice or voice_for(text)
    audio = bytearray()
    async for chunk in edge_tts.Communicate(text, voice, rate="+4%").stream():
        if chunk.get("type") == "audio":
            audio += chunk["data"]
    if not audio:
        raise RuntimeError("no audio from the voice service")
    return bytes(audio)
