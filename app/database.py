"""Асинхронный движок и сессии SQLAlchemy."""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


def _prepare_sqlite_path(url: str) -> None:
    """Создаёт каталог для файла SQLite, если его нет."""
    marker = ":///"
    if "sqlite" in url and marker in url:
        path = url.split(marker, 1)[1]
        if path and path != ":memory:":
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)


_prepare_sqlite_path(settings.database_url)

engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Зависимость FastAPI: выдаёт сессию БД."""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Создаёт таблицы (для MVP — без Alembic)."""
    from app import models  # noqa: F401  (регистрация моделей)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
