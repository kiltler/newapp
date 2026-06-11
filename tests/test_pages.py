"""Интеграционные тесты новых страниц и API (журнал, CSV, план, TV, история)."""
import httpx

from app.database import SessionLocal
from app.main import app
from app.models import ApiType, Device


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _make_device() -> int:
    async with SessionLocal() as s:
        d = Device(
            name="Объект-1", host="127.0.0.1", http_port=9, username="admin",
            api_type=ApiType.HIKVISION, capabilities={},
        )
        s.add(d)
        await s.commit()
        return d.id


async def test_notes_crud(db):
    did = await _make_device()
    async with _client() as c:
        r = await c.post(f"/api/devices/{did}/notes", json={"text": "заменил БП", "channel_id": 3})
        assert r.status_code == 200
        r = await c.get(f"/api/devices/{did}/notes")
        assert r.status_code == 200 and len(r.json()) == 1 and r.json()[0]["channel_id"] == 3
        note_id = r.json()[0]["id"]
        r = await c.delete(f"/api/notes/{note_id}")
        assert r.status_code == 200
        assert (await c.get(f"/api/devices/{did}/notes")).json() == []


async def test_csv_export(db):
    await _make_device()
    async with _client() as c:
        r = await c.get("/api/events/export.csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]


async def test_plan_markers(db):
    did = await _make_device()
    async with _client() as c:
        r = await c.post("/api/plan/markers", json={"device_id": did, "channel_id": 1, "label": "L", "x": 10, "y": 20})
        assert r.status_code == 200
        mid = r.json()["id"]
        assert len((await c.get("/api/plan/markers")).json()) == 1
        r = await c.put(f"/api/plan/markers/{mid}", json={"x": 33, "y": 44})
        assert r.status_code == 200
        r = await c.delete(f"/api/plan/markers/{mid}")
        assert r.status_code == 200
        assert (await c.get("/api/plan/markers")).json() == []


async def test_pages_render(db):
    did = await _make_device()
    async with _client() as c:
        for path in ["/", "/tv", "/history", "/plan",
                     f"/devices/{did}", f"/devices/{did}/report", f"/devices/{did}/wall"]:
            r = await c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
