import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot


class InviteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)

    def credits(self, user_id):
        return self.memory.gen_info(user_id)[1]

    def test_a_new_person_brings_credits_to_both_once(self):
        self.assertTrue(self.memory.add_referral(2, 1, 3))
        self.assertEqual((self.credits(1), self.credits(2)), (3, 3))
        self.assertFalse(self.memory.add_referral(2, 1, 3))  # only once
        self.assertFalse(self.memory.add_referral(2, 5, 3))  # nor by someone else
        self.assertEqual(self.memory.referral_count(1), 1)

    def test_oneself_and_old_users_do_not_count(self):
        self.assertFalse(self.memory.add_referral(1, 1, 3))
        self.memory.append_turn(4, "привет", "привет!")
        self.assertFalse(self.memory.add_referral(4, 1, 3))
        self.assertEqual(self.credits(1), 0)

    def test_daily_limit_per_referrer(self):
        for user in range(100, 100 + bot.REFERRALS_PER_DAY):
            self.assertTrue(self.memory.add_referral(user, 1, 1))
        self.assertFalse(self.memory.add_referral(999, 1, 1))

    async def test_start_with_a_link_welcomes_and_tells_the_friend(self):
        store = NS(get_meta=lambda name, *a, **k: None)
        tg = NS(send_message=AsyncMock())
        context = NS(bot_data={"memory": self.memory, "connections": store}, bot=tg, args=["ref_1"])
        message = NS(reply_text=AsyncMock())
        update = NS(effective_user=NS(id=2, language_code="ru"), effective_message=message)
        with patch.object(bot, "authorized", AsyncMock(return_value=True)), \
                patch.object(bot, "send_welcome", AsyncMock()):
            await bot.start(update, context)
        self.assertIn("пригласил друг", message.reply_text.call_args_list[0].args[0])
        self.assertEqual(tg.send_message.call_args.args[0], 1)
        self.assertEqual(self.credits(2), bot.REFERRAL_CREDITS)

    async def test_invite_command_shows_the_link_and_a_share_button(self):
        context = NS(bot_data={"memory": self.memory, "bot_name": "secret_ai_bot"})
        message = NS(reply_text=AsyncMock())
        update = NS(effective_user=NS(id=7, language_code="ru"), effective_message=message)
        with patch.object(bot, "authorized", AsyncMock(return_value=True)):
            await bot.invite_command(update, context)
        text = message.reply_text.call_args.args[0]
        self.assertIn("https://t.me/secret_ai_bot?start=ref_7", text)
        button = message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertTrue(button.url.startswith("https://t.me/share/url?"))


if __name__ == "__main__":
    unittest.main()
