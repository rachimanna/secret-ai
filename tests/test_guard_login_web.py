import asyncio
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx

import guard_session
from tools import guard_login_web as web


class FakeClient:
    def __init__(self,*args,**kwargs):
        self.session=NS(save=lambda:'FAKE_SESSION')
        self.disconnect=AsyncMock()
        self.get_me=AsyncMock(return_value=NS(bot=False))

    async def start(self,phone,code_callback,password):
        assert phone=='+79991234567'
        assert await code_callback()=='11111'
        assert await password()=='fixture-password'


class WebLoginTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name)/'session.enc'
        self.wizard=web.Wizard(self.output,FakeClient)

    def wait_phase(self,phase):
        until=time.monotonic()+5
        while self.wizard.state()['phase']!=phase and time.monotonic()<until:
            time.sleep(.01)
        self.assertEqual(self.wizard.state()['phase'],phase)

    def test_visible_fields_and_clipboard_button(self):
        self.assertIn('data-paste="api-hash"',web.PAGE)
        self.assertIn("navigator.clipboard.readText()",web.PAGE)
        self.assertNotIn('type="password"',web.PAGE)

    def test_login_stages_and_encrypted_output_without_real_telegram(self):
        self.wizard.submit(dict(api_id='123',api_hash='a'*32,phone='+79991234567',secret='fixture-key'))
        self.wait_phase('code')
        self.assertNotIn('a'*32,str(self.wizard.state()))
        self.wizard.submit(dict(phase='code',answer='11111'))
        self.wait_phase('password')
        self.wizard.submit(dict(phase='password',answer='fixture-password'))
        self.wait_phase('done')
        self.assertNotIn(b'FAKE_SESSION',self.output.read_bytes())
        self.assertEqual(guard_session.load(self.output,guard_session.cipher('fixture-key'))['session'],'FAKE_SESSION')

    def test_invalid_setup_does_not_start_login(self):
        with self.assertRaises(ValueError):
            self.wizard.submit(dict(api_id='123',api_hash='bad',phone='+79991234567',secret='key'))
        self.assertEqual(self.wizard.state()['phase'],'setup')
        self.assertFalse(self.output.exists())

    def test_password_spaces_are_preserved(self):
        self.wizard.set('password','Password')
        self.wizard.submit({'phase':'password','answer':' fixture password '})
        self.assertEqual(self.wizard.answers.get_nowait(),' fixture password ')

    def test_formatted_phone_number_is_accepted(self):
        self.wizard.submit(dict(api_id='123',api_hash='a'*32,phone='+7 (999) 123-45-67',secret='fixture-key'))
        self.wait_phase('code')
        self.wizard.answers.put('11111')
        self.wait_phase('password')
        self.wizard.answers.put('fixture-password')
        self.wait_phase('done')

    def test_errors_identify_field_without_echoing_secrets(self):
        good=dict(api_id='123',api_hash='a'*32,phone='+79991234567',secret='fixture-key')
        for field,value,expected in [('api_id','wrong','api-id'),('api_hash','private-invalid-hash','api-hash'),('phone','79991234567','phone'),('secret',' ','secret')]:
            with self.subTest(field=field),self.assertRaises(web.ValidationError) as caught:
                self.wizard.submit(dict(good,**{field:value}))
            self.assertEqual(caught.exception.field,expected)
            self.assertNotIn('private-invalid-hash',str(caught.exception))
        self.output.write_bytes(b'preserved')
        with self.assertRaisesRegex(web.ValidationError,'уже есть файл'):
            self.wizard.submit(good)
        self.assertEqual(self.output.read_bytes(),b'preserved')

    def test_http_requires_private_capability_and_never_returns_credentials(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),web.make_handler(self.wizard))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}') as client:
                self.assertEqual(client.post('/api/state').status_code,403)
                result=client.post('/api/state',json={},headers={'X-Guard-Token':self.wizard.token})
                self.assertEqual(result.status_code,200)
                self.assertEqual(set(result.json()),{'phase','message'})
                page=client.get('/')
                self.assertNotIn(self.wizard.token,page.text)
                self.assertEqual(page.headers['Cache-Control'],'no-store')
                invalid=client.post('/api/submit',json={'api_id':'not-an-id'},headers={'X-Guard-Token':self.wizard.token})
                self.assertEqual(invalid.status_code,400)
                self.assertEqual(invalid.json()['field'],'api-id')
                self.assertIn('только цифры',invalid.json()['error'])
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':
    unittest.main()
