"""Шифрование паролей устройств (Fernet, симметричное)."""
from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

log = logging.getLogger(__name__)

_PREFIX = "enc:"


def _build_key() -> bytes:
    """Возвращает валидный 32-байтный urlsafe-base64 ключ Fernet.

    Если SECRET_KEY уже валидный ключ Fernet — используем как есть.
    Иначе детерминированно растягиваем произвольную строку через SHA-256.
    Если ключ пуст — генерируем временный (с предупреждением).
    """
    raw = settings.secret_key.strip()
    if not raw:
        log.warning(
            "SECRET_KEY не задан — сгенерирован ВРЕМЕННЫЙ ключ. "
            "Сохранённые пароли НЕ расшифруются после перезапуска! "
            "Задайте SECRET_KEY в .env."
        )
        return Fernet.generate_key()
    try:
        # Проверяем, что строка — готовый ключ Fernet
        Fernet(raw.encode())
        return raw.encode()
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode()).digest()
        return base64.urlsafe_b64encode(digest)


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
