import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot


def job(**extra):
    return dict({"user_id": 7, "language": "ru", "prompt": "кот в космосе", "fmt": "square", "images": [], "chat_id": 7},
                **extra)


def press(data, user_id=7):
    return NS(data=data, answer=AsyncMock(), message=NS(message_id=55))


class PhotoButtonsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = {"http": NS(get=AsyncMock(return_value=NS(content=b"downloaded", raise_for_status=lambda: None)))}
        self.bot = NS(send_document=AsyncMock(), send_video=AsyncMock(), send_chat_action=AsyncMock(),
                      send_message=AsyncMock())
        self.context = NS(bot_data=self.data, bot=self.bot, application=NS(bot_data=self.data))

    def update(self, user_id=7):
        return NS(effective_user=NS(id=user_id, language_code="ru"))

    def token(self, markup):
        return markup.inline_keyboard[0][0].callback_data.split(":")[2]

    async def finish_tasks(self):
        await asyncio.gather(*list(self.data.get("gen_tasks", ())))

    async def test_buttons_and_old_pictures_are_forgotten(self):
        markup = bot.photo_keyboard(self.data, job(), image=b"img")
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertEqual(labels, ["🔁 Ещё вариант", "📎 Оригинал", "🔍 4K", "🎬 Оживить"])
        for _ in range(bot.PHOTO_ACTIONS_KEPT + 5):
            bot.photo_keyboard(self.data, job(), image=b"img")
        self.assertEqual(len(self.data["photo_actions"]), bot.PHOTO_ACTIONS_KEPT)
        query = press(f"ph:f:{self.token(markup)}")
        await bot.photo_button(self.update(), self.context, query)
        self.assertIn("старая", query.answer.call_args.args[0])

    async def test_only_the_person_who_asked_can_press(self):
        token = self.token(bot.photo_keyboard(self.data, job(), image=b"img"))
        query = press(f"ph:f:{token}")
        await bot.photo_button(self.update(user_id=8), self.context, query)
        self.assertIn("заказал", query.answer.call_args.args[0])
        self.bot.send_document.assert_not_called()

    async def test_original_file_downloads_a_url_picture_once(self):
        token = self.token(bot.photo_keyboard(self.data, job(), url="https://cdn.test/p.png"))
        await bot.photo_button(self.update(), self.context, press(f"ph:f:{token}"))
        await self.finish_tasks()
        sent = self.bot.send_document.call_args
        self.assertEqual(sent.args[0], 7)
        self.assertEqual(self.data["photo_actions"][token]["image"], b"downloaded")
        self.assertFalse(self.data["photo_actions"][token]["busy"])

    async def test_living_picture_is_sent_as_video(self):
        token = self.token(bot.photo_keyboard(self.data, job(), image=b"img"))
        with patch.object(bot, "living_picture", AsyncMock(return_value=b"mp4")) as living:
            await bot.photo_button(self.update(), self.context, press(f"ph:l:{token}"))
            await self.finish_tasks()
        self.assertEqual(living.call_args.args[:3], (b"img", bot.LIVING_SECONDS, "square"))
        self.bot.send_video.assert_called_once()

    async def test_another_take_starts_a_normal_generation(self):
        token = self.token(bot.photo_keyboard(self.data, job(images=["file1"]), image=b"img"))
        query = press(f"ph:a:{token}")
        with patch.object(bot, "start_generation", AsyncMock(return_value=None)) as start:
            await bot.photo_button(self.update(), self.context, query)
        args, kwargs = start.call_args
        self.assertEqual(args[1:6], (7, "ru", "photo", "кот в космосе", "square"))
        self.assertEqual(kwargs["images"], ["file1"])
        self.assertEqual(kwargs["reply_id"], 55)
        self.assertIn("ещё вариант", query.answer.call_args.args[0].lower())


if __name__ == "__main__":
    unittest.main()
