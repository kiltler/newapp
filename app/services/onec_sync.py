"""Интеграция 1С:Отель: синхронизация заселений и отметки на таймлайне.

Архитектура (CLAUDE.md §12 + патч мульти-ГС):
- Конфигурация — per-hotel (`OneCConnection`), никаких глобальных кредов в .env.
- Доступ к 1С ТОЛЬКО через стандартный OData (адаптер `OneCBackend`, чтобы позже
  заменить на HTTP-сервис). Имена объекта/реквизитов настраиваемые.
- Время заселения (`doc_time`) — НАИВНОЕ локальное время Хабаровска (§6.2):
  парсим дату 1С в поясе conn.onec_tz, переводим в пояс камер CAMERA_TZ и
  отбрасываем tzinfo. Никаких конверсий в UTC.
- Позиция отметки на видео: video_offset = (event_local + offset − clip.start_ts),
  где offset — смещение часов регистратора (time_offset_sec, меряется калибровкой).
  Окно PRE/POST-ROLL: гость появляется на камере раньше проводки документа.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crypto import decrypt
from app.database import SessionLocal
from app.models import (
    CheckinHotel,
    CheckinNotification,
    CheckinRecorder,
    NotificationStatus,
    OneCCheckin,
    OneCConnection,
    utcnow,
)

log = logging.getLogger(__name__)

_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# ── Время и этажи ────────────────────────────────────────────────────────────
# Фолбэк для систем с урезанной базой поясов (в свежих tzdata часть российских
# зон переехала в backward-ссылки и может отсутствовать). Etc/GMT-10 = UTC+10.
_TZ_FALLBACK = {
    "Asia/Khabarovsk": "Etc/GMT-10",
    "Asia/Vladivostok": "Etc/GMT-10",
}


def _tz(name: str):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001  (нет такой зоны в tzdata)
        alt = _TZ_FALLBACK.get(name or "")
        if alt:
            try:
                return ZoneInfo(alt)
            except Exception:  # noqa: BLE001
                pass
        log.warning("Часовой пояс «%s» не найден — использую UTC", name)
        return ZoneInfo("UTC")


def to_camera_local(value: dt.datetime, onec_tz: str) -> dt.datetime:
    """Дата 1С (наивная в поясе 1С) → наивное локальное время камер (CAMERA_TZ).

    Обычно оба пояса — Хабаровск, и преобразование тождественно. tzinfo в
    результате ОТБРАСЫВАЕТСЯ (§6.2 — настенное время, не UTC).
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=_tz(onec_tz))
    return value.astimezone(_tz(settings.camera_tz)).replace(tzinfo=None)


def parse_onec_datetime(raw: str) -> dt.datetime | None:
    """Дата из OData 1С: обычно ISO 'YYYY-MM-DDTHH:MM:SS' (без пояса)."""
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", ""))
    except ValueError:
        return None


# ── OData-бэкенд (адаптер: позже можно заменить на HTTP-сервис 1С) ───────────
class ODataBackend:
    """Клиент стандартного OData 1С для одной гостиницы (Document_Accommodation)."""

    def __init__(self, conn: OneCConnection):
        self.conn = conn
        self.base_url = (conn.base_url or "").rstrip("/")
        self.auth = httpx.BasicAuth(conn.username or "", decrypt(conn.password_enc) if conn.password_enc else "")
        # Дефолты на случай незаполненных полей (у несохранённого объекта
        # SQLAlchemy-дефолты ещё не применены — они срабатывают при INSERT)
        self.entity = conn.entity or settings.onec_default_entity
        self.field_ref = conn.field_ref or "Ref_Key"
        self.field_date = conn.field_date or "CheckInDate"          # время метки
        self.field_date_fallback = conn.field_date_fallback or "Date"
        self.field_query_date = conn.field_query_date or "Date"     # по нему $filter/$orderby
        self.field_guest = conn.field_guest or "GuestFullName"
        self.expand_room = conn.expand_room or "Room"
        self.field_room_number = conn.field_room_number or "Description"
        self.field_room_floor = conn.field_room_floor or "Floor"

    _MAX_PAGES = 100  # предохранитель от бесконечного листания

    def _filters(self) -> str:
        """$filter БЕЗ даты (в этой 1С отбор по полям документа запрещён — HTTP 500).
        Остаются только опциональный Posted и разделитель объекта."""
        c = self.conn
        parts: list[str] = []
        if c.filter_posted:
            parts.append("Posted eq true")
        if c.property_field and c.property_value:
            val = c.property_value.strip()
            if _GUID_RE.match(val):
                parts.append(f"{c.property_field} eq guid'{val}'")
            else:
                parts.append(f"{c.property_field} eq '{val}'")
        return " and ".join(parts)

    def _params(self, top: int, skip: int = 0) -> dict:
        # Свежие сверху: сортируем по служебной Date desc (по ней сортировка
        # разрешена, в отличие от CheckInDate). Фильтр по дате НЕ добавляем.
        params = {"$format": "json", "$top": str(top), "$orderby": f"{self.field_query_date} desc"}
        if skip:
            params["$skip"] = str(skip)
        flt = self._filters()
        if flt:
            params["$filter"] = flt
        if self.expand_room:  # разворот комнаты — номер и этаж готовыми полями
            params["$expand"] = self.expand_room
        return params

    def _query_date(self, row: dict) -> dt.datetime | None:
        """Дата документа (по ней решаем о стопе) в наивном локальном камер."""
        raw = parse_onec_datetime(row.get(self.field_query_date))
        return to_camera_local(raw, self.conn.onec_tz) if raw is not None else None

    async def fetch_checkins(self, since: dt.datetime, *, page: int = 500) -> list[dict]:
        """Заселения не старше `since` (граница). Тянем страницами Date desc и
        останавливаемся, как только встретили запись старее границы (список убыв.).

        Подробно логируется (для диагностики): полный URL запроса, HTTP-статус,
        число записей в value, и по каждой записи — её Date, граница и вердикт.
        """
        url = f"{self.base_url}/{self.entity}"
        log.info("1С: граница отсечения (наивное локальное) = %s; поле сортировки=%s, окно фильтруем на своей стороне",
                 since.isoformat(), self.field_query_date)
        rows: list[dict] = []
        async with httpx.AsyncClient(auth=self.auth, timeout=30.0, verify=False) as http:
            for pageno in range(self._MAX_PAGES):
                params = self._params(page, pageno * page)
                full_url = str(httpx.URL(url, params=params))
                log.info("1С GET %s", full_url)  # логин/пароль в заголовке Basic, в URL их нет
                resp = await http.get(url, params=params)
                log.info("1С ответ: HTTP %s (страница %d, skip=%d)", resp.status_code, pageno, pageno * page)
                if resp.status_code != 200:
                    log.warning("1С тело ответа: %s", resp.text[:500])
                    raise RuntimeError(f"OData HTTP {resp.status_code}: {resp.text[:200]}")
                batch = resp.json().get("value") or []
                log.info("1С: записей в value до фильтрации = %d", len(batch))
                stop = False
                for row in batch:
                    raw = row.get(self.field_query_date)
                    qd = self._query_date(row)
                    if qd is not None and qd < since:
                        log.info("  ref=%s %s=%s → %s < граница %s → ОТСЕКАЕТСЯ (и всё дальше старее) → СТОП",
                                 row.get(self.field_ref), self.field_query_date, raw,
                                 qd.isoformat(), since.isoformat())
                        stop = True
                        break
                    log.info("  ref=%s %s=%s → %s >= граница %s → проходит",
                             row.get(self.field_ref), self.field_query_date, raw,
                             qd.isoformat() if qd else "?", since.isoformat())
                    rows.append(row)
                if stop or len(batch) < page:
                    if len(batch) < page and not stop:
                        log.info("1С: страница неполная (%d < %d) → это конец данных", len(batch), page)
                    break
        log.info("1С: итог выборки — %d записей в окне (прочитано страниц: %d)", len(rows), pageno + 1)
        return rows

    async def probe(self) -> dict:
        """Проба связи: $top=1 (Date desc, $expand=Room) — видно свежую запись и разворот."""
        url = f"{self.base_url}/{self.entity}"
        params = self._params(top=1)
        try:
            async with httpx.AsyncClient(auth=self.auth, timeout=15.0, verify=False) as http:
                resp = await http.get(url, params=params)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code, "error": resp.text[:300]}
        rows = (resp.json().get("value") or [])
        sample = rows[0] if rows else None
        room = self.extract_room_floor(sample) if sample else (None, None)
        return {"ok": True, "status": 200, "sample": sample,
                "room_expanded": {"number": room[0], "floor": room[1]}}

    def extract_room_floor(self, row: dict) -> tuple[str | None, int | None]:
        """Номер и этаж из развёрнутого объекта Room. Пустой Room_Key → (None, None)."""
        room_obj = row.get(self.expand_room) or {}
        if not isinstance(room_obj, dict):
            return None, None
        number = (str(room_obj.get(self.field_room_number) or "").strip()) or None
        floor_raw = str(room_obj.get(self.field_room_floor) or "").strip()
        floor = int(floor_raw) if floor_raw.isdigit() else None
        return number, floor


def onec_backend_for(conn: OneCConnection) -> ODataBackend:
    """Фабрика бэкенда (точка замены OData → HTTP-сервис в будущем)."""
    return ODataBackend(conn)


# ── Синхронизация ────────────────────────────────────────────────────────────
async def _notify_unreachable(session: AsyncSession, hotel_id: int, hotel_name: str, msg: str) -> None:
    """Уведомление «1С недоступна» через Центр уведомлений заселений — без дублей."""
    existing = (
        await session.execute(
            select(CheckinNotification).where(
                CheckinNotification.type == "onec_unreachable",
                CheckinNotification.hotel_id == hotel_id,
                CheckinNotification.status == NotificationStatus.NEW,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(CheckinNotification(
            type="onec_unreachable", hotel_id=hotel_id,
            title=f"1С:Отель «{hotel_name}» недоступна: {msg[:200]}",
            payload={"hotel_id": hotel_id, "error": msg[:500]},
        ))


async def sync_hotel(session: AsyncSession, hotel_id: int, *, full: bool = False) -> dict:
    """Синхронизация заселений одной гостиницы. Ошибка не роняет остальные ГС."""
    conn = (
        await session.execute(
            select(OneCConnection).where(OneCConnection.hotel_id == hotel_id)
        )
    ).scalar_one_or_none()
    if conn is None or not conn.enabled:
        return {"hotel_id": hotel_id, "skipped": "disabled"}
    hotel = await session.get(CheckinHotel, hotel_id)
    hotel_name = hotel.name if hotel else str(hotel_id)

    # Скользящее окно: каждый синк перечитывает заселения за последние N дней и
    # делает идемпотентный upsert. Ловит и новые заселения, и поздние правки
    # недавних; строгий watermark не нужен (для anti-theft важны свежие события).
    lookback = max(conn.lookback_days or 5, 1)
    if full:
        lookback = max(lookback, 30)
    day_start = dt.datetime.now(_tz(settings.camera_tz)).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    since = day_start - dt.timedelta(days=lookback)

    backend = onec_backend_for(conn)
    result = {"hotel_id": hotel_id, "fetched": 0, "inserted": 0, "updated": 0, "skipped": 0}
    try:
        rows = await backend.fetch_checkins(since=since)
    except Exception as exc:  # noqa: BLE001  (сеть/учётка/кривой OData — фиксируем и живём)
        conn.last_sync_at = utcnow()
        conn.last_ok = False
        conn.last_msg = str(exc)[:500]
        await _notify_unreachable(session, hotel_id, hotel_name, str(exc))
        await session.commit()
        log.warning("1С «%s»: синхронизация не удалась: %s", hotel_name, exc)
        return {**result, "error": str(exc)[:300]}

    existing = {
        r.onec_ref: r
        for r in (
            await session.execute(
                select(OneCCheckin).where(OneCCheckin.hotel_id == hotel_id)
            )
        ).scalars()
    }
    last_doc: dt.datetime | None = None
    for row in rows:
        result["fetched"] += 1
        ref = str(row.get(backend.field_ref) or "").strip()
        # Основная метка — CheckInDate (момент заезда), запасная — Date (проведение)
        raw_date = parse_onec_datetime(
            row.get(backend.field_date) or row.get(backend.field_date_fallback)
        )
        if not ref or raw_date is None:
            result["skipped"] += 1
            continue
        doc_time = to_camera_local(raw_date, conn.onec_tz)
        room, floor = backend.extract_room_floor(row)  # из развёрнутого Room
        guest_val = row.get(backend.field_guest)
        guest = str(guest_val).strip() if guest_val not in (None, "") else None

        rec = existing.get(ref)
        if rec is None:
            session.add(OneCCheckin(
                hotel_id=hotel_id, onec_ref=ref, doc_time=doc_time, room=room,
                floor=floor, guest=guest, arrival_planned=None, raw=row,
            ))
            result["inserted"] += 1
        else:
            rec.doc_time = doc_time
            rec.room = room
            rec.floor = floor
            rec.guest = guest
            rec.raw = row
            result["updated"] += 1
        if last_doc is None or doc_time > last_doc:
            last_doc = doc_time

    conn.last_sync_at = utcnow()
    conn.last_ok = True
    conn.last_msg = f"получено {result['fetched']}, новых {result['inserted']}"
    if last_doc is not None:
        conn.last_doc_time = last_doc
    await session.commit()
    result["last_doc_time"] = last_doc.isoformat() if last_doc else None
    return result


async def sync_all(session: AsyncSession, *, full: bool = False) -> list[dict]:
    """Синхронизация всех включённых подключений; сбой одной ГС не мешает остальным."""
    ids = (
        await session.execute(
            select(OneCConnection.hotel_id).where(OneCConnection.enabled.is_(True))
        )
    ).scalars().all()
    results = []
    for hid in ids:
        try:
            results.append(await sync_hotel(session, hid, full=full))
        except Exception as exc:  # noqa: BLE001  (страховка: не прерываем перебор)
            log.exception("1С: неожиданная ошибка синка гостиницы %s", hid)
            results.append({"hotel_id": hid, "error": str(exc)[:300]})
    return results


async def scheduled_sync() -> None:
    """Обёртка для планировщика: калибровка часов + синк всех ГС."""
    await calibrate_clocks()
    async with SessionLocal() as session:
        await sync_all(session)


# ── Калибровка часов регистраторов ──────────────────────────────────────────
async def calibrate_clocks() -> list[dict]:
    """Меряет смещение часов каждого регистратора: offset = время_рег − время_сервера.

    Нужна для точной постановки отметок 1С на таймлайн (событие 1С живёт в
    «правильном» времени, а клип — в настенном времени регистратора).
    """
    from app.services.checkin_ingest import build_recorder_client

    out: list[dict] = []
    async with SessionLocal() as session:
        recorders = (
            await session.execute(
                select(CheckinRecorder).where(CheckinRecorder.enabled.is_(True))
            )
        ).scalars().all()
        for rec in recorders:
            try:
                dev_now = await build_recorder_client(rec).get_device_time()
                server_now = dt.datetime.now(_tz(settings.camera_tz)).replace(tzinfo=None)
                offset = int((dev_now - server_now).total_seconds())
                rec.time_offset_sec = offset
                rec.time_offset_at = utcnow()
                out.append({"recorder_id": rec.id, "offset_sec": offset})
                if abs(offset) > settings.onec_clock_warn_sec:
                    log.warning("Часы регистратора %s (%s) уехали на %d с",
                                rec.id, rec.name or rec.host, offset)
            except Exception as exc:  # noqa: BLE001  (регистратор недоступен — пропуск)
                out.append({"recorder_id": rec.id, "error": str(exc)[:200]})
        await session.commit()
    return out


# ── Отметки на таймлайне клипа ───────────────────────────────────────────────
async def markers_for_clip(session: AsyncSession, clip) -> dict:
    """Отметки заселений 1С для клипа: только события ЕГО гостиницы.

    Позиция: video_offset = (doc_time + offset − clip.start_ts), окно
    [−PRE_ROLL, +POST_ROLL] + запас MARGIN. Для этажных камер (роль floorN)
    показываются только заселения этого этажа.
    """
    recorder = await session.get(CheckinRecorder, clip.recorder_id)
    offset = (recorder.time_offset_sec or 0) if recorder else 0
    conn = (
        await session.execute(
            select(OneCConnection).where(OneCConnection.hotel_id == clip.hotel_id)
        )
    ).scalar_one_or_none()
    mask = conn.mask_guest if conn else True

    start = clip.start_ts.replace(tzinfo=None) if clip.start_ts.tzinfo else clip.start_ts
    end = clip.end_ts.replace(tzinfo=None) if clip.end_ts.tzinfo else clip.end_ts
    duration = max((end - start).total_seconds(), 1)
    pre = settings.onec_pre_roll_sec
    post = settings.onec_post_roll_sec
    margin = settings.onec_marker_margin_sec

    # События, чьё окно пересекает клип (с учётом смещения часов)
    lo = start - dt.timedelta(seconds=offset + post + margin)
    hi = end + dt.timedelta(seconds=pre + margin - offset)
    rows = (
        await session.execute(
            select(OneCCheckin)
            .where(
                OneCCheckin.hotel_id == clip.hotel_id,
                OneCCheckin.doc_time >= lo,
                OneCCheckin.doc_time <= hi,
            )
            .order_by(OneCCheckin.doc_time)
        )
    ).scalars().all()

    floor_match = re.fullmatch(r"floor(\d+)", clip.role or "")
    clip_floor = int(floor_match.group(1)) if floor_match else None

    markers = []
    for ev in rows:
        if clip_floor is not None and ev.floor is not None and ev.floor != clip_floor:
            continue  # этажная камера — чужие этажи не показываем
        doc_local = ev.doc_time.replace(tzinfo=None) if ev.doc_time.tzinfo else ev.doc_time
        pos = (doc_local - start).total_seconds() + offset
        w0 = max(pos - pre, 0)
        w1 = min(pos + post, duration)
        if w1 <= 0 or w0 >= duration:
            continue  # окно не попало в клип
        guest = ev.guest
        if guest and mask:
            parts = guest.split()
            guest = parts[0] + (" " + " ".join(p[0] + "." for p in parts[1:] if p) if len(parts) > 1 else "")
        markers.append({
            "id": ev.id,
            "room": ev.room,
            "floor": ev.floor,
            "guest": guest,
            "doc_time": doc_local.isoformat(),
            "offset_sec": round(max(min(pos, duration), 0)),
            "window_start_sec": round(w0),
            "window_end_sec": round(w1),
            "matched_log_id": ev.matched_log_id,
        })

    return {
        "clip_id": clip.id,
        "duration_sec": round(duration),
        "clock": {
            "offset_sec": offset,
            "measured_at": recorder.time_offset_at.isoformat() if recorder and recorder.time_offset_at else None,
            "warn": abs(offset) > settings.onec_clock_warn_sec,
        },
        "markers": markers,
    }
