import asyncio
import io
import json
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot
import chatserial as serial
import digest
from fun import GroupStore
from media import _duration, _has_audio
from telegram.error import TimedOut


def plan_json():
    return json.dumps(dict(title="Кто украл понедельник?", recap="Рахим ищет пропавший понедельник.", scenes=[
        dict(speaker=name, text=text, visual="A mysterious city at night", mood="mystery")
        for name, text in [("Рассказчик", "В чате неожиданно пропал понедельник."),
                           ("Рахим", "Я проверил календарь. Тут только пятница!"),
                           ("Анна", "Это мой лучший план за неделю."),
                           ("Рассказчик", "Но кто-то уже спрятал вторник…")]]), ensure_ascii=False)


def wav_audio():
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00" * 22050)
    return out.getvalue()


class SerialTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "test.db")
        self.store = GroupStore(self.path)
        self.ask = AsyncMock(return_value=plan_json())
        self.show = serial.Serial(self.store, self.ask, AsyncMock(return_value=wav_audio()))
        self.telegram = NS(send_message=AsyncMock(), send_video=AsyncMock(), send_photo=AsyncMock())
        self.store.touch(-1, "Друзья", 1)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def populate(self, count=20):
        for n in range(count):
            self.store.add_line(-1, f"{'Рахим' if n % 2 else 'Анна'}: Где понедельник? {n}")

    async def fake_render(self, folder, plan, episode, cast, speak, image):
        serial.voice_for("Рахим", cast)
        (folder / "episode.mp4").write_bytes(b"video")

    def test_history_isolated_recent_commands_and_bot_excluded(self):
        self.populate()
        self.store.add_line(-2, "Чужой: secret")
        self.store.add_line(-1, "Secret AI: answer")
        self.store.add_line(-1, "Анна: /serial")
        self.store.add_line(-1, "Старый: не включать")
        with self.store.db:
            self.store.db.execute("UPDATE group_messages SET created=? WHERE line LIKE 'Старый:%'",
                                  (int(time.time()) - serial.WEEK - 1,))
        lines, names = self.show.history(-1)
        self.assertEqual(len(lines), 20)
        self.assertEqual(set(names), {"Рахим", "Анна"})
        self.assertTrue(lines[0].endswith("0"))

    def test_screenplay_validation_and_budgets(self):
        plan = serial.screenplay("```json\n" + plan_json() + "\n```", ["Рахим"])
        self.assertEqual(plan["scenes"][2]["speaker"], serial.NARRATOR)
        for raw in ("[]", "null", '{"scenes":[]}', "not json"):
            with self.assertRaises((ValueError, TypeError)):
                serial.screenplay(raw, [])
        obj = json.loads(plan_json())
        obj["scenes"] *= 10
        for s in obj["scenes"]:
            s["text"] = "Очень длинная реплика. " * 40
        bounded = serial.screenplay(json.dumps(obj), ["Рахим", "Анна"])
        self.assertLessEqual(len(bounded["scenes"]), 8)
        self.assertLessEqual(sum(len(s["text"]) for s in bounded["scenes"]), 850)

    def test_schedule_survives_restart_and_on_is_idempotent(self):
        with patch.object(serial.time, "time", return_value=2000000000):
            self.show.enable(-1, True)
        due = self.show.state(-1)["next_at"]
        other = serial.Serial(self.store, self.ask, AsyncMock())
        with patch.object(serial.time, "time", return_value=2000000100):
            other.enable(-1, True)
        self.assertEqual(other.state(-1)["next_at"], due)
        other.enable(-1, False)
        self.assertFalse(other.state(-1)["enabled"])
        self.assertEqual(other.due(), [])

    async def test_delivery_success_advances_story_and_continuation(self):
        self.populate()
        with patch.object(serial, "make_episode", side_effect=self.fake_render):
            self.assertTrue(await self.show.post(self.telegram, -1, "Друзья", manual=True))
            self.assertEqual(self.show.state(-1)["episode"], 1)
            self.assertIn("Рахим", self.show.state(-1)["cast"])
            with self.store.db:
                self.store.db.execute("UPDATE group_serial SET last_attempt=0")
            self.assertTrue(await self.show.post(self.telegram, -1, "Друзья", manual=True))
        context = json.loads(self.ask.call_args.args[2])
        self.assertEqual(context["episode"], 2)
        self.assertIn("понедельник", context["previous_story"])

    async def test_failed_upload_does_not_advance_and_has_retry_backoff(self):
        self.populate()
        self.show.enable(-1, True)
        self.telegram.send_video.side_effect = TimedOut()
        with patch.object(serial, "make_episode", side_effect=self.fake_render):
            self.assertFalse(await self.show.post(self.telegram, -1, "Друзья", manual=True))
        state = self.show.state(-1)
        self.assertEqual(state["episode"], 0)
        self.assertEqual(state["recap"], "")
        self.assertGreater(state["retry_at"], time.time())
        self.assertEqual(state["lease_until"], 0)
        self.telegram.send_video.assert_awaited_once()

    async def test_no_history_does_not_invent_episode(self):
        self.assertFalse(await self.show.post(self.telegram, -1, "Друзья", manual=True))
        self.ask.assert_not_awaited()
        self.telegram.send_video.assert_not_awaited()

    async def test_concurrent_request_and_cancel_release_lease(self):
        self.populate()
        entered = asyncio.Event()
        async def hanging(*args):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(serial, "make_episode", side_effect=hanging):
            task = asyncio.create_task(self.show.post(self.telegram, -1, "Друзья"))
            await asyncio.wait_for(entered.wait(), 2)
            self.assertFalse(await self.show.post(self.telegram, -1, "Друзья", manual=True))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.show.busy)
        self.assertEqual(self.show.state(-1)["lease_until"], 0)
        self.assertEqual(self.show.state(-1)["episode"], 0)

    async def test_admin_switch_off_during_automatic_render(self):
        self.populate()
        self.show.enable(-1, True)
        async def render(*args):
            await self.fake_render(*args)
            self.show.enable(-1, False)
        with patch.object(serial, "make_episode", side_effect=render):
            self.assertFalse(await self.show.post(self.telegram, -1, "Друзья"))
        self.telegram.send_video.assert_not_awaited()

    async def test_weekly_tick_and_success_prevent_second_release(self):
        self.populate()
        self.show.enable(-1, True)
        with self.store.db:
            self.store.db.execute("UPDATE group_serial SET next_at=0")
        with patch.object(serial, "make_episode", side_effect=self.fake_render):
            await self.show.tick(self.telegram)
            await self.show.tick(self.telegram)
        self.telegram.send_video.assert_awaited_once()

    async def test_wipe_during_render_does_not_publish_forgotten_story(self):
        self.populate()
        async def render(*args):
            await self.fake_render(*args)
            with self.store.db:
                self.store.db.execute("DELETE FROM group_serial")
        with patch.object(serial, "make_episode", side_effect=render):
            self.assertFalse(await self.show.post(self.telegram, -1, "Друзья", manual=True))
        self.telegram.send_video.assert_not_awaited()

    async def test_command_permissions_allowlist_and_on_off(self):
        message = NS(chat=NS(type="group", title="Друзья"), chat_id=-1, reply_text=AsyncMock())
        data = dict(allowed=set(), groups=self.store, serial=self.show, models=["model"])
        update = NS(effective_message=message, effective_user=NS(id=1, is_bot=False))
        telegram = NS(get_chat_member=AsyncMock(return_value=NS(status="member")))
        context = NS(bot_data=data, bot=telegram, args=["on"])
        await bot.serial_command(update, context)
        self.assertFalse(self.show.state(-1)["enabled"])
        telegram.get_chat_member.return_value.status = "administrator"
        await bot.serial_command(update, context)
        self.assertTrue(self.show.state(-1)["enabled"])
        context.args = ["off"]
        await bot.serial_command(update, context)
        self.assertFalse(self.show.state(-1)["enabled"])
        data["allowed"] = {2}
        telegram.get_chat_member.reset_mock()
        await bot.serial_command(update, context)
        telegram.get_chat_member.assert_not_awaited()

    async def test_real_mp4_with_audio_and_cyrillic_subtitles_without_network(self):
        folder = Path(self.tmp.name)
        plan = serial.screenplay(plan_json(), ["Рахим", "Анна"])
        cast = {}
        await serial.make_episode(folder, plan, 1, cast, AsyncMock(return_value=wav_audio()))
        output = folder / "episode.mp4"
        self.assertGreater(output.stat().st_size, 10000)
        self.assertTrue(await _has_audio(output))
        self.assertGreater(await _duration(output), 9)
        self.assertEqual(len(cast), 3)
        self.assertNotEqual(cast["Рахим"], cast["Анна"])

    async def test_digest_marks_only_after_delivery_and_does_not_duplicate_timeout(self):
        self.populate()
        day = digest.Digest(self.store, AsyncMock(return_value="<b>Итоги</b>"))
        self.telegram.send_photo.side_effect = TimedOut()
        with patch.object(digest, "render", return_value=b"photo"):
            with self.assertRaises(TimedOut):
                await day.post(self.telegram, -1, "Друзья")
            self.assertIn(-1, day._due(digest.today().isoformat()))
            self.telegram.send_photo.assert_awaited_once()
            self.telegram.send_photo.side_effect = None
            await day.post(self.telegram, -1, "Друзья")
        self.assertNotIn(-1, day._due(digest.today().isoformat()))


if __name__ == "__main__":
    unittest.main()
