"""Тесты watchdog (оценка здоровья монитора) и сборки коллажа бота."""
import datetime as dt
import io

from PIL import Image

from app.database import SessionLocal
from app.models import ApiType, Device
from app.services import bot, poller, watchdog


def _jpeg(color=(0, 120, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="JPEG")
    return buf.getvalue()


async def test_watchdog_ok_when_no_devices(db):
    poller.last_poll_at = None
    healthy, _ = await watchdog.evaluate()
    assert healthy is True


async def test_watchdog_unhealthy_when_poll_stale(db):
    async with SessionLocal() as s:
        s.add(Device(name="D", host="h", username="a", api_type=ApiType.HIKVISION))
        await s.commit()
    # последний опрос был давно
    poller.last_poll_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    healthy, detail = await watchdog.evaluate()
    assert healthy is False
    assert "завис" in detail


async def test_watchdog_healthy_when_poll_recent(db):
    async with SessionLocal() as s:
        s.add(Device(name="D", host="h", username="a", api_type=ApiType.HIKVISION))
        await s.commit()
    poller.last_poll_at = dt.datetime.now(dt.timezone.utc)
    healthy, _ = await watchdog.evaluate()
    assert healthy is True


def test_make_collage():
    items = [(f"cam{i}", _jpeg()) for i in range(5)]
    data = bot.make_collage(items)
    assert data[:2] == b"\xff\xd8"  # JPEG
    assert len(data) > 100


def test_make_collage_empty():
    assert bot.make_collage([]) is None


async def test_build_status_text(db):
    async with SessionLocal() as s:
        s.add(Device(name="Объект", host="1.2.3.4", username="a",
                     api_type=ApiType.HIKVISION, reachable=False))
        await s.commit()
    text = await bot.build_status_text()
    assert "Парк" in text and "Объект" in text
