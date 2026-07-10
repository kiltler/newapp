"""Резервное копирование данных (только админ)."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.services import backup

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
from app.templatefilters import register as _register_filters  # noqa: E402
_register_filters(templates)

router = APIRouter(tags=["backup"])


def _require_admin(request: Request) -> None:
    if not (request.session.get("is_owner") or "backup" in request.session.get("caps", [])):
        raise HTTPException(403, "Нет доступа к бэкапу")


@router.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request):
    _require_admin(request)
    return templates.TemplateResponse("backup.html", {"request": request, "files": backup.list_backups()})


@router.get("/api/backup/download")
async def download_backup(request: Request, session: AsyncSession = Depends(get_session)):
    _require_admin(request)
    data = await backup.export_data(session)
    import datetime as dt
    fname = f"nvrmon-backup-{dt.datetime.now():%Y%m%d-%H%M}.json"
    return JSONResponse(
        data, headers={"Content-Disposition": f"attachment; filename={fname}"}
    )


@router.post("/api/backup/restore")
async def restore_backup(request: Request, file: UploadFile, session: AsyncSession = Depends(get_session)):
    _require_admin(request)
    try:
        data = json.loads(await file.read())
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Файл не похож на JSON-бэкап")
    if "tables" not in data:
        raise HTTPException(400, "В файле нет данных бэкапа")
    counts = await backup.import_data(session, data)
    return {"ok": True, "restored": counts}
