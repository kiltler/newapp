"""Интеграционный тест опроса: устройство в БД ↔ mock-сервер ↔ алерты."""
import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.models import AlertState, ApiType, Channel, ChannelState, Device, Event, Hdd
from mock.state import MockChannel
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


async def test_channel_mute_suppresses_alert(db, monkeypatch):
    nvr = MockNVR.default(channels=3)
    _patch_build_client(monkeypatch, nvr)
    async with SessionLocal() as session:
        device_id = await _make_device(session)
    await poller.poll_device(device_id)  # создаёт каналы

    async with SessionLocal() as session:
        ch = (
            await session.execute(
                select(Channel).where(Channel.device_id == device_id, Channel.channel_id == 2)
            )
        ).scalar_one()
        ch.enabled = False
        await session.commit()

    nvr.get_channel(2).online = False  # канал упал, но он заглушён
    await poller.poll_device(device_id)

    async with SessionLocal() as session:
        alert = (
            await session.execute(
                select(AlertState).where(
                    AlertState.scope_key == f"device:{device_id}:channel:2:down"
                )
            )
        ).scalar_one_or_none()
        assert alert is None or alert.active is False  # заглушённый канал не тревожит


async def test_camera_add_remove_events(db, monkeypatch):
    nvr = MockNVR.default(channels=3)
    _patch_build_client(monkeypatch, nvr)
    async with SessionLocal() as session:
        device_id = await _make_device(session)

    await poller.poll_device(device_id)  # первый опрос — 3 канала, без событий "добавлена"

    nvr.channels.append(MockChannel(id=4, name="Cam4"))  # добавили камеру
    await poller.poll_device(device_id)

    nvr.channels = [c for c in nvr.channels if c.id != 2]  # убрали камеру 2
    await poller.poll_device(device_id)

    async with SessionLocal() as session:
        types = (
            await session.execute(select(Event.type, Event.channel_id))
        ).all()
        added = [c for (t, c) in types if t == "camera_added"]
        removed = [c for (t, c) in types if t == "camera_removed"]
        assert 4 in added
        assert 2 in removed


async def test_overheat_alert(db, monkeypatch):
    nvr = MockNVR.default(channels=1)
    nvr.temperature_c = 90.0  # перегрев
    _patch_build_client(monkeypatch, nvr)
    async with SessionLocal() as session:
        device_id = await _make_device(session)

    await poller.poll_device(device_id)

    async with SessionLocal() as session:
        device = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
        alert = (
            await session.execute(
                select(AlertState).where(AlertState.scope_key == f"device:{device_id}:overheat")
            )
        ).scalar_one_or_none()
        assert device.temperature == 90.0
        assert alert is not None and alert.active is True


async def test_archive_depth_measured(db, monkeypatch):
    from app.services import archive as archive_mod

    nvr = MockNVR.default(channels=2)
    nvr.get_channel(1).archive = "none"  # по каналу 1 архива нет → глубина 0
    app = make_mock_app(nvr, "hikvision")

    def fake_build(device, *, semaphore=None, client=None, password=None):
        from app.drivers.hikvision import HikvisionClient

        return HikvisionClient(
            host="t", port=80, username=nvr.username, password=nvr.password,
            client=make_http(app, nvr.username, nvr.password),
        )

    monkeypatch.setattr(archive_mod, "build_client", fake_build)

    async with SessionLocal() as session:
        device = Device(
            name="D", host="t", http_port=80, username="admin",
            api_type=ApiType.HIKVISION, capabilities={"archive": True},
        )
        session.add(device)
        await session.commit()
        did = device.id
        session.add(Channel(device_id=did, channel_id=1))
        session.add(Channel(device_id=did, channel_id=2))
        await session.commit()

    await archive_mod.measure_device_depth(did)

    async with SessionLocal() as session:
        chs = (
            await session.execute(select(Channel).where(Channel.device_id == did))
        ).scalars().all()
        depths = {c.channel_id: c.archive_depth_days for c in chs}
        assert depths[1] == 0           # нет архива
        assert depths[2] is not None and depths[2] > 0  # глубина измерена


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
