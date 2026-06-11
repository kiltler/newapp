"""Эмулятор NVR: ISAPI (Hikvision) и CGI (Dahua) поверх FastAPI.

Используется и в тестах (через httpx ASGITransport), и в дев-режиме
(standalone uvicorn). Состояние :class:`MockNVR` управляемо — можно гасить
каналы, ломать диски, сдвигать часы, эмулировать дыры в архиве.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json

from fastapi import FastAPI, Request, Response

from mock.auth import require_auth
from mock.state import MockNVR

_HIK_TIME = "%Y-%m-%dT%H:%M:%SZ"
_DAHUA_TIME = "%Y-%m-%d %H:%M:%S"


# ── Генерация записей архива ────────────────────────────────────────────────
def _in_gap(t: dt.time, gaps: list) -> bool:
    for g in gaps:
        start = dt.time.fromisoformat(g[0])
        end = dt.time.fromisoformat(g[1])
        if start <= t < end:
            return True
    return False


def generate_segments(
    archive_cfg: object, day_start: dt.datetime, day_end: dt.datetime
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Почасовые файлы записи с учётом конфигурации (full / none / список дыр)."""
    if archive_cfg == "none":
        return []
    gaps = [] if archive_cfg == "full" else list(archive_cfg)  # type: ignore[arg-type]
    segments: list[tuple[dt.datetime, dt.datetime]] = []
    cursor = day_start.replace(minute=0, second=0, microsecond=0)
    while cursor < day_end:
        nxt = min(cursor + dt.timedelta(hours=1), day_end)
        if not _in_gap(cursor.time(), gaps):
            segments.append((max(cursor, day_start), nxt))
        cursor = nxt
    return segments


def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def _txt(body: str) -> Response:
    return Response(content=body, media_type="text/plain")


def make_mock_app(nvr: MockNVR | None = None, profile: str = "both") -> FastAPI:
    """Создаёт FastAPI-приложение мока.

    profile: 'hikvision' (только ISAPI), 'dahua' (только CGI), 'both'.
    """
    app = FastAPI(title=f"MockNVR[{profile}]")
    app.state.nvr = nvr or MockNVR.default(channels=4)
    app.state.find_objects = {}
    app.state.find_counter = itertools.count(1)

    def state() -> MockNVR:
        return app.state.nvr

    # ────────────────────────── ISAPI (Hikvision) ──────────────────────────
    if profile in ("hikvision", "both"):

        @app.get("/ISAPI/System/deviceInfo")
        async def device_info(request: Request):
            n = state()
            require_auth(request, n.username, n.password)
            return _xml(
                "<DeviceInfo>"
                f"<deviceName>{n.model}</deviceName>"
                f"<model>{n.model}</model>"
                f"<serialNumber>{n.serial}</serialNumber>"
                f"<firmwareVersion>{n.firmware}</firmwareVersion>"
                f"<deviceType>{n.device_type}</deviceType>"
                "</DeviceInfo>"
            )

        @app.get("/ISAPI/ContentMgmt/InputProxy/channels/status")
        async def ip_channels(request: Request):
            n = state()
            require_auth(request, n.username, n.password)
            if "channels" in n.disabled_features:
                return Response(status_code=404)
            items = []
            for c in n.channels:
                if c.kind != "ip":
                    continue
                online = "true" if c.online else "false"
                items.append(
                    f"<InputProxyChannelStatus><id>{c.id}</id>"
                    f"<name>{c.name}</name><online>{online}</online>"
                    "</InputProxyChannelStatus>"
                )
            return _xml(f"<InputProxyChannelStatusList>{''.join(items)}</InputProxyChannelStatusList>")

        @app.get("/ISAPI/System/Video/inputs/channels")
        async def analog_channels(request: Request):
            n = state()
            require_auth(request, n.username, n.password)
            items = []
            for c in n.channels:
                if c.kind != "analog":
                    continue
                enabled = "true"
                res = "" if c.video_loss else "1920*1080"
                items.append(
                    f"<VideoInputChannel><id>{c.id}</id><name>{c.name}</name>"
                    f"<videoInputEnabled>{enabled}</videoInputEnabled>"
                    f"<resDesc>{res}</resDesc></VideoInputChannel>"
                )
            return _xml(f"<VideoInputChannelList>{''.join(items)}</VideoInputChannelList>")

        @app.get("/ISAPI/ContentMgmt/Storage/hdd")
        async def hdd(request: Request):
            n = state()
            require_auth(request, n.username, n.password)
            if "hdd" in n.disabled_features:
                return Response(status_code=404)
            items = []
            for h in n.hdds:
                cap = 0 if h.status == "no_disk" else h.capacity_mb
                free = 0 if h.status == "no_disk" else h.free_mb
                st = {"ok": "ok", "error": "error", "no_disk": "idle"}[h.status]
                items.append(
                    f"<hdd><id>{h.id}</id><hddName>{h.name}</hddName>"
                    f"<capacity>{cap}</capacity><freeSpace>{free}</freeSpace>"
                    f"<status>{st}</status></hdd>"
                )
            return _xml(f"<hddList>{''.join(items)}</hddList>")

        @app.post("/ISAPI/ContentMgmt/search")
        async def search(request: Request):
            n = state()
            require_auth(request, n.username, n.password)
            if "archive" in n.disabled_features:
                return Response(status_code=404)
            body = (await request.body()).decode()
            # trackID и временной диапазон
            import re

            track = re.search(r"<trackID>(\d+)</trackID>", body)
            start_m = re.search(r"<startTime>([^<]+)</startTime>", body)
            end_m = re.search(r"<endTime>([^<]+)</endTime>", body)
            pos_m = re.search(r"<searchResultPosition>(\d+)</searchResultPosition>", body)
            if not (track and start_m and end_m):
                return _xml("<CMSearchResult><responseStatusStrg>NO MATCHES</responseStatusStrg></CMSearchResult>")
            position = int(pos_m.group(1)) if pos_m else 0
            channel_id = int(track.group(1)) // 100
            ch = n.get_channel(channel_id)
            start = dt.datetime.strptime(start_m.group(1), _HIK_TIME)
            end = dt.datetime.strptime(end_m.group(1), _HIK_TIME)
            segs = generate_segments(ch.archive, start, end) if ch else []
            if position > 0:  # всё отдаём за один проход
                segs = []
            matches = "".join(
                "<searchMatchItem><timeSpan>"
                f"<startTime>{s.strftime(_HIK_TIME)}</startTime>"
                f"<endTime>{e.strftime(_HIK_TIME)}</endTime>"
                "</timeSpan></searchMatchItem>"
                for s, e in segs
            )
            return _xml(
                "<CMSearchResult><responseStatus>true</responseStatus>"
                "<responseStatusStrg>OK</responseStatusStrg>"
                f"<numOfMatches>{len(segs)}</numOfMatches>"
                f"<matchList>{matches}</matchList></CMSearchResult>"
            )

        @app.get("/ISAPI/System/time")
        async def sys_time(request: Request):
            n = state()
            require_auth(request, n.username, n.password)
            local = n.device_now().strftime("%Y-%m-%dT%H:%M:%S")
            return _xml(
                f"<Time><timeMode>NTP</timeMode><localTime>{local}</localTime>"
                "<timeZone>CST-8:00:00</timeZone></Time>"
            )

    # ──────────────────────────── CGI (Dahua) ──────────────────────────────
    if profile in ("dahua", "both"):

        @app.get("/cgi-bin/magicBox.cgi")
        async def magicbox(request: Request, action: str = ""):
            n = state()
            require_auth(request, n.username, n.password)
            if action == "getDeviceType":
                return _txt(f"type={n.model}")
            if action == "getSoftwareVersion":
                return _txt(f"version={n.firmware}")
            # getSystemInfo
            return _txt(
                f"deviceType={n.model}\nserialNumber={n.serial}\n"
                f"hardwareVersion=1.00\nprocessor=ARM\n"
            )

        @app.post("/cgi-bin/api/LogicDeviceManager/getCameraState")
        async def camera_state(request: Request):
            n = state()
            require_auth(request, n.username, n.password)
            if "channels" in n.disabled_features:
                return Response(status_code=404)
            states = []
            for c in n.channels:
                states.append(
                    {
                        "channel": c.id - 1,  # Dahua 0-based
                        "name": c.name,
                        "connectionState": "Connected" if c.online else "Disconnected",
                        "videoInputState": "LossVideo" if c.video_loss else "Normal",
                    }
                )
            return Response(content=json.dumps({"states": states}),
                            media_type="application/json")

        @app.get("/cgi-bin/eventManager.cgi")
        async def event_manager(request: Request, action: str = "", code: str = ""):
            n = state()
            require_auth(request, n.username, n.password)
            loss = [c.id - 1 for c in n.channels if c.video_loss]
            lines = [f"channels[{i}]={ch}" for i, ch in enumerate(loss)]
            return _txt("\n".join(lines) if lines else "channels[0]=")

        @app.get("/cgi-bin/storageDevice.cgi")
        async def storage(request: Request, action: str = ""):
            n = state()
            require_auth(request, n.username, n.password)
            if "hdd" in n.disabled_features:
                return Response(status_code=404)
            lines = []
            for i, h in enumerate(n.hdds):
                total_b = 0 if h.status == "no_disk" else h.capacity_mb * 1024 * 1024
                used_b = 0 if h.status == "no_disk" else (h.capacity_mb - h.free_mb) * 1024 * 1024
                st = {"ok": "Running", "error": "Error", "no_disk": "NoDisk"}[h.status]
                p = f"list[0].Detail[{i}]"
                lines += [
                    f"{p}.Name={h.name}",
                    f"{p}.TotalBytes={total_b}",
                    f"{p}.UsedBytes={used_b}",
                    f"{p}.State={st}",
                ]
            return _txt("\n".join(lines))

        @app.get("/cgi-bin/mediaFileFind.cgi")
        async def media_find(request: Request, action: str = "", object: str = ""):
            n = state()
            require_auth(request, n.username, n.password)
            if "archive" in n.disabled_features:
                return Response(status_code=404)
            params = dict(request.query_params)

            if action == "factory.create":
                oid = str(next(app.state.find_counter))
                app.state.find_objects[oid] = {"segments": [], "cursor": 0}
                return _txt(f"result={oid}")

            if action == "findFile":
                channel = int(params.get("condition.Channel", 0))
                start = dt.datetime.strptime(params["condition.StartTime"], _DAHUA_TIME)
                end = dt.datetime.strptime(params["condition.EndTime"], _DAHUA_TIME)
                ch = n.get_channel(channel + 1)
                segs = generate_segments(ch.archive, start, end) if ch else []
                app.state.find_objects[object] = {"segments": segs, "cursor": 0}
                return _txt("OK" if segs else "found=0")

            if action == "findNextFile":
                store = app.state.find_objects.get(object, {"segments": [], "cursor": 0})
                count = int(params.get("count", 100))
                segs = store["segments"][store["cursor"]: store["cursor"] + count]
                store["cursor"] += len(segs)
                lines = [f"found={len(segs)}"]
                for i, (s, e) in enumerate(segs):
                    lines += [
                        f"items[{i}].Channel={params.get('Channel', 0)}",
                        f"items[{i}].StartTime={s.strftime(_DAHUA_TIME)}",
                        f"items[{i}].EndTime={e.strftime(_DAHUA_TIME)}",
                        f"items[{i}].Type=dav",
                    ]
                return _txt("\n".join(lines))

            if action == "destroy":
                app.state.find_objects.pop(object, None)
                return _txt("OK")

            return Response(status_code=400)

        @app.get("/cgi-bin/global.cgi")
        async def global_cgi(request: Request, action: str = ""):
            n = state()
            require_auth(request, n.username, n.password)
            return _txt(f"result={n.device_now().strftime(_DAHUA_TIME)}")

    return app


# Дефолтное приложение для дев-режима / standalone-запуска
mock_app = make_mock_app(MockNVR.default(channels=4, analog=2), profile="both")


if __name__ == "__main__":  # pragma: no cover
    import os

    import uvicorn

    profile = os.getenv("MOCK_PROFILE", "both")
    app_to_run = make_mock_app(MockNVR.default(channels=4, analog=2), profile=profile)
    uvicorn.run(app_to_run, host="0.0.0.0", port=int(os.getenv("MOCK_PORT", "8088")))
