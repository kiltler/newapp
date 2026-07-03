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
import re

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
_STALL_SECONDS = 40  # если файл не растёт столько секунд — считаем поток мёртвым и убиваем ffmpeg
_CANCEL_MSG = "отменено пользователем"


def _try_next_track(err: str) -> bool:
    """Стоит ли пробовать другой trackid: трек не найден (404) или поток не отдаёт данные."""
    e = (err or "").lower()
    return "404" in e or "not found" in e or "нет данных" in e or "таймаут" in e


# Прогоны, помеченные на отмену (run_id). Проверяется между сегментами и внутри
# скачивания (kill ffmpeg).
_CANCELLED: set[int] = set()


def request_cancel(run_id: int) -> None:
    _CANCELLED.add(run_id)


def is_cancelled(run_id: int | None) -> bool:
    return run_id is not None and run_id in _CANCELLED


def _clear_cancel(run_id: int | None) -> None:
    _CANCELLED.discard(run_id)


async def _ffmpeg_download(
    url: str, out_path: str, duration_s: float,
    run_id: int | None = None, label: str = "", seg: ArchiveSegment | None = None,
) -> tuple[bool, str]:
    """Качает клип через ffmpeg: видео copy, аудио → AAC. Во время работы раз в 2с
    обновляет прогресс размером файла (видно, что идёт) и слушает отмену (kill)."""
    cmd = [
        settings.ffmpeg_bin, "-y", "-nostdin",
        "-rtsp_transport", "tcp",
        "-i", url,
        "-t", f"{max(duration_s, 1):.0f}",
        # Видео копируем без перекодирования (легко/быстро). Аудио регистраторов
        # часто в pcm_alaw/g711, который нельзя положить в MP4 при -c copy —
        # поэтому звук перекодируем в AAC (нужен, чтобы слышать разговор на ресепшене).
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c:v", "copy",
        "-c:a", "aac",
        out_path,
    ]
    log.info("ffmpeg ← %s", _mask(url))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "ffmpeg не найден (добавьте его в образ)"

    span = f"{seg.start.strftime('%H:%M:%S')}–{seg.end.strftime('%H:%M:%S')}" if seg else ""
    comm = asyncio.create_task(proc.communicate())
    max_seconds = duration_s * 2 + 120
    waited, last_size, stalled = 0.0, -1, 0.0

    async def _kill() -> None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(comm, timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass

    while True:
        done, _ = await asyncio.wait({comm}, timeout=2.0)
        if comm in done:
            break
        waited += 2.0
        if is_cancelled(run_id):
            await _kill()
            return False, _CANCEL_MSG
        try:
            sz = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        except OSError:
            sz = 0
        if sz > last_size:
            last_size, stalled = sz, 0.0
        else:
            stalled += 2.0
        if stalled >= _STALL_SECONDS:
            await _kill()
            return False, (f"нет данных от RTSP {int(_STALL_SECONDS)}с — проверьте trackid "
                           "субпотока, тип потока и сеть (порт 554)")
        if waited > max_seconds:
            await _kill()
            return False, f"таймаут ffmpeg ({max_seconds:.0f}с)"
        msg = (f"{label}: подключение {span}, ожидание данных…" if sz <= 0
               else f"{label}: качается {span} — {sz / 1048576:.1f} МБ")
        await _patch_run(run_id, current=msg)

    _, stderr = comm.result()
    if is_cancelled(run_id):
        return False, _CANCEL_MSG
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        return False, "ffmpeg: " + " | ".join(tail)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return False, "файл пустой/не создан"
    return True, ""


def _rewrite_uri_window(uri: str, start: dt.datetime, end: dt.datetime) -> str:
    """Подменяет только starttime/endtime в playbackURI устройства, СОХРАНЯЯ name/size.

    Важно: у HiWatch-DVR путь трека одинаков для sub и main (tracks/6001),
    а различаются они параметром name (идентификатор физического файла записи).
    Поэтому name трогать нельзя — иначе устройство отдаёт основной поток."""
    st = start.strftime("%Y%m%dT%H%M%SZ")
    en = end.strftime("%Y%m%dT%H%M%SZ")
    if "starttime=" in uri:
        uri = re.sub(r"starttime=[^&]*", "starttime=" + st, uri)
    if "endtime=" in uri:
        uri = re.sub(r"endtime=[^&]*", "endtime=" + en, uri)
    return uri


def _mask(url: str) -> str:
    """Прячет пароль в rtsp://user:pass@host для логов."""
    return re.sub(r"(rtsp://[^:/@]+:)[^@]*@", r"\1***@", url or "")


def _closed_segment(segs: list[dict], min_age: dt.timedelta = dt.timedelta(minutes=15)) -> dict | None:
    """Выбирает ПОСЛЕДНИЙ уже закрытый сегмент (конец которого не свежее min_age
    от самого нового куска). Hikvision не отдаёт playback открытого (пишущегося)
    файла — поэтому качать надо из закрытого."""
    if not segs:
        return None
    ref = max(s["end"] for s in segs)  # ~ «сейчас» по времени устройства
    closed = [s for s in segs if s["end"] <= ref - min_age]
    if closed:
        return max(closed, key=lambda s: s["end"])
    return min(segs, key=lambda s: s["start"])  # запасной: самый старый


async def _test_clip_jobs(client, ch, search_start, search_end, minutes) -> list[tuple]:
    """Для тест-клипа: спрашиваем у устройства записанные сегменты субпотока и
    берём последние N минут ЗАКРЫТОГО сегмента. Возвращает [(seg, win_start, win_end)],
    где seg — полный сегмент устройства (его качаем по HTTP, потом режем ffmpeg'ом)."""
    sub = main = 0
    try:
        segs = await client.search_playback(ch.channel_id, search_start, search_end, substream=True)
        sub = len(segs)
        if not segs:
            segs = await client.search_playback(ch.channel_id, search_start, search_end, substream=False)
            main = len(segs)
    except NVRError as exc:
        log.warning("кан.%s: playback-поиск недоступен: %s", ch.channel_id, exc)
        return []
    log.info("тест-клип кан.%s: сегментов субпотока=%d, основного=%d", ch.channel_id, sub, main)
    seg = _closed_segment(segs)
    if seg is None:
        return []
    win_end = seg["end"]
    win_start = max(win_end - dt.timedelta(minutes=minutes), seg["start"])
    log.info("тест-клип кан.%s: сегмент %s..%s, окно %s..%s", ch.channel_id,
             seg["start"].strftime("%H:%M:%S"), seg["end"].strftime("%H:%M:%S"),
             win_start.strftime("%H:%M:%S"), win_end.strftime("%H:%M:%S"))
    return [(seg, win_start, win_end)]


async def _run_ffmpeg_trim(src, dst, offset_s, duration_s, reencode: bool) -> tuple[bool, str]:
    vcodec = (["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
              if reencode else ["-c:v", "copy"])
    cmd = [
        settings.ffmpeg_bin, "-y", "-nostdin", "-fflags", "+genpts",
        "-ss", f"{max(offset_s, 0):.3f}", "-i", src, "-t", f"{max(duration_s, 1):.0f}",
        "-map", "0:v:0?", "-map", "0:a:0?", *vcodec, "-c:a", "aac",
        "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", dst,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return False, "ffmpeg не найден"
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "таймаут обрезки"
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()[-2:]
        return False, "ffmpeg: " + " | ".join(tail)
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        return False, "пустой результат обрезки"
    return True, ""


async def _has_playable_duration(path: str) -> bool:
    """Проверяет через ffprobe, что у файла есть валидная длительность (иначе
    браузер покажет 0:00 и не сыграет)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nk=1:nw=1", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        return float((out or b"").decode().strip() or 0) > 0.1
    except Exception:  # noqa: BLE001
        return False


async def _window_jobs(client, ch, win_start: dt.datetime, win_end: dt.datetime) -> list[tuple]:
    """Сегменты записи, попадающие в окно [win_start, win_end] → [(seg, cs, ce)].

    Поиск делаем ШИРОКИМ (интерпретация времени запроса устройством ненадёжна из-за
    часовых поясов), а фильтруем по времени самих сегментов — они приходят в
    локальном времени регистратора, том же, что и win_start/win_end. «Живой край»
    (последние 15 мин) исключаем.
    """
    lo, hi = win_start - dt.timedelta(hours=36), win_end + dt.timedelta(hours=36)
    try:
        segs = await client.search_playback(ch.channel_id, lo, hi, substream=True)
        if not segs:
            segs = await client.search_playback(ch.channel_id, lo, hi, substream=False)
    except NVRError as exc:
        log.warning("кан.%s: playback-поиск недоступен: %s", ch.channel_id, exc)
        return []
    if not segs:
        return []
    ref = max(s["end"] for s in segs)
    safe_end = min(win_end, ref - dt.timedelta(minutes=15))  # не трогаем пишущийся файл
    jobs = []
    for seg in sorted(segs, key=lambda s: s["start"]):
        cs = max(seg["start"], win_start)
        ce = min(seg["end"], safe_end)
        if (ce - cs).total_seconds() >= 10:
            jobs.append((seg, cs, ce))
    log.info("кан.%s: окно %s..%s → %d клип(ов) из %d сегментов",
             ch.channel_id, win_start.strftime("%d.%m %H:%M"), win_end.strftime("%d.%m %H:%M"),
             len(jobs), len(segs))
    return jobs[: settings.checkin_max_segments]


async def _ffmpeg_trim(src: str, dst: str, offset_s: float, duration_s: float) -> tuple[bool, str]:
    """Вырезает окно [offset, offset+duration] в играбельный MP4.

    Сначала быстрый путь (-c:v copy + фикс тайминга). Если результат
    неиграбельный (0:00 — у Hikvision кривой тайминг), перекодируем в H.264.
    """
    ok, err = await _run_ffmpeg_trim(src, dst, offset_s, duration_s, reencode=False)
    if ok and await _has_playable_duration(dst):
        return True, ""
    ok, err = await _run_ffmpeg_trim(src, dst, offset_s, duration_s, reencode=True)
    return ok, err


async def _ingest_http_clip(
    session, client, recorder, hotel_id, ch, seg: dict,
    win_start: dt.datetime, win_end: dt.datetime, run_id: int | None, label: str,
) -> tuple[str, str]:
    """Скачивает сегмент по HTTP (ISAPI) и вырезает окно ffmpeg'ом. Идемпотентно."""
    existing = (
        await session.execute(
            select(CheckinClip).where(
                CheckinClip.recorder_id == recorder.id,
                CheckinClip.channel_id == ch.channel_id,
                CheckinClip.start_ts == win_start,
                CheckinClip.end_ts == win_end,
            )
        )
    ).scalar_one_or_none()
    if existing and existing.status == ClipStatus.OK and existing.path and os.path.exists(existing.path):
        return "skip", ""

    out_seg = ArchiveSegment(win_start, win_end)
    out_path = clip_path(recorder, hotel_id, ch.channel_id, out_seg)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".full"

    clip = existing or CheckinClip(
        hotel_id=hotel_id, recorder_id=recorder.id, channel_id=ch.channel_id, role=ch.role,
        day=win_start.date(), start_ts=win_start, end_ts=win_end, path=out_path,
    )
    clip.path, clip.status, clip.error = out_path, ClipStatus.PENDING, None
    if existing is None:
        session.add(clip)
    await session.commit()

    uri = seg.get("uri") or ""
    if not uri:
        clip.status, clip.error = ClipStatus.ERROR, "нет playbackURI сегмента"
        await session.commit()
        return "error", clip.error

    await _patch_run(run_id, current=f"{label}: качаю сегмент по HTTP…")
    ok, nbytes, err = await client.download_segment(uri, tmp, max_bytes=800 * 1024 * 1024)
    if not ok or nbytes == 0:
        clip.status, clip.error = ClipStatus.ERROR, f"HTTP-скачивание: {err}"
        await session.commit()
        _safe_remove(tmp)
        return "error", clip.error

    await _patch_run(run_id, current=f"{label}: обрезаю клип из {nbytes // 1048576} МБ…")
    offset = max((win_start - seg["start"]).total_seconds(), 0)
    duration = (win_end - win_start).total_seconds()
    ok2, err2 = await _ffmpeg_trim(tmp, out_path, offset, duration)
    _safe_remove(tmp)

    if ok2:
        clip.status, clip.size_bytes, clip.error = ClipStatus.OK, os.path.getsize(out_path), None
    else:
        clip.status, clip.error = ClipStatus.ERROR, f"обрезка: {err2}"
        log.warning("рег.%s кан.%s: обрезка: %s", recorder.id, ch.channel_id, err2)
    await session.commit()
    return ("ok", "") if ok2 else ("error", clip.error)


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# Папка для клипов: переопределяется из UI (AppSetting), иначе из env CLIPS_DIR.
_clips_dir_override: str | None = None


def active_clips_dir() -> str:
    return _clips_dir_override or settings.clips_dir


def set_clips_dir(path: str | None) -> None:
    global _clips_dir_override
    _clips_dir_override = (path.strip() or None) if path else None


async def load_clips_dir() -> None:
    """Загружает выбранную из UI папку клипов (AppSetting) при старте."""
    from app.models import AppSetting

    async with SessionLocal() as s:
        row = await s.get(AppSetting, "checkin_clips_dir")
    set_clips_dir(row.value if row and row.value else None)
    try:
        os.makedirs(active_clips_dir(), exist_ok=True)
    except OSError:
        pass
    log.info("Папка клипов: %s", active_clips_dir())


def clip_path(recorder: CheckinRecorder, hotel_id: int, channel_id: int, seg: ArchiveSegment) -> str:
    day = seg.start.strftime("%Y-%m-%d")
    fname = f"{seg.start.strftime('%H%M%S')}-{seg.end.strftime('%H%M%S')}.mp4"
    return os.path.join(active_clips_dir(), str(hotel_id), day, str(channel_id), fname)


async def _ingest_segment(
    session: AsyncSession, client: HikvisionClient, recorder: CheckinRecorder,
    hotel_id: int, ch: CheckinChannel, seg: ArchiveSegment,
    run_id: int | None = None, label: str = "", playback_uri: str | None = None,
) -> tuple[str, str]:
    """Идемпотентно качает один сегмент. Возвращает (статус, ошибка).

    playback_uri — готовый RTSP-URI от устройства (тогда без перебора trackid).
    """
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
    duration = (seg.end - seg.start).total_seconds()
    ch_num = ch.channel_id
    if playback_uri:
        # Готовый URI устройства — грузим по нему, без перебора trackid.
        url_candidates: list[tuple[int | None, str]] = [(None, playback_uri)]
    else:
        # Кандидаты trackid: заданный → субпоток (N*100+2) → основной (N*100+1).
        tids: list[int] = []
        for t in (ch.substream_trackid, ch_num * 100 + 2, ch_num * 100 + 1):
            if t and t not in tids:
                tids.append(t)
        url_candidates = [
            (t, client.rtsp_playback_url(t, seg.start, seg.end, rtsp_port=recorder.rtsp_port))
            for t in tids
        ]

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

    ok, err, used_track = False, "не начато", None
    for tid, url in url_candidates:
        for attempt in range(_DOWNLOAD_RETRIES + 1):
            ok, err = await _ffmpeg_download(url, out_path, duration, run_id, label, seg)
            if ok or _try_next_track(err) or err == _CANCEL_MSG:
                break
            await asyncio.sleep(1.0 * (attempt + 1))
        if err == _CANCEL_MSG:
            break  # при отмене не перебираем
        if ok:
            used_track = tid
            break
        if not _try_next_track(err):
            break  # ошибка не про трек/данные (auth и т.п.) — смена trackid не поможет

    if ok:
        clip.status = ClipStatus.OK
        clip.size_bytes = os.path.getsize(out_path)
        clip.error = None
        # авто-обучение trackid (только когда собирали URL сами)
        if used_track and ch.substream_trackid != used_track:
            log.info("рег.%s кан.%s: рабочий trackid субпотока = %s (сохранён)",
                     recorder.id, ch_num, used_track)
            ch.substream_trackid = used_track
    else:
        clip.status = ClipStatus.ERROR
        clip.error = err
        log.warning("рег.%s кан.%s %s: %s", recorder.id, ch.channel_id,
                    seg.start.strftime("%H:%M:%S"), err)
    await session.commit()
    return ("ok", "") if ok else ("error", err)


# ── Прогон по регистратору / всем гостиницам ─────────────────────────────────
async def ingest_recorder(
    day: dt.date, recorder_id: int, run_id: int | None = None, *,
    window: tuple[dt.datetime, dt.datetime] | None = None, whole: bool = False,
    test_minutes: int | None = None,
) -> dict:
    """Скачивает клипы одного регистратора за указанную дату. Идемпотентно.

    Если задан run_id — по ходу обновляет прогресс прогона для UI.
    window — явные границы (мимо ночного окна, для тест-клипа).
    whole=True — качать всё окно целиком, без поиска активности (для теста).
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
            use_whole = whole
            test_mode = test_minutes is not None
            search_start = search_end = None
            if test_mode:
                # Широкое окно поиска в UTC (±сутки) — ловит запись при любом TZ и
                # даже если часы NVR сбиты. Реальное окно клипа берём из времени
                # НАЙДЕННОГО сегмента, а качаем по родному playbackURI устройства.
                search_end = dt.datetime.utcnow() + dt.timedelta(hours=14)
                search_start = search_end - dt.timedelta(hours=54)
                await _patch_run(run_id, rec_id=recorder_id,
                                 current=f"{label}: ищу свежую запись в архиве…")
            elif use_whole:
                # День (За вчера/сегодня): окно в терминах ВРЕМЕНИ РЕГИСТРАТОРА,
                # иначе TZ-сдвиг сервера обрежет день до пары часов.
                win_start, win_end = night_window(recorder, day)
                try:
                    dev_now = await client.get_device_time()
                except Exception:  # noqa: BLE001  (NVRError или драйвер без метода)
                    dev_now = dt.datetime.now()
                if win_end > dev_now:  # текущий день ещё не закрыт
                    win_end = dev_now
                if win_end <= win_start:
                    await _patch_run(run_id, rec_id=recorder_id, rec_sets={"status": "done"},
                                     current=f"{label}: окно ещё не наступило")
                    return stats
                await _patch_run(run_id, rec_id=recorder_id,
                                 current=f"{label}: время регистратора {dev_now.strftime('%d.%m %H:%M')}, качаю день")
            elif window is not None:
                win_start, win_end = window
            else:
                win_start, win_end = night_window(recorder, day)
                now = dt.datetime.now()
                if win_end > now:
                    win_end = now
                if win_end <= win_start:
                    await _patch_run(run_id, rec_id=recorder_id, rec_sets={"status": "done"},
                                     current=f"{label}: окно ещё не наступило")
                    return stats
            for idx, ch in enumerate(channels, 1):
                if is_cancelled(run_id):
                    break
                await _patch_run(run_id, current=f"{label}: канал {ch.channel_id} ({idx}/{len(channels)}), скачано {stats['downloaded']}")
                if test_mode:
                    # HTTP-скачивание сегмента + обрезка ffmpeg (RTSP на DVR может не отдавать).
                    jobs = await _test_clip_jobs(client, ch, search_start, search_end, test_minutes)
                    if not jobs:
                        stats["errors"] += 1
                        await _patch_run(run_id, incs={"errors": 1}, rec_id=recorder_id,
                                         rec_incs={"errors": 1, "channels_done": 1},
                                         error_sample=f"кан.{ch.channel_id}: нет записи в архиве за последние сутки")
                        continue
                    for seg_dict, ws, we in jobs:
                        res, err = await _ingest_http_clip(
                            session, client, recorder, recorder.hotel_id, ch, seg_dict, ws, we, run_id, label)
                        if res == "ok":
                            stats["downloaded"] += 1
                            await _patch_run(run_id, incs={"downloaded": 1}, rec_id=recorder_id, rec_incs={"downloaded": 1})
                        elif res == "skip":
                            stats["skipped"] += 1
                            await _patch_run(run_id, incs={"skipped": 1}, rec_id=recorder_id, rec_incs={"skipped": 1})
                        else:
                            stats["errors"] += 1
                            await _patch_run(run_id, incs={"errors": 1}, rec_id=recorder_id,
                                             rec_incs={"errors": 1}, error_sample=f"кан.{ch.channel_id}: {err}")
                    await _patch_run(run_id, rec_id=recorder_id, rec_incs={"channels_done": 1})
                    continue
                if use_whole:
                    # За вчера/сегодня: HTTP-скачивание сегментов окна + обрезка.
                    hjobs = await _window_jobs(client, ch, win_start, win_end)
                    for seg_dict, cs, ce in hjobs:
                        res, err = await _ingest_http_clip(
                            session, client, recorder, recorder.hotel_id, ch, seg_dict, cs, ce, run_id, label)
                        if res == "ok":
                            stats["downloaded"] += 1
                            await _patch_run(run_id, incs={"downloaded": 1}, rec_id=recorder_id, rec_incs={"downloaded": 1})
                        elif res == "skip":
                            stats["skipped"] += 1
                            await _patch_run(run_id, incs={"skipped": 1}, rec_id=recorder_id, rec_incs={"skipped": 1})
                        else:
                            stats["errors"] += 1
                            await _patch_run(run_id, incs={"errors": 1}, rec_id=recorder_id,
                                             rec_incs={"errors": 1}, error_sample=f"кан.{ch.channel_id}: {err}")
                    await _patch_run(run_id, rec_id=recorder_id, rec_incs={"channels_done": 1})
                    continue
                try:
                    segs = await find_activity(client, recorder, ch.channel_id, win_start, win_end)
                except NVRError as exc:
                    stats["errors"] += 1
                    await _patch_run(run_id, incs={"errors": 1}, rec_id=recorder_id,
                                     rec_incs={"errors": 1, "channels_done": 1},
                                     error_sample=f"кан.{ch.channel_id}: поиск: {exc}")
                    log.warning("рег.%s кан.%s: поиск сорвался: %s", recorder.id, ch.channel_id, exc)
                    continue
                jobs = [(s, None) for s in segs]
                for seg, uri in jobs:
                    res, err = await _ingest_segment(
                        session, client, recorder, recorder.hotel_id, ch, seg,
                        run_id=run_id, label=label, playback_uri=uri)
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
    day: dt.date | None = None, hotel_ids: list[int] | None = None, trigger: str = "manual",
    *, window: tuple[dt.datetime, dt.datetime] | None = None, whole: bool = False,
    test_minutes: int | None = None,
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
            if is_cancelled(run_id):
                break
            st = await ingest_recorder(day, rid, run_id, window=window, whole=whole,
                                       test_minutes=test_minutes)
            summary["recorders"].append(st)
            summary["downloaded"] += st.get("downloaded", 0)
            summary["errors"] += st.get("errors", 0)
            await _patch_run(run_id, incs={"recorders_done": 1})
        if is_cancelled(run_id):
            await _patch_run(run_id, sets={
                "status": IngestRunStatus.CANCELED, "finished_at": utcnow(), "current": "Отменено",
            })
        else:
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
    finally:
        _clear_cancel(run_id)
    return summary


async def scheduled_ingestion() -> dict:
    """Обёртка для планировщика — помечает прогон как автоматический."""
    return await run_ingestion(trigger="schedule")


async def abort_orphan_runs() -> None:
    """На старте: прогоны и клипы не переживают перезапуск процесса. Помечаем
    зависшие running/pending как прерванные, чтобы UI не показывал вечное «идёт»."""
    async with SessionLocal() as s:
        runs = (
            await s.execute(
                select(CheckinIngestRun).where(CheckinIngestRun.status == IngestRunStatus.RUNNING)
            )
        ).scalars().all()
        for r in runs:
            r.status = IngestRunStatus.ERROR
            r.finished_at = utcnow()
            r.current = "прервано (перезапуск сервера)"
            r.error = r.error or "прервано перезапуском сервера"
        clips = (
            await s.execute(select(CheckinClip).where(CheckinClip.status == ClipStatus.PENDING))
        ).scalars().all()
        for c in clips:
            c.status = ClipStatus.ERROR
            c.error = "прервано (перезапуск сервера)"
        if runs or clips:
            await s.commit()
            log.info("Очищено зависших прогонов: %d, клипов: %d", len(runs), len(clips))


async def run_test_clip(minutes: int = 10, hotel_ids: list[int] | None = None) -> dict:
    """Тест-клип: тянет ПОСЛЕДНИЕ N минут субпотока целиком, мимо ночного окна и
    поиска активности. Окно привязывается к ЧАСАМ РЕГИСТРАТОРА (иначе при
    рассинхроне TZ сервера и NVR просим несуществующее время → поток не идёт)."""
    return await run_ingestion(
        day=dt.date.today(), hotel_ids=hotel_ids, trigger="test", test_minutes=max(minutes, 1),
    )


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
            raw = (st.name or "").strip()
            # Имя с устройства часто в битой кодировке (кракозябры) — тогда ведём
            # по номеру канала, а превью-кадр даёт визуальное опознание.
            clean = raw if (raw and "�" not in raw) else f"канал {st.channel_id}"
            result["channels"].append({
                "channel_id": st.channel_id, "name": clean, "raw_name": raw,
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
