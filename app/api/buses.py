"""Модуль «Автобусы»: ручной офлайн-учёт дисковой ротации.

Никакой сети/пингов — все статусы вносятся руками. Атомарная операция «Замена
диска» одним диалогом + отдельные действия для нестандартных ситуаций.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.config import settings
from app.database import get_session
from app.models import Bus, Disk, DiskStatus, SwapLog, utcnow

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(tags=["buses"])


# ── Статус автобуса (для индикации) ─────────────────────────────────────────────
def bus_status(bus: Bus, disks: list[Disk]) -> tuple[str, str]:
    """Возвращает (цвет, причина): red/orange/yellow/green."""
    now = utcnow()
    if bus.installed_disk_id is None:
        return "red", "регистратор без диска"
    if bus.has_problem:
        return "red", "отмечена проблема"
    overdue = (
        bus.installed_since is not None
        and (now - bus.installed_since).days >= settings.bus_swap_alert_days
    )
    if overdue:
        days = (now - bus.installed_since).days
        return "orange", f"диск стоит {days} дн. — пора менять"
    ready_reserve = any(
        d.assigned_bus_id == bus.id and d.status == DiskStatus.READY for d in disks
    )
    if not ready_reserve:
        return "yellow", "резерв не готов (второй диск на просмотре)"
    return "green", "всё ок"


_RANK = {"red": 0, "orange": 1, "yellow": 2, "green": 3}


async def _disks(session: AsyncSession) -> list[Disk]:
    return list((await session.execute(select(Disk))).scalars())


async def _bus(session: AsyncSession, bus_id: int) -> Bus:
    bus = (await session.execute(select(Bus).where(Bus.id == bus_id))).scalar_one_or_none()
    if bus is None:
        raise HTTPException(404, "Автобус не найден")
    return bus


async def _disk(session: AsyncSession, disk_id: int) -> Disk:
    disk = (await session.execute(select(Disk).where(Disk.id == disk_id))).scalar_one_or_none()
    if disk is None:
        raise HTTPException(404, "Диск не найден")
    return disk


# ── REST: автобусы ──────────────────────────────────────────────────────────────
@router.get("/api/buses")
async def list_buses(session: AsyncSession = Depends(get_session)):
    buses = list((await session.execute(select(Bus))).scalars())
    disks = await _disks(session)
    by_id = {d.id: d for d in disks}
    out = []
    for b in buses:
        color, reason = bus_status(b, disks)
        cur = by_id.get(b.installed_disk_id) if b.installed_disk_id else None
        out.append({
            "id": b.id, "bus_number": b.bus_number, "route": b.route,
            "dvr_model": b.dvr_model, "has_problem": b.has_problem,
            "installed_disk": (f"{cur.label} ({cur.type})" if cur else None),
            "installed_since": b.installed_since.isoformat() if b.installed_since else None,
            "color": color, "reason": reason,
        })
    out.sort(key=lambda x: (_RANK[x["color"]], x["bus_number"]))
    return out


@router.post("/api/buses")
async def create_bus(data: schemas.BusCreate, session: AsyncSession = Depends(get_session)):
    bus = Bus(bus_number=data.bus_number, route=data.route, dvr_model=data.dvr_model)
    session.add(bus)
    await session.commit()
    return {"id": bus.id}


@router.put("/api/buses/{bus_id}")
async def update_bus(bus_id: int, data: schemas.BusUpdate, session: AsyncSession = Depends(get_session)):
    bus = await _bus(session, bus_id)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(bus, k, v)
    await session.commit()
    return {"ok": True}


@router.delete("/api/buses/{bus_id}")
async def delete_bus(bus_id: int, session: AsyncSession = Depends(get_session)):
    bus = await _bus(session, bus_id)
    await session.delete(bus)
    await session.commit()
    return {"ok": True}


# ── REST: диски ──────────────────────────────────────────────────────────────────
@router.get("/api/disks")
async def list_disks(session: AsyncSession = Depends(get_session)):
    disks = await _disks(session)
    buses = {b.id: b.bus_number for b in (await session.execute(select(Bus))).scalars()}
    return [
        {"id": d.id, "label": d.label, "type": d.type, "capacity_gb": d.capacity_gb,
         "status": d.status, "assigned_bus_id": d.assigned_bus_id,
         "assigned_bus": buses.get(d.assigned_bus_id), "note": d.note,
         "status_since": d.status_since.isoformat() if d.status_since else None}
        for d in disks
    ]


@router.post("/api/disks")
async def create_disk(data: schemas.DiskCreate, session: AsyncSession = Depends(get_session)):
    disk = Disk(label=data.label, type=data.type, capacity_gb=data.capacity_gb,
                assigned_bus_id=data.assigned_bus_id, status=data.status, note=data.note)
    session.add(disk)
    await session.commit()
    return {"id": disk.id}


@router.put("/api/disks/{disk_id}")
async def update_disk(disk_id: int, data: schemas.DiskUpdate, session: AsyncSession = Depends(get_session)):
    disk = await _disk(session, disk_id)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(disk, k, v)
    await session.commit()
    return {"ok": True}


@router.post("/api/disks/{disk_id}/reviewed")
async def disk_reviewed(disk_id: int, session: AsyncSession = Depends(get_session)):
    disk = await _disk(session, disk_id)
    if disk.status != DiskStatus.REMOVED_REVIEW:
        raise HTTPException(400, "Просмотренным можно пометить только диск 'на просмотре'")
    disk.status = DiskStatus.READY
    disk.status_since = utcnow()
    await session.commit()
    return {"ok": True}


@router.post("/api/disks/{disk_id}/faulty")
async def disk_faulty(disk_id: int, data: schemas.FaultyRequest, session: AsyncSession = Depends(get_session)):
    disk = await _disk(session, disk_id)
    # если был установлен — автобус остаётся без диска
    if disk.assigned_bus_id:
        bus = (await session.execute(select(Bus).where(Bus.id == disk.assigned_bus_id))).scalar_one_or_none()
        if bus and bus.installed_disk_id == disk.id:
            bus.installed_disk_id = None
            bus.installed_since = None
    disk.status = DiskStatus.FAULTY
    disk.status_since = utcnow()
    if data.note:
        disk.note = data.note
    await session.commit()
    return {"ok": True}


# ── Ключевая операция: замена/снятие/установка ──────────────────────────────────
@router.post("/api/buses/{bus_id}/swap")
async def swap_disk(bus_id: int, data: schemas.SwapRequest, session: AsyncSession = Depends(get_session)):
    """Атомарно: снять текущий диск (→ на просмотр) и/или установить выбранный."""
    bus = await _bus(session, bus_id)
    now = utcnow()

    removed = None
    if bus.installed_disk_id:
        removed = await _disk(session, bus.installed_disk_id)

    new = None
    if data.installed_disk_id:
        new = await _disk(session, data.installed_disk_id)
        if new.status in (DiskStatus.INSTALLED, DiskStatus.FAULTY):
            raise HTTPException(400, f"Нельзя установить диск со статусом «{new.status}»")
        if new.assigned_bus_id not in (None, bus.id) and not data.force:
            raise HTTPException(409, "Диск закреплён за другим автобусом — подтвердите установку")

    # применяем
    if removed:
        removed.status = DiskStatus.REMOVED_REVIEW
        removed.status_since = now
    if new:
        new.status = DiskStatus.INSTALLED
        new.status_since = now
        new.assigned_bus_id = bus.id
        bus.installed_disk_id = new.id
        bus.installed_since = now
    else:
        bus.installed_disk_id = None
        bus.installed_since = None

    session.add(SwapLog(
        date=now, bus_id=bus.id,
        removed_disk_id=removed.id if removed else None,
        installed_disk_id=new.id if new else None,
        note=data.note,
    ))
    await session.commit()
    return {"ok": True}


# ── Журнал замен ─────────────────────────────────────────────────────────────────
async def _swaplog(session: AsyncSession, bus_id: int | None = None, disk_id: int | None = None):
    q = select(SwapLog).order_by(SwapLog.date.desc())
    if bus_id is not None:
        q = q.where(SwapLog.bus_id == bus_id)
    rows = list((await session.execute(q)).scalars())
    if disk_id is not None:
        rows = [r for r in rows if disk_id in (r.removed_disk_id, r.installed_disk_id)]
    return rows


@router.get("/api/buses/swaplog.csv")
async def swaplog_csv(bus_id: int | None = None, disk_id: int | None = None,
                      session: AsyncSession = Depends(get_session)):
    rows = await _swaplog(session, bus_id, disk_id)
    buses = {b.id: b.bus_number for b in (await session.execute(select(Bus))).scalars()}
    disks = {d.id: d.label for d in await _disks(session)}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Автобус", "Снят диск", "Установлен диск", "Заметка"])
    for r in rows:
        w.writerow([
            r.date.strftime("%Y-%m-%d %H:%M"), buses.get(r.bus_id, r.bus_id),
            disks.get(r.removed_disk_id, "") if r.removed_disk_id else "",
            disks.get(r.installed_disk_id, "") if r.installed_disk_id else "",
            r.note or "",
        ])
    return StreamingResponse(iter(["﻿" + buf.getvalue()]),
                             media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=swaplog.csv"})


# ── Страницы ─────────────────────────────────────────────────────────────────────
@router.get("/buses", response_class=HTMLResponse)
async def buses_page(request: Request, q: str = "", session: AsyncSession = Depends(get_session)):
    buses = list((await session.execute(select(Bus))).scalars())
    disks = await _disks(session)
    by_id = {d.id: d for d in disks}
    now = utcnow()
    rows, attention = [], []
    for b in buses:
        if q and q.lower() not in (f"{b.bus_number} {b.route or ''}").lower():
            continue
        color, reason = bus_status(b, disks)
        cur = by_id.get(b.installed_disk_id) if b.installed_disk_id else None
        rows.append({"bus": b, "color": color, "reason": reason, "disk": cur})
        if color in ("red", "orange"):
            attention.append({"bus": b, "reason": reason})
    rows.sort(key=lambda x: (_RANK[x["color"]], x["bus"].bus_number))
    # забытые на просмотре диски
    stale = [
        d for d in disks
        if d.status == DiskStatus.REMOVED_REVIEW and d.status_since
        and (now - d.status_since).days >= settings.bus_review_alert_days
    ]
    return templates.TemplateResponse("buses.html", {
        "request": request, "rows": rows, "attention": attention,
        "stale_disks": stale, "q": q, "swap_days": settings.bus_swap_alert_days,
    })


@router.get("/buses/swaplog", response_class=HTMLResponse)
async def swaplog_page(request: Request, session: AsyncSession = Depends(get_session)):
    rows = await _swaplog(session)
    buses = {b.id: b for b in (await session.execute(select(Bus))).scalars()}
    disks = {d.id: d for d in await _disks(session)}
    return templates.TemplateResponse("swaplog.html", {
        "request": request, "rows": rows, "buses": buses, "disks": disks,
    })


@router.get("/buses/{bus_id}", response_class=HTMLResponse)
async def bus_page(bus_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    bus = (await session.execute(select(Bus).where(Bus.id == bus_id))).scalar_one_or_none()
    if bus is None:
        return HTMLResponse("Автобус не найден", status_code=404)
    disks = await _disks(session)
    by_id = {d.id: d for d in disks}
    color, reason = bus_status(bus, disks)
    assigned = [d for d in disks if d.assigned_bus_id == bus.id]
    ready = [d for d in disks if d.status == DiskStatus.READY]
    history = await _swaplog(session, bus_id=bus_id)
    return templates.TemplateResponse("bus.html", {
        "request": request, "bus": bus, "color": color, "reason": reason,
        "installed": by_id.get(bus.installed_disk_id) if bus.installed_disk_id else None,
        "assigned": assigned, "ready_disks": ready, "history": history,
        "disks_by_id": by_id,
    })


@router.get("/disks", response_class=HTMLResponse)
async def disks_page(request: Request, status: str = "", session: AsyncSession = Depends(get_session)):
    disks = await _disks(session)
    if status:
        disks = [d for d in disks if d.status == status]
    disks.sort(key=lambda d: (d.status, d.label))
    buses = {b.id: b.bus_number for b in (await session.execute(select(Bus))).scalars()}
    return templates.TemplateResponse("disks.html", {
        "request": request, "disks": disks, "buses": buses, "status": status,
    })
