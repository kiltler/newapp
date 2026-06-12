"""Тесты действий: снапшот, синхронизация времени, перезагрузка (оба драйвера)."""
import datetime as dt

import pytest

from tests.conftest import make_driver


@pytest.mark.parametrize("api", ["hikvision", "dahua"])
async def test_snapshot(api, nvr):
    client = make_driver(api, nvr)
    data = await client.get_snapshot(1)
    assert data[:2] == b"\xff\xd8"  # сигнатура JPEG
    assert len(data) > 10


@pytest.mark.parametrize("api", ["hikvision", "dahua"])
async def test_sync_time(api, nvr):
    nvr.time_offset_seconds = 3600  # часы убежали на час
    client = make_driver(api, nvr)
    await client.sync_time()
    # после синхронизации дрейф пропал
    device_time = await client.get_device_time()
    drift = abs((device_time - dt.datetime.now()).total_seconds())
    assert drift < 120


@pytest.mark.parametrize("api", ["hikvision", "dahua"])
async def test_reboot(api, nvr):
    client = make_driver(api, nvr)
    await client.reboot()  # не должно бросить исключение


async def test_health_hikvision(nvr):
    nvr.temperature_c = 52.0
    nvr.cpu_percent = 33
    client = make_driver("hikvision", nvr)
    h = await client.get_health()
    assert h.temperature_c == 52.0
    assert h.cpu_percent == 33
    assert h.memory_percent is not None
