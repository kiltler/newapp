"""Тесты модуля «Заселения» — Фаза 1 (драйвер, ingestion, API)."""
from __future__ import annotations

import datetime as dt

import httpx

from app.database import SessionLocal
from app.drivers.base import ArchiveSegment, FeatureUnavailable
from app.drivers.hikvision import HikvisionClient
from app.main import app
from app.models import CheckinChannel, CheckinClip, CheckinHotel, CheckinRecorder, ClipStatus
from app.services import checkin_ingest


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _hik() -> HikvisionClient:
    return HikvisionClient(host="10.0.0.5", username="admin", password="p@ss/1")


def _resp(status: int, xml: str) -> httpx.Response:
    return httpx.Response(status, content=xml.encode(), request=httpx.Request("POST", "http://x"))


# ── Драйвер ──────────────────────────────────────────────────────────────────
def test_rtsp_url_encodes_credentials():
    c = _hik()
    s = dt.datetime(2026, 6, 15, 7, 0, 0)
    e = dt.datetime(2026, 6, 15, 8, 0, 0)
    url = c.rtsp_playback_url(102, s, e, rtsp_port=554)
    assert url == (
        "rtsp://admin:p%40ss%2F1@10.0.0.5:554/Streaming/tracks/102"
        "?starttime=20260615T070000Z&endtime=20260615T080000Z"
    )


async def test_list_tracks_parses_substream(monkeypatch):
    c = _hik()
    xml = (
        "<TrackList>"
        "<Track><id>101</id><Channel>1</Channel><TrackType>Video</TrackType></Track>"
        "<Track><id>102</id><Channel>1</Channel><TrackType>Video</TrackType></Track>"
        "<Track><id>202</id><Channel>2</Channel><TrackType>Video</TrackType></Track>"
        "</TrackList>"
    )

    async def fake(method, path, **kw):
        return _resp(200, xml)

    monkeypatch.setattr(c, "_request", fake)
    tracks = await c.list_tracks()
    assert len(tracks) == 3
    subs = [t for t in tracks if t["is_sub"]]
    assert {t["trackid"] for t in subs} == {102, 202}
    assert tracks[0]["channel"] == 1


async def test_search_activity_all_is_whole_window():
    c = _hik()
    s = dt.datetime(2026, 6, 15, 7, 0, 0)
    e = dt.datetime(2026, 6, 15, 8, 0, 0)
    segs = await c.search_activity(1, s, e, mode="all")
    assert segs == [ArchiveSegment(s, e)]


async def test_search_activity_motion_parses(monkeypatch):
    c = _hik()
    xml = (
        "<CMSearchResult><matchList>"
        "<searchMatchItem><timeSpan>"
        "<startTime>2026-06-15T07:10:00Z</startTime>"
        "<endTime>2026-06-15T07:12:00Z</endTime>"
        "</timeSpan></searchMatchItem>"
        "</matchList><responseStatusStrg>OK</responseStatusStrg></CMSearchResult>"
    )

    async def fake(method, path, **kw):
        return _resp(200, xml)

    monkeypatch.setattr(c, "_request", fake)
    segs = await c.search_activity(1, dt.datetime(2026, 6, 15, 7), dt.datetime(2026, 6, 15, 8), mode="motion")
    assert len(segs) == 1
    assert segs[0].start == dt.datetime(2026, 6, 15, 7, 10)


async def test_search_activity_unsupported_raises(monkeypatch):
    c = _hik()

    async def fake(method, path, **kw):
        return _resp(400, "<err/>")

    monkeypatch.setattr(c, "_request", fake)
    try:
        await c.search_activity(1, dt.datetime(2026, 6, 15, 7), dt.datetime(2026, 6, 15, 8), mode="human")
        assert False, "ожидали FeatureUnavailable"
    except FeatureUnavailable:
        pass


# ── Ночное окно и склейка сегментов ──────────────────────────────────────────
def test_night_window_full_day():
    rec = CheckinRecorder(hotel_id=1, host="h", night_start="07:00", night_end="24:00")
    start, end = checkin_ingest.night_window(rec, dt.date(2026, 6, 15))
    assert start == dt.datetime(2026, 6, 15, 7, 0)
    assert end == dt.datetime(2026, 6, 16, 0, 0)


def test_merge_segments_pads_and_merges():
    win_s = dt.datetime(2026, 6, 15, 7, 0)
    win_e = dt.datetime(2026, 6, 15, 23, 0)
    segs = [
        ArchiveSegment(dt.datetime(2026, 6, 15, 8, 0, 0), dt.datetime(2026, 6, 15, 8, 0, 30)),
        ArchiveSegment(dt.datetime(2026, 6, 15, 8, 0, 40), dt.datetime(2026, 6, 15, 8, 1, 0)),  # близко → склеить
        ArchiveSegment(dt.datetime(2026, 6, 15, 10, 0, 0), dt.datetime(2026, 6, 15, 10, 0, 20)),
    ]
    merged = checkin_ingest.merge_segments(segs, win_s, win_e)
    assert len(merged) == 2  # первые два склеились
    assert merged[0].start < segs[0].start  # применён пре-ролл


# ── Идемпотентный ingestion с замоканным ffmpeg ──────────────────────────────
async def test_ingestion_downloads_and_is_idempotent(db, monkeypatch, tmp_path):
    monkeypatch.setattr(checkin_ingest.settings, "clips_dir", str(tmp_path))

    async with SessionLocal() as s:
        hotel = CheckinHotel(name="A")
        s.add(hotel)
        await s.flush()
        rec = CheckinRecorder(hotel_id=hotel.id, host="10.0.0.5", model_type="dsh332_2q",
                              analytics_capable=True, night_start="07:00", night_end="24:00")
        s.add(rec)
        await s.flush()
        s.add(CheckinChannel(recorder_id=rec.id, channel_id=1, role="entrance", substream_trackid=102))
        await s.commit()
        rid = rec.id

    seg = ArchiveSegment(dt.datetime(2026, 6, 15, 8, 0, 0), dt.datetime(2026, 6, 15, 8, 1, 0))

    class FakeClient:
        async def search_activity(self, ch, a, b, *, mode="all"):
            return [seg]

        def rtsp_playback_url(self, trackid, a, b, *, rtsp_port=554):
            return f"rtsp://fake/{trackid}"

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: FakeClient())

    calls = {"n": 0}

    async def fake_ffmpeg(url, out_path, duration_s):
        calls["n"] += 1
        with open(out_path, "wb") as fh:
            fh.write(b"FAKECLIP")
        return True, ""

    monkeypatch.setattr(checkin_ingest, "_run_ffmpeg", fake_ffmpeg)

    st1 = await checkin_ingest.ingest_recorder(dt.date(2026, 6, 15), rid)
    assert st1["downloaded"] == 1
    assert calls["n"] == 1

    async with SessionLocal() as s:
        clips = (await s.execute(__import__("sqlalchemy").select(CheckinClip))).scalars().all()
        assert len(clips) == 1
        assert clips[0].status == ClipStatus.OK
        assert clips[0].size_bytes == len(b"FAKECLIP")

    # Повторный прогон — не качаем заново
    st2 = await checkin_ingest.ingest_recorder(dt.date(2026, 6, 15), rid)
    assert st2["skipped"] == 1
    assert calls["n"] == 1  # ffmpeg больше не вызывался


# ── API + рендер ─────────────────────────────────────────────────────────────
async def test_pages_render(db):
    async with _client() as c:
        for p in ["/checkin", "/checkin/settings", "/checkin/notifications"]:
            r = await c.get(p)
            assert r.status_code == 200, f"{p} -> {r.status_code}"


async def test_settings_crud_and_analytics_default(db):
    async with _client() as c:
        hid = (await c.post("/api/checkin/hotels", json={"name": "Гост А"})).json()["id"]
        r = await c.post("/api/checkin/recorders", json={
            "hotel_id": hid, "host": "10.0.0.7", "model_type": "ds7616ni_e2",
            "username": "admin", "password": "secret",
        })
        rid = r.json()["id"]
        r = await c.post("/api/checkin/channels", json={
            "recorder_id": rid, "channel_id": 1, "role": "reception", "substream_trackid": 102,
        })
        assert r.status_code == 200

    async with SessionLocal() as s:
        rec = await s.get(CheckinRecorder, rid)
        assert rec.analytics_capable is False       # E2 → аналитики нет (авто)
        assert rec.password_enc.startswith("enc:")  # пароль зашифрован
        ch = (await s.execute(__import__("sqlalchemy").select(CheckinChannel))).scalars().first()
        assert ch.substream_trackid == 102


async def test_connection_test_endpoint(db, monkeypatch):
    async def fake_test(recorder, password=None):
        return {"ok": True, "message": "OK: mock", "info": {"model": "DS-H332"},
                "channels": [{"channel_id": 1, "name": "Вход", "online": True}],
                "tracks": [{"trackid": 102, "channel": 1, "type": "video", "is_sub": True}]}

    monkeypatch.setattr(checkin_ingest, "test_recorder", fake_test)
    async with _client() as c:
        r = await c.post("/api/checkin/test-recorder", json={"host": "10.0.0.7", "username": "a", "password": "b"})
        d = r.json()
        assert d["ok"] and d["tracks"][0]["is_sub"] is True


async def test_schedule_saved(db):
    async with _client() as c:
        r = await c.post("/api/checkin/schedule", json={"hour": 3, "minute": 15})
        assert r.json() == {"ok": True, "hour": 3, "minute": 15}
