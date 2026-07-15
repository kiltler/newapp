"""Тесты SNMP-поллера принтеров. Реальные принтеры не нужны — read_printer_snmp
подменяется моком (как fetch_checkins в 1С)."""
from __future__ import annotations

from app.crypto import encrypt
from app.database import SessionLocal
from app.models import InvAsset
from app.services import snmp_poller
from app.services.snmp_poller import PrinterSnmp


async def _printer(s, *, meta) -> int:
    a = InvAsset(type="printer", model=None, serial=None, meta=meta)
    s.add(a)
    await s.flush()
    return a.id


async def test_poll_updates_meta_and_fills_model_serial(db, monkeypatch):
    async def fake(ip, community, *, port=161, timeout=2.0):
        return PrinterSnmp(ok=True, model="ECOSYS M2040dn", serial="VLL1234567",
                           pages=264612, toner_pct=42, waste_pct=10,
                           supplies={"Toner Black": 42, "Waste Toner Box": 10})
    monkeypatch.setattr(snmp_poller, "read_printer_snmp", fake)

    async with SessionLocal() as s:
        aid = await _printer(s, meta={"ip": "192.168.11.6", "snmp_enabled": True})
        await s.commit()
        await snmp_poller.poll_printer(s, aid)
        await s.commit()
        a = await s.get(InvAsset, aid)
        c = a.meta["last_counters"]
        assert c["pages"] == 264612 and c["toner_pct"] == 42 and c["bunker_pct"] == 10
        assert "last_poll_at" in a.meta and "last_error" not in a.meta
        assert a.model == "ECOSYS M2040dn" and a.serial == "VLL1234567"  # дозаполнили пустые


async def test_poll_records_error_on_failure(db, monkeypatch):
    async def fake(ip, community, *, port=161, timeout=2.0):
        return PrinterSnmp(ok=False, error="нет ответа по SNMP")
    monkeypatch.setattr(snmp_poller, "read_printer_snmp", fake)

    async with SessionLocal() as s:
        aid = await _printer(s, meta={"ip": "192.168.11.9", "snmp_enabled": True})
        await s.commit()
        await snmp_poller.poll_printer(s, aid)
        await s.commit()
        a = await s.get(InvAsset, aid)
        assert a.meta["last_error"] == "нет ответа по SNMP"
        assert "last_poll_at" in a.meta
        assert "last_counters" not in a.meta   # счётчики не трогали


async def test_poll_skips_disabled_or_no_ip(db, monkeypatch):
    calls = {"n": 0}

    async def fake(ip, community, *, port=161, timeout=2.0):
        calls["n"] += 1
        return PrinterSnmp(ok=True)
    monkeypatch.setattr(snmp_poller, "read_printer_snmp", fake)

    async with SessionLocal() as s:
        a_off = await _printer(s, meta={"ip": "192.168.11.6", "snmp_enabled": False})
        a_noip = await _printer(s, meta={"snmp_enabled": True})
        await s.commit()
        await snmp_poller.poll_printer(s, a_off)
        await snmp_poller.poll_printer(s, a_noip)
        await s.commit()
    assert calls["n"] == 0   # ни одного SNMP-запроса — оба пропущены


async def test_community_decrypted_from_meta(db, monkeypatch):
    seen = {}

    async def fake(ip, community, *, port=161, timeout=2.0):
        seen["community"] = community
        return PrinterSnmp(ok=True)
    monkeypatch.setattr(snmp_poller, "read_printer_snmp", fake)

    async with SessionLocal() as s:
        aid = await _printer(s, meta={"ip": "192.168.11.6", "snmp_enabled": True,
                                      "snmp_community": encrypt("sekret1")})
        await s.commit()
        await snmp_poller.poll_printer(s, aid)
        await s.commit()
    assert seen["community"] == "sekret1"   # расшифровали enc: перед запросом
