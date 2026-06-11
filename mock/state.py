"""Состояние эмулируемого NVR — управляемое из тестов и для дев-режима."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class MockChannel:
    id: int
    name: str = ""
    kind: str = "ip"  # ip | analog
    online: bool = True
    video_loss: bool = False
    # Режим архива: "full" | "none" | список окон-дыр [["HH:MM","HH:MM"], ...]
    archive: object = "full"


@dataclass
class MockHdd:
    id: str = "1"
    name: str = "HDD1"
    capacity_mb: int = 4_000_000   # ~4 ТБ
    free_mb: int = 800_000
    status: str = "ok"             # ok | error | no_disk


@dataclass
class MockNVR:
    """Управляемое состояние одного эмулируемого регистратора."""

    username: str = "admin"
    password: str = "admin12345"
    model: str = "DS-7616NI-E2/8P"
    firmware: str = "V4.30.005"
    serial: str = "MOCK0000000001"
    device_type: str = "NVR"
    realm: str = "NVRMock"

    channels: list[MockChannel] = field(default_factory=list)
    hdds: list[MockHdd] = field(default_factory=list)

    # Сдвиг часов устройства относительно сервера (сек) — для теста дрейфа времени
    time_offset_seconds: int = 0

    # Какие фичи "урезаны" (для эмуляции HiWatch / старых прошивок)
    disabled_features: set[str] = field(default_factory=set)

    @classmethod
    def default(cls, channels: int = 4, analog: int = 0) -> "MockNVR":
        nvr = cls()
        for i in range(1, channels + 1):
            nvr.channels.append(MockChannel(id=i, name=f"Camera {i}", kind="ip"))
        for j in range(channels + 1, channels + analog + 1):
            nvr.channels.append(MockChannel(id=j, name=f"Analog {j}", kind="analog"))
        nvr.hdds.append(MockHdd())
        return nvr

    def device_now(self) -> dt.datetime:
        return dt.datetime.now() + dt.timedelta(seconds=self.time_offset_seconds)

    def get_channel(self, cid: int) -> MockChannel | None:
        return next((c for c in self.channels if c.id == cid), None)
