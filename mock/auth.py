"""Проверка HTTP Digest / Basic auth на стороне mock-сервера.

Реалистично эмулирует digest-челлендж Hikvision/Dahua, чтобы httpx.DigestAuth
проходил полный цикл 401 → Authorization. Дополнительно принимает Basic
(нюанс некоторых прошивок Dahua).
"""
from __future__ import annotations

import base64
import hashlib

from fastapi import HTTPException, Request

_NONCE = "0a0b0c0d0e0f"  # статичный nonce — для мока достаточно
_OPAQUE = "5ccc069c403ebaf9f0171e9517f40e41"


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _parse_digest(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in header[len("Digest ") :].split(","):
        if "=" not in part:
            continue
        key, _, val = part.strip().partition("=")
        out[key.strip()] = val.strip().strip('"')
    return out


def _challenge() -> HTTPException:
    header = (
        f'Digest realm="NVRMock", qop="auth", '
        f'nonce="{_NONCE}", opaque="{_OPAQUE}"'
    )
    return HTTPException(status_code=401, detail="auth required",
                         headers={"WWW-Authenticate": header})


def require_auth(request: Request, username: str, password: str) -> None:
    """Бросает 401 при отсутствии/неверной аутентификации."""
    header = request.headers.get("authorization", "")

    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode()
            user, _, pwd = decoded.partition(":")
        except Exception:  # noqa: BLE001
            raise _challenge()
        if user == username and pwd == password:
            return
        raise _challenge()

    if header.startswith("Digest "):
        d = _parse_digest(header)
        if d.get("username") != username:
            raise _challenge()
        ha1 = _md5(f"{username}:{d.get('realm', 'NVRMock')}:{password}")
        ha2 = _md5(f"{request.method}:{d.get('uri', '')}")
        if d.get("qop"):
            expected = _md5(
                f"{ha1}:{d.get('nonce')}:{d.get('nc')}:{d.get('cnonce')}:"
                f"{d.get('qop')}:{ha2}"
            )
        else:
            expected = _md5(f"{ha1}:{d.get('nonce')}:{ha2}")
        if expected == d.get("response"):
            return
        raise _challenge()

    raise _challenge()
