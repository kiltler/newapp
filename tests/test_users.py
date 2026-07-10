"""Тесты учёток: владелец, гранулярные права-вкладки, вход, защита владельца."""
import httpx

import app.main as main_mod
from app.database import SessionLocal
from app.main import app
from app.permissions import ALL_KEYS
from app.services import users


def _client(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", **kw)


# ── Сервис ───────────────────────────────────────────────────────────────────
async def test_hash_auth_and_disabled(db):
    async with SessionLocal() as s:
        u = await users.create_user(s, "oper", "secret1", permissions=["checkin"])
    async with SessionLocal() as s:
        assert (await users.authenticate(s, "oper", "secret1")).username == "oper"
        assert await users.authenticate(s, "oper", "wrong") is None
        # отключённый пользователь не входит
        await users.update_user(s, u.id, enabled=False)
        assert await users.authenticate(s, "oper", "secret1") is None


async def test_permissions_normalized(db):
    async with SessionLocal() as s:
        u = await users.create_user(s, "x", "pass1", permissions=["checkin", "мусор", "buses"])
        assert u.permissions == [k for k in ALL_KEYS if k in ("checkin", "buses")]  # порядок реестра, мусор отброшен


# ── Гейтинг по правам (владелец-seam выключаем, логинимся реально) ───────────
async def _login(c, username, password):
    r = await c.post("/login", data={"username": username, "password": password})
    return r


async def test_gating_by_capabilities(db):
    async with SessionLocal() as s:
        await users.create_user(s, "busman", "pass123", permissions=["buses"])
    main_mod.TEST_SESSION_OVERRIDE = None  # проверяем настоящий вход/гейтинг
    async with _client(follow_redirects=False) as c:
        r = await _login(c, "busman", "pass123")
        assert r.status_code == 303 and r.headers["location"] == "/buses"  # стартовая по правам
        assert (await c.get("/buses")).status_code == 200
        assert (await c.get("/")).status_code == 303                 # нет monitoring → редирект
        assert (await c.get("/api/devices")).status_code == 403      # чужой API — запрет
        assert (await c.get("/api/buses")).status_code == 200        # свой API — ок
        assert (await c.get("/api/checkin/ingest/status")).status_code == 403
        assert (await c.get("/users")).status_code == 303            # не владелец — нет доступа к панели


async def test_checkin_without_onec(db):
    """С правом checkin, но без onec: плеер/клипы доступны, эндпоинты 1С — 403."""
    async with SessionLocal() as s:
        await users.create_user(s, "recept", "pass123", permissions=["checkin"])
    main_mod.TEST_SESSION_OVERRIDE = None
    async with _client(follow_redirects=False) as c:
        await _login(c, "recept", "pass123")
        assert (await c.get("/checkin")).status_code == 200
        assert (await c.get("/api/checkin/ingest/status")).status_code == 200
        assert (await c.get("/api/checkin/onec/status")).status_code == 403  # 1С закрыта


async def test_no_env_admin_and_no_open_panel(db):
    """Встроенного .env-админа нет; без входа — редирект/401."""
    main_mod.TEST_SESSION_OVERRIDE = None
    async with _client(follow_redirects=False) as c:
        # старый admin/adminpass больше не пускает
        r = await c.post("/login", data={"username": "admin", "password": "adminpass"})
        assert r.status_code == 401
        # без сессии — панель закрыта
        assert (await c.get("/")).status_code == 303
        assert (await c.get("/api/devices")).status_code == 401


async def test_owner_sees_everything(db):
    async with SessionLocal() as s:
        await users.create_user(s, "boss", "pass123", is_owner=True)
    main_mod.TEST_SESSION_OVERRIDE = None
    async with _client(follow_redirects=False) as c:
        r = await _login(c, "boss", "pass123")
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert (await c.get("/")).status_code == 200
        assert (await c.get("/users")).status_code == 200           # владелец → панель учёток
        assert (await c.get("/api/checkin/onec/status")).status_code == 200


# ── Панель управления (владелец-seam включён = мы владелец IOO) ───────────────
async def test_owner_crud_and_last_owner_protection(db):
    async with _client() as c:
        # владелец создаёт пользователя с набором вкладок (в т.ч. onec отдельно)
        r = await c.post("/api/users", json={
            "username": "u1", "password": "1234", "display_name": "Оператор",
            "permissions": ["checkin", "onec"], "is_owner": False,
        })
        assert r.status_code == 200
        uid = r.json()["id"]
        assert set(r.json()["permissions"]) == {"checkin", "onec"}

        # обновление прав
        r = await c.post(f"/api/users/{uid}", json={"permissions": ["buses"], "enabled": False})
        assert r.status_code == 200 and r.json()["permissions"] == ["buses"] and r.json()["enabled"] is False

        # короткий пароль отклоняется
        assert (await c.post("/api/users", json={"username": "u2", "password": "1"})).status_code == 400
        # дубликат логина
        assert (await c.post("/api/users", json={"username": "u1", "password": "1234"})).status_code == 409

        # защита последнего владельца: IOO единственный владелец
        me = next(u for u in (await c.get("/api/users")).json() if u["username"] == "IOO")
        assert (await c.post(f"/api/users/{me['id']}", json={"is_owner": False})).status_code == 409
        assert (await c.post(f"/api/users/{me['id']}", json={"enabled": False})).status_code == 409
        assert (await c.delete(f"/api/users/{me['id']}")).status_code == 409

        # обычного пользователя удалить можно
        assert (await c.delete(f"/api/users/{uid}")).status_code == 200


async def test_only_owner_manages_users(db):
    async with SessionLocal() as s:
        await users.create_user(s, "plain", "pass123", permissions=["monitoring"])
        u = (await users.list_users(s))
    main_mod.TEST_SESSION_OVERRIDE = None
    async with _client(follow_redirects=False) as c:
        await _login(c, "plain", "pass123")
        assert (await c.get("/api/users")).status_code == 403
        assert (await c.post("/api/users", json={"username": "z", "password": "1234"})).status_code == 403


async def test_permission_change_applies_without_relogin(db):
    """Снятие права закрывает доступ на следующем запросе (middleware читает БД)."""
    async with SessionLocal() as s:
        u = await users.create_user(s, "live", "pass123", permissions=["buses"])
        uid = u.id
    main_mod.TEST_SESSION_OVERRIDE = None
    async with _client(follow_redirects=False) as c:
        await _login(c, "live", "pass123")
        assert (await c.get("/api/buses")).status_code == 200
        # владелец забирает право (правим БД напрямую = как через панель)
        async with SessionLocal() as s:
            await users.update_user(s, uid, permissions=[])
        # следующий же запрос — уже без доступа, без перелогина
        assert (await c.get("/api/buses")).status_code == 403


async def test_owner_bootstrap_and_role_migration(db, monkeypatch):
    """ensure_owner_and_migrate_roles: legacy-роли → права, IOO → владелец, сброс пароля."""
    from app.config import settings
    from app.database import ensure_owner_and_migrate_roles
    from app.models import User

    async with SessionLocal() as s:
        s.add(User(username="olda", password_hash=users.hash_password("a"), role="admin"))
        s.add(User(username="oldb", password_hash=users.hash_password("b"), role="bus"))
        await s.commit()

    monkeypatch.setattr(settings, "owner_username", "IOO")
    monkeypatch.setattr(settings, "owner_password", "reset99")
    await ensure_owner_and_migrate_roles()

    async with SessionLocal() as s:
        a = await users.authenticate(s, "olda", "a")
        assert set(a.permissions) == set(ALL_KEYS)      # admin → все права
        b = (await s.execute(__import__("sqlalchemy").select(User).where(User.username == "oldb"))).scalar_one()
        assert b.permissions == ["buses"]               # bus → только автобусы
        # IOO существовал (из фикстуры) → остаётся владельцем, пароль сброшен
        io = await users.authenticate(s, "IOO", "reset99")
        assert io is not None and io.is_owner
