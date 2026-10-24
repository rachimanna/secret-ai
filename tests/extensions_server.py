"""Local Mini App fixture: real extension HTTP routes, SQLite and encryption, no live services."""
import json
import asyncio
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import codex
import integrations
import miniapp
import message_guard
from test_extensions import init_data

model='anymodel:xai/grok-4.7'
with tempfile.TemporaryDirectory() as folder:
    store=integrations.Connections(Path(folder)/'vault','fixture-token',owner=1)
    tasks=codex.CodexStore(store)
    group={'id':-1001234567890,'title':'Тестовая группа','level':'normal','messages':3,'stickers':0,'memes':0,'phrases':[],'model':''}
    observer=message_guard.Worker(message_guard.Store(store),'unused');observer.ready=True;observer.error=''
    group_bot=NS(get_chat_member=AsyncMock(side_effect=lambda chat,uid:NS(status='administrator' if uid==1 else 'member')),
                 get_chat=AsyncMock(return_value=NS(type='supergroup')))
    group_app=NS(bot=group_bot,bot_data={'connections':store,'message_guard':observer,'allowed':{1,2}})
    bridge=NS(settings=lambda uid:{'model':model,'models':[model],'persona':'assistant','premium':{},'messages':0,'personas':[{'id':'assistant','emoji':'','ru':'Ассистент','en':'Assistant'}]},
              gen_state=lambda uid:{'ready':False,'owner':True,'unlimited':True,'credits':0,'free_left':0,'free_per_day':0,'video_ready':False,'video_on':False},
              secretary=lambda uid:{'connected':False,'enabled':False},groups=lambda uid:[group],
              group_guard=lambda uid,chat,enabled=None:asyncio.run(message_guard.settings(group_app,uid,chat,enabled)))
    config={'token':'fixture-token','allowed':{1,2},'store':store,'public_url':'',
            'pipedream':NS(enabled=False),'direct':NS(connected=lambda uid:[]),
            'github':NS(creds=lambda:None),'bot_name':lambda:'fixture_bot','bridge':bridge,'codex':lambda:tasks}
    server=ThreadingHTTPServer(('127.0.0.1',0),miniapp.make_handler(config))
    print(json.dumps({'port':server.server_port,'initData':init_data(),'otherInitData':init_data(2)}),flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close();store.db.close()
