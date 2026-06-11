"""Автоопределение типа API устройства.

Логика по ТЗ:
  1. GET /ISAPI/System/deviceInfo → валидный XML с <model>  ⇒ hikvision
  2. GET /cgi-bin/magicBox.cgi?action=getDeviceType → type=XXX ⇒ dahua
  3. оба мимо → пробуем ONVIF, иначе 'unknown' (ручной выбор)
Дополнительно для Dahua пробуем basic-auth, если digest не прошёл.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.drivers.base import DeviceInfo
from app.drivers.dahua import DahuaClient
from app.drivers.hikvision import HikvisionClient
from app.models import ApiType

log = logging.getLogger(__name__)


@dataclass
class DetectResult:
    api_type: str
    auth_scheme: str = "digest"
    info: DeviceInfo | None = None
    detail: str = ""


async def detect_api_type(
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    use_https: bool = False,
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> DetectResult:
    common = dict(
        host=host, port=port, username=username, password=password,
        use_https=use_https, timeout=timeout, retries=1, client=client,
    )

    # 1) Hikvision ISAPI
    try:
        hik = HikvisionClient(**common)
        info = await hik.get_device_info()
        if info.model or info.serial:
            return DetectResult(ApiType.HIKVISION, "digest", info, "ISAPI deviceInfo")
    except Exception as exc:  # noqa: BLE001
        log.debug("hik detect miss: %s", exc)

    # 2) Dahua CGI (digest, затем basic)
    for scheme in ("digest", "basic"):
        try:
            dah = DahuaClient(auth_scheme=scheme, **common)
            resp = await dah._request(
                "GET", "/cgi-bin/magicBox.cgi", params={"action": "getDeviceType"}
            )
            if resp.status_code == 200 and "type=" in resp.text.lower():
                info = await dah.get_device_info()
                return DetectResult(ApiType.DAHUA, scheme, info, f"Dahua CGI ({scheme})")
        except Exception as exc:  # noqa: BLE001
            log.debug("dahua detect miss (%s): %s", scheme, exc)

    # 3) ONVIF fallback — заглушка (драйвер добавляется отдельно)
    # Здесь можно подключить onvif-zeep-async для probe.

    return DetectResult(ApiType.UNKNOWN, "digest", None, "тип не определён")
