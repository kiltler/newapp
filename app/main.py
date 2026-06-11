"""Точка входа FastAPI: БД, планировщик, роуты, дашборд, (опц.) mock-сервер."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import dashboard, devices, monitoring
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

app.include_router(devices.router)
app.include_router(monitoring.router)
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
