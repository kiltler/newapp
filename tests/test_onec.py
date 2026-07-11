"""Тесты интеграции 1С:Отель: синхронизация per-hotel, изоляция ГС, маркеры."""
from __future__ import annotations

import datetime as dt

import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    CheckinClip,
    CheckinHotel,
    CheckinNotification,
    CheckinRecorder,
    ClipStatus,
    OneCCheckin,
    OneCConnection,
)
from app.services import onec_sync


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _hotel_with_conn(name: str, **conn_kw) -> tuple[int, int]:
    """Гостиница + включённое подключение 1С. Возвращает (hotel_id, conn_id)."""
    async with SessionLocal() as s:
        h = CheckinHotel(name=name)
        s.add(h)
        await s.flush()
        conn = OneCConnection(hotel_id=h.id, enabled=True, base_url="http://onec/base/odata/standard.odata",
                              username="svc", **conn_kw)
        s.add(conn)
        await s.commit()
        return h.id, conn.id


def _row(ref: str, checkin: str, room: str | None = "614", floor: str = "6",
         guest: str = "Иванов Иван Иванович", **extra) -> dict:
    """Запись Document_Accommodation с развёрнутым объектом Room."""
    r = {"Ref_Key": ref, "CheckInDate": checkin, "GuestFullName": guest,
         "Room_Key": f"room-{ref}", **extra}
    if room is not None:
        r["Room"] = {"Description": room, "Floor": floor}
    return r


# ── Чистые функции ───────────────────────────────────────────────────────────
def test_to_camera_local_naive():
    """Дата 1С → наивное локальное время камер, tzinfo отброшен (§6.2)."""
    src = dt.datetime(2026, 7, 10, 14, 30)  # наивная, пояс 1С = Хабаровск
    out = onec_sync.to_camera_local(src, "Asia/Khabarovsk")
    assert out == dt.datetime(2026, 7, 10, 14, 30)
    assert out.tzinfo is None
    # пояс 1С отличается от пояса камер — время сдвигается
    out2 = onec_sync.to_camera_local(dt.datetime(2026, 7, 10, 7, 30), "Europe/Moscow")
    assert out2 == dt.datetime(2026, 7, 10, 14, 30)  # МСК+7 = Хабаровск
    assert out2.tzinfo is None


def test_odata_filter_building():
    """$filter: дата + Posted + разделитель объекта (guid и строка)."""
    conn = OneCConnection(hotel_id=1, filter_posted=True,
                          property_field="Организация_Key",
                          property_value="a1b2c3d4-1111-2222-3333-444455556666")
    b = onec_sync.ODataBackend(conn)
    flt = b._filters(dt.datetime(2026, 7, 10, 0, 0))
    assert "CheckInDate ge datetime'2026-07-10T00:00:00'" in flt   # основная метка — заезд
    assert "Posted eq true" in flt
    assert "Организация_Key eq guid'a1b2c3d4-1111-2222-3333-444455556666'" in flt
    # не-GUID значение — строковое сравнение
    conn2 = OneCConnection(hotel_id=1, filter_posted=False,
                           property_field="Объект", property_value="Суворова 8")
    assert onec_sync.ODataBackend(conn2)._filters(None) == "Объект eq 'Суворова 8'"


def test_odata_expand_room_and_extract():
    """$expand=Room всегда добавляется; из Room берутся Description и Floor(int)."""
    b = onec_sync.ODataBackend(OneCConnection(hotel_id=1))
    params = b._params(None, top=10)
    assert params.get("$expand") == "Room"
    assert b.extract_room_floor({"Room": {"Description": "614", "Floor": "6"}}) == ("614", 6)
    # пустой Room_Key → нет объекта Room → (None, None), но заселение сохранимо
    assert b.extract_room_floor({"Room_Key": "00000000-0000-0000-0000-000000000000"}) == (None, None)
    # этаж не число → None, номер остаётся
    assert b.extract_room_floor({"Room": {"Description": "Люкс", "Floor": ""}}) == ("Люкс", None)


# ── Синхронизация ────────────────────────────────────────────────────────────
async def test_sync_hotel_upsert_sliding_window(db, monkeypatch):
    hid, _ = await _hotel_with_conn("ГС-А")
    calls = {"since": []}

    async def fake_fetch(self, since, **kw):
        calls["since"].append(since)
        return [
            _row("ref-1", "2026-07-09T21:15:00", room="312", floor="3"),
            _row("ref-2", "2026-07-09T23:40:00", room="507", floor="5"),
        ]

    monkeypatch.setattr(onec_sync.ODataBackend, "fetch_checkins", fake_fetch)

    async with SessionLocal() as s:
        r1 = await onec_sync.sync_hotel(s, hid)
        assert r1["inserted"] == 2 and r1["fetched"] == 2

        rows = (await s.execute(select(OneCCheckin).order_by(OneCCheckin.doc_time))).scalars().all()
        assert len(rows) == 2
        assert rows[0].room == "312" and rows[0].floor == 3   # из развёрнутого Room
        assert rows[1].room == "507" and rows[1].floor == 5
        assert rows[0].doc_time == dt.datetime(2026, 7, 9, 21, 15)  # CheckInDate, наивное локальное
        assert rows[0].guest == "Иванов Иван Иванович"

        # повторный синк — те же ref → апдейт без дублей; окно скользящее (не watermark)
        r2 = await onec_sync.sync_hotel(s, hid)
        assert r2["updated"] == 2 and r2["inserted"] == 0
        assert calls["since"][0] == calls["since"][1]  # одинаковое окно последних N дней

        conn = (await s.execute(select(OneCConnection))).scalar_one()
        assert conn.last_ok is True


async def test_checkin_date_fallback_and_empty_room(db, monkeypatch):
    """Нет CheckInDate → берём Date; пустой Room → заселение сохраняется без номера/этажа."""
    hid, _ = await _hotel_with_conn("Фолбэк")

    async def fake_fetch(self, since, **kw):
        return [
            {"Ref_Key": "a", "Date": "2026-07-09T10:00:00", "GuestFullName": "Гость",
             "Room_Key": "00000000-0000-0000-0000-000000000000"},  # нет CheckInDate и нет Room
            _row("b", "2026-07-09T12:00:00", room="204", floor="2"),
        ]

    monkeypatch.setattr(onec_sync.ODataBackend, "fetch_checkins", fake_fetch)
    async with SessionLocal() as s:
        r = await onec_sync.sync_hotel(s, hid)
        assert r["inserted"] == 2
        a = (await s.execute(select(OneCCheckin).where(OneCCheckin.onec_ref == "a"))).scalar_one()
        assert a.doc_time == dt.datetime(2026, 7, 9, 10, 0)  # взято из Date
        assert a.room is None and a.floor is None            # пустой Room — но заселение есть
        b = (await s.execute(select(OneCCheckin).where(OneCCheckin.onec_ref == "b"))).scalar_one()
        assert b.room == "204" and b.floor == 2


async def test_sync_isolation_between_hotels(db, monkeypatch):
    """События ГС-А не попадают к ГС-Б; сбой одной не мешает другой."""
    hid_a, _ = await _hotel_with_conn("Изол-А")
    hid_b, _ = await _hotel_with_conn("Изол-Б")

    async def fake_fetch(self, since, **kw):
        if self.conn.hotel_id == hid_a:
            return [_row("ref-a", "2026-07-09T20:00:00")]
        raise RuntimeError("1С гостиницы Б лежит")

    monkeypatch.setattr(onec_sync.ODataBackend, "fetch_checkins", fake_fetch)

    async with SessionLocal() as s:
        results = await onec_sync.sync_all(s)
        by_hotel = {r["hotel_id"]: r for r in results}
        assert by_hotel[hid_a]["inserted"] == 1          # А синхронизировалась
        assert "error" in by_hotel[hid_b]                 # Б упала, но не уронила А

        rows = (await s.execute(select(OneCCheckin))).scalars().all()
        assert len(rows) == 1 and rows[0].hotel_id == hid_a

        # уведомление о недоступности 1С — только для Б, без дублей при повторе
        await onec_sync.sync_all(s)
        notifs = (
            await s.execute(
                select(CheckinNotification).where(CheckinNotification.type == "onec_unreachable")
            )
        ).scalars().all()
        assert len(notifs) == 1 and notifs[0].hotel_id == hid_b


async def test_sync_skips_disabled(db):
    async with SessionLocal() as s:
        h = CheckinHotel(name="Выкл")
        s.add(h)
        await s.flush()
        s.add(OneCConnection(hotel_id=h.id, enabled=False))
        await s.commit()
        r = await onec_sync.sync_hotel(s, h.id)
        assert r["skipped"] == "disabled"


# ── Маркеры на таймлайне ─────────────────────────────────────────────────────
async def _clip(hid: int, rid: int, role: str = "reception") -> int:
    async with SessionLocal() as s:
        clip = CheckinClip(
            hotel_id=hid, recorder_id=rid, channel_id=6, role=role,
            day=dt.date(2026, 7, 9), start_ts=dt.datetime(2026, 7, 9, 20, 0),
            end_ts=dt.datetime(2026, 7, 9, 23, 0), path="/x.mp4",
            size_bytes=1, status=ClipStatus.OK,
        )
        s.add(clip)
        await s.commit()
        return clip.id


async def test_markers_position_offset_and_isolation(db):
    hid, _ = await _hotel_with_conn("Маркеры")
    hid2, _ = await _hotel_with_conn("Чужая")
    async with SessionLocal() as s:
        rec = CheckinRecorder(hotel_id=hid, host="10.0.0.1", time_offset_sec=60)  # часы NVR на +60с
        s.add(rec)
        await s.flush()
        rid = rec.id
        # событие своей ГС в 21:00, чужой ГС — тоже в 21:00 (не должно попасть)
        s.add(OneCCheckin(hotel_id=hid, onec_ref="r1", doc_time=dt.datetime(2026, 7, 9, 21, 0),
                          room="312", floor=3, guest="Иванов Иван"))
        s.add(OneCCheckin(hotel_id=hid2, onec_ref="r1", doc_time=dt.datetime(2026, 7, 9, 21, 0),
                          room="777", floor=7, guest="Чужой Гость"))
        await s.commit()
    cid = await _clip(hid, rid)

    async with _client() as c:
        d = (await c.get(f"/api/checkin/clips/{cid}/markers")).json()
    assert len(d["markers"]) == 1                       # чужая гостиница отфильтрована
    m = d["markers"][0]
    # позиция: (21:00 − 20:00) + 60с смещения часов = 3660-я секунда видео
    assert m["offset_sec"] == 3660
    assert m["window_start_sec"] == 3660 - 300           # PRE_ROLL
    assert m["window_end_sec"] == 3660 + 120             # POST_ROLL
    assert m["room"] == "312"
    assert m["guest"] == "Иванов И."                     # ФИО замаскировано
    assert d["clock"]["offset_sec"] == 60 and d["clock"]["warn"] is False


async def test_markers_floor_filtering(db):
    """Этажная камера видит только заселения своего этажа."""
    hid, _ = await _hotel_with_conn("Этажи")
    async with SessionLocal() as s:
        rec = CheckinRecorder(hotel_id=hid, host="10.0.0.2")
        s.add(rec)
        await s.flush()
        rid = rec.id
        s.add(OneCCheckin(hotel_id=hid, onec_ref="f3", doc_time=dt.datetime(2026, 7, 9, 21, 0),
                          room="312", floor=3))
        s.add(OneCCheckin(hotel_id=hid, onec_ref="f5", doc_time=dt.datetime(2026, 7, 9, 21, 30),
                          room="507", floor=5))
        await s.commit()
    cid = await _clip(hid, rid, role="floor3")

    async with _client() as c:
        d = (await c.get(f"/api/checkin/clips/{cid}/markers")).json()
    assert [m["room"] for m in d["markers"]] == ["312"]  # только 3-й этаж


# ── API подключения ──────────────────────────────────────────────────────────
async def test_connection_save_load_and_password_keep(db):
    async with SessionLocal() as s:
        h = CheckinHotel(name="Конн")
        s.add(h)
        await s.commit()
        hid = h.id

    async with _client() as c:
        # до создания — форма с дефолтами
        d = (await c.get(f"/api/checkin/onec/connection?hotel_id={hid}")).json()
        assert d["exists"] is False and d["entity"] == "Document_Accommodation"
        assert d["field_date"] == "CheckInDate" and d["expand_room"] == "Room"

        body = {"hotel_id": hid, "enabled": True, "base_url": "http://1c/hotel/odata/standard.odata",
                "username": "svc", "password": "s3cret", "property_field": "Организация_Key",
                "property_value": "a1b2c3d4-1111-2222-3333-444455556666"}
        assert (await c.post("/api/checkin/onec/connection", json=body)).status_code == 200

        d2 = (await c.get(f"/api/checkin/onec/connection?hotel_id={hid}")).json()
        assert d2["exists"] and d2["has_password"] and d2["username"] == "svc"

        # пустой пароль при повторном сохранении — не затирает старый
        body["password"] = ""
        body["username"] = "svc2"
        assert (await c.post("/api/checkin/onec/connection", json=body)).status_code == 200

    async with SessionLocal() as s:
        from app.crypto import decrypt
        conn = (await s.execute(select(OneCConnection))).scalar_one()
        assert conn.username == "svc2"
        assert decrypt(conn.password_enc) == "s3cret"  # пароль сохранился

    async with _client() as c:
        # включённое подключение без URL — ошибка валидации
        bad = {"hotel_id": hid, "enabled": True, "base_url": "ftp://х"}
        assert (await c.post("/api/checkin/onec/connection", json=bad)).status_code == 422
        # статус по гостиницам отвечает
        st = (await c.get("/api/checkin/onec/status")).json()
        assert any(x["hotel_id"] == hid and x["enabled"] for x in st)
