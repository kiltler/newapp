"""Менеджер алертов: анти-спам (дедупликация) и уведомления 'восстановлено'.

Каждая проблема идентифицируется уникальным ``scope_key`` (например
``device:5:unreachable`` или ``device:5:channel:3:offline``). Пока проблема
активна, повторные алерты не шлются. При устранении проблемы отправляется
сообщение 'восстановлено'.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AlertState, Event, Severity, utcnow
from app.services import telegram

log = logging.getLogger(__name__)

_ICON = {Severity.INFO: "ℹ️", Severity.WARNING: "⚠️", Severity.CRITICAL: "🔴"}


async def _log_event(
    session: AsyncSession,
    *,
    type_: str,
    message: str,
    severity: str,
    device_id: int | None,
    channel_id: int | None = None,
) -> None:
    session.add(
        Event(
            device_id=device_id,
            channel_id=channel_id,
            type=type_,
            severity=severity,
            message=message,
        )
    )


async def raise_alert(
    session: AsyncSession,
    *,
    scope_key: str,
    alert_type: str,
    message: str,
    severity: str = Severity.WARNING,
    device_id: int | None = None,
    channel_id: int | None = None,
    context: dict | None = None,
    notify: bool = True,
) -> bool:
    """Открывает алерт, если он ещё не активен. Возвращает True, если новый.

    Идемпотентно: повторный вызов с тем же scope_key, пока проблема активна,
    ничего не шлёт (анти-спам).
    """
    existing = (
        await session.execute(select(AlertState).where(AlertState.scope_key == scope_key))
    ).scalar_one_or_none()

    if existing and existing.active:
        return False  # уже активен — не дублируем

    now = utcnow()
    if existing:  # был, но разрешён → переоткрываем
        existing.active = True
        existing.opened_at = now
        existing.resolved_at = None
        existing.context = context or {}
        state = existing
    else:
        state = AlertState(
            scope_key=scope_key,
            device_id=device_id,
            alert_type=alert_type,
            active=True,
            opened_at=now,
            context=context or {},
        )
        session.add(state)

    await _log_event(
        session, type_=alert_type, message=message, severity=severity,
        device_id=device_id, channel_id=channel_id,
    )

    if notify:
        icon = _ICON.get(severity, "⚠️")
        sent = await telegram.send_message(f"{icon} <b>АЛЕРТ</b>\n{telegram.esc(message)}")
        if sent:
            state.last_notified_at = now
    return True


async def resolve_alert(
    session: AsyncSession,
    *,
    scope_key: str,
    message: str,
    device_id: int | None = None,
    channel_id: int | None = None,
    notify: bool = True,
) -> bool:
    """Закрывает активный алерт и шлёт 'восстановлено'. True, если что-то закрыли."""
    existing = (
        await session.execute(select(AlertState).where(AlertState.scope_key == scope_key))
    ).scalar_one_or_none()
    if not existing or not existing.active:
        return False

    existing.active = False
    existing.resolved_at = utcnow()

    await _log_event(
        session, type_=existing.alert_type + "_resolved", message=message,
        severity=Severity.INFO, device_id=device_id or existing.device_id,
        channel_id=channel_id,
    )
    if notify:
        await telegram.send_message(f"✅ <b>ВОССТАНОВЛЕНО</b>\n{telegram.esc(message)}")
    return True


async def is_active(session: AsyncSession, scope_key: str) -> bool:
    state = (
        await session.execute(select(AlertState).where(AlertState.scope_key == scope_key))
    ).scalar_one_or_none()
    return bool(state and state.active)
