import unittest
from collections import defaultdict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import asyncio

import bot

LONG = "Длинный ответ про бег. " * 20


class AnswerButtonsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = {"locks": defaultdict(asyncio.Lock)}
        self.context = NS(bot_data=self.data)

    def update(self, user_id=1, code="ru"):
        return NS(effective_user=NS(id=user_id, language_code=code))

    def token(self, markup):
        return markup.inline_keyboard[0][0].callback_data.split(":")[2]

    def test_only_longer_answers_get_buttons(self):
        self.assertIsNone(bot.answer_keyboard(self.data, 1, "ru", "Коротко"))
        markup = bot.answer_keyboard(self.data, 1, "ru", LONG)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertEqual(labels, ["✂️ Короче", "➕ Подробнее", "🌐 Перевести"])
        self.data["tts_model"] = "tts"
        labels = [b.text for row in bot.answer_keyboard(self.data, 1, "ru", LONG).inline_keyboard for b in row]
        self.assertIn("🔊 Озвучить", labels)

    async def test_shorter_asks_the_model_about_that_exact_answer(self):
        token = self.token(bot.answer_keyboard(self.data, 1, "ru", LONG))
        query = NS(data=f"ans:s:{token}", answer=AsyncMock(), message=NS(chat_id=1))
        with patch.object(bot, "respond", AsyncMock()) as respond:
            await bot.answer_button(self.update(), self.context, query)
        target, content, saved = respond.call_args.args[2:5]
        self.assertIs(target, query.message)
        self.assertIn("короче", content.lower())
        self.assertIn(LONG.strip(), content)
        self.assertEqual(saved, "[Короче]")

    async def test_translation_goes_to_the_other_language(self):
        token = self.token(bot.answer_keyboard(self.data, 1, "en", LONG))
        query = NS(data=f"ans:t:{token}", answer=AsyncMock(), message=NS(chat_id=1))
        with patch.object(bot, "respond", AsyncMock()) as respond:
            await bot.answer_button(self.update(code="en"), self.context, query)
        self.assertIn("into Russian", respond.call_args.args[3])

    async def test_someone_else_or_an_old_answer_gets_nothing(self):
        token = self.token(bot.answer_keyboard(self.data, 1, "ru", LONG))
        for user, data in ((2, f"ans:s:{token}"), (1, "ans:s:missing")):
            query = NS(data=data, answer=AsyncMock(), message=NS(chat_id=1))
            with patch.object(bot, "respond", AsyncMock()) as respond:
                await bot.answer_button(self.update(user_id=user), self.context, query)
            respond.assert_not_called()
            self.assertIn("старый", query.answer.call_args.args[0])

    async def test_read_aloud_needs_a_voice(self):
        token = self.token(bot.answer_keyboard(self.data, 1, "ru", LONG))
        query = NS(data=f"ans:v:{token}", answer=AsyncMock(), message=NS(chat_id=1))
        await bot.answer_button(self.update(), self.context, query)
        self.assertIn("не настроена", query.answer.call_args.args[0])
        self.data["tts_model"] = "tts"
        with patch.object(bot, "voice_reply", AsyncMock()) as voice:
            await bot.answer_button(self.update(), self.context, query)
            await asyncio.gather(*self.data["voice_tasks"])
        self.assertEqual(voice.call_args.args[3], LONG)


if __name__ == "__main__":
    unittest.main()
