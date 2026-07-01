"""Модуль «Заселения» — ночной ingestion отфильтрованных клипов субпотока.

Идея: удалённый просмотр архива тормозит (тяжёлый main-stream через тонкий
аплоад гостиниц). Поэтому ночью для каждого регистратора находим отрезки с
активностью (аналитика «человек» для H332, motion для E2, иначе — всё окно) и
качаем ТОЛЬКО их лёгким субпотоком через ffmpeg -c copy на локальный сервер.
Оператор потом смотрит локальные файлы на x16 без тормозов.

Все параметры берутся из БД (гостиницы/регистраторы/каналы, время джоба) —
никаких настроек в файлах. Джоб перечитывает БД перед каждым запуском.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import SessionLocal
from app.drivers.base import ArchiveSegment, FeatureUnavailable, NVRError
from app.drivers.hikvision import HikvisionClient
from app.models import (
    CheckinChannel,
    CheckinClip,
    CheckinHotel,
    CheckinIngestRun,
    CheckinRecorder,
    ClipStatus,
    IngestRunStatus,
    utcnow,
)

log = logging.getLogger("nvrmon.checkin")

_DOWNLOAD_RETRIES = 2


# ── Клиент регистратора ──────────────────────────────────────────────────────
def build_recorder_client(recorder: CheckinRecorder, *, password: str | None = None) -> HikvisionClient:
    """Строит ISAPI-клиент для регистратора (Hikvision/HiWatch — один протокол).

    Пароль берётся расшифрованным из password_enc либо передаётся явно (для теста
    подключения до сохранения).
    """
    from app.crypto import decrypt

    pwd = password if password is not None else decrypt(recorder.password_enc)
    return HikvisionClient(
        host=recorder.host,
        port=recorder.http_port,
        username=recorder.username,
        password=pwd,
        timeout=settings.default_http_timeout,
        retries=settings.default_http_retries,
    )


# ── Прогресс прогона (для UI) ────────────────────────────────────────────────
async def _patch_run(
    run_id: int | None, *, rec_id: int | None = None, current: str | None = None,
    sets: dict | None = None, incs: dict | None = None,
    rec_sets: dict | None = None, rec_incs: dict | None = None, error_sample: str | None = None,
) -> None:
    """Атомарно обновляет запись прогона: агрегаты и/или блок конкретного регистратора."""
    if run_id is None:
        return
    async with SessionLocal() as s:
        run = await s.get(CheckinIngestRun, run_id)
        if run is None:
            return
        if current is not None:
            run.current = current
        for k, val in (sets or {}).items():
            setattr(run, k, val)
        for k, val in (incs or {}).items():
            setattr(run, k, (getattr(run, k) or 0) + val)
        if rec_id is not None:
            detail = list(run.detail or [])
            for e in detail:
                if e.get("recorder_id") == rec_id:
                    e.update(rec_sets or {})
                    for k, val in (rec_incs or {}).items():
                        e[k] = e.get(k, 0) + val
                    if error_sample:
                        e.setdefault("error_samples", [])
                        if len(e["error_samples"]) < 8:
                            e["error_samples"].append(error_sample)
                    break
            run.detail = detail
            flag_modified(run, "detail")  # JSON не отслеживает in-place мутации
        await s.commit()


# ── Ночное окно ──────────────────────────────────────────────────────────────
def _parse_hhmm(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = value.strip().split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        return fallback


def night_window(recorder: CheckinRecorder, day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Границы ночного окна регистратора для указанной даты (локальное время).

    24:00 в конце означает конец суток (00:00 следующего дня).
    """
    sh, sm = _parse_hhmm(recorder.night_start, (7, 0))
    eh, em = _parse_hhmm(recorder.night_end, (24, 0))
    start = dt.datetime.combine(day, dt.time(sh, sm))
    if eh >= 24:
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(eh - 24, em))
    else:
        end = dt.datetime.combine(day, dt.time(eh, em))
    if end <= start:
        end = start + dt.timedelta(days=1)
    return start, end


# ── Обработка сегментов ──────────────────────────────────────────────────────
def merge_segments(
    segments: list[ArchiveSegment], win_start: dt.datetime, win_end: dt.datetime
) -> list[ArchiveSegment]:
    """Сортирует, добавляет пре/пост-ролл, склеивает близкие, обрезает по окну."""
    pad = dt.timedelta(seconds=settings.checkin_segment_padding_sec)
    gap = dt.timedelta(seconds=settings.checkin_merge_gap_sec)
    norm: list[ArchiveSegment] = []
    for s in sorted(segments, key=lambda x: x.start):
        st = max(s.start - pad, win_start)
        en = min(s.end + pad, win_end)
        if en <= st:
            continue
        if norm and st - norm[-1].end <= gap:
            if en > norm[-1].end:
                norm[-1] = ArchiveSegment(norm[-1].start, en)
        else:
            norm.append(ArchiveSegment(st, en))
    return norm[: settings.checkin_max_segments]


async def find_activity(
    client: HikvisionClient, recorder: CheckinRecorder, channel_id: int,
    win_start: dt.datetime, win_end: dt.datetime,
) -> list[ArchiveSegment]:
    """Ищет отрезки активности с фолбэк-цепочкой: человек → motion → всё окно."""
    modes = ["human", "motion"] if recorder.analytics_capable else ["motion"]
    for mode in modes:
        try:
            segs = await client.search_activity(channel_id, win_start, win_end, mode=mode)
            log.info(
                "рег.%s кан.%s: поиск '%s' → %d сегм.",
                recorder.id, channel_id, mode, len(segs),
            )
            return merge_segments(segs, win_start, win_end)
        except (FeatureUnavailable, NVRError) as exc:
            log.info("рег.%s кан.%s: '%s' недоступен (%s) → фолбэк", recorder.id, channel_id, mode, exc)
    # Фолбэк из ТЗ: тянем всё окно целиком (субпоток лёгкий)
    log.info("рег.%s кан.%s: тянем всё окно целиком", recorder.id, channel_id)
    return [ArchiveSegment(win_start, win_end)]


# ── Скачивание одного клипа через ffmpeg ─────────────────────────────────────
async def _run_ffmpeg(url: str, out_path: str, duration_s: float) -> tuple[bool, str]:
    """ffmpeg -c copy (без перекодирования). Возвращает (успех, текст ошибки)."""
    cmd = [
        settings.ffmpeg_bin, "-y", "-nostdin",
        "-rtsp_transport", "tcp",
        "-i", url,
        "-t", f"{max(duration_s, 1):.0f}",
        "-c", "copy",
        "-map", "0",
        out_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "ffmpeg не найден (добавьте его в образ)"
    timeout = duration_s * 2 + 90
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False, f"таймаут ffmpeg ({timeout:.0f}с)"
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        return False, "ffmpeg: " + " | ".join(tail)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return False, "файл пустой/не создан"
    return True, ""


def clip_path(recorder: CheckinRecorder, hotel_id: int, channel_id: int, seg: ArchiveSegment) -> str:
    day = seg.start.strftime("%Y-%m-%d")
    fname = f"{seg.start.strftime('%H%M%S')}-{seg.end.strftime('%H%M%S')}.mp4"
    return os.path.join(settings.clips_dir, str(hotel_id), day, str(channel_id), fname)


async def _ingest_segment(
    session: AsyncSession, client: HikvisionClient, recorder: CheckinRecorder,
    hotel_id: int, ch: CheckinChannel, seg: ArchiveSegment,
) -> str:
    """Идемпотентно качает один сегмент. Возвращает статус: ok|skip|error."""
    existing = (
        await session.execute(
            select(CheckinClip).where(
                CheckinClip.recorder_id == recorder.id,
                CheckinClip.channel_id == ch.channel_id,
                CheckinClip.start_ts == seg.start,
                CheckinClip.end_ts == seg.end,
            )
        )
    ).scalar_one_or_none()
    if existing and existing.status == ClipStatus.OK and existing.path and os.path.exists(existing.path):
        return "skip", ""  # уже скачан — не качаем повторно

    out_path = clip_path(recorder, hotel_id, ch.channel_id, seg)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    trackid = ch.substream_trackid or (ch.channel_id * 100 + 2)
    url = client.rtsp_playback_url(trackid, seg.start, seg.end, rtsp_port=recorder.rtsp_port)
    duration = (seg.end - seg.start).total_seconds()

    clip = existing or CheckinClip(
        hotel_id=hotel_id, recorder_id=recorder.id, channel_id=ch.channel_id, role=ch.role,
        day=seg.start.date(), start_ts=seg.start, end_ts=seg.end, path=out_path,
    )
    clip.path = out_path
    clip.status = ClipStatus.PENDING
    clip.error = None
    if existing is None:
        session.add(clip)
    await session.commit()

    ok, err = False, "не начато"
    for attempt in range(_DOWNLOAD_RETRIES + 1):
        ok, err = await _run_ffmpeg(url, out_path, duration)
        if ok:
            break
        await asyncio.sleep(1.0 * (attempt + 1))

    if ok:
        clip.status = ClipStatus.OK
        clip.size_bytes = os.path.getsize(out_path)
        clip.error = None
    else:
        clip.status = ClipStatus.ERROR
        clip.error = err
        log.warning("рег.%s кан.%s %s: %s", recorder.id, ch.channel_id,
                    seg.start.strftime("%H:%M:%S"), err)
    await session.commit()
    return ("ok", "") if ok else ("error", err)


# ── Прогон по регистратору / всем гостиницам ─────────────────────────────────
async def ingest_recorder(day: dt.date, recorder_id: int, run_id: int | None = None) -> dict:
    """Скачивает клипы одного регистратора за указанную дату. Идемпотентно.

    Если задан run_id — по ходу обновляет прогресс прогона для UI.
    """
    async with SessionLocal() as session:
        recorder = (
            await session.execute(
                select(CheckinRecorder)
                .where(CheckinRecorder.id == recorder_id)
                .options(selectinload(CheckinRecorder.channels))
            )
        ).scalar_one_or_none()
        if recorder is None or not recorder.enabled:
            await _patch_run(run_id, rec_id=recorder_id, rec_sets={"status": "skipped"})
            return {"recorder_id": recorder_id, "skipped": True}

        label = recorder.name or recorder.host
        stats = {"recorder_id": recorder_id, "downloaded": 0, "skipped": 0, "errors": 0}
        channels = [c for c in recorder.channels if c.enabled]
        await _patch_run(run_id, rec_id=recorder_id,
                         rec_sets={"status": "running", "channels_total": len(channels)},
                         current=f"{label}: подключаюсь…")
        try:
            client = build_recorder_client(recorder)
            win_start, win_end = night_window(recorder, day)
            for idx, ch in enumerate(channels, 1):
                await _patch_run(run_id, current=f"{label}: канал {ch.channel_id} ({idx}/{len(channels)}), скачано {stats['downloaded']}")
                try:
                    segments = await find_activity(client, recorder, ch.channel_id, win_start, win_end)
                except NVRError as exc:
                    stats["errors"] += 1
                    await _patch_run(run_id, incs={"errors": 1}, rec_id=recorder_id,
                                     rec_incs={"errors": 1}, error_sample=f"кан.{ch.channel_id}: поиск: {exc}")
                    log.warning("рег.%s кан.%s: поиск сорвался: %s", recorder.id, ch.channel_id, exc)
                    await _patch_run(run_id, rec_id=recorder_id, rec_incs={"channels_done": 1})
                    continue
                for seg in segments:
                    res, err = await _ingest_segment(session, client, recorder, recorder.hotel_id, ch, seg)
                    if res == "ok":
                        stats["downloaded"] += 1
                        await _patch_run(run_id, incs={"downloaded": 1}, rec_id=recorder_id, rec_incs={"downloaded": 1})
                    elif res == "skip":
                        stats["skipped"] += 1
                        await _patch_run(run_id, incs={"skipped": 1}, rec_id=recorder_id, rec_incs={"skipped": 1})
                    else:
                        stats["errors"] += 1
                        await _patch_run(run_id, incs={"errors": 1}, rec_id=recorder_id,
                                         rec_incs={"errors": 1},
                                         error_sample=f"кан.{ch.channel_id} {seg.start.strftime('%H:%M:%S')}: {err}")
                await _patch_run(run_id, rec_id=recorder_id, rec_incs={"channels_done": 1})
        except NVRError as exc:
            stats["errors"] += 1
            await _patch_run(run_id, incs={"errors": 1}, rec_id=recorder_id,
                             rec_sets={"status": "error"}, error_sample=f"регистратор недоступен: {exc}")
            log.error("рег.%s: недоступен: %s", recorder.id, exc)
        else:
            await _patch_run(run_id, rec_id=recorder_id,
                             rec_sets={"status": "error" if stats["errors"] else "done"})
        log.info(
            "рег.%s за %s: скачано=%d, пропущено=%d, ошибок=%d",
            recorder_id, day, stats["downloaded"], stats["skipped"], stats["errors"],
        )
        return stats


async def run_ingestion(
    day: dt.date | None = None, hotel_ids: list[int] | None = None, trigger: str = "manual"
) -> dict:
    """Ночной джоб: качает клипы по всем включённым гостиницам за дату.

    day по умолчанию — вчерашний день (ночью выкачиваем прошедшие сутки).
    Создаёт запись прогона (CheckinIngestRun) и обновляет её по ходу — для UI.
    """
    if day is None:
        day = dt.date.today() - dt.timedelta(days=1)
    async with SessionLocal() as session:
        q = (
            select(CheckinRecorder).join(CheckinHotel)
            .where(CheckinRecorder.enabled.is_(True), CheckinHotel.enabled.is_(True))
            .options(selectinload(CheckinRecorder.hotel))
        )
        if hotel_ids:
            q = q.where(CheckinRecorder.hotel_id.in_(hotel_ids))
        recorders = (await session.execute(q)).scalars().all()
        rec_ids = [r.id for r in recorders]
        detail = [{
            "recorder_id": r.id, "name": r.name or r.host,
            "hotel": r.hotel.name if r.hotel else "", "status": "queued",
            "channels_total": 0, "channels_done": 0,
            "downloaded": 0, "skipped": 0, "errors": 0, "error_samples": [],
        } for r in recorders]
        run = CheckinIngestRun(
            day=day, trigger=trigger, status=IngestRunStatus.RUNNING,
            recorders_total=len(rec_ids), detail=detail,
            current="Старт…" if rec_ids else "Нет включённых регистраторов",
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    log.info("Ingestion за %s: регистраторов=%d (run %s)", day, len(rec_ids), run_id)
    summary = {"run_id": run_id, "day": day.isoformat(), "recorders": [], "downloaded": 0, "errors": 0}
    try:
        # Последовательно по регистраторам — бережём тонкий аплоад гостиниц.
        for rid in rec_ids:
            st = await ingest_recorder(day, rid, run_id)
            summary["recorders"].append(st)
            summary["downloaded"] += st.get("downloaded", 0)
            summary["errors"] += st.get("errors", 0)
            await _patch_run(run_id, incs={"recorders_done": 1})
        await _patch_run(run_id, sets={
            "status": IngestRunStatus.DONE, "finished_at": utcnow(), "current": "Готово",
        })
    except Exception as exc:  # noqa: BLE001
        await _patch_run(run_id, sets={
            "status": IngestRunStatus.ERROR, "finished_at": utcnow(),
            "error": str(exc), "current": "Сбой",
        })
        log.exception("Ingestion run %s упал: %s", run_id, exc)
        raise
    return summary


async def scheduled_ingestion() -> dict:
    """Обёртка для планировщика — помечает прогон как автоматический."""
    return await run_ingestion(trigger="schedule")


# ── Тест подключения (для UI: пинг + список каналов и треков субпотока) ───────
async def test_recorder(recorder: CheckinRecorder, *, password: str | None = None) -> dict:
    """Проверяет подключение и вытягивает каналы + track-id субпотоков.

    Возвращает {ok, message, info, channels:[...], tracks:[...]} для выпадающих
    списков в UI (чтобы каналы/субпоток выбирались, а не вводились руками).
    """
    client = build_recorder_client(recorder, password=password)
    result: dict = {"ok": False, "message": "", "info": None, "channels": [], "tracks": []}
    try:
        info = await client.test_connection()
        result["info"] = {"model": info.model, "firmware": info.firmware, "serial": info.serial}
    except NVRError as exc:
        result["message"] = f"Не удалось подключиться: {exc}"
        return result

    try:
        for st in await client.get_channel_statuses():
            result["channels"].append({
                "channel_id": st.channel_id, "name": st.name or f"канал {st.channel_id}",
                "online": st.online,
            })
    except NVRError as exc:
        log.info("test_recorder: каналы недоступны: %s", exc)

    try:
        result["tracks"] = await client.list_tracks()
    except NVRError as exc:
        log.info("test_recorder: треки недоступны: %s", exc)

    result["ok"] = True
    model = (result["info"] or {}).get("model") or "?"
    result["message"] = f"OK: {model}, каналов {len(result['channels'])}, треков {len(result['tracks'])}"
    return result
