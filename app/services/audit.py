"""Журнал действий оператора в панели (аудит)."""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def log_action(
    session: AsyncSession,
    request: Request | None,
    action: str,
    target: str | None = None,
    detail: str | None = None,
) -> None:
    user = None
    if request is not None:
        try:
            user = request.session.get("user")
        except Exception:  # noqa: BLE001
            user = None
    session.add(AuditLog(user=user or "—", action=action, target=target, detail=detail))
    await session.commit()
