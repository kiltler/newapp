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


# Лёгкие миграции: новые колонки, которые create_all НЕ добавит к уже
# существующим таблицам. (table, column, DDL-тип). Без Alembic для простоты.
_NEW_COLUMNS = [
    ("devices", "cpu_load", "FLOAT"),
    ("devices", "memory_usage", "FLOAT"),
    ("devices", "temperature", "FLOAT"),
    ("devices", "latitude", "FLOAT"),
    ("devices", "longitude", "FLOAT"),
    ("channels", "archive_depth_days", "INTEGER"),
    ("channels", "quality", "VARCHAR(16)"),
    ("channels", "quality_checked_at", "TIMESTAMP"),
    ("channels", "frame_sig", "TEXT"),
    ("channels", "frozen_count", "INTEGER DEFAULT 0"),
    ("swap_log", "user", "VARCHAR(64)"),
    ("disks", "location", "VARCHAR(16) DEFAULT 'shelf'"),
    ("disks", "last_audit_at", "TIMESTAMP"),
    ("buses", "collect_weekday", "INTEGER"),
    ("buses", "location", "VARCHAR(128)"),
    ("buses", "problem_note", "TEXT"),
    ("buses", "has_problem", "BOOLEAN DEFAULT FALSE"),
    ("buses", "swap_alert_enabled", "BOOLEAN DEFAULT FALSE"),
    ("devices", "auth_failures", "INTEGER DEFAULT 0"),
    ("disks", "batch_id", "INTEGER"),
    ("disks", "warranty_until", "DATE"),
    ("assets", "assigned_bus_id", "INTEGER"),
    ("checkin_clips", "download_bytes", "INTEGER"),
    ("checkin_clips", "download_ms", "INTEGER"),
    ("checkin_ingest_runs", "dl_bytes", "INTEGER DEFAULT 0"),
    ("checkin_ingest_runs", "dl_ms", "INTEGER DEFAULT 0"),
]


def _lightweight_migrate(sync_conn) -> None:
    import logging

    from sqlalchemy import inspect, text

    log = logging.getLogger("nvrmon.migrate")
    insp = inspect(sync_conn)
    tables = set(insp.get_table_names())
    for table, column, ddl in _NEW_COLUMNS:
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        if column not in existing:
            # Имена в кавычках — column может быть зарезервированным словом (напр. "user" в Postgres).
            # SAVEPOINT, чтобы сбой одной миграции не ронял весь старт и не «портил» транзакцию.
            try:
                with sync_conn.begin_nested():
                    sync_conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))
            except Exception as exc:  # noqa: BLE001
                log.warning("Миграция %s.%s пропущена: %s", table, column, exc)


async def init_db() -> None:
    """Создаёт таблицы и добавляет недостающие колонки (для MVP — без Alembic)."""
    from app import models  # noqa: F401  (регистрация моделей)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_lightweight_migrate)
