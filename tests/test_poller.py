"""Интеграционный тест опроса: устройство в БД ↔ mock-сервер ↔ алерты."""
import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.models import AlertState, ApiType, Channel, ChannelState, Device, Hdd
from app.services import poller
from mock.server import make_mock_app
from mock.state import MockNVR
from tests.conftest import make_http


async def _make_device(session) -> int:
    device = Device(
        name="Объект-1", host="testserver", http_port=80,
        username="admin", api_type=ApiType.HIKVISION, capabilities={},
    )
    session.add(device)
    await session.commit()
    return device.id


def _patch_build_client(monkeypatch, nvr: MockNVR):
    app = make_mock_app(nvr, "hikvision")

    def fake_build_client(device, *, semaphore=None, client=None, password=None):
        from app.drivers.hikvision import HikvisionClient

        http = make_http(app, nvr.username, nvr.password)
        return HikvisionClient(
            host="testserver", port=80, username=nvr.username,
            password=nvr.password, client=http,
        )

    monkeypatch.setattr(poller, "build_client", fake_build_client)


async def test_poll_stores_channels_and_hdd(db, monkeypatch):
    nvr = MockNVR.default(channels=3)
    _patch_build_client(monkeypatch, nvr)
    async with SessionLocal() as session:
        device_id = await _make_device(session)

    await poller.poll_device(device_id)

    async with SessionLocal() as session:
        channels = (
            await session.execute(select(Channel).where(Channel.device_id == device_id))
        ).scalars().all()
        hdds = (
            await session.execute(select(Hdd).where(Hdd.device_id == device_id))
        ).scalars().all()
        device = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
        assert len(channels) == 3
        assert all(c.status == ChannelState.ONLINE for c in channels)
        assert len(hdds) == 1
        assert device.reachable is True


async def test_poll_raises_camera_alert(db, monkeypatch):
    nvr = MockNVR.default(channels=3)
    nvr.get_channel(2).online = False
    _patch_build_client(monkeypatch, nvr)
    async with SessionLocal() as session:
        device_id = await _make_device(session)

    await poller.poll_device(device_id)

    async with SessionLocal() as session:
        alert = (
            await session.execute(
                select(AlertState).where(
                    AlertState.scope_key == f"device:{device_id}:channel:2:down"
                )
            )
        ).scalar_one_or_none()
        assert alert is not None and alert.active is True


async def test_poll_unreachable(db, monkeypatch):
    """Устройство недоступно по сети → алерт nvr_unreachable (порог=1)."""
    def fake_build_client(device, *, semaphore=None, client=None, password=None):
        from app.drivers.hikvision import HikvisionClient

        # Транспорт, всегда бросающий ConnectError
        def handler(request):
            raise httpx.ConnectError("unreachable")

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return HikvisionClient(host="x", port=80, username="a", password="b", client=http)

    monkeypatch.setattr(poller, "build_client", fake_build_client)
    async with SessionLocal() as session:
        device_id = await _make_device(session)

    await poller.poll_device(device_id)

    async with SessionLocal() as session:
        device = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
        alert = (
            await session.execute(
                select(AlertState).where(
                    AlertState.scope_key == f"device:{device_id}:unreachable"
                )
            )
        ).scalar_one_or_none()
        assert device.reachable is False
        assert alert is not None and alert.active is True
