"""Вход в веб-панель по паролю (сессионная кука).

Если ``ADMIN_PASSWORD`` не задан — вход отключён, панель открыта (с предупреждением
в логах). Проверку доступа выполняет middleware в app.main.
"""
from __future__ import annotations

import hmac
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.services import users

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(tags=["auth"])


def _is_env_admin(username: str, password: str) -> bool:
    # встроенный админ из .env (constant-time сравнение)
    if not settings.admin_password:
        return False
    return hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
        password, settings.admin_password
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if not settings.admin_password or request.session.get("auth"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request, username: str = Form("admin"), password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    # сначала учётки из БД, затем встроенный админ из .env
    role = await users.authenticate(session, username, password)
    if role is None and _is_env_admin(username, password):
        role = "admin"
    if role:
        request.session["auth"] = True
        request.session["user"] = username
        request.session["role"] = role
        return RedirectResponse("/buses" if role == "bus" else "/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Неверный логин или пароль"},
        status_code=401,
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
