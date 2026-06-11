"""Точка входа FastAPI: БД, планировщик, роуты, дашборд, (опц.) mock-сервер."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import auth, dashboard, devices, monitoring, plan
from app.config import settings
from app.database import init_db
from app.scheduler import shutdown_scheduler, start_scheduler

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nvrmon")

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    log.info("NVR Monitor запущен (mock_mode=%s)", settings.mock_mode)
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(title="NVR Monitor", version="0.1.0", lifespan=lifespan)

# Открытые без авторизации пути (статика, страница входа, проверки, mock)
_PUBLIC_PREFIXES = ("/static", "/login", "/logout", "/healthz", "/mock", "/docs", "/openapi.json")


@app.middleware("http")
async def require_login(request: Request, call_next):
    # Если пароль не задан — вход отключён (панель открыта).
    if settings.admin_password and not request.url.path.startswith(_PUBLIC_PREFIXES):
        if not request.session.get("auth"):
            if request.url.path.startswith("/api"):
                return JSONResponse({"detail": "Требуется вход"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# SessionMiddleware добавляется ПОСЛЕ — значит он внешний и обрабатывает запрос
# первым, наполняя request.session до проверки require_login.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key or secrets.token_hex(32),
    session_cookie="nvrmon_session",
    max_age=60 * 60 * 24 * 7,
)

if not settings.admin_password:
    log.warning("ADMIN_PASSWORD не задан — панель открыта без авторизации!")

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(monitoring.router)
app.include_router(plan.router)
app.include_router(dashboard.router)

# Статика
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Встроенный mock-сервер NVR (только для разработки)
if settings.mock_mode:
    from mock.server import mock_app

    app.mount("/mock", mock_app)
    log.warning("MOCK_MODE включён: эмулятор NVR доступен на /mock")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
