import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import inline_ai

VALUES = {'translate': 'Hello', 'correct': 'Привет', 'shorten': 'Привет!', 'reply': 'Привет, как дела?'}


class InlineTest(unittest.IsolatedAsyncioTestCase):
    def setup_case(self, text='Привет', user=1, **extra):
        api = NS(quick_answer=AsyncMock(return_value=json.dumps(VALUES)), test_mode=Mock(return_value=False))
        assistant = inline_ai.InlineAssistant(api)
        query = NS(id='query-1', from_user=NS(id=user), query=text, offset='', answer=AsyncMock())
        context = NS(bot_data=dict(chat_default='anymodel:xai/grok-4.7', **extra))
        return assistant, api, query, context

    async def run_query(self, assistant, query, context):
        with patch.object(inline_ai.asyncio, 'sleep', AsyncMock()):
            await assistant.handle(NS(inline_query=query), context)

    async def test_four_results_are_plain_text_and_use_no_private_context(self):
        a, api, q, c = self.setup_case(memory=Mock(), connections=Mock())
        await self.run_query(a, q, c)
        results = q.answer.call_args.args[0]
        self.assertEqual(len(results), 4)
        self.assertEqual(results[0].input_message_content.message_text, 'Hello')
        self.assertEqual(results[0].input_message_content.parse_mode, '')
        self.assertTrue(q.answer.call_args.kwargs['is_personal'])
        self.assertEqual(q.answer.call_args.kwargs['cache_time'], 0)
        self.assertEqual(api.quick_answer.call_args.args[3], 'Привет')
        self.assertEqual(c.bot_data['memory'].mock_calls, [])
        self.assertEqual(c.bot_data['connections'].mock_calls, [])

    async def test_explicit_mode_uses_same_cached_generation(self):
        a, api, q, c = self.setup_case()
        await self.run_query(a, q, c)
        q.id, q.query = 'query-2', 'исправить Привет'
        await self.run_query(a, q, c)
        api.quick_answer.assert_awaited_once()
        results = q.answer.call_args.args[0]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].input_message_content.message_text, 'Привет')

    async def test_cache_is_scoped_to_user(self):
        a, api, q, c = self.setup_case()
        await self.run_query(a, q, c)
        q.id, q.from_user = 'query-2', NS(id=2)
        await self.run_query(a, q, c)
        self.assertEqual(api.quick_answer.await_count, 2)

    async def test_restricted_empty_long_and_pagination_never_call_model(self):
        for text, allowed, offset in [('secret', {2}, ''), ('', set(), ''), ('a' * 1501, set(), ''), ('abc', set(), 'next')]:
            a, api, q, c = self.setup_case(text, allowed=allowed)
            q.offset = offset
            await self.run_query(a, q, c)
            api.quick_answer.assert_not_called()
            self.assertEqual(q.answer.call_args.args[0], [])

    async def test_failure_does_not_publish_provider_secrets(self):
        for failure in [RuntimeError('sk-secret'), TimeoutError(), ValueError()]:
            a, api, q, c = self.setup_case()
            api.quick_answer.side_effect = failure
            await self.run_query(a, q, c)
            self.assertEqual(q.answer.call_args.args[0], [])
            self.assertNotIn('sk-secret', str(q.answer.call_args))
            self.assertFalse(a.running)

    async def test_malformed_output_is_not_sent(self):
        a, api, q, c = self.setup_case()
        api.quick_answer.return_value = 'I cannot return JSON'
        await self.run_query(a, q, c)
        self.assertEqual(q.answer.call_args.args[0], [])

    async def test_rate_limit_blocks_new_text(self):
        a, api, q, c = self.setup_case()
        await self.run_query(a, q, c)
        q.id, q.query = 'query-2', 'Другой текст'
        await self.run_query(a, q, c)
        api.quick_answer.assert_awaited_once()
        self.assertEqual(q.answer.call_args.args[0], [])

    async def test_newer_query_supersedes_older(self):
        a, api, q, c = self.setup_case()
        async def supersede(_):
            a.latest[1] = 'newer'
        with patch.object(inline_ai.asyncio, 'sleep', supersede):
            await a.handle(NS(inline_query=q), c)
        api.quick_answer.assert_not_called()

    def test_invalid_fields_and_oversized_results_are_removed(self):
        self.assertEqual(inline_ai.parse_result('{"reply": 123, "correct": "", "secret":"no", "shorten":"ok"}'), {'shorten': 'ok'})
        self.assertEqual(inline_ai.parse_result(json.dumps({'reply': 'x' * 3501})), {})
