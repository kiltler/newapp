"""REST API: сводка, события, календарь архива."""
from __future__ import annotations

import datetime as dt

import csv
import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, schemas
from app.database import get_session
from app.models import ArchiveCoverage, Channel, ChannelState, Device

router = APIRouter(prefix="/api", tags=["monitoring"])


@router.get("/summary")
async def summary(session: AsyncSession = Depends(get_session)):
    devices = (await session.execute(select(Device))).scalars().all()
    total = len(devices)
    online = sum(1 for d in devices if d.reachable and d.enabled)
    unreachable = sum(1 for d in devices if not d.reachable and d.enabled)

    channels_down = (
        await session.execute(
            select(func.count())
            .select_from(Channel)
            .where(Channel.status != ChannelState.ONLINE, Channel.enabled.is_(True))
        )
    ).scalar() or 0

    # устройства с проблемами: недоступны, есть offline-каналы или дрейф времени
    problem_device_ids: set[int] = {d.id for d in devices if not d.reachable and d.enabled}
    rows = (
        await session.execute(
            select(Channel.device_id).where(
                Channel.status != ChannelState.ONLINE, Channel.enabled.is_(True)
            )
        )
    ).scalars()
    problem_device_ids.update(rows)

    return {
        "devices_total": total,
        "devices_online": online,
        "devices_unreachable": unreachable,
        "devices_with_problems": len(problem_device_ids),
        "channels_down": channels_down,
    }


@router.get("/events", response_model=list[schemas.EventOut])
async def events(
    device_id: int | None = None,
    severity: str | None = None,
    limit: int = Query(200, le=1000),
    session: AsyncSession = Depends(get_session),
):
    return await crud.list_events(
        session, device_id=device_id, severity=severity, limit=limit
    )


@router.get("/events/export.csv")
async def export_events_csv(
    device_id: int | None = None,
    limit: int = Query(5000, le=20000),
    session: AsyncSession = Depends(get_session),
):
    """Экспорт журнала событий в CSV (для актов и истории)."""
    events = await crud.list_events(session, device_id=device_id, limit=limit)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Дата/время", "Устройство", "Канал", "Тип", "Важность", "Сообщение"])
    for e in events:
        writer.writerow([
            e.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            e.device_id or "",
            e.channel_id if e.channel_id is not None else "",
            e.type, e.severity, e.message,
        ])
    buf.seek(0)
    # BOM, чтобы Excel корректно открыл кириллицу
    data = "﻿" + buf.getvalue()
    return StreamingResponse(
        iter([data]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=events.csv"},
    )


@router.get("/alerts/recent")
async def recent_alerts(after_id: int = 0, session: AsyncSession = Depends(get_session)):
    """Свежие алерты (проблемы + 'восстановлено') с id больше after_id.

    Для живых браузерных оповещений: клиент опрашивает раз в несколько секунд.
    """
    from app.models import Event

    rows = (
        await session.execute(
            select(Event)
            .where(
                Event.id > after_id,
                or_(Event.severity != "info", Event.type.like("%_resolved")),
            )
            .order_by(Event.id.desc())
            .limit(40)
        )
    ).scalars().all()
    rows = list(reversed(rows))  # по возрастанию id
    return [
        {"id": e.id, "type": e.type, "severity": e.severity, "message": e.message,
         "device_id": e.device_id, "created_at": e.created_at.isoformat()}
        for e in rows
    ]


@router.get("/devices/{device_id}/archive")
async def archive_calendar(
    device_id: int,
    days: int = Query(14, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """Матрица 'канал × день' с покрытием архива за последние N дней."""
    today = dt.date.today()
    start_day = today - dt.timedelta(days=days)
    rows = (
        await session.execute(
            select(ArchiveCoverage).where(
                ArchiveCoverage.device_id == device_id,
                ArchiveCoverage.day >= start_day,
            )
        )
    ).scalars().all()

    day_list = [(start_day + dt.timedelta(days=i)).isoformat() for i in range(days + 1)]
    matrix: dict[int, dict[str, dict]] = {}
    for r in rows:
        matrix.setdefault(r.channel_id, {})[r.day.isoformat()] = {
            "status": r.status,
            "recorded_minutes": r.recorded_minutes,
            "largest_gap_minutes": r.largest_gap_minutes,
            "gaps": r.gaps,
        }
    return {"days": day_list, "channels": matrix}
