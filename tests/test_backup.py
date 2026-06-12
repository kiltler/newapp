"""Тесты резервного копирования и восстановления."""
import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import ApiType, Bus, Device, Disk
from app.services import backup


def _client(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", **kw)


async def test_export_import_roundtrip(db):
    async with SessionLocal() as s:
        s.add(Device(name="Объект-1", host="1.2.3.4", username="a", api_type=ApiType.HIKVISION))
        s.add(Bus(bus_number="55", route="5"))
        s.add(Disk(label="Д-1", type="SSD", status="ready"))
        await s.commit()
        data = await backup.export_data(s)

    assert len(data["tables"]["devices"]) == 1
    assert len(data["tables"]["buses"]) == 1

    # всё стёрли
    async with SessionLocal() as s:
        from sqlalchemy import delete
        await s.execute(delete(Device)); await s.execute(delete(Bus)); await s.execute(delete(Disk))
        await s.commit()
    async with SessionLocal() as s:
        assert (await s.execute(select(Device))).scalars().first() is None

    # восстановили
    async with SessionLocal() as s:
        counts = await backup.import_data(s, data)
        assert counts["devices"] == 1 and counts["buses"] == 1
    async with SessionLocal() as s:
        dev = (await s.execute(select(Device))).scalars().all()
        bus = (await s.execute(select(Bus))).scalars().all()
        assert len(dev) == 1 and dev[0].name == "Объект-1"
        assert len(bus) == 1 and bus[0].route == "5"


async def test_backup_download_and_page(db):
    async with _client() as c:
        r = await c.get("/api/backup/download")
        assert r.status_code == 200 and "tables" in r.json()
        assert (await c.get("/backup")).status_code == 200


async def test_restore_via_api(db):
    async with SessionLocal() as s:
        s.add(Bus(bus_number="77", route="9"))
        await s.commit()
        data = await backup.export_data(s)
    import io, json
    async with _client() as c:
        files = {"file": ("b.json", io.BytesIO(json.dumps(data).encode()), "application/json")}
        r = await c.post("/api/backup/restore", files=files)
        assert r.status_code == 200 and r.json()["restored"]["buses"] == 1
