"""Модуль «Заселения»: настройки (гостиницы/регистраторы/каналы), тест
подключения, ночной ingestion и локальный просмотр клипов.

Все операционные настройки — только через этот UI, хранятся в SQLite.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import schemas
from app.config import settings
from app.crypto import encrypt
from app.database import get_session
from app.models import (
    CHANNEL_ROLE_NAMES,
    RECORDER_ANALYTICS_DEFAULT,
    RECORDER_MODEL_NAMES,
    CheckinChannel,
    CheckinClip,
    CheckinHotel,
    CheckinIngestRun,
    CheckinLog,
    CheckinNotification,
    CheckinRecorder,
    CheckinVerdict,
    ClipStatus,
    NotificationStatus,
    RecorderModel,
)
from app.scheduler import reschedule_checkin_job
from app.services import appsettings, checkin_ingest

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
from app.templatefilters import register as _register_filters  # noqa: E402

_register_filters(templates)

router = APIRouter(tags=["checkin"])


def _clip_url(path: str) -> str:
    """Файловый путь клипа → веб-URL под /clips."""
    try:
        rel = os.path.relpath(path, settings.clips_dir)
    except ValueError:
        rel = os.path.basename(path)
    return "/clips/" + rel.replace(os.sep, "/")


# ── Страницы ─────────────────────────────────────────────────────────────────
@router.get("/checkin", response_class=HTMLResponse)
async def checkin_clips(
    request: Request,
    hotel: int | None = None,
    day: str | None = None,
    channel: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    hotels = (await session.execute(select(CheckinHotel).order_by(CheckinHotel.name))).scalars().all()
    q = select(CheckinClip).order_by(CheckinClip.start_ts.desc())
    if hotel:
        q = q.where(CheckinClip.hotel_id == hotel)
    if channel is not None:
        q = q.where(CheckinClip.channel_id == channel)
    day_val = None
    if day:
        try:
            day_val = dt.date.fromisoformat(day)
            q = q.where(CheckinClip.day == day_val)
        except ValueError:
            pass
    clips = (await session.execute(q.limit(500))).scalars().all()
    hotel_names = {h.id: h.name for h in hotels}
    rows = [
        {
            "id": c.id, "hotel": hotel_names.get(c.hotel_id, c.hotel_id),
            "channel_id": c.channel_id, "role": CHANNEL_ROLE_NAMES.get(c.role, c.role),
            "day": c.day.isoformat() if c.day else "",
            "start": c.start_ts, "end": c.end_ts,
            "status": c.status, "size_mb": round(c.size_bytes / 1048576, 1) if c.size_bytes else 0,
            "url": _clip_url(c.path) if c.status == ClipStatus.OK else None,
            "error": c.error,
        }
        for c in clips
    ]
    channels = sorted({c.channel_id for c in clips})
    unread = (
        await session.execute(
            select(func.count()).select_from(CheckinNotification).where(
                CheckinNotification.status == NotificationStatus.NEW
            )
        )
    ).scalar_one()
    return templates.TemplateResponse("checkin_clips.html", {
        "request": request, "clips": rows, "hotels": hotels, "channels": channels,
        "f_hotel": hotel, "f_day": day or "", "f_channel": channel, "unread": unread,
    })


@router.get("/checkin/settings", response_class=HTMLResponse)
async def checkin_settings(request: Request, session: AsyncSession = Depends(get_session)):
    hotels = (
        await session.execute(
            select(CheckinHotel)
            .options(selectinload(CheckinHotel.recorders).selectinload(CheckinRecorder.channels))
            .order_by(CheckinHotel.name)
        )
    ).scalars().all()
    job_hour = await appsettings.get_int(session, "checkin_job_hour", 1)
    job_minute = await appsettings.get_int(session, "checkin_job_minute", 0)
    return templates.TemplateResponse("checkin_settings.html", {
        "request": request, "hotels": hotels,
        "model_names": RECORDER_MODEL_NAMES, "role_names": CHANNEL_ROLE_NAMES,
        "analytics_default": RECORDER_ANALYTICS_DEFAULT,
        "job_hour": job_hour, "job_minute": job_minute,
    })


@router.get("/checkin/notifications", response_class=HTMLResponse)
async def checkin_notifications(
    request: Request, status: str | None = None, session: AsyncSession = Depends(get_session)
):
    q = select(CheckinNotification).order_by(CheckinNotification.created_at.desc())
    if status:
        q = q.where(CheckinNotification.status == status)
    notes = (await session.execute(q.limit(300))).scalars().all()
    hotels = {h.id: h.name for h in (await session.execute(select(CheckinHotel))).scalars()}
    return templates.TemplateResponse("checkin_notifications.html", {
        "request": request, "notes": notes, "hotels": hotels, "f_status": status or "",
    })


# ── Гостиницы ────────────────────────────────────────────────────────────────
@router.post("/api/checkin/hotels")
async def create_hotel(data: schemas.CheckinHotelIn, session: AsyncSession = Depends(get_session)):
    hotel = CheckinHotel(name=data.name.strip(), enabled=data.enabled)
    session.add(hotel)
    await session.commit()
    return {"id": hotel.id}


@router.post("/api/checkin/hotels/{hotel_id}")
async def update_hotel(hotel_id: int, data: schemas.CheckinHotelIn, session: AsyncSession = Depends(get_session)):
    hotel = await session.get(CheckinHotel, hotel_id)
    if hotel is None:
        raise HTTPException(404, "Гостиница не найдена")
    hotel.name = data.name.strip()
    hotel.enabled = data.enabled
    await session.commit()
    return {"ok": True}


@router.post("/api/checkin/hotels/{hotel_id}/delete")
async def delete_hotel(hotel_id: int, session: AsyncSession = Depends(get_session)):
    await session.execute(delete(CheckinHotel).where(CheckinHotel.id == hotel_id))
    await session.commit()
    return {"ok": True}


# ── Регистраторы ─────────────────────────────────────────────────────────────
def _resolve_analytics(model_type: str, explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return RECORDER_ANALYTICS_DEFAULT.get(model_type, False)


def _validate_recorder(data: schemas.CheckinRecorderIn) -> None:
    if not data.host.strip():
        raise HTTPException(422, "Укажите host/IP регистратора")
    if data.model_type not in RECORDER_MODEL_NAMES:
        raise HTTPException(422, "Неизвестный тип регистратора")


@router.post("/api/checkin/recorders")
async def create_recorder(data: schemas.CheckinRecorderIn, session: AsyncSession = Depends(get_session)):
    _validate_recorder(data)
    if await session.get(CheckinHotel, data.hotel_id) is None:
        raise HTTPException(404, "Гостиница не найдена")
    rec = CheckinRecorder(
        hotel_id=data.hotel_id, name=data.name.strip(), host=data.host.strip(),
        http_port=data.http_port, rtsp_port=data.rtsp_port, username=data.username.strip(),
        password_enc=encrypt(data.password) if data.password else "",
        model_type=data.model_type,
        analytics_capable=_resolve_analytics(data.model_type, data.analytics_capable),
        night_start=data.night_start, night_end=data.night_end, enabled=data.enabled,
    )
    session.add(rec)
    await session.commit()
    return {"id": rec.id}


@router.post("/api/checkin/recorders/{rec_id}")
async def update_recorder(rec_id: int, data: schemas.CheckinRecorderIn, session: AsyncSession = Depends(get_session)):
    _validate_recorder(data)
    rec = await session.get(CheckinRecorder, rec_id)
    if rec is None:
        raise HTTPException(404, "Регистратор не найден")
    rec.hotel_id = data.hotel_id
    rec.name = data.name.strip()
    rec.host = data.host.strip()
    rec.http_port = data.http_port
    rec.rtsp_port = data.rtsp_port
    rec.username = data.username.strip()
    if data.password:  # пусто = не менять пароль
        rec.password_enc = encrypt(data.password)
    rec.model_type = data.model_type
    rec.analytics_capable = _resolve_analytics(data.model_type, data.analytics_capable)
    rec.night_start = data.night_start
    rec.night_end = data.night_end
    rec.enabled = data.enabled
    await session.commit()
    return {"ok": True}


@router.post("/api/checkin/recorders/{rec_id}/delete")
async def delete_recorder(rec_id: int, session: AsyncSession = Depends(get_session)):
    await session.execute(delete(CheckinRecorder).where(CheckinRecorder.id == rec_id))
    await session.commit()
    return {"ok": True}


@router.post("/api/checkin/test-recorder")
async def test_recorder_endpoint(data: schemas.RecorderTestIn, session: AsyncSession = Depends(get_session)):
    """Пинг ISAPI + логин + список каналов и track-id субпотоков (для дропдаунов)."""
    if data.recorder_id is not None:
        rec = await session.get(CheckinRecorder, data.recorder_id)
        if rec is None:
            raise HTTPException(404, "Регистратор не найден")
        # пароль берётся из БД (password=None → decrypt внутри)
        return await checkin_ingest.test_recorder(rec, password=None)
    # тест до сохранения — по явным реквизитам
    if not data.host:
        raise HTTPException(422, "Укажите host/IP")
    tmp = CheckinRecorder(
        hotel_id=0, host=data.host.strip(), http_port=data.http_port,
        username=data.username.strip(), password_enc="",
    )
    return await checkin_ingest.test_recorder(tmp, password=data.password)


@router.get("/api/checkin/recorders/{rec_id}/snapshot/{channel_id}")
async def recorder_snapshot(rec_id: int, channel_id: int, session: AsyncSession = Depends(get_session)):
    """Кадр канала (JPEG) — чтобы опознавать вход/ресепшн визуально, а не по имени."""
    rec = await session.get(CheckinRecorder, rec_id)
    if rec is None:
        raise HTTPException(404, "Регистратор не найден")
    from app.drivers.base import NVRError

    client = checkin_ingest.build_recorder_client(rec)
    try:
        jpeg = await client.get_snapshot(channel_id)
    except NVRError:
        return Response(status_code=204)  # нет кадра — превью просто не покажется
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ── Каналы ───────────────────────────────────────────────────────────────────
def _validate_channel(data: schemas.CheckinChannelIn) -> None:
    if data.role not in CHANNEL_ROLE_NAMES:
        raise HTTPException(422, "Роль канала: вход или ресепшн")


@router.post("/api/checkin/channels")
async def create_channel(data: schemas.CheckinChannelIn, session: AsyncSession = Depends(get_session)):
    _validate_channel(data)
    if await session.get(CheckinRecorder, data.recorder_id) is None:
        raise HTTPException(404, "Регистратор не найден")
    # авто-trackid субпотока, если не задан: канал*100+2 (напр. кан.1 → 102)
    trackid = data.substream_trackid or (data.channel_id * 100 + 2)
    ch = CheckinChannel(
        recorder_id=data.recorder_id, channel_id=data.channel_id, name=data.name,
        role=data.role, substream_trackid=trackid, enabled=data.enabled,
    )
    session.add(ch)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, f"Канал {data.channel_id} уже добавлен для этого регистратора")
    return {"id": ch.id}


@router.post("/api/checkin/channels/{ch_id}")
async def update_channel(ch_id: int, data: schemas.CheckinChannelIn, session: AsyncSession = Depends(get_session)):
    _validate_channel(data)
    ch = await session.get(CheckinChannel, ch_id)
    if ch is None:
        raise HTTPException(404, "Канал не найден")
    ch.channel_id = data.channel_id
    ch.name = data.name
    ch.role = data.role
    ch.substream_trackid = data.substream_trackid
    ch.enabled = data.enabled
    await session.commit()
    return {"ok": True}


@router.post("/api/checkin/channels/{ch_id}/delete")
async def delete_channel(ch_id: int, session: AsyncSession = Depends(get_session)):
    await session.execute(delete(CheckinChannel).where(CheckinChannel.id == ch_id))
    await session.commit()
    return {"ok": True}


# ── Расписание ночного джоба ─────────────────────────────────────────────────
@router.post("/api/checkin/schedule")
async def set_schedule(data: schemas.CheckinScheduleIn, session: AsyncSession = Depends(get_session)):
    await appsettings.set_value(session, "checkin_job_hour", data.hour)
    await appsettings.set_value(session, "checkin_job_minute", data.minute)
    reschedule_checkin_job(data.hour, data.minute)  # применяется без рестарта
    return {"ok": True, "hour": data.hour, "minute": data.minute}


# ── Запуск ingestion вручную ─────────────────────────────────────────────────
@router.post("/api/checkin/ingest/run")
async def run_ingest_now(
    data: schemas.IngestRunIn, background: BackgroundTasks, session: AsyncSession = Depends(get_session)
):
    # Не запускаем второй прогон поверх активного
    active = (
        await session.execute(
            select(CheckinIngestRun).where(CheckinIngestRun.status == "running").limit(1)
        )
    ).scalar_one_or_none()
    if active is not None:
        return {"ok": False, "running": True, "message": "Ingestion уже выполняется"}
    day = data.day or (dt.date.today() - dt.timedelta(days=1))
    hotel_ids = [data.hotel_id] if data.hotel_id else None
    background.add_task(checkin_ingest.run_ingestion, day, hotel_ids, "manual")
    return {"ok": True, "day": day.isoformat(), "message": "Ingestion запущен в фоне"}


@router.get("/api/checkin/ingest/status")
async def ingest_status(session: AsyncSession = Depends(get_session)):
    """Состояние последнего прогона ingestion — для прогресс-бара в UI."""
    run = (
        await session.execute(
            select(CheckinIngestRun).order_by(CheckinIngestRun.started_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return {"exists": False}
    def _aware(t):
        return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t

    started = _aware(run.started_at)
    end = _aware(run.finished_at) if run.finished_at else dt.datetime.now(dt.timezone.utc)
    elapsed = max(int((end - started).total_seconds()), 0)
    pct = round(run.recorders_done / run.recorders_total * 100) if run.recorders_total else (
        100 if run.status != "running" else 0
    )
    return {
        "exists": True, "id": run.id, "status": run.status, "trigger": run.trigger,
        "day": run.day.isoformat() if run.day else None,
        "recorders_total": run.recorders_total, "recorders_done": run.recorders_done,
        "downloaded": run.downloaded, "skipped": run.skipped, "errors": run.errors,
        "current": run.current, "error": run.error, "detail": run.detail or [],
        "percent": pct, "elapsed": elapsed,
        "started_at": started.isoformat(), "finished": run.finished_at is not None,
    }


# ── Центр уведомлений ────────────────────────────────────────────────────────
@router.post("/api/checkin/notifications/{note_id}/status")
async def set_notification_status(
    note_id: int, data: schemas.NotificationStatusIn, session: AsyncSession = Depends(get_session)
):
    if data.status not in (NotificationStatus.NEW, NotificationStatus.SEEN, NotificationStatus.RESOLVED):
        raise HTTPException(422, "Неизвестный статус")
    note = await session.get(CheckinNotification, note_id)
    if note is None:
        raise HTTPException(404, "Уведомление не найдено")
    note.status = data.status
    await session.commit()
    return {"ok": True}


# ── Лог заселений (Фаза 2) ───────────────────────────────────────────────────
_VERDICTS = (CheckinVerdict.CHECKIN, CheckinVerdict.NOT, CheckinVerdict.DISPUTED)


@router.post("/api/checkin/logs")
async def create_log(
    data: schemas.CheckinLogIn, request: Request, session: AsyncSession = Depends(get_session)
):
    if data.verdict not in _VERDICTS:
        raise HTTPException(422, "Неизвестный вердикт")
    hotel_id, day = data.hotel_id, data.day
    if data.clip_id:
        clip = await session.get(CheckinClip, data.clip_id)
        if clip is None:
            raise HTTPException(404, "Клип не найден")
        hotel_id = clip.hotel_id
        day = day or clip.day
    if hotel_id is None:
        raise HTTPException(422, "Не указана гостиница")
    if day is None:
        day = data.event_time.date() if data.event_time else dt.date.today()
    entry = CheckinLog(
        clip_id=data.clip_id, hotel_id=hotel_id, day=day, shift=(data.shift or None),
        room=(data.room or None), event_time=data.event_time, verdict=data.verdict,
        note=(data.note or None), operator=request.session.get("user"),
    )
    session.add(entry)
    await session.commit()
    return {"id": entry.id}


@router.post("/api/checkin/logs/{log_id}/delete")
async def delete_log(log_id: int, session: AsyncSession = Depends(get_session)):
    await session.execute(delete(CheckinLog).where(CheckinLog.id == log_id))
    await session.commit()
    return {"ok": True}


@router.get("/checkin/logs", response_class=HTMLResponse)
async def checkin_logs_page(
    request: Request, hotel: int | None = None, day: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    hotels = (await session.execute(select(CheckinHotel).order_by(CheckinHotel.name))).scalars().all()
    q = select(CheckinLog).order_by(CheckinLog.created_at.desc())
    if hotel:
        q = q.where(CheckinLog.hotel_id == hotel)
    if day:
        try:
            q = q.where(CheckinLog.day == dt.date.fromisoformat(day))
        except ValueError:
            pass
    logs = (await session.execute(q.limit(500))).scalars().all()
    hotel_names = {h.id: h.name for h in hotels}
    counts = {v: sum(1 for x in logs if x.verdict == v) for v in _VERDICTS}
    rows = [
        {
            "id": x.id, "hotel": hotel_names.get(x.hotel_id, x.hotel_id),
            "day": x.day.isoformat() if x.day else "", "shift": x.shift or "—",
            "room": x.room or "—", "verdict": x.verdict,
            "event_time": x.event_time, "note": x.note or "",
            "operator": x.operator or "—", "created_at": x.created_at,
        }
        for x in logs
    ]
    return templates.TemplateResponse("checkin_logs.html", {
        "request": request, "logs": rows, "hotels": hotels,
        "f_hotel": hotel, "f_day": day or "", "counts": counts,
    })
