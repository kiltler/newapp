"""Метрики в формате Prometheus (для Grafana)."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import list_devices
from app.models import AlertState, ChannelState


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


async def render_metrics(session: AsyncSession) -> str:
    devices = await list_devices(session)
    active_alerts = (
        await session.execute(
            select(func.count()).select_from(AlertState).where(AlertState.active.is_(True))
        )
    ).scalar() or 0

    total = len(devices)
    online = sum(1 for d in devices if d.reachable and d.enabled)
    unreachable = sum(1 for d in devices if not d.reachable and d.enabled)

    # Собираем образцы по каждой метрике (чтобы выводить сгруппированно)
    samples: dict[str, list[str]] = {}

    def add(name: str, value, labels: str = ""):
        samples.setdefault(name, []).append(
            f"{name}{{{labels}}} {value}" if labels else f"{name} {value}"
        )

    ch_total = ch_online = ch_problem = 0
    for d in devices:
        lbl = f'device="{_esc(d.name)}",host="{_esc(d.host)}"'
        c_total = len(d.channels)
        c_online = sum(1 for c in d.channels if c.status == ChannelState.ONLINE)
        c_problem = sum(
            1 for c in d.channels
            if c.enabled is not False and c.status not in (ChannelState.ONLINE, ChannelState.UNKNOWN)
        )
        ch_total += c_total
        ch_online += c_online
        ch_problem += c_problem
        add("nvrmon_device_reachable", 1 if d.reachable else 0, lbl)
        add("nvrmon_device_channels_total", c_total, lbl)
        add("nvrmon_device_channels_online", c_online, lbl)
        add("nvrmon_device_channels_problem", c_problem, lbl)
        if d.temperature is not None:
            add("nvrmon_device_temperature_celsius", d.temperature, lbl)
        if d.cpu_load is not None:
            add("nvrmon_device_cpu_percent", d.cpu_load, lbl)
        if d.time_drift_seconds is not None:
            add("nvrmon_device_time_drift_seconds", d.time_drift_seconds, lbl)

    add("nvrmon_devices_total", total)
    add("nvrmon_devices_online", online)
    add("nvrmon_devices_unreachable", unreachable)
    add("nvrmon_active_alerts", active_alerts)
    add("nvrmon_channels_total", ch_total)
    add("nvrmon_channels_online", ch_online)
    add("nvrmon_channels_problem", ch_problem)

    _HELP = {
        "nvrmon_devices_total": "Всего устройств",
        "nvrmon_devices_online": "Доступных устройств",
        "nvrmon_devices_unreachable": "Недоступных устройств",
        "nvrmon_active_alerts": "Активных алертов",
        "nvrmon_channels_total": "Всего каналов",
        "nvrmon_channels_online": "Каналов онлайн",
        "nvrmon_channels_problem": "Каналов с проблемой",
        "nvrmon_device_reachable": "Доступность устройства (1/0)",
        "nvrmon_device_channels_total": "Каналов на устройстве",
        "nvrmon_device_channels_online": "Каналов онлайн на устройстве",
        "nvrmon_device_channels_problem": "Каналов с проблемой на устройстве",
        "nvrmon_device_temperature_celsius": "Температура NVR, °C",
        "nvrmon_device_cpu_percent": "Загрузка CPU NVR, %",
        "nvrmon_device_time_drift_seconds": "Дрейф времени NVR, сек",
    }

    out: list[str] = []
    for name in _HELP:
        if name not in samples:
            continue
        out.append(f"# HELP {name} {_HELP[name]}")
        out.append(f"# TYPE {name} gauge")
        out.extend(samples[name])
    return "\n".join(out) + "\n"
