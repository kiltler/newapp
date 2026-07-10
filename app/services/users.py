"""Учётные записи панели: хеширование паролей, аутентификация, права-вкладки.

Доступ описывается ``is_owner`` (владелец — полный контроль) + ``permissions``
(список ключей вкладок из ``app/permissions.py``). Роли (admin/bus) — legacy.
"""
from __future__ import annotations

import hashlib
import hmac
import os

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, utcnow
from app.permissions import normalize_permissions

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


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    """Возвращает User при верном пароле и активной учётке, иначе None."""
    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None or not user.enabled or not verify_password(password, user.password_hash):
        return None
    user.last_login_at = utcnow()
    await session.commit()
    return user


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def list_users(session: AsyncSession) -> list[User]:
    return list((await session.execute(select(User).order_by(User.username))).scalars())


async def count_owners(session: AsyncSession) -> int:
    """Число включённых владельцев (для защиты «последнего владельца»)."""
    return (
        await session.execute(
            select(func.count()).select_from(User).where(
                User.is_owner.is_(True), User.enabled.is_(True)
            )
        )
    ).scalar_one()


async def create_user(
    session: AsyncSession, username: str, password: str, *,
    permissions: list[str] | None = None, is_owner: bool = False,
    display_name: str | None = None,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        is_owner=is_owner,
        permissions=[] if is_owner else normalize_permissions(permissions),
        display_name=display_name or None,
        enabled=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def update_user(
    session: AsyncSession, user_id: int, *,
    display_name: str | None = ..., permissions: list[str] | None = ...,
    enabled: bool | None = ..., is_owner: bool | None = ...,
) -> User | None:
    """Точечное обновление (переданные поля). ... = не менять."""
    user = await session.get(User, user_id)
    if user is None:
        return None
    if display_name is not ...:
        user.display_name = (display_name or None)
    if permissions is not ...:
        user.permissions = normalize_permissions(permissions)
    if enabled is not ...:
        user.enabled = bool(enabled)
    if is_owner is not ...:
        user.is_owner = bool(is_owner)
    await session.commit()
    await session.refresh(user)
    return user


async def set_password(session: AsyncSession, user_id: int, new_password: str) -> bool:
    user = await session.get(User, user_id)
    if user is None:
        return False
    user.password_hash = hash_password(new_password)
    await session.commit()
    return True


async def delete_user(session: AsyncSession, user_id: int) -> None:
    user = await session.get(User, user_id)
    if user:
        await session.delete(user)
        await session.commit()
