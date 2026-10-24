"""Durable, user-scoped everyday tasks, inbox and notification outbox."""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
MAX_TASKS, MAX_SOURCES, MAX_NOTES = 100, 300, 200
TABLES = ('dy_links', 'dy_notes', 'dy_jobs', 'dy_batches', 'dy_tasks', 'dy_sources', 'dy_profiles')


class DailyError(ValueError):
    pass


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise DailyError('timezone') from None


def clock(value):
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise DailyError('time')
    return value


def stamp(value, tz='UTC', now=None):
    """Explicit local date/time or relative delay, never the model's guess for a notification."""
    now = time.time() if now is None else now
    value = value.strip().lower()
    match = re.fullmatch(r'(?:через|in)\s+(\d{1,4})\s*(минут[уы]?|мин|minutes?|m|час(?:а|ов)?|hours?|h|дн(?:я|ей)|день|days?|d)', value)
    if match:
        n, unit = int(match[1]), match[2]
        scale = 60 if unit.startswith(('мин', 'min')) or unit == 'm' else 3600 if unit.startswith(('час', 'hour')) or unit == 'h' else 86400
        result = now + n * scale
    else:
        local = datetime.fromtimestamp(now, zone(tz))
        match = re.fullmatch(r'(сегодня|завтра|today|tomorrow)\s+(\d{2}:\d{2})', value)
        if match:
            day = local.date() + timedelta(days=match[1] in ('завтра', 'tomorrow'))
            clock(match[2])
            parsed = datetime.fromisoformat(str(day) + 'T' + match[2]).replace(tzinfo=zone(tz))
        else:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                raise DailyError('time') from None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=zone(tz))
        result = parsed.timestamp()
    if not now + 5 <= result <= now + 366 * 86400:
        raise DailyError('future')
    return int(result)


def json_object(raw):
    raw = raw.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0]
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise DailyError('model_format')
    return value


def text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ''


class Store:
    def __init__(self, db):
        self.db = db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS dy_profiles (
          user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, mode TEXT NOT NULL DEFAULT 'off',
          tz TEXT NOT NULL DEFAULT 'UTC', active_task INTEGER, morning TEXT, evening TEXT);
        CREATE TABLE IF NOT EXISTS dy_sources (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          message_id INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, attachment TEXT NOT NULL,
          created INTEGER NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
          UNIQUE(user_id,chat_id,message_id));
        CREATE TABLE IF NOT EXISTS dy_tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, title TEXT NOT NULL,
          details TEXT NOT NULL, next_step TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
          priority INTEGER NOT NULL DEFAULT 2, due INTEGER, created INTEGER NOT NULL,
          updated INTEGER NOT NULL, completed INTEGER);
        CREATE INDEX IF NOT EXISTS dy_tasks_user ON dy_tasks(user_id,status);
        CREATE TABLE IF NOT EXISTS dy_links (user_id INTEGER NOT NULL, task_id INTEGER NOT NULL,
          source_id INTEGER NOT NULL, PRIMARY KEY(user_id,task_id,source_id));
        CREATE TABLE IF NOT EXISTS dy_notes (id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL, task_id INTEGER NOT NULL, question TEXT NOT NULL,
          answer TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS dy_batches (user_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,
          result TEXT NOT NULL, PRIMARY KEY(user_id,fingerprint));
        CREATE TABLE IF NOT EXISTS dy_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, task_id INTEGER NOT NULL DEFAULT 0,
          kind TEXT NOT NULL, run_at INTEGER NOT NULL, event_key TEXT UNIQUE,
          status TEXT NOT NULL DEFAULT 'pending', tries INTEGER NOT NULL DEFAULT 0,
          lease_until INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS dy_due ON dy_jobs(status,run_at);
        ''')
        if "recurrence" not in {r[1] for r in db.execute("PRAGMA table_info(dy_jobs)")}:
            db.execute("ALTER TABLE dy_jobs ADD COLUMN recurrence TEXT NOT NULL DEFAULT ''")
            db.commit()

    def _one(self, sql, args=()):
        cur = self.db.execute(sql, args)
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else None

    def _many(self, sql, args=()):
        cur = self.db.execute(sql, args)
        return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

    def profile(self, user, chat=None):
        row = self._one('SELECT * FROM dy_profiles WHERE user_id=?', (user,))
        if not row and chat is not None:
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO dy_profiles(user_id,chat_id) VALUES (?,?)', (user, chat))
            row = self.profile(user)
        return row

    def configure(self, user, chat, **changes):
        self.profile(user, chat)
        if set(changes) - {'mode', 'tz', 'active_task', 'morning', 'evening'}:
            raise DailyError('arguments')
        if 'tz' in changes:
            zone(changes['tz'])
        if 'mode' in changes and changes['mode'] not in ('off', 'auto', 'inbox'):
            raise DailyError('arguments')
        for k in ('morning', 'evening'):
            if changes.get(k) is not None:
                clock(changes[k])
        if changes.get('active_task') is not None:
            task = self.task(user, changes['active_task'])
            if not task or task['status'] != 'open':
                raise DailyError('task')
        with self.db:
            for key, value in changes.items():
                self.db.execute(f'UPDATE dy_profiles SET {key}=?,chat_id=? WHERE user_id=?', (value, chat, user))
            if any(k in changes for k in ('morning', 'evening', 'tz')):
                self.db.execute("DELETE FROM dy_jobs WHERE user_id=? AND kind!='reminder' AND status='pending'", (user,))
        return self.profile(user)

    def source(self, user, source_id):
        row = self._one('SELECT * FROM dy_sources WHERE user_id=? AND id=?', (user, source_id))
        if row:
            row['attachment'] = json.loads(row['attachment'])
        return row

    def add_source(self, user, chat, message, kind, content, attachment=None):
        existing = self._one('SELECT id FROM dy_sources WHERE user_id=? AND chat_id=? AND message_id=?', (user, chat, message))
        if existing:
            return self.source(user, existing['id'])
        if self.db.execute('SELECT COUNT(*) FROM dy_sources WHERE user_id=?', (user,)).fetchone()[0] >= MAX_SOURCES:
            raise DailyError('sources_limit')
        with self.db:
            cur = self.db.execute('INSERT INTO dy_sources(user_id,chat_id,message_id,kind,text,attachment,created) VALUES (?,?,?,?,?,?,?)',
                                  (user, chat, message, kind, text(content, 60000), json.dumps(attachment or {}), int(time.time())))
        return self.source(user, cur.lastrowid)

    def delete_source(self,user,source_id):
        if not self.source(user,source_id):
            raise DailyError('task')
        with self.db:
            self.db.execute('DELETE FROM dy_links WHERE user_id=? AND source_id=?',(user,source_id))
            self.db.execute('DELETE FROM dy_sources WHERE user_id=? AND id=?',(user,source_id))

    def inbox(self, user):
        return self._many('SELECT id,kind,text,created FROM dy_sources WHERE user_id=? AND processed=0 ORDER BY id LIMIT 12', (user,))

    def task(self, user, task_id):
        return self._one('SELECT * FROM dy_tasks WHERE user_id=? AND id=?', (user, task_id))

    def tasks(self, user, status=None):
        clause, args = (' AND status=?', (user, status)) if status else ('', (user,))
        return self._many('SELECT * FROM dy_tasks WHERE user_id=?' + clause + ' ORDER BY status,COALESCE(due,9999999999),priority,id', args)

    def find(self, user, query):
        if query.strip().lstrip('#').isdigit():
            task = self.task(user,int(query.strip().lstrip('#')))
            return [task] if task and task['status']=='open' else []
        words = [re.sub(r'(?:ами|ями|ого|ему|ом|ем|ой|ей|ах|ях|ами|а|у|е|ы|и)$','',w) if len(w)>4 else w
                 for w in re.findall(r'\w{3,}', query.lower()) if w not in {'что','там','вернемся','вернёмся','продолжи','задача','задаче'}]
        ranked = []
        for task in self.tasks(user, 'open'):
            hay = (task['title'] + ' ' + task['details']).lower()
            score = sum(w in hay for w in words)
            if score:
                ranked.append((score, task))
        ranked.sort(key=lambda pair: -pair[0])
        return [task for _, task in ranked[:6]]

    def commit_batch(self, user, sources, result):
        ids = [row['id'] for row in sources]
        if not ids:
            return []
        key = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        old = self._one('SELECT result FROM dy_batches WHERE user_id=? AND fingerprint=?', (user, key))
        if old:
            return [self.task(user, i) for i in json.loads(old['result']) if self.task(user, i)]
        tasks = result.get('tasks')
        if not isinstance(tasks, list) or len(tasks) > 8:
            raise DailyError('model_format')
        validated = []
        tz = (self.profile(user) or {}).get('tz', 'UTC')
        for row in tasks:
            if not isinstance(row, dict) or not text(row.get('title'), 120):
                raise DailyError('model_format')
            refs = row.get('sources')
            if not isinstance(refs, list) or not refs or any(type(i) is not int or i not in ids for i in refs):
                raise DailyError('model_format')
            due = None
            if row.get('due'):
                try:
                    date = datetime.fromisoformat(str(row['due']))
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=zone(tz))
                    if 2000 <= date.year <= 2100:
                        due = int(date.timestamp())  # past deadlines remain visible, no notification is scheduled
                except (ValueError,TypeError,OverflowError):
                    pass
            priority = row.get('priority', 2)
            priority = priority if type(priority) is int and priority in (1, 2, 3) else 2
            existing_id = row.get('task_id')
            if existing_id is not None:
                existing = self.task(user,existing_id) if type(existing_id) is int else None
                if not existing or existing['status'] != 'open':
                    raise DailyError('model_format')
            validated.append((text(row['title'],120), text(row.get('details'),1800), text(row.get('next_step'),1000), priority, due, refs, existing_id))
        if len(self.tasks(user)) + sum(row[-1] is None for row in validated) > MAX_TASKS:
            raise DailyError('tasks_limit')
        now, created = int(time.time()), []
        with self.db:
            # Never commit an obsolete or foreign snapshot after a model request.
            for source_id in ids:
                src = self.source(user, source_id)
                if not src or src['processed']:
                    raise DailyError('changed')
            for title, details, step, priority, due, refs, existing_id in validated:
                if existing_id is None:
                    cur = self.db.execute('INSERT INTO dy_tasks(user_id,title,details,next_step,priority,due,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                                          (user,title,details,step,priority,due,now,now))
                    task_id = cur.lastrowid
                else:
                    task_id = existing_id
                    old_task = self.task(user,task_id)
                    self.db.execute('UPDATE dy_tasks SET details=?,next_step=?,due=?,updated=? WHERE user_id=? AND id=?',
                                    (details or old_task['details'],step or old_task['next_step'],due or old_task['due'],now,user,task_id))
                if task_id not in created:
                    created.append(task_id)
                for source_id in set(refs):
                    self.db.execute('INSERT OR IGNORE INTO dy_links VALUES (?,?,?)', (user,task_id,source_id))
            for source_id in ids:
                self.db.execute('UPDATE dy_sources SET processed=1 WHERE user_id=? AND id=?', (user,source_id))
            self.db.execute('INSERT INTO dy_batches VALUES (?,?,?)', (user,key,json.dumps(created)))
        return [self.task(user, i) for i in created]

    def status(self, user, task_id, status):
        if status not in ('open','done','paused') or not self.task(user, task_id):
            raise DailyError('task')
        now = int(time.time())
        with self.db:
            self.db.execute('UPDATE dy_tasks SET status=?,updated=?,completed=? WHERE user_id=? AND id=?',
                            (status,now,now if status=='done' else None,user,task_id))
            if status != 'open':
                self.db.execute('UPDATE dy_profiles SET active_task=NULL WHERE user_id=? AND active_task=?', (user,task_id))
                self.db.execute("UPDATE dy_jobs SET status='cancelled' WHERE user_id=? AND task_id=? AND status='pending'", (user,task_id))

    def delete(self, user, task_id):
        if not self.task(user, task_id):
            raise DailyError('task')
        with self.db:
            for table in ('dy_links','dy_notes','dy_jobs'):
                self.db.execute(f'DELETE FROM {table} WHERE user_id=? AND task_id=?', (user,task_id))
            self.db.execute('DELETE FROM dy_tasks WHERE user_id=? AND id=?', (user,task_id))
            self.db.execute('UPDATE dy_profiles SET active_task=NULL WHERE user_id=? AND active_task=?', (user,task_id))

    def link(self, user, task_id, source_id):
        task, source = self.task(user, task_id), self.source(user, source_id)
        if not task or not source or task['status'] != 'open':
            raise DailyError('task')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO dy_links VALUES (?,?,?)', (user,task_id,source_id))
            self.db.execute('UPDATE dy_sources SET processed=1 WHERE user_id=? AND id=?', (user,source_id))

    def materials(self, user, task_id):
        if not self.task(user, task_id):
            raise DailyError('task')
        ids = self.db.execute('SELECT source_id FROM dy_links WHERE user_id=? AND task_id=? ORDER BY source_id', (user,task_id)).fetchall()
        return [self.source(user, row[0]) for row in ids if self.source(user, row[0])]

    def notes(self, user, task_id):
        return self._many('SELECT question,answer,created FROM dy_notes WHERE user_id=? AND task_id=? ORDER BY id DESC LIMIT 8', (user,task_id))[::-1]

    def save_answer(self, user, task_id, question, answer, step):
        task = self.task(user, task_id)
        if not task or task['status'] != 'open':
            raise DailyError('task')
        if self.db.execute('SELECT COUNT(*) FROM dy_notes WHERE user_id=? AND task_id=?', (user,task_id)).fetchone()[0] >= MAX_NOTES:
            raise DailyError('notes_limit')
        with self.db:
            self.db.execute('INSERT INTO dy_notes(user_id,task_id,question,answer,created) VALUES (?,?,?,?,?)',
                            (user,task_id,text(question,6000),text(answer,12000),int(time.time())))
            self.db.execute('UPDATE dy_tasks SET next_step=?,updated=? WHERE user_id=? AND id=?',
                            (text(step,1000) or task['next_step'],int(time.time()),user,task_id))

    def save_bundle(self,user,allowed_ids,result):
        rows=result.get('tasks')
        if not isinstance(rows,list) or len(rows)>len(allowed_ids):
            raise DailyError('model_format')
        checked=[]
        seen=set()
        for row in rows:
            if not isinstance(row,dict) or type(row.get('id')) is not int or row['id'] not in allowed_ids or row['id'] in seen:
                raise DailyError('model_format')
            task=self.task(user,row['id'])
            answer=text(row.get('answer'),5000)
            if not task or task['status']!='open' or not answer:
                raise DailyError('model_format')
            count=self.db.execute('SELECT COUNT(*) FROM dy_notes WHERE user_id=? AND task_id=?',(user,row['id'])).fetchone()[0]
            if count>=MAX_NOTES:
                raise DailyError('notes_limit')
            checked.append((row['id'],answer,text(row.get('next_step'),1000) or task['next_step']))
            seen.add(row['id'])
        if not checked:
            raise DailyError('model_format')
        now=int(time.time())
        with self.db:
            for task_id,answer,step in checked:
                self.db.execute('INSERT INTO dy_notes(user_id,task_id,question,answer,created) VALUES (?,?,?,?,?)',
                                (user,task_id,'Подготовь меня к завтрашнему дню',answer,now))
                self.db.execute('UPDATE dy_tasks SET next_step=?,updated=? WHERE user_id=? AND id=?',(step,now,user,task_id))
        return checked

    def context(self, user, task_id):
        task = self.task(user,task_id)
        if not task:
            raise DailyError('task')
        # Bound each source while preserving all filenames/IDs; recent conversation stays task-local.
        materials = self.materials(user,task_id)
        selected = materials if len(materials)<=8 else materials[:1]+materials[-7:]
        sources = [dict(id=s['id'], kind=s['kind'], name=s['attachment'].get('name'), text=s['text'][:1500],
                        truncated=len(s['text'])>1500) for s in selected]
        notes = [dict(question=n['question'][:600],answer=n['answer'][:1800],created=n['created'])
                 for n in self.notes(user,task_id)[-4:]]
        return json.dumps(dict(task=task,sources=sources,recent=notes,omitted_materials=len(materials)-len(selected)),ensure_ascii=False)

    def reminder(self, user, chat, task_id, when):
        task = self.task(user,task_id)
        if not task or task['status'] != 'open':
            raise DailyError('task')
        profile = self.profile(user,chat)
        import recurring
        rule = recurring.parse(when,profile['tz'])
        at = recurring.next_at(rule,time.time()) if rule else stamp(when,profile['tz'])
        if self.db.execute("SELECT COUNT(*) FROM dy_jobs WHERE user_id=? AND kind='reminder' AND status IN ('pending','leased')", (user,)).fetchone()[0] >= 20:
            raise DailyError('reminders_limit')
        with self.db:
            cur = self.db.execute("INSERT INTO dy_jobs(user_id,chat_id,task_id,kind,run_at,recurrence) VALUES (?,?,?,'reminder',?,?)",
                                  (user,chat,task_id,at,json.dumps(rule) if rule else ''))
        return cur.lastrowid,at

    def reminders(self,user):
        return self._many("SELECT * FROM dy_jobs WHERE user_id=? AND kind='reminder' AND status IN ('pending','leased') ORDER BY run_at", (user,))

    def cancel(self,user,job):
        with self.db:
            return bool(self.db.execute("UPDATE dy_jobs SET status='cancelled' WHERE user_id=? AND id=? AND status IN ('pending','leased')", (user,job)).rowcount)

    def schedule_routines(self, now=None):
        now = int(time.time() if now is None else now)
        with self.db:
            self.db.execute("DELETE FROM dy_jobs WHERE status IN ('sent','failed','cancelled') AND run_at<?", (now-30*86400,))
        for p in self._many('SELECT * FROM dy_profiles WHERE morning IS NOT NULL OR evening IS NOT NULL'):
            local = datetime.fromtimestamp(now,zone(p['tz']))
            for kind in ('morning','evening'):
                if not p[kind]:
                    continue
                at = datetime.fromisoformat(f"{local.date()}T{p[kind]}").replace(tzinfo=zone(p['tz'])).timestamp()
                if at <= now < at + 7200:
                    key = f"{p['user_id']}:{kind}:{local.date()}"
                    with self.db:
                        self.db.execute('INSERT OR IGNORE INTO dy_jobs(user_id,chat_id,kind,run_at,event_key) VALUES (?,?,?,?,?)',
                                        (p['user_id'],p['chat_id'],kind,int(at),key))

    def claim(self, now=None):
        now = int(time.time() if now is None else now)
        with self.db:
            job = self._one("UPDATE dy_jobs SET status='leased',tries=tries+1,lease_until=? WHERE id=(SELECT id FROM dy_jobs WHERE (status='pending' AND run_at<=?) OR (status='leased' AND lease_until<=?) ORDER BY run_at,id LIMIT 1) RETURNING *", (now+120,now,now))
        return job

    def valid_job(self, job):
        row = self._one('SELECT * FROM dy_jobs WHERE id=? AND user_id=?', (job['id'],job['user_id']))
        if not row or row['status'] != 'leased':
            return False
        if job['task_id']:
            task = self.task(job['user_id'],job['task_id'])
            return bool(task and task['status']=='open')
        p = self.profile(job['user_id'])
        return bool(p and p.get(job['kind']))

    def finish_job(self, job, error=None, permanent=False):
        now = int(time.time())
        state = 'sent' if error is None else 'failed' if permanent or job['tries']>=4 else 'pending'
        run_at = now + 60 * job['tries']
        tries = job['tries']
        if error is None and job.get('recurrence'):
            import recurring
            state, run_at, tries = 'pending', recurring.next_at(job['recurrence'], now), 0
        with self.db:
            self.db.execute("UPDATE dy_jobs SET status=?,run_at=?,tries=?,error=? WHERE id=? AND user_id=? AND status='leased'",
                            (state,run_at,tries,text(error,80),job['id'],job['user_id']))

    def snooze(self, user, job_id, seconds=900):
        row = self._one("SELECT * FROM dy_jobs WHERE user_id=? AND id=? AND kind='reminder'", (user,job_id))
        if not row or row['status'] not in ('sent','pending') or not self.task(user,row['task_id']) or self.task(user,row['task_id'])['status'] != 'open':
            raise DailyError('task')
        if seconds not in (900,3600,86400):
            raise DailyError('time')
        with self.db:
            self.db.execute("UPDATE dy_jobs SET status='pending',tries=0,run_at=? WHERE id=? AND user_id=?",
                            (int(time.time())+seconds,job_id,user))
        return True

    def plan(self,user,kind='plan',now=None):
        now = time.time() if now is None else now
        p = self.profile(user) or {'tz':'UTC'}
        local = datetime.fromtimestamp(now,zone(p['tz']))
        rows = self.tasks(user)
        if kind == 'review':
            done = [t for t in rows if t['completed'] and datetime.fromtimestamp(t['completed'],zone(p['tz'])).date()==local.date()]
            lines = [f"Итоги {local:%d.%m} · {p['tz']}", f"Завершено за день: {len(done)}"]
            lines += [f"✓ #{t['id']} {t['title']}" for t in done[:12]]
            lines.append('\nОсталось открытых дел: ' + str(sum(t['status']=='open' for t in rows)))
        else:
            lines = [f"План на {local:%d.%m} · {p['tz']}"]
        open_tasks = [t for t in rows if t['status']=='open']
        for task in open_tasks[:12]:
            due = (' · срок ' + datetime.fromtimestamp(task['due'],zone(p['tz'])).strftime('%d.%m %H:%M')) if task['due'] else ''
            lines += [f"\n#{task['id']} {task['title']}{due}", 'Следующий шаг: ' + (task['next_step'] or 'Открой задачу и уточни следующий шаг.')]
        if not open_tasks:
            lines.append('Открытых задач нет. /life on — начать собирать дела.')
        return '\n'.join(lines)[:3800]

    def reset(self, user=None):
        with self.db:
            for table in TABLES:
                self.db.execute('DELETE FROM '+table+(' WHERE user_id=?' if user is not None else ''), (user,) if user is not None else ())
