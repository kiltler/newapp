"""Тесты модуля «Инвентарь» (принтеры/ЗиП/расходники). Против моков, стиль проекта."""
from __future__ import annotations

import datetime as dt

import httpx

import app.main as main_mod
from app.crypto import decrypt
from app.database import SessionLocal
from app.main import app
from app.models import (
    InvAsset,
    InvAssetStatus,
    InvConsumableModel,
    InvConsumableStock,
    InvService,
    Location,
)
from app.services import inventory, users


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _loc(s, name="АВ") -> int:
    l = Location(name=name)
    s.add(l)
    await s.flush()
    return l.id


async def _asset(s, loc_id, *, model="Kyocera P2040", status=InvAssetStatus.installed) -> int:
    a = InvAsset(type="printer", model=model, location_id=loc_id, status=status)
    s.add(a)
    await s.flush()
    return a.id


async def _cmodel(s, model="TK-1150", price_new=700.0) -> int:
    m = InvConsumableModel(kind="toner", model=model, price_new=price_new)
    s.add(m)
    await s.flush()
    return m.id


# 1. Движение: в пути → приёмка переставляет локацию
async def test_move_then_receive(db):
    async with SessionLocal() as s:
        av, office = await _loc(s, "АВ"), await _loc(s, "Офис")
        aid = await _asset(s, av)
        await s.commit()

        mv = await inventory.move_asset(s, aid, office, carrier="Петров")
        await s.commit()
        a = await s.get(InvAsset, aid)
        assert a.location_id == av           # НЕ переставили до приёмки
        assert mv.received_at is None        # в пути

        await inventory.receive_asset(s, mv.id, received_by="Сидоров")
        await s.commit()
        a = await s.get(InvAsset, aid)
        assert a.location_id == office        # теперь переставили
        mv2 = await s.get(type(mv), mv.id)
        assert mv2.received_at is not None


# 2. Установка расходника списывает склад; минус запрещён
async def test_install_consumable_decrements_and_no_negative(db):
    async with SessionLocal() as s:
        av = await _loc(s)
        aid = await _asset(s, av)
        cm = await _cmodel(s)
        s.add(InvConsumableStock(consumable_model_id=cm, location_id=av, qty=1))
        await s.commit()

        await inventory.install_consumable(s, aid, cm)
        await s.commit()
        st = (await s.execute(
            __import__("sqlalchemy").select(InvConsumableStock))).scalar_one()
        assert st.qty == 0

        # второй раз — остатка нет, понятная ошибка, склад не в минусе
        try:
            await inventory.install_consumable(s, aid, cm)
            assert False, "ожидали InventoryError"
        except inventory.InventoryError as e:
            assert "ЗиПе" in str(e)
        await s.rollback()
        st = (await s.execute(
            __import__("sqlalchemy").select(InvConsumableStock))).scalar_one()
        assert st.qty == 0   # не ушли в минус


# 3. ЗиП: «в ЗиПе 3 Kyocera»
async def test_stock_summary_spare_count(db):
    async with SessionLocal() as s:
        av = await _loc(s, "АВ")
        office = await _loc(s, "Офис")
        for _ in range(3):
            await _asset(s, av, status=InvAssetStatus.reserve)
        await _asset(s, office, status=InvAssetStatus.reserve)  # чужая точка
        await _asset(s, av, status=InvAssetStatus.installed)    # рабочий, не в ЗиПе
        await s.commit()

        summary = await inventory.stock_summary(s, av)
        spare = [g for g in summary["spare_assets"] if g["location_id"] == av]
        assert len(spare) == 1 and spare[0]["count"] == 3 and spare[0]["model"] == "Kyocera P2040"


# 4. Ремонт: в ремонт → обратно, история пишется
async def test_service_roundtrip(db):
    async with SessionLocal() as s:
        av = await _loc(s)
        aid = await _asset(s, av)
        await s.commit()

        svc = await inventory.send_to_service(s, aid, vendor="Лаба", issue="печь")
        await s.commit()
        a = await s.get(InvAsset, aid)
        assert a.status == InvAssetStatus.at_service

        await inventory.return_from_service(s, svc.id, cost=1500.0, result="заменили печь")
        await s.commit()
        a = await s.get(InvAsset, aid)
        assert a.status == InvAssetStatus.reserve
        svc2 = await s.get(InvService, svc.id)
        assert svc2.returned_at is not None and svc2.cost == 1500.0


# 5. Экономика заправок: дельта верная
async def test_refill_economy(db):
    async with SessionLocal() as s:
        av = await _loc(s)
        cm = await _cmodel(s, price_new=700.0)
        # две заправки по 1450 (дороже новых 700 → переплата)
        await inventory.return_from_refill(s, cm, av, qty=1, cost=1450.0)
        await inventory.return_from_refill(s, cm, av, qty=1, cost=1450.0)
        await s.commit()

        econ = await inventory.refill_economy(s)
        assert econ["refills"] == 2
        assert econ["total_refill_cost"] == 2900.0
        assert econ["total_new_cost"] == 1400.0
        assert econ["saved"] == -1500.0        # переплатили 1500


# 6. Права: без inventory → 403; buses не даёт доступ; владелец — всё
async def test_permissions(db):
    async with SessionLocal() as s:
        u_none = await users.create_user(s, "u_none", "p", permissions=[])
        u_bus = await users.create_user(s, "u_bus", "p", permissions=["buses"])
        u_inv = await users.create_user(s, "u_inv", "p", permissions=["inventory"])

    async with _client() as c:
        # владелец (по умолчанию из фикстуры) — открыт API
        assert (await c.get("/api/inventory/assets")).status_code == 200

        main_mod.TEST_SESSION_OVERRIDE = {"auth": True, "user_id": u_none.id}
        assert (await c.get("/api/inventory/assets")).status_code == 403
        assert (await c.get("/inventory")).status_code == 303   # редирект с HTML

        main_mod.TEST_SESSION_OVERRIDE = {"auth": True, "user_id": u_bus.id}
        # дыра, которую закрываем: вкладка «Автобусы» НЕ даёт доступ к инвентарю
        assert (await c.get("/api/inventory/assets")).status_code == 403
        assert (await c.get("/inventory")).status_code == 303

        main_mod.TEST_SESSION_OVERRIDE = {"auth": True, "user_id": u_inv.id}
        assert (await c.get("/api/inventory/assets")).status_code == 200
        assert (await c.get("/inventory")).status_code == 200


# 7. Время: created_at — UTC (значение), человеческие даты — без конверсии
async def test_time_conventions(db):
    naive = dt.datetime(2026, 7, 11, 7, 32, 0)   # наивное локальное
    async with SessionLocal() as s:
        av, office = await _loc(s, "АВ"), await _loc(s, "Офис")
        aid = await _asset(s, av)
        await s.commit()
        a = await s.get(InvAsset, aid)
        # created_at выставлен автоматически временем UTC (utcnow); SQLite tzinfo не
        # хранит, поэтому сверяем значение с UTC-now, а не tzinfo.
        assert abs(a.created_at.replace(tzinfo=None) - dt.datetime.utcnow()) < dt.timedelta(minutes=2)

        mv = await inventory.move_asset(s, aid, office, sent_at=naive)
        await s.commit()
        mv2 = await s.get(type(mv), mv.id)
        assert mv2.sent_at == naive               # round-trip ТОЧНЫЙ — не конвертировали


# 8. Секрет: snmp_community хранится шифрованным, в выдаче не светится
async def test_snmp_community_encrypted(db):
    async with _client() as c:
        r = await c.post("/api/inventory/assets", json={
            "type": "printer", "model": "Kyocera", "meta": {"ip": "10.0.0.5", "snmp_community": "secret123"}})
        aid = r.json()["id"]
    async with SessionLocal() as s:
        a = await s.get(InvAsset, aid)
        assert a.meta["snmp_community"].startswith("enc:")     # в БД зашифровано
        assert decrypt(a.meta["snmp_community"]) == "secret123"
    async with _client() as c:
        card = (await c.get(f"/api/inventory/assets/{aid}")).json()
        assert "snmp_community" not in card["asset"]["meta"]   # в выдаче не светится
        assert card["asset"]["meta"]["has_snmp_community"] is True


# 10. Массовый импорт: создаёт активы, повтор идемпотентен (дедуп по IP)
async def test_bulk_import_idempotent(db):
    rows = [
        {"model": "ECOSYS M2040dn", "ip": "192.168.11.6", "pages": 264612},
        {"model": "ECOSYS M2540dn", "ip": "192.168.11.120", "pages": 19934},
    ]
    async with _client() as c:
        r1 = (await c.post("/api/inventory/assets/bulk", json={"type": "printer", "rows": rows})).json()
        assert r1["created"] == 2 and r1["updated"] == 0
        # повтор с обновлённым счётчиком — не дублирует, обновляет
        rows[0]["pages"] = 265000
        r2 = (await c.post("/api/inventory/assets/bulk", json={"type": "printer", "rows": rows})).json()
        assert r2["created"] == 0 and r2["updated"] == 2
    from sqlalchemy import select as _sel
    async with SessionLocal() as s:
        assets = (await s.execute(_sel(InvAsset))).scalars().all()
        assert len(assets) == 2                              # дублей нет
        a6 = next(a for a in assets if a.meta.get("ip") == "192.168.11.6")
        assert a6.model == "ECOSYS M2040dn"
        assert a6.meta["last_counters"]["pages"] == 265000   # счётчик обновлён


# 9. Смоук: страницы рендерятся (владелец)
async def test_pages_render(db):
    async with SessionLocal() as s:
        av = await _loc(s)
        aid = await _asset(s, av)
        await s.commit()
    async with _client() as c:
        for url in ["/inventory", "/inventory/stock", "/inventory/locations",
                    "/inventory/consumables", "/inventory/report", f"/inventory/{aid}"]:
            r = await c.get(url)
            assert r.status_code == 200, f"{url} → {r.status_code}"
