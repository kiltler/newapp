"""Драйвер Hikvision / HiWatch — ISAPI (HTTP + XML, digest auth)."""
from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
import xml.etree.ElementTree as ET

import httpx

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


def _strip_ns(xml_text: str) -> ET.Element:
    """Парсит XML, убирая namespace из тегов (ISAPI любит namespaces)."""
    # удаляем xmlns=... чтобы теги были без префиксов
    cleaned = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml_text, count=0)
    cleaned = re.sub(r"<(/?)\w+:", r"<\1", cleaned)
    return ET.fromstring(cleaned)


def _body(resp) -> str:
    """Декодирует тело ответа в текст.

    ISAPI заявляет UTF-8, но многие прошивки Hikvision отдают имена камер в
    Windows-1251 (кириллица). Поэтому: пробуем строгий UTF-8, при ошибке —
    откатываемся на cp1251.
    """
    raw = resp.content
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def _text(el: ET.Element | None, tag: str) -> str | None:
    if el is None:
        return None
    found = el.find(f".//{tag}")
    return found.text.strip() if found is not None and found.text else None


def _parse_hik_time(value: str) -> dt.datetime:
    """Парсит время ISAPI (ISO 8601, иногда с 'Z' для локального времени)."""
    v = value.strip()
    try:
        if v.endswith("Z"):
            return dt.datetime.fromisoformat(v[:-1])
        return dt.datetime.fromisoformat(v).replace(tzinfo=None)
    except ValueError:
        return dt.datetime.strptime(v[:19], "%Y-%m-%dT%H:%M:%S")


class HikvisionClient(NVRClient):
    api_type = ApiType.HIKVISION

    # ── Идентификация ─────────────────────────────────────────────────────────
    async def get_device_info(self) -> DeviceInfo:
        resp = await self._request("GET", "/ISAPI/System/deviceInfo")
        if resp.status_code != 200:
            raise FeatureUnavailable(f"deviceInfo: HTTP {resp.status_code}")
        root = _strip_ns(_body(resp))
        return DeviceInfo(
            model=_text(root, "model"),
            firmware=_text(root, "firmwareVersion"),
            serial=_text(root, "serialNumber"),
            device_type=_text(root, "deviceType"),
        )

    # ── Каналы ────────────────────────────────────────────────────────────────
    async def get_channel_statuses(self) -> list[ChannelStatus]:
        result: dict[int, ChannelStatus] = {}

        # 1) IP-камеры через InputProxy
        try:
            resp = await self._request(
                "GET", "/ISAPI/ContentMgmt/InputProxy/channels/status"
            )
            if resp.status_code == 200:
                root = _strip_ns(_body(resp))
                for item in root.findall(".//InputProxyChannelStatus"):
                    cid = _text(item, "id")
                    if cid is None:
                        continue
                    online = (_text(item, "online") or "false").lower() == "true"
                    name = _text(item, "name") or _text(item, "ipAddress")
                    result[int(cid)] = ChannelStatus(
                        channel_id=int(cid), name=name, online=online,
                        video_loss=not online, kind="ip",
                    )
        except FeatureUnavailable:
            pass

        # 2) Аналоговые/гибридные входы (DS-H332/2Q и т.п.) — videoloss
        try:
            resp = await self._request("GET", "/ISAPI/System/Video/inputs/channels")
            if resp.status_code == 200:
                root = _strip_ns(_body(resp))
                for ch in root.findall(".//VideoInputChannel"):
                    cid = _text(ch, "id")
                    if cid is None:
                        continue
                    cid_i = int(cid)
                    if cid_i in result:
                        continue  # уже учтён как IP
                    enabled = (_text(ch, "videoInputEnabled") or "true").lower() == "true"
                    if not enabled:
                        continue  # вход административно отключён — не мониторим
                    # Нет сигнала: пустое разрешение или явный маркер "NO VIDEO"
                    res_desc = (_text(ch, "resDesc") or "").strip().upper()
                    video_loss = (not res_desc) or "NO VIDEO" in res_desc
                    result[cid_i] = ChannelStatus(
                        channel_id=cid_i,
                        name=_text(ch, "name"),
                        online=True,           # аналоговый вход физически есть
                        video_loss=video_loss,
                        kind="analog",
                    )
        except FeatureUnavailable:
            pass

        if not result:
            raise FeatureUnavailable("статус каналов недоступен")
        return [result[k] for k in sorted(result)]

    # ── HDD ───────────────────────────────────────────────────────────────────
    async def get_hdd_info(self) -> list[HddInfo]:
        resp = await self._request("GET", "/ISAPI/ContentMgmt/Storage/hdd")
        if resp.status_code != 200:
            raise FeatureUnavailable(f"hdd: HTTP {resp.status_code}")
        root = _strip_ns(_body(resp))
        hdds: list[HddInfo] = []
        for hdd in root.findall(".//hdd"):
            hid = _text(hdd, "id") or "0"
            raw_status = (_text(hdd, "status") or "").lower()
            cap = int(_text(hdd, "capacity") or 0)        # МБ
            free = int(_text(hdd, "freeSpace") or 0)       # МБ
            if raw_status in ("ok", "normal", "unformatted"):
                status = HddState.OK
            elif raw_status in ("error", "failed", "smartfailed"):
                status = HddState.ERROR
            elif raw_status in ("", "idle") and cap == 0:
                status = HddState.NO_DISK
            else:
                status = HddState.OK
            hdds.append(
                HddInfo(hdd_id=hid, name=_text(hdd, "hddName"),
                        capacity_mb=cap, free_mb=free, status=status)
            )
        if not hdds:
            raise FeatureUnavailable("список HDD пуст")
        return hdds

    # ── Архив ─────────────────────────────────────────────────────────────────
    async def search_archive(
        self, channel_id: int, start: dt.datetime, end: dt.datetime
    ) -> list[ArchiveSegment]:
        track_id = channel_id * 100 + 1  # 1 → 101 (основной поток)
        # Часть прошивок Hikvision требует searchID строго в формате UUID,
        # иначе отвечают 400/невалидным XML. Используем настоящий UUID.
        search_id = str(uuid.uuid4())
        segments: list[ArchiveSegment] = []
        position = 0
        page = 100
        for _ in range(50):  # ограничение пагинации
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                "<CMSearchDescription>"
                f"<searchID>{search_id}</searchID>"
                f"<trackList><trackID>{track_id}</trackID></trackList>"
                "<timeSpanList><timeSpan>"
                f"<startTime>{start.strftime('%Y-%m-%dT%H:%M:%SZ')}</startTime>"
                f"<endTime>{end.strftime('%Y-%m-%dT%H:%M:%SZ')}</endTime>"
                "</timeSpan></timeSpanList>"
                f"<maxResults>{page}</maxResults>"
                f"<searchResultPosition>{position}</searchResultPosition>"
                "</CMSearchDescription>"
            )
            resp = await self._request(
                "POST", "/ISAPI/ContentMgmt/search", data=body,
                headers={"Content-Type": "application/xml"},
            )
            if resp.status_code == 404:
                raise FeatureUnavailable("поиск архива не поддерживается")
            if resp.status_code != 200:
                raise FeatureUnavailable(f"search: HTTP {resp.status_code}")
            root = _strip_ns(_body(resp))
            matches = root.findall(".//searchMatchItem")
            for m in matches:
                ts = m.find(".//timeSpan")
                if ts is None:
                    continue
                st = _text(ts, "startTime")
                en = _text(ts, "endTime")
                if st and en:
                    segments.append(
                        ArchiveSegment(_parse_hik_time(st), _parse_hik_time(en))
                    )
            status_str = (_text(root, "responseStatusStrg") or "").upper()
            if len(matches) < page or status_str == "OK":
                break
            position += page
        return segments

    # ── Время устройства ───────────────────────────────────────────────────────
    async def get_device_time(self) -> dt.datetime:
        resp = await self._request("GET", "/ISAPI/System/time")
        if resp.status_code != 200:
            raise FeatureUnavailable(f"time: HTTP {resp.status_code}")
        root = _strip_ns(_body(resp))
        local = _text(root, "localTime")
        if not local:
            raise FeatureUnavailable("нет localTime")
        return _parse_hik_time(local)

    # ── Действия ───────────────────────────────────────────────────────────────
    async def get_snapshot(self, channel_id: int) -> bytes:
        # Основной поток канала: /Streaming/channels/<ch*100+1>/picture
        resp = await self._request(
            "GET", f"/ISAPI/Streaming/channels/{channel_id * 100 + 1}/picture"
        )
        if resp.status_code != 200 or not resp.content:
            raise FeatureUnavailable(f"snapshot: HTTP {resp.status_code}")
        return resp.content

    async def sync_time(self) -> None:
        # Берём текущий XML времени, подменяем localTime на локальное время сервера
        # (с поясным смещением) и режим на ручной, затем PUT обратно.
        cur = await self._request("GET", "/ISAPI/System/time")
        if cur.status_code != 200:
            raise FeatureUnavailable(f"time GET: HTTP {cur.status_code}")
        now_iso = dt.datetime.now().astimezone().replace(microsecond=0).isoformat()
        body = re.sub(r"<localTime>.*?</localTime>", f"<localTime>{now_iso}</localTime>", _body(cur))
        body = re.sub(r"<timeMode>.*?</timeMode>", "<timeMode>manual</timeMode>", body)
        resp = await self._request(
            "PUT", "/ISAPI/System/time", data=body,
            headers={"Content-Type": "application/xml"},
        )
        if resp.status_code not in (200, 201):
            raise FeatureUnavailable(f"time PUT: HTTP {resp.status_code}")

    async def reboot(self) -> None:
        resp = await self._request("PUT", "/ISAPI/System/reboot")
        if resp.status_code not in (200, 201):
            raise FeatureUnavailable(f"reboot: HTTP {resp.status_code}")
