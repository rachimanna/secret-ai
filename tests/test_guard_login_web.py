import asyncio
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from telethon import types
from telethon.errors import SessionPasswordNeededError

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
        await self.send_code_request(phone)
        assert await code_callback()=='11111'
        assert await password()=='fixture-password'

    async def send_code_request(self,phone):
        return NS(type=types.auth.SentCodeTypeApp(length=5),phone_code_hash='DO_NOT_DISCLOSE')


class FakeQR:
    url='tg://login?token=FAKE_QR_NOT_A_LOGIN'
    def __init__(self,scanned,with_password=False,expire_once=False):
        self.scanned,self.with_password,self.expire_once=scanned,with_password,expire_once
        self.recreated=0

    async def wait(self):
        if self.expire_once and not self.recreated:
            raise asyncio.TimeoutError()
        if not await asyncio.to_thread(self.scanned.wait,5):raise TimeoutError()
        if self.with_password:raise SessionPasswordNeededError(request=None)
        return NS(id=1,bot=False)

    async def recreate(self):
        self.recreated+=1


class FakeQRClient(FakeClient):
    scanned=threading.Event()
    with_password=False
    expire_once=False
    instances=[]
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.receives_updates=kwargs['receive_updates']
        self.connect=AsyncMock();self.sign_in=AsyncMock()
        self.qr=FakeQR(self.scanned,self.with_password,self.expire_once)
        self.instances.append(self)

    async def qr_login(self):return self.qr


class AutoQRClient(FakeQRClient):
    async def qr_login(self):
        async def accepted():
            await asyncio.sleep(3)
            return NS(id=1,bot=False)
        self.qr.wait=accepted
        return self.qr


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

    def test_delivery_comes_from_telegram_response_not_a_fixed_label(self):
        for actual,delivery,label in [(types.auth.SentCodeTypeApp(length=5),'app','Telegram'),
                (types.auth.SentCodeTypeSms(length=5),'sms','SMS'),
                (types.auth.SentCodeTypeEmailCode(email_pattern='m***@example.com',length=5),'email','почты')]:
            self.wizard.record_delivery(NS(type=actual,phone_code_hash='DO_NOT_DISCLOSE'))
            state=self.wizard.state()
            self.assertEqual(state['delivery'],delivery)
            self.assertIn(label,state['code_label'])
            self.assertNotIn('DO_NOT_DISCLOSE',str(state))

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

    def test_qr_login_without_phone_or_code_and_expired_token_refresh(self):
        class Client(FakeQRClient):
            scanned=threading.Event();expire_once=True;instances=[]
        self.wizard.client_factory=Client
        self.wizard.submit(dict(mode='qr',api_id='123',api_hash='a'*32,secret='fixture-key'))
        try:
            self.wait_phase('qr')
            self.assertIn('<svg',self.wizard.state()['qr_svg'])
            Client.scanned.set()
            self.wait_phase('done')
            self.assertEqual(self.wizard.state()['qr_svg'],'')
            self.assertTrue(Client.instances[0].receives_updates)
            self.assertEqual(Client.instances[0].qr.recreated,1)
        finally:Client.scanned.set()

    def test_qr_two_factor_password_is_still_required(self):
        class Client(FakeQRClient):
            scanned=threading.Event();with_password=True;instances=[]
        self.wizard.client_factory=Client
        self.wizard.submit(dict(mode='qr',api_id='123',api_hash='a'*32,secret='fixture-key'))
        try:
            self.wait_phase('qr');Client.scanned.set();self.wait_phase('password')
            self.assertEqual(self.wizard.state()['qr_svg'],'')
            self.wizard.submit(dict(phase='password',answer='fixture-password'))
            self.wait_phase('done')
            Client.instances[0].sign_in.assert_awaited_once_with(password='fixture-password')
        finally:Client.scanned.set()

    def test_http_requires_private_capability_and_never_returns_credentials(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),web.make_handler(self.wizard))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}') as client:
                rejected=client.post('/api/state',json={'unused':'fixture body'})
                self.assertEqual(rejected.status_code,403)
                self.assertIn('запусти программу',rejected.json()['error'])
                result=client.post('/api/state',json={},headers={'X-Guard-Token':self.wizard.token})
                self.assertEqual(result.status_code,200)
                self.assertEqual(set(result.json()),{'phase','message','delivery','code_label','qr_svg'})
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
