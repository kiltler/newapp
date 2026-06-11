"""Тесты драйвера Hikvision (ISAPI) против mock-сервера."""
import datetime as dt

import pytest

from app.models import ChannelState, HddState
from tests.conftest import make_driver


async def test_device_info(nvr):
    client = make_driver("hikvision", nvr)
    info = await client.get_device_info()
    assert info.model == nvr.model
    assert info.serial == nvr.serial
    assert info.firmware == nvr.firmware


async def test_channels_online(nvr):
    client = make_driver("hikvision", nvr)
    statuses = await client.get_channel_statuses()
    assert len(statuses) == 4
    assert all(s.state == ChannelState.ONLINE for s in statuses)


async def test_channel_offline(nvr):
    nvr.get_channel(2).online = False
    client = make_driver("hikvision", nvr)
    statuses = {s.channel_id: s for s in await client.get_channel_statuses()}
    assert statuses[2].state == ChannelState.OFFLINE
    assert statuses[1].state == ChannelState.ONLINE


async def test_analog_video_loss(nvr_hybrid):
    # каналы 3,4 — аналоговые; ставим videoloss на 3
    nvr_hybrid.get_channel(3).video_loss = True
    client = make_driver("hikvision", nvr_hybrid)
    statuses = {s.channel_id: s for s in await client.get_channel_statuses()}
    assert statuses[3].kind == "analog"
    assert statuses[3].state == ChannelState.NO_VIDEO
    assert statuses[4].state == ChannelState.ONLINE


async def test_cyrillic_channel_name(nvr):
    nvr.get_channel(1).name = "Камера 1 Вход"
    client = make_driver("hikvision", nvr)
    statuses = {s.channel_id: s for s in await client.get_channel_statuses()}
    assert statuses[1].name == "Камера 1 Вход"  # кириллица не должна ломаться


async def test_hdd_ok(nvr):
    client = make_driver("hikvision", nvr)
    hdds = await client.get_hdd_info()
    assert hdds[0].status == HddState.OK
    assert hdds[0].capacity_mb > 0


async def test_hdd_error(nvr):
    nvr.hdds[0].status = "error"
    client = make_driver("hikvision", nvr)
    hdds = await client.get_hdd_info()
    assert hdds[0].status == HddState.ERROR


async def test_archive_full(nvr):
    client = make_driver("hikvision", nvr)
    day = dt.date.today() - dt.timedelta(days=1)
    start = dt.datetime.combine(day, dt.time.min)
    end = dt.datetime.combine(day, dt.time.max).replace(microsecond=0)
    segs = await client.search_archive(1, start, end)
    total = sum(s.duration_minutes for s in segs)
    assert total > 23 * 60  # почти сутки записи


async def test_archive_none(nvr):
    nvr.get_channel(1).archive = "none"
    client = make_driver("hikvision", nvr)
    day = dt.date.today() - dt.timedelta(days=1)
    start = dt.datetime.combine(day, dt.time.min)
    end = dt.datetime.combine(day, dt.time.max).replace(microsecond=0)
    segs = await client.search_archive(1, start, end)
    assert segs == []


async def test_time_drift(nvr):
    nvr.time_offset_seconds = 600  # +10 минут
    client = make_driver("hikvision", nvr)
    device_time = await client.get_device_time()
    drift = abs((device_time - dt.datetime.now()).total_seconds())
    assert 500 < drift < 700


async def test_capability_check_skips_disabled(nvr):
    nvr.disabled_features.add("archive")
    client = make_driver("hikvision", nvr)
    caps = await client.probe_capabilities()
    assert caps.channels is True
    assert caps.hdd is True
    assert caps.archive is False  # урезанный ISAPI (как у HiWatch)
