"""Вход в веб-панель по учётной записи из БД (сессионная кука).

Режима «открытой панели» и встроенного .env-админа больше нет — вход всегда
только по учёткам. Проверку доступа к вкладкам выполняет middleware в app.main.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.permissions import start_page
from app.services import users

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
from app.templatefilters import register as _register_filters  # noqa: E402
_register_filters(templates)

router = APIRouter(tags=["auth"])


def _apply_session(request: Request, user) -> None:
    request.session["auth"] = True
    request.session["user_id"] = user.id
    request.session["user"] = user.username           # атрибуция действий (audit)
    request.session["is_owner"] = bool(user.is_owner)
    request.session["caps"] = list(user.permissions or [])


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if request.session.get("auth"):
        dest = start_page(request.session.get("is_owner"), request.session.get("caps", [])) or "/no-access"
        return RedirectResponse(dest, status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request, username: str = Form(...), password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    user = await users.authenticate(session, username.strip(), password)
    if user is not None:
        _apply_session(request, user)
        dest = start_page(user.is_owner, list(user.permissions or [])) or "/no-access"
        return RedirectResponse(dest, status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Неверный логин или пароль"},
        status_code=401,
    )


@router.get("/no-access", response_class=HTMLResponse)
async def no_access(request: Request):
    """Заглушка для вошедшего пользователя без единого права."""
    if not request.session.get("auth"):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("no_access.html", {"request": request})


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
