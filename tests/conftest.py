"""Общие фикстуры тестов. Все тесты идут против mock-сервера NVR."""
from __future__ import annotations

import os
import tempfile

# ── Окружение ДО импорта приложения ──────────────────────────────────────────
_TMP_DB = os.path.join(tempfile.gettempdir(), "nvrmon_test.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP_DB}")
os.environ["TELEGRAM_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "dGVzdC1rZXktZml4ZWQtZm9yLXVuaXQtdGVzdHMtMDEyMzQ="
os.environ["CAMERA_OFFLINE_ALERT_MINUTES"] = "0"   # алерт сразу
os.environ["NVR_UNREACHABLE_THRESHOLD"] = "1"
os.environ["MAX_CONCURRENT_POLLS"] = "5"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.drivers.dahua import DahuaClient  # noqa: E402
from app.drivers.hikvision import HikvisionClient  # noqa: E402
from mock.server import make_mock_app  # noqa: E402
from mock.state import MockNVR  # noqa: E402


def make_http(app, user: str, password: str, scheme: str = "digest") -> httpx.AsyncClient:
    auth = (
        httpx.BasicAuth(user, password)
        if scheme == "basic"
        else httpx.DigestAuth(user, password)
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        auth=auth,
        base_url="http://testserver",
    )


def make_driver(api_type: str, nvr: MockNVR, *, scheme: str = "digest"):
    """Создаёт драйвер, подключённый к mock-приложению через ASGITransport."""
    profile = "hikvision" if api_type == "hikvision" else "dahua"
    app = make_mock_app(nvr, profile)
    http = make_http(app, nvr.username, nvr.password, scheme)
    cls = HikvisionClient if api_type == "hikvision" else DahuaClient
    return cls(
        host="testserver", port=80,
        username=nvr.username, password=nvr.password,
        auth_scheme=scheme, client=http,
    )


@pytest.fixture
def nvr() -> MockNVR:
    return MockNVR.default(channels=4, analog=0)


@pytest.fixture
def nvr_hybrid() -> MockNVR:
    return MockNVR.default(channels=2, analog=2)


@pytest.fixture
async def db():
    """Чистая БД на каждый тест (пересоздаём таблицы)."""
    from app.database import Base, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
