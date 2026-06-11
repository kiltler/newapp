"""Драйвер Dahua / RVI (Dahua-based) — HTTP CGI API (digest или basic auth).

Особенности, заложенные по ТЗ:
* статус камер через LogicDeviceManager (JSON), fallback на VideoLoss-индексы;
* поиск архива через фабрику mediaFileFind (create → findFile → findNextFile → destroy);
* поиск использует ЛОКАЛЬНОЕ время устройства (часы NVR могут плыть);
* часть CGI на старых прошивках отсутствует → FeatureUnavailable вместо падения.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse

from app.drivers.base import (
    ArchiveSegment,
    ChannelStatus,
    DeviceInfo,
    FeatureUnavailable,
    HddInfo,
    NVRClient,
)
from app.models import ApiType, HddState

log = logging.getLogger(__name__)

_DAHUA_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_kv(text: str) -> dict[str, str]:
    """Парсит ответ Dahua вида key=value (по строке на пару)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line:
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip()
    return out


def _parse_dahua_time(value: str) -> dt.datetime:
    return dt.datetime.strptime(value.strip(), _DAHUA_TIME_FMT)


class DahuaClient(NVRClient):
    api_type = ApiType.DAHUA

    # ── Идентификация ─────────────────────────────────────────────────────────
    async def get_device_info(self) -> DeviceInfo:
        resp = await self._request(
            "GET", "/cgi-bin/magicBox.cgi", params={"action": "getSystemInfo"}
        )
        if resp.status_code != 200:
            raise FeatureUnavailable(f"getSystemInfo: HTTP {resp.status_code}")
        kv = _parse_kv(resp.text)
        firmware = None
        try:
            r2 = await self._request(
                "GET", "/cgi-bin/magicBox.cgi", params={"action": "getSoftwareVersion"}
            )
            if r2.status_code == 200:
                firmware = _parse_kv(r2.text).get("version")
        except FeatureUnavailable:
            pass
        return DeviceInfo(
            model=kv.get("deviceType") or kv.get("DeviceType"),
            firmware=firmware or kv.get("version"),
            serial=kv.get("serialNumber") or kv.get("sn"),
            device_type=kv.get("deviceType"),
        )

    # ── Каналы ────────────────────────────────────────────────────────────────
    async def get_channel_statuses(self) -> list[ChannelStatus]:
        # Основной путь: LogicDeviceManager (JSON)
        try:
            resp = await self._request(
                "POST",
                "/cgi-bin/api/LogicDeviceManager/getCameraState",
                data=json.dumps({"uniqueChannels": [-1]}),
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200 and resp.text.strip().startswith("{"):
                data = json.loads(resp.text)
                states = data.get("states", [])
                result: list[ChannelStatus] = []
                for st in states:
                    ch = int(st.get("channel", 0))
                    conn = str(st.get("connectionState", "")).lower()
                    video = str(st.get("videoInputState", "normal")).lower()
                    online = conn == "connected"
                    video_loss = (not online) or video in ("lossvideo", "novideo", "loss")
                    result.append(
                        ChannelStatus(
                            channel_id=ch + 1,  # Dahua 0-based → 1-based для UI
                            name=st.get("name"),
                            online=online,
                            video_loss=video_loss,
                            kind="ip",
                        )
                    )
                if result:
                    return result
        except FeatureUnavailable:
            pass

        # Fallback: VideoLoss-индексы (старые прошивки)
        resp = await self._request(
            "GET",
            "/cgi-bin/eventManager.cgi",
            params={"action": "getEventIndexes", "code": "VideoLoss"},
        )
        if resp.status_code != 200:
            raise FeatureUnavailable("статус каналов недоступен")
        kv = _parse_kv(resp.text)
        # channels[0]=0, channels[1]=3 ... — каналы С потерей видео
        loss_channels = {int(v) for k, v in kv.items() if k.startswith("channels[")}
        total = int(kv.get("channels", 0)) if kv.get("channels", "").isdigit() else 16
        result = []
        for ch in range(total):
            result.append(
                ChannelStatus(
                    channel_id=ch + 1,
                    online=True,
                    video_loss=ch in loss_channels,
                    kind="ip",
                )
            )
        return result

    # ── HDD ───────────────────────────────────────────────────────────────────
    async def get_hdd_info(self) -> list[HddInfo]:
        resp = await self._request(
            "GET", "/cgi-bin/storageDevice.cgi", params={"action": "getDeviceAllInfo"}
        )
        if resp.status_code != 200:
            raise FeatureUnavailable(f"storageDevice: HTTP {resp.status_code}")
        kv = _parse_kv(resp.text)
        # Формат: list[0].Detail[0].TotalBytes=..., .UsedBytes=..., .State=...
        # Собираем по индексам list[i].Detail[j]
        disks: dict[str, dict[str, str]] = {}
        for key, val in kv.items():
            if key.startswith("list[") and ".Detail[" in key:
                prefix, _, field = key.rpartition(".")
                disks.setdefault(prefix, {})[field] = val
        hdds: list[HddInfo] = []
        for prefix, fields in sorted(disks.items()):
            total_b = int(fields.get("TotalBytes", 0) or 0)
            used_b = int(fields.get("UsedBytes", 0) or 0)
            state = fields.get("State", "").lower()
            name = fields.get("Name") or fields.get("Path") or prefix
            if state in ("error", "abnormal", "broken"):
                status = HddState.ERROR
            elif total_b == 0:
                status = HddState.NO_DISK
            else:
                status = HddState.OK
            hdds.append(
                HddInfo(
                    hdd_id=prefix,
                    name=name,
                    capacity_mb=total_b // (1024 * 1024),
                    free_mb=max(total_b - used_b, 0) // (1024 * 1024),
                    status=status,
                )
            )
        if not hdds:
            raise FeatureUnavailable("HDD не обнаружены")
        return hdds

    # ── Архив (фабрика mediaFileFind) ──────────────────────────────────────────
    async def search_archive(
        self, channel_id: int, start: dt.datetime, end: dt.datetime
    ) -> list[ArchiveSegment]:
        dahua_channel = channel_id - 1  # обратно в 0-based
        # a) factory.create
        resp = await self._request(
            "GET", "/cgi-bin/mediaFileFind.cgi", params={"action": "factory.create"}
        )
        if resp.status_code == 404:
            raise FeatureUnavailable("mediaFileFind не поддерживается")
        if resp.status_code != 200:
            raise FeatureUnavailable(f"factory.create: HTTP {resp.status_code}")
        obj = _parse_kv(resp.text).get("result")
        if not obj:
            raise FeatureUnavailable("mediaFileFind: нет object id")

        segments: list[ArchiveSegment] = []
        try:
            # b) findFile с условием
            find_params = {
                "action": "findFile",
                "object": obj,
                "condition.Channel": dahua_channel,
                "condition.StartTime": start.strftime(_DAHUA_TIME_FMT),
                "condition.EndTime": end.strftime(_DAHUA_TIME_FMT),
                "condition.Types[0]": "dav",
            }
            r = await self._request(
                "GET", "/cgi-bin/mediaFileFind.cgi", params=find_params
            )
            if r.status_code != 200 or "ok" not in r.text.lower():
                return segments  # ничего не найдено / пусто

            # c) findNextFile (постранично)
            for _ in range(200):
                rn = await self._request(
                    "GET",
                    "/cgi-bin/mediaFileFind.cgi",
                    params={"action": "findNextFile", "object": obj, "count": 100},
                )
                if rn.status_code != 200:
                    break
                kv = _parse_kv(rn.text)
                found = int(kv.get("found", 0) or 0)
                if found == 0:
                    break
                items: dict[int, dict[str, str]] = {}
                for key, val in kv.items():
                    if key.startswith("items["):
                        idx_str = key[len("items["):].split("]", 1)[0]
                        field = key.split(".", 1)[1] if "." in key else key
                        items.setdefault(int(idx_str), {})[field] = val
                for _, fields in sorted(items.items()):
                    st = fields.get("StartTime")
                    en = fields.get("EndTime")
                    if st and en:
                        try:
                            segments.append(
                                ArchiveSegment(_parse_dahua_time(st), _parse_dahua_time(en))
                            )
                        except ValueError:
                            continue
                if found < 100:
                    break
        finally:
            # d) destroy
            try:
                await self._request(
                    "GET",
                    "/cgi-bin/mediaFileFind.cgi",
                    params={"action": "destroy", "object": obj},
                )
            except FeatureUnavailable:
                pass
        return segments

    # ── Время устройства ───────────────────────────────────────────────────────
    async def get_device_time(self) -> dt.datetime:
        resp = await self._request(
            "GET", "/cgi-bin/global.cgi", params={"action": "getCurrentTime"}
        )
        if resp.status_code != 200:
            raise FeatureUnavailable(f"getCurrentTime: HTTP {resp.status_code}")
        kv = _parse_kv(resp.text)
        raw = kv.get("result") or resp.text.strip()
        raw = urllib.parse.unquote(raw)
        return _parse_dahua_time(raw)
