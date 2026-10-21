"""Persistent, owner-scoped document projects, retrieval, comparisons and reports."""
import difflib
import hashlib
import io
import json
import re
import time
import zipfile
from xml.sax.saxutils import escape

import docreader

MAX_PROJECTS = 8
MAX_FILES = 30  # revisions per person/chat
MAX_USER_FILES = 150
CONTEXT_CHARS = 50_000
CHUNK_CHARS = 2000
STOP = {'the', 'and', 'for', 'this', 'that', 'what', 'with', 'from', 'как', 'что', 'это', 'для', 'или', 'мне', 'все', 'про', 'пожалуйста', 'найди', 'проверь', 'объясни'}


class WorkspaceError(ValueError):
    pass


def terms(text):
    return list(dict.fromkeys(w for w in re.findall(r'[\w-]{3,}', text.casefold()) if w not in STOP))[:16]


def redact(text):
    """Remove common credential shapes from diagnostic/export excerpts, not arbitrary numbers."""
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{16,}', '[API_KEY_REDACTED]', text)
    text = re.sub(r'\b\d{6,12}:[A-Za-z0-9_-]{30,}\b', '[BOT_TOKEN_REDACTED]', text)
    text = re.sub(r'(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]{12,}', r'\1[REDACTED]', text)
    pattern = r"(?im)(\b(?:api[_-]?key|access[_-]?token|password|secret|authorization)\b[\s\"']*[:=]\s*)(?:([\"'])([^\r\n]*?)\2|([^\s,;\r\n]+))"
    def hide(match):
        quote = match[2] or ''
        return match[1] + quote + '[REDACTED]' + quote
    return re.sub(pattern, hide, text)


def chunks(text):
    """Bounded chunks retain source labels plus explicit extracted-text offsets."""
    for start in range(0, len(text), CHUNK_CHARS):
        end = min(len(text), start + CHUNK_CHARS)
        headers = list(re.finditer(r'(?m)^\[(?:Page |Sheet: |ppt/|word/|File: )[^\n]+', text[:start]))
        label = headers[-1].group(0) if headers else ''
        yield {'start': start, 'end': end, 'location': f'{label} chars {start + 1}–{end}'.strip(), 'text': text[start:end]}


def score(query_terms, text, name=''):
    haystack, title = text.casefold(), name.casefold()
    return sum(min(haystack.count(w), 6) + (8 if w in title else 0) for w in query_terms)


class Store:
    def __init__(self, db):
        self.db = db
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS wb_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS wb_projects (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, '
                       'name TEXT NOT NULL, created INTEGER NOT NULL, UNIQUE(user_id,chat_id,name))')
            db.execute('CREATE TABLE IF NOT EXISTS wb_scopes (user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, '
                       'project_id INTEGER NOT NULL DEFAULT 0, selected_id INTEGER, focus INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,chat_id))')
            db.execute('CREATE TABLE IF NOT EXISTS wb_files (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, '
                       'project_id INTEGER NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL, '
                       'content TEXT NOT NULL, created INTEGER NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS wb_file_scope ON wb_files(user_id,chat_id,project_id,id)')
            if not db.execute("SELECT 1 FROM wb_meta WHERE key='legacy_files_v1'").fetchone():
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='file_context'").fetchone():
                    for uid, cid, raw in db.execute('SELECT user_id,chat_id,content FROM file_context').fetchall():
                        try:
                            self.add(uid, cid, json.loads(raw), importing=True)
                        except (ValueError, TypeError, KeyError):
                            continue
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='file_context'").fetchone():
                    db.execute("DELETE FROM file_context")
                db.execute("INSERT INTO wb_meta(key,value) VALUES ('legacy_files_v1','done')")

    def scope(self, user, chat):
        row = self.db.execute('SELECT project_id,selected_id FROM wb_scopes WHERE user_id=? AND chat_id=?', (user, chat)).fetchone()
        return row if row else (0, None)

    def focused(self, user, chat):
        row = self.db.execute('SELECT focus FROM wb_scopes WHERE user_id=? AND chat_id=?', (user, chat)).fetchone()
        return bool(row and row[0])

    def _scope(self, user, chat, project, selected, focus=False):
        self.db.execute('INSERT INTO wb_scopes(user_id,chat_id,project_id,selected_id,focus) VALUES (?,?,?,?,?) '
                        'ON CONFLICT(user_id,chat_id) DO UPDATE SET project_id=excluded.project_id,selected_id=excluded.selected_id,focus=excluded.focus',
                        (user, chat, project, selected, int(focus)))

    def project(self, user, chat, project=None):
        project = self.scope(user, chat)[0] if project is None else project
        row = self.db.execute('SELECT id,name FROM wb_projects WHERE id=? AND user_id=? AND chat_id=?', (project, user, chat)).fetchone()
        return {'id': row[0], 'name': row[1]} if row else None

    def projects(self, user, chat):
        return [{'id': row[0], 'name': row[1], 'count': row[2]} for row in self.db.execute(
            'SELECT p.id,p.name,(SELECT COUNT(*) FROM wb_files f WHERE f.project_id=p.id AND f.user_id=p.user_id AND f.chat_id=p.chat_id) '
            'FROM wb_projects p WHERE p.user_id=? AND p.chat_id=? ORDER BY p.id', (user, chat))]

    def create(self, user, chat, name):
        name = ' '.join(name.split()).strip()[:80]
        if not name:
            raise WorkspaceError('name')
        if any(p['name'].casefold() == name.casefold() for p in self.projects(user, chat)):
            raise WorkspaceError('duplicate')
        if len(self.projects(user, chat)) >= MAX_PROJECTS:
            raise WorkspaceError('projects_limit')
        with self.db:
            cur = self.db.execute('INSERT INTO wb_projects(user_id,chat_id,name,created) VALUES (?,?,?,?)', (user, chat, name, int(time.time())))
            self._scope(user, chat, cur.lastrowid, None)
        return self.project(user, chat)

    def open(self, user, chat, project):
        if project and not self.project(user, chat, project):
            raise WorkspaceError('not_found')
        last = self.db.execute('SELECT id FROM wb_files WHERE user_id=? AND chat_id=? AND project_id=? ORDER BY id DESC LIMIT 1', (user, chat, project)).fetchone()
        with self.db:
            self._scope(user, chat, project, last[0] if last else None)

    def files(self, user, chat, latest=False):
        project = self.scope(user, chat)[0]
        rows = self.db.execute('SELECT id,name,version,digest,content,created FROM wb_files WHERE user_id=? AND chat_id=? AND project_id=? ORDER BY id DESC',
                               (user, chat, project)).fetchall()
        found, result = set(), []
        for row in rows:
            if latest and row[1] in found:
                continue
            found.add(row[1])
            result.append({'id': row[0], 'name': row[1], 'version': row[2], 'digest': row[3], 'document': json.loads(row[4]), 'created': row[5]})
        return result

    def file(self, user, chat, file_id=None):
        project, selected = self.scope(user, chat)
        file_id = selected if file_id is None else file_id
        row = self.db.execute('SELECT id,project_id,name,version,digest,content FROM wb_files WHERE id=? AND user_id=? AND chat_id=?',
                              (file_id, user, chat)).fetchone()
        return {'id': row[0], 'project_id': row[1], 'name': row[2], 'version': row[3], 'digest': row[4], 'document': json.loads(row[5])} if row else None

    def use(self, user, chat, file_id):
        document = self.file(user, chat, file_id)
        if not document:
            raise WorkspaceError('not_found')
        with self.db:
            self._scope(user, chat, document['project_id'], document['id'], focus=True)
        return document

    def add(self, user, chat, document, importing=False):
        if not isinstance(document.get('text'), str) or not document['text']:
            raise WorkspaceError('empty')
        document = {'name': str(document.get('name') or 'document')[:200], 'text': document['text'][:docreader.MAX_TEXT],
                    'warnings': [str(w)[:1000] for w in document.get('warnings', [])[:20]]}
        project = self.scope(user, chat)[0]
        digest = hashlib.sha256(document['text'].encode()).hexdigest()
        old = self.db.execute('SELECT id,digest,version FROM wb_files WHERE user_id=? AND chat_id=? AND project_id=? AND name=? ORDER BY id DESC LIMIT 1',
                              (user, chat, project, document['name'])).fetchone()
        if old and old[1] == digest:
            return self.use(user, chat, old[0])
        count = self.db.execute('SELECT COUNT(*) FROM wb_files WHERE user_id=? AND chat_id=?', (user, chat)).fetchone()[0]
        total = self.db.execute('SELECT COUNT(*) FROM wb_files WHERE user_id=?', (user,)).fetchone()[0]
        if not importing and (count >= MAX_FILES or total >= MAX_USER_FILES):
            raise WorkspaceError('files_limit')
        with self.db:
            cur = self.db.execute('INSERT INTO wb_files(user_id,chat_id,project_id,name,version,digest,content,created) VALUES (?,?,?,?,?,?,?,?)',
                                  (user, chat, project, document['name'], old[2] + 1 if old else 1, digest,
                                   json.dumps(document, ensure_ascii=False), int(time.time())))
            self._scope(user, chat, project, cur.lastrowid)
        return self.file(user, chat)

    def delete_file(self, user, chat, file_id):
        document = self.file(user, chat, file_id)
        if not document:
            raise WorkspaceError('not_found')
        with self.db:
            self.db.execute('DELETE FROM wb_files WHERE id=? AND user_id=? AND chat_id=?', (file_id, user, chat))
            if self.scope(user, chat)[1] == file_id:
                self.open(user, chat, document['project_id'])
        return document['name']

    def delete_project(self, user, chat, project):
        item = self.project(user, chat, project)
        if not item:
            raise WorkspaceError('not_found')
        with self.db:
            self.db.execute('DELETE FROM wb_files WHERE user_id=? AND chat_id=? AND project_id=?', (user, chat, project))
            self.db.execute('DELETE FROM wb_projects WHERE user_id=? AND chat_id=? AND id=?', (user, chat, project))
            if self.scope(user, chat)[0] == project:
                self.open(user, chat, 0)
        return item['name']

    def reset(self, user=None):
        with self.db:
            for table in ('wb_files', 'wb_projects', 'wb_scopes'):
                self.db.execute(f'DELETE FROM {table}' + (' WHERE user_id=?' if user is not None else ''), (user,) if user is not None else ())

    def search(self, user, chat, query, limit=8):
        words = terms(query)
        if not words:
            return []
        hits = []
        for file in self.files(user, chat, latest=True):
            for chunk in chunks(file['document']['text']):
                rank = score(words, chunk['text'], file['name'])
                if rank:
                    hits.append({**chunk, 'score': rank, 'id': file['id'], 'name': file['name'], 'version': file['version']})
        hits.sort(key=lambda hit: (-hit['score'], -hit['id'], hit['start']))
        return hits[:limit]

    def context(self, user, chat, question, force_project=False):
        project = self.project(user, chat)
        selected = self.file(user, chat)
        if (not project or self.focused(user, chat)) and not force_project:
            return docreader.prompt(selected['document'], question) if selected else None
        files = self.files(user, chat, latest=True)
        if not files:
            return None
        words, candidates = terms(question), []
        index = []
        for file in files:
            index.append(f"#{file['id']} {file['name']} v{file['version']}")
            for chunk in chunks(file['document']['text']):
                candidates.append({**chunk, 'file': file, 'score': score(words, chunk['text'], file['name'])})
        candidates.sort(key=lambda c: (-c['score'], -c['file']['id'], c['start']))
        # First represent every file, then fill remaining space with the most relevant chunks.
        chosen, seen, size = [], set(), 0
        def take(chunk):
            nonlocal size
            key = (chunk['file']['id'], chunk['start'])
            if key in seen:
                return
            block = (f"\n[Source #{key[0]} {json.dumps(chunk['file']['name'], ensure_ascii=False)} v{chunk['file']['version']} | {chunk['location']}]\n"
                     + chunk['text'] + '\n')
            if size + len(block) <= CONTEXT_CHARS:
                chosen.append(block)
                seen.add(key)
                size += len(block)
        for file in files:
            best = next(c for c in candidates if c['file']['id'] == file['id'])
            take(best)
        for chunk in candidates:
            take(chunk)
        warnings = [f'Only selected excerpts are included ({len(seen)} of {len(candidates)} chunks); omitted sections were not inspected.']
        for file in files:
            warnings.extend(f"#{file['id']}: {w}" for w in file['document']['warnings'])
        result = {'name': f"Project: {project['name']}" if project else 'Selected files',
                  'text': '[File index]\n' + '\n'.join(index) + '\n' + ''.join(chosen), 'warnings': warnings}
        return docreader.prompt(result, question)

    def pair(self, user, chat, first=None, second=None):
        files = self.files(user, chat)
        if first is None and second is None:
            if len(files) < 2:
                raise WorkspaceError('two_files')
            first, second = files[1]['id'], files[0]['id']
        a, b = self.file(user, chat, first), self.file(user, chat, second)
        if not a or not b:
            raise WorkspaceError('not_found')
        if a['project_id'] != self.scope(user, chat)[0] or b['project_id'] != a['project_id']:
            raise WorkspaceError('same_project')
        return a, b

    def compare(self, user, chat, first=None, second=None):
        return compare_documents(*self.pair(user, chat, first, second))


def compare_documents(a, b):
    normalise = lambda text: re.sub(r'(?m)^(?:L\d+|Paragraph \d+): ', '', text)
    old, new = normalise(a['document']['text']), normalise(b['document']['text'])
    # A line-count bound keeps difflib from degenerating on adversarial tiny lines.
    old_lines, new_lines = old.splitlines()[:6000], new.splitlines()[:6000]
    differ = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=True)
    added = removed = 0
    for kind, i1, i2, j1, j2 in differ.get_opcodes():
        if kind in ('delete', 'replace'):
            removed += i2 - i1
        if kind in ('insert', 'replace'):
            added += j2 - j1
    output, size, cut = [], 0, False
    for line in difflib.unified_diff(old_lines, new_lines, fromfile=f"#{a['id']} {a['name']} v{a['version']}",
                                     tofile=f"#{b['id']} {b['name']} v{b['version']}", lineterm=''):
        if size + len(line) > 80_000:
            cut = True
            break
        output.append(line)
        size += len(line) + 1
    partial = cut or len(old.splitlines()) > 6000 or len(new.splitlines()) > 6000 or any(
        'truncat' in w.lower() or 'Only' in w for f in (a, b) for w in f['document']['warnings'])
    return {'first': a, 'second': b, 'added': added, 'removed': removed, 'partial': partial,
            'identical': old == new, 'diff': '\n'.join(output)}

DIAGNOSTICS = [
    (r'(?i)ModuleNotFoundError|No module named', 'Не найден Python-модуль', 'Проверь requirements.txt и установку зависимостей в том же окружении Python.'),
    (r'(?i)\b401\b|unauthorized|invalid.api.key|AuthenticationError', 'Ошибка авторизации', 'Проверь ключ и адрес API; не публикуй значение ключа.'),
    (r'(?i)\b429\b|RateLimit|too many requests', 'Лимит запросов', 'Проверь квоту и заголовок Retry-After; ограничь параллельные запросы.'),
    (r'(?i)insufficient.quota|insufficient.balance|no remaining credits', 'Недостаточно квоты/кредита', 'Проверь баланс и лимиты именно используемого ключа.'),
    (r'(?i)TimeoutError|TimedOut|APITimeoutError|ReadTimeout|ETIMEDOUT', 'Превышено время ожидания', 'Определи, какой запрос завис: сеть, API или обработка файла; проверь таймаут и ограниченные повторы.'),
    (r'(?i)database is locked|SQLITE_BUSY', 'SQLite занят', 'Проверь длинные транзакции, параллельные записи и timeout подключения.'),
    (r'(?i)PermissionError|Permission denied|EACCES', 'Нет прав доступа', 'Проверь путь и права пользователя процесса на конкретный файл или каталог.'),
    (r'(?i)SyntaxError|IndentationError|JSONDecodeError', 'Ошибка синтаксиса/формата', 'Проверь строку, указанную в traceback, отступы и ожидаемый формат данных.'),
    (r'(?i)409.*conflict|terminated by other getUpdates|another getUpdates', 'Возможен второй экземпляр Telegram-бота', 'Проверь, не запущен ли polling с тем же токеном в другом процессе.'),
    (r'(?i)disk.full|No space left on device|ENOSPC', 'Закончено место на диске', 'Проверь свободное место, журналы и временные файлы.'),
]


def diagnose(text):
    lines = redact(text).splitlines()
    found = []
    for pattern, title, check in DIAGNOSTICS:
        matches = [(i + 1, line[:400]) for i, line in enumerate(lines) if re.search(pattern, line)][:3]
        if matches:
            found.append({'title': title, 'check': check, 'matches': matches})
    return found


def diagnostic_text(text):
    hints = diagnose(text)
    return '\n\n'.join(f"{h['title']} (гипотеза, нужна проверка):\n" + '\n'.join(f"L{i}: {line}" for i, line in h['matches'])
                        + '\nЧто проверить: ' + h['check'] for h in hints) or 'Типовые шаблоны ошибок не найдены; это не подтверждает отсутствие ошибок.'


def docx_report(title, text):
    """Small standards-based Word document, with no executable content or external resources."""
    text = redact(text)[:60_000]
    def paragraph(line, heading=False):
        clean = ''.join(c for c in line if c in '\t\n\r' or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
        style = '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' if heading else ''
        return f'<w:p>{style}<w:r><w:t xml:space="preserve">{escape(clean)}</w:t></w:r></w:p>'
    body = paragraph(title, True) + ''.join(paragraph(line.lstrip('# ').rstrip(), line.startswith('#')) for line in text.splitlines())
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + body +
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr></w:body></w:document>')
    contents = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>'
    rels = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    styles = '<?xml version="1.0"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="30"/></w:rPr></w:style></w:styles>'
    doc_rels = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in {'[Content_Types].xml': contents, '_rels/.rels': rels, 'word/document.xml': document,
                           'word/styles.xml': styles, 'word/_rels/document.xml.rels': doc_rels}.items():
            archive.writestr(name, data.encode())
    return buffer.getvalue()
