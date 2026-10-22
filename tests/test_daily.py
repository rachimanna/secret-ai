import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import Forbidden

import bot
import daily
import daily_ui


def result(source, **extra):
    return {'tasks':[dict(title='Купить компьютер',details='Бюджет 300 евро',next_step='Сравнить варианты',
                         priority=2,due=None,sources=[source['id']],**extra)]}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.store=daily.Store(self.db)
        self.store.profile(1,1)

    def source(self,user=1,msg=1,text='Хочу компьютер за 300 евро'):
        return self.store.add_source(user,user,msg,'text',text)

    def task(self,user=1,msg=1):
        s=self.source(user,msg)
        return self.store.commit_batch(user,[s],result(s))[0]

    def test_batch_is_atomic_idempotent_and_keeps_constraints(self):
        s=self.source()
        t=self.store.commit_batch(1,[s],result(s))[0]
        self.assertEqual(self.store.commit_batch(1,[s],result(s))[0]['id'],t['id'])
        self.assertEqual(len(self.store.tasks(1)),1)
        self.assertEqual(self.store.inbox(1),[])
        self.assertIn('300',self.store.context(1,t['id']))

    def test_invalid_references_roll_back_without_losing_inbox(self):
        s=self.source()
        value=result(s)
        value['tasks'].append(dict(title='Чужая задача',sources=[999]))
        with self.assertRaises(daily.DailyError):
            self.store.commit_batch(1,[s],value)
        self.assertEqual(self.store.tasks(1),[])
        self.assertEqual(len(self.store.inbox(1)),1)

    def test_existing_task_merges_new_material_without_duplicate(self):
        t=self.task()
        s=self.source(msg=2,text='Ещё нужна доставка')
        value=result(s,task_id=t['id'])
        value['tasks'][0]['details']='Бюджет 300 евро, нужна доставка'
        self.store.commit_batch(1,[s],value)
        self.assertEqual(len(self.store.tasks(1)),1)
        self.assertEqual(len(self.store.materials(1,t['id'])),2)
        self.assertIn('доставка',self.store.task(1,t['id'])['details'])

    def test_foreign_tasks_sources_and_mutations_are_isolated(self):
        t=self.task(1)
        s=self.source(2)
        self.assertIsNone(self.store.task(2,t['id']))
        self.assertIsNone(self.store.source(2,1))
        for fn in (lambda:self.store.link(2,t['id'],s['id']),lambda:self.store.delete(2,t['id']),
                   lambda:self.store.status(2,t['id'],'done'),lambda:self.store.save_answer(2,t['id'],'q','a','step'),
                   lambda:self.store.commit_batch(2,[s],result(s,task_id=t['id']))):
            with self.assertRaises(daily.DailyError): fn()
        self.assertEqual(self.store.task(1,t['id'])['status'],'open')

    def test_done_cancels_reminder_and_focus_but_keeps_result(self):
        t=self.task()
        self.store.configure(1,1,active_task=t['id'])
        self.store.save_answer(1,t['id'],'Напиши продавцу','Guten Tag','Отправить текст продавцу')
        job,_=self.store.reminder(1,1,t['id'],'через 1 час')
        self.store.status(1,t['id'],'done')
        self.assertIsNone(self.store.profile(1)['active_task'])
        self.assertEqual(self.store.reminders(1),[])
        self.assertEqual(self.store.notes(1,t['id'])[0]['answer'],'Guten Tag')
        self.assertIn('Завершено за день: 1',self.store.plan(1,'review'))

    def test_informational_sources_are_retained_without_fake_tasks(self):
        s=self.source(text='Привет')
        self.assertEqual(self.store.commit_batch(1,[s],{'tasks':[]}),[])
        self.assertIsNotNone(self.store.source(1,s['id']))
        self.assertEqual(self.store.tasks(1),[])

    def test_duplicate_telegram_delivery_does_not_duplicate_sources(self):
        self.assertEqual(self.source()['id'],self.source()['id'])
        self.assertEqual(len(self.store.inbox(1)),1)

    def test_restart_preserves_tasks_sources_results_and_reminders(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'daily.db'
            db=sqlite3.connect(path)
            store=daily.Store(db)
            store.profile(1,1)
            s=store.add_source(1,1,1,'text','Бюджет 300 евро')
            t=store.commit_batch(1,[s],result(s))[0]
            store.save_answer(1,t['id'],'Напиши письмо','Guten Tag','Отправить')
            store.reminder(1,1,t['id'],'через 1 час')
            db.close()
            db=sqlite3.connect(path)
            try:
                store=daily.Store(db)
                self.assertEqual(store.task(1,t['id'])['next_step'],'Отправить')
                self.assertEqual(len(store.materials(1,t['id'])),1)
                self.assertEqual(len(store.reminders(1)),1)
            finally: db.close()

    def test_claim_is_leased_recoverable_and_sent_only_once(self):
        t=self.task()
        now=int(time.time())
        self.store.reminder(1,1,t['id'],'через 1 минуту')
        first=self.store.claim(now+61)
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.claim(now+62))
        recovered=self.store.claim(now+182)
        self.assertEqual(recovered['id'],first['id'])
        self.assertEqual(recovered['tries'],2)
        self.store.finish_job(recovered)
        self.assertIsNone(self.store.claim(now+500))

    def test_retry_then_permanent_failure_is_bounded(self):
        t=self.task()
        self.store.reminder(1,1,t['id'],'через 1 минуту')
        now=int(time.time())+100
        for i in range(4):
            job=self.store.claim(now+i*500)
            self.assertIsNotNone(job)
            self.store.finish_job(job,'NetworkError')
        self.assertIsNone(self.store.claim(now+5000))

    def test_reminder_uses_explicit_timezone_and_rejects_past(self):
        self.store.configure(1,1,tz='Europe/Berlin')
        fixed=datetime(2026,10,22,12,tzinfo=timezone.utc).timestamp()
        self.assertEqual(daily.stamp('завтра 08:00','Europe/Berlin',fixed),int(datetime(2026,10,23,6,tzinfo=timezone.utc).timestamp()))
        with self.assertRaises(daily.DailyError): daily.stamp('сегодня 08:00','Europe/Berlin',fixed)
        with self.assertRaises(daily.DailyError): self.store.configure(1,1,tz='No/Such_Zone')
        with self.assertRaises(daily.DailyError): daily.clock('25:00')

    def test_routines_deduplicate_across_dst_and_skip_old_days(self):
        self.store.configure(1,1,tz='Europe/Berlin',morning='02:30',evening='21:00')
        # Fall-back: local 02:30 happens twice; one event per local date.
        one=datetime(2026,10,25,0,35,tzinfo=timezone.utc).timestamp()
        two=datetime(2026,10,25,1,35,tzinfo=timezone.utc).timestamp()
        self.store.schedule_routines(one)
        self.store.schedule_routines(two)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM dy_jobs WHERE kind='morning'").fetchone()[0],1)
        self.store.configure(1,1,morning=None,evening=None)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM dy_jobs WHERE status='pending'").fetchone()[0],0)

    def test_reset_only_deletes_own_tasks_and_delivery(self):
        self.task(1); self.task(2)
        self.store.reset(1)
        self.assertEqual(self.store.tasks(1),[])
        self.assertEqual(len(self.store.tasks(2)),1)
        self.assertIsNone(self.store.profile(1))

    def test_task_context_is_valid_bounded_json_and_has_only_own_material(self):
        t=self.task()
        for i in range(2,14):
            s=self.source(msg=i,text='x'*60000)
            self.store.link(1,t['id'],s['id'])
        for i in range(8): self.store.save_answer(1,t['id'],'q'*6000,'a'*12000,'next')
        value=self.store.context(1,t['id'])
        parsed=json.loads(value)
        self.assertLess(len(value),28000)
        self.assertEqual(parsed['omitted_materials'],5)
        self.assertEqual(len(parsed['recent']),4)

    def test_task_search_handles_compom_and_ambiguous_matches(self):
        self.task()
        self.assertEqual(len(self.store.find(1,'компом')),1)
        self.assertEqual(self.store.find(2,'компом'),[])

    def test_bundle_validation_is_atomic_and_saved_drafts_are_task_local(self):
        t=self.task()
        invalid={'tasks':[dict(id=t['id'],answer='draft',next_step='send'),dict(id=999,answer='wrong')]}
        with self.assertRaises(daily.DailyError): self.store.save_bundle(1,{t['id']},invalid)
        self.assertEqual(self.store.notes(1,t['id']),[])
        self.store.save_bundle(1,{t['id']},{'tasks':[dict(id=t['id'],answer='draft',next_step='send')]})
        self.assertEqual(self.store.notes(1,t['id'])[0]['answer'],'draft')
        self.assertEqual(self.store.task(1,t['id'])['next_step'],'send')
        self.assertEqual(self.store.notes(2,t['id']),[])

    def test_past_deadline_is_visible_without_creating_reminder(self):
        s=self.source()
        value=result(s)
        value['tasks'][0]['due']='2026-01-01T12:00:00+00:00'
        t=self.store.commit_batch(1,[s],value)[0]
        self.assertIsNotNone(t['due'])
        self.assertEqual(self.store.reminders(1),[])

    def test_deleting_source_unlinks_only_own_material_and_keeps_results(self):
        t=self.task()
        self.store.save_answer(1,t['id'],'q','prepared','next')
        with self.assertRaises(daily.DailyError): self.store.delete_source(2,1)
        self.store.delete_source(1,1)
        self.assertEqual(self.store.materials(1,t['id']),[])
        self.assertEqual(self.store.notes(1,t['id'])[0]['answer'],'prepared')
        self.assertIsNotNone(self.store.task(1,t['id']))

    def test_memory_reset_includes_daily_data(self):
        memory=bot.Memory(':memory:')
        try:
            memory.daily.profile(1,1)
            memory.daily.add_source(1,1,1,'text','hello')
            memory.reset(1)
            self.assertIsNone(memory.daily.profile(1))
        finally: memory.close()


class UITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.store=daily.Store(self.db)
        self.api=NS(authorized=AsyncMock(return_value=True),counted=lambda fn:fn,
                    user_lock=lambda ctx,user:asyncio.Lock(),send=AsyncMock(),quick_answer=AsyncMock(),
                    read_question=AsyncMock(return_value=('Купить компьютер','Хочу компьютер за 300 евро','Купить компьютер')),
                    lookup=AsyncMock(return_value='Search unavailable'),test_mode=Mock(return_value=False))
        self.controls=daily_ui.Controls(self.api)
        self.message=NS(message_id=1,text='Хочу компьютер за 300 евро',caption=None,quote=None,reply_to_message=None,
                        photo=[],document=None,voice=None,audio=None,video_note=None,reply_document=AsyncMock())
        self.update=NS(effective_user=NS(id=1,language_code='ru'),effective_chat=NS(id=1,type='private'),effective_message=self.message)
        self.context=NS(args=[],bot_data=dict(memory=NS(daily=self.store),chat_default='anymodel:xai/grok-4.7',allowed=set()),bot=NS())

    def make_task(self,user=1):
        self.store.profile(user,user)
        source=self.store.add_source(user,user,100,'text','Купить компьютер за 300 евро')
        return self.store.commit_batch(user,[source],result(source))[0]

    async def test_capture_sort_continue_and_reminder_end_to_end(self):
        self.store.configure(1,1,mode='auto')
        self.api.quick_answer.return_value=json.dumps({'tasks':[dict(title='Купить компьютер',details='300 евро',next_step='Написать продавцу',sources=[1])]})
        await self.controls.route(self.update,self.context)
        task=self.store.tasks(1)[0]
        self.assertEqual(len(self.store.materials(1,task['id'])),1)
        await self.controls.show_task(self.update,self.context,task['id'])
        self.message.message_id=2
        self.message.text='Подготовь письмо продавцу'
        self.api.read_question.return_value=('Подготовь письмо продавцу','Подготовь письмо продавцу','saved')
        self.api.quick_answer.return_value=json.dumps(dict(answer='Guten Tag! Ist der Computer noch verfügbar?',next_step='Отправить письмо'))
        await self.controls.route(self.update,self.context)
        self.assertEqual(self.store.notes(1,task['id'])[0]['answer'],'Guten Tag! Ist der Computer noch verfügbar?')
        self.store.reminder(1,1,task['id'],'через 1 минуту')
        self.db.execute('UPDATE dy_jobs SET run_at=0'); self.db.commit()
        send=AsyncMock()
        app=NS(bot_data=self.context.bot_data,bot=NS(send_message=send))
        await self.controls.tick(app)
        await self.controls.tick(app)
        send.assert_awaited_once()
        self.assertIn('Guten Tag',send.call_args.args[1])
        self.assertIn('Отправить письмо',send.call_args.args[1])

    async def test_model_failure_keeps_source_for_retry(self):
        self.store.configure(1,1,mode='auto')
        self.api.quick_answer.side_effect=RuntimeError('provider failure')
        await self.controls.guarded(self.controls.route)(self.update,self.context)
        self.assertEqual(len(self.store.inbox(1)),1)
        self.assertEqual(self.store.tasks(1),[])
        self.assertNotIn('provider failure',str(self.api.send.call_args))

    async def test_no_model_and_batch_mode_capture_without_ai(self):
        self.store.configure(1,1,mode='inbox')
        self.context.bot_data['chat_default']=None
        await self.controls.route(self.update,self.context)
        self.api.quick_answer.assert_not_called()
        self.assertEqual(len(self.store.inbox(1)),1)

    async def test_image_ocr_failure_preserves_original_attachment(self):
        self.store.configure(1,1,mode='auto')
        self.message.photo=[NS(file_id='telegram-photo')]
        self.api.read_question.return_value=('Разбери скриншот',[{'type':'input_image','image_url':'data:image/png;base64,eA=='}],'photo')
        self.api.quick_answer.side_effect=RuntimeError('no vision')
        await self.controls.route(self.update,self.context)
        source=self.store.source(1,1)
        self.assertEqual(source['attachment']['file_id'],'telegram-photo')
        self.assertIn('не прочитано',source['text'])
        self.assertEqual(self.store.tasks(1),[])

    async def test_foreign_callback_cannot_read_or_change_task(self):
        task=self.make_task(2)
        q=NS(data=f"dy:2:done:{task['id']}",answer=AsyncMock())
        self.update.callback_query=q
        await self.controls.callback(self.update,self.context)
        self.assertEqual(self.store.task(2,task['id'])['status'],'open')
        self.api.authorized.assert_not_called()
        self.api.quick_answer.assert_not_called()

    async def test_forged_callback_own_prefix_foreign_id_is_rejected(self):
        task=self.make_task(2)
        self.update.callback_query=NS(data=f"dy:1:source:1",answer=AsyncMock())
        await self.controls.callback(self.update,self.context)
        self.message.reply_document.assert_not_called()

    async def test_completed_task_is_not_reminded(self):
        task=self.make_task()
        self.store.reminder(1,1,task['id'],'через 1 минуту')
        self.store.status(1,task['id'],'done')
        self.db.execute('UPDATE dy_jobs SET run_at=0');self.db.commit()
        app=NS(bot_data=self.context.bot_data,bot=NS(send_message=AsyncMock()))
        await self.controls.tick(app)
        app.bot.send_message.assert_not_called()

    async def test_blocked_user_disables_routine(self):
        self.store.configure(1,1,morning='08:00')
        self.db.execute("INSERT INTO dy_jobs(user_id,chat_id,kind,run_at) VALUES (1,1,'morning',0)")
        self.db.commit()
        app=NS(bot_data=self.context.bot_data,bot=NS(send_message=AsyncMock(side_effect=Forbidden('blocked'))))
        await self.controls.tick(app)
        self.assertIsNone(self.store.profile(1)['morning'])

    async def test_plan_and_review_do_not_require_ai(self):
        self.make_task()
        await self.controls.plan(self.update,self.context)
        await self.controls.review(self.update,self.context)
        self.api.quick_answer.assert_not_called()

    async def test_live_search_not_claimed_when_gateway_has_no_search(self):
        task=self.make_task()
        self.api.quick_answer.return_value=json.dumps(dict(answer='Пришли ссылки для сравнения.',next_step='Собрать ссылки'))
        await self.controls.continue_task(self.update,self.context,task['id'],'найди компьютер')
        self.api.lookup.assert_not_called()
        self.assertIn('Live search unavailable',self.api.quick_answer.call_args.args[3])

    def test_collection_is_opt_in_and_explicit_replies_keep_existing_behavior(self):
        self.assertFalse(self.controls.should_handle(self.update,self.context))
        self.store.configure(1,1,mode='auto')
        self.assertTrue(self.controls.should_handle(self.update,self.context))
        self.message.reply_to_message=NS(text='Выбранный старый ответ',document=None)
        self.assertFalse(self.controls.should_handle(self.update,self.context))

    async def test_prepare_saves_each_generated_result_in_its_task(self):
        task=self.make_task()
        self.api.quick_answer.return_value=json.dumps(dict(overview='Готовимся к завтра',tasks=[dict(id=task['id'],answer='Guten Tag',next_step='Отправить письмо')]))
        await self.controls.prepare(self.update,self.context)
        self.assertEqual(self.store.notes(1,task['id'])[0]['answer'],'Guten Tag')
        self.assertEqual(self.store.task(1,task['id'])['next_step'],'Отправить письмо')

    async def test_daily_documents_do_not_consume_project_file_quota(self):
        doc=NS(file_name='notes.txt',mime_type='text/plain')
        self.message.document=doc
        self.message.text=None
        self.message.caption='Подготовиться к контрольной'
        status=NS(delete=AsyncMock())
        extracted=dict(name='notes.txt',text='Вопросы к контрольной',warnings=[])
        with patch.object(bot,'reply',AsyncMock(return_value=status)), \
             patch.object(bot.docreader,'read_attachment',AsyncMock(return_value=extracted)):
            parsed=await bot.read_question(self.update,self.context,daily_input=True)
        self.assertIn('Вопросы к контрольной',parsed[1])
        self.assertEqual(self.store.tasks(1),[])

    async def test_actual_chat_router_captures_inbox_when_model_is_down(self):
        self.store.configure(1,1,mode='inbox')
        self.context.bot_data.update(daily_controls=self.controls,chat_default=None,models=[])
        self.message.entities=[]
        self.message.caption_entities=[]
        with patch.object(bot,'authorized',AsyncMock(return_value=True)), \
             patch.object(bot,'promo_typed',AsyncMock(return_value=False)), \
             patch.object(bot,'secretary_switch',AsyncMock(return_value=False)), \
             patch.object(bot.music,'handle_text',AsyncMock()) as music, \
             patch.object(bot,'respond',AsyncMock()) as respond:
            await bot.chat(self.update,self.context)
        self.assertEqual(len(self.store.inbox(1)),1)
        music.assert_not_called()
        respond.assert_not_called()

    async def test_quick_answer_converts_screenshot_parts_for_gateway(self):
        content=[{'type':'input_text','text':'Read this'},{'type':'input_image','image_url':'data:image/png;base64,eA=='}]
        create=AsyncMock(return_value=NS(choices=[NS(message=NS(content='visible text'))]))
        data=dict(anymodel_chat=NS(chat=NS(completions=NS(create=create))))
        answer=await bot.quick_answer_once(data,'anymodel:xai/grok-4.7','Read',content)
        self.assertEqual(answer,'visible text')
        self.assertEqual(create.call_args.kwargs['messages'][1]['content'][1]['type'],'image_url')
