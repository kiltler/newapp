"""Планировщик фоновых задач (APScheduler): опрос и суточная проверка архива."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.services import archive, backup, poller, quality, watchdog

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        poller.poll_all,
        trigger=IntervalTrigger(minutes=settings.poll_interval_minutes),
        id="poll_all",
        name="Опрос устройств",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        archive.daily_archive_job,
        trigger=CronTrigger(
            hour=settings.archive_check_hour, minute=settings.archive_check_minute
        ),
        id="archive_check",
        name="Суточная проверка архива + глубина",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    if settings.quality_check_minutes > 0:
        scheduler.add_job(
            quality.check_quality_all,
            trigger=IntervalTrigger(minutes=settings.quality_check_minutes),
            id="quality_check",
            name="Контроль качества картинки",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

    if settings.watchdog_url:
        scheduler.add_job(
            watchdog.ping,
            trigger=IntervalTrigger(minutes=settings.watchdog_interval_minutes),
            id="watchdog",
            name="Watchdog-пульс",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

    scheduler.add_job(
        backup.auto_backup,
        trigger=CronTrigger(hour=3, minute=30),
        id="auto_backup",
        name="Ежедневный авто-бэкап",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.start()
    log.info(
        "Планировщик запущен: опрос каждые %d мин, архив в %02d:%02d",
        settings.poll_interval_minutes,
        settings.archive_check_hour,
        settings.archive_check_minute,
    )


def _add_checkin_job(hour: int, minute: int) -> None:
    from app.services import checkin_ingest

    scheduler.add_job(
        checkin_ingest.run_ingestion,
        trigger=CronTrigger(hour=hour, minute=minute),
        id="checkin_ingest",
        name="Ночной ingestion заселений",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )


async def configure_checkin_job() -> None:
    """Регистрирует ночной джоб заселений, читая время из БД (AppSetting)."""
    from app.database import SessionLocal
    from app.services import appsettings

    async with SessionLocal() as s:
        hour = await appsettings.get_int(s, "checkin_job_hour", 1)
        minute = await appsettings.get_int(s, "checkin_job_minute", 0)
    _add_checkin_job(hour, minute)
    log.info("Ночной ingestion заселений: %02d:%02d", hour, minute)


def reschedule_checkin_job(hour: int, minute: int) -> None:
    """Перепланирует ночной джоб без рестарта контейнера (вызывается из UI)."""
    if scheduler.running:
        _add_checkin_job(hour, minute)
        log.info("Ночной ingestion заселений перепланирован на %02d:%02d", hour, minute)


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
