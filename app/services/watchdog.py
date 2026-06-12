"""Watchdog: внешний «пульс», чтобы заметить смерть самого монитора.

Раз в N минут монитор пингует внешний сервис (например healthchecks.io). Если
монитор/сервер упал или опрос завис — пульс пропадает, и сервис сам шлёт вам
уведомление. «Сторож для сторожа».
"""
from __future__ import annotations

import datetime as dt
import logging

import httpx
from sqlalchemy import func, select

from app.config import settings
from app.database import SessionLocal
from app.models import Device
from app.services import poller

log = logging.getLogger(__name__)


async def evaluate() -> tuple[bool, str]:
    """Здоров ли монитор: идёт ли опрос вовремя. Возвращает (healthy, detail)."""
    async with SessionLocal() as session:
        device_count = (
            await session.execute(
                select(func.count()).select_from(Device).where(Device.enabled.is_(True))
            )
        ).scalar() or 0

    if device_count == 0:
        return True, "ok (нет устройств)"

    if poller.last_poll_at is None:
        return True, "ok (опрос ещё не запускался)"  # стартовая фора

    age = (dt.datetime.now(dt.timezone.utc) - poller.last_poll_at).total_seconds()
    limit = settings.poll_interval_minutes * 60 * 2 + 120  # 2 цикла + запас
    if age > limit:
        return False, f"опрос завис: {int(age)} с без обновления"
    return True, f"ok (опрос {int(age)} с назад)"


async def ping() -> None:
    """Пингует watchdog-URL: успех — обычный GET, проблема — на /fail."""
    if not settings.watchdog_url:
        return
    healthy, detail = await evaluate()
    base = settings.watchdog_url.rstrip("/")
    url = base if healthy else base + "/fail"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            await http.post(url, content=detail.encode("utf-8"))
    except httpx.HTTPError as exc:
        log.warning("Watchdog ping не удался: %s", exc)
