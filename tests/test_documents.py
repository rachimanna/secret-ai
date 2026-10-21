"""Extraction and Telegram question flow without real Telegram/model credentials."""
import asyncio
import json
import plistlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot
import docreader as docs


class Files(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'file'

    def archive(self, members):
        with zipfile.ZipFile(self.path, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, value in members.items():
                z.writestr(name, value)

    def test_text_line_locations_and_truncation(self):
        self.path.write_text('Привет\nошибка\n' + 'x' * 70_000)
        result = docs.extract(self.path, 'test.txt')
        self.assertIn('L2: ошибка', result['text'])
        self.assertEqual(len(result['text']), docs.MAX_TEXT)
        self.assertTrue(any('truncated' in w for w in result['warnings']))
        self.assertIn(docs.MARKER, docs.prompt(result, 'Проверь'))

    def test_docx_includes_paragraphs_tables_and_comments(self):
        self.archive({'word/document.xml': '<document><body><p><r><t>Первый</t></r></p><tbl><tr><tc><p><r><t>Таблица</t></r></p></tc></tr></tbl></body></document>',
                      'word/comments.xml': '<comments><comment><p><r><t>Поправить</t></r></p></comment></comments>'})
        text = docs.extract(self.path, 'report.docx')['text']
        self.assertIn('Paragraph 2: Таблица', text)
        self.assertIn('word/comments.xml', text)
        self.assertIn('Поправить', text)

    def test_xlsx_preserves_sheet_cells_and_formula_errors(self):
        self.archive({'xl/workbook.xml': '<workbook xmlns:r="urn:r"><sheets><sheet name="Расходы" r:id="rId1"/></sheets></workbook>',
                      'xl/_rels/workbook.xml.rels': '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
                      'xl/sharedStrings.xml': '<sst><si><t>Итого</t></si></sst>',
                      'xl/worksheets/sheet1.xml': '<worksheet><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><v>10</v></c><c r="C1" t="e"><f>B1/0</f><v>#DIV/0!</v></c></row></sheetData></worksheet>'})
        text = docs.extract(self.path, 'budget.xlsx')['text']
        for expected in ('Sheet: Расходы', 'A1: Итого', 'B1: 10', 'C1: =B1/0 [cached: #DIV/0!]'):
            self.assertIn(expected, text)

    def test_pptx_orders_slides_numerically_and_reads_notes(self):
        self.archive({'ppt/slides/slide10.xml': '<slide><t>Ten</t></slide>',
                      'ppt/slides/slide2.xml': '<slide><t>Two</t></slide>',
                      'ppt/notesSlides/notesSlide2.xml': '<notes><t>Note</t></notes>'})
        text = docs.extract(self.path, 'talk.pptx')['text']
        self.assertLess(text.index('Two'), text.index('Ten'))
        self.assertIn('Note', text)

    def test_pdf_text_pages_and_password(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 20 200 Td (Hello document) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        writer.write(self.path)
        result = docs.extract(self.path, 'book.pdf')
        self.assertIn('[Page 1]', result['text'])
        self.assertIn('Hello document', result['text'])
        writer.encrypt('secret')
        writer.write(self.path)
        with self.assertRaisesRegex(docs.DocumentError, 'encrypted'):
            docs.extract(self.path, 'book.pdf')

    def test_ipa_binary_plist_and_missing_executable(self):
        info = plistlib.dumps({'CFBundleIdentifier': 'org.test.app', 'CFBundleExecutable': 'Missing',
                              'NSCameraUsageDescription': 'Camera'}, fmt=plistlib.FMT_BINARY)
        self.archive({'Payload/Demo.app/Info.plist': info})
        result = docs.extract(self.path, 'app.ipa')
        self.assertIn('org.test.app', result['text'])
        self.assertTrue(any('executable missing' in w for w in result['warnings']))
        self.assertTrue(any('not verified' in w for w in result['warnings']))

    def test_apk_binary_manifest_does_not_invent_permissions(self):
        self.archive({'AndroidManifest.xml': b'\x03\x00\x08\x00binary', 'classes.dex': b'dex\n035\x00', 'assets/config.json': '{"debug":true}'})
        result = docs.extract(self.path, 'app.apk')
        self.assertIn('L1: {"debug":true}', result['text'])
        self.assertTrue(any('not decoded' in w for w in result['warnings']))

    def test_archives_are_not_extracted_and_binary_source_is_skipped(self):
        self.archive({'../outside.txt': 'hello', 'code.py': 'raise RuntimeError("DO NOT RUN")', 'data.txt': b'\x00binary'})
        result = docs.extract(self.path, 'source.zip')
        self.assertIn('DO NOT RUN', result['text'])
        self.assertTrue(any('traversal' in w for w in result['warnings']))
        self.assertFalse((self.path.parent.parent / 'outside.txt').exists())
        with patch.object(docs, 'MAX_EXPANDED', 2):
            with self.assertRaisesRegex(docs.DocumentError, 'archive_limit'):
                docs.extract(self.path, 'source.zip')

    def test_xml_entities_rejected_and_legacy_format_explained(self):
        self.archive({'word/document.xml': '<!DOCTYPE d [<!ENTITY x "EXPAND">]><d><p><t>&x;</t></p></d>'})
        with self.assertRaises(Exception):
            docs.extract(self.path, 'bad.docx')
        with self.assertRaisesRegex(docs.DocumentError, 'unsupported'):
            docs.extract(self.path, 'old.doc')


class TelegramDocuments(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(':memory:')
        self.addCleanup(self.memory.close)
        self.context = NS(bot_data={'memory': self.memory, 'stt': None})
        self.message = NS(document=None, reply_to_message=None, text=None, caption=None, photo=[], voice=None, audio=None, video_note=None)
        self.update = NS(effective_message=self.message, effective_user=NS(id=1, language_code='ru'),
                         effective_chat=NS(id=100, type='private'))

    async def test_real_download_worker_then_followup_and_reset(self):
        async def download(path):
            Path(path).write_text('Important text\ncheck this line')
        remote = NS(download_to_drive=AsyncMock(side_effect=download))
        document = NS(file_name='notes.txt', file_size=40, mime_type='text/plain', get_file=AsyncMock(return_value=remote))
        self.message.document, self.message.caption = document, 'Найди ошибки'
        status = NS(delete=AsyncMock(), edit_text=AsyncMock())
        with patch.object(bot, 'reply', AsyncMock(return_value=status)):
            question, content, saved = await bot.read_question(self.update, self.context)
        self.assertEqual(question, 'Найди ошибки')
        self.assertIn('L2: check this line', content)
        self.assertNotIn('Important text', saved)
        status.delete.assert_awaited_once()
        self.message.document = None
        self.message.text = 'Проверь вторую строку'
        _, followup, _ = await bot.read_question(self.update, self.context)
        self.assertIn('Important text', followup)
        self.assertIsNone(self.memory.document(2, 100))
        self.assertIsNone(self.memory.document(1, 200))
        self.memory.reset(1)
        self.assertIsNone(self.memory.document(1, 100))

    async def test_reply_to_file_reads_it_and_oversize_never_downloads(self):
        doc = NS(file_name='large.txt', mime_type='text/plain', file_size=docs.MAX_FILE + 1, get_file=AsyncMock())
        self.message.text = 'Объясни файл'
        self.message.reply_to_message = NS(document=doc)
        status = NS(delete=AsyncMock(), edit_text=AsyncMock())
        with patch.object(bot, 'reply', AsyncMock(return_value=status)):
            self.assertIsNone(await bot.read_question(self.update, self.context))
        self.assertIn('19 МБ', status.edit_text.call_args.args[0])
        doc.get_file.assert_not_called()
        self.assertIsNone(self.memory.document(1, 100))

    async def test_worker_timeout_is_killed(self):
        async def download(path):
            Path(path).write_text('test')
        doc = NS(file_name='test.txt', file_size=4, get_file=AsyncMock(return_value=NS(download_to_drive=download)))
        async def hang():
            await asyncio.Future()
        process = NS(returncode=None, communicate=hang, kill=lambda: setattr(process, 'returncode', -9), wait=AsyncMock())
        original = asyncio.wait_for
        async def short_wait(awaitable, timeout):
            return await original(awaitable, .01)
        with patch.object(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), patch.object(asyncio, 'wait_for', short_wait):
            with self.assertRaises(asyncio.TimeoutError):
                await docs.read_attachment(doc)
        self.assertEqual(process.returncode, -9)
        process.wait.assert_awaited_once()

    async def test_corrupt_document_returns_readable_error(self):
        async def download(path):
            Path(path).write_bytes(b'not a zip')
        doc = NS(file_name='bad.docx', file_size=9, get_file=AsyncMock(return_value=NS(download_to_drive=download)))
        with self.assertRaisesRegex(docs.DocumentError, 'format'):
            await docs.read_attachment(doc)

    def test_context_survives_restart_and_replaces_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'memory.db')
            memory = bot.Memory(path)
            memory.save_document(1, 100, {'name': 'first.txt', 'text': 'first', 'warnings': []})
            memory.save_document(1, 100, {'name': 'second.txt', 'text': 'second', 'warnings': []})
            memory.close()
            memory = bot.Memory(path)
            self.assertEqual(memory.document(1, 100)['text'], 'second')
            self.assertEqual(memory.db.execute('SELECT COUNT(*) FROM file_context').fetchone()[0], 1)
            memory.close()


if __name__ == '__main__':
    unittest.main()
