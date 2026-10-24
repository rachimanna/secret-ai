"""Opt-in restoration of this bot's saved messages, using a user account's supergroup admin log."""
import asyncio
import json
import logging
import time
from pathlib import Path

from telegram import Message, ReplyParameters
from telegram.error import RetryAfter, TelegramError
from telegram.request import HTTPXRequest

import guard_session

LOG = logging.getLogger("secret_ai.message_guard")
RETENTION = 48 * 3600
FIELDS = {"message_id","chat","from","date","text","entities","caption","caption_entities","photo","video",
          "animation","audio","voice","document","sticker","video_note","message_thread_id","is_topic_message",
          "has_media_spoiler","show_caption_above_media","link_preview_options","reply_markup"}
MEDIA = ("animation","video","audio","voice","document","sticker","video_note")


def supported(message):
    if message.get("is_automatic_forward") or message.get("is_ephemeral"):
        return False
    buttons = (message.get("reply_markup") or {}).get("inline_keyboard",[])
    if any(not button.get("url") for row in buttons for button in row):
        return False  # callback buttons belong to the original message's game/task
    return bool(message.get("text") or message.get("photo") or any(message.get(k) for k in MEDIA))


class Store:
    def __init__(self,vault):
        self.vault,self.db,self.lock = vault,vault.db,vault.lock
        with self.lock,self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS mg_chats(chat_id INTEGER PRIMARY KEY,enabled INTEGER NOT NULL,since INTEGER NOT NULL,cursor INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '')")
            self.db.execute("CREATE TABLE IF NOT EXISTS mg_messages(chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,root_id INTEGER NOT NULL,payload BLOB NOT NULL,created INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'saved',PRIMARY KEY(chat_id,message_id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS mg_attempts(chat_id INTEGER NOT NULL,root_id INTEGER NOT NULL,created INTEGER NOT NULL)")

    def chats(self):
        with self.lock,self.db:
            self.db.execute("DELETE FROM mg_messages WHERE created<?",(int(time.time())-RETENTION,))
            self.db.execute("DELETE FROM mg_attempts WHERE created<?",(int(time.time())-600,))
            return [dict(zip(("id","since","cursor"),r)) for r in self.db.execute("SELECT chat_id,since,cursor FROM mg_chats WHERE enabled=1")]

    def enabled(self,chat):
        with self.lock:
            row=self.db.execute("SELECT enabled FROM mg_chats WHERE chat_id=?",(chat,)).fetchone()
        return bool(row and row[0])

    def configure(self,chat,enabled):
        with self.lock,self.db:
            previous=self.db.execute("SELECT enabled FROM mg_chats WHERE chat_id=?",(chat,)).fetchone()
            if enabled and previous and previous[0]:
                return
            self.db.execute("INSERT INTO mg_chats(chat_id,enabled,since) VALUES (?,?,?) ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled,since=excluded.since,cursor=0,error=''",(chat,int(enabled),int(time.time())))
            # Re-enabling never revives previously attempted or deleted messages.
            if not enabled:
                self.db.execute("DELETE FROM mg_messages WHERE chat_id=?",(chat,))

    def status(self,chat):
        with self.lock:
            row=self.db.execute("SELECT enabled,error FROM mg_chats WHERE chat_id=?",(chat,)).fetchone()
            counts=dict(self.db.execute("SELECT state,COUNT(*) FROM mg_messages WHERE chat_id=? GROUP BY state",(chat,)).fetchall())
        return row,counts

    def advance(self,chat,cursor):
        with self.lock,self.db:
            self.db.execute("UPDATE mg_chats SET cursor=MAX(cursor,?) WHERE chat_id=?",(cursor,chat))

    def error(self,chat,message):
        with self.lock,self.db:
            self.db.execute("UPDATE mg_chats SET error=? WHERE chat_id=?",(message[:200],chat))

    def capture(self,message,bot_id):
        chat=(message.get("chat") or {})
        if chat.get("type") != "supergroup" or not self.enabled(chat.get("id")):
            return
        if (message.get("from") or {}).get("id") != bot_id:
            return
        if not supported(message):
            self.discard(chat["id"],[message["message_id"]]);return
        clean={k:v for k,v in message.items() if k in FIELDS}
        payload={"message":clean,"reply_id":(message.get("reply_to_message") or {}).get("message_id")}
        raw=json.dumps(payload,ensure_ascii=False).encode()
        if len(raw)>64*1024:
            return
        now=int(time.time());cid,mid=chat["id"],message["message_id"]
        with self.lock,self.db:
            self.db.execute("INSERT INTO mg_messages(chat_id,message_id,root_id,payload,created) VALUES (?,?,?,?,?) ON CONFLICT(chat_id,message_id) DO UPDATE SET payload=excluded.payload",(cid,mid,mid,self.vault.fernet.encrypt(raw),now))
            self.db.execute("DELETE FROM mg_messages WHERE created<?",(now-RETENTION,))
            self.db.execute("DELETE FROM mg_messages WHERE chat_id=? AND message_id NOT IN (SELECT message_id FROM mg_messages WHERE chat_id=? ORDER BY created DESC,message_id DESC LIMIT 1000)",(cid,cid))
            self.db.execute("DELETE FROM mg_attempts WHERE created<?",(now-600,))

    def discard(self,chat,ids):
        with self.lock,self.db:
            self.db.executemany("UPDATE mg_messages SET state='dismissed' WHERE chat_id=? AND message_id=?",((chat,mid) for mid in ids))

    def find(self,chat,mid):
        with self.lock:
            row=self.db.execute("SELECT root_id,payload,state,created FROM mg_messages WHERE chat_id=? AND message_id=?",(chat,mid)).fetchone()
        if not row or row[2]!='saved' or row[3]<time.time()-RETENTION:
            return None
        return row[0],json.loads(self.vault.fernet.decrypt(row[1]))

    def claim(self,chat,mid,root):
        now=int(time.time())
        with self.lock,self.db:
            self.db.execute("DELETE FROM mg_attempts WHERE created<?",(now-600,))
            if self.db.execute("SELECT COUNT(*) FROM mg_attempts WHERE chat_id=? AND (created>? OR root_id=?)",(chat,now-60,root)).fetchone()[0]>=10:
                return False
            if self.db.execute("SELECT COUNT(*) FROM mg_attempts WHERE chat_id=? AND root_id=?",(chat,root)).fetchone()[0]>=3:
                return False
            changed=self.db.execute("UPDATE mg_messages SET state='attempting' WHERE chat_id=? AND message_id=? AND state='saved' AND EXISTS(SELECT 1 FROM mg_chats WHERE chat_id=? AND enabled=1)",(chat,mid,chat)).rowcount
            if changed:
                self.db.execute("INSERT INTO mg_attempts VALUES (?,?,?)",(chat,root,now))
            return bool(changed)

    def finish(self,chat,mid,state,new_id=None,root=None):
        with self.lock,self.db:
            self.db.execute("UPDATE mg_messages SET state=? WHERE chat_id=? AND message_id=?",(state,chat,mid))
            if new_id:
                self.db.execute("UPDATE mg_messages SET root_id=? WHERE chat_id=? AND message_id=?",(root,chat,new_id))


class RecordingRequest(HTTPXRequest):
    def __init__(self,store,**kwargs):
        super().__init__(**kwargs)
        self.guard_store,self.bot_id=store,None

    async def do_request(self,url,method,request_data=None,**kwargs):
        status,raw=await super().do_request(url,method,request_data=request_data,**kwargs)
        if status==200 and self.bot_id:
            try:
                envelope=json.loads(raw);result=envelope.get("result")
                endpoint=url.rsplit("/",1)[-1]
                if envelope.get("ok") and endpoint in ("deleteMessage","deleteMessages"):
                    params=request_data.parameters if request_data else {}
                    ids=params.get("message_ids") or [params.get("message_id")]
                    self.guard_store.discard(params.get("chat_id"),ids)
                elif envelope.get("ok") and endpoint.startswith(("send","edit","copy","forward")):
                    for message in result if isinstance(result,list) else [result]:
                        if isinstance(message,dict) and message.get("message_id"):
                            self.guard_store.capture(message,self.bot_id)
            except Exception as error:
                LOG.warning("Guard could not record Bot API result: %s",type(error).__name__)
        return status,raw


async def resend(bot,payload):
    message=Message.de_json(payload["message"],bot)
    common={"chat_id":message.chat_id,"disable_notification":True}
    if message.is_topic_message:
        common["message_thread_id"]=message.message_thread_id
    if payload.get("reply_id"):
        common["reply_parameters"]=ReplyParameters(message_id=payload["reply_id"],allow_sending_without_reply=True)
    if message.reply_markup:
        common["reply_markup"]=message.reply_markup
    if message.text:
        return await bot.send_message(text=message.text,entities=message.entities,link_preview_options=message.link_preview_options,**common)
    captions={"caption":message.caption,"caption_entities":message.caption_entities}
    if message.photo:
        return await bot.send_photo(photo=message.photo[-1].file_id,has_spoiler=message.has_media_spoiler,show_caption_above_media=message.show_caption_above_media,**captions,**common)
    for kind in MEDIA:
        media=getattr(message,kind,None)
        if media:
            args={kind:media.file_id,**common}
            if kind not in ("sticker","video_note"):
                args.update(captions)
            if kind in ("video","animation"):
                args.update(has_spoiler=message.has_media_spoiler,show_caption_above_media=message.show_caption_above_media)
            return await getattr(bot,"send_"+kind)(**args)
    raise ValueError("Unsupported message")


class Worker:
    def __init__(self,store,session_file):
        self.store,self.session_file=store,Path(session_file)
        self.ready=False
        self.error="Наблюдатель ещё не подключён."
        self.retry_at={}

    async def event(self,bot,chat,event):
        message=event.old if event.deleted_message else None
        if not message or event.date.timestamp()<chat["since"]:
            return True
        mid=message.id
        item=self.store.find(chat["id"],mid)
        if not item:
            return True
        admins=await bot.get_chat_administrators(chat["id"])
        owner=next((a.user.id for a in admins if a.status=="creator"),None)
        if owner is None:
            raise ValueError("Cannot establish group owner")
        if event.user_id in (owner,bot.id):
            self.store.discard(chat["id"],[mid]);return True
        root,payload=item
        if not self.store.enabled(chat["id"]):
            return True
        if not self.store.claim(chat["id"],mid,root):
            return False  # throttle: keep the cursor, try on a later pass
        try:
            sent=await resend(bot,payload)
        except RetryAfter as error:
            # Telegram explicitly rejected the send. Retry later; uncertain network failures aren't retried.
            self.store.finish(chat["id"],mid,"saved")
            self.store.error(chat["id"],"Telegram ограничил частоту отправки; повтор после паузы.")
            delay=error.retry_after.total_seconds() if hasattr(error.retry_after,"total_seconds") else error.retry_after
            self.retry_at[chat["id"]]=time.monotonic()+max(20,delay)+1
            return False
        except (TelegramError,OSError,ValueError):
            self.store.finish(chat["id"],mid,"uncertain")
            self.store.error(chat["id"],"Не удалось подтвердить восстановление. Автоповтор исключён, чтобы не создавать дубликаты.")
            return True
        self.store.finish(chat["id"],mid,"restored",sent.message_id,root)
        return True

    async def run(self,bot):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.errors import FloodWaitError
        client=None
        try:
            config=guard_session.load(self.session_file,self.store.vault.fernet)
            client=TelegramClient(StringSession(config["session"]),config["api_id"],config["api_hash"],receive_updates=False,request_retries=1,flood_sleep_threshold=0)
            await asyncio.wait_for(client.connect(),30)
            if not await client.is_user_authorized() or (await client.get_me()).bot:
                raise ValueError("Account session not authorized")
            self.ready=True;self.error=""
            entities={}
            while True:
                for chat in self.store.chats():
                    if self.retry_at.get(chat["id"],0)>time.monotonic():
                        continue
                    try:
                        if chat["id"] not in entities:
                            async for dialog in client.iter_dialogs(limit=500):
                                if dialog.id==chat["id"]:
                                    entities[chat["id"]]=dialog.input_entity;break
                        if chat["id"] not in entities:
                            raise ValueError("Group not found among account dialogs")
                        events=[e async for e in client.iter_admin_log(entities[chat["id"]],delete=True,min_id=chat["cursor"],limit=201)]
                        if len(events)>200 and events[-1].date.timestamp()>=chat["since"]:
                            self.store.error(chat["id"],"Слишком большой пропуск журнала. Выключите и снова включите /guard.")
                            continue
                        for event in sorted(events,key=lambda e:e.id):
                            if not await self.event(bot,chat,event):
                                break
                            self.store.advance(chat["id"],event.id)
                    except FloodWaitError as error:
                        await asyncio.sleep(max(error.seconds,20))
                    except Exception as error:
                        self.store.error(chat["id"],"Нет доступа к журналу или соединения. Проверьте аккаунт и его права администратора.")
                        LOG.warning("Guard chat check failed: %s",type(error).__name__)
                await asyncio.sleep(20)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error="Не удалось подключить наблюдатель. Проверьте файл сессии и ключ шифрования; при отзыве входа выполните вход заново."
            LOG.warning("Guard observer unavailable: %s",type(error).__name__)
        finally:
            self.ready=False
            if client:
                await client.disconnect()


async def settings(app,user_id,chat_id,enabled=None):
    """Authenticated Mini App group controls: check current Telegram admin rights on every call."""
    if type(chat_id) is not int or not -(10**15)<chat_id<0:
        raise ValueError("Некорректный ID группы.")
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("enabled должен быть true или false.")
    allowed=app.bot_data.get("allowed")
    if allowed and user_id not in allowed:
        raise PermissionError("group_admin")
    try:
        member=await app.bot.get_chat_member(chat_id,user_id)
        if member.status not in ("administrator","creator"):
            raise PermissionError("group_admin")
        chat=await app.bot.get_chat(chat_id)
    except TelegramError:
        raise PermissionError("group_admin") from None
    if chat.type!="supergroup":
        raise ValueError("Восстановление доступно в супергруппе.")
    guard=app.bot_data.get("message_guard")
    store=guard.store if guard else Store(app.bot_data["connections"])
    ready=bool(guard and guard.ready)
    if enabled is True and not ready:
        raise ValueError((guard.error if guard else "") or "Наблюдатель не подключён. Загрузите файл сессии, задайте GUARD_ENABLED=1 в .env и перезапустите бота.")
    if enabled is not None:
        store.configure(chat_id,enabled)
    row,counts=store.status(chat_id)
    error=(guard.error if guard and not ready else "") or (row[1] if row else "")
    if not guard:
        error="Наблюдатель не настроен: загрузите файл сессии, задайте GUARD_ENABLED=1 в .env и перезапустите бота."
    return {"configured":bool(guard),"ready":ready,"enabled":bool(row and row[0]),
            "saved":counts.get("saved",0),"restored":counts.get("restored",0),
            "uncertain":counts.get("uncertain",0)+counts.get("attempting",0),"error":error}


async def command(update,context):
    message,user=update.effective_message,update.effective_user
    if not message or not user or message.chat.type!="supergroup":
        if message:
            await message.reply_text("Команда /guard работает в супергруппе. Включать защиту могут владелец и администраторы.")
        return
    allowed=context.bot_data.get("allowed")
    if allowed and user.id not in allowed:
        return
    member=await context.bot.get_chat_member(message.chat_id,user.id)
    if member.status not in ("administrator","creator") or message.sender_chat:
        await message.reply_text("Управлять восстановлением могут владелец и администраторы группы от своего аккаунта.");return
    guard=context.bot_data.get("message_guard")
    if not guard:
        await message.reply_text("Наблюдатель не настроен: загрузите .guard-session.enc и задайте GUARD_ENABLED=1 в .env хостинга, затем перезапустите бота.");return
    action=(context.args or ["status"])[0].lower()
    if action in ("on","off"):
        if action=="on" and not guard.ready:
            await message.reply_text(guard.error or "Наблюдатель ещё подключается.");return
        guard.store.configure(message.chat_id,action=="on")
    elif action!="status":
        await message.reply_text("/guard on — включить; /guard off — выключить; /guard status — состояние.");return
    row,counts=guard.store.status(message.chat_id)
    text="Восстановление сообщений: "+("включено" if row and row[0] else "выключено")
    text+=". Наблюдатель: "+("подключён" if guard.ready else "не подключён")+"."
    if not guard.ready and guard.error:
        text+="\n"+guard.error
    if row and row[1]:
        text+="\n"+row[1]
    text+=f"\nСохранено: {counts.get('saved',0)}; восстановлено: {counts.get('restored',0)}; неподтверждённых отправок: {counts.get('uncertain',0)+counts.get('attempting',0)}."
    await message.reply_text(text)
