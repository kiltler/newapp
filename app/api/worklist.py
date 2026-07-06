"""Рабочий список проблем («разбор полётов»): страница + API пометок."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.database import get_session
from app.models import Group, IssueAck, utcnow
from app.services import worklist

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
from app.templatefilters import register as _register_filters  # noqa: E402
_register_filters(templates)

router = APIRouter(tags=["worklist"])


@router.get("/worklist", response_class=HTMLResponse)
async def worklist_page(request: Request, session: AsyncSession = Depends(get_session)):
    issues = await worklist.collect_issues(session)
    summary = worklist.summarize(issues)
    groups = {g.id: g for g in (await session.execute(select(Group))).scalars()}
    return templates.TemplateResponse(
        "worklist.html",
        {"request": request, "issues": issues, "summary": summary, "groups": groups},
    )


@router.get("/api/worklist")
async def worklist_api(session: AsyncSession = Depends(get_session)):
    issues = await worklist.collect_issues(session)
    return {"issues": issues, "summary": worklist.summarize(issues)}


@router.post("/api/worklist/ack")
async def worklist_ack(
    data: schemas.IssueAckIn, request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Пометить проблему «взял в работу» (или обновить заметку)."""
    if not data.key:
        raise HTTPException(400, "нужен ключ проблемы")
    row = (
        await session.execute(select(IssueAck).where(IssueAck.issue_key == data.key))
    ).scalar_one_or_none()
    if row is None:
        row = IssueAck(issue_key=data.key)
        session.add(row)
    row.note = (data.note or "").strip() or None
    try:
        row.ack_by = request.session.get("user") or "—"
    except Exception:  # noqa: BLE001
        row.ack_by = "—"
    row.ack_at = utcnow()
    await session.commit()
    return {"ok": True}


@router.post("/api/worklist/unack")
async def worklist_unack(
    data: schemas.IssueAckIn, session: AsyncSession = Depends(get_session)
):
    """Снять пометку «взял в работу»."""
    row = (
        await session.execute(select(IssueAck).where(IssueAck.issue_key == data.key))
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.commit()
    return {"ok": True}
