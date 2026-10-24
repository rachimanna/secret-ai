"""Run yourself in a private local terminal. Never paste login codes into chat or hosting logs."""
import argparse
import asyncio
import getpass
import re
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import guard_session


async def login(output):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    if not sys.stdin.isatty():
        raise ValueError("Откройте обычный терминал на своём компьютере; консоль панели хостинга не подходит для скрытого ввода.")
    if Path(output).exists():
        raise ValueError("Файл уже существует. Выберите другое имя через --output.")
    api_id = int(input("Ваш api_id: ").strip())
    api_hash = input("Ваш api_hash (текст виден): ").strip()
    if api_id <= 0 or not re.fullmatch(r"[a-fA-F0-9]{32}",api_hash):
        raise ValueError("Проверьте api_id и api_hash.")
    print("Нужен тот же ENCRYPTION_KEY, который указан в .env бота на сервере.")
    print("Если ENCRYPTION_KEY не задан, используйте TELEGRAM_TOKEN этого бота. Значение никуда не отправляется.")
    secret = getpass.getpass("Ключ шифрования сервера (ввод скрыт): ").strip()
    guard_session.cipher(secret)
    client = TelegramClient(StringSession(),api_id,api_hash,receive_updates=False)
    try:
        await client.start(phone=lambda:getpass.getpass("Номер аккаунта с + (ввод скрыт): "),
                           code_callback=lambda:getpass.getpass("Код из Telegram (ввод скрыт): "),
                           password=lambda:getpass.getpass("Пароль двухэтапной проверки (ввод скрыт): "))
        me = await client.get_me()
        if me.bot:
            raise ValueError("Нужен пользовательский аккаунт, а не бот.")
        guard_session.save(output,secret,api_id,api_hash,client.session.save())
        print("Готово. Зашифрованный файл сохранён:",Path(output).resolve())
        print("Загрузите его в папку бота на hCloud. Не публикуйте файл, ключ и коды входа.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description="Безопасный локальный вход наблюдателя Secret AI")
    parser.add_argument("--output",default=".guard-session.enc")
    args=parser.parse_args()
    try:
        asyncio.run(login(args.output))
    except (ValueError,FileExistsError) as error:
        print(str(error),file=sys.stderr);sys.exit(1)
    except KeyboardInterrupt:
        print("Вход отменён.",file=sys.stderr);sys.exit(1)
    except Exception as error:
        print("Вход не завершён:",type(error).__name__,"— проверьте подключение и повторите.",file=sys.stderr);sys.exit(1)
