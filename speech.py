"""Speech without OpenAI: recognition by a local Whisper model, voices by Microsoft's free neural voices.

LocalWhisper answers client.audio.transcriptions.create(...) like the OpenAI client, so the code that reads voice messages
and TikTok sound works with either. The model (WHISPER_MODEL, "base" by default) is downloaded once into models/ next to
the bot and runs on the CPU; a 30-second voice message takes a few seconds.
"""
import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace

LOG = logging.getLogger("secret_ai.speech")
HERE = Path(__file__).resolve().parent
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base").strip() or "base"
EDGE_VOICE = os.environ.get("EDGE_VOICE", "ru-RU-DmitryNeural").strip() or "ru-RU-DmitryNeural"  # a warm male voice
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
