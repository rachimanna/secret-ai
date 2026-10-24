import asyncio
import base64
import io
import json
import sqlite3
import tempfile
import time
import unittest
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import bot
import codex
import daily
import docreader
import document_edit
import integrations
import persist
import recurring
import release_features as features
import workbench


class ReminderRegressionTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.store=daily.Store(self.db)
        self.store.configure(1,1,tz="Europe/Moscow")
        source=self.store.add_source(1,1,1,"text","Сдать отчёт")
        self.task=self.store.commit_batch(1,[source],{"tasks":[{"title":"Отчёт","sources":[source["id"]]}]})[0]

    def test_changed_routine_replaces_pending_slot_on_same_day(self):
        fixed=datetime(2026,10,26,9,5,tzinfo=timezone.utc).timestamp()
        self.store.configure(1,1,tz="UTC",morning="09:00")
        self.store.schedule_routines(fixed)
        self.store.configure(1,1,morning="10:00")
        self.store.schedule_routines(fixed+3600)
        jobs=self.store._many("SELECT * FROM dy_jobs WHERE status='pending'")
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]["run_at"],int(fixed+3300))

    def test_completed_routine_not_sent_twice_after_edit(self):
        fixed=datetime(2026,10,26,9,5,tzinfo=timezone.utc).timestamp()
        self.store.configure(1,1,tz="UTC",morning="09:00")
        self.store.schedule_routines(fixed)
        job=self.store.claim(fixed)
        self.store.finish_job(job)
        self.store.configure(1,1,morning="10:00")
        self.store.schedule_routines(fixed+3600)
        self.assertEqual(len(self.store._many("SELECT * FROM dy_jobs")),1)

    def test_repeating_reminder_advances_and_done_cancels_it(self):
        ident,at=self.store.reminder(1,1,self.task["id"],"каждый понедельник в 10:00")
        job=self.store.claim(at+1)
        self.assertEqual(job["id"],ident)
        with patch("daily.time.time",return_value=at+1):
            self.store.finish_job(job)
        next_job=self.store.reminders(1)[0]
        self.assertGreater(next_job["run_at"],at+6*86400)
        self.assertEqual(next_job["tries"],0)
        self.store.status(1,self.task["id"],"done")
        self.assertEqual(self.store.reminders(1),[])

    def test_weekdays_skip_weekend_in_user_timezone(self):
        friday=datetime(2026,10,23,19,tzinfo=daily.zone("Europe/Moscow")).timestamp()
        rule=recurring.parse("по будням в 18:00","Europe/Moscow")
        target=datetime.fromtimestamp(recurring.next_at(rule,friday),daily.zone("Europe/Moscow"))
        self.assertEqual((target.weekday(),target.hour),(0,18))

    def test_snooze_is_owner_scoped_and_keeps_recurrence(self):
        ident,_=self.store.reminder(1,1,self.task["id"],"каждый день в 09:00")
        with self.assertRaises(daily.DailyError):
            self.store.snooze(2,ident)
        self.store.snooze(1,ident)
        self.assertTrue(self.store.reminders(1)[0]["recurrence"])

    def test_schema_upgrade_preserves_existing_jobs(self):
        self.db.execute("ALTER TABLE dy_jobs DROP COLUMN recurrence")
        self.store.reminders(1)
        daily.Store(self.db)
        self.assertIn("recurrence",{r[1] for r in self.db.execute("PRAGMA table_info(dy_jobs)")})


class OfficeCopyTest(unittest.TestCase):
    def test_word_replacement_spanning_runs_preserves_original(self):
        raw=workbench.docx_report("Title","before target after")
        edited,log=document_edit.edited(raw,"test.docx",[{"find":"target","replace":"fixed"}])
        self.assertNotEqual(raw,edited)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"copy.docx";path.write_bytes(edited)
            self.assertIn("before fixed after",docreader.extract(path,"copy.docx")["text"])
        self.assertEqual(len(log),1)

    def test_word_ambiguous_edit_is_rejected(self):
        raw=workbench.docx_report("Title","same same")
        with self.assertRaises(ValueError):
            document_edit.edited(raw,"test.docx",[{"find":"same","replace":"new"}])

    def test_xlsx_edit_retains_formulas_and_other_entries(self):
        import zipfile
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,"w") as z:
            z.writestr("xl/workbook.xml",'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" r:id="r1"/></sheets></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels",'<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr("xl/worksheets/sheet1.xml",'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><v>2</v></c><c r="B1"><f>A1*2</f><v>4</v></c></row></sheetData></worksheet>')
            z.writestr("preserve.txt","untouched")
        original=raw.getvalue()
        result,_=document_edit.edited(original,"book.xlsx",[{"sheet":"Sheet1","cell":"A1","value":"3","type":"number"}])
        with zipfile.ZipFile(io.BytesIO(result)) as z:
            text=z.read("xl/worksheets/sheet1.xml").decode()
            self.assertIn(">3<",text)
            self.assertIn("A1*2",text)
            self.assertEqual(z.read("preserve.txt"),b"untouched")
        with self.assertRaises(ValueError):
            document_edit.edited(original,"book.xlsx",[{"sheet":"Sheet1","cell":"B1","value":"9"}])


class SnapshotSafetyTest(unittest.TestCase):
    def test_failed_restore_cannot_be_followed_by_save(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshots=persist.Snapshots("unused",Path(folder)/"memory.db")
            with patch.object(snapshots,"_connect",side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    snapshots.restore()
            self.assertFalse(snapshots.ready)
            with self.assertRaises(RuntimeError):
                snapshots.save()

    def test_corrupt_snapshot_is_rejected(self):
        with self.assertRaises((ValueError,sqlite3.DatabaseError)):
            persist.unpack(zlib.compress(b"not sqlite"))

    def test_trailing_and_truncated_compressed_data_rejected(self):
        with self.assertRaises(ValueError):
            persist.unpack(zlib.compress(b"data")+b"extra")
        with self.assertRaises(ValueError):
            persist.unpack(zlib.compress(b"data")[:-2])

    def test_existing_local_database_survives_postgres_outage(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"memory.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE x(id INTEGER)")
            snapshots=persist.Snapshots("unused",path)
            with patch.object(snapshots,"_connect",side_effect=OSError("offline")):
                self.assertFalse(snapshots.restore())
            self.assertTrue(snapshots.ready)


class FeatureBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.memory=bot.Memory(":memory:")
        self.addCleanup(self.memory.close)
        self.app=NS(bot_data={"memory":self.memory,"connections":NS(get_meta=lambda k:1),"locks":defaultdict(asyncio.Lock)})
        self.answer=AsyncMock(return_value="Report")

    async def test_history_never_reads_another_users_messages(self):
        self.memory.append_turn(1,"server mine","answer")
        self.memory.append_turn(2,"server private","secret")
        result=await features.dispatch(self.app,1,"history",{"query":"server","user_id":2},self.answer)
        self.assertEqual(len(result["results"]),1)
        self.assertNotIn("private",str(result))

    async def test_owner_operations_reject_other_users_before_access(self):
        for action in ("status","backups","backup","restore"):
            with self.assertRaises(PermissionError):
                await features.dispatch(self.app,2,action,{},self.answer)

    async def test_document_upload_extract_export_and_cleanup(self):
        result=await features.dispatch(self.app,1,"upload",
            {"name":"notes.txt","content":base64.b64encode(b"hello project").decode()},self.answer)
        ident=result["files"][0]["id"]
        text=await features.dispatch(self.app,1,"file-text",{"id":ident},self.answer)
        self.assertIn(b"hello project",base64.b64decode(text["content"]))
        with self.assertRaises(ValueError):
            await features.dispatch(self.app,2,"file-text",{"id":ident},self.answer)
        await features.dispatch(self.app,1,"file-delete",{"id":ident,"confirm":True},self.answer)
        self.assertEqual(self.memory.db.execute("SELECT COUNT(*) FROM app_originals").fetchone()[0],0)

    async def test_document_delete_requires_explicit_confirmation(self):
        with self.assertRaises(ValueError):
            await features.dispatch(self.app,1,"file-delete",{"id":1},self.answer)

    async def test_bridge_timeout_cancels_pending_coroutine(self):
        bridge=bot.Bridge(NS(bot_data={"loop":Mock()}))
        future=Mock()
        future.result.side_effect=TimeoutError
        coro=asyncio.sleep(0)
        try:
            with patch("bot.asyncio.run_coroutine_threadsafe",return_value=future):
                with self.assertRaises(TimeoutError):
                    bridge._run(coro)
            future.cancel.assert_called_once()
        finally:
            coro.close()


class GitHubRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_delete_binary_does_not_try_utf8_decoding(self):
        pending={}
        store=NS(get=lambda user,service:[{"token":"test","meta":{"login":"owner"}}] if service=="github" else [])
        tools=integrations.Toolbox(None,store,pending,1)
        gh=NS(branch_sha=AsyncMock(return_value="head"),tree=AsyncMock(return_value=("tree",{"a.png":("blob","100644")},False)),
              text=AsyncMock(side_effect=integrations.GitHubError("binary")))
        tools._default_branch=AsyncMock(return_value="main")
        await tools._propose(gh,"owner/repo",{"files":[{"path":"a.png","action":"delete"}]})
        gh.text.assert_not_awaited()
        change=next(iter(pending.values()))
        self.assertIsNone(change["files"][0]["new"])

    async def test_concurrent_apply_runs_only_once_and_returns_same_result(self):
        with tempfile.TemporaryDirectory() as folder:
            connections=integrations.Connections(Path(folder)/"db","key")
            self.addCleanup(connections.close)
            store=codex.CodexStore(connections)
            task=store.create(1,"repo","fix",repo="o/r")
            store.update(task["id"],status=codex.STATUS_DONE,change={"user_id":1})
            app=NS(bot_data={"codex":store,"connections":connections,"github_apps":NS(fresh_token=AsyncMock()),"http":None})
            async def apply(*args,**kwargs):
                await asyncio.sleep(0.02)
                return "commit","pull"
            with patch("bot.apply_change",new=AsyncMock(side_effect=apply)) as mocked:
                results=await asyncio.gather(bot.codex_apply(app,1,task["id"]),bot.codex_apply(app,1,task["id"]))
            self.assertEqual(mocked.await_count,1)
            self.assertTrue(all(r["status"]==codex.STATUS_APPLIED for r in results))

    async def test_pr_retry_uses_saved_commit(self):
        gh=NS(call=AsyncMock(return_value=[{"html_url":"pull"}]))
        change={"user_id":1,"repo":"o/r","branch":"fix","default":"main","pull_request":True,
                "message":"Fix","applied_commit":{"sha":"sha","html_url":"commit"}}
        with patch("integrations.GitHub",return_value=gh):
            result=await integrations.apply_change(None,NS(get=lambda *a:[{"token":"test"}]),change)
        self.assertEqual(result,("commit","pull"))
        self.assertEqual(gh.call.await_count,1)

    async def test_restart_pauses_task_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as folder:
            conn=integrations.Connections(Path(folder)/"db","key")
            self.addCleanup(conn.close)
            store=codex.CodexStore(conn)
            task=store.create(1,"project","build",files={"main.py":"print(1)"})
            restarted=codex.CodexStore(conn)
            saved=restarted.get(task["id"],1)
            self.assertEqual(saved["status"],codex.STATUS_PAUSED)
            self.assertEqual(saved["files"],{"main.py":"print(1)"})


class BootstrapRegressionTest(unittest.TestCase):
    def namespace(self):
        import ast
        source=Path("deploy/start.py").read_text()
        tree=ast.parse(source)
        tree.body=[node for node in tree.body if not (isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)
                   and isinstance(node.value.func,ast.Name) and node.value.func.id=="main")]
        namespace={"__name__":"bootstrap_test"}
        exec(compile(tree,"deploy/start.py","exec"),namespace)
        return namespace

    def test_archive_fallback_respects_requested_branch(self):
        namespace=self.namespace()
        namespace["branch"]=lambda:"stable"
        import zipfile
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,"w") as z:
            z.writestr("secret-ai-stable/hello.txt","ok")
        with tempfile.TemporaryDirectory() as folder:
            namespace["HOME"]=folder
            with patch("shutil.which",return_value=None),patch("urllib.request.urlopen",return_value=io.BytesIO(archive.getvalue())) as fetched:
                namespace["fetch_code"]()
            self.assertIn("/stable.zip",fetched.call_args.args[0])
            self.assertEqual((Path(folder)/"hello.txt").read_text(),"ok")

    def test_checkout_failure_never_reports_success(self):
        namespace=self.namespace()
        namespace["branch"]=lambda:"stable"
        namespace["git"]=Mock(side_effect=[NS(returncode=0),NS(returncode=0),NS(returncode=0),NS(returncode=1,stderr="denied")])
        with tempfile.TemporaryDirectory() as folder,patch("shutil.which",return_value="git"):
            namespace["HOME"]=folder
            with self.assertRaises(RuntimeError):
                namespace["fetch_code"]()

    def test_install_failure_aborts_instead_of_starting_broken_bot(self):
        namespace=self.namespace()
        with patch("subprocess.run",return_value=NS(returncode=1)):
            with self.assertRaises(RuntimeError):
                namespace["install"]()


@unittest.skipUnless(__import__("os").environ.get("TEST_SNAPSHOT_DATABASE_URL"),"requires isolated test Postgres")
class PostgresSnapshotIntegrationTest(unittest.TestCase):
    def test_versions_restore_and_pending_request_survive_fresh_machine(self):
        import os
        import uuid
        url=os.environ["TEST_SNAPSHOT_DATABASE_URL"]
        name="test-"+uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"memory.db"
            snapshots=persist.Snapshots(url,path,name)
            self.assertFalse(snapshots.restore())
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE values_test(value TEXT)")
                db.execute("INSERT INTO values_test VALUES ('first')")
            self.assertTrue(snapshots.save())
            old=snapshots.versions()[0]["id"]
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO values_test VALUES ('second')")
            self.assertTrue(snapshots.save())
            self.assertEqual(len(snapshots.versions()),2)
            snapshots.stage_restore(old)
            # Emulate a redeploy with no local disk: restore intent is in Postgres.
            fresh_path=Path(folder)/"fresh.db"
            fresh=persist.Snapshots(url,fresh_path,name)
            self.assertTrue(fresh.restore())
            with sqlite3.connect(fresh_path) as db:
                self.assertEqual(db.execute("SELECT value FROM values_test").fetchall(),[("first",)])
            self.assertTrue(fresh.ready)
            with fresh._connect() as conn:
                self.assertIsNone(conn.execute("SELECT data FROM bot_snapshot_restore WHERE name=%s",(name,)).fetchone())
                for table in ("bot_snapshot_versions","bot_snapshots"):
                    conn.execute("DELETE FROM "+table+" WHERE name=%s",(name,))

    def test_invalid_pending_restore_fails_closed_even_with_existing_database(self):
        import os
        import uuid
        name="test-"+uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"memory.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE x(id INTEGER)")
            snapshots=persist.Snapshots(os.environ["TEST_SNAPSHOT_DATABASE_URL"],path,name)
            snapshots.restore()
            with snapshots._connect() as conn:
                conn.execute("INSERT INTO bot_snapshot_restore(name,data) VALUES (%s,%s)",(name,zlib.compress(b"broken")))
            with self.assertRaises(ValueError):
                persist.Snapshots(snapshots.url,path,name).restore()
            with snapshots._connect() as conn:
                conn.execute("DELETE FROM bot_snapshot_restore WHERE name=%s",(name,))


class CodexRestartAccountingTest(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_pauses_without_refund_and_explicit_stop_refunds(self):
        for explicit in (False,True):
            with self.subTest(explicit=explicit),tempfile.TemporaryDirectory() as folder:
                conn=integrations.Connections(Path(folder)/"db","key")
                try:
                    store=codex.CodexStore(conn)
                    task=store.create(1,"project","Build",files={"main.py":"print(1)"})
                    store.update(task["id"],charged=3,stop_requested=explicit)
                    memory=NS(gen_refund=Mock())
                    app=NS(bot_data={"codex":store,"codex_jobs":{},"memory":memory,"chat_default":"test","models":["test"]})
                    started=asyncio.Event()
                    async def wait(*args,**kwargs):
                        started.set()
                        await asyncio.Event().wait()
                    with patch("bot.generate",side_effect=wait),patch("bot.codex_can_run",return_value=False):
                        job=asyncio.create_task(bot.run_codex(app,task["id"],"en"))
                        await started.wait()
                        job.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await job
                    saved=store.get(task["id"])
                    self.assertEqual(saved["status"],codex.STATUS_STOPPED if explicit else codex.STATUS_PAUSED)
                    self.assertEqual(saved["charged"],0 if explicit else 3)
                    self.assertEqual(memory.gen_refund.call_count,int(explicit))
                finally:
                    conn.close()
