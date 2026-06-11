"""REST API: устройства, группы, тест соединения, автоопределение, опрос."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, schemas
from app.database import get_session
from app.drivers import build_client, detect_api_type
from app.drivers.base import NVRError
from app.models import ApiType, Device
from app.services import archive, poller

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["devices"])


# ── Группы ────────────────────────────────────────────────────────────────────
@router.get("/groups", response_model=list[schemas.GroupOut])
async def get_groups(session: AsyncSession = Depends(get_session)):
    return await crud.list_groups(session)


@router.post("/groups", response_model=schemas.GroupOut)
async def post_group(data: schemas.GroupCreate, session: AsyncSession = Depends(get_session)):
    return await crud.create_group(session, data)


# ── Устройства ──────────────────────────────────────────────────────────────────
@router.get("/devices", response_model=list[schemas.DeviceOut])
async def get_devices(session: AsyncSession = Depends(get_session)):
    return await crud.list_devices(session)


@router.get("/devices/{device_id}", response_model=schemas.DeviceDetail)
async def get_device(device_id: int, session: AsyncSession = Depends(get_session)):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    return device


@router.post("/devices", response_model=schemas.DeviceDetail)
async def post_device(data: schemas.DeviceCreate, session: AsyncSession = Depends(get_session)):
    # Автоопределение типа при необходимости + capability-check
    if data.api_type in (ApiType.AUTO, "", None):
        result = await detect_api_type(
            data.host, data.http_port, data.username, data.password,
            use_https=data.use_https, timeout=data.timeout,
        )
        data.api_type = result.api_type
        data.auth_scheme = result.auth_scheme

    device = await crud.create_device(session, data)
    await _enrich_device(session, device)
    await session.refresh(device)
    return await crud.get_device(session, device.id)


@router.put("/devices/{device_id}", response_model=schemas.DeviceDetail)
async def put_device(
    device_id: int, data: schemas.DeviceUpdate, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    await crud.update_device(session, device, data)
    return await crud.get_device(session, device_id)


@router.delete("/devices/{device_id}")
async def remove_device(device_id: int, session: AsyncSession = Depends(get_session)):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    await crud.delete_device(session, device)
    return {"ok": True}


# ── Тест соединения / автоопределение ──────────────────────────────────────────
@router.post("/devices/test", response_model=schemas.TestConnectionResult)
async def test_connection(data: schemas.TestConnectionRequest):
    api_type = data.api_type
    auth_scheme = data.auth_scheme
    detail = ""

    if api_type in (ApiType.AUTO, "", None):
        det = await detect_api_type(
            data.host, data.http_port, data.username, data.password,
            use_https=data.use_https, timeout=data.timeout,
        )
        api_type, auth_scheme, detail = det.api_type, det.auth_scheme, det.detail
        if api_type == ApiType.UNKNOWN:
            return schemas.TestConnectionResult(
                ok=False, api_type=api_type, auth_scheme=auth_scheme, detail=detail
            )

    # Временное устройство (без сохранения) для теста + probe
    tmp = Device(
        name="test", host=data.host, http_port=data.http_port, use_https=data.use_https,
        username=data.username, api_type=api_type, auth_scheme=auth_scheme,
        timeout=data.timeout, retries=1,
    )
    client = build_client(tmp, password=data.password)
    try:
        info = await client.test_connection()
        caps = await client.probe_capabilities()
        return schemas.TestConnectionResult(
            ok=True, api_type=api_type, auth_scheme=auth_scheme,
            model=info.model, firmware=info.firmware, serial=info.serial,
            capabilities=caps.as_dict(), detail=detail or "соединение успешно",
        )
    except NVRError as exc:
        return schemas.TestConnectionResult(
            ok=False, api_type=api_type, auth_scheme=auth_scheme, detail=str(exc)
        )


# ── Ручной запуск опроса / проверки архива ──────────────────────────────────────
@router.post("/devices/{device_id}/poll")
async def poll_now(device_id: int, session: AsyncSession = Depends(get_session)):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    await poller.poll_device(device_id)
    return {"ok": True}


@router.get("/devices/{device_id}/channels/{channel_id}/snapshot")
async def channel_snapshot(
    device_id: int, channel_id: int, session: AsyncSession = Depends(get_session)
):
    """Текущий кадр канала (JPEG)."""
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    client = build_client(device)
    try:
        data = await client.get_snapshot(channel_id)
    except NVRError as exc:
        raise HTTPException(502, f"Снимок недоступен: {exc}")
    return Response(content=data, media_type="image/jpeg")


@router.post("/devices/{device_id}/sync-time")
async def sync_time(device_id: int, session: AsyncSession = Depends(get_session)):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    client = build_client(device)
    try:
        await client.sync_time()
    except NVRError as exc:
        raise HTTPException(502, f"Не удалось синхронизировать время: {exc}")
    # сразу пересчитаем дрейф
    await poller.poll_device(device_id)
    return {"ok": True}


@router.post("/devices/{device_id}/reboot")
async def reboot_device(device_id: int, session: AsyncSession = Depends(get_session)):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    client = build_client(device)
    try:
        await client.reboot()
    except NVRError as exc:
        raise HTTPException(502, f"Не удалось перезагрузить: {exc}")
    return {"ok": True}


@router.post("/devices/{device_id}/recheck", response_model=schemas.DeviceDetail)
async def recheck(device_id: int, session: AsyncSession = Depends(get_session)):
    """Заново тянет модель/прошивку и перепроверяет возможности устройства."""
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    await _enrich_device(session, device)
    return await crud.get_device(session, device_id)


@router.post("/devices/{device_id}/archive-check")
async def archive_check_now(
    device_id: int, day: str | None = None, session: AsyncSession = Depends(get_session)
):
    import datetime as dt

    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    target = (
        dt.date.fromisoformat(day) if day else dt.date.today() - dt.timedelta(days=1)
    )
    await archive.check_device_archive(device_id, target)
    return {"ok": True, "day": target.isoformat()}


# ── Вспомогательное: обогащение устройства инфо + capabilities ──────────────────
async def _enrich_device(session: AsyncSession, device: Device) -> None:
    """После создания: тянем модель/прошивку и проверяем возможности."""
    if device.api_type == ApiType.UNKNOWN:
        return
    client = build_client(device)
    try:
        info = await client.test_connection()
        device.model = info.model
        device.firmware = info.firmware
        device.serial = info.serial
    except NVRError as exc:
        device.last_error = str(exc)
        log.warning("Не удалось получить инфо устройства %s: %s", device.id, exc)
    try:
        caps = await client.probe_capabilities()
        device.capabilities = caps.as_dict()
    except NVRError as exc:
        log.warning("Capability-check %s не выполнен: %s", device.id, exc)
    await session.commit()
