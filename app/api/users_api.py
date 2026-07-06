"""Управление учётными записями (только для админа)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.database import get_session
from app.services import users

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
from app.templatefilters import register as _register_filters  # noqa: E402
_register_filters(templates)

router = APIRouter(tags=["users"])


def _require_admin(request: Request) -> None:
    # роль None = открытый режим (auth выключен) → считаем админом
    role = request.session.get("role")
    if role not in (None, "admin"):
        raise HTTPException(403, "Только для администратора")


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, session: AsyncSession = Depends(get_session)):
    _require_admin(request)
    rows = await users.list_users(session)
    return templates.TemplateResponse("users.html", {"request": request, "users": rows})


@router.get("/api/users")
async def api_list_users(request: Request, session: AsyncSession = Depends(get_session)):
    _require_admin(request)
    return [{"id": u.id, "username": u.username, "role": u.role} for u in await users.list_users(session)]


@router.post("/api/users")
async def api_create_user(
    request: Request, data: schemas.UserCreate, session: AsyncSession = Depends(get_session)
):
    _require_admin(request)
    if not data.username.strip() or len(data.password) < 4:
        raise HTTPException(400, "Логин обязателен, пароль не короче 4 символов")
    try:
        user = await users.create_user(session, data.username.strip(), data.password, data.role)
    except Exception:  # noqa: BLE001 — например, дубликат логина
        raise HTTPException(409, "Такой логин уже существует")
    return {"id": user.id, "username": user.username, "role": user.role}


@router.delete("/api/users/{user_id}")
async def api_delete_user(
    user_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    _require_admin(request)
    await users.delete_user(session, user_id)
    return {"ok": True}
