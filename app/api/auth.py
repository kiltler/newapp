"""Вход в веб-панель по паролю (сессионная кука).

Если ``ADMIN_PASSWORD`` не задан — вход отключён, панель открыта (с предупреждением
в логах). Проверку доступа выполняет middleware в app.main.
"""
from __future__ import annotations

import hmac
import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(tags=["auth"])


def _check(username: str, password: str) -> bool:
    # constant-time сравнение, чтобы не палить пароль по времени ответа
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
    request: Request, username: str = Form("admin"), password: str = Form(...)
):
    if _check(username, password):
        request.session["auth"] = True
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Неверный логин или пароль"},
        status_code=401,
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
