"""Encrypted portable user authorization; shared by the offline login helper and the worker."""
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet


def cipher(secret):
    if not secret or not secret.strip():
        raise ValueError("Нужен ENCRYPTION_KEY сервера, а если он не задан — токен бота.")
    key = hashlib.sha256(("secret-ai-vault:" + secret.strip()).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def save(path, secret, api_id, api_hash, session):
    data = json.dumps({"api_id":int(api_id),"api_hash":api_hash,"session":session}).encode()
    encrypted = cipher(secret).encrypt(data)
    # Never overwrite an existing authorization file or print its contents.
    descriptor = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor,"wb") as output:
        output.write(encrypted)


def load(path, fernet):
    path = Path(path)
    if path.stat().st_size > 16_384:
        raise ValueError("Некорректный файл сессии.")
    data = json.loads(fernet.decrypt(path.read_bytes()))
    if (type(data.get("api_id")) is not int or data["api_id"] <= 0
            or not isinstance(data.get("api_hash"),str) or len(data["api_hash"]) != 32
            or not isinstance(data.get("session"),str) or not data["session"]):
        raise ValueError("Некорректный файл сессии.")
    return data
