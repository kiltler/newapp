"""Управление учётными записями — только для владельца панели (RBAC)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.database import get_session
from app.permissions import CAPABILITIES
from app.services import audit, users

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
from app.templatefilters import register as _register_filters  # noqa: E402
_register_filters(templates)

router = APIRouter(tags=["users"])


def _require_owner(request: Request) -> None:
    if not request.session.get("is_owner"):
        raise HTTPException(403, "Только для владельца панели")


def _user_dict(u) -> dict:
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "is_owner": u.is_owner, "enabled": u.enabled,
        "permissions": list(u.permissions or []),
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, session: AsyncSession = Depends(get_session)):
    _require_owner(request)
    rows = await users.list_users(session)
    return templates.TemplateResponse("users.html", {
        "request": request,
        "users": rows,  # ORM-объекты: в шаблоне нужен datetime last_login_at
        "capabilities": [{"key": c.key, "label": c.label} for c in CAPABILITIES],
        "cap_labels": {c.key: c.label for c in CAPABILITIES},
        "me_id": request.session.get("user_id"),
    })


@router.get("/api/users")
async def api_list_users(request: Request, session: AsyncSession = Depends(get_session)):
    _require_owner(request)
    return [_user_dict(u) for u in await users.list_users(session)]


@router.post("/api/users")
async def api_create_user(
    request: Request, data: schemas.UserCreate, session: AsyncSession = Depends(get_session)
):
    _require_owner(request)
    if not data.username.strip() or len(data.password) < 4:
        raise HTTPException(400, "Логин обязателен, пароль не короче 4 символов")
    try:
        user = await users.create_user(
            session, data.username.strip(), data.password,
            permissions=data.permissions, is_owner=data.is_owner,
            display_name=data.display_name,
        )
    except Exception:  # noqa: BLE001 — например, дубликат логина
        raise HTTPException(409, "Такой логин уже существует")
    await audit.log_action(session, request, "user_create", target=user.username)
    return _user_dict(user)


@router.post("/api/users/{user_id}")
async def api_update_user(
    user_id: int, request: Request, data: schemas.UserUpdate,
    session: AsyncSession = Depends(get_session),
):
    _require_owner(request)
    user = await users.get_user(session, user_id)
    if user is None:
        raise HTTPException(404, "Пользователь не найден")

    # Защита последнего владельца: нельзя разжаловать/отключить, если он один.
    losing_owner = (data.is_owner is False and user.is_owner)
    disabling = (data.enabled is False and user.enabled)
    if user.is_owner and (losing_owner or disabling) and await users.count_owners(session) <= 1:
        raise HTTPException(409, "Нельзя снять/отключить последнего владельца")

    await users.update_user(
        session, user_id,
        display_name=data.display_name if data.display_name is not None else ...,
        permissions=data.permissions if data.permissions is not None else ...,
        enabled=data.enabled if data.enabled is not None else ...,
        is_owner=data.is_owner if data.is_owner is not None else ...,
    )
    if data.password:
        if len(data.password) < 4:
            raise HTTPException(400, "Пароль не короче 4 символов")
        await users.set_password(session, user_id, data.password)
    await audit.log_action(session, request, "user_update", target=user.username)
    return _user_dict(await users.get_user(session, user_id))


@router.delete("/api/users/{user_id}")
async def api_delete_user(
    user_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    _require_owner(request)
    user = await users.get_user(session, user_id)
    if user is None:
        return {"ok": True}
    if user.is_owner and await users.count_owners(session) <= 1:
        raise HTTPException(409, "Нельзя удалить последнего владельца")
    await users.delete_user(session, user_id)
    await audit.log_action(session, request, "user_delete", target=user.username)
    return {"ok": True}
