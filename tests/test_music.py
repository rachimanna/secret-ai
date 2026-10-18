import asyncio
import sys
import tempfile
import time
import unittest
import wave
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot
import music
from telegram.error import BadRequest


TRACK = music.Track("abcdefghijk", "Кино — Спокойная ночь", "Кино", 360)


def message(chat_id=-10, message_id=42):
    return NS(chat_id=chat_id, message_id=message_id, text="найди песню Кино Спокойная ночь",
              reply_text=AsyncMock(), reply_audio=AsyncMock(return_value=NS(audio=NS(file_id="telegram-track"))))


def selection(service, msg, user_id=7):
    token = "abcdefghijk"
    service.selections[token] = music.Selection(user_id, msg.chat_id, msg.message_id, time.monotonic(), [TRACK])
    return NS(data=f"music:{token}:0", message=msg, answer=AsyncMock())


class MusicTest(unittest.IsolatedAsyncioTestCase):
    def test_requests_and_unrelated_chat(self):
        for text in ("найди песню Кино Спокойная ночь", "найти песню Кино Спокойная ночь",
                     "@SecretBot найди песню Кино Спокойная ночь", "Найди мне песню: Кино Спокойная ночь @secretbot",
                     "пожалуйста, поищи трек Кино Спокойная ночь", "пришли песню «Кино Спокойная ночь»"):
            self.assertEqual(music.request(text, "secretbot"), "Кино Спокойная ночь")
        self.assertEqual(music.request("find song Kino"), "Kino")
        self.assertEqual(music.request("найди песню"), "")
        for text in ("привет", "как найти песню?", "напиши песню про школу", "не ищи песню Кино", "/song Kino",
                     "@OtherBot найди песню Кино"):
            self.assertIsNone(music.request(text, "secretbot"))

    def test_filters_duplicates_streams_missing_duration_and_limits(self):
        entry = dict(id=TRACK.id, title=TRACK.title, duration=TRACK.duration, uploader=TRACK.performer)
        entries = [None, dict(entry, is_live=True), dict(entry, duration=None), dict(entry, duration=float("nan")),
                   dict(entry, live_status="is_upcoming"), dict(entry, duration=1201), dict(entry, id="https://evil.test"),
                   dict(entry, availability="private"), entry, entry]
        self.assertEqual(music.tracks(entries), [TRACK])
        self.assertLessEqual(len(music.tracks([dict(entry, id=f"abcdefghij{i}") for i in range(9)])), 5)

    async def test_search_buttons_empty_and_failures(self):
        service, msg, status = music.Music(), message(), NS(message_id=50, edit_text=AsyncMock())
        msg.reply_text.return_value = status
        with patch.object(music, "search", AsyncMock(return_value=[TRACK])) as find:
            await service.find(msg, 7, "Кино")
        find.assert_awaited_once_with("Кино")
        keyboard = status.edit_text.call_args.kwargs["reply_markup"]
        self.assertIn("6:00", keyboard.inline_keyboard[0][0].text)
        self.assertLess(len(keyboard.inline_keyboard[0][0].callback_data.encode()), 65)
        self.assertEqual(next(iter(service.selections.values())).message_id, 50)
        self.assertFalse(service.searching)
        for result, error in (([], None), (None, music.MusicError("timeout"))):
            with patch.object(music, "search", AsyncMock(return_value=result, side_effect=error)):
                await service.find(msg, 7, "Кино")
            self.assertFalse(service.searching)
        await service.find(msg, 7, "")
        self.assertEqual(msg.reply_text.call_args.args[0], music.HELP)

    async def test_callback_cannot_be_used_by_another_person_chat_or_message(self):
        for change in ("user", "chat", "message", "index", "expired", "malformed"):
            service, msg = music.Music(), message()
            query = selection(service, msg)
            user_id = 7
            if change == "user":
                user_id = 8
            elif change == "chat":
                msg.chat_id = -11
            elif change == "message":
                msg.message_id = 43
            elif change == "index":
                query.data = query.data[:-1] + "4"
            elif change == "expired":
                next(iter(service.selections.values())).created -= music.RESULT_TTL + 1
            else:
                query.data = "music:https://evil.test:0"
            with patch.object(music, "download", AsyncMock()) as load:
                await service.choose(query, user_id)
            load.assert_not_awaited()
            self.assertTrue(query.answer.call_args.kwargs["show_alert"])

    async def test_full_audio_upload_cache_and_temp_cleanup(self):
        service, msg = music.Music(), message()
        msg.reply_text.return_value = NS(edit_text=AsyncMock())
        query = selection(service, msg)
        made = []

        async def load(track, folder):
            path = Path(folder) / "track.mp3"
            path.write_bytes(b"full audio data")
            made.append(path)
            return path

        with patch.object(music, "download", side_effect=load) as download:
            await service.choose(query, 7)
            self.assertFalse(made[0].exists())
            self.assertFalse(service.busy)
            self.assertEqual(service.files[TRACK.id], "telegram-track")
            self.assertEqual(msg.reply_audio.call_args.kwargs["duration"], 360)
            self.assertTrue(msg.reply_audio.call_args.args[0].closed)
            await service.choose(query, 7)
            self.assertEqual(download.await_count, 1)
            self.assertEqual(msg.reply_audio.call_args.args[0], "telegram-track")

    async def test_download_failure_releases_slot_and_preserves_choices(self):
        service, msg = music.Music(), message()
        status = NS(edit_text=AsyncMock())
        msg.reply_text.return_value = status
        query = selection(service, msg)
        with patch.object(music, "download", AsyncMock(side_effect=music.MusicError("unavailable"))):
            await service.choose(query, 7)
        self.assertFalse(service.busy)
        self.assertTrue(service.selections)
        msg.reply_audio.assert_not_awaited()
        self.assertIn("другой вариант", status.edit_text.call_args.args[0])

    async def test_invalid_telegram_cache_falls_back_to_download(self):
        service, msg = music.Music(), message()
        msg.reply_text.return_value = NS(edit_text=AsyncMock())
        query = selection(service, msg)
        service.files[TRACK.id] = "expired-file"
        msg.reply_audio.side_effect = BadRequest("Wrong file identifier")
        with patch.object(music, "download", AsyncMock(side_effect=music.MusicError("unavailable"))) as load:
            await service.choose(query, 7)
        load.assert_awaited_once()
        self.assertNotIn(TRACK.id, service.files)

    async def test_concurrency_limits_and_bounded_memory(self):
        service, msg = music.Music(), message()
        query = selection(service, msg)
        service.busy.update((1, 2))
        with patch.object(music, "download", AsyncMock()) as load:
            await service.choose(query, 7)
        load.assert_not_awaited()
        service.searching.add(7)
        with patch.object(music, "search", AsyncMock()) as find:
            await service.find(msg, 7, "Кино")
        find.assert_not_awaited()
        for i in range(300):
            service.selections[str(i)] = music.Selection(7, -1, i, time.monotonic(), [TRACK])
        service.prune()
        self.assertEqual(len(service.selections), music.MAX_SEARCHES)

    async def test_real_conversion_keeps_complete_duration(self):
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.wav"
            with wave.open(str(source), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(b"\x01\x00" * 16000 * 3)
            with patch.object(music, "worker", AsyncMock(return_value={"path": str(source)})):
                audio = await music.download(TRACK, folder)
            pcm = await music.run_process(imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", audio,
                                          "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1", timeout=10)
            self.assertAlmostEqual(len(pcm) / 32000, 3, delta=0.1)

    async def test_subprocess_timeout_and_cancellation(self):
        with self.assertRaises(music.MusicError) as raised:
            await music.run_process(sys.executable, "-c", "import time; time.sleep(30)", timeout=0.1)
        self.assertEqual(raised.exception.code, "timeout")
        task = asyncio.create_task(music.run_process(sys.executable, "-c", "import time; time.sleep(30)", timeout=40))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_worker_rejects_long_or_large_sources_before_download(self):
        from unittest.mock import MagicMock
        import yt_dlp
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        with patch.object(yt_dlp, "YoutubeDL", return_value=ydl) as factory:
            music.worker_main("search", "Кино")
            ydl.extract_info.assert_called_once_with("ytsearch12:Кино", download=False)
            self.assertTrue(factory.call_args.args[0]["js_runtimes"]["deno"]["path"])
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.webm"
            source.write_bytes(b"audio")
            ydl.prepare_filename.return_value = str(source)
            ydl.extract_info.return_value = dict(duration=360)
            with patch.object(yt_dlp, "YoutubeDL", return_value=ydl) as factory:
                self.assertEqual(music.worker_main("download", TRACK.id, folder), {"path": str(source)})
                options = factory.call_args.args[0]
                self.assertFalse(options["skip_unavailable_fragments"])
                for info, code in ((dict(duration=1201), "too_long"), (dict(duration=360, is_live=True), "too_long"),
                                   (dict(duration=360, filesize=music.MAX_BYTES + 1), "too_large"),
                                   (dict(duration=None), "unavailable")):
                    with self.assertRaises(music.MusicError) as raised:
                        options["match_filter"](info)
                    self.assertEqual(raised.exception.code, code)
                with self.assertRaises(music.MusicError):
                    options["progress_hooks"][0]({"downloaded_bytes": music.MAX_BYTES + 1})

    async def test_group_search_does_not_require_models_or_mention(self):
        msg, service = message(), music.Music()
        msg.sticker = None
        msg.chat = NS(title="Group")
        groups = NS(touch=lambda *args: None)
        update = NS(effective_message=msg, effective_user=NS(id=7, is_bot=False, full_name="User", username="user"))
        context = NS(bot=NS(username="SecretBot"), bot_data=dict(
            allowed=set(), groups=groups, locks=defaultdict(asyncio.Lock), models=[], music=service))
        with patch.object(service, "find", AsyncMock()) as find, patch.object(bot, "remember_group_message"):
            await bot.group_chat(update, context)
        find.assert_awaited_once_with(msg, 7, "Кино Спокойная ночь")

    async def test_private_text_and_group_callback_routing(self):
        msg, service = message(chat_id=7), music.Music()
        update = NS(effective_message=msg, effective_chat=NS(type="private"),
                    effective_user=NS(id=7, is_bot=False))
        msg.entities = msg.caption_entities = None
        context = NS(bot=NS(username="SecretBot"), bot_data=dict(allowed=set(), music=service))
        with patch.object(bot, "authorized", AsyncMock(return_value=True)), \
                patch.object(bot, "promo_typed", AsyncMock(return_value=False)), \
                patch.object(bot, "secretary_switch", AsyncMock(return_value=False)), \
                patch.object(service, "find", AsyncMock()) as find:
            await bot.chat(update, context)
        find.assert_awaited_once_with(msg, 7, "Кино Спокойная ночь")
        update.callback_query = selection(service, msg)
        with patch.object(service, "choose", AsyncMock()) as choose:
            await bot.on_button(update, context)
        choose.assert_awaited_once_with(update.callback_query, 7)
        context.bot_data["allowed"] = {999}
        with patch.object(service, "choose", AsyncMock()) as choose:
            await bot.on_button(update, context)
        choose.assert_not_awaited()

    def test_generation_removed(self):
        self.assertFalse(hasattr(bot, "song_command"))
        self.assertFalse((Path(bot.__file__).parent / "songs.py").exists())
        self.assertFalse(any(command == "song" for commands in bot.COMMANDS.values() for command, _ in commands))


if __name__ == "__main__":
    unittest.main()
