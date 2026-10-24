import asyncio
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import release_notices as notices

HAS_TELEGRAM = importlib.util.find_spec("telegram") is not None


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.store = notices.Store(self.db)
        self.release = {"version": 1, "text": "Первая версия"}

    def test_restart_and_new_release(self):
        self.store.remember([1,1,-10,0])
        self.assertTrue(self.store.stage(self.release))
        job = self.store.claim(1)
        self.store.finish(job,"sent")
        other = self.store.claim(1)
        self.store.finish(other,"skipped")
        restarted = notices.Store(self.db)
        self.assertFalse(restarted.stage(self.release))
        self.assertIsNone(restarted.claim(1))
        self.assertTrue(restarted.stage({"version":2,"text":"Новая версия"}))
        self.assertIsNotNone(restarted.claim(2))

    def test_new_recipient_does_not_receive_historical_release(self):
        self.store.remember([1])
        self.store.stage(self.release)
        self.store.remember([2])
        self.store.stage(self.release)
        self.assertEqual(self.db.execute("SELECT chat_id FROM release_deliveries").fetchall(),[(1,)])

    def test_rollback_and_superseded_jobs(self):
        self.store.remember([1])
        self.store.stage(self.release)
        self.store.stage({"version":2,"text":"Вторая"})
        self.assertFalse(self.store.stage(self.release))
        self.assertIsNone(self.store.claim(1))
        self.assertIsNotNone(self.store.claim(2))

    def test_published_text_cannot_silently_change(self):
        self.store.stage(self.release)
        with self.assertRaises(ValueError):
            self.store.stage({"version":1,"text":"Другое сообщение"})

    def test_allowed_users_but_no_group_member_dms(self):
        self.db.executescript("CREATE TABLE messages(user_id INTEGER); INSERT INTO messages VALUES (1),(2);"
                              "CREATE TABLE settings(user_id INTEGER); INSERT INTO settings VALUES (3);"
                              "CREATE TABLE group_info(chat_id INTEGER); INSERT INTO group_info VALUES (-10);"
                              "CREATE TABLE group_members(chat_id INTEGER,user_id INTEGER);"
                              "INSERT INTO group_members VALUES (-10,99);"
                              "CREATE TABLE bot_meta(key TEXT); INSERT INTO bot_meta VALUES ('welcomed:4');")
        self.store.seed()
        self.store.stage(self.release,allowed={1,4})
        self.assertEqual(set(self.db.execute("SELECT chat_id FROM release_deliveries")),{(1,),(4,),(-10,)})

    def test_lease_expiry_and_stale_acknowledgement(self):
        self.store.remember([1])
        self.store.stage(self.release)
        old = self.store.claim(1,100)
        self.assertIsNone(self.store.claim(1,101))
        fresh = self.store.claim(1,221)
        self.store.finish(old,"sent")
        self.assertEqual(self.db.execute("SELECT status FROM release_deliveries").fetchone()[0],"pending")
        self.store.finish(fresh,"sent")
        self.assertIsNone(self.store.claim(1,999))

    def test_retry_budget_is_persistent(self):
        self.store.remember([1])
        self.store.stage(self.release)
        for attempt in range(notices.MAX_TRIES):
            job = self.store.claim(1,1000+attempt*200)
            self.assertEqual(job[3],attempt+1)
            # Simulate a worker crashing while Telegram is unreachable.
        self.assertIsNone(self.store.claim(1,9999))
        self.assertEqual(self.db.execute("SELECT status FROM release_deliveries").fetchone()[0],"failed")

    def test_rate_limit_does_not_use_retry_budget(self):
        self.store.remember([1])
        self.store.stage(self.release)
        job = self.store.claim(1)
        self.store.finish(job,"pending","RetryAfter",rate_limited=True)
        self.assertEqual(self.store.claim(1)[3],1)

    def test_partial_progress_survives_real_database_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"memory.db"
            with sqlite3.connect(path) as db:
                store = notices.Store(db)
                store.remember([1,2])
                store.stage(self.release)
                store.finish(store.claim(1),"sent")
            db.close()
            with sqlite3.connect(path) as db:
                store = notices.Store(db)
                self.assertFalse(store.stage(self.release))
                self.assertEqual(store.claim(1)[1],2)
            db.close()

    def test_migration_merges_existing_target_without_duplicate(self):
        self.store.remember([-10,-100])
        self.store.stage(self.release)
        job = self.store.claim(1)
        self.assertEqual(job[1],-100)
        self.store.finish(job,"sent")
        job = self.store.claim(1)
        self.store.migrate(job,-100)
        self.assertIsNone(self.store.claim(1))
        self.assertEqual(self.db.execute("SELECT chat_id FROM release_chats").fetchall(),[(-100,)])


class ManifestTest(unittest.TestCase):
    def test_shipped_manifest(self):
        value = notices.load_release()
        self.assertGreater(value["version"],0)
        self.assertIn("Secret AI",value["text"])

    def test_invalid_manifest_and_telegram_utf16_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"release.json"
            for value in ({"version":True,"text":"x"},{"version":0,"text":"x"},{"version":1,"text":""},
                          {"version":1,"text":"😀"*2000}):
                path.write_text(json.dumps(value),encoding="utf-8")
                with self.assertRaises(ValueError):
                    notices.load_release(path)


@unittest.skipUnless(HAS_TELEGRAM,"Telegram dependency is installed by the full CI workflow")
class WorkerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.store = notices.Store(self.db)
        self.store.remember([1,2,-10])
        self.app = NS(bot=NS(send_message=AsyncMock()),bot_data={"release_notices":self.store,"allowed":set()})
        self.worker = notices.Worker(self.app,is_test_bot=lambda:False,test_mode=lambda d:d.get("test",False),rate=0)
        self.worker.release = {"version":1,"text":"Что нового"}

    async def test_delivers_once_and_keeps_manual_broadcast_independent(self):
        self.app.bot_data["broadcast"] = {7:{"text":"Ручная рассылка"}}
        await self.worker.tick()
        await self.worker.tick()
        self.assertEqual(self.app.bot.send_message.await_count,3)
        self.assertEqual({c.kwargs["chat_id"] for c in self.app.bot.send_message.await_args_list},{1,2,-10})
        self.assertTrue(all(c.kwargs["parse_mode"]=="" for c in self.app.bot.send_message.await_args_list))
        self.assertEqual(self.app.bot_data["broadcast"],{7:{"text":"Ручная рассылка"}})

    async def test_test_bot_never_stages_or_sends(self):
        self.worker.is_test_bot = lambda:True
        await self.worker.tick()
        self.app.bot.send_message.assert_not_awaited()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM release_batches").fetchone()[0],0)

    async def test_test_mode_pauses_and_resumes_automatically(self):
        self.app.bot_data["test"] = True
        await self.worker.tick()
        self.app.bot.send_message.assert_not_awaited()
        self.app.bot_data["test"] = False
        await self.worker.tick()
        self.assertEqual(self.app.bot.send_message.await_count,3)

    async def test_forbidden_is_skipped_and_network_failure_retries(self):
        from telegram.error import Forbidden, NetworkError
        self.app.bot.send_message.side_effect = [Forbidden("blocked"),NetworkError("offline"),None]
        await self.worker.tick()
        rows = dict(self.db.execute("SELECT chat_id,status FROM release_deliveries"))
        self.assertEqual(rows,{-10:"skipped",1:"pending",2:"sent"})
        self.app.bot.send_message.side_effect = None
        with self.db:
            self.db.execute("UPDATE release_deliveries SET due=0 WHERE status='pending'")
        await self.worker.tick()
        self.assertEqual(self.app.bot.send_message.await_count,4)

    async def test_retry_after_waits_for_whole_worker(self):
        from telegram.error import RetryAfter
        self.app.bot.send_message.side_effect = RetryAfter(timedelta(seconds=1))
        with patch("release_notices.asyncio.sleep",new_callable=AsyncMock) as sleep:
            await self.worker.tick()
        self.assertEqual(self.app.bot.send_message.await_count,1)
        sleep.assert_awaited_once_with(2)
        self.assertEqual(self.db.execute("SELECT tries FROM release_deliveries WHERE chat_id=-10").fetchone()[0],0)

    async def test_migration_is_followed(self):
        from telegram.error import ChatMigrated
        self.app.bot.send_message.side_effect = [ChatMigrated(-100),None,None,None]
        await self.worker.tick()
        calls = [c.kwargs["chat_id"] for c in self.app.bot.send_message.await_args_list]
        self.assertEqual(calls,[-10,-100,1,2])

    async def test_failed_initial_checkpoint_prevents_sending(self):
        self.worker.checkpoint = AsyncMock(side_effect=RuntimeError("database offline"))
        with self.assertRaises(RuntimeError):
            await self.worker.tick()
        self.app.bot.send_message.assert_not_awaited()
        self.assertFalse(self.worker.prepared)
        self.worker.checkpoint.side_effect = None
        await self.worker.tick()
        self.assertEqual(self.app.bot.send_message.await_count,3)

    async def test_failed_delivery_checkpoint_blocks_further_sends(self):
        snapshots = NS(save=Mock(side_effect=[None,RuntimeError("offline"),RuntimeError("offline"),None,None,None]))
        self.app.bot_data["snapshots"] = snapshots
        with self.assertRaises(RuntimeError):
            await self.worker.tick()
        self.assertEqual(self.app.bot.send_message.await_count,1)
        with self.assertRaises(RuntimeError):
            await self.worker.tick()
        self.assertEqual(self.app.bot.send_message.await_count,1)
        await self.worker.tick()
        self.assertEqual(self.app.bot.send_message.await_count,3)

    async def test_cancellation_retains_lease_for_recovery(self):
        self.app.bot.send_message.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.worker.tick()
        self.assertEqual(self.db.execute("SELECT status,tries FROM release_deliveries WHERE chat_id=-10").fetchone(),("pending",1))
        self.assertIsNotNone(self.store.claim(1))
        # Interrupted delivery becomes available after its lease expires.
        with self.db:
            self.db.execute("UPDATE release_deliveries SET due=0 WHERE chat_id=-10")
        self.assertEqual(self.store.claim(1)[1],-10)

    async def test_tracker_accepts_start_only_chats_and_excludes_business(self):
        update = NS(effective_chat=NS(id=99,type="private"),effective_user=NS(id=99,is_bot=False),
                    effective_message=NS(text="/start"),business_message=None,business_connection=None)
        context = NS(bot_data=self.app.bot_data)
        await notices.track(update,context)
        self.assertIn((99,),self.db.execute("SELECT chat_id FROM release_chats").fetchall())
        update.effective_chat.id = 100
        update.business_message = object()
        await notices.track(update,context)
        self.assertNotIn((100,),self.db.execute("SELECT chat_id FROM release_chats").fetchall())


if __name__ == "__main__":
    unittest.main()
