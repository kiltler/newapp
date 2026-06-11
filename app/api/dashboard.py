"""Веб-дашборд (server-side рендеринг через Jinja2)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud
from app.api.monitoring import archive_calendar, summary
from app.database import get_session
from app.models import ChannelState, Group, PlanMarker

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(tags=["dashboard"])


def _device_health(device) -> str:
    """green / yellow / red для индикации статуса объекта."""
    if not device.enabled:
        return "gray"
    if not device.reachable:
        return "red"
    down = [c for c in device.channels if c.enabled and c.status != ChannelState.ONLINE]
    if down:
        return "yellow"
    return "green"


templates.env.globals["device_health"] = _device_health


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_session)):
    devices = await crud.list_devices(session)
    groups = {g.id: g for g in (await session.execute(select(Group))).scalars()}
    stats = await summary(session)
    events = await crud.list_events(session, limit=20)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "devices": devices,
            "groups": groups,
            "stats": stats,
            "events": events,
        },
    )


_STATUS_COLOR = {"online": "green", "offline": "red", "no_video": "yellow", "unknown": "gray"}


@router.get("/tv", response_class=HTMLResponse)
async def tv_mode(request: Request, session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse("tv.html", {"request": request})


@router.get("/history", response_class=HTMLResponse)
async def history(request: Request, session: AsyncSession = Depends(get_session)):
    days = 30
    since = dt.datetime.utcnow() - dt.timedelta(days=days)
    events = await crud.list_events(session, limit=5000)
    period = [
        e for e in events
        if e.created_at.replace(tzinfo=None) >= since and not e.type.endswith("_resolved")
    ]
    # инциденты по дням
    counts: dict[str, int] = {}
    for e in period:
        key = e.created_at.replace(tzinfo=None).date().isoformat()
        counts[key] = counts.get(key, 0) + 1
    day_seq = [(since.date() + dt.timedelta(days=i)) for i in range(days + 1)]
    by_day = [(d.strftime("%d.%m"), counts.get(d.isoformat(), 0)) for d in day_seq]
    max_day = max([c for _, c in by_day] + [1])

    # топ проблемных камер
    devices = await crud.list_devices(session)
    dname = {d.id: d.name for d in devices}
    cname = {(d.id, c.channel_id): c.name for d in devices for c in d.channels}
    cam: dict[tuple, int] = {}
    for e in period:
        if e.device_id and e.channel_id is not None and e.type in (
            "camera_down", "bad_image", "camera_removed"
        ):
            k = (e.device_id, e.channel_id)
            cam[k] = cam.get(k, 0) + 1
    top = sorted(cam.items(), key=lambda kv: -kv[1])[:15]
    top_rows = [
        {"device": dname.get(did, did), "channel": cid,
         "name": cname.get((did, cid), ""), "count": n}
        for (did, cid), n in top
    ]
    return templates.TemplateResponse(
        "history.html",
        {"request": request, "by_day": by_day, "max_day": max_day,
         "top_rows": top_rows, "events": period[:60], "days": days},
    )


@router.get("/plan", response_class=HTMLResponse)
async def plan_page(request: Request, session: AsyncSession = Depends(get_session)):
    import glob

    devices = await crud.list_devices(session)
    markers = (await session.execute(select(PlanMarker))).scalars().all()
    # карты статусов каналов и здоровья устройств
    ch_status = {
        (d.id, c.channel_id): c.status for d in devices for c in d.channels
    }
    health = {d.id: _device_health(d) for d in devices}
    marker_rows = []
    for m in markers:
        if m.channel_id is not None:
            color = _STATUS_COLOR.get(ch_status.get((m.device_id, m.channel_id), "unknown"), "gray")
        else:
            color = health.get(m.device_id, "gray")
        marker_rows.append({
            "id": m.id, "device_id": m.device_id, "channel_id": m.channel_id,
            "label": m.label or "", "x": m.x, "y": m.y, "color": color,
        })
    has_image = bool(glob.glob("data/plan.*"))
    devices_json = [
        {"id": d.id, "name": d.name,
         "channels": [{"channel_id": c.channel_id, "name": c.name or ""}
                      for c in sorted(d.channels, key=lambda c: c.channel_id)]}
        for d in devices
    ]
    return templates.TemplateResponse(
        "plan.html",
        {"request": request, "devices_json": devices_json,
         "markers": marker_rows, "has_image": has_image},
    )


@router.get("/devices/{device_id}/wall", response_class=HTMLResponse)
async def camera_wall(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        return HTMLResponse("Устройство не найдено", status_code=404)
    channels = sorted(
        [c for c in device.channels if c.enabled], key=lambda c: c.channel_id
    )
    return templates.TemplateResponse(
        "wall.html", {"request": request, "device": device, "channels": channels}
    )


@router.get("/devices/{device_id}/report", response_class=HTMLResponse)
async def device_report(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        return HTMLResponse("Устройство не найдено", status_code=404)
    since = dt.datetime.utcnow() - dt.timedelta(days=30)
    events = await crud.list_events(session, device_id=device_id, limit=1000)
    period_events = [e for e in events if e.created_at.replace(tzinfo=None) >= since]
    # Сводка инцидентов по типам за период
    incidents: dict[str, int] = {}
    for e in period_events:
        if not e.type.endswith("_resolved"):
            incidents[e.type] = incidents.get(e.type, 0) + 1
    return templates.TemplateResponse(
        "report.html",
        {
            "request": request,
            "device": device,
            "incidents": incidents,
            "period_from": since.date(),
            "period_to": dt.date.today(),
            "now": dt.datetime.now(),
            "channels": sorted(device.channels, key=lambda c: c.channel_id),
        },
    )


@router.get("/devices/{device_id}", response_class=HTMLResponse)
async def device_page(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        return HTMLResponse("Устройство не найдено", status_code=404)
    events = await crud.list_events(session, device_id=device_id, limit=50)
    calendar = await archive_calendar(device_id, days=14, session=session)
    return templates.TemplateResponse(
        "device.html",
        {
            "request": request,
            "device": device,
            "events": events,
            "calendar": calendar,
            "now": dt.datetime.utcnow(),
        },
    )
