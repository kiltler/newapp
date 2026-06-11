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
from app.models import ChannelState, Group

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
