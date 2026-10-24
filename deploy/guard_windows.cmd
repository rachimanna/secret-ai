@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "tools\guard_login_web.py" (
  echo Рядом с файлом запуска нет папки tools. Распакуйте архив целиком.
  pause
  exit /b 1
)
set "PYEXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
if not exist ".venv\Scripts\python.exe" "%PYEXE%" -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-cache-dir "Telethon>=1.45,<2" "cryptography>=42,<46" "qrcode>=8,<9"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" tools\guard_login_web.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo Не удалось запустить помощник. Если Python не найден, установите Python 3.12.
pause
exit /b 1
