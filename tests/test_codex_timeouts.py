import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import APITimeoutError

import bot

MODEL = 'anymodel:gemini-3-flash-preview'


def timeout():
    return APITimeoutError(request=httpx.Request('POST', 'https://provider.test/v1/chat/completions'))


def chunk(content=None, calls=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=calls or []), finish_reason=finish)])


async def stalled():
    yield NS(choices=[])  # metadata/keepalive is not a partial answer
    raise timeout()


async def text_stream():
    yield chunk('Project overview', finish='stop')


class CodexTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_tool_result_does_not_reexecute_tool(self):
        async def tool_stream():
            yield chunk(calls=[NS(index=0, id='read_1', function=NS(name='read', arguments='{"path":"README.md"}'))],
                        finish='tool_calls')
        create = AsyncMock(side_effect=[tool_stream(), stalled(), text_stream()])
        data = dict(anymodel_chat=NS(chat=NS(completions=NS(create=create))))
        tools = NS(definitions=lambda: [{'type': 'function', 'name': 'read'}],
                   status=lambda *args: 'Reading files', run=AsyncMock(return_value='Repository README'))
        live = NS(language='en', update=AsyncMock(), status=Mock())
        result = await bot.chat_answer(NS(bot_data=data), live,
            dict(model=MODEL, input=[{'role': 'user', 'content': 'Explain this repository'}],
                 api_timeout=180, stream_retries=1), tools)
        self.assertEqual(result, ('Project overview', False))
        tools.run.assert_awaited_once_with('read', {'path': 'README.md'})
        self.assertEqual(create.call_count, 3)
        self.assertEqual(create.call_args.kwargs['timeout'].read, 180)
        messages = create.call_args.kwargs['messages']
        self.assertEqual([m['content'] for m in messages if m['role'] == 'tool'], ['Repository README'])
        self.assertTrue(any('retrying' in str(c).lower() for c in live.status.call_args_list))

    async def test_timeout_before_headers_retries_once_and_stays_bounded(self):
        create = AsyncMock(side_effect=[timeout(), text_stream()])
        client = NS(chat=NS(completions=NS(create=create)))
        result = [c async for c in bot.chat_stream({}, MODEL, client, retries=1, model='gemini-3-flash-preview')]
        self.assertEqual(len(result), 1)
        self.assertEqual(create.call_count, 2)
        create.reset_mock()
        create.side_effect = timeout()
        with self.assertRaises(APITimeoutError):
            _ = [c async for c in bot.chat_stream({}, MODEL, client, retries=1, model='gemini-3-flash-preview')]
        self.assertEqual(create.call_count, 2)
        create.reset_mock()
        with self.assertRaises(APITimeoutError):
            _ = [c async for c in bot.chat_stream({}, MODEL, client, model='gemini-3-flash-preview')]
        self.assertEqual(create.call_count, 1)  # ordinary chat has no additional retry

    async def test_partial_text_or_tool_call_is_not_replayed(self):
        for first in [chunk('Partial answer'), chunk(calls=[NS(index=0)])]:
            async def partial():
                yield first
                raise timeout()
            create = AsyncMock(side_effect=lambda **kwargs: partial())
            client = NS(chat=NS(completions=NS(create=create)))
            with self.assertRaises(APITimeoutError):
                _ = [c async for c in bot.chat_stream({}, MODEL, client, retries=1, model='gemini-3-flash-preview')]
            self.assertEqual(create.call_count, 1)

    async def test_codex_passes_longer_timeout_and_reports_failure_clearly(self):
        task = dict(id='task', thread='thread', created=1, user_id=1, mode='repo',
                    repo='owner/repo', prompt='Explain this repository', model=MODEL)
        store = NS(get=lambda tid: task, thread=lambda *args: [], update=Mock())
        data = dict(codex=store, codex_jobs={}, github_apps=NS(fresh_token=AsyncMock()),
                    http=NS(), connections=NS(), pending={})
        app = NS(bot_data=data, bot=NS(send_message=AsyncMock()))
        generate = AsyncMock(side_effect=timeout())
        with patch.object(bot, 'codex_can_run', return_value=False), \
             patch.object(bot.codex, 'TaskLive', return_value=NS(status=Mock())), \
             patch.object(bot, 'Toolbox'), \
             patch.object(bot, 'RepoTools', return_value=NS(proposals=[])), \
             patch.object(bot, 'generate', generate):
            await bot.run_codex(app, 'task', 'ru')
        self.assertEqual(generate.call_args.kwargs['api_timeout'], 180)
        self.assertEqual(generate.call_args.kwargs['stream_retries'], 1)
        self.assertEqual(store.update.call_args.kwargs['status'], bot.codex.STATUS_FAILED)
        self.assertIn('не ответила вовремя', store.update.call_args.kwargs['error'])
        app.bot.send_message.assert_not_called()
