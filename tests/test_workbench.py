import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree as ET

import bot
import docreader
import workbench as wb
import workbench_ui


def document(name='notes.txt', text='L1: First\nL2: Second', warnings=None):
    return {'name': name, 'text': text, 'warnings': warnings or []}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.memory = bot.Memory(':memory:')
        self.addCleanup(self.memory.close)
        self.store = self.memory.workbench

    def test_projects_versions_selection_and_scope_isolation(self):
        p = self.store.create(1, 100, 'Мой бот')
        first = self.store.add(1, 100, document())
        self.assertEqual(first['version'], 1)
        self.assertEqual(self.store.add(1, 100, document())['id'], first['id'])
        second = self.store.add(1, 100, document(text='changed'))
        self.assertEqual(second['version'], 2)
        self.assertEqual(len(self.store.files(1, 100)), 2)
        self.assertEqual(len(self.store.files(1, 100, latest=True)), 1)
        self.assertIsNone(self.store.file(2, 100, first['id']))
        self.assertIsNone(self.store.file(1, 200, first['id']))
        with self.assertRaises(wb.WorkspaceError):
            self.store.open(2, 100, p['id'])
        with self.assertRaises(wb.WorkspaceError):
            self.store.use(1, 200, first['id'])
        self.store.use(1, 100, first['id'])
        self.assertEqual(self.memory.document(1, 100)['text'], document()['text'])
        context = self.store.context(1, 100, 'Объясни выбранный файл')
        self.assertNotIn('changed', context)
        self.store.open(1, 100, p['id'])
        self.assertIn('changed', self.store.context(1, 100, 'Объясни проект'))

    def test_search_and_context_use_only_latest_revisions_in_active_project(self):
        p = self.store.create(1, 100, 'Code')
        self.store.add(1, 100, document('a.py', 'old secret_word'))
        a = self.store.add(1, 100, document('a.py', 'def work():\n return budget_total'))
        b = self.store.add(1, 100, document('b.py', 'import a\nclass Budget: pass'))
        other = self.store.create(1, 100, 'Other')
        self.store.add(1, 100, document('other.txt', 'secret_other_project'))
        self.store.open(1, 100, p['id'])
        self.assertFalse(self.store.search(1, 100, 'secret_word'))
        hits = self.store.search(1, 100, 'budget_total')
        self.assertEqual(hits[0]['id'], a['id'])
        content = self.store.context(1, 100, 'Как связаны файлы?')
        for expected in ('a.py', 'b.py', 'budget_total', 'class Budget', docreader.MARKER):
            self.assertIn(expected, content)
        self.assertNotIn('secret_other_project', content)
        self.assertNotIn('old secret_word', content)
        self.assertEqual(self.store.search(2, 100, 'budget'), [])

    def test_context_is_bounded_and_can_retrieve_late_sections(self):
        self.store.create(1, 100, 'Many')
        for i in range(20):
            self.store.add(1, 100, document(f'file{i}.txt', 'filler\n' * 7000 + f'UNIQUE_TARGET_{i}'))
        content = self.store.context(1, 100, 'UNIQUE_TARGET_0')
        self.assertIn('UNIQUE_TARGET_0', content)
        self.assertLess(len(content), wb.CONTEXT_CHARS + 10_000)
        self.assertIn('omitted sections', content)

    def test_compare_counts_insertions_without_line_number_noise(self):
        a = self.store.add(1, 100, document('a.txt', 'L1: first\nL2: last'))
        b = self.store.add(1, 100, document('a.txt', 'L1: first\nL2: middle\nL3: last'))
        result = self.store.compare(1, 100)
        self.assertEqual((result['added'], result['removed']), (1, 0))
        self.assertIn('+middle', result['diff'])
        self.assertFalse(result['partial'])
        self.assertEqual(result['first']['id'], a['id'])
        self.assertEqual(result['second']['id'], b['id'])
        self.store.add(2, 100, document('foreign', 'foreign'))
        with self.assertRaises(wb.WorkspaceError):
            self.store.compare(2, 100, a['id'], b['id'])

    def test_compare_partial_limits_and_other_project_rejected(self):
        first = self.store.add(1, 100, document('first', 'a', ['Content truncated to 60000 characters']))
        second = self.store.add(1, 100, document('second', 'b'))
        self.assertTrue(self.store.compare(1, 100)['partial'])
        self.store.create(1, 100, 'Other')
        third = self.store.add(1, 100, document('third', 'c'))
        with self.assertRaisesRegex(wb.WorkspaceError, 'same_project'):
            self.store.compare(1, 100, first['id'], third['id'])

    def test_quotas_do_not_delete_or_replace_existing_files(self):
        self.store.create(1, 100, 'One')
        with patch.object(wb, 'MAX_FILES', 2):
            self.store.add(1, 100, document('a', 'a'))
            selected = self.store.add(1, 100, document('b', 'b'))
            with self.assertRaisesRegex(wb.WorkspaceError, 'files_limit'):
                self.store.add(1, 100, document('c', 'c'))
            self.assertEqual(self.store.file(1, 100)['id'], selected['id'])
            self.assertEqual(len(self.store.files(1, 100)), 2)
            self.assertEqual(self.store.add(1, 100, document('b', 'b'))['id'], selected['id'])
        with patch.object(wb, 'MAX_PROJECTS', 1):
            with self.assertRaisesRegex(wb.WorkspaceError, 'projects_limit'):
                self.store.create(1, 100, 'Two')
        with self.assertRaisesRegex(wb.WorkspaceError, 'duplicate'):
            self.store.create(1, 100, 'one')

    def test_delete_and_reset_do_not_resurrect_legacy_documents(self):
        first = self.memory.save_document(1, 100, document('a', 'a'))
        self.memory.save_document(2, 100, document('other', 'keep'))
        self.store.delete_file(1, 100, first['id'])
        self.assertIsNone(self.memory.document(1, 100))
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM file_context').fetchone()[0], 0)
        self.store.create(1, 100, 'Project')
        self.memory.save_document(1, 100, document('b', 'b'))
        self.memory.reset(1)
        self.assertEqual(self.store.projects(1, 100), [])
        self.assertIsNone(self.store.file(1, 100))
        self.assertEqual(self.memory.document(2, 100)['text'], 'keep')

    def test_delete_project_resets_active_scope_and_never_reuses_callback_id(self):
        inbox = self.store.add(1, 100, document('inbox', 'saved'))
        project = self.store.create(1, 100, 'Old')
        self.store.add(1, 100, document('project', 'remove'))
        self.store.delete_project(1, 100, project['id'])
        self.assertEqual(self.store.scope(1, 100), (0, inbox['id']))
        new = self.store.create(1, 100, 'New')
        self.assertGreater(new['id'], project['id'])
        with self.assertRaises(wb.WorkspaceError):
            self.store.delete_project(1, 100, project['id'])

    def test_legacy_migration_and_restart_preserve_content_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'legacy.db')
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE file_context(user_id INTEGER,chat_id INTEGER,content TEXT)')
            db.execute('INSERT INTO file_context VALUES(1,100,?)', (json.dumps(document(text='legacy')),))
            db.commit()
            db.close()
            mem = bot.Memory(path)
            self.assertEqual(mem.document(1, 100)['text'], 'legacy')
            self.assertEqual(mem.db.execute('SELECT COUNT(*) FROM file_context').fetchone()[0], 0)
            mem.workbench.create(1, 100, 'Persistent')
            mem.save_document(1, 100, document('v.py', 'print(1)'))
            mem.close()
            mem = bot.Memory(path)
            self.assertEqual(mem.workbench.project(1, 100)['name'], 'Persistent')
            self.assertEqual(mem.document(1, 100)['text'], 'print(1)')
            self.assertEqual(mem.db.execute('SELECT COUNT(*) FROM wb_files').fetchone()[0], 2)
            mem.close()

    def test_diagnostics_redact_secrets_and_do_not_misclassify_account_number(self):
        token = 'sk-' + 'x' * 30
        text = 'ModuleNotFoundError: No module named abc\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz\napi_key=' + token + '\nReadTimeout: server\nAccount=14011429'
        hints = wb.diagnostic_text(text)
        self.assertIn('Python', hints)
        self.assertIn('время ожидания', hints)
        self.assertNotIn(token, wb.redact(text))
        self.assertNotIn('abcdefghijklmnopqrstuvwxyz', wb.redact(text))
        self.assertFalse(any('авторизации' in h['title'] for h in wb.diagnose('Account=14011429')))

    def test_word_report_is_valid_and_can_be_read_back(self):
        payload = wb.docx_report('Отчёт', '# Заголовок\nТекст <&>\nстрока\x01 bad\uffff\napi_key=secret_value')
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in archive.namelist():
                ET.fromstring(archive.read(name))
            xml = archive.read('word/document.xml').decode()
            self.assertNotIn('secret_value', xml)
            self.assertIn('&lt;&amp;&gt;', xml)
            self.assertIn('Heading1', xml)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.docx'
            path.write_bytes(payload)
            result = docreader.extract(path, path.name)
            self.assertIn('Заголовок', result['text'])
            self.assertIn('Текст <&>', result['text'])


class ControlsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(':memory:')
        self.addCleanup(self.memory.close)
        self.message = NS(document=None, reply_to_message=None, text=None, caption=None, photo=[], voice=None, audio=None, video_note=None,
                          reply_document=AsyncMock())
        self.user = NS(id=1, language_code='ru', is_bot=False)
        self.update = NS(effective_user=self.user, effective_chat=NS(id=100, type='private'), effective_message=self.message)
        self.context = NS(args=[], bot_data={'memory': self.memory, 'stt': None, 'locks': defaultdict(asyncio.Lock)})
        self.status = NS(delete=AsyncMock())
        self.api = NS(authorized=AsyncMock(return_value=True), counted=lambda f: f,
                      user_lock=lambda ctx, uid: ctx.bot_data['locks'][uid],
                      send=AsyncMock(), reply=AsyncMock(return_value=self.status),
                      ui=lambda u, ru, en: ru if u.effective_user.language_code == 'ru' else en,
                      error_text=lambda e: ('Ошибка запроса', 'Request failed'), user_model=lambda data, uid: 'reformboss:grok-4.7-fast',
                      quick_answer=AsyncMock(return_value='# Отчёт\nПодтверждённые выводы.'))
        self.controls = workbench_ui.Controls(self.api)

    async def command(self, handler, args=None):
        self.context.args = args or []
        await self.controls.guarded(handler)(self.update, self.context)

    async def test_project_upload_search_compare_report_diagnose_flow(self):
        await self.command(self.controls.project, ['create', 'My', 'bot'])
        remote = NS(download_to_drive=AsyncMock(side_effect=lambda path: Path(path).write_text('ModuleNotFoundError: No module named demo\nvalue=1')))
        self.message.document = NS(file_name='server.log', file_size=70, mime_type='text/plain', get_file=AsyncMock(return_value=remote))
        self.message.caption = 'Проверь файл'
        status = NS(delete=AsyncMock(), edit_text=AsyncMock())
        with patch.object(bot, 'reply', AsyncMock(return_value=status)):
            _, content, saved = await bot.read_question(self.update, self.context)
        self.assertIn('ModuleNotFoundError', content)
        self.assertIn('Attachment #', saved)
        self.memory.save_document(1, 100, document('settings.py', 'value=2'))
        self.message.document = None
        self.message.text = 'Объясни связь файлов'
        _, context, _ = await bot.read_question(self.update, self.context)
        self.assertIn('server.log', context)
        self.assertIn('settings.py', context)
        await self.command(self.controls.find, ['ModuleNotFoundError'])
        self.assertIn('ModuleNotFoundError', self.api.send.call_args.args[1])
        await self.command(self.controls.compare)
        self.assertTrue(any(c.kwargs.get('filename') == 'secret-ai-comparison.diff' for c in self.message.reply_document.call_args_list))
        await self.command(self.controls.report)
        names = [c.kwargs.get('filename') for c in self.message.reply_document.call_args_list]
        self.assertIn('secret-ai-report.md', names)
        self.assertIn('secret-ai-report.docx', names)
        log = self.memory.workbench.files(1, 100)[-1]
        await self.command(self.controls.diagnose, [str(log['id'])])
        self.assertIn('Local pattern matches', self.api.quick_answer.call_args.args[3])
        self.assertIn('ModuleNotFoundError', self.api.quick_answer.call_args.args[3])
        self.status.delete.assert_awaited()

    async def test_explicit_file_selection_limits_followup_context(self):
        self.memory.workbench.create(1, 100, 'Project')
        first = self.memory.save_document(1, 100, document('a', 'first_only'))
        self.memory.save_document(1, 100, document('b', 'second_only'))
        await self.command(self.controls.files, ['use', str(first['id'])])
        self.message.text = 'Что в выбранном файле?'
        _, context, _ = await bot.read_question(self.update, self.context)
        self.assertIn('first_only', context)
        self.assertNotIn('second_only', context)

    async def test_delete_requires_confirmation_and_rejects_another_users_button(self):
        file = self.memory.save_document(1, 100, document())
        await self.command(self.controls.files, ['delete', str(file['id'])])
        self.assertIsNotNone(self.memory.document(1, 100))
        markup = self.api.send.call_args.args[2]
        data = markup.inline_keyboard[0][0].callback_data
        query = NS(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
        self.update.callback_query = query
        self.user.id = 2
        await self.controls.callback(self.update, self.context)
        query.answer.assert_awaited_with('Эта кнопка принадлежит другому пользователю.', show_alert=True)
        self.assertIsNotNone(self.memory.document(1, 100))
        self.user.id = 1
        await self.controls.callback(self.update, self.context)
        self.assertIsNone(self.memory.document(1, 100))
        self.assertIsNone(self.memory.workbench.file(1, 100, file['id']))

    async def test_unknown_ids_and_no_files_are_useful_errors(self):
        await self.command(self.controls.files, ['export', '999999'])
        self.assertIn('не найден', self.api.send.call_args.args[1])
        await self.command(self.controls.report)
        self.assertIn('Сначала отправь файл', self.api.send.call_args.args[1])
        self.api.quick_answer.assert_not_called()

    async def test_model_failure_removes_status_and_preserves_files(self):
        self.memory.save_document(1, 100, document())
        self.api.quick_answer.side_effect = RuntimeError('provider offline')
        await self.command(self.controls.report)
        self.status.delete.assert_awaited_once()
        self.assertEqual(self.api.send.call_args.args[1], 'Ошибка запроса')
        self.assertIsNotNone(self.memory.document(1, 100))
        self.message.reply_document.assert_not_called()

    async def test_ordinary_chat_after_project_off_keeps_normal_question(self):
        self.memory.workbench.create(1, 100, 'Project')
        self.memory.save_document(1, 100, document())
        await self.command(self.controls.project, ['off'])
        self.message.text = 'Сколько будет два плюс два?'
        q, content, _ = await bot.read_question(self.update, self.context)
        self.assertEqual(content, q)
        self.assertNotIn(docreader.MARKER, content)

    async def test_access_denied_never_reads_or_queries_models(self):
        self.api.authorized.return_value = False
        await self.command(self.controls.report)
        self.api.quick_answer.assert_not_called()
        self.message.reply_document.assert_not_called()

    async def test_group_policy_and_private_documents_stay_separate(self):
        self.context.bot_data['allowed'] = {1}
        self.update.effective_chat = NS(id=-100, type='supergroup')
        self.assertTrue(await bot.workbench_authorized(self.update, self.context))
        self.user.id = 2
        self.assertFalse(await bot.workbench_authorized(self.update, self.context))
        self.user.id = 1
        private = self.memory.save_document(1, 100, document('private.txt', 'private only'))
        self.memory.save_document(1, -100, document('group.txt', 'group only'))
        await self.command(self.controls.files, ['export', str(private['id'])])
        self.message.reply_document.assert_not_called()
        self.assertIn('не найден', self.api.send.call_args.args[1])

    async def test_unavailable_model_keeps_files_and_returns_specific_message(self):
        self.memory.save_document(1, 100, document())
        self.api.user_model = lambda data, uid: None
        await self.command(self.controls.report)
        self.api.quick_answer.assert_not_called()
        self.assertIn('Файлы сохранены', self.api.send.call_args.args[1])
        self.assertIsNotNone(self.memory.document(1, 100))

    def test_commands_registered_before_generic_callback_and_document_filters(self):
        handlers = []
        self.controls.register(NS(add_handler=handlers.append))
        names = {name for h in handlers if hasattr(h, 'commands') for name in h.commands}
        self.assertEqual(names, {'project', 'files', 'find', 'compare', 'report', 'diagnose'})
        self.assertTrue(handlers[-1].pattern.match('wb:1:fileuse:1'))


if __name__ == '__main__':
    unittest.main()
