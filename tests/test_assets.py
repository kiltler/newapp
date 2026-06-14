"""Учёт активов: поступление партиями, списание, замена по неисправности, склад."""
import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import AssetBatch, Bus, Disk, DiskStatus


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_batch_receipt_creates_disks(db):
    async with _client() as c:
        r = await c.post("/api/asset-batches", json={
            "kind": "disk", "model": "Kingston A400 1ТБ", "vendor": "Kingston",
            "qty": 5, "disk_type": "SSD", "capacity_gb": 1000,
            "warranty_until": "2028-01-01", "supplier": "ООО Поставка",
        })
        assert r.status_code == 200 and r.json()["created"] == 5
    async with SessionLocal() as s:
        batch = (await s.execute(select(AssetBatch))).scalars().one()
        disks = (await s.execute(select(Disk).where(Disk.batch_id == batch.id))).scalars().all()
        assert len(disks) == 5
        assert all(d.status == DiskStatus.READY and d.location == "shelf" for d in disks)
        assert all(d.capacity_gb == 1000 and str(d.warranty_until) == "2028-01-01" for d in disks)


async def test_write_off(db):
    async with _client() as c:
        d = (await c.post("/api/disks", json={"label": "СП-1", "status": "faulty"})).json()["id"]
        r = await c.post(f"/api/disks/{d}/write-off", json={"reason": "не читается"})
        assert r.status_code == 200
    async with SessionLocal() as s:
        disk = (await s.execute(select(Disk).where(Disk.id == d))).scalar_one()
        assert disk.status == DiskStatus.WRITTEN_OFF
        assert "не читается" in (disk.note or "")


async def test_write_off_installed_blocked(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "WO"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "WO-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
        r = await c.post(f"/api/disks/{d}/write-off", json={"reason": "x"})
        assert r.status_code == 400  # установленный нельзя списать


async def test_replace_faulty(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "RF"})).json()["id"]
        old = (await c.post("/api/disks", json={"label": "OLD", "assigned_bus_id": bus})).json()["id"]
        new = (await c.post("/api/disks", json={"label": "NEW", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": old})  # OLD стоит, NEW резерв
        r = await c.post(f"/api/buses/{bus}/replace-faulty", json={"reason": "посыпался"})
        assert r.status_code == 200
    async with SessionLocal() as s:
        o = (await s.execute(select(Disk).where(Disk.id == old))).scalar_one()
        n = (await s.execute(select(Disk).where(Disk.id == new))).scalar_one()
        b = (await s.execute(select(Bus).where(Bus.id == bus))).scalar_one()
        assert o.status == DiskStatus.FAULTY and "посыпался" in (o.note or "")  # старый неисправен
        assert n.status == DiskStatus.INSTALLED and b.installed_disk_id == new   # новый установлен


async def test_assets_page_renders(db):
    async with _client() as c:
        await c.post("/api/asset-batches", json={"kind": "disk", "model": "WD 2ТБ", "qty": 1, "capacity_gb": 2000})
        page = (await c.get("/assets")).text
        assert "Склад" in page and "Поступления" in page and "WD 2ТБ" in page
