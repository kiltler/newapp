"""Модуль «Инвентарь»: API активов, точек, расходников, ЗиПа, экономики.

Неймспейс /api/inventory/* и право `inventory` (реестр permissions). Секрет
`meta.snmp_community` хранится шифрованным (crypto), в выдаче маскируется.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.crypto import encrypt
from app.database import get_session
from app.models import (
    InvAsset,
    InvAssetStatus,
    InvAssetType,
    InvConsumableEvent,
    InvConsumableKind,
    InvConsumableModel,
    InvConsumableStock,
    InvMovement,
    InvService,
    Location,
)
from app.services import audit, inventory

BASE_DIR = Path(__file__).resolve().parent.parent
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from app.models import INV_CONSUMABLE_KIND_NAMES, InvConsumableEventType  # noqa: E402
from app.templatefilters import register as _register_filters  # noqa: E402

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
_register_filters(templates)

router = APIRouter(tags=["inventory"])

_STATUS_NAMES = {
    InvAssetStatus.installed: "работает", InvAssetStatus.reserve: "в ЗиПе",
    InvAssetStatus.at_service: "в ремонте", InvAssetStatus.broken: "неисправен",
    InvAssetStatus.written_off: "списан",
}
_TYPE_NAMES = {InvAssetType.printer: "принтер", InvAssetType.kkt: "ККТ",
               InvAssetType.ups: "ИБП", InvAssetType.other: "прочее"}


# ── Сериализация ─────────────────────────────────────────────────────────────
def _safe_meta(meta: dict | None) -> dict:
    """Копия meta без утечки secret: snmp_community → флаг has_snmp_community."""
    meta = dict(meta or {})
    if "snmp_community" in meta:
        meta["has_snmp_community"] = bool(meta.pop("snmp_community"))
    return meta


def _asset_dict(a: InvAsset, *, in_transit: bool = False) -> dict:
    return {
        "id": a.id, "type": a.type, "model": a.model, "serial": a.serial,
        "inv_number": a.inv_number, "name": a.name, "location_id": a.location_id,
        "status": a.status, "responsible": a.responsible,
        "commissioned_at": a.commissioned_at.isoformat() if a.commissioned_at else None,
        "note": a.note, "meta": _safe_meta(a.meta), "in_transit": in_transit,
    }


def _merge_asset_meta(existing: dict | None, incoming: dict | None) -> dict:
    """Слить meta: секрет snmp_community шифруем; пустой не затирает старый."""
    out = dict(existing or {})
    inc = dict(incoming or {})
    sc = inc.pop("snmp_community", None)
    out.update(inc)
    if sc:  # непустой новый community — шифруем
        out["snmp_community"] = sc if str(sc).startswith("enc:") else encrypt(str(sc))
    return out


# ── Точки (справочник) ───────────────────────────────────────────────────────
@router.get("/api/inventory/locations")
async def list_locations(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Location).order_by(Location.name))).scalars().all()
    return [{"id": l.id, "name": l.name, "kind": l.kind, "address": l.address,
             "note": l.note, "active": l.active} for l in rows]


@router.post("/api/inventory/locations")
async def create_location(data: schemas.LocationIn, request: Request,
                          session: AsyncSession = Depends(get_session)):
    loc = Location(name=data.name.strip(), kind=data.kind, address=data.address,
                   note=data.note, active=data.active)
    session.add(loc)
    await session.flush()
    await audit.log_action(session, request, "inv_location_create", target=data.name)
    return {"ok": True, "id": loc.id}


@router.post("/api/inventory/locations/{loc_id}")
async def update_location(loc_id: int, data: schemas.LocationIn, request: Request,
                          session: AsyncSession = Depends(get_session)):
    loc = await session.get(Location, loc_id)
    if loc is None:
        raise HTTPException(404, "Точка не найдена")
    loc.name, loc.kind, loc.address = data.name.strip(), data.kind, data.address
    loc.note, loc.active = data.note, data.active
    await audit.log_action(session, request, "inv_location_update", target=str(loc_id))
    await session.commit()
    return {"ok": True}


# ── Активы ───────────────────────────────────────────────────────────────────
@router.get("/api/inventory/assets")
async def list_assets(
    type: str | None = None, location_id: int | None = None, status: str | None = None,
    model: str | None = None, q: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(InvAsset)
    if type:
        stmt = stmt.where(InvAsset.type == type)
    if location_id is not None:
        stmt = stmt.where(InvAsset.location_id == location_id)
    if status:
        stmt = stmt.where(InvAsset.status == status)
    if model:
        stmt = stmt.where(InvAsset.model.ilike(f"%{model}%"))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(InvAsset.serial.ilike(like) | InvAsset.inv_number.ilike(like)
                          | InvAsset.name.ilike(like))
    assets = (await session.execute(stmt.order_by(InvAsset.id.desc()))).scalars().all()
    # какие активы «в пути» — есть незакрытое движение
    open_ids = set((await session.execute(
        select(InvMovement.asset_id).where(InvMovement.received_at.is_(None))
    )).scalars())
    return [_asset_dict(a, in_transit=a.id in open_ids) for a in assets]


@router.post("/api/inventory/assets")
async def create_asset(data: schemas.InvAssetIn, request: Request,
                       session: AsyncSession = Depends(get_session)):
    if data.type not in InvAssetType.ALL:
        raise HTTPException(422, "Недопустимый тип актива")
    if data.status not in InvAssetStatus.ALL:
        raise HTTPException(422, "Недопустимый статус")
    a = InvAsset(
        type=data.type, model=data.model, serial=data.serial, inv_number=data.inv_number,
        name=data.name, location_id=data.location_id, status=data.status,
        responsible=data.responsible, commissioned_at=data.commissioned_at, note=data.note,
        meta=_merge_asset_meta({}, data.meta),
    )
    session.add(a)
    await session.flush()
    await audit.log_action(session, request, "inv_asset_create",
                           target=f"{data.type} {data.model or ''} {data.serial or ''}".strip())
    return {"ok": True, "id": a.id}


@router.get("/api/inventory/assets/{asset_id}")
async def get_asset(asset_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(InvAsset, asset_id)
    if a is None:
        raise HTTPException(404, "Актив не найден")
    movements = (await session.execute(
        select(InvMovement).where(InvMovement.asset_id == asset_id)
        .order_by(InvMovement.id.desc())
    )).scalars().all()
    services = (await session.execute(
        select(InvService).where(InvService.asset_id == asset_id).order_by(InvService.id.desc())
    )).scalars().all()
    consumables = (await session.execute(
        select(InvConsumableEvent).where(InvConsumableEvent.asset_id == asset_id)
        .order_by(InvConsumableEvent.id.desc())
    )).scalars().all()
    in_transit = any(m.received_at is None for m in movements)
    return {
        "asset": _asset_dict(a, in_transit=in_transit),
        "movements": [{"id": m.id, "from_location_id": m.from_location_id,
                       "to_location_id": m.to_location_id,
                       "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                       "received_at": m.received_at.isoformat() if m.received_at else None,
                       "carrier": m.carrier, "sent_by": m.sent_by, "received_by": m.received_by,
                       "note": m.note} for m in movements],
        "services": [{"id": s.id, "vendor": s.vendor, "issue": s.issue,
                      "sent_at": s.sent_at.isoformat() if s.sent_at else None,
                      "promised_at": s.promised_at.isoformat() if s.promised_at else None,
                      "returned_at": s.returned_at.isoformat() if s.returned_at else None,
                      "cost": s.cost, "result": s.result} for s in services],
        "consumables": [{"id": e.id, "consumable_model_id": e.consumable_model_id,
                         "event_type": e.event_type,
                         "at": e.at.isoformat() if e.at else None,
                         "counter_at": e.counter_at, "by_user": e.by_user} for e in consumables],
    }


@router.post("/api/inventory/assets/{asset_id}")
async def update_asset(asset_id: int, data: schemas.InvAssetIn, request: Request,
                       session: AsyncSession = Depends(get_session)):
    a = await session.get(InvAsset, asset_id)
    if a is None:
        raise HTTPException(404, "Актив не найден")
    if data.status not in InvAssetStatus.ALL:
        raise HTTPException(422, "Недопустимый статус")
    a.type, a.model, a.serial = data.type, data.model, data.serial
    a.inv_number, a.name, a.location_id = data.inv_number, data.name, data.location_id
    a.status, a.responsible = data.status, data.responsible
    a.commissioned_at, a.note = data.commissioned_at, data.note
    a.meta = _merge_asset_meta(a.meta, data.meta)
    await audit.log_action(session, request, "inv_asset_update", target=str(asset_id))
    await session.commit()
    return {"ok": True}


@router.delete("/api/inventory/assets/{asset_id}")
async def delete_asset(asset_id: int, request: Request,
                       session: AsyncSession = Depends(get_session)):
    a = await session.get(InvAsset, asset_id)
    if a is None:
        raise HTTPException(404, "Актив не найден")
    # Защита: не удаляем «живой» актив — только списанный (архив истории через списание)
    if a.status != InvAssetStatus.written_off:
        raise HTTPException(409, "Удалять можно только списанный актив (status=written_off). "
                                 "Сначала смените статус на «списан».")
    await session.delete(a)
    await audit.log_action(session, request, "inv_asset_delete", target=str(asset_id))
    await session.commit()
    return {"ok": True}


# ── Действия над активом ─────────────────────────────────────────────────────
def _err(exc: inventory.InventoryError) -> HTTPException:
    return HTTPException(400, str(exc))


@router.post("/api/inventory/assets/{asset_id}/move")
async def move(asset_id: int, data: schemas.InvMoveIn, request: Request,
               session: AsyncSession = Depends(get_session)):
    try:
        mv = await inventory.move_asset(
            session, asset_id, data.to_location_id, sent_at=data.sent_at,
            carrier=data.carrier, sent_by=data.sent_by, note=data.note, request=request)
    except inventory.InventoryError as e:
        raise _err(e)
    await session.commit()
    return {"ok": True, "movement_id": mv.id}


@router.post("/api/inventory/movements/{movement_id}/receive")
async def receive(movement_id: int, data: schemas.InvReceiveIn, request: Request,
                  session: AsyncSession = Depends(get_session)):
    try:
        await inventory.receive_asset(session, movement_id, received_at=data.received_at,
                                      received_by=data.received_by, request=request)
    except inventory.InventoryError as e:
        raise _err(e)
    await session.commit()
    return {"ok": True}


@router.post("/api/inventory/assets/{asset_id}/service")
async def service(asset_id: int, data: schemas.InvServiceIn, request: Request,
                  session: AsyncSession = Depends(get_session)):
    try:
        svc = await inventory.send_to_service(
            session, asset_id, vendor=data.vendor, issue=data.issue,
            sent_at=data.sent_at, promised_at=data.promised_at, request=request)
    except inventory.InventoryError as e:
        raise _err(e)
    await session.commit()
    return {"ok": True, "service_id": svc.id}


@router.post("/api/inventory/services/{service_id}/return")
async def service_return(service_id: int, data: schemas.InvServiceReturnIn, request: Request,
                         session: AsyncSession = Depends(get_session)):
    try:
        await inventory.return_from_service(
            session, service_id, returned_at=data.returned_at, cost=data.cost,
            result=data.result, to_status=data.to_status, request=request)
    except inventory.InventoryError as e:
        raise _err(e)
    await session.commit()
    return {"ok": True}


@router.post("/api/inventory/assets/{asset_id}/consumable")
async def install_consumable(asset_id: int, data: schemas.InvInstallConsumableIn, request: Request,
                             session: AsyncSession = Depends(get_session)):
    try:
        ev = await inventory.install_consumable(
            session, asset_id, data.consumable_model_id, at=data.at,
            counter_at=data.counter_at, by_user=data.by_user, request=request)
    except inventory.InventoryError as e:
        raise _err(e)
    await session.commit()
    return {"ok": True, "event_id": ev.id}


# ── SKU расходников ──────────────────────────────────────────────────────────
@router.get("/api/inventory/consumable-models")
async def list_consumable_models(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(InvConsumableModel).order_by(InvConsumableModel.model))).scalars().all()
    return [{"id": m.id, "kind": m.kind, "model": m.model,
             "compatible_with": m.compatible_with or [], "price_new": m.price_new,
             "price_refill": m.price_refill, "resource_pages": m.resource_pages,
             "active": m.active, "note": m.note} for m in rows]


@router.post("/api/inventory/consumable-models")
async def create_consumable_model(data: schemas.InvConsumableModelIn, request: Request,
                                  session: AsyncSession = Depends(get_session)):
    if data.kind not in InvConsumableKind.ALL:
        raise HTTPException(422, "Недопустимый вид расходника")
    m = InvConsumableModel(
        kind=data.kind, model=data.model.strip(), compatible_with=data.compatible_with,
        price_new=data.price_new, price_refill=data.price_refill,
        resource_pages=data.resource_pages, active=data.active, note=data.note)
    session.add(m)
    await session.flush()
    await audit.log_action(session, request, "inv_consumable_model_create", target=data.model)
    return {"ok": True, "id": m.id}


@router.post("/api/inventory/consumable-models/{model_id}")
async def update_consumable_model(model_id: int, data: schemas.InvConsumableModelIn, request: Request,
                                  session: AsyncSession = Depends(get_session)):
    m = await session.get(InvConsumableModel, model_id)
    if m is None:
        raise HTTPException(404, "SKU не найден")
    m.kind, m.model, m.compatible_with = data.kind, data.model.strip(), data.compatible_with
    m.price_new, m.price_refill = data.price_new, data.price_refill
    m.resource_pages, m.active, m.note = data.resource_pages, data.active, data.note
    await audit.log_action(session, request, "inv_consumable_model_update", target=str(model_id))
    await session.commit()
    return {"ok": True}


# ── ЗиП / склад ──────────────────────────────────────────────────────────────
@router.get("/api/inventory/stock")
async def stock(location_id: int | None = None, session: AsyncSession = Depends(get_session)):
    return await inventory.stock_summary(session, location_id)


@router.post("/api/inventory/stock/receive")
async def stock_receive(data: schemas.InvStockReceiveIn, request: Request,
                        session: AsyncSession = Depends(get_session)):
    try:
        await inventory.receive_consumable(
            session, data.consumable_model_id, data.location_id, qty=data.qty,
            cost=data.cost, at=data.at, by_user=data.by_user, request=request)
    except inventory.InventoryError as e:
        raise _err(e)
    await session.commit()
    return {"ok": True}


@router.post("/api/inventory/stock/refill/send")
async def refill_send(data: schemas.InvRefillSendIn, request: Request,
                      session: AsyncSession = Depends(get_session)):
    await inventory.send_consumable_to_refill(
        session, data.consumable_model_id, data.location_id, qty=data.qty,
        at=data.at, by_user=data.by_user, request=request)
    await session.commit()
    return {"ok": True}


@router.post("/api/inventory/stock/refill/return")
async def refill_return(data: schemas.InvRefillReturnIn, request: Request,
                        session: AsyncSession = Depends(get_session)):
    try:
        await inventory.return_from_refill(
            session, data.consumable_model_id, data.location_id, qty=data.qty,
            cost=data.cost, at=data.at, by_user=data.by_user, request=request)
    except inventory.InventoryError as e:
        raise _err(e)
    await session.commit()
    return {"ok": True}


@router.get("/api/inventory/report")
async def report(date_from: str | None = None, date_to: str | None = None,
                 session: AsyncSession = Depends(get_session)):
    df = dt.datetime.fromisoformat(date_from) if date_from else None
    dtu = dt.datetime.fromisoformat(date_to) if date_to else None
    return await inventory.refill_economy(session, date_from=df, date_to=dtu)


# ── HTML-страницы ────────────────────────────────────────────────────────────
async def _locations(session: AsyncSession) -> list[Location]:
    return list((await session.execute(select(Location).order_by(Location.name))).scalars())


@router.get("/inventory", response_class=HTMLResponse)
async def page_list(request: Request, type: str | None = None, location_id: int | None = None,
                    status: str | None = None, model: str | None = None, q: str | None = None,
                    session: AsyncSession = Depends(get_session)):
    stmt = select(InvAsset)
    if type:
        stmt = stmt.where(InvAsset.type == type)
    if location_id is not None:
        stmt = stmt.where(InvAsset.location_id == location_id)
    if status:
        stmt = stmt.where(InvAsset.status == status)
    if model:
        stmt = stmt.where(InvAsset.model.ilike(f"%{model}%"))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(InvAsset.serial.ilike(like) | InvAsset.inv_number.ilike(like)
                          | InvAsset.name.ilike(like))
    assets = list((await session.execute(stmt.order_by(InvAsset.id.desc()))).scalars())
    open_ids = set((await session.execute(
        select(InvMovement.asset_id).where(InvMovement.received_at.is_(None)))).scalars())
    locs = await _locations(session)
    loc_names = {l.id: l.name for l in locs}
    return templates.TemplateResponse("inventory_list.html", {
        "request": request, "assets": assets, "open_ids": open_ids, "loc_names": loc_names,
        "locations": locs, "status_names": _STATUS_NAMES, "type_names": _TYPE_NAMES,
        "f": {"type": type or "", "location_id": location_id, "status": status or "",
              "model": model or "", "q": q or ""},
    })


@router.get("/inventory/stock", response_class=HTMLResponse)
async def page_stock(request: Request, location_id: int | None = None,
                     session: AsyncSession = Depends(get_session)):
    summary = await inventory.stock_summary(session, location_id)
    locs = await _locations(session)
    models = list((await session.execute(
        select(InvConsumableModel).where(InvConsumableModel.active.is_(True))
        .order_by(InvConsumableModel.model))).scalars())
    return templates.TemplateResponse("inventory_stock.html", {
        "request": request, "summary": summary, "locations": locs, "models": models,
        "kind_names": INV_CONSUMABLE_KIND_NAMES, "type_names": _TYPE_NAMES,
        "location_id": location_id,
    })


@router.get("/inventory/locations", response_class=HTMLResponse)
async def page_locations(request: Request, session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse("inventory_locations.html", {
        "request": request, "locations": await _locations(session),
    })


@router.get("/inventory/consumables", response_class=HTMLResponse)
async def page_consumables(request: Request, session: AsyncSession = Depends(get_session)):
    models = list((await session.execute(
        select(InvConsumableModel).order_by(InvConsumableModel.model))).scalars())
    return templates.TemplateResponse("inventory_consumables.html", {
        "request": request, "models": models, "kind_names": INV_CONSUMABLE_KIND_NAMES,
    })


@router.get("/inventory/report", response_class=HTMLResponse)
async def page_report(request: Request, date_from: str | None = None, date_to: str | None = None,
                      session: AsyncSession = Depends(get_session)):
    df = dt.datetime.fromisoformat(date_from) if date_from else None
    dtu = dt.datetime.fromisoformat(date_to) if date_to else None
    econ = await inventory.refill_economy(session, date_from=df, date_to=dtu)
    return templates.TemplateResponse("inventory_report.html", {
        "request": request, "econ": econ, "date_from": date_from or "", "date_to": date_to or "",
    })


@router.get("/inventory/{asset_id}", response_class=HTMLResponse)
async def page_asset(asset_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    a = await session.get(InvAsset, asset_id)
    if a is None:
        raise HTTPException(404, "Актив не найден")
    movements = list((await session.execute(
        select(InvMovement).where(InvMovement.asset_id == asset_id)
        .order_by(InvMovement.id.desc()))).scalars())
    services = list((await session.execute(
        select(InvService).where(InvService.asset_id == asset_id)
        .order_by(InvService.id.desc()))).scalars())
    consumables = list((await session.execute(
        select(InvConsumableEvent).where(InvConsumableEvent.asset_id == asset_id)
        .order_by(InvConsumableEvent.id.desc()))).scalars())
    locs = await _locations(session)
    loc_names = {l.id: l.name for l in locs}
    cmodels = list((await session.execute(
        select(InvConsumableModel).where(InvConsumableModel.active.is_(True)))).scalars())
    return templates.TemplateResponse("inventory_asset.html", {
        "request": request, "a": a, "meta": _safe_meta(a.meta),
        "movements": movements, "services": services, "consumables": consumables,
        "in_transit": any(m.received_at is None for m in movements),
        "locations": locs, "loc_names": loc_names, "cmodels": cmodels,
        "status_names": _STATUS_NAMES, "type_names": _TYPE_NAMES,
        "event_names": {InvConsumableEventType.installed: "установлен",
                        InvConsumableEventType.removed: "снят"},
    })
