"""Бизнес-логика модуля «Инвентарь»: перемещения, ремонты, расходники, ЗиП.

Все изменяющие операции пишут аудит (`audit.log_action`). Времена-«человеческие»
(sent_at/received_at/at/…) — наивные локальные Хабаровска (§6.2), приходят из UI.
"""
from __future__ import annotations

import datetime as dt

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    InvAsset,
    InvAssetStatus,
    InvConsumableEvent,
    InvConsumableEventType,
    InvConsumableModel,
    InvConsumableStock,
    InvMovement,
    InvService,
    Location,
)
from app.services import audit


class InventoryError(Exception):
    """Бизнес-ошибка инвентаря (показывается пользователю понятным текстом)."""


# ── Массовый импорт активов (напр. парк принтеров из SNMP/PJL-скана) ─────────
async def bulk_import_assets(
    session: AsyncSession, *, type: str, location_id: int | None, rows: list[dict],
    request: Request | None = None,
) -> dict:
    """Создать/обновить активы пачкой. Идемпотентно по IP (meta.ip): повторный
    импорт того же парка не плодит дубли, а обновляет модель/счётчик.

    row: {model, serial, inv_number, ip, pages}. Пустые поля игнорируются.
    """
    from app.models import InvAsset  # локальный импорт — избегаем цикла на верхнем уровне

    existing = list((await session.execute(select(InvAsset).where(InvAsset.type == type))).scalars())
    by_ip = {(a.meta or {}).get("ip"): a for a in existing if (a.meta or {}).get("ip")}
    by_serial = {a.serial: a for a in existing if a.serial}

    created = updated = 0
    for row in rows:
        ip = (row.get("ip") or "").strip() or None
        serial = (row.get("serial") or "").strip() or None
        model = (row.get("model") or "").strip() or None
        inv_number = (row.get("inv_number") or "").strip() or None
        pages = row.get("pages")
        a = (by_ip.get(ip) if ip else None) or (by_serial.get(serial) if serial else None)
        if a is None:
            # живые принтеры из скана — «работает» (installed), не «в ЗиПе»
            a = InvAsset(type=type, status=InvAssetStatus.installed, location_id=location_id, meta={})
            session.add(a)
            created += 1
        else:
            updated += 1
        meta = dict(a.meta or {})
        if ip:
            meta["ip"] = ip
            by_ip[ip] = a
        if pages not in (None, ""):
            counters = dict(meta.get("last_counters") or {})
            counters["pages"] = int(pages)
            meta["last_counters"] = counters
        a.meta = meta
        if model:
            a.model = model
        if serial:
            a.serial = serial
            by_serial[serial] = a
        if inv_number:
            a.inv_number = inv_number
        if location_id is not None and a.location_id is None:
            a.location_id = location_id
    await audit.log_action(session, request, "inv_bulk_import",
                           target=f"{type}", detail=f"создано {created}, обновлено {updated}")
    return {"created": created, "updated": updated}


# ── Активы: перемещения и ремонты ────────────────────────────────────────────
async def move_asset(
    session: AsyncSession, asset_id: int, to_location_id: int, *,
    sent_at: dt.datetime | None = None, carrier: str | None = None,
    sent_by: str | None = None, note: str | None = None, request: Request | None = None,
) -> InvMovement:
    """Отправить актив на другую точку. Создаёт движение «в пути»
    (received_at=NULL) и НЕ меняет location_id до приёмки."""
    asset = await session.get(InvAsset, asset_id)
    if asset is None:
        raise InventoryError("актив не найден")
    mv = InvMovement(
        asset_id=asset_id, from_location_id=asset.location_id, to_location_id=to_location_id,
        sent_at=sent_at, carrier=carrier, sent_by=sent_by, note=note,
    )
    session.add(mv)
    await session.flush()
    await audit.log_action(session, request, "inv_move",
                           target=f"актив {asset_id}", detail=f"на точку {to_location_id}")
    return mv


async def receive_asset(
    session: AsyncSession, movement_id: int, *,
    received_at: dt.datetime | None = None, received_by: str | None = None,
    request: Request | None = None,
) -> InvMovement:
    """Принять актив: закрыть движение и ТОЛЬКО тогда переставить location_id."""
    mv = await session.get(InvMovement, movement_id)
    if mv is None:
        raise InventoryError("движение не найдено")
    if mv.received_at is not None:
        raise InventoryError("это движение уже принято")
    mv.received_at = received_at or dt.datetime.now()
    mv.received_by = received_by
    asset = await session.get(InvAsset, mv.asset_id)
    if asset is not None and mv.to_location_id is not None:
        asset.location_id = mv.to_location_id
    await audit.log_action(session, request, "inv_receive",
                           target=f"актив {mv.asset_id}", detail=f"точка {mv.to_location_id}")
    return mv


async def send_to_service(
    session: AsyncSession, asset_id: int, *, vendor: str | None = None,
    issue: str | None = None, sent_at: dt.datetime | None = None,
    promised_at: dt.datetime | None = None, request: Request | None = None,
) -> InvService:
    """Отправить актив в ремонт: запись InvService + статус at_service."""
    asset = await session.get(InvAsset, asset_id)
    if asset is None:
        raise InventoryError("актив не найден")
    svc = InvService(asset_id=asset_id, vendor=vendor, issue=issue,
                     sent_at=sent_at, promised_at=promised_at)
    session.add(svc)
    asset.status = InvAssetStatus.at_service
    await session.flush()
    await audit.log_action(session, request, "inv_service_send",
                           target=f"актив {asset_id}", detail=vendor)
    return svc


async def return_from_service(
    session: AsyncSession, service_id: int, *, returned_at: dt.datetime | None = None,
    cost: float | None = None, result: str | None = None,
    to_status: str = InvAssetStatus.reserve, request: Request | None = None,
) -> InvService:
    """Вернуть из ремонта: закрыть InvService, статус → reserve (по умолч.) или installed."""
    svc = await session.get(InvService, service_id)
    if svc is None:
        raise InventoryError("ремонт не найден")
    if to_status not in (InvAssetStatus.reserve, InvAssetStatus.installed, InvAssetStatus.broken):
        raise InventoryError("недопустимый статус возврата")
    svc.returned_at = returned_at or dt.datetime.now()
    svc.cost = cost
    svc.result = result
    asset = await session.get(InvAsset, svc.asset_id)
    if asset is not None:
        asset.status = to_status
    await audit.log_action(session, request, "inv_service_return",
                           target=f"актив {svc.asset_id}", detail=result)
    return svc


# ── Расходники: склад (ЗиП) и движения ───────────────────────────────────────
async def _get_stock(session: AsyncSession, model_id: int, location_id: int) -> InvConsumableStock | None:
    return (
        await session.execute(
            select(InvConsumableStock).where(
                InvConsumableStock.consumable_model_id == model_id,
                InvConsumableStock.location_id == location_id,
            )
        )
    ).scalar_one_or_none()


async def _add_stock(session: AsyncSession, model_id: int, location_id: int, delta: int) -> InvConsumableStock:
    stock = await _get_stock(session, model_id, location_id)
    if stock is None:
        stock = InvConsumableStock(consumable_model_id=model_id, location_id=location_id, qty=0)
        session.add(stock)
    stock.qty = (stock.qty or 0) + delta
    return stock


async def receive_consumable(
    session: AsyncSession, consumable_model_id: int, location_id: int, *,
    qty: int = 1, cost: float | None = None, at: dt.datetime | None = None,
    by_user: str | None = None, request: Request | None = None,
) -> InvConsumableStock:
    """Приход нового расходника на точку: qty += n, событие purchased."""
    if qty <= 0:
        raise InventoryError("количество должно быть больше нуля")
    stock = await _add_stock(session, consumable_model_id, location_id, qty)
    session.add(InvConsumableEvent(
        consumable_model_id=consumable_model_id, event_type=InvConsumableEventType.purchased,
        location_id=location_id, qty=qty, at=at, cost=cost, by_user=by_user,
    ))
    await audit.log_action(session, request, "inv_stock_receive",
                           target=f"расходник {consumable_model_id}", detail=f"+{qty} на точку {location_id}")
    return stock


async def install_consumable(
    session: AsyncSession, asset_id: int, consumable_model_id: int, *,
    at: dt.datetime | None = None, counter_at: int | None = None,
    by_user: str | None = None, request: Request | None = None,
) -> InvConsumableEvent:
    """Поставить расходник в принтер: событие installed + списание 1 со склада точки.

    Уход склада в минус ЗАПРЕЩЁН — при отсутствии остатка понятная ошибка.
    """
    asset = await session.get(InvAsset, asset_id)
    if asset is None:
        raise InventoryError("принтер не найден")
    if asset.location_id is None:
        raise InventoryError("у принтера не указана точка — некуда списывать расходник")
    stock = await _get_stock(session, consumable_model_id, asset.location_id)
    if stock is None or (stock.qty or 0) <= 0:
        raise InventoryError("на точке нет этого расходника в ЗиПе")
    stock.qty -= 1
    ev = InvConsumableEvent(
        consumable_model_id=consumable_model_id, event_type=InvConsumableEventType.installed,
        asset_id=asset_id, location_id=asset.location_id, qty=1,
        at=at, counter_at=counter_at, by_user=by_user,
    )
    session.add(ev)
    await session.flush()
    await audit.log_action(session, request, "inv_consumable_install",
                           target=f"принтер {asset_id}", detail=f"расходник {consumable_model_id}")
    return ev


async def send_consumable_to_refill(
    session: AsyncSession, consumable_model_id: int, location_id: int, *,
    qty: int = 1, at: dt.datetime | None = None, by_user: str | None = None,
    request: Request | None = None,
) -> InvConsumableEvent:
    """Отправить использованный расходник на заправку (событие, без изменения ЗиПа —
    отработанный картридж в ЗиПе не числится)."""
    ev = InvConsumableEvent(
        consumable_model_id=consumable_model_id, event_type=InvConsumableEventType.sent_to_refill,
        location_id=location_id, qty=qty, at=at, by_user=by_user,
    )
    session.add(ev)
    await session.flush()
    await audit.log_action(session, request, "inv_refill_send",
                           target=f"расходник {consumable_model_id}", detail=f"точка {location_id}")
    return ev


async def return_from_refill(
    session: AsyncSession, consumable_model_id: int, location_id: int, *,
    qty: int = 1, cost: float | None = None, at: dt.datetime | None = None,
    by_user: str | None = None, request: Request | None = None,
) -> InvConsumableStock:
    """Вернуть заправленный расходник в ЗиП: qty += n, событие returned_from_refill c ценой
    (источник данных для отчёта по экономике)."""
    if qty <= 0:
        raise InventoryError("количество должно быть больше нуля")
    stock = await _add_stock(session, consumable_model_id, location_id, qty)
    session.add(InvConsumableEvent(
        consumable_model_id=consumable_model_id, event_type=InvConsumableEventType.returned_from_refill,
        location_id=location_id, qty=qty, at=at, cost=cost, by_user=by_user,
    ))
    await audit.log_action(session, request, "inv_refill_return",
                           target=f"расходник {consumable_model_id}", detail=f"+{qty} на точку {location_id}, {cost or 0}₽")
    return stock


# ── ЗиП: сводка ───────────────────────────────────────────────────────────────
async def stock_summary(session: AsyncSession, location_id: int | None = None) -> dict:
    """Что в ЗиПе: остатки расходников (модель×точка) + запасные активы (reserve),
    сгруппированные по точке/типу/модели. Отвечает на «в ЗиПе 3 Kyocera?»."""
    loc_names = {
        loc.id: loc.name for loc in (await session.execute(select(Location))).scalars()
    }
    model_by_id = {
        m.id: m for m in (await session.execute(select(InvConsumableModel))).scalars()
    }

    # Остатки расходников (>0)
    stock_q = select(InvConsumableStock).where(InvConsumableStock.qty > 0)
    if location_id is not None:
        stock_q = stock_q.where(InvConsumableStock.location_id == location_id)
    consumables = []
    for st in (await session.execute(stock_q)).scalars():
        cm = model_by_id.get(st.consumable_model_id)
        consumables.append({
            "consumable_model_id": st.consumable_model_id,
            "model": cm.model if cm else None,
            "kind": cm.kind if cm else None,
            "location_id": st.location_id,
            "location_name": loc_names.get(st.location_id),
            "qty": st.qty,
        })

    # Запасные активы (reserve), группировка по (точка, тип, модель)
    spare_q = select(InvAsset).where(InvAsset.status == InvAssetStatus.reserve)
    if location_id is not None:
        spare_q = spare_q.where(InvAsset.location_id == location_id)
    groups: dict[tuple, int] = {}
    for a in (await session.execute(spare_q)).scalars():
        key = (a.location_id, a.type, a.model)
        groups[key] = groups.get(key, 0) + 1
    spare_assets = [
        {"location_id": loc, "location_name": loc_names.get(loc),
         "type": typ, "model": model, "count": cnt}
        for (loc, typ, model), cnt in sorted(groups.items(), key=lambda kv: (str(kv[0][2]), str(kv[0][0])))
    ]
    return {"consumables": consumables, "spare_assets": spare_assets}


# ── Отчёт по экономике заправок ──────────────────────────────────────────────
async def refill_economy(
    session: AsyncSession, *, date_from: dt.datetime | None = None, date_to: dt.datetime | None = None,
) -> dict:
    """За период: сумма заправок vs стоимость новых картриджей на то же количество.

    Дельта = сколько сэкономили (или переплатили) заправками. По моделям + итог.
    """
    q = select(InvConsumableEvent).where(
        InvConsumableEvent.event_type == InvConsumableEventType.returned_from_refill
    )
    if date_from is not None:
        q = q.where(InvConsumableEvent.at >= date_from)
    if date_to is not None:
        q = q.where(InvConsumableEvent.at <= date_to)
    events = list((await session.execute(q)).scalars())
    model_by_id = {
        m.id: m for m in (await session.execute(select(InvConsumableModel))).scalars()
    }

    by_model: dict[int, dict] = {}
    for ev in events:
        cm = model_by_id.get(ev.consumable_model_id)
        row = by_model.setdefault(ev.consumable_model_id, {
            "consumable_model_id": ev.consumable_model_id,
            "model": cm.model if cm else None,
            "qty": 0, "refill_cost": 0.0, "new_cost": 0.0,
        })
        qty = ev.qty or 1
        row["qty"] += qty
        row["refill_cost"] += (ev.cost or 0.0)
        row["new_cost"] += (cm.price_new or 0.0) * qty if cm else 0.0

    for row in by_model.values():
        row["saved"] = round(row["new_cost"] - row["refill_cost"], 2)

    total_refill = round(sum(r["refill_cost"] for r in by_model.values()), 2)
    total_new = round(sum(r["new_cost"] for r in by_model.values()), 2)
    return {
        "refills": sum(r["qty"] for r in by_model.values()),
        "total_refill_cost": total_refill,
        "total_new_cost": total_new,
        "saved": round(total_new - total_refill, 2),   # + сэкономили, − переплатили
        "by_model": sorted(by_model.values(), key=lambda r: str(r["model"])),
    }
