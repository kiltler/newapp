"""Хранилище настроек ключ/значение (правится из UI), с фолбэком на .env."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting


async def get_int(session: AsyncSession, key: str, default: int) -> int:
    row = await session.get(AppSetting, key)
    if row and str(row.value).lstrip("-").isdigit():
        return int(row.value)
    return default


async def set_value(session: AsyncSession, key: str, value) -> None:
    row = await session.get(AppSetting, key)
    if row:
        row.value = str(value)
    else:
        session.add(AppSetting(key=key, value=str(value)))
    await session.commit()
