"""Шифрование паролей устройств (Fernet, симметричное)."""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

log = logging.getLogger(__name__)

_PREFIX = "enc:"
# Персистентный автоген-ключ (когда ни NVR_SECRET_KEY, ни SECRET_KEY не заданы).
_KEY_FILE = "data/secret.key"


def _key_from_raw(raw: str) -> bytes:
    """Строка → валидный ключ Fernet (как есть либо растянуть через SHA-256)."""
    try:
        Fernet(raw.encode())
        return raw.encode()
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode()).digest()
        return base64.urlsafe_b64encode(digest)


def _persistent_key() -> bytes:
    """Читает ключ из data/secret.key, при отсутствии — генерирует и сохраняет.

    Позволяет паролям переживать перезапуск даже без заданного SECRET_KEY.
    """
    try:
        if os.path.exists(_KEY_FILE):
            with open(_KEY_FILE, "rb") as fh:
                data = fh.read().strip()
            if data:
                return data
        os.makedirs(os.path.dirname(_KEY_FILE) or ".", exist_ok=True)
        key = Fernet.generate_key()
        with open(_KEY_FILE, "wb") as fh:
            fh.write(key)
        try:
            os.chmod(_KEY_FILE, 0o600)
        except OSError:
            pass
        log.warning(
            "Мастер-ключ шифрования не задан — сгенерирован и сохранён в %s. "
            "СОХРАНИТЕ его (или задайте NVR_SECRET_KEY) — иначе пароли не "
            "расшифруются после потери файла.",
            _KEY_FILE,
        )
        return key
    except OSError as exc:
        log.error("Не удалось сохранить ключ в %s: %s — ключ временный!", _KEY_FILE, exc)
        return Fernet.generate_key()


def _build_key() -> bytes:
    """Возвращает 32-байтный ключ Fernet.

    Приоритет: NVR_SECRET_KEY → SECRET_KEY → персистентный data/secret.key.
    """
    raw = (settings.nvr_secret_key or settings.secret_key).strip()
    if raw:
        return _key_from_raw(raw)
    return _persistent_key()


_fernet = Fernet(_build_key())


def encrypt(plaintext: str) -> str:
    return _PREFIX + _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    if not ciphertext.startswith(_PREFIX):
        # Обратная совместимость: значение не зашифровано
        return ciphertext
    token = ciphertext[len(_PREFIX):].encode()
    try:
        return _fernet.decrypt(token).decode()
    except InvalidToken:
        log.error("Не удалось расшифровать пароль (изменился SECRET_KEY?)")
        return ""
