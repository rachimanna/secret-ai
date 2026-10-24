import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

from cryptography.fernet import InvalidToken
from telegram import Bot
from telegram.error import RetryAfter,TimedOut
from telegram.request import HTTPXRequest
from telethon import types
from telethon.tl.custom import AdminLogEvent

import guard_session
import integrations
import message_guard as guard

CHAT=-1001234567890
BOT=42
OWNER=1


def message(mid=10,text='Hello',**kwargs):
    return {'message_id':mid,'date':int(time.time()),'chat':{'id':CHAT,'type':'supergroup','title':'Test'},
            'from':{'id':BOT,'is_bot':True,'first_name':'Bot'},'text':text,**kwargs}


def event(mid=10,actor=2,ident=100):
    return NS(deleted_message=True,old=NS(id=mid),user_id=actor,id=ident,date=datetime.now(timezone.utc))


class GuardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.vault=integrations.Connections(Path(self.temp.name)/'db','key');self.addCleanup(self.vault.close)
        self.store=guard.Store(self.vault);self.store.configure(CHAT,True)
        self.worker=guard.Worker(self.store,'unused')
        self.bot=NS(id=BOT,get_chat_administrators=AsyncMock(return_value=[NS(status='creator',user=NS(id=OWNER))]),
                    send_message=AsyncMock(return_value=NS(message_id=20)))

    async def test_restore_only_own_saved_messages_and_owner_deletion_excluded(self):
        self.store.capture(message(),BOT)
        self.assertTrue(await self.worker.event(self.bot,self.store.chats()[0],event(actor=OWNER)))
        self.bot.send_message.assert_not_called()
        self.assertIsNone(self.store.find(CHAT,10))
        self.store.capture(message(11),BOT)
        self.assertTrue(await self.worker.event(self.bot,self.store.chats()[0],event(11,actor=2)))
        self.assertEqual(self.bot.send_message.call_args.kwargs['text'],'Hello')
        self.assertTrue(self.bot.send_message.call_args.kwargs['disable_notification'])
        await self.worker.event(self.bot,self.store.chats()[0],event(11,actor=2))
        self.assertEqual(self.bot.send_message.await_count,1)

    async def test_bot_own_deletion_and_unrelated_messages_not_restored(self):
        self.store.capture(message(),BOT)
        await self.worker.event(self.bot,self.store.chats()[0],event(actor=BOT))
        await self.worker.event(self.bot,self.store.chats()[0],event(999))
        foreign=message(12);foreign['from']['id']=99
        self.store.capture(foreign,BOT)
        self.assertIsNone(self.store.find(CHAT,12));self.bot.send_message.assert_not_called()

    async def test_disable_and_restart_do_not_resend_uncertain_attempts(self):
        self.store.capture(message(),BOT)
        self.bot.send_message.side_effect=TimedOut('lost response')
        await self.worker.event(self.bot,self.store.chats()[0],event())
        restarted=guard.Store(self.vault)
        self.assertIsNone(restarted.find(CHAT,10))
        await self.worker.event(self.bot,self.store.chats()[0],event())
        self.assertEqual(self.bot.send_message.await_count,1)
        self.store.configure(CHAT,False)
        self.assertFalse(self.store.enabled(CHAT))

    async def test_retry_after_keeps_cursor_for_later_pass(self):
        self.store.capture(message(),BOT)
        self.bot.send_message.side_effect=RetryAfter(30)
        self.assertFalse(await self.worker.event(self.bot,self.store.chats()[0],event()))
        self.assertIsNotNone(self.store.find(CHAT,10))

    async def test_owner_lookup_failure_does_not_claim_message(self):
        self.store.capture(message(),BOT)
        self.bot.get_chat_administrators.return_value=[]
        with self.assertRaises(ValueError):
            await self.worker.event(self.bot,self.store.chats()[0],event())
        self.assertIsNotNone(self.store.find(CHAT,10));self.bot.send_message.assert_not_called()

    async def test_telethon_event_uses_original_deleted_message(self):
        original=types.ChannelAdminLogEvent(id=100,date=datetime.now(timezone.utc),user_id=2,
            action=types.ChannelAdminLogEventActionDeleteMessage(message=types.Message(id=10,peer_id=types.PeerChannel(1234567890),date=datetime.now(timezone.utc),message='Hello')))
        actual=AdminLogEvent(original,{2:types.User(id=2,access_hash=123,first_name='Admin')})
        self.assertIs(actual.deleted_message,True)
        self.store.capture(message(),BOT)
        await self.worker.event(self.bot,self.store.chats()[0],actual)
        self.bot.send_message.assert_awaited_once()

    async def test_capture_edits_latest_text_and_ignores_callback_games(self):
        self.store.capture(message(),BOT);self.store.capture(message(text='Updated'),BOT)
        self.assertEqual(self.store.find(CHAT,10)[1]['message']['text'],'Updated')
        self.store.capture(message(reply_markup={'inline_keyboard':[[{'text':'Play','callback_data':'game:1'}]]}),BOT)
        self.assertIsNone(self.store.find(CHAT,10))

    async def test_encrypted_cache_retention_and_per_root_rate_limit(self):
        self.store.capture(message(),BOT)
        raw=self.vault.db.execute('SELECT payload FROM mg_messages').fetchone()[0]
        self.assertNotIn(b'Hello',raw)
        for index in range(3):
            self.assertTrue(self.store.claim(CHAT,10,10));self.store.finish(CHAT,10,'saved')
        self.assertFalse(self.store.claim(CHAT,10,10))
        with patch('message_guard.time.time',return_value=time.time()+601):
            self.assertTrue(self.store.claim(CHAT,10,10))
        self.store.finish(CHAT,10,'saved')
        with patch('message_guard.time.time',return_value=time.time()+guard.RETENTION+1):
            self.assertIsNone(self.store.find(CHAT,10))

    async def test_capture_only_opted_in_supergroups(self):
        self.store.configure(CHAT,False)
        self.store.capture(message(),BOT);self.assertIsNone(self.store.find(CHAT,10))
        self.store.configure(CHAT,True)
        private=message();private['chat']['type']='private'
        self.store.capture(private,BOT);self.assertIsNone(self.store.find(CHAT,10))

    async def test_request_adapter_records_real_bot_api_result_and_delete(self):
        request=guard.RecordingRequest(self.store);request.bot_id=BOT
        try:
            response=(200,json.dumps({'ok':True,'result':message()}).encode())
            with patch.object(HTTPXRequest,'do_request',AsyncMock(return_value=response)):
                result=await request.do_request('https://example.com/botTOKEN/sendMessage','POST')
            self.assertEqual(result,response);self.assertIsNotNone(self.store.find(CHAT,10))
            with patch.object(HTTPXRequest,'do_request',AsyncMock(return_value=(200,b'{"ok":true,"result":true}'))):
                await request.do_request('https://example.com/botTOKEN/deleteMessage','POST',request_data=NS(parameters={'chat_id':CHAT,'message_id':10}))
            self.assertIsNone(self.store.find(CHAT,10))
        finally:
            await request.shutdown()

    async def test_media_uses_saved_bot_api_file_id_and_forum_thread(self):
        payload={'message':message(text=None,photo=[{'file_id':'PHOTO','file_unique_id':'UNIQUE','width':10,'height':10}],caption='Caption',is_topic_message=True,message_thread_id=7),'reply_id':5}
        bot=NS(send_photo=AsyncMock(return_value=NS(message_id=20)))
        await guard.resend(bot,payload)
        args=bot.send_photo.call_args.kwargs
        self.assertEqual(args['photo'],'PHOTO');self.assertEqual(args['caption'],'Caption')
        self.assertEqual(args['message_thread_id'],7);self.assertEqual(args['reply_parameters'].message_id,5)

    async def test_guard_command_admin_can_manage_but_member_cannot(self):
        self.worker.ready=True
        target=NS(chat_id=CHAT,chat=NS(type='supergroup'),sender_chat=None,reply_text=AsyncMock())
        update=NS(effective_message=target,effective_user=NS(id=2))
        bot=NS(get_chat_member=AsyncMock(return_value=NS(status='member')))
        context=NS(bot=bot,args=['off'],bot_data={'message_guard':self.worker,'allowed':set()})
        await guard.command(update,context);self.assertTrue(self.store.enabled(CHAT))
        bot.get_chat_member.return_value=NS(status='administrator')
        await guard.command(update,context);self.assertFalse(self.store.enabled(CHAT))

    async def test_miniapp_guard_permissions_are_checked_live_for_each_group(self):
        self.worker.ready=True
        bot=NS(get_chat_member=AsyncMock(return_value=NS(status='administrator')),get_chat=AsyncMock(return_value=NS(type='supergroup')))
        app=NS(bot=bot,bot_data={'message_guard':self.worker,'connections':self.vault,'allowed':set()})
        result=await guard.settings(app,2,CHAT,False)
        self.assertFalse(result['enabled'])
        self.assertTrue((await guard.settings(app,2,CHAT,True))['enabled'])
        bot.get_chat_member.return_value=NS(status='member')
        with self.assertRaises(PermissionError):await guard.settings(app,2,CHAT,False)
        with self.assertRaises(PermissionError):await guard.settings(app,2,CHAT)
        self.assertTrue(self.store.enabled(CHAT))
        bot.get_chat_member.return_value=NS(status='creator')
        self.assertTrue((await guard.settings(app,1,CHAT))['enabled'])

    async def test_miniapp_requires_ready_observer_and_can_disable_during_failure(self):
        self.worker.ready=False
        bot=NS(get_chat_member=AsyncMock(return_value=NS(status='administrator')),get_chat=AsyncMock(return_value=NS(type='supergroup')))
        app=NS(bot=bot,bot_data={'message_guard':self.worker,'connections':self.vault,'allowed':{2}})
        with self.assertRaises(ValueError):await guard.settings(app,2,CHAT,True)
        self.assertFalse((await guard.settings(app,2,CHAT,False))['enabled'])
        with self.assertRaises(PermissionError):await guard.settings(app,1,CHAT)
        with self.assertRaises(ValueError):await guard.settings(app,2,CHAT,'true')
        with self.assertRaises(ValueError):await guard.settings(app,2,True)


class SessionTest(unittest.TestCase):
    def test_bundle_is_encrypted_roundtrips_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'session.enc'
            guard_session.save(path,'key',123,'a'*32,'PRIVATE_SESSION')
            self.assertNotIn(b'PRIVATE_SESSION',path.read_bytes())
            loaded=guard_session.load(path,guard_session.cipher('key'))
            self.assertEqual(loaded['session'],'PRIVATE_SESSION')
            with self.assertRaises(InvalidToken):guard_session.load(path,guard_session.cipher('wrong'))
            with self.assertRaises(FileExistsError):guard_session.save(path,'key',123,'a'*32,'OTHER')


if __name__=='__main__':
    unittest.main()
