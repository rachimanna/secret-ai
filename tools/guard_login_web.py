"""Local browser login wizard. All fields are visible; credentials stay in memory until encryption."""
import argparse
import asyncio
import hmac
import json
import queue
import re
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import guard_session

PAGE = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Secret AI — подключение аккаунта</title><style>
*{box-sizing:border-box}body{margin:0;background:#111827;color:#edf2fa;font:17px/1.5 system-ui,sans-serif;padding:30px 16px}
main{max-width:640px;margin:auto;background:#1e293b;padding:28px;border-radius:20px}h1{font-size:26px;margin:0 0 12px}p{color:#becbdb}
label{display:block;margin-top:18px}input{width:100%;padding:13px;border:1px solid #64748b;border-radius:10px;background:#0f172a;color:white;font:inherit;min-width:0}
.line{display:flex;gap:8px}.line input{flex:1}.line button{width:auto}button{cursor:pointer;border:0;border-radius:10px;background:#3b82f6;color:white;padding:13px;font:inherit}
button:disabled{opacity:.5;cursor:wait}button[type=submit]{width:100%;margin-top:24px}.hint{font-size:14px;color:#a8b8ce}#status{white-space:pre-wrap;overflow-wrap:anywhere}#status.error{color:#fca5a5}input[aria-invalid=true]{border:2px solid #f87171}
[hidden]{display:none!important}@media(max-width:480px){main{padding:20px}.line{flex-wrap:wrap}.line button{font-size:14px}}
</style><main><h1>Подключение аккаунта</h1><p>Ввод виден. Можно вставлять мышью или кнопкой «Вставить». Данные передаются только локальной программе на этом компьютере.</p>
<form id="setup">
<label for="api-id">api_id</label><div class="line"><input id="api-id" inputmode="numeric" autocomplete="off" required><button type="button" data-paste="api-id">Вставить</button></div>
<label for="api-hash">api_hash</label><div class="line"><input id="api-hash" autocomplete="off" spellcheck="false" required><button type="button" data-paste="api-hash">Вставить</button></div><div class="hint" id="hash-count">Нужно 32 символа.</div>
<label for="phone">Номер аккаунта с +</label><div class="line"><input id="phone" type="tel" autocomplete="off" required><button type="button" data-paste="phone">Вставить</button></div>
<label for="secret">Ключ шифрования бота на сервере</label><div class="line"><input id="secret" autocomplete="off" spellcheck="false" required><button type="button" data-paste="secret">Вставить</button></div>
<p class="hint">Возьми ENCRYPTION_KEY из .env бота на hCloud. Если он не задан — TELEGRAM_TOKEN этого бота. Этот ключ не отправляется в Telegram.</p>
<button type="submit">Продолжить</button></form>
<form id="prompt" hidden><label for="answer" id="question"></label><div class="line"><input id="answer" autocomplete="off" spellcheck="false"><button type="button" data-paste="answer">Вставить</button></div><button type="submit">Подтвердить</button></form>
<p id="status" role="status" aria-live="polite"></p><p class="hint">Не закрывай чёрное окно до завершения входа. Не публикуй снимок экрана с заполненными полями. После входа будет сохранён только зашифрованный файл сессии.</p></main>
<script>
let token=location.hash.slice(1);
try{token=token||sessionStorage.getItem('guard-access')||'';if(token)sessionStorage.setItem('guard-access',token);history.replaceState(null,'',location.pathname);}catch{}
const $=id=>document.getElementById(id);let phase='setup',sending=false,hasError=false;
const status=text=>{$('status').textContent=text;$('status').className='';};
function showError(message,field){hasError=true;status(message);$('status').className='error';if(field&&$(field)){$(field).setAttribute('aria-invalid','true');$(field).focus();}}
async function api(action,data={}){if(!token)throw Error('Эта вкладка открыта без ключа связи. Закрой её и запусти файл помощника заново.');let r;try{r=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Guard-Token':token},body:JSON.stringify(data)});}catch{throw Error('Нет связи с локальной программой. Не закрывай чёрное окно запуска. Если оно уже закрыто, закрой эту вкладку и запусти помощник заново.');}const result=await r.json();if(!r.ok){const error=Error(result.error||'Не удалось выполнить запрос');error.field=result.field;throw error;}return result;}
document.querySelectorAll('input').forEach(input=>input.addEventListener('input',()=>{input.removeAttribute('aria-invalid');hasError=false;}));
document.querySelectorAll('[data-paste]').forEach(b=>b.onclick=async()=>{try{const field=$(b.dataset.paste);field.value=(await navigator.clipboard.readText()).trim();field.dispatchEvent(new Event('input'));field.focus();}catch{status('Нажми правой кнопкой мыши внутри нужного поля и выбери «Вставить».');}});
$('api-hash').oninput=()=>{$('hash-count').textContent='Введено символов: '+$('api-hash').value.trim().length+' из 32';};
function draw(s){if(hasError&&phase===s.phase)return;hasError=false;const changed=phase!==s.phase;phase=s.phase;$('setup').hidden=phase!=='setup';$('prompt').hidden=!['code','password'].includes(phase);
if(changed){$('answer').value='';if(phase==='code'||phase==='password'){$('question').textContent=phase==='code'?(s.code_label||'Код входа'):'Пароль двухэтапной проверки';$('answer').focus();}if(phase==='working'||phase==='done'){for(const id of ['api-id','api-hash','secret','phone'])$(id).value='';}}
status(s.message||'');document.querySelectorAll('button[type=submit]').forEach(b=>b.disabled=sending||phase==='working');}
async function submit(data){sending=true;hasError=false;document.querySelectorAll('button[type=submit]').forEach(b=>b.disabled=true);try{draw(await api('submit',data));}catch(e){showError(e.message,e.field);}finally{sending=false;document.querySelectorAll('button[type=submit]').forEach(b=>b.disabled=phase==='working');}}
$('setup').onsubmit=e=>{e.preventDefault();if(!/^[0-9]+$/.test($('api-id').value.trim())){showError('В api_id нужны только цифры из my.telegram.org.','api-id');return;}if(!/^[a-fA-F0-9]{32}$/.test($('api-hash').value.trim())){showError('В api_hash должно быть ровно 32 символа: цифры и латинские буквы a–f.','api-hash');return;}submit({api_id:$('api-id').value.trim(),api_hash:$('api-hash').value.trim(),phone:$('phone').value.trim(),secret:$('secret').value.trim()});};
$('prompt').onsubmit=e=>{e.preventDefault();const answer=phase==='password'?$('answer').value:$('answer').value.trim();if(answer)submit({answer,phase});};
setInterval(async()=>{if(sending)return;try{draw(await api('state'));}catch{}},800);
</script></html>'''


class ValidationError(ValueError):
    def __init__(self,message,field=None):
        super().__init__(message)
        self.field=field


class Wizard:
    def __init__(self,output,client_factory=None):
        self.output=Path(output);self.client_factory=client_factory
        self.token=secrets.token_urlsafe(32)
        self.lock=threading.Lock();self.answers=queue.Queue()
        self.phase='setup';self.message='Заполни поля и нажми «Продолжить».'
        self.delivery='unknown';self.code_label='Код входа'
        self.code_hint='Telegram запросил код входа. Способ доставки пока не определён.'
        self.last_seen=time.monotonic()

    def state(self):
        with self.lock:
            return {'phase':self.phase,'message':self.message,'delivery':self.delivery,'code_label':self.code_label}

    def record_delivery(self,sent):
        kind=type(getattr(sent,'type',None)).__name__
        if kind=='SentCodeTypeApp':
            delivery,label,hint='app','Код из служебного чата Telegram','Telegram выбрал доставку сообщением в приложение. Открой служебный чат Telegram с галочкой в том аккаунте, который уже открыт на компьютере.'
        elif kind in ('SentCodeTypeSms','SentCodeTypeSmsWord','SentCodeTypeSmsPhrase','SentCodeTypeFirebaseSms'):
            delivery,label,hint='sms','Код из SMS','Telegram выбрал доставку SMS на номер аккаунта. Для этого способа нужен доступ к SMS номера. Помощник не может принудительно заменить SMS сообщением в Telegram.'
        elif kind=='SentCodeTypeEmailCode':
            delivery,label,hint='email','Код из электронной почты','Telegram выбрал доставку кода на привязанную электронную почту. Проверь почту этого аккаунта.'
        elif kind in ('SentCodeTypeCall','SentCodeTypeFlashCall','SentCodeTypeMissedCall'):
            delivery,label,hint='call','Код из звонка','Telegram выбрал доставку звонком. Для этого способа нужен доступ к звонкам номера; сообщение в Telegram этим запросом не выбрано.'
        elif kind=='SentCodeTypeFragmentSms':
            delivery,label,hint='fragment','Код через Fragment','Telegram выбрал получение кода через Fragment. Обычный служебный чат Telegram этим запросом не выбран.'
        else:
            delivery,label,hint='unknown','Код входа','Telegram запросил код, но эта версия помощника не распознала способ доставки. Нельзя утверждать, что он отправлен сообщением в Telegram.'
        with self.lock:
            self.delivery,self.code_label,self.code_hint=delivery,label,hint

    def set(self,phase,message):
        with self.lock:
            self.phase,self.message=phase,message

    async def ask(self,phase):
        self.set(phase,self.code_hint if phase=='code' else 'Введи пароль двухэтапной проверки.')
        answer=await asyncio.to_thread(self.answers.get,True,600)
        if answer is None:
            raise ValueError('Cancelled')
        return answer

    def submit(self,data):
        with self.lock:
            if self.phase=='setup':
                try:
                    api_id=int(data.get('api_id',''))
                except (TypeError,ValueError):
                    raise ValidationError('В поле api_id нужны только цифры из my.telegram.org.','api-id') from None
                if api_id<=0:
                    raise ValidationError('api_id должен быть положительным числом.','api-id')
                api_hash=data.get('api_hash','');phone=data.get('phone','');secret=data.get('secret','')
                if isinstance(api_hash,str):api_hash=api_hash.strip()
                if not isinstance(api_hash,str) or not re.fullmatch(r'[a-fA-F0-9]{32}',api_hash):
                    raise ValidationError('В api_hash должно быть ровно 32 символа: цифры и латинские буквы a–f. Скопируй только значение App api_hash.','api-hash')
                if isinstance(phone,str):phone=re.sub(r'[\s()\-]+','',phone)
                if not isinstance(phone,str) or not re.fullmatch(r'\+[0-9]{7,16}',phone):
                    raise ValidationError('Номер телефона: начни с + и кода страны. Пробелы, скобки и дефисы можно оставить.','phone')
                if isinstance(secret,str):secret=secret.strip()
                if not isinstance(secret,str) or not 1<=len(secret)<=1024:
                    raise ValidationError('Поле ключа сервера пустое или слишком длинное. Нужен ENCRYPTION_KEY из .env; если он не задан — TELEGRAM_TOKEN.','secret')
                guard_session.cipher(secret)
                if self.output.exists():
                    raise ValidationError('В этой папке уже есть файл '+self.output.name+'. Помощник не перезаписывает его. Если вход уже завершён, используй готовый файл; для нового входа запусти помощник из другой папки.')
                self.phase,self.message='working','Подключаюсь к Telegram…'
                threading.Thread(target=self.run,args=(api_id,api_hash,phone,secret),daemon=True).start()
            elif self.phase in ('code','password') and data.get('phase')==self.phase:
                answer=data.get('answer')
                if not isinstance(answer,str) or not answer or len(answer)>1024:
                    raise ValidationError('Заполни поле кода или пароля.','answer')
                phase=self.phase
                self.phase,self.message='working','Проверяю данные…'
                self.answers.put(answer if phase=='password' else answer.strip())
            else:
                raise ValidationError('Дождись следующего шага: запрос уже обрабатывается.')

    def run(self,api_id,api_hash,phone,secret):
        try:
            asyncio.run(self.authorize(api_id,api_hash,phone,secret))
            self.set('done','Готово! Зашифрованный файл сохранён:\n'+str(self.output.resolve())+'\nЗагрузи его в папку бота на hCloud после установки обновления с /guard.')
        except Exception as error:
            self.set('error','Вход не завершён ('+type(error).__name__+'). Закрой программу и запусти её заново. Проверь данные и подключение.')

    async def authorize(self,api_id,api_hash,phone,secret):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        wizard=self
        class DeliveryClient(self.client_factory or TelegramClient):
            async def send_code_request(self,*args,**kwargs):
                sent=await super().send_code_request(*args,**kwargs)
                wizard.record_delivery(sent)
                return sent
        client=DeliveryClient(StringSession(),api_id,api_hash,receive_updates=False)
        try:
            await client.start(phone=phone,code_callback=lambda:self.ask('code'),password=lambda:self.ask('password'))
            if (await client.get_me()).bot:
                raise ValueError('User account required')
            guard_session.save(self.output,secret,api_id,api_hash,client.session.save())
        finally:
            await client.disconnect()


def make_handler(wizard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def send(self,status,data,kind='application/json; charset=utf-8'):
            raw=data.encode() if isinstance(data,str) else json.dumps(data,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers();self.wfile.write(raw)

        def do_GET(self):
            if self.path=='/':self.send(200,PAGE,'text/html; charset=utf-8')
            else:self.send(404,{'error':'Не найдено'})

        def do_POST(self):
            try:
                size=int(self.headers.get('Content-Length') or 0)
                if not 0<=size<=8192:raise ValueError()
            except ValueError:
                self.send(400,{'error':'Некорректный размер запроса.'});return
            # Consume the small body before replying, including on denied access. Closing a socket
            # with unread bytes can reset it on Windows and hide the actual 403 from fetch().
            raw=self.rfile.read(size)
            if not hmac.compare_digest(self.headers.get('X-Guard-Token',''),wizard.token):
                self.send(403,{'error':'Закрой эту вкладку и запусти программу заново.'});return
            try:
                data=json.loads(raw or b'{}')
                if not isinstance(data,dict):raise ValueError('Некорректный запрос.')
                wizard.last_seen=time.monotonic()
                if self.path=='/api/submit':wizard.submit(data)
                elif self.path!='/api/state':self.send(404,{'error':'Не найдено'});return
                self.send(200,wizard.state())
            except ValidationError as error:
                self.send(400,{'error':str(error),'field':error.field})
            except (ValueError,TypeError):
                self.send(400,{'error':'Не удалось прочитать запрос. Закрой вкладку и заново открой помощник.'})
    return Handler


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Вход Secret AI с видимым текстом и кнопками вставки')
    parser.add_argument('--output',default='.guard-session.enc');parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args();wizard=Wizard(args.output)
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(wizard));server.daemon_threads=True;server.timeout=1
    address=f'http://127.0.0.1:{server.server_port}/#{wizard.token}'
    print('Откроется окно браузера. Все поля видны; есть кнопки «Вставить». Не закрывай это чёрное окно до завершения.')
    if not args.no_browser:webbrowser.open(address)
    else:print(address,flush=True)
    try:
        while time.monotonic()-wizard.last_seen<1800:server.handle_request()
    except KeyboardInterrupt:pass
    finally:
        wizard.answers.put(None)
        server.server_close()
