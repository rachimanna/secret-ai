"""Browser fixture with a fake Telegram client. No account authorization or network calls."""
import json
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import guard_login_web as web
from test_guard_login_web import FakeClient

with tempfile.TemporaryDirectory() as folder:
    wizard=web.Wizard(Path(folder)/'session.enc',FakeClient)
    server=ThreadingHTTPServer(('127.0.0.1',0),web.make_handler(wizard));server.daemon_threads=True
    print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/#{wizard.token}'}),flush=True)
    try:server.serve_forever()
    finally:server.server_close()
