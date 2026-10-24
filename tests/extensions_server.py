"""Local Mini App fixture: real extension HTTP routes, SQLite and encryption, no live services."""
import json
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import codex
import integrations
import miniapp
from test_extensions import init_data

model='anymodel:xai/grok-4.7'
with tempfile.TemporaryDirectory() as folder:
    store=integrations.Connections(Path(folder)/'vault','fixture-token',owner=1)
    tasks=codex.CodexStore(store)
    bridge=NS(settings=lambda uid:{'model':model,'models':[model],'persona':'assistant','premium':{},'messages':0,'personas':[{'id':'assistant','emoji':'','ru':'Ассистент','en':'Assistant'}]},
              gen_state=lambda uid:{'ready':False,'owner':True,'unlimited':True,'credits':0,'free_left':0,'free_per_day':0,'video_ready':False,'video_on':False},
              secretary=lambda uid:{'connected':False,'enabled':False},groups=lambda uid:[])
    config={'token':'fixture-token','allowed':{1,2},'store':store,'public_url':'',
            'pipedream':NS(enabled=False),'direct':NS(connected=lambda uid:[]),
            'github':NS(creds=lambda:None),'bot_name':lambda:'fixture_bot','bridge':bridge,'codex':lambda:tasks}
    server=ThreadingHTTPServer(('127.0.0.1',0),miniapp.make_handler(config))
    print(json.dumps({'port':server.server_port,'initData':init_data(),'otherInitData':init_data(2)}),flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close();store.db.close()
