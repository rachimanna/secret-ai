"""Run with python -m unittest discover -s tests."""
import asyncio
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import BadRequestError

import bot

SONNET = 'anymodel:anthropic/claude-sonnet-4-6'
HAIKU = 'anymodel:claude-haiku-5-5'
OPUS = 'anymodel:claude-opus-5-5'
OPUS46 = 'anymodel:claude-opus-4-6'
IDS = [m.split(':', 1)[1] for m in (OPUS46, SONNET, HAIKU, OPUS)]


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
    async def test_paused_catalog_clears_old_routes_and_selections(self):
        clients = {name: gateway(IDS) for name in ('anymodel_chat', 'abacus', 'experiential', 'reformboss')}
        data = dict(**clients, connections=Store(default_model=OPUS46),
                    models=[OPUS46], model=OPUS46, chat_default=OPUS46,
                    fallbacks=[SONNET], cursor_models=['cursor:grok-4.7'], premium={OPUS: 5},
                    memory=NS(model=lambda uid: OPUS46))
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['models'], [])
        self.assertEqual(data['fallbacks'], [])
        self.assertEqual(data['cursor_models'], [])
        self.assertEqual(data['premium'], {})
        self.assertIsNone(bot.user_model(data, 1))
        self.assertIsNone(bot.codex_model(data, OPUS46))
        for client in clients.values():
            client.models.list.assert_not_called()
        await bot.discover_models(NS(bot_data={}))

    async def test_new_gateway_routes_chat_completions(self):
        self.assertEqual(bot.EXTRA_PROVIDERS['reformboss'][0], 'https://api.reformboss.com/v1')
        model = 'reformboss:chosen-model'
        create = AsyncMock(return_value=NS(choices=[NS(message=NS(content='Привет'))]))
        data = dict(reformboss=NS(chat=NS(completions=NS(create=create))))
        self.assertEqual(await bot.quick_answer_once(data, model, 'Be helpful', 'Привет', 2000), 'Привет')
        self.assertEqual(create.call_args.kwargs['model'], 'chosen-model')

    async def test_effort_compatibility_is_remembered_and_bounded(self):
        create = AsyncMock(side_effect=[bad_request('unsupported reasoning_effort minimal'),
                                       bad_request('unsupported reasoning_effort low'), 'stream', 'next stream'])
        client = NS(chat=NS(completions=NS(create=create)))
        data = {'chat_reasoning': 'minimal'}
        result = await bot.chat_completion(data, SONNET, client, model=IDS[0], stream=True)
        self.assertEqual(result, 'stream')
        self.assertEqual([c.kwargs.get('reasoning_effort') for c in create.call_args_list],
                         ['minimal', 'low', None])
        await bot.chat_completion(data, SONNET, client, model=IDS[0], stream=True)
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
        data = dict(models=[SONNET], memory=NS(persona=lambda uid: next(iter(bot.PERSONAS))), pending={},
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
