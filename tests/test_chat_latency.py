import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import bot

MODEL = 'anymodel:claude-sonnet-4-6'


class ChatLatencyTest(unittest.IsolatedAsyncioTestCase):
    def test_only_standalone_greetings_get_the_short_path(self):
        for text in ['Привет как дела?', 'Привет! Как у тебя дела?', 'Салам 👋', 'Hello, how are you?', 'Как дела?']:
            self.assertTrue(bot.is_greeting(text), text)
        for text in ['Привет, напиши код', 'Привет, переведи письмо', 'Привет! Почему болит голова?', 'ещё', 'да', '']:
            self.assertFalse(bot.is_greeting(text), text)

    def test_greeting_history_limit_does_not_delete_older_messages(self):
        memory = bot.Memory(':memory:')
        try:
            for i in range(20):
                memory.append_turn(1, f'Question {i}', f'Answer {i}')
            short = memory.history(1, limit=6)
            self.assertEqual(len(short), 6)
            self.assertEqual(short[0]['content'], 'Question 17')
            self.assertEqual(len(memory.history(1)), 40)
        finally:
            memory.db.close()

    async def test_available_text_is_shown_in_first_draft_tick_without_finish_delay(self):
        api = NS(send_message_draft=AsyncMock())
        target = NS(chat_id=1, get_bot=lambda: api, reply_text=AsyncMock(return_value=NS(message_id=1)))
        draft = bot.DraftReply(target, 'ru', {'logo': '✨'})
        text = 'Привет! У меня всё хорошо. Как твои дела? ' * 8
        await draft.update(text)
        await draft._tick()
        self.assertEqual(api.send_message_draft.call_args.args[2], text)
        # No artificial sleep/typewriter catch-up after the model has finished.
        with patch.object(bot.asyncio, 'sleep', AsyncMock(side_effect=AssertionError('Artificial delay'))):
            await draft.finish(text)
        self.assertEqual(target.reply_text.call_args.args[0], text)

    async def run_prompt(self, prompt, persona='assistant'):
        memory = NS(persona=lambda uid: persona, model=lambda uid: None,
                    history=Mock(return_value=[{'id': 1, 'role': 'user', 'content': 'Ранее я писал по-русски'},
                                               {'id': 2, 'role': 'assistant', 'content': 'Хорошо'}]),
                    recall=Mock(return_value=[]), append_turn=Mock())
        data = dict(models=[MODEL], model=MODEL, chat_default=MODEL, fallbacks=[MODEL],
                    memory=memory, pending={}, http=NS(), connections=NS(), last_answer={})
        live = NS(text='', halted=None, finish=AsyncMock(return_value=NS(message_id=1)), abort=AsyncMock())
        tools = NS(mcp=[], instructions=Mock(return_value='Tool instructions'), proposals=[])
        generate = AsyncMock(return_value=('Привет! Как ты?', False))
        update = NS(effective_user=NS(id=1, language_code='ru'))
        with ExitStack() as stack:
            stack.enter_context(patch.object(bot, 'open_reply', AsyncMock(return_value=live)))
            stack.enter_context(patch.object(bot, 'Toolbox', return_value=tools))
            stack.enter_context(patch.object(bot, 'generate', generate))
            for name in ('memory_tool', 'secretary_tool', 'edit_tool', 'cancel_tool'):
                stack.enter_context(patch.object(bot, name, Mock()))
            await bot.respond(update, NS(bot_data=data), NS(), prompt, prompt)
        return memory, tools, generate.call_args.kwargs

    async def test_greeting_request_keeps_recent_context_but_skips_archive_and_unrelated_rules(self):
        memory, tools, request = await self.run_prompt('Привет как дела?')
        memory.history.assert_called_once_with(1, limit=6)
        memory.recall.assert_not_called()
        tools.instructions.assert_not_called()
        self.assertNotIn(bot.TRANSLATION_RULES, request['instructions'])
        self.assertNotIn(bot.CODE_RULES, request['instructions'])
        self.assertEqual(len(request['input']), 3)
        self.assertEqual(request['model'], MODEL)
        self.assertEqual(request['max_output_tokens'], 1024)
        memory.append_turn.assert_called_once()

    async def test_substantive_requests_and_translator_keep_full_rules_and_recall(self):
        for prompt, persona in [('Привет, объясни SQL', 'assistant'), ('Привет как дела?', 'translator')]:
            memory, _, request = await self.run_prompt(prompt, persona)
            memory.history.assert_called_once_with(1)
            memory.recall.assert_called_once()
            self.assertIn(bot.TRANSLATION_RULES, request['instructions'])
            self.assertIn(bot.CODE_RULES, request['instructions'])
            self.assertEqual(request['max_output_tokens'], 32000)
