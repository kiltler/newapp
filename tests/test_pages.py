"""Интеграционные тесты новых страниц и API (журнал, CSV, план, TV, история)."""
import httpx

from app.database import SessionLocal
from app.main import app
from app.models import ApiType, ArchiveCoverage, ArchiveState, Channel, Device


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


async def test_archive_timeline_on_device_page(db):
    """Таймлайн архива подставляет данные покрытия (дыры) в страницу объекта."""
    import datetime as dt

    async with SessionLocal() as s:
        d = Device(name="ТЛ", host="127.0.0.1", http_port=80, username="admin",
                   api_type=ApiType.HIKVISION, capabilities={"archive": True})
        s.add(d)
        await s.flush()
        s.add(Channel(device_id=d.id, channel_id=1, name="Вход"))
        day = dt.date.today() - dt.timedelta(days=1)
        s.add(ArchiveCoverage(
            device_id=d.id, channel_id=1, day=day, status=ArchiveState.PARTIAL,
            recorded_minutes=1320, largest_gap_minutes=120, gaps=[["02:00", "04:00"]],
        ))
        await s.commit()
        did = d.id

    async with _client() as c:
        r = await c.get(f"/devices/{did}")
        assert r.status_code == 200
        body = r.text
        # секция и данные покрытия должны попасть в страницу
        assert "Таймлайн архива" in body
        assert "TL_CAL" in body
        assert '"02:00", "04:00"' in body  # интервал дыры прокинут в шаблон
        assert day.isoformat() in body     # день с данными есть в селекторе


async def test_pages_render(db):
    did = await _make_device()
    async with _client() as c:
        for path in ["/", "/tv", "/slideshow", "/history", "/plan", "/firmware", "/calc",
                     "/audit", "/labels", f"/m/{did}",
                     f"/devices/{did}", f"/devices/{did}/report", f"/devices/{did}/wall"]:
            r = await c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"


async def test_recent_alerts(db):
    from app.database import SessionLocal
    from app.models import Event, Severity

    async with SessionLocal() as s:
        s.add(Event(type="camera_down", severity=Severity.WARNING, message="канал упал"))
        s.add(Event(type="camera_added", severity=Severity.INFO, message="инфо"))
        await s.commit()
    async with _client() as c:
        r = await c.get("/api/alerts/recent?after_id=0")
        assert r.status_code == 200
        msgs = [e["message"] for e in r.json()]
        assert "канал упал" in msgs          # проблема — есть
        assert "инфо" not in msgs            # info-событие не показываем


async def test_metrics(db):
    did = await _make_device()
    async with _client() as c:
        r = await c.get("/metrics")
        assert r.status_code == 200
        body = r.text
        assert "nvrmon_devices_total" in body
        assert "nvrmon_device_reachable{" in body  # есть метрика с лейблом устройства


async def test_qr_png(db):
    did = await _make_device()
    async with _client() as c:
        r = await c.get(f"/api/devices/{did}/qr.png")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
