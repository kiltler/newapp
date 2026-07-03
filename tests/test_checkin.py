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

    async def fake_ffmpeg(url, out_path, duration_s, run_id=None, label="", seg=None):
        calls["n"] += 1
        with open(out_path, "wb") as fh:
            fh.write(b"FAKECLIP")
        return True, ""

    monkeypatch.setattr(checkin_ingest, "_ffmpeg_download", fake_ffmpeg)

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


# ── Фаза 2: лог заселений ────────────────────────────────────────────────────
async def _make_hotel_with_clip():
    import datetime as _dt
    async with SessionLocal() as s:
        h = CheckinHotel(name="Гост Л")
        s.add(h); await s.flush()
        clip = CheckinClip(
            hotel_id=h.id, recorder_id=1, channel_id=1, role="reception",
            day=_dt.date(2026, 6, 15),
            start_ts=_dt.datetime(2026, 6, 15, 8, 0, 0),
            end_ts=_dt.datetime(2026, 6, 15, 8, 1, 0),
            path="clips/x.mp4", status="ok",
        )
        s.add(clip); await s.commit()
        return h.id, clip.id


async def test_log_created_from_clip(db):
    hid, cid = await _make_hotel_with_clip()
    async with _client() as c:
        r = await c.post("/api/checkin/logs", json={
            "clip_id": cid, "verdict": "checkin", "room": "214",
            "shift": "день", "note": "багаж, ключ выдан",
            "event_time": "2026-06-15T08:00:30",
        })
        assert r.status_code == 200
    from app.models import CheckinLog
    async with SessionLocal() as s:
        rows = (await s.execute(__import__("sqlalchemy").select(CheckinLog))).scalars().all()
        assert len(rows) == 1
        lg = rows[0]
        assert lg.verdict == "checkin" and lg.room == "214"
        assert lg.hotel_id == hid and lg.day.isoformat() == "2026-06-15"  # взято из клипа


async def test_log_bad_verdict_rejected(db):
    hid, cid = await _make_hotel_with_clip()
    async with _client() as c:
        r = await c.post("/api/checkin/logs", json={"clip_id": cid, "verdict": "maybe"})
        assert r.status_code == 422


async def test_log_delete_and_page(db):
    hid, cid = await _make_hotel_with_clip()
    async with _client() as c:
        lid = (await c.post("/api/checkin/logs", json={"clip_id": cid, "verdict": "disputed"})).json()["id"]
        r = await c.get("/checkin/logs")
        assert r.status_code == 200 and "спорное" in r.text
        assert (await c.post(f"/api/checkin/logs/{lid}/delete")).status_code == 200
    from app.models import CheckinLog
    async with SessionLocal() as s:
        assert (await s.execute(__import__("sqlalchemy").select(CheckinLog))).scalars().first() is None


async def test_channel_manual_add_auto_trackid_and_duplicate(db):
    async with _client() as c:
        hid = (await c.post("/api/checkin/hotels", json={"name": "Гост М"})).json()["id"]
        rid = (await c.post("/api/checkin/recorders", json={
            "hotel_id": hid, "host": "10.0.0.9", "model_type": "ds7616ni_e2"})).json()["id"]
        # ручной ввод только номера канала — trackid должен проставиться авто (1→102)
        r = await c.post("/api/checkin/channels", json={"recorder_id": rid, "channel_id": 1, "role": "entrance"})
        assert r.status_code == 200
        # дубликат того же канала — дружелюбный 409, а не 500
        r2 = await c.post("/api/checkin/channels", json={"recorder_id": rid, "channel_id": 1, "role": "entrance"})
        assert r2.status_code == 409
    async with SessionLocal() as s:
        ch = (await s.execute(__import__("sqlalchemy").select(CheckinChannel))).scalars().first()
        assert ch.substream_trackid == 102


# ── Прогресс ingestion ───────────────────────────────────────────────────────
async def test_ingestion_run_tracks_progress(db, monkeypatch, tmp_path):
    monkeypatch.setattr(checkin_ingest.settings, "clips_dir", str(tmp_path))
    async with SessionLocal() as s:
        h = CheckinHotel(name="Гост П")
        s.add(h); await s.flush()
        rec = CheckinRecorder(hotel_id=h.id, host="10.0.0.5", model_type="ds7616ni_e2",
                              night_start="07:00", night_end="24:00")
        s.add(rec); await s.flush()
        s.add(CheckinChannel(recorder_id=rec.id, channel_id=1, substream_trackid=102))
        s.add(CheckinChannel(recorder_id=rec.id, channel_id=2, substream_trackid=202))
        await s.commit()
        hid = h.id

    seg = ArchiveSegment(dt.datetime(2026, 6, 15, 8, 0, 0), dt.datetime(2026, 6, 15, 8, 0, 30))

    class FakeClient:
        async def search_activity(self, ch, a, b, *, mode="all"):
            return [seg]
        def rtsp_playback_url(self, trackid, a, b, *, rtsp_port=554):
            return f"rtsp://fake/{trackid}"

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: FakeClient())

    async def fake_ffmpeg(url, out_path, duration_s, run_id=None, label="", seg=None):
        if url.endswith("202"):                     # канал 2 «падает»
            return False, "ffmpeg: connection refused"
        with open(out_path, "wb") as fh:
            fh.write(b"OK")
        return True, ""

    monkeypatch.setattr(checkin_ingest, "_ffmpeg_download", fake_ffmpeg)

    summary = await checkin_ingest.run_ingestion(dt.date(2026, 6, 15), [hid], "manual")

    from app.models import CheckinIngestRun, IngestRunStatus
    async with SessionLocal() as s:
        run = (await s.execute(__import__("sqlalchemy").select(CheckinIngestRun))).scalars().first()
        assert run.status == IngestRunStatus.DONE
        assert run.downloaded == 1 and run.errors == 1
        assert run.recorders_done == 1 and run.recorders_total == 1
        assert run.detail[0]["error_samples"], "должны сохраниться примеры ошибок"

    # Эндпоинт статуса отдаёт последний прогон
    async with _client() as c:
        d = (await c.get("/api/checkin/ingest/status")).json()
        assert d["exists"] and d["status"] == "done"
        assert d["downloaded"] == 1 and d["errors"] == 1 and d["percent"] == 100


async def test_trackid_fallback_and_autolearn(db, monkeypatch, tmp_path):
    monkeypatch.setattr(checkin_ingest.settings, "clips_dir", str(tmp_path))
    async with SessionLocal() as s:
        h = CheckinHotel(name="Гост Т"); s.add(h); await s.flush()
        rec = CheckinRecorder(hotel_id=h.id, host="10.0.0.8", model_type="ds7616ni_e2",
                              night_start="07:00", night_end="24:00")
        s.add(rec); await s.flush()
        # заведомо неверный trackid (как «60» у пользователя) при канале 60
        s.add(CheckinChannel(recorder_id=rec.id, channel_id=60, substream_trackid=60))
        await s.commit(); rid = rec.id

    seg = ArchiveSegment(dt.datetime(2026, 6, 15, 8, 0, 0), dt.datetime(2026, 6, 15, 8, 0, 30))

    class FakeClient:
        async def search_activity(self, ch, a, b, *, mode="all"):
            return [seg]
        def rtsp_playback_url(self, trackid, a, b, *, rtsp_port=554):
            return f"rtsp://fake/{trackid}"

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: FakeClient())

    async def fake_ffmpeg(url, out_path, duration_s, run_id=None, label="", seg=None):
        tid = url.rsplit("/", 1)[-1]
        if tid == "6002":                       # правильный субпоток канала 60
            with open(out_path, "wb") as fh:
                fh.write(b"OK")
            return True, ""
        return False, "ffmpeg: Server returned 404 Not Found"

    monkeypatch.setattr(checkin_ingest, "_ffmpeg_download", fake_ffmpeg)

    st = await checkin_ingest.ingest_recorder(dt.date(2026, 6, 15), rid)
    assert st["downloaded"] == 1  # перебор кандидатов нашёл рабочий trackid 6002
    async with SessionLocal() as s:
        ch = (await s.execute(__import__("sqlalchemy").select(CheckinChannel))).scalars().first()
        assert ch.substream_trackid == 6002  # авто-запомнили рабочий trackid


# ── Превью каналов и битые имена ─────────────────────────────────────────────
async def test_recorder_snapshot_endpoint(db, monkeypatch):
    async with SessionLocal() as s:
        h = CheckinHotel(name="Г"); s.add(h); await s.flush()
        rec = CheckinRecorder(hotel_id=h.id, host="1.2.3.4"); s.add(rec); await s.commit(); rid = rec.id

    class Fake:
        async def get_snapshot(self, ch):
            return b"\xff\xd8jpegbytes"

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: Fake())
    async with _client() as c:
        r = await c.get(f"/api/checkin/recorders/{rid}/snapshot/6")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content == b"\xff\xd8jpegbytes"


async def test_recorder_snapshot_no_frame(db, monkeypatch):
    from app.drivers.base import NVRError
    async with SessionLocal() as s:
        h = CheckinHotel(name="Г2"); s.add(h); await s.flush()
        rec = CheckinRecorder(hotel_id=h.id, host="1.2.3.5"); s.add(rec); await s.commit(); rid = rec.id

    class Fake:
        async def get_snapshot(self, ch):
            raise NVRError("нет кадра")

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: Fake())
    async with _client() as c:
        r = await c.get(f"/api/checkin/recorders/{rid}/snapshot/6")
        assert r.status_code == 204


async def test_test_recorder_sanitizes_garbled_name(monkeypatch):
    from app.drivers.base import ChannelStatus, DeviceInfo

    class Fake:
        async def test_connection(self):
            return DeviceInfo(model="X")
        async def get_channel_statuses(self):
            return [ChannelStatus(channel_id=6, name="IP ���6", online=True)]
        async def list_tracks(self):
            return []

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: Fake())
    rec = CheckinRecorder(hotel_id=1, host="h")
    res = await checkin_ingest.test_recorder(rec)
    ch = res["channels"][0]
    assert ch["name"] == "канал 6"          # кракозябры → номер канала
    assert ch["raw_name"].startswith("IP")  # исходное имя сохранено


async def test_today_window_clamped_to_now(db, monkeypatch, tmp_path):
    monkeypatch.setattr(checkin_ingest.settings, "clips_dir", str(tmp_path))
    async with SessionLocal() as s:
        h = CheckinHotel(name="Гост С"); s.add(h); await s.flush()
        rec = CheckinRecorder(hotel_id=h.id, host="10.0.0.9", model_type="ds7616ni_e2",
                              analytics_capable=False, night_start="00:00", night_end="24:00")
        s.add(rec); await s.flush()
        s.add(CheckinChannel(recorder_id=rec.id, channel_id=1, substream_trackid=102))
        await s.commit(); rid = rec.id

    captured = {}

    class FakeClient:
        async def search_activity(self, ch, a, b, *, mode="all"):
            captured["start"], captured["end"] = a, b
            return []  # активности нет → скачивать нечего
        def rtsp_playback_url(self, *a, **k):
            return "rtsp://x"

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: FakeClient())

    today = dt.date.today()
    await checkin_ingest.ingest_recorder(today, rid)
    now = dt.datetime.now()
    assert captured["start"] == dt.datetime.combine(today, dt.time(0, 0))
    assert captured["end"] <= now + dt.timedelta(seconds=5)   # обрезано до «сейчас»
    assert captured["end"] > captured["start"]


async def test_test_clip_http_download_and_trim(db, monkeypatch, tmp_path):
    """Тест-клип: HTTP-скачивание сегмента ЗАКРЫТОГО файла + обрезка окна ffmpeg'ом."""
    monkeypatch.setattr(checkin_ingest.settings, "clips_dir", str(tmp_path))
    async with SessionLocal() as s:
        h = CheckinHotel(name="Гост Тест"); s.add(h); await s.flush()
        rec = CheckinRecorder(hotel_id=h.id, host="192.168.100.8", model_type="ds7616ni_e2")
        s.add(rec); await s.flush()
        s.add(CheckinChannel(recorder_id=rec.id, channel_id=60, role="reception", substream_trackid=6002))
        await s.commit(); hid = h.id

    dev_time = dt.datetime(2026, 7, 2, 23, 42, 0)
    open_seg = {"start": dev_time - dt.timedelta(minutes=12), "end": dev_time,
                "uri": "rtsp://192.168.100.8/Streaming/tracks/6001/?name=OPEN"}
    closed_seg = {"start": dev_time - dt.timedelta(hours=1), "end": dev_time - dt.timedelta(minutes=20),
                  "uri": "rtsp://192.168.100.8/Streaming/tracks/6001/?starttime=X&endtime=Y&name=CLOSED&size=9"}

    dl = {}

    class FakeClient:
        async def search_playback(self, ch, s0, e0, *, substream=True):
            return [open_seg, closed_seg]
        async def download_segment(self, uri, out_path, max_bytes=None):
            dl["uri"] = uri
            with open(out_path, "wb") as fh:
                fh.write(b"X" * 2048)
            return True, 2048, ""

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: FakeClient())

    trim = {}

    async def fake_trim(src, dst, offset_s, duration_s):
        trim["offset"], trim["duration"] = offset_s, duration_s
        with open(dst, "wb") as fh:
            fh.write(b"CLIP")
        return True, ""

    monkeypatch.setattr(checkin_ingest, "_ffmpeg_trim", fake_trim)

    summary = await checkin_ingest.run_test_clip(3, [hid])
    assert summary["downloaded"] == 1
    # качали по URI ЗАКРЫТОГО сегмента (не открытого)
    assert "name=CLOSED" in dl["uri"]
    # обрезка: смещение = win_start - seg_start = (−23мин) − (−60мин) = 37 мин; длительность 3 мин
    assert trim["offset"] == 37 * 60
    assert trim["duration"] == 3 * 60
    async with SessionLocal() as s:
        clip = (await s.execute(__import__("sqlalchemy").select(CheckinClip))).scalars().first()
        assert clip.status == ClipStatus.OK
        assert clip.end_ts == dev_time - dt.timedelta(minutes=20)
        assert clip.start_ts == dev_time - dt.timedelta(minutes=23)


# ── Отмена прогона ───────────────────────────────────────────────────────────
async def test_cancel_endpoint_marks_run(db):
    from app.models import CheckinIngestRun
    async with SessionLocal() as s:
        run = CheckinIngestRun(status="running", recorders_total=1)
        s.add(run); await s.commit(); rid = run.id
    async with _client() as c:
        d = (await c.post("/api/checkin/ingest/cancel")).json()
        assert d["ok"]
    assert checkin_ingest.is_cancelled(rid)
    checkin_ingest._clear_cancel(rid)


async def test_ingestion_cancel_stops(db, monkeypatch, tmp_path):
    monkeypatch.setattr(checkin_ingest.settings, "clips_dir", str(tmp_path))
    async with SessionLocal() as s:
        h = CheckinHotel(name="Гост Отм"); s.add(h); await s.flush()
        rec = CheckinRecorder(hotel_id=h.id, host="10.0.0.5", model_type="ds7616ni_e2")
        s.add(rec); await s.flush()
        s.add(CheckinChannel(recorder_id=rec.id, channel_id=1, substream_trackid=102))
        await s.commit(); hid = h.id

    class FakeClient:
        async def search_activity(self, ch, a, b, *, mode="all"):
            return [ArchiveSegment(dt.datetime(2026, 6, 15, 8), dt.datetime(2026, 6, 15, 8, 10))]
        def rtsp_playback_url(self, tid, a, b, *, rtsp_port=554):
            return f"rtsp://f/{tid}"

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: FakeClient())

    async def fake_dl(url, out_path, duration_s, run_id=None, label="", seg=None):
        checkin_ingest.request_cancel(run_id)  # имитируем нажатие «Отменить» во время скачивания
        return False, "отменено пользователем"

    monkeypatch.setattr(checkin_ingest, "_ffmpeg_download", fake_dl)

    # не-тестовый прогон (RTSP/ffmpeg-путь): за прошлый день, окно ночи валидно
    await checkin_ingest.run_ingestion(dt.date(2026, 6, 15), [hid], "manual")
    from app.models import CheckinIngestRun, IngestRunStatus
    async with SessionLocal() as s:
        run = (await s.execute(__import__("sqlalchemy").select(CheckinIngestRun))).scalars().first()
        assert run.status == IngestRunStatus.CANCELED


def test_try_next_track_conditions():
    assert checkin_ingest._try_next_track("Server returned 404 Not Found")
    assert checkin_ingest._try_next_track("нет данных от RTSP 40с — проверьте trackid")
    assert checkin_ingest._try_next_track("таймаут ffmpeg (1290с)")
    assert not checkin_ingest._try_next_track("401 Unauthorized")


async def test_abort_orphan_runs(db):
    from app.models import CheckinIngestRun, IngestRunStatus
    async with SessionLocal() as s:
        h = CheckinHotel(name="Z"); s.add(h); await s.flush()
        run = CheckinIngestRun(status=IngestRunStatus.RUNNING, recorders_total=1, current="идёт")
        s.add(run)
        s.add(CheckinClip(hotel_id=h.id, recorder_id=1, channel_id=60, day=dt.date(2026, 7, 2),
                          start_ts=dt.datetime(2026, 7, 2, 2, 51), end_ts=dt.datetime(2026, 7, 2, 3, 1),
                          path="x.mp4", status=ClipStatus.PENDING))
        await s.commit()

    await checkin_ingest.abort_orphan_runs()

    async with SessionLocal() as s:
        run = (await s.execute(__import__("sqlalchemy").select(CheckinIngestRun))).scalars().first()
        assert run.status == IngestRunStatus.ERROR and run.finished_at is not None
        clip = (await s.execute(__import__("sqlalchemy").select(CheckinClip))).scalars().first()
        assert clip.status == ClipStatus.ERROR


async def test_search_playback_parses_uri(monkeypatch):
    c = _hik()
    xml = (
        "<CMSearchResult><matchList>"
        "<searchMatchItem><timeSpan>"
        "<startTime>2026-07-02T02:00:00Z</startTime><endTime>2026-07-02T03:49:00Z</endTime>"
        "</timeSpan><mediaSegmentDescriptor><playbackURI>"
        "rtsp://192.168.100.8/Streaming/tracks/6002/?starttime=20260702T020000Z&amp;endtime=20260702T034900Z&amp;name=ch&amp;size=1"
        "</playbackURI></mediaSegmentDescriptor></searchMatchItem>"
        "</matchList><responseStatusStrg>OK</responseStatusStrg></CMSearchResult>"
    )

    async def fake(method, path, **kw):
        return _resp(200, xml)

    monkeypatch.setattr(c, "_request", fake)
    segs = await c.search_playback(60, dt.datetime(2026, 7, 2, 1), dt.datetime(2026, 7, 2, 4), substream=True)
    assert len(segs) == 1
    assert segs[0]["end"] == dt.datetime(2026, 7, 2, 3, 49)
    assert "tracks/6002" in segs[0]["uri"]


def test_authed_rtsp_injects_credentials():
    c = _hik()  # admin / p@ss/1
    u = c.authed_rtsp("rtsp://192.168.100.8/Streaming/tracks/6002/?starttime=x")
    assert u == "rtsp://admin:p%40ss%2F1@192.168.100.8/Streaming/tracks/6002/?starttime=x"
    assert c.authed_rtsp("rtsp://a:b@h/x") == "rtsp://a:b@h/x"  # уже с кредами — не трогаем


def test_rewrite_uri_window():
    uri = "rtsp://h/Streaming/tracks/6001/?starttime=20200601T020000Z&endtime=20200601T030000Z&name=n&size=9"
    out = checkin_ingest._rewrite_uri_window(uri, dt.datetime(2026, 7, 2, 3, 46), dt.datetime(2026, 7, 2, 3, 49))
    assert "starttime=20260702T034600Z" in out
    assert "endtime=20260702T034900Z" in out
    assert "name=n" in out and "size=9" in out  # name/size СОХРАНЯЕМ (иначе sub↔main путаются)


async def test_recorder_diag_endpoint(db, monkeypatch):
    async with SessionLocal() as s:
        h = CheckinHotel(name="Д"); s.add(h); await s.flush()
        rec = CheckinRecorder(hotel_id=h.id, host="192.168.100.8"); s.add(rec); await s.flush()
        s.add(CheckinChannel(recorder_id=rec.id, channel_id=60, substream_trackid=6002))
        await s.commit(); rid = rec.id

    class Fake:
        async def get_device_time(self):
            return dt.datetime(2026, 7, 2, 4, 0, 0)
        async def list_tracks(self):
            return [{"trackid": 6002, "channel": 60, "is_sub": True}]
        async def search_playback(self, ch, s0, e0, *, substream=True):
            if substream:
                return []  # субпоток не пишется
            return [{"start": dt.datetime(2026, 7, 1, 16), "end": dt.datetime(2026, 7, 1, 20),
                     "uri": "rtsp://192.168.100.8/Streaming/tracks/6001/?starttime=x&endtime=y"}]

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: Fake())
    async with _client() as c:
        d = (await c.get(f"/api/checkin/recorders/{rid}/diag")).json()
    assert d["build"]
    ch0 = d["channels"][0]
    assert ch0["sub_matches"] == 0
    assert ch0["main_matches"] == 1
    assert ch0["main_trackid_returned"] == "6001"


# ── Хранилище / удаление / HTTP-день ─────────────────────────────────────────
async def test_storage_info_and_set(db, tmp_path):
    async with _client() as c:
        d = (await c.get("/api/checkin/storage")).json()
        assert "dir" in d and "mounts" in d and "clips_count" in d
        r = await c.post("/api/checkin/storage", json={"path": str(tmp_path)})
        assert r.json()["ok"]
    assert checkin_ingest.active_clips_dir() == str(tmp_path)
    checkin_ingest.set_clips_dir(None)  # сброс, чтобы не влиять на другие тесты


async def test_delete_clip(db, tmp_path):
    from app.models import CheckinClip as _Clip
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video")
    async with SessionLocal() as s:
        h = CheckinHotel(name="Х"); s.add(h); await s.flush()
        clip = _Clip(hotel_id=h.id, recorder_id=1, channel_id=60, day=dt.date(2026, 7, 2),
                     start_ts=dt.datetime(2026, 7, 2, 1), end_ts=dt.datetime(2026, 7, 2, 1, 3),
                     path=str(f), status="ok", size_bytes=5)
        s.add(clip); await s.commit(); cid = clip.id
    async with _client() as c:
        assert (await c.post(f"/api/checkin/clips/{cid}/delete")).json()["ok"]
    assert not f.exists()  # файл удалён
    async with SessionLocal() as s:
        assert (await s.execute(__import__("sqlalchemy").select(CheckinClip))).scalars().first() is None


async def test_day_whole_http_download(db, monkeypatch, tmp_path):
    monkeypatch.setattr(checkin_ingest.settings, "clips_dir", str(tmp_path))
    checkin_ingest.set_clips_dir(None)
    async with SessionLocal() as s:
        h = CheckinHotel(name="День"); s.add(h); await s.flush()
        rec = CheckinRecorder(hotel_id=h.id, host="10.0.0.5", model_type="ds7616ni_e2",
                              night_start="07:00", night_end="24:00")
        s.add(rec); await s.flush()
        s.add(CheckinChannel(recorder_id=rec.id, channel_id=60, role="reception", substream_trackid=6002))
        await s.commit(); hid = h.id

    seg = {"start": dt.datetime(2026, 6, 15, 8), "end": dt.datetime(2026, 6, 15, 12),
           "uri": "rtsp://10.0.0.5/Streaming/tracks/6001/?starttime=X&endtime=Y&name=F&size=9"}

    class FakeClient:
        async def search_playback(self, ch, s0, e0, *, substream=True):
            return [seg]
        async def download_segment(self, uri, out_path, max_bytes=None):
            with open(out_path, "wb") as fh:
                fh.write(b"X" * 1000)
            return True, 1000, ""

    monkeypatch.setattr(checkin_ingest, "build_recorder_client", lambda rec, password=None: FakeClient())

    async def fake_trim(src, dst, offset_s, duration_s):
        with open(dst, "wb") as fh:
            fh.write(b"CLIP")
        return True, ""

    monkeypatch.setattr(checkin_ingest, "_ffmpeg_trim", fake_trim)

    summary = await checkin_ingest.run_ingestion(dt.date(2026, 6, 15), [hid], "manual", whole=True)
    assert summary["downloaded"] == 1
    async with SessionLocal() as s:
        clip = (await s.execute(__import__("sqlalchemy").select(CheckinClip))).scalars().first()
        assert clip.status == ClipStatus.OK
        assert clip.start_ts == dt.datetime(2026, 6, 15, 8)   # обрезано по началу окна (07:00 → сегмент 08:00)
