"""Authenticated Mini App document, search, reminder, recovery and owner operations."""
import asyncio
import base64
import io
import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import codex
import docreader
import document_edit
import workbench


def record(data, model, started, error=""):
    stats = data.setdefault("service_metrics", {})
    key = str(model)[:120]
    previous = stats.get(key,{})
    stats[key] = {"model":key,"last_at":int(time.time()),"latency_ms":round((time.monotonic()-started)*1000),
                  "requests":previous.get("requests",0)+1,"errors":previous.get("errors",0)+bool(error),
                  "last_error":str(error)[:80]}


def tables(db):
    db.execute("CREATE TABLE IF NOT EXISTS app_originals (file_id INTEGER PRIMARY KEY, content BLOB NOT NULL)")
    db.execute("CREATE TRIGGER IF NOT EXISTS app_original_cleanup AFTER DELETE ON wb_files "
               "BEGIN DELETE FROM app_originals WHERE file_id=OLD.id; END")
    db.commit()


class Upload:
    def __init__(self,name,raw):
        self.file_name,self.raw,self.file_size=name,raw,len(raw)

    async def get_file(self):
        return self

    async def download_to_drive(self,path):
        await asyncio.to_thread(Path(path).write_bytes,self.raw)


def require_owner(app,uid):
    if app.bot_data["connections"].get_meta("owner") != uid:
        raise PermissionError("owner")


def positive(value):
    if type(value) is not int or value<=0:
        raise ValueError("Нужен положительный ID.")
    return value


def state(space,uid):
    files=space.files(uid,uid)
    return {"projects":space.projects(uid,uid),"project":space.scope(uid,uid)[0],
            "selected":space.scope(uid,uid)[1],
            "files":[{"id":f["id"],"name":f["name"],"version":f["version"],"warnings":f["document"]["warnings"]} for f in files]}


async def dispatch(app, uid, action, payload, answer):
    data=app.bot_data
    memory=data["memory"]
    space=memory.workbench
    owner_actions={"status","backups","backup","restore"}
    if action in owner_actions:
        require_owner(app,uid)
    if action=="status":
        snapshots=data.get("snapshots")
        metrics=list(data.get("service_metrics",{}).values())
        return {"version":__import__("hosting").version()[0],
                "free_bytes":shutil.disk_usage(Path(__file__).parent).free,
                "active_tasks":sum(not j.done() for jobs in (data.get("codex_jobs",{}),data.get("codex_apply_jobs",{})) for j in jobs.values()),
                "models":list(data.get("models",[])),"providers":metrics,
                "backup":{"enabled":bool(snapshots),"ready":bool(snapshots and snapshots.ready),
                          "last_success":snapshots.last_success if snapshots else None,
                          "last_error":snapshots.last_error if snapshots else ""}}
    if action in ("backups","backup","restore"):
        snapshots=data.get("snapshots")
        if not snapshots:
            raise ValueError("Резервирование не настроено: нужен DATABASE_URL.")
        if action=="restore":
            if payload.get("confirm") is not True:
                raise ValueError("Подтверди восстановление выбранной версии.")
            return await asyncio.to_thread(snapshots.stage_restore,positive(payload.get("id")))
        if action=="backup":
            await asyncio.to_thread(snapshots.save)
        return {"versions":await asyncio.to_thread(snapshots.versions)}
    if action=="history":
        query=str(payload.get("query") or "").strip().casefold()
        if not 2<=len(query)<=200:
            raise ValueError("Введи от 2 до 200 символов.")
        rows=memory.db.execute("SELECT id,role,content,created FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 3000",(uid,)).fetchall()
        hits=[]
        for mid,role,content,created in rows:
            index=content.casefold().find(query)
            if index>=0:
                hits.append({"id":mid,"role":role,"created":created,
                             "text":workbench.redact(content[max(0,index-150):index+850])})
            if len(hits)>=30:
                break
        return {"results":hits,"scope":"Сохранённая история личного диалога; удалённые сообщения недоступны."}
    if action=="reminders":
        return {"reminders":memory.daily.reminders(uid),
                "timezone":(memory.daily.profile(uid) or {}).get("tz","UTC")}
    if action=="remind":
        ident,when=positive(payload.get("id")),str(payload.get("when") or "")[:150]
        jid,at=memory.daily.reminder(uid,uid,ident,when)
        return {"id":jid,"at":at}
    if action=="cancel-reminder":
        if not memory.daily.cancel(uid,positive(payload.get("id"))):
            raise ValueError("Напоминание не найдено.")
        return {"ok":True}
    if action=="resume":
        store=data["codex"]
        task=store.get(str(payload.get("id") or ""),uid)
        if not task or task["status"]!=codex.STATUS_PAUSED:
            raise ValueError("Нет приостановленной задачи.")
        if any(t["status"]==codex.STATUS_RUNNING for t in store.list(uid)):
            raise ValueError("Сначала дождись текущей задачи.")
        store.update(task["id"],status=codex.STATUS_RUNNING,error="",resumed=True,stop_requested=False)
        # The original task and its files are reused; no extra charge for a restart.
        import bot
        language="ru" if str(payload.get("language") or "ru").startswith("ru") else "en"
        job=asyncio.create_task(bot.run_codex(app,task["id"],language))
        data["codex_jobs"][task["id"]]=job
        return {"task":codex.public(store.get(task["id"]))}
    if action=="apply":
        import bot
        store=data["codex"]
        tid=str(payload.get("id") or "")
        task=store.get(tid,uid)
        if not task or not task.get("change"):
            raise ValueError("Нет изменений для применения.")
        if task["status"]==codex.STATUS_APPLIED:
            return {"task":codex.public(task)}
        jobs=data.setdefault("codex_apply_jobs",{})
        if tid not in jobs or jobs[tid].done():
            if task["status"]!=codex.STATUS_DONE:
                raise ValueError("Задача пока не готова.")
            async def apply():
                try:
                    await bot.codex_apply(app,uid,tid)
                except Exception:
                    pass  # codex_apply records a safe user-facing error and allows an idempotent retry.
            store.update(tid,status="applying",error="")
            jobs[tid]=asyncio.create_task(apply())
        return {"task":codex.public(store.get(tid)),"pending":True}
    # Document actions share Telegram's private-chat project and ownership boundaries.
    tables(space.db)
    if action=="documents":
        return state(space,uid)
    if action=="project-create":
        space.create(uid,uid,str(payload.get("name") or ""))
        return state(space,uid)
    if action=="project-open":
        project=payload.get("id")
        if type(project) is not int or project<0:
            raise ValueError("project")
        space.open(uid,uid,project)
        return state(space,uid)
    if action=="file-use":
        space.use(uid,uid,positive(payload.get("id")))
        return state(space,uid)
    if action=="file-delete":
        if payload.get("confirm") is not True:
            raise ValueError("Подтверди удаление.")
        space.delete_file(uid,uid,positive(payload.get("id")))
        return state(space,uid)
    if action=="upload":
        name=Path(str(payload.get("name") or "").replace("\\","/")).name[:200]
        raw=base64.b64decode(str(payload.get("content") or ""),validate=True)
        if not raw or len(raw)>docreader.MAX_FILE:
            raise ValueError("Файл должен быть не больше 19 МБ.")
        document=await docreader.read_attachment(Upload(name,raw))
        item=space.add(uid,uid,document)
        with space.db:
            space.db.execute("INSERT INTO app_originals(file_id,content) VALUES (?,?) "
                             "ON CONFLICT(file_id) DO UPDATE SET content=excluded.content",(item["id"],raw))
        return state(space,uid)
    if action=="report":
        question=str(payload.get("question") or "Подготовь отчёт с выводами, источниками и ограничениями.")[:1500]
        context=space.context(uid,uid,question)
        if not context:
            raise ValueError("Сначала загрузи или выбери файл.")
        model=data.get("chat_default")
        if not model:
            raise ValueError("Модель временно недоступна.")
        result=await answer(data,model,"Write a document report in the user's language. Cite source locations, "
                            "state extraction limits; never claim to have inspected omitted content. "+docreader.RULES,context,max_tokens=4000)
        report=workbench.redact(result or "")
        if not report.strip():
            raise ValueError("Модель не вернула отчёт.")
        return {"text":report,"name":"secret-ai-report.docx",
                "content":base64.b64encode(workbench.docx_report("Secret AI — отчёт",report)).decode()}
    file=space.file(uid,uid,positive(payload.get("id")))
    if not file:
        raise ValueError("Файл не найден.")
    if action=="file-text":
        return {"name":file["name"]+".txt","content":base64.b64encode(file["document"]["text"].encode()).decode()}
    if action=="edit-document":
        row=space.db.execute("SELECT content FROM app_originals WHERE file_id=?",(file["id"],)).fetchone()
        if not row:
            raise ValueError("Для редактирования загрузи исходный DOCX/XLSX через Mini App.")
        raw,log=await asyncio.to_thread(document_edit.edited,bytes(row[0]),file["name"],payload.get("changes"))
        name=Path(file["name"]).stem+"-исправлено"+Path(file["name"]).suffix
        return {"name":name,"content":base64.b64encode(raw).decode(),"changes":log}
    raise ValueError("Неизвестное действие.")
