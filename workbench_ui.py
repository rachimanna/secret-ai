"""Telegram controls for the document workbench; credentials and model routing remain in bot.py."""
import asyncio
import io
import logging
import re
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler

import docreader
import workbench

LOG = logging.getLogger('secret_ai.workbench')
HELP_RU = ('Работа с файлами и проектами:\n'
           '/project — проекты; /project create Название — создать\n'
           '/project open ID — открыть; /project off — входящие\n'
           '/files — файлы и версии; /files use ID — выбрать\n'
           '/files export ID — скачать извлечённый текст\n'
           '/find запрос — найти фрагменты в файлах проекта\n'
           '/compare — сравнить два последних файла; /compare ID1 ID2 — выбрать\n'
           '/report вопрос — отчёт в Word и Markdown\n'
           '/diagnose — найти причины ошибок в последнем выбранном файле\n\n'
           'После открытия проекта загруженные файлы сохраняются в него. Повторная загрузка того же имени с новым содержимым создаёт версию. '
           'Вопросы в открытом проекте учитывают несколько его файлов. /project off возвращает обычный чат. '
           '/project delete ID и /files delete ID — удалить после подтверждения.')
HELP_EN = ('Document workbench:\n'
           '/project — list; /project create Name — create\n'
           '/project open ID — open; /project off — inbox\n'
           '/files — files/versions; /files use ID — select\n'
           '/files export ID — download extracted text\n'
           '/find query — search project files\n'
           '/compare [ID1 ID2] — compare two files\n'
           '/report question — Word and Markdown report\n'
           '/diagnose — analyse the selected log/file\n\n'
           'Uploads belong to the open project. Re-uploading a name with changed content creates a version. '
           'Questions in a project use excerpts from its files. /project off returns to ordinary chat. '
           '/project delete ID and /files delete ID require confirmation.')
ERRORS = {
    'name': ('Укажи название: /project create Мой бот', 'Give a name: /project create My bot'),
    'duplicate': ('Такой проект уже есть. Открой его из /project.', 'That project already exists. Open it from /project.'),
    'projects_limit': (f'Лимит — {workbench.MAX_PROJECTS} проектов. Удали ненужный через /project delete ID.', f'Limit: {workbench.MAX_PROJECTS} projects. Remove one with /project delete ID.'),
    'files_limit': (f'Достигнут лимит: {workbench.MAX_FILES} файлов/версий на чат или {workbench.MAX_USER_FILES} на пользователя. Удали ненужные через /files delete ID.',
                    f'Limit reached: {workbench.MAX_FILES} files/versions per chat or {workbench.MAX_USER_FILES} per user. Remove unused versions with /files delete ID.'),
    'not_found': ('Файл или проект не найден в этом чате. Открой /files или /project.', 'File/project not found in this chat. Open /files or /project.'),
    'two_files': ('Нужно два файла или две версии. Загрузи их в один проект и повтори /compare.', 'Upload two files or versions in one project, then run /compare.'),
    'same_project': ('Для сравнения выбери два файла из одного открытого проекта.', 'Choose two files in the same open project.'),
    'no_files': ('Сначала отправь файл. Для нескольких файлов создай /project create Название.', 'Send a file first. For multiple files use /project create Name.'),
    'no_model': ('Модель сейчас недоступна. Файлы сохранены; попробуй позже.', 'The model is currently unavailable. Your files are saved; try later.'),
    'empty_answer': ('Модель вернула пустой ответ. Попробуй ещё раз.', 'The model returned an empty answer. Try again.'),
    'arguments': ('Проверь команду и числовые ID. Подсказка: /project help', 'Check the command and numeric IDs. Help: /project help'),
}


def error_text(update, api, exc):
    return api.ui(update, *ERRORS.get(str(exc), ERRORS['arguments']))


class Controls:
    def __init__(self, api):
        self.api = api

    def coordinates(self, update, context):
        return context.bot_data['memory'].workbench, update.effective_user.id, update.effective_chat.id

    def button(self, update, text, action, item=0):
        return InlineKeyboardButton(text, callback_data=f'wb:{update.effective_user.id}:{action}:{item}')

    def guarded(self, handler):
        @wraps(handler)
        async def run(update, context):
            if not await self.api.authorized(update, context):
                return
            async with self.api.user_lock(context, update.effective_user.id):
                try:
                    await handler(update, context)
                except workbench.WorkspaceError as exc:
                    await self.api.send(update.effective_message, error_text(update, self.api, exc))
                except Exception as exc:
                    LOG.warning('Workbench command failed: %s', type(exc).__name__)
                    await self.api.send(update.effective_message, self.api.ui(update, *self.api.error_text(exc)))
        return self.api.counted(run)

    def register(self, app):
        for command, handler in (('project', self.project), ('files', self.files), ('find', self.find),
                                 ('compare', self.compare), ('report', self.report), ('diagnose', self.diagnose)):
            app.add_handler(CommandHandler(command, self.guarded(handler)))
        app.add_handler(CallbackQueryHandler(self.api.counted(self.callback), pattern=r'^wb:'))

    async def project(self, update, context):
        store, user, chat = self.coordinates(update, context)
        args = context.args or []
        action = args[0].lower() if args else 'list'
        if action in ('help', 'помощь'):
            await self.api.send(update.effective_message, self.api.ui(update, HELP_RU, HELP_EN))
            return
        if action == 'create':
            item = store.create(user, chat, ' '.join(args[1:]))
            await self.api.send(update.effective_message, self.api.ui(update,
                f"Открыт проект #{item['id']} «{item['name']}». Теперь отправляй файлы и задавай вопросы. /files — содержимое.",
                f"Project #{item['id']} “{item['name']}” is open. Send files and questions. /files lists contents."))
            return
        if action in ('open', 'delete'):
            if len(args) != 2 or not args[1].isdigit():
                raise workbench.WorkspaceError('arguments')
            item = store.project(user, chat, int(args[1]))
            if not item:
                raise workbench.WorkspaceError('not_found')
            if action == 'open':
                store.open(user, chat, item['id'])
            else:
                await self.confirm(update, f"проект «{item['name']}» со всеми его файлами", f"project “{item['name']}” and all its files", 'projectdel', item['id'])
                return
        elif action == 'off':
            store.open(user, chat, 0)
        elif action != 'list':
            raise workbench.WorkspaceError('arguments')
        current = store.project(user, chat)
        title = current['name'] if current else self.api.ui(update, 'Входящие / обычный чат', 'Inbox / ordinary chat')
        lines = [self.api.ui(update, f'Текущий: {title}', f'Current: {title}')]
        rows = []
        for item in store.projects(user, chat):
            lines.append(f"#{item['id']} · {item['name']} · {item['count']} " + self.api.ui(update, 'файлов/версий', 'files/versions'))
            rows.append([self.button(update, item['name'][:45], 'projectopen', item['id'])])
        lines.append(self.api.ui(update, '\n/project create Название — создать\n/project help — все возможности', '\n/project create Name — create\n/project help — all features'))
        rows.append([self.button(update, self.api.ui(update, 'Обычный чат', 'Ordinary chat'), 'projectopen', 0)])
        await self.api.send(update.effective_message, '\n'.join(lines), InlineKeyboardMarkup(rows))

    async def files(self, update, context):
        store, user, chat = self.coordinates(update, context)
        args = context.args or []
        if args:
            if len(args) != 2 or not args[1].isdigit():
                raise workbench.WorkspaceError('arguments')
            file_id = int(args[1])
            item = store.file(user, chat, file_id)
            if not item:
                raise workbench.WorkspaceError('not_found')
            if args[0] == 'delete':
                await self.confirm(update, f"файл #{file_id} «{item['name']}» v{item['version']}", f"file #{file_id} “{item['name']}” v{item['version']}", 'filedel', file_id)
                return
            if args[0] == 'export':
                document = item['document']
                text = f"{document['name']} v{item['version']}\n" + '\n'.join(document['warnings']) + '\n\n' + workbench.redact(document['text'])
                await update.effective_message.reply_document(document=io.BytesIO(text.encode()), filename=f'secret-ai-file-{file_id}.txt')
                return
            if args[0] != 'use':
                raise workbench.WorkspaceError('arguments')
            store.use(user, chat, file_id)
        items = store.files(user, chat)
        current = store.project(user, chat)
        title = current['name'] if current else self.api.ui(update, 'Входящие', 'Inbox')
        selected = store.scope(user, chat)[1]
        lines = [self.api.ui(update, f'Файлы · {title}', f'Files · {title}')]
        rows = []
        for item in items:
            lines.append(f"{'→ ' if item['id'] == selected else ''}#{item['id']} {item['name']} · v{item['version']} · {len(item['document']['text'])} "
                         + self.api.ui(update, 'симв.', 'chars'))
            if len(rows) < 8:
                rows.append([self.button(update, f"#{item['id']} {item['name'][:35]} v{item['version']}", 'fileuse', item['id'])])
        if not items:
            lines.append(self.api.ui(update, 'Пока пусто. Отправь документ в этот чат.', 'Empty. Send a document in this chat.'))
        lines.append(self.api.ui(update, '\n/files use ID — выбрать\n/files delete ID — удалить\n/files export ID — скачать текст',
                                              '\n/files use ID — select\n/files delete ID — remove\n/files export ID — download text'))
        await self.api.send(update.effective_message, '\n'.join(lines), InlineKeyboardMarkup(rows) if rows else None)

    async def confirm(self, update, ru, en, action, item):
        text = self.api.ui(update, f'Удалить {ru}? Вернуть удалённое через бот не получится.', f'Delete {en}? The bot cannot undo this.')
        keyboard = InlineKeyboardMarkup([[self.button(update, self.api.ui(update, 'Удалить', 'Delete'), action, item),
                                          self.button(update, self.api.ui(update, 'Отмена', 'Cancel'), 'cancel')]])
        await self.api.send(update.effective_message, text, keyboard)

    async def callback(self, update, context):
        query = update.callback_query
        match = re.fullmatch(r'wb:(\d+):(projectopen|projectdel|fileuse|filedel|cancel):(\d+)', query.data or '')
        if not match or int(match[1]) != update.effective_user.id:
            await query.answer(self.api.ui(update, 'Эта кнопка принадлежит другому пользователю.', 'This button belongs to another user.'), show_alert=True)
            return
        await query.answer()
        if not await self.api.authorized(update, context):
            return
        async with self.api.user_lock(context, update.effective_user.id):
            store, user, chat = self.coordinates(update, context)
            action, item = match[2], int(match[3])
            try:
                if action == 'projectopen':
                    store.open(user, chat, item)
                    project = store.project(user, chat)
                    text = self.api.ui(update, 'Открыт проект: ', 'Project opened: ') + (project['name'] if project else self.api.ui(update, 'Входящие / обычный чат', 'Inbox / ordinary chat'))
                elif action == 'projectdel':
                    name = store.delete_project(user, chat, item)
                    text = self.api.ui(update, f'Проект «{name}» удалён.', f'Project “{name}” deleted.')
                elif action == 'fileuse':
                    document = store.use(user, chat, item)
                    text = self.api.ui(update, f"Выбран #{item} «{document['name']}» v{document['version']}. Задавай вопрос по файлу.",
                                                   f"Selected #{item} “{document['name']}” v{document['version']}. Ask about it.")
                elif action == 'filedel':
                    name = store.delete_file(user, chat, item)
                    text = self.api.ui(update, f'Файл «{name}» удалён.', f'File “{name}” deleted.')
                else:
                    text = self.api.ui(update, 'Отменено.', 'Cancelled.')
                await query.edit_message_text(text, reply_markup=None)
            except workbench.WorkspaceError as exc:
                await self.api.send(update.effective_message, error_text(update, self.api, exc))

    async def find(self, update, context):
        store, user, chat = self.coordinates(update, context)
        query = ' '.join(context.args or []).strip()
        if not query:
            await self.api.send(update.effective_message, self.api.ui(update, 'Напиши /find слово или фразу', 'Use /find word or phrase'))
            return
        hits = store.search(user, chat, query)
        if not hits:
            await self.api.send(update.effective_message, self.api.ui(update, 'Совпадений в текущем проекте не найдено. Поиск проверяет извлечённый текст, не картинки.',
                                                                                  'No matches in the current project. Search covers extracted text, not images.'))
            return
        blocks = []
        words = workbench.terms(query)
        for hit in hits:
            text = workbench.redact(hit['text'])
            first = min((text.casefold().find(w) for w in words if w in text.casefold()), default=0)
            excerpt = text[max(0, first - 140):max(0, first - 140) + 650]
            blocks.append(f"#{hit['id']} {hit['name']} v{hit['version']}\n{hit['location']}\n{excerpt}")
        await self.api.send(update.effective_message, '\n\n'.join(blocks))

    async def analyse(self, update, context, text, instructions, limit=5000):
        model = self.api.user_model(context.bot_data, update.effective_user.id)
        if not model:
            raise workbench.WorkspaceError('no_model')
        status = await self.api.reply(update.effective_message, self.api.ui(update, 'Анализирую файлы…', 'Analysing files…'))
        try:
            answer = await asyncio.wait_for(self.api.quick_answer(context.bot_data, model,
                docreader.RULES + ' Reply in the language of the user. User language: ' + (update.effective_user.language_code or 'ru') + '. ' + instructions,
                workbench.redact(text), max_tokens=limit), 150)
            answer = workbench.redact((answer or '').strip())
            if not answer:
                raise workbench.WorkspaceError('empty_answer')
            return answer[:60_000]
        finally:
            try:
                await status.delete()
            except TelegramError:
                pass

    async def compare(self, update, context):
        store, user, chat = self.coordinates(update, context)
        args = context.args or []
        if args and (len(args) != 2 or not all(a.isdigit() for a in args)):
            raise workbench.WorkspaceError('arguments')
        pair = store.pair(user, chat, *map(int, args)) if args else store.pair(user, chat)
        result = await asyncio.to_thread(workbench.compare_documents, *pair)
        a, b = result['first'], result['second']
        summary = self.api.ui(update, 'Сравнение извлечённого текста:\n', 'Extracted-text comparison:\n') + f"#{a['id']} {a['name']} v{a['version']} → #{b['id']} {b['name']} v{b['version']}\n"
        summary += self.api.ui(update, f"Добавлено строк: {result['added']}; удалено: {result['removed']}.",
                                       f"Lines added: {result['added']}; removed: {result['removed']}.")
        if result['identical']:
            summary += self.api.ui(update, '\nИзвлечённый текст совпадает.', '\nExtracted text is identical.')
        if result['partial']:
            summary += self.api.ui(update, '\nСравнение частичное: есть ограничения извлечения или размера. Визуальное оформление не сравнивается.',
                                           '\nPartial comparison: extraction/size limits apply. Visual formatting is not compared.')
        await self.api.send(update.effective_message, summary)
        if not result['diff']:
            return
        await update.effective_message.reply_document(document=io.BytesIO(workbench.redact(result['diff']).encode()), filename='secret-ai-comparison.diff')
        answer = await self.analyse(update, context, docreader.MARKER + '\n' + summary + '\n' + result['diff'],
            'Summarise the material changes shown in this unified diff. Point out potential mistakes/regressions with evidence. '
            'Do not claim to have executed code, verified calculations, or inspected omitted material. Keep the answer useful and concise.', 3000)
        await self.api.send(update.effective_message, answer)
        context.bot_data['memory'].append_turn(user, f"[File comparison #{a['id']} → #{b['id']}]", answer)

    async def report(self, update, context):
        store, user, chat = self.coordinates(update, context)
        question = ' '.join(context.args or []).strip() or self.api.ui(update,
            'Подготовь отчёт: содержание, основные выводы, подтверждённые ошибки, предложения и следующие шаги. Приводи ссылки на места в файлах.',
            'Prepare a report: contents, findings, confirmed errors, suggestions and next steps, citing locations in the files.')
        content = store.context(user, chat, question)
        if not content:
            raise workbench.WorkspaceError('no_files')
        answer = await self.analyse(update, context, content,
            'Create a standalone report in clear Markdown, using headings, paragraphs and lists. '
            'Include scope/limitations, evidence with source IDs/locations, findings, actionable fixes and unresolved questions. '
            'Never invent an error just to fill a section. No preamble outside the report.')
        sources = store.files(user, chat, latest=True) if store.project(user, chat) and not store.focused(user, chat) else [store.file(user, chat)]
        source_text = '\n'.join(f"- #{item['id']} {item['name']} v{item['version']}" for item in sources)
        answer = workbench.redact(answer + '\n\n' + self.api.ui(update, 'Источники (извлечённое содержимое):\n', 'Sources (extracted contents):\n') + source_text)
        title = self.api.ui(update, 'Secret AI — отчёт по файлам', 'Secret AI — document report')
        await update.effective_message.reply_document(document=io.BytesIO(answer.encode()), filename='secret-ai-report.md')
        await update.effective_message.reply_document(document=io.BytesIO(workbench.docx_report(title, answer)), filename='secret-ai-report.docx')
        context.bot_data['memory'].append_turn(user, '[Document report] ' + question, answer[:20_000])
        await self.api.send(update.effective_message, self.api.ui(update, 'Отчёт готов: Word и Markdown. /files — исходные файлы.', 'Report ready: Word and Markdown. /files lists source files.'))

    async def diagnose(self, update, context):
        store, user, chat = self.coordinates(update, context)
        args = context.args or []
        if args and (len(args) != 1 or not args[0].isdigit()):
            raise workbench.WorkspaceError('arguments')
        file = store.file(user, chat, int(args[0])) if args else store.file(user, chat)
        if not file:
            raise workbench.WorkspaceError('no_files')
        notes = workbench.diagnostic_text(file['document']['text'])
        question = self.api.ui(update, 'Разбери ошибки и объясни, что проверить и исправить.', 'Analyse the errors and explain checks and fixes.')
        content = docreader.prompt(file['document'], question) + '\n\n[Local pattern matches: hints, not verified root causes]\n' + notes
        answer = await self.analyse(update, context, content,
            'Diagnose this uploaded log or source document. Use actual error lines and available code as evidence; '
            'treat local pattern matches as hypotheses. Credential placeholders are redactions, not original syntax errors. '
            'Give the likely cause, an ordered repair plan and how to verify success. '
            'If evidence is insufficient, say what exact missing log/config is needed. Do not invent stack frames or claim execution.', 4000)
        await self.api.send(update.effective_message, answer)
        await update.effective_message.reply_document(document=io.BytesIO(answer.encode()), filename='secret-ai-diagnostics.txt')
        context.bot_data['memory'].append_turn(user, f"[Diagnostics #{file['id']} {file['name']}]", answer[:20_000])
