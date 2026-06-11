"""Тесты менеджера алертов: анти-спам и 'восстановлено'."""
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import AlertState, Event
from app.services import alerts


async def test_raise_dedup(db):
    async with SessionLocal() as session:
        new1 = await alerts.raise_alert(
            session, scope_key="dev:1:unreachable", alert_type="nvr_unreachable",
            message="NVR недоступен", device_id=None,
        )
        new2 = await alerts.raise_alert(
            session, scope_key="dev:1:unreachable", alert_type="nvr_unreachable",
            message="NVR недоступен снова", device_id=None,
        )
        await session.commit()
        assert new1 is True
        assert new2 is False  # анти-спам: не дублируем
        count = (await session.execute(select(func.count()).select_from(Event))).scalar()
        assert count == 1  # одно событие


async def test_resolve(db):
    async with SessionLocal() as session:
        await alerts.raise_alert(
            session, scope_key="dev:2:hdd:1:fault", alert_type="hdd_fault",
            message="HDD ошибка",
        )
        await session.commit()
    async with SessionLocal() as session:
        resolved = await alerts.resolve_alert(
            session, scope_key="dev:2:hdd:1:fault", message="HDD норма"
        )
        await session.commit()
        assert resolved is True
        state = (
            await session.execute(
                select(AlertState).where(AlertState.scope_key == "dev:2:hdd:1:fault")
            )
        ).scalar_one()
        assert state.active is False
        assert state.resolved_at is not None


async def test_reopen_after_resolve(db):
    async with SessionLocal() as session:
        await alerts.raise_alert(session, scope_key="k", alert_type="t", message="m1")
        await alerts.resolve_alert(session, scope_key="k", message="ok")
        reopened = await alerts.raise_alert(session, scope_key="k", alert_type="t", message="m2")
        await session.commit()
        assert reopened is True  # повторно открываем после устранения


async def test_resolve_nonexistent(db):
    async with SessionLocal() as session:
        result = await alerts.resolve_alert(session, scope_key="nope", message="x")
        assert result is False
