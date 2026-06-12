"""Тесты учётных записей и ролевого доступа."""
import httpx

from app.database import SessionLocal
from app.main import app
from app.services import users


def _client(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", **kw)


async def test_password_hash_and_auth(db):
    async with SessionLocal() as s:
        await users.create_user(s, "diskman", "secret1", "bus")
    async with SessionLocal() as s:
        assert await users.authenticate(s, "diskman", "secret1") == "bus"
        assert await users.authenticate(s, "diskman", "wrong") is None
        assert await users.authenticate(s, "nope", "secret1") is None


async def test_users_api_open_mode(db):
    # ADMIN_PASSWORD не задан → роль None считается админом, управление доступно
    async with _client() as c:
        r = await c.post("/api/users", json={"username": "d1", "password": "1234", "role": "bus"})
        assert r.status_code == 200
        assert any(u["username"] == "d1" for u in (await c.get("/api/users")).json())
        # короткий пароль отклоняется
        r = await c.post("/api/users", json={"username": "d2", "password": "1", "role": "bus"})
        assert r.status_code == 400


async def test_bus_role_restriction(db, monkeypatch):
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "admin_password", "adminpass")  # включаем авторизацию

    async with SessionLocal() as s:
        await users.create_user(s, "viewer", "pass123", "bus")

    async with _client(follow_redirects=False) as c:
        r = await c.post("/login", data={"username": "viewer", "password": "pass123"})
        assert r.status_code == 303 and r.headers["location"] == "/buses"  # bus → на автобусы
        assert (await c.get("/buses")).status_code == 200          # своя страница — ок
        assert (await c.get("/")).status_code == 303               # дашборд — редирект
        assert (await c.get("/api/devices")).status_code == 403    # чужой API — запрет
        assert (await c.get("/api/buses")).status_code == 200      # свой API — ок


async def test_admin_login_via_env(db, monkeypatch):
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "admin_password", "adminpass")
    async with _client(follow_redirects=False) as c:
        r = await c.post("/login", data={"username": "admin", "password": "adminpass"})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert (await c.get("/")).status_code == 200  # админу всё можно
