"""Run with python -m unittest discover -s tests."""
import asyncio
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import AuthenticationError, BadRequestError, NotFoundError, RateLimitError

import bot

SONNET = 'anymodel:anthropic/claude-sonnet-4-6'
HAIKU = 'anymodel:claude-haiku-5-5'
OPUS = 'anymodel:claude-opus-5-5'
OPUS46 = 'anymodel:claude-opus-4-6'
IDS = [m.split(':', 1)[1] for m in (OPUS46, SONNET, HAIKU, OPUS)]
UNREQUESTED = IDS[1:]  # a catalog without any model the bot offers


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
    async def test_missing_requested_model_clears_old_routes_and_selections(self):
        clients = {name: gateway(UNREQUESTED) for name in ('anymodel_chat', 'abacus', 'experiential', 'reformboss')}
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
            if client is clients["reformboss"] or client is clients["anymodel_chat"]:
                client.models.list.assert_awaited_once()
            else:
                client.models.list.assert_not_called()
        await bot.discover_models(NS(bot_data={}))

    async def test_only_requested_grok_fast_is_enabled_and_becomes_default(self):
        for model_id in ('grok-4.7-fast', 'x-ai/grok-4.7-fast', 'grok-4-7-fast', 'grok-4.7-fast-non-reasoning', 'grok-4.7-fast-reasoning'):
            with self.subTest(model_id=model_id):
                client = gateway([model_id, 'deepseek-v4.1-flash', 'grok-4.7', 'grok-4.6-fast', 'grok-4.70-fast', 'deepseek-v4.1-pro', 'deepseek-v4-flash',
                                  'deepseek-v4.10-flash', 'claude-sonnet-4-6', 'grok-4.7-fast-vision'])
                store = Store(default_model=OPUS46, default_pick='old')
                data = dict(reformboss=client, anymodel_chat=gateway(UNREQUESTED), connections=store,
                            memory=NS(model=lambda uid: OPUS46))
                await bot.discover_models(NS(bot_data=data))
                expected = 'reformboss:' + model_id
                self.assertEqual(data['models'], [expected])
                self.assertEqual(data['fallbacks'], [expected])
                self.assertEqual(data['chat_default'], expected)
                self.assertEqual(store.get_meta('default_model'), expected)
                self.assertEqual(bot.user_model(data, 1), expected)
                data['anymodel_chat'].models.list.assert_awaited_once()

    async def test_opus_46_is_default_and_grok_is_backup(self):
        store = Store(default_model='reformboss:grok-4.7-fast', default_pick='old')
        data = dict(anymodel_chat=gateway(IDS), reformboss=gateway(['grok-4.7-fast']), connections=store,
                    memory=NS(model=lambda uid: None))
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['models'], [OPUS46, 'reformboss:grok-4.7-fast'])
        self.assertEqual(data['chat_default'], OPUS46)
        self.assertEqual(store.get_meta('default_model'), OPUS46)
        self.assertEqual(bot.user_model(data, 1), OPUS46)

    async def test_failed_gateway_catalog_does_not_guess_an_id(self):
        client = gateway([])
        client.models.list.side_effect = RuntimeError('offline')
        data = dict(reformboss=client, anymodel_chat=gateway(['deepseek-v4.1-flash']))
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['models'], [])
        self.assertIsNone(data['chat_default'])
        data['anymodel_chat'].models.list.assert_awaited_once()

    async def test_gateway_outage_after_restart_uses_persisted_confirmed_id(self):
        expected = 'reformboss:grok-4.7-fast'
        store = Store()
        data = dict(reformboss=gateway(['grok-4.7-fast']), connections=store)
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(store.get_meta('confirmed_chat_models:reformboss'), [expected])
        offline = gateway([])
        offline.models.list.side_effect = RuntimeError('temporary outage')
        restarted = dict(reformboss=offline, connections=store)
        await bot.discover_models(NS(bot_data=restarted))
        self.assertEqual(restarted['models'], [expected])
        self.assertEqual(restarted['chat_default'], expected)

    async def test_legacy_confirmed_default_recovers_without_enabling_old_models(self):
        offline = gateway([])
        offline.models.list.side_effect = RuntimeError('offline')
        for saved, wanted in [('reformboss:grok-4.7-fast', ['reformboss:grok-4.7-fast']),
                              (OPUS46, []), ('reformboss:deepseek-v4.1-flash', []), ('reformboss:deepseek-v4.1-pro', [])]:
            with self.subTest(saved=saved):
                data = dict(reformboss=offline, connections=Store(default_model=saved))
                await bot.discover_models(NS(bot_data=data))
                self.assertEqual(data['models'], wanted)
        missing_key = dict(connections=Store(default_model='reformboss:grok-4.7-fast'))
        await bot.discover_models(NS(bot_data=missing_key))
        self.assertEqual(missing_key['models'], [])

    async def test_successful_catalog_removal_is_not_undone_by_stale_default(self):
        store = Store(default_model='reformboss:grok-4.7-fast')
        client = gateway([])
        data = dict(reformboss=client, connections=store)
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(store.get_meta('confirmed_chat_models:reformboss'), [])
        client.models.list.side_effect = RuntimeError('offline')
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['models'], [])

    async def test_concurrent_recovery_is_single_and_throttled_then_succeeds(self):
        client = gateway([])
        client.models.list.side_effect = RuntimeError('offline')
        data = dict(reformboss=client, models=[])
        await asyncio.gather(*(bot.refresh_chat_models(data) for _ in range(10)))
        client.models.list.assert_awaited_once()
        client.models.list.side_effect = None
        client.models.list.return_value = NS(data=[NS(id='grok-4.7-fast')])
        data['model_refresh_at'] -= bot.MODEL_RETRY_SECONDS + 1
        self.assertTrue(await bot.refresh_chat_models(data))
        self.assertEqual(client.models.list.await_count, 2)
        self.assertEqual(data['models'], ['reformboss:grok-4.7-fast'])

    async def test_refused_sole_grok_route_is_not_removed_permanently(self):
        model = 'reformboss:grok-4.7-fast'
        for cls, status, text in [(AuthenticationError, 401, 'unauthorized'),
                                   (NotFoundError, 404, 'not found'),
                                   (RateLimitError, 429, 'insufficient_quota')]:
            with self.subTest(error=cls):
                data = dict(models=[model], model=model, chat_default=model, fallbacks=[model])
                response = httpx.Response(status, request=httpx.Request('POST', 'https://provider.test/v1/chat/completions'))
                error = cls(text, response=response, body={'error': text})
                bot.drop_model(data, model, error)
                self.assertEqual(data['models'], [model])
                self.assertEqual(data['chat_default'], model)

    async def test_periodic_loop_rediscovers_model_and_propagates_cancellation(self):
        data = dict(reformboss=gateway(['grok-4.7-fast']), models=[])
        with patch.object(bot.asyncio, 'sleep', AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with self.assertRaises(asyncio.CancelledError):
                await bot.refresh_models_loop(NS(bot_data=data))
        self.assertEqual(data['models'], ['reformboss:grok-4.7-fast'])

    async def test_grok_fast_prefers_plain_then_non_reasoning_and_excludes_deepseek(self):
        ids = ['deepseek-v4.1-flash', 'grok-4.7', 'grok-4.7-fast-reasoning', 'grok-4.7-fast-non-reasoning']
        store = Store(default_model='reformboss:deepseek-v4.1-flash', default_pick='old')
        data = dict(reformboss=gateway(ids), connections=store,
                    memory=NS(model=lambda uid: 'reformboss:deepseek-v4.1-flash'))
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['models'], ['reformboss:grok-4.7-fast-non-reasoning'])
        self.assertEqual(bot.user_model(data, 1), data['models'][0])
        self.assertEqual(store.get_meta('default_model'), data['models'][0])
        data['reformboss'] = gateway(ids + ['grok-4.7-fast'])
        store.set_meta('default_model', None)
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['models'], ['reformboss:grok-4.7-fast'])

    async def test_anymodel_is_preferred_and_reformboss_is_backup(self):
        data = dict(anymodel_chat=gateway(['x-ai/grok-4.7-fast']),
                    reformboss=gateway(['grok-4.7-fast']), connections=Store())
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['models'], ['anymodel:x-ai/grok-4.7-fast'])
        self.assertEqual(data['fallbacks'], ['anymodel:x-ai/grok-4.7-fast', 'reformboss:grok-4.7-fast'])
        data['anymodel_chat'].models.list.side_effect = RuntimeError('offline')
        data['models'] = []
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data['models'], ['anymodel:x-ai/grok-4.7-fast'])
        self.assertEqual(data['connections'].get_meta('confirmed_chat_models:reformboss'), ['reformboss:grok-4.7-fast'])
        self.assertEqual(data['connections'].get_meta('confirmed_chat_models:anymodel_chat'), ['anymodel:x-ai/grok-4.7-fast'])

    async def test_observed_anymodel_catalog_recovers_with_reformboss_unauthorized(self):
        ids = ['gcli/grok-4.7', 'xai/grok-4.6', 'xai/grok-imagine-image',
               'xai/grok-4.7', 'xai/grok-4.20-0309-reasoning']
        failed = gateway([])
        response = httpx.Response(401, request=httpx.Request('GET', 'https://provider.test/models'))
        failed.models.list.side_effect = AuthenticationError('invalid key', response=response, body={})
        store = Store(default_pick='old', default_model='reformboss:grok-4.7-fast')
        data = dict(anymodel_chat=gateway(ids), reformboss=failed, connections=store)
        await bot.discover_models(NS(bot_data=data))
        model = 'anymodel:xai/grok-4.7'
        self.assertEqual(data['models'], [model])
        self.assertEqual(data['fallbacks'], [model])
        self.assertEqual(data['chat_default'], model)
        self.assertEqual(store.get_meta('default_model'), model)
        create = AsyncMock(return_value=NS(choices=[NS(message=NS(content='Привет'))]))
        data['anymodel_chat'].chat = NS(completions=NS(create=create))
        self.assertEqual(await bot.quick_answer_once(data, model, 'Be helpful', 'Привет', 2000), 'Привет')
        self.assertEqual(create.call_args.kwargs['model'], 'xai/grok-4.7')

    async def test_anymodel_works_when_reformboss_client_is_absent(self):
        data = dict(anymodel_chat=gateway(['grok-4.7-fast']))
        self.assertTrue(await bot.refresh_chat_models(data))
        self.assertEqual(data['models'], ['anymodel:grok-4.7-fast'])
        response = httpx.Response(401, request=httpx.Request('POST', 'https://provider.test'))
        bot.drop_model(data, data['models'][0], AuthenticationError('bad key', response=response, body={}))
        self.assertEqual(data['models'], ['anymodel:grok-4.7-fast'])

    def test_diagnostic_errors_hide_exact_key_and_report_http_status(self):
        key = 'a-provider-secret-token-1234'
        response = httpx.Response(401, request=httpx.Request('GET', 'https://provider.test/models'))
        error = AuthenticationError('bad key ' + key, response=response, body={'error': {'message': 'invalid key ' + key}})
        shown = bot.safe_api_error(NS(api_key=key), error)
        self.assertIn('HTTP 401', shown)
        self.assertIn('invalid key', shown)
        self.assertNotIn(key, shown)

    async def test_owner_diagnostic_reports_missing_key_and_catalog_without_secrets(self):
        status = NS(delete=AsyncMock())
        data = dict(connections=Store(owner=1), reformboss=gateway(['grok-4.7', 'deepseek-v4.1-flash']),
                    chat_default=None)
        update = NS(effective_user=NS(id=1), effective_message=NS())
        context = NS(bot_data=data)
        with patch.object(bot, 'authorized', AsyncMock(return_value=True)), \
             patch.object(bot, 'reply', AsyncMock(return_value=status)), \
             patch.object(bot, 'send', AsyncMock()) as send:
            await bot.api_status(update, context)
        shown = send.call_args.args[1]
        self.assertIn('ANYMODEL_API_KEY', shown)
        self.assertIn('точного совпадения нет', shown)
        self.assertIn('grok-4.7', shown)
        status.delete.assert_awaited_once()

    async def test_nonowner_cannot_probe_provider_configuration(self):
        client = gateway(['grok-4.7-fast'])
        data = dict(connections=Store(owner=1), anymodel_chat=client)
        with patch.object(bot, 'authorized', AsyncMock(return_value=True)), patch.object(bot, 'send', AsyncMock()):
            await bot.api_status(NS(effective_user=NS(id=2), effective_message=NS()), NS(bot_data=data))
        client.models.list.assert_not_called()

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
                    github_apps=NS(fresh_token=refresh), http=NS(), connections=NS(get=lambda *a: [], public=lambda *a: [], get_meta=lambda *a: None))
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
