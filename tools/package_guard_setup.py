"""Build a self-extracting Windows launcher from a fixed list of source files, never session data."""
import argparse
import base64
import io
import textwrap
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
README='''Secret AI — видимый ввод

Запусти START-WINDOWS.cmd. Откроется форма в браузере с видимыми полями и кнопками «Вставить».
Не закрывай чёрное окно, пока вход не завершён. Подтверждай шаги кнопками на странице.

Нужны api_id, api_hash и ключ шифрования сервера. По умолчанию выбран вход по QR.
На iPhone открой нужный аккаунт Telegram → Настройки → Устройства → Подключить устройство.
Отсканируй QR с экрана компьютера и подтверди вход. SMS не нужны. Если включена
двухэтапная проверка, помощник запросит её пароль. QR обновляется автоматически.
Номер аккаунта понадобится только для отдельного режима «Через код».
Ключ — ENCRYPTION_KEY из .env бота на hCloud; если он не задан, TELEGRAM_TOKEN этого бота.
Код из Telegram и пароль двухэтапной проверки вводятся на следующих шагах.

После входа здесь появится .guard-session.enc. Не присылай его, ключ или снимок заполненной формы в чат.
После установки обновления бота загрузи файл на hCloud рядом с bot.py и добавь в .env:
GUARD_ENABLED=1
GUARD_SESSION_FILE=.guard-session.enc
Перезапусти бота. Владелец супергруппы включает восстановление командой /guard on.
Состояние — /guard status, выключение — /guard off.
'''
LAUNCHER=r'''@echo off
chcp 65001 >nul
setlocal
set "GUARD_SETUP_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $text=[IO.File]::ReadAllText($env:GUARD_SETUP_SELF); $payload=($text -split '(?m)^:GUARD_PAYLOAD\r?$',2)[1]; $folder=Join-Path ([IO.Path]::GetDirectoryName($env:GUARD_SETUP_SELF)) 'Secret-AI-Guard-Visible'; [IO.Directory]::CreateDirectory($folder)|Out-Null; $zip=Join-Path $folder 'setup.zip'; [IO.File]::WriteAllBytes($zip,[Convert]::FromBase64String($payload)); Expand-Archive -LiteralPath $zip -DestinationPath $folder -Force;"
if errorlevel 1 goto failed
call "%~dp0Secret-AI-Guard-Visible\START-WINDOWS.cmd"
exit /b %errorlevel%
:failed
echo Не удалось распаковать помощник. Перемести файл в обычную папку с правом записи и повтори.
pause
exit /b 1
:GUARD_PAYLOAD
'''


def build(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    files={
        'guard_session.py':(ROOT/'guard_session.py').read_bytes(),
        'tools/guard_login_web.py':(ROOT/'tools/guard_login_web.py').read_bytes(),
        'START-WINDOWS.cmd':(ROOT/'deploy/guard_windows.cmd').read_text(encoding='utf-8').replace('\r\n','\n').replace('\n','\r\n').encode(),
        'ПРОЧТИ.txt':README.encode(),
    }
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():archive.writestr(name,data)
    encoded='\n'.join(textwrap.wrap(base64.b64encode(raw.getvalue()).decode(),76))
    launcher=output/'START-GUARD-VISIBLE.cmd'
    launcher.write_bytes((LAUNCHER+encoded+'\n').replace('\n','\r\n').encode('utf-8'))
    with zipfile.ZipFile(output/'Secret-AI-Guard-Setup.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():archive.writestr('GuardSetup/'+name,data)
    print('Created:',launcher)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output');build(parser.parse_args().output)
