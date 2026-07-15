"""SNMP-поллер принтеров (модуль «Инвентарь»).

Опрашивает сетевые принтеры (`InvAsset.type == printer` с `meta.snmp_enabled` и
`meta.ip`) по SNMP и обновляет `meta.last_counters` (страницы, тонер %, бункер %)
+ `meta.last_poll_at`. Пустые серийник/модель дозаполняет.

Транспорт — net-snmp (`snmpget`/`snmpwalk`) через subprocess (как ffmpeg в
«Заселениях»): нулевые Python-зависимости, пакет `snmp` ставится в Docker-образ.
Реальные принтеры для тестов не нужны — `read_printer_snmp` подменяется моком.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from app.config import settings
from app.crypto import decrypt
from app.database import SessionLocal
from app.models import InvAsset, InvAssetType, utcnow

log = logging.getLogger(__name__)

# OID'ы (Printer-MIB / Host-Resources / system)
_OID_SYSDESCR = ".1.3.6.1.2.1.1.1.0"                 # модель/описание
_OID_SERIAL = ".1.3.6.1.2.1.43.5.1.1.17.1"           # prtGeneralSerialNumber
_OID_PAGES = ".1.3.6.1.2.1.43.10.2.1.4.1.1"          # prtMarkerLifeCount (страниц)
_OID_SUP_DESC = ".1.3.6.1.2.1.43.11.1.1.6"           # prtMarkerSuppliesDescription
_OID_SUP_MAX = ".1.3.6.1.2.1.43.11.1.1.8"            # prtMarkerSuppliesMaxCapacity
_OID_SUP_LVL = ".1.3.6.1.2.1.43.11.1.1.9"            # prtMarkerSuppliesLevel

_semaphore = asyncio.Semaphore(settings.max_concurrent_polls)


@dataclass
class PrinterSnmp:
    ok: bool = False
    error: str | None = None
    model: str | None = None
    serial: str | None = None
    pages: int | None = None
    toner_pct: int | None = None
    waste_pct: int | None = None
    supplies: dict = field(default_factory=dict)   # описание расходника → %


def _clean(v: str | None) -> str | None:
    v = (v or "").strip().strip('"')
    return v or None


def _to_int(v: str | None) -> int | None:
    try:
        return int(_clean(v))
    except (TypeError, ValueError):
        return None


async def _snmp(cmd: list[str], timeout: float) -> str | None:
    """Запускает net-snmp-утилиту, возвращает stdout или None (ошибка/таймаут)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        log.warning("net-snmp не установлен (пакет snmp) — SNMP-опрос невозможен")
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace")


async def _walk_map(base: list[str], target: str, oid: str, timeout: float) -> dict[str, str]:
    """snmpwalk колонки таблицы → {индекс: значение}. `-Oqn` = числовой OID + значение."""
    out = await _snmp(["snmpwalk", *base, "-Oqn", target, oid], timeout + 2)
    res: dict[str, str] = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith(oid + "."):
            continue
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        idx = parts[0][len(oid) + 1:]
        res[idx] = parts[1].strip().strip('"')
    return res


async def read_printer_snmp(ip: str, community: str, *, port: int = 161, timeout: float = 2.0) -> PrinterSnmp:
    """Читает у принтера модель/серийник/страницы/тонер/бункер. Ошибка → ok=False."""
    target = f"{ip}:{port}"
    base = ["-v2c", "-c", community, "-t", str(int(max(timeout, 1))), "-r", "0"]

    # sysDescr первым — заодно проверка связи
    out = await _snmp(["snmpget", *base, "-Ovq", target, _OID_SYSDESCR], timeout + 1)
    if out is None:
        return PrinterSnmp(ok=False, error="нет ответа по SNMP (таймаут/выключен/community)")
    res = PrinterSnmp(ok=True, model=_clean(out))

    serial = _clean(await _snmp(["snmpget", *base, "-Ovq", target, _OID_SERIAL], timeout + 1))
    res.serial = None if serial in (None, "?") else serial
    res.pages = _to_int(await _snmp(["snmpget", *base, "-Ovq", target, _OID_PAGES], timeout + 1))

    descs = await _walk_map(base, target, _OID_SUP_DESC, timeout)
    maxs = await _walk_map(base, target, _OID_SUP_MAX, timeout)
    lvls = await _walk_map(base, target, _OID_SUP_LVL, timeout)
    for idx, desc in descs.items():
        mx, lv = _to_int(maxs.get(idx)), _to_int(lvls.get(idx))
        if not mx or mx <= 0 or lv is None or lv < 0:   # -2/-3 = «неизвестно»
            continue
        pct = max(0, min(100, round(lv * 100 / mx)))
        res.supplies[desc] = pct
        d = desc.lower()
        if any(k in d for k in ("waste", "отраб", "бункер")):
            res.waste_pct = pct
        elif any(k in d for k in ("toner", "тонер", "black", "чёрн", "чер")):
            if res.toner_pct is None:
                res.toner_pct = pct
    return res


def _community(meta: dict) -> str:
    c = meta.get("snmp_community")
    return (decrypt(c) or settings.snmp_default_community) if c else settings.snmp_default_community


async def poll_printer(session, asset_id: int) -> None:
    """Опрос одного принтера + запись в meta. Недоступность → meta.last_error."""
    a = await session.get(InvAsset, asset_id)
    if a is None or a.type != InvAssetType.printer:
        return
    meta = dict(a.meta or {})
    if not meta.get("snmp_enabled") or not meta.get("ip"):
        return
    async with _semaphore:
        res = await read_printer_snmp(meta["ip"], _community(meta),
                                      port=int(meta.get("snmp_port") or 161))
    meta["last_poll_at"] = utcnow().isoformat()
    if not res.ok:
        meta["last_error"] = res.error
        a.meta = meta
        return
    meta.pop("last_error", None)
    counters = dict(meta.get("last_counters") or {})
    if res.pages is not None:
        counters["pages"] = res.pages
    if res.toner_pct is not None:
        counters["toner_pct"] = res.toner_pct
    if res.waste_pct is not None:
        counters["bunker_pct"] = res.waste_pct
    if res.supplies:
        counters["supplies"] = res.supplies
    meta["last_counters"] = counters
    a.meta = meta
    if res.model and not a.model:
        a.model = res.model
    if res.serial and not a.serial:
        a.serial = res.serial


async def poll_all_printers(session_factory=None) -> None:
    """Точка для APScheduler: опросить все включённые принтеры (семафор внутри)."""
    factory = session_factory or SessionLocal
    async with factory() as s:
        ids = list((await s.execute(
            select(InvAsset.id).where(InvAsset.type == InvAssetType.printer))).scalars())

    async def _one(aid: int) -> None:
        async with factory() as s:
            try:
                await poll_printer(s, aid)
                await s.commit()
            except Exception:  # noqa: BLE001 — сбой одного не рушит остальных
                log.exception("SNMP-опрос принтера %s сорвался", aid)

    await asyncio.gather(*(_one(i) for i in ids), return_exceptions=True)
