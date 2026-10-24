import asyncio
import sqlite3
import unittest
from collections import defaultdict
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import bot
import daily
import miniapp


class DayBridgeTest(unittest.TestCase):
    def setUp(self):
        self.memory=bot.Memory(':memory:')
        self.addCleanup(self.memory.close)
        self.app=NS(bot_data={'memory':self.memory,'locks':defaultdict(asyncio.Lock)})
        self.bridge=bot.Bridge(self.app)
        self.bridge._run=asyncio.run
        self.tasks={}
        for user in (1,2):
            store=self.memory.daily
            store.profile(user,user)
            source=store.add_source(user,user,1,'text',f'Личные материалы пользователя {user}')
            self.tasks[user]=store.commit_batch(user,[source],{'tasks':[dict(title=f'Задача {user}',details='Условия',next_step='Следующий шаг',sources=[source['id']])]})[0]
            store.save_answer(user,self.tasks[user]['id'],'Подготовь','Личный результат '+str(user),'Продолжить')

    def test_snapshot_contains_only_authenticated_users_tasks(self):
        data=self.bridge.day_state(1)
        self.assertEqual([t['user_id'] for t in data['tasks']],[1])
        self.assertNotIn('Задача 2',str(data))
        self.assertNotIn('Личный результат 2',str(data))
        self.assertEqual(data['timezone'],'UTC')

    def test_result_is_scoped_and_missing_or_foreign_id_is_rejected(self):
        self.assertEqual(self.bridge.day_task(1,self.tasks[1]['id'])['result'],'Личный результат 1')
        with self.assertRaises(ValueError):self.bridge.day_task(1,self.tasks[2]['id'])
        with self.assertRaises(ValueError):self.bridge.day_status(1,self.tasks[2]['id'],'done')
        self.assertEqual(self.memory.daily.task(2,self.tasks[2]['id'])['status'],'open')

    def test_status_update_uses_existing_durable_reminder_cancellation(self):
        task=self.tasks[1]
        self.memory.daily.reminder(1,1,task['id'],'через 1 час')
        state=self.bridge.day_status(1,task['id'],'done')
        self.assertEqual(state['tasks'][0]['status'],'done')
        self.assertEqual(self.memory.daily.reminders(1),[])
        self.assertEqual(self.memory.daily.notes(1,task['id'])[0]['answer'],'Личный результат 1')

    def test_snapshot_does_not_enable_notifications_or_collection(self):
        self.bridge.day_state(1)
        p=self.memory.daily.profile(1)
        self.assertEqual(p['mode'],'off')
        self.assertIsNone(p['morning'])
        self.assertIsNone(p['evening'])


class DayRouteTest(unittest.TestCase):
    def setUp(self):
        self.bridge=Mock()
        self.config=dict(pipedream=NS(),github=NS(),direct=NS(),store=Mock(),bridge=self.bridge,token='test',allowed=set())
        self.handler=object.__new__(miniapp.make_handler(self.config))
        self.handler._send=Mock()

    def test_body_user_id_cannot_override_verified_user(self):
        self.handler._route('/api/day/status',{'id':1},{'id':7,'status':'done','user_id':2})
        self.bridge.day_status.assert_called_once_with(1,7,'done')

    def test_group_guard_uses_verified_user_and_strict_types(self):
        self.bridge.group_guard.return_value={'enabled':True}
        self.handler._route('/api/groups/guard',{'id':1},{'chat_id':-100123,'enabled':True,'user_id':2})
        self.bridge.group_guard.assert_called_once_with(1,-100123,True)
        self.bridge.group_guard.reset_mock()
        for value in (True,0,1,'-100123',None):
            self.handler._route('/api/groups/guard',{'id':1},{'chat_id':value,'enabled':True})
        self.handler._route('/api/groups/guard',{'id':1},{'chat_id':-100123,'enabled':'true'})
        self.bridge.group_guard.assert_not_called()
        self.assertEqual(self.handler._send.call_args.args[0],400)

    def test_group_guard_denies_revoked_permissions(self):
        self.bridge.group_guard.side_effect=PermissionError('group_admin')
        self.handler._route('/api/groups/guard',{'id':1},{'chat_id':-100123,'enabled':True})
        self.assertEqual(self.handler._send.call_args.args[0],403)

    def test_invalid_ids_and_statuses_never_reach_bridge(self):
        for value in (True,-1,0,'2',1.5,None):
            self.handler._route('/api/day/task',{'id':1},{'id':value})
        self.handler._route('/api/day/status',{'id':1},{'id':2,'status':'delete'})
        self.bridge.day_task.assert_not_called()
        self.bridge.day_status.assert_not_called()
        self.assertEqual(self.handler._send.call_args.args[0],400)

    def test_authentication_is_still_required(self):
        self.handler.headers={}
        with patch.object(miniapp,'check_init_data',return_value=None):
            self.assertIsNone(self.handler._user())
        self.assertEqual(self.handler._send.call_args.args[0],401)

    def test_allowlist_is_enforced_before_routes(self):
        self.config['allowed']={2}
        self.handler.headers={}
        with patch.object(miniapp,'check_init_data',return_value={'id':1}):
            self.assertIsNone(self.handler._user())
        self.assertEqual(self.handler._send.call_args.args[0],403)
