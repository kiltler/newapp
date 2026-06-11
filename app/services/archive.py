"""Суточная проверка видеоархива: есть ли записи за вчера, нет ли больших дыр."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal
from app.drivers import build_client
from app.drivers.base import ArchiveSegment, NVRError
from app.models import (
    ArchiveCoverage,
    ArchiveState,
    Device,
    Severity,
    utcnow,
)
from app.services import alerts
from app.services.poller import _semaphore

log = logging.getLogger(__name__)


def compute_coverage(
    segments: list[ArchiveSegment],
    day_start: dt.datetime,
    day_end: dt.datetime,
    gap_threshold_minutes: int,
) -> tuple[str, int, int, list[list[str]]]:
    """Сводит отрезки записи в покрытие дня.

    Возвращает (статус, записано_минут, макс_дыра_минут, список_дыр[["HH:MM","HH:MM"]]).
    """
    # Обрезаем по границам дня и отбрасываем пустые
    clamped: list[tuple[dt.datetime, dt.datetime]] = []
    for seg in segments:
        s = max(seg.start, day_start)
        e = min(seg.end, day_end)
        if e > s:
            clamped.append((s, e))

    if not clamped:
        return ArchiveState.NONE, 0, int((day_end - day_start).total_seconds() // 60), [
            [day_start.strftime("%H:%M"), day_end.strftime("%H:%M")]
        ]

    # Объединяем пересекающиеся/смежные отрезки
    clamped.sort()
    merged: list[list[dt.datetime]] = [list(clamped[0])]
    for s, e in clamped[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    recorded = sum((e - s).total_seconds() for s, e in merged) / 60.0

    # Ищем дыры: до первого отрезка, между, после последнего
    gaps: list[list[str]] = []
    largest = 0.0
    cursor = day_start
    for s, e in merged:
        if s > cursor:
            gap_min = (s - cursor).total_seconds() / 60.0
            if gap_min > gap_threshold_minutes:
                gaps.append([cursor.strftime("%H:%M"), s.strftime("%H:%M")])
            largest = max(largest, gap_min)
        cursor = max(cursor, e)
    if day_end > cursor:
        gap_min = (day_end - cursor).total_seconds() / 60.0
        if gap_min > gap_threshold_minutes:
            gaps.append([cursor.strftime("%H:%M"), day_end.strftime("%H:%M")])
        largest = max(largest, gap_min)

    if largest > gap_threshold_minutes:
        status = ArchiveState.PARTIAL
    else:
        status = ArchiveState.FULL
    return status, int(recorded), int(largest), gaps


async def check_archive_all(target_day: dt.date | None = None) -> None:
    """Проверяет архив за указанный день (по умолчанию — вчера) по всем устройствам."""
    if target_day is None:
        target_day = dt.date.today() - dt.timedelta(days=1)
    async with SessionLocal() as session:
        devices = (
            await session.execute(select(Device).where(Device.enabled.is_(True)))
        ).scalars().all()
        ids = [d.id for d in devices]
    log.info("Проверка архива за %s по %d устройствам", target_day, len(ids))
    for did in ids:
        await check_device_archive(did, target_day)


async def daily_archive_job() -> None:
    """Суточная задача: проверка вчерашнего архива + замер глубины хранения."""
    await check_archive_all()
    await measure_depth_all()


async def check_device_archive(device_id: int, target_day: dt.date) -> None:
    async with SessionLocal() as session:
        device = (
            await session.execute(
                select(Device).where(Device.id == device_id).options(
                    selectinload(Device.channels)
                )
            )
        ).scalar_one_or_none()
        if device is None or not device.enabled:
            return

        caps = device.capabilities or {}
        if not caps.get("archive", True):
            log.info("Устройство %s: поиск архива недоступен — пропуск", device_id)
            return

        client = build_client(device, semaphore=_semaphore)
        day_start = dt.datetime.combine(target_day, dt.time.min)
        day_end = dt.datetime.combine(target_day, dt.time.max).replace(microsecond=0)
        gap_threshold = settings.archive_gap_alert_minutes

        channels = [c for c in device.channels if c.enabled] or device.channels
        for ch in channels:
            try:
                segments = await client.search_archive(ch.channel_id, day_start, day_end)
            except NVRError as exc:
                log.warning("Архив %s ch%s: %s", device_id, ch.channel_id, exc)
                continue

            status, recorded, largest, gaps = compute_coverage(
                segments, day_start, day_end, gap_threshold
            )
            await _upsert_coverage(
                session, device_id, ch.channel_id, target_day,
                status, recorded, largest, gaps,
            )

            scope = f"device:{device_id}:channel:{ch.channel_id}:archive:{target_day}"
            if status == ArchiveState.NONE:
                await alerts.raise_alert(
                    session, scope_key=scope, alert_type="archive_missing",
                    severity=Severity.CRITICAL, device_id=device_id,
                    channel_id=ch.channel_id,
                    message=(
                        f"«{device.name}» канал {ch.channel_id} ({ch.name or '—'}): "
                        f"НЕТ архива за {target_day}"
                    ),
                )
            elif status == ArchiveState.PARTIAL:
                await alerts.raise_alert(
                    session, scope_key=scope, alert_type="archive_gap",
                    severity=Severity.WARNING, device_id=device_id,
                    channel_id=ch.channel_id,
                    message=(
                        f"«{device.name}» канал {ch.channel_id} ({ch.name or '—'}): "
                        f"дыра в архиве {largest} мин за {target_day}"
                    ),
                )
        await session.commit()


async def measure_depth_all(target_day: dt.date | None = None) -> None:
    """Измеряет реальную глубину архива по всем устройствам."""
    async with SessionLocal() as session:
        ids = (
            await session.execute(select(Device.id).where(Device.enabled.is_(True)))
        ).scalars().all()
    for did in ids:
        await measure_device_depth(did)


async def measure_device_depth(device_id: int) -> None:
    """Определяет, сколько дней архива реально хранится по каждому каналу.

    Делает один широкий поиск на канал и берёт самую раннюю запись. Сравнивает
    с обещанной клиенту глубиной (``archive_retention_days``) и шлёт алерт, если
    реальная глубина меньше.
    """
    async with SessionLocal() as session:
        device = (
            await session.execute(
                select(Device).where(Device.id == device_id).options(
                    selectinload(Device.channels)
                )
            )
        ).scalar_one_or_none()
        if device is None or not device.enabled:
            return
        caps = device.capabilities or {}
        if not caps.get("archive", True):
            return

        promised = device.archive_retention_days or 0
        # Окно поиска: обещанная глубина + запас, иначе 60 дней по умолчанию.
        window_days = (promised + 7) if promised else 60
        now = dt.datetime.now()
        window_start = now - dt.timedelta(days=window_days)

        client = build_client(device, semaphore=_semaphore)
        channels = [c for c in device.channels if c.enabled] or device.channels
        device_depth: int | None = None

        for ch in channels:
            try:
                segments = await client.search_archive(ch.channel_id, window_start, now)
            except NVRError:
                continue
            if not segments:
                ch.archive_depth_days = 0
                device_depth = 0 if device_depth is None else min(device_depth, 0)
                continue
            oldest = min(s.start for s in segments)
            depth = max((now - oldest).days, 0)
            ch.archive_depth_days = depth
            device_depth = depth if device_depth is None else min(device_depth, depth)

        # Алерт, если глубина меньше обещанной клиенту
        scope = f"device:{device_id}:archive_depth"
        if promised and device_depth is not None and device_depth < promised:
            await alerts.raise_alert(
                session, scope_key=scope, alert_type="archive_depth_low",
                severity=Severity.WARNING, device_id=device_id,
                message=(
                    f"«{device.name}»: глубина архива {device_depth} дн. "
                    f"меньше обещанных клиенту {promised} дн."
                ),
                context={"depth": device_depth, "promised": promised},
            )
        elif promised:
            await alerts.resolve_alert(
                session, scope_key=scope, device_id=device_id,
                message=f"«{device.name}»: глубина архива в норме ({device_depth} дн.)",
                notify=False,
            )
        await session.commit()


async def _upsert_coverage(
    session: AsyncSession,
    device_id: int,
    channel_id: int,
    day: dt.date,
    status: str,
    recorded: int,
    largest: int,
    gaps: list[list[str]],
) -> None:
    row = (
        await session.execute(
            select(ArchiveCoverage).where(
                ArchiveCoverage.device_id == device_id,
                ArchiveCoverage.channel_id == channel_id,
                ArchiveCoverage.day == day,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = ArchiveCoverage(device_id=device_id, channel_id=channel_id, day=day)
        session.add(row)
    row.status = status
    row.recorded_minutes = recorded
    row.largest_gap_minutes = largest
    row.gaps = gaps
    row.checked_at = utcnow()
