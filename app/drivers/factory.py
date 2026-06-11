"""Фабрика драйверов: по типу API строит нужный NVRClient."""
from __future__ import annotations

import asyncio

import httpx

from app.crypto import decrypt
from app.drivers.base import NVRClient
from app.drivers.dahua import DahuaClient
from app.drivers.hikvision import HikvisionClient
from app.models import ApiType, Device

_DRIVERS: dict[str, type[NVRClient]] = {
    ApiType.HIKVISION: HikvisionClient,
    ApiType.DAHUA: DahuaClient,
    # ApiType.ONVIF: OnvifClient,  # резерв, добавляется по необходимости
}


def build_client(
    device: Device,
    *,
    semaphore: asyncio.Semaphore | None = None,
    client: httpx.AsyncClient | None = None,
    password: str | None = None,
) -> NVRClient:
    """Создаёт драйвер для устройства из БД.

    ``password`` можно передать явно (для теста соединения до сохранения),
    иначе берётся расшифрованный из device.password_enc.
    """
    driver_cls = _DRIVERS.get(device.api_type)
    if driver_cls is None:
        raise ValueError(f"Драйвер для типа API '{device.api_type}' не реализован")
    pwd = password if password is not None else decrypt(device.password_enc)
    return driver_cls(
        host=device.host,
        port=device.http_port,
        username=device.username,
        password=pwd,
        use_https=device.use_https,
        timeout=device.timeout,
        retries=device.retries,
        auth_scheme=device.auth_scheme,
        semaphore=semaphore,
        client=client,
    )
