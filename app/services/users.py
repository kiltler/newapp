"""Учётные записи панели: хеширование паролей и аутентификация (роли admin/bus)."""
from __future__ import annotations

import hashlib
import hmac
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

ROLES = ("admin", "bus")
_ITER = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2$256${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False


async def authenticate(session: AsyncSession, username: str, password: str) -> str | None:
    """Возвращает роль пользователя при верном пароле, иначе None."""
    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user and verify_password(password, user.password_hash):
        return user.role
    return None


async def create_user(session: AsyncSession, username: str, password: str, role: str) -> User:
    if role not in ROLES:
        role = "bus"
    user = User(username=username, password_hash=hash_password(password), role=role)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def list_users(session: AsyncSession) -> list[User]:
    return list((await session.execute(select(User).order_by(User.username))).scalars())


async def delete_user(session: AsyncSession, user_id: int) -> None:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user:
        await session.delete(user)
        await session.commit()
