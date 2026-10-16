"""Run with python -m unittest discover -s tests."""
import asyncio
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import BadRequestError

import bot

GEMINI = 'anymodel:google/gemini-3-flash-preview'
HAIKU = 'anymodel:claude-haiku-5-5'
OPUS = 'anymodel:claude-opus-5-5'
IDS = [m.split(':', 1)[1] for m in (GEMINI, HAIKU, OPUS)]


class Store:
    def __init__(self, **values):
        self.values = values

    def get_meta(self, key):
        return self.values.get(key)

    def set_meta(self, key, value):
        self.values[key] = value


def gateway(ids):
    return NS(models=NS(list=AsyncMock(return_value=NS(data=[NS(id=i) for i in ids]))))


def bad_request(message):
    response = httpx.Response(400, request=httpx.Request('POST', 'https://provider.test/v1/chat/completions'))
    return BadRequestError(message, response=response, body={'error': message})


class ChatModelsTest(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_default_migration_and_old_selections(self):
        forbidden = ['claude-sonnet-5-5', 'route-llm', 'gpt-6.1-sol', 'gemini-3.1-flash-lite',
                     'gemini-3-flash-image', 'gemini-3.8-pro', 'claude-opus-4-6', 'claude-haiku-5-50']
        store = Store(default_model='anymodel:claude-sonnet-5-5', default_pick='old')
        data = dict(anymodel_chat=gateway(IDS + forbidden + ['cc/claude-opus-5-5']),
                    abacus=gateway(IDS + forbidden), connections=store,
                    model='gpt-6.1-sol', cursor_models=['cursor:grok-4.7'],
                    memory=NS(model=lambda uid: 'anymodel:claude-sonnet-5-5' if uid == 1 else OPUS))
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['models'], [GEMINI, HAIKU, OPUS])
        self.assertEqual(data['chat_default'], GEMINI)
        self.assertEqual(store.get_meta('default_model'), GEMINI)
        self.assertEqual(data['cursor_models'], [])
        self.assertEqual(len(data['fallbacks']), 6)
        self.assertTrue(all(any(re.search(p, m) for _, p in bot.MODEL_CATALOG)
                            for m in data['fallbacks']))
        self.assertEqual(bot.user_model(data, 1), GEMINI)
        self.assertEqual(bot.user_model(data, 2), OPUS)
        self.assertEqual(bot.codex_model(data, 'anymodel:claude-sonnet-5-5'), GEMINI)

    async def test_plain_gemini_and_missing_gemini_then_recovery(self):
        store = Store(default_model='anymodel:claude-sonnet-5-5')
        data = dict(anymodel_chat=gateway(['claude-haiku-5-5']), connections=store)
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['chat_default'], HAIKU)
        data['anymodel_chat'] = gateway(['claude-haiku-5-5', 'gemini-3-flash-preview'])
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['chat_default'], 'anymodel:gemini-3-flash-preview')
        self.assertEqual(store.get_meta('default_pick'), bot.DEFAULT_PICK)

    async def test_failed_catalog_does_not_guess_or_enable_other_providers(self):
        failed = gateway([])
        failed.models.list.side_effect = RuntimeError('offline')
        data = dict(anymodel_chat=failed, abacus=gateway(['claude-opus-5.5']),
                    groq=gateway(['llama-4']), client=gateway(['gpt-6.1-sol']))
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['models'], ['abacus:claude-opus-5.5'])
        data['groq'].models.list.assert_not_called()
        data['client'].models.list.assert_not_called()
        await bot.discover_models(NS(bot_data={}))

    async def test_catalog_requests_run_concurrently(self):
        started = set()
        ready = asyncio.Event()

        async def listing(name):
            started.add(name)
            if len(started) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 1)
            return NS(data=[NS(id='gemini-3-flash-preview')])

        async def any_list():
            return await listing('anymodel_chat')
        async def abacus_list():
            return await listing('abacus')
        data = dict(anymodel_chat=NS(models=NS(list=AsyncMock(side_effect=any_list))),
                    abacus=NS(models=NS(list=AsyncMock(side_effect=abacus_list))))
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(len(started), 2)
        self.assertEqual(len(data['models']), 1)

    async def test_effort_compatibility_is_remembered_and_bounded(self):
        create = AsyncMock(side_effect=[bad_request('unsupported reasoning_effort minimal'),
                                       bad_request('unsupported reasoning_effort low'), 'stream', 'next stream'])
        client = NS(chat=NS(completions=NS(create=create)))
        data = {}
        result = await bot.chat_completion(data, GEMINI, client, model=IDS[0], stream=True)
        self.assertEqual(result, 'stream')
        self.assertEqual([c.kwargs.get('reasoning_effort') for c in create.call_args_list],
                         ['minimal', 'low', None])
        await bot.chat_completion(data, GEMINI, client, model=IDS[0], stream=True)
        self.assertNotIn('reasoning_effort', create.call_args.kwargs)
        create.side_effect = bad_request('invalid messages')
        with self.assertRaises(BadRequestError):
            await bot.chat_completion(data, OPUS, client, model=IDS[2])
        self.assertEqual(create.call_count, 5)

    async def test_quick_answers_use_speed_setting_without_shortening_output(self):
        create = AsyncMock(return_value=NS(choices=[NS(message=NS(content='Привет'))]))
        data = dict(anymodel_chat=NS(chat=NS(completions=NS(create=create))))
        self.assertEqual(await bot.quick_answer_once(data, HAIKU, 'Be helpful', 'Привет', 2000), 'Привет')
        self.assertEqual(create.call_args.kwargs['reasoning_effort'], 'low')
        self.assertEqual(create.call_args.kwargs['max_tokens'], 2000)

    async def test_small_talk_does_not_refresh_github_or_load_apps(self):
        class Prepared(Exception):
            pass
        refresh, app_tools = AsyncMock(), AsyncMock()
        data = dict(models=[GEMINI], memory=NS(persona=lambda uid: next(iter(bot.PERSONAS))), pending={},
                    github_apps=NS(fresh_token=refresh), http=NS(), connections=NS())
        update = NS(effective_user=NS(id=1, language_code='ru'))
        with patch.object(bot, 'open_reply', AsyncMock(return_value=NS())), \
             patch.object(bot, 'app_tools', app_tools), \
             patch.object(bot, 'memory_tool', Mock()), \
             patch.object(bot, 'secretary_tool', Mock()), \
             patch.object(bot, 'edit_tool', Mock()), \
             patch.object(bot, 'cancel_tool', Mock()), \
             patch.object(bot, 'Toolbox', side_effect=Prepared):
            with self.assertRaises(Prepared):
                await bot.respond(update, NS(bot_data=data), NS(), 'Привет', 'Привет')
        refresh.assert_not_called()
        app_tools.assert_not_called()

    async def test_stream_handles_both_effort_and_tools_rejection(self):
        async def stream():
            yield NS(choices=[NS(delta=NS(content='Привет', tool_calls=[]), finish_reason='stop')])
        create = AsyncMock(side_effect=[bad_request('unsupported reasoning_effort'),
                                       bad_request('tools not supported'), stream()])
        data = dict(anymodel_chat=NS(chat=NS(completions=NS(create=create))))
        live = NS(update=AsyncMock(), status=lambda value: None)
        tools = NS(definitions=lambda: [{'type': 'function', **bot.WEB_TOOL['function']}])
        answer, incomplete = await bot.chat_answer(NS(bot_data=data), live,
            dict(model=OPUS, input=[{'role': 'user', 'content': 'Привет'}]), tools)
        self.assertEqual(answer, 'Привет')
        self.assertFalse(incomplete)
        live.update.assert_awaited()
        self.assertNotIn('tools', create.call_args.kwargs)
        self.assertNotIn('reasoning_effort', create.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
