import json
import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telegram import Update
import bot
import reply_context as replies


def message(text='короче', target='Первый вариант, который нужно сократить.', quote=None):
    parent = NS(text=target, caption=None, document=None, message_id=42)
    return NS(text=text, caption=None, document=None, photo=[], reply_to_message=parent,
              quote=NS(text=quote) if quote else None, voice=None, audio=None, video_note=None)


class ReplyContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_reply_overrides_active_project_and_last_document(self):
        msg = message()
        memory = Mock()
        update = NS(effective_message=msg)
        parsed = await bot.read_question(update, NS(bot_data={'memory': memory}))
        self.assertIsInstance(parsed[1], replies.Prompt)
        data = json.loads(parsed[1])
        self.assertEqual(data['request'], 'короче')
        self.assertEqual(data['target'], msg.reply_to_message.text)
        self.assertEqual(data['message_id'], 42)
        self.assertEqual(memory.mock_calls, [])
        self.assertIn(msg.reply_to_message.text, parsed[2])

    async def test_quote_is_only_selected_target_with_unicode(self):
        update = Update.de_json({'update_id': 1, 'message': {
            'message_id': 5, 'date': 1, 'chat': {'id': 1, 'type': 'private'},
            'from': {'id': 1, 'is_bot': False, 'first_name': 'User'}, 'text': 'исправь',
            'quote': {'text': 'Ошибкa 😀', 'position': 8, 'is_manual': True},
            'reply_to_message': {'message_id': 4, 'date': 1, 'chat': {'id': 1, 'type': 'private'},
                                 'text': 'До 😀. Ошибкa 😀. Другой абзац.'}}}, None)
        parsed = await bot.read_question(update, NS(bot_data={}))
        data = json.loads(parsed[1])
        self.assertEqual(data['target'], 'Ошибкa 😀')
        self.assertEqual(data['selection'], 'quote')
        self.assertNotIn('Другой абзац', parsed[1])

    async def test_quote_available_without_full_parent_message(self):
        msg = message(quote='Только доступная цитата')
        msg.reply_to_message = None
        result = await bot.read_question(NS(effective_message=msg), NS(bot_data={}))
        self.assertEqual(json.loads(result[1])['target'], 'Только доступная цитата')

    async def test_unavailable_reply_does_not_substitute_last_file(self):
        msg = message(target=None)
        memory = Mock()
        parsed = await bot.read_question(NS(effective_message=msg), NS(bot_data={'memory': memory}))
        self.assertIsNone(json.loads(parsed[1])['target'])
        self.assertEqual(memory.mock_calls, [])

    def test_caption_and_document_quotes(self):
        msg = message(target=None)
        msg.reply_to_message.caption = 'Подпись фотографии'
        self.assertEqual(replies.target(msg)[0], 'Подпись фотографии')
        msg.reply_to_message.document = NS(mime_type='text/plain')
        self.assertIsNone(replies.target(msg))
        msg.quote = NS(text='Абзац из подписи документа')
        self.assertEqual(replies.target(msg)[0], 'Абзац из подписи документа')

    async def test_reply_to_document_preserves_file_reader(self):
        msg = message()
        doc = NS(mime_type='text/plain')
        msg.reply_to_message.document = doc
        with patch.object(bot, 'document_question', AsyncMock(return_value=('q', 'file text', 'saved'))) as read:
            result = await bot.read_question(NS(effective_message=msg), NS())
        self.assertEqual(result[1], 'file text')
        self.assertIs(read.call_args.args[2], doc)

    async def test_voice_instruction_can_target_replied_text(self):
        msg = message(text=None)
        msg.voice = NS()
        status = NS(delete=AsyncMock())
        update = NS(effective_message=msg, effective_user=NS(language_code='ru'))
        with patch.object(bot, 'reply', AsyncMock(return_value=status)), \
             patch.object(bot, 'media_text', AsyncMock(return_value='переведи на немецкий')):
            parsed = await bot.read_question(update, NS(bot_data={'memory': NS(workbench=NS()), 'stt': NS()}))
        self.assertEqual(json.loads(parsed[1])['request'], 'переведи на немецкий')
        self.assertEqual(json.loads(parsed[1])['target'], msg.reply_to_message.text)

    async def test_private_generation_uses_target_without_history_apps_or_tools(self):
        model = 'anymodel:xai/grok-4.7'
        memory = NS(persona=lambda uid: 'assistant', model=lambda uid: None,
                    history=Mock(side_effect=AssertionError('Unrelated history accessed')),
                    recall=Mock(side_effect=AssertionError('Unrelated recall accessed')), append_turn=Mock())
        data = dict(models=[model], model=model, chat_default=model, fallbacks=[model], memory=memory,
                    pending={}, http=NS(), connections=NS(), last_answer={})
        live = NS(text='', halted=None, finish=AsyncMock(return_value=NS(message_id=3)), abort=AsyncMock())
        tools = NS(mcp=[], instructions=Mock(return_value='App tool instructions'), proposals=[])
        generate = AsyncMock(return_value=('Краткий первый вариант.', False))
        update = NS(effective_user=NS(id=1, language_code='ru'))
        _, content, saved = replies.prepare(message(), 'короче')
        with ExitStack() as stack:
            stack.enter_context(patch.object(bot, 'open_reply', AsyncMock(return_value=live)))
            stack.enter_context(patch.object(bot, 'Toolbox', return_value=tools))
            stack.enter_context(patch.object(bot, 'generate', generate))
            apps = stack.enter_context(patch.object(bot, 'wants_apps', Mock(side_effect=AssertionError('Apps lookup'))))
            for name in ('memory_tool', 'secretary_tool', 'edit_tool', 'cancel_tool'):
                stack.enter_context(patch.object(bot, name, Mock()))
            await bot.respond(update, NS(bot_data=data), NS(), content, saved)
        request = generate.call_args.kwargs
        self.assertEqual(request['input'], [{'role': 'user', 'content': content}])
        self.assertIn(replies.RULES, request['instructions'])
        self.assertTrue(request['light'])
        self.assertIsNone(generate.call_args.args[2])
        memory.history.assert_not_called()
        memory.recall.assert_not_called()
        apps.assert_not_called()
        tools.instructions.assert_not_called()
        self.assertEqual(memory.append_turn.call_args.args[1], saved)

    async def test_group_generation_omits_private_memory_and_preserves_punctuation(self):
        _, content, _ = replies.prepare(message(), 'исправь')
        live = NS(start=AsyncMock(), finish=AsyncMock(), abort=AsyncMock())
        data = {'tts_model': '', 'groups': NS(add_line=Mock())}
        update = NS(effective_user=NS(id=1))
        msg = NS(chat_id=-1)
        with patch.object(bot, 'GroupReply', return_value=live), \
             patch.object(bot, 'group_model', return_value='anymodel:xai/grok-4.7'), \
             patch.object(bot, 'generate', AsyncMock(return_value=('Исправлено.', False))) as generate, \
             patch.object(bot, 'memory_notes', side_effect=AssertionError('Private memory accessed')), \
             patch.object(bot, 'send_files', AsyncMock()):
            await bot.group_respond(update, NS(bot_data=data), msg, content)
        self.assertIsNone(generate.call_args.args[2])
        self.assertIn(replies.RULES, generate.call_args.kwargs['instructions'])
        self.assertTrue(generate.call_args.kwargs['light'])
        live.finish.assert_awaited_once_with('Исправлено.')

    async def test_group_reply_does_not_prepend_recent_or_archived_messages(self):
        msg = message()
        msg.chat_id, msg.message_id, msg.chat = -1, 50, NS(title='Group')
        msg.sticker = None
        user = NS(id=1, is_bot=False)
        update = NS(effective_message=msg, effective_user=user)
        store = NS(touch=Mock(), add_line=Mock(),
                   lines=Mock(side_effect=AssertionError('Recent history accessed')),
                   search=Mock(side_effect=AssertionError('Archived history accessed')))
        data = dict(allowed=set(), models=['anymodel:xai/grok-4.7'], groups=store,
                    locks={-1: asyncio.Lock()}, group_spoke={})
        parsed = replies.prepare(msg, msg.text)
        with patch.object(bot, 'speaker', return_value='User'), \
             patch.object(bot, 'addressed_to_bot', return_value=True), \
             patch.object(bot, 'user_lock', return_value=asyncio.Lock()), \
             patch.object(bot, 'read_question', AsyncMock(return_value=parsed)), \
             patch.object(bot, 'group_respond', AsyncMock()) as respond:
            await bot.group_chat(update, NS(bot_data=data, bot=NS()))
        self.assertIs(respond.call_args.args[3], parsed[1])
        self.assertIsInstance(respond.call_args.args[3], replies.Prompt)
        store.lines.assert_not_called()
        store.search.assert_not_called()

    def test_nonreply_and_oversize_never_guess_another_target(self):
        msg = message()
        msg.reply_to_message = None
        self.assertIsNone(replies.prepare(msg, 'короче'))
        msg.reply_to_message = NS(text='x' * 16001, document=None)
        self.assertIsNone(json.loads(replies.prepare(msg, 'короче')[1])['target'])

    def test_media_replies_do_not_produce_null_text_target(self):
        msg = message(target=None)
        msg.reply_to_message.photo = [NS(file_id='photo1')]
        self.assertIsNone(replies.target(msg))

        msg_gif = message(target=None)
        msg_gif.reply_to_message.animation = NS(file_id='gif1')
        self.assertIsNone(replies.target(msg_gif))

        msg_stk = message(target=None)
        msg_stk.reply_to_message.sticker = NS(file_id='stk1')
        self.assertIsNone(replies.target(msg_stk))

        msg.quote = NS(text='Цитата из подписи')
        self.assertEqual(replies.target(msg)[0], 'Цитата из подписи')
