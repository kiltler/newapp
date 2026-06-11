"""Тесты драйвера Dahua (CGI) против mock-сервера."""
import datetime as dt

from app.models import ChannelState, HddState
from tests.conftest import make_driver


async def test_device_info(nvr):
    nvr.model = "DH-NVR4216-16P"
    client = make_driver("dahua", nvr)
    info = await client.get_device_info()
    assert info.model == "DH-NVR4216-16P"
    assert info.serial == nvr.serial


async def test_channels(nvr):
    nvr.get_channel(2).online = False
    nvr.get_channel(3).video_loss = True
    client = make_driver("dahua", nvr)
    statuses = {s.channel_id: s for s in await client.get_channel_statuses()}
    # Dahua 0-based на проводе → драйвер отдаёт 1-based
    assert statuses[1].state == ChannelState.ONLINE
    assert statuses[2].state == ChannelState.OFFLINE
    assert statuses[3].state == ChannelState.NO_VIDEO


async def test_hdd(nvr):
    client = make_driver("dahua", nvr)
    hdds = await client.get_hdd_info()
    assert hdds[0].status == HddState.OK
    assert hdds[0].capacity_mb > 0


async def test_hdd_error(nvr):
    nvr.hdds[0].status = "error"
    client = make_driver("dahua", nvr)
    hdds = await client.get_hdd_info()
    assert hdds[0].status == HddState.ERROR


async def test_archive_full(nvr):
    client = make_driver("dahua", nvr)
    day = dt.date.today() - dt.timedelta(days=1)
    start = dt.datetime.combine(day, dt.time.min)
    end = dt.datetime.combine(day, dt.time.max).replace(microsecond=0)
    segs = await client.search_archive(1, start, end)
    total = sum(s.duration_minutes for s in segs)
    assert total > 23 * 60


async def test_archive_gap(nvr):
    nvr.get_channel(1).archive = [["02:00", "05:00"]]  # дыра 3 часа
    client = make_driver("dahua", nvr)
    day = dt.date.today() - dt.timedelta(days=1)
    start = dt.datetime.combine(day, dt.time.min)
    end = dt.datetime.combine(day, dt.time.max).replace(microsecond=0)
    segs = await client.search_archive(1, start, end)
    total = sum(s.duration_minutes for s in segs)
    assert 20 * 60 < total < 22 * 60  # ~21 час записи


async def test_basic_auth(nvr):
    """Нюанс Dahua: некоторые прошивки требуют basic вместо digest."""
    client = make_driver("dahua", nvr, scheme="basic")
    info = await client.get_device_info()
    assert info.serial == nvr.serial


async def test_time(nvr):
    nvr.time_offset_seconds = -400
    client = make_driver("dahua", nvr)
    device_time = await client.get_device_time()
    drift = (device_time - dt.datetime.now()).total_seconds()
    assert -500 < drift < -300
