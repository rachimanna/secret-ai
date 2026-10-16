import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardButton, MessageEntity

import bot


class ReplyEmojiTest(unittest.IsolatedAsyncioTestCase):
    def test_decorative_emoji_are_removed_without_breaking_words(self):
        text = 'Че так ❓ — в смысле 🤖 почему 🌐 скучно 🐺?\n\nДавай 🤝 выберем ➡️ другое 💯!'
        self.assertEqual(bot.clean_chat_text(text), 'Че так — в смысле почему скучно?\n\nДавай выберем другое!')
        self.assertEqual(bot.clean_chat_text('Привет🌍мир! 👨‍👩‍👧‍👦 👍🏽 🇩🇪 1️⃣'), 'Привет мир!')
        self.assertEqual(bot.clean_chat_text('x + y = 2; 10% → 20%; № 7'), 'x + y = 2; 10% → 20%; № 7')

    def test_code_urls_and_incomplete_streamed_code_stay_intact(self):
        code = '```python\n    print("👩🏽‍💻")\n```'
        text = 'Код 🚀:\n' + code + '\nИ `"🔥"` 😀 тоже.'
        self.assertEqual(bot.clean_chat_text(text), 'Код:\n' + code + '\nИ `"🔥"` тоже.')
        unfinished = '```python\n    print("👩🏽‍💻'
        self.assertEqual(bot.clean_chat_text(unfinished), unfinished)
        self.assertEqual(bot.clean_chat_text('Ссылка 🌍 https://example.test/🌍'), 'Ссылка https://example.test/🌍')

    async def test_stream_and_final_send_use_the_same_filtered_text(self):
        target = NS(reply_text=AsyncMock(return_value=NS(message_id=1)))
        underlying = bot.SilentReply(target)
        live = bot.CleanChatReply(underlying)
        with patch.dict(bot.PREMIUM_EMOJI, {' ': '100', '?': '101', '🌍': '102'}, clear=True):
            await live.update('Привет 🌍')
            self.assertEqual(live.text, 'Привет')
            await live.update('Привет 🌍! Как 🤖 дела?')
            self.assertEqual(live.text, 'Привет! Как дела?')
            await live.finish('Привет 🌍! Как 🤖 дела?')
        self.assertEqual(target.reply_text.call_args.args[0], 'Привет! Как дела?')
        self.assertIsNone(target.reply_text.call_args.kwargs['entities'])

    async def test_telegram_draft_filters_before_chunk_offsets_are_calculated(self):
        api = NS(send_message_draft=AsyncMock())
        target = NS(chat_id=1, get_bot=lambda: api,
                    reply_text=AsyncMock(return_value=NS(message_id=1)))
        draft = bot.DraftReply(target, 'ru', {'logo': '✨'})
        live = bot.CleanChatReply(draft)
        raw = 'Привет 🤖! ' * 600
        expected = bot.clean_chat_text(raw)
        await live.update(raw)
        await draft._tick()
        self.assertGreater(draft.offset, 0)
        draft.closed = True  # no background tasks in this test
        await live.finish(raw)
        sent = ''.join(c.args[0] for c in target.reply_text.call_args_list)
        self.assertEqual(sent, expected)
        self.assertTrue(all('🤖' not in c.args[2] for c in api.send_message_draft.call_args_list))

    async def test_custom_emoji_learning_rejects_letters_spaces_and_punctuation(self):
        entities = [NS(type=MessageEntity.CUSTOM_EMOJI, custom_emoji_id=str(i), plain=p)
                    for i, p in enumerate([' ', '?', 'а', 'word', '✨', '👨‍👩‍👧‍👦'])]
        message = NS(entities=entities, caption_entities=[], text='test', parse_entity=lambda e: e.plain)
        saved = []
        context = NS(bot_data={'connections': NS(set_meta=lambda *args: saved.append(args))})
        with patch.dict(bot.PREMIUM_EMOJI, {}, clear=True):
            self.assertEqual(bot.learn_premium(context, message), ['✨', '👨‍👩‍👧‍👦'])
            self.assertEqual(set(bot.PREMIUM_EMOJI), {'✨', '👨‍👩‍👧‍👦'})
        self.assertEqual(len(saved), 1)

    def test_old_invalid_mapping_cannot_replace_ordinary_text_or_button_label(self):
        with patch.dict(bot.PREMIUM_EMOJI, {' ': '100', '?': '101', 'а': '102', '✨': '103'}, clear=True):
            self.assertEqual(bot.premium_entities('Как дела?'), [])
            button = InlineKeyboardButton('а главное меню', callback_data='menu')
            self.assertIs(bot.premium_button(button), button)
            entities = bot.premium_entities('А ✨?')
            self.assertEqual(len(entities), 1)
            self.assertEqual((entities[0].offset, entities[0].length, entities[0].custom_emoji_id), (2, 1, '103'))
            icon = bot.premium_button(InlineKeyboardButton('✨ Меню', callback_data='menu'))
            self.assertEqual(icon.icon_custom_emoji_id, '103')
