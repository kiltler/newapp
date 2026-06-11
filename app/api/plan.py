"""План объекта: загрузка картинки плана и метки камер на ней."""
from __future__ import annotations

import glob
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.database import get_session
from app.models import PlanMarker

router = APIRouter(tags=["plan"])

PLAN_DIR = Path("data")
PLAN_DIR.mkdir(parents=True, exist_ok=True)


def _plan_file() -> str | None:
    files = sorted(glob.glob(str(PLAN_DIR / "plan.*")))
    return files[0] if files else None


@router.get("/plan/image")
async def plan_image():
    path = _plan_file()
    if not path:
        raise HTTPException(404, "План не загружен")
    return FileResponse(path)


@router.post("/api/plan/image")
async def upload_plan(file: UploadFile):
    ext = os.path.splitext(file.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        raise HTTPException(400, "Поддерживаются PNG/JPG/WEBP/GIF")
    for old in glob.glob(str(PLAN_DIR / "plan.*")):
        os.remove(old)
    dest = PLAN_DIR / f"plan{ext}"
    dest.write_bytes(await file.read())
    return {"ok": True}


@router.get("/api/plan/markers")
async def list_markers(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(PlanMarker))).scalars().all()
    return [
        {"id": m.id, "device_id": m.device_id, "channel_id": m.channel_id,
         "label": m.label, "x": m.x, "y": m.y}
        for m in rows
    ]


@router.post("/api/plan/markers")
async def add_marker(data: schemas.MarkerCreate, session: AsyncSession = Depends(get_session)):
    m = PlanMarker(
        device_id=data.device_id, channel_id=data.channel_id,
        label=data.label, x=data.x, y=data.y,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)
    return {"id": m.id}


@router.put("/api/plan/markers/{marker_id}")
async def move_marker(
    marker_id: int, data: schemas.MarkerMove, session: AsyncSession = Depends(get_session)
):
    m = (await session.execute(select(PlanMarker).where(PlanMarker.id == marker_id))).scalar_one_or_none()
    if m is None:
        raise HTTPException(404, "Метка не найдена")
    m.x, m.y = data.x, data.y
    await session.commit()
    return {"ok": True}


@router.delete("/api/plan/markers/{marker_id}")
async def delete_marker(marker_id: int, session: AsyncSession = Depends(get_session)):
    m = (await session.execute(select(PlanMarker).where(PlanMarker.id == marker_id))).scalar_one_or_none()
    if m is None:
        raise HTTPException(404, "Метка не найдена")
    await session.delete(m)
    await session.commit()
    return {"ok": True}
