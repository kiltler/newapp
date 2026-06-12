"""Тесты модуля «Автобусы»: замена дисков, валидации, страницы."""
import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Bus, Disk, DiskStatus, SwapLog


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _disk_status(disk_id: int) -> str:
    async with SessionLocal() as s:
        return (await s.execute(select(Disk).where(Disk.id == disk_id))).scalar_one().status


async def test_install_into_empty_and_swap(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "56", "route": "56"})).json()["id"]
        d1 = (await c.post("/api/disks", json={"label": "А-1", "assigned_bus_id": bus})).json()["id"]
        d2 = (await c.post("/api/disks", json={"label": "А-2", "assigned_bus_id": bus})).json()["id"]

        # установка в пустой регистратор
        r = await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d1, "note": "первая установка"})
        assert r.status_code == 200
        assert await _disk_status(d1) == DiskStatus.INSTALLED

        # замена d1 -> d2
        r = await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d2})
        assert r.status_code == 200
        assert await _disk_status(d1) == DiskStatus.REMOVED_REVIEW  # снятый → на просмотр
        assert await _disk_status(d2) == DiskStatus.INSTALLED

    async with SessionLocal() as s:
        b = (await s.execute(select(Bus).where(Bus.id == bus))).scalar_one()
        assert b.installed_disk_id == d2
        logs = (await s.execute(select(SwapLog).where(SwapLog.bus_id == bus))).scalars().all()
        assert len(logs) == 2


async def test_remove_without_replacement(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "7"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "Б-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
        r = await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": None, "note": "сняли"})
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.REMOVED_REVIEW
    async with SessionLocal() as s:
        b = (await s.execute(select(Bus).where(Bus.id == bus))).scalar_one()
        assert b.installed_disk_id is None  # автобус без диска


async def test_validations(db):
    async with _client() as c:
        bus1 = (await c.post("/api/buses", json={"bus_number": "1"})).json()["id"]
        bus2 = (await c.post("/api/buses", json={"bus_number": "2"})).json()["id"]
        d_other = (await c.post("/api/disks", json={"label": "Ч-1", "assigned_bus_id": bus2})).json()["id"]
        # чужой диск без force → 409
        r = await c.post(f"/api/buses/{bus1}/swap", json={"installed_disk_id": d_other})
        assert r.status_code == 409
        # с force → ок
        r = await c.post(f"/api/buses/{bus1}/swap", json={"installed_disk_id": d_other, "force": True})
        assert r.status_code == 200
        # установить уже установленный → 400
        r = await c.post(f"/api/buses/{bus2}/swap", json={"installed_disk_id": d_other})
        assert r.status_code == 400


async def test_reviewed_and_faulty(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "9"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "Р-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": None})  # снят -> review
        r = await c.post(f"/api/disks/{d}/reviewed")
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.READY
        r = await c.post(f"/api/disks/{d}/faulty", json={"note": "битый"})
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.FAULTY


async def test_restore_faulty(db):
    async with _client() as c:
        d = (await c.post("/api/disks", json={"label": "Ф-1"})).json()["id"]
        await c.post(f"/api/disks/{d}/faulty", json={"note": "битый"})
        assert await _disk_status(d) == DiskStatus.FAULTY
        r = await c.post(f"/api/disks/{d}/restore")
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.READY
        # ready нельзя «вернуть в строй»
        assert (await c.post(f"/api/disks/{d}/restore")).status_code == 400


async def test_collection_page_and_csv(db):
    async with _client() as c:
        await c.post("/api/buses", json={"bus_number": "55", "route": "5"})
        assert (await c.get("/buses/collection")).status_code == 200
        r = await c.get("/api/buses/collection.csv")
        assert r.status_code == 200 and "text/csv" in r.headers["content-type"]


async def test_swap_reserve_pair(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "21"})).json()["id"]
        d1 = (await c.post("/api/disks", json={"label": "П-1", "assigned_bus_id": bus})).json()["id"]
        d2 = (await c.post("/api/disks", json={"label": "П-2", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d1})  # d1 стоит, d2 резерв
        r = await c.post(f"/api/buses/{bus}/swap-reserve")                       # пара в один тап
        assert r.status_code == 200 and r.json()["installed"] == "П-2"
        assert await _disk_status(d2) == DiskStatus.INSTALLED
        assert await _disk_status(d1) == DiskStatus.REMOVED_REVIEW
        # резерва больше нет → 400
        assert (await c.post(f"/api/buses/{bus}/swap-reserve")).status_code == 400


async def test_disk_review_and_stats(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "30", "route": "7"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "Р-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": None})  # снят → review
        # запись наблюдения с проблемой + пометить готовым
        r = await c.post(f"/api/disks/{d}/review", json={"tags": ["нет записи"], "note": "канал 3", "finish": True})
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.READY
        # статистика видит проблемный автобус
        stats = (await c.get("/buses/stats")).text
        assert "нет записи" in stats and "30" in stats


async def test_review_and_dup_pages(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "40"})).json()["id"]
        await c.post("/api/disks", json={"label": "ДУБ", "assigned_bus_id": bus})
        await c.post("/api/disks", json={"label": "дуб", "assigned_bus_id": bus})  # дубль (регистр)
        assert (await c.get("/buses/review")).status_code == 200
        page = (await c.get("/disks")).text
        assert "Дубли меток" in page
        assert (await c.get("/disks?q=ДУБ")).status_code == 200


async def test_location_auto_set(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "12"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "Л-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
    async with SessionLocal() as s:
        disk = (await s.execute(select(Disk).where(Disk.id == d))).scalar_one()
        assert disk.location == "in_bus"  # установлен → в автобусе
    async with _client() as c:
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": None})  # снят
    async with SessionLocal() as s:
        disk = (await s.execute(select(Disk).where(Disk.id == d))).scalar_one()
        assert disk.location == "reviewer"  # снят → у смотрящего


async def test_audit_and_passport(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "13"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "А-7", "assigned_bus_id": bus})).json()["id"]
        r = await c.post("/api/disks/audit", json={"present_ids": [d]})
        assert r.status_code == 200 and r.json()["confirmed"] == 1
        assert (await c.get("/disks/audit")).status_code == 200
        assert (await c.get(f"/disks/{d}/passport")).status_code == 200


async def test_plan_and_today(db):
    async with _client() as c:
        import datetime as dt
        bus = (await c.post("/api/buses", json={"bus_number": "14", "route": "5"})).json()["id"]
        wd = dt.datetime.now().weekday()
        await c.put(f"/api/buses/{bus}", json={"collect_weekday": wd})
        assert (await c.get("/buses/plan")).status_code == 200
        page = (await c.get("/buses/collection?today=1")).text
        assert "14" in page  # автобус с сегодняшним днём сбора попал в список


async def test_pages_render(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "100", "route": "5"})).json()["id"]
        disk = (await c.post("/api/disks", json={"label": "Д-9", "assigned_bus_id": bus})).json()["id"]
        for path in ["/buses", "/buses?sort=route", "/buses?sort=number",
                     f"/buses/{bus}", "/disks", "/buses/swaplog",
                     f"/buses/swaplog?disk_id={disk}", "/api/buses/swaplog.csv"]:
            r = await c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"


async def test_settings_and_grouping(db):
    async with _client() as c:
        await c.post("/api/buses", json={"bus_number": "А1", "route": "5"})
        await c.post("/api/buses", json={"bus_number": "А2", "route": "5"})
        await c.post("/api/buses", json={"bus_number": "Б1", "route": "10"})
        # пороги сохраняются и применяются
        r = await c.post("/api/buses/settings", json={"swap_days": 21, "review_days": 3})
        assert r.status_code == 200
        page = (await c.get("/buses")).text
        assert 'value="21"' in page and 'value="3"' in page  # подставились в форму
        # группировка по маршрутам
        grouped = (await c.get("/buses?sort=group")).text
        assert "Маршрут 5" in grouped and "Маршрут 10" in grouped


async def test_route_sort_order(db):
    async with _client() as c:
        await c.post("/api/buses", json={"bus_number": "А1", "route": "10"})
        await c.post("/api/buses", json={"bus_number": "А2", "route": "5"})
        r = await c.get("/buses?sort=route")
        body = r.text
        # маршрут 5 должен идти раньше маршрута 10 (натуральная сортировка)
        assert body.index("марш. 5") < body.index("марш. 10")
