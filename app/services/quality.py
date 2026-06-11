"""Контроль качества картинки по снапшоту (компьютерное зрение, без ML).

Ловит проблемы, которые сам NVR не замечает («канал online», а толку нет):
тёмный/чёрный кадр, однотонный (залеплен/закрашен объектив), расфокус,
зависший поток (два одинаковых кадра подряд).
"""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass

import numpy as np
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal
from app.drivers import build_client
from app.drivers.base import NVRError
from app.models import Channel, ChannelState, Device, Quality, Severity, utcnow
from app.services import alerts
from app.services.poller import _semaphore

log = logging.getLogger(__name__)

_SIG_SIZE = 32  # размер подписи кадра (32×32 grayscale)


@dataclass
class QualityResult:
    verdict: str
    brightness: float
    contrast: float
    sharpness: float
    signature: str  # base64 уменьшенного кадра — для детекта фриза
    frozen: bool


def analyze(jpeg: bytes, prev_sig: str | None = None) -> QualityResult:
    """Анализирует JPEG-кадр и выносит вердикт о качестве."""
    img = Image.open(io.BytesIO(jpeg)).convert("L")
    w, h = img.size

    # Для скорости ужимаем большие кадры до ~256px по большей стороне
    scale = max(w, h)
    if scale > 256:
        img_m = img.resize((max(w * 256 // scale, 1), max(h * 256 // scale, 1)))
    else:
        img_m = img
    arr = np.asarray(img_m, dtype=np.float32)

    brightness = float(arr.mean())
    contrast = float(arr.std())

    # Резкость: дисперсия лапласиана (низкая → размыто/расфокус)
    if arr.shape[0] >= 3 and arr.shape[1] >= 3:
        lap = (
            4 * arr[1:-1, 1:-1]
            - arr[:-2, 1:-1] - arr[2:, 1:-1]
            - arr[1:-1, :-2] - arr[1:-1, 2:]
        )
        sharpness = float(lap.var())
    else:
        sharpness = 1e9  # слишком маленький кадр — резкость не оцениваем

    # Подпись кадра (32×32) для детекта зависшего потока
    small = np.asarray(img.resize((_SIG_SIZE, _SIG_SIZE)), dtype=np.uint8)
    signature = base64.b64encode(small.tobytes()).decode()

    frozen = False
    if prev_sig:
        try:
            prev = np.frombuffer(base64.b64decode(prev_sig), dtype=np.uint8).astype(np.float32)
            cur = small.astype(np.float32).flatten()
            if prev.shape == cur.shape:
                # У живого потока всегда есть шум сенсора → кадры не идентичны.
                # Почти нулевая разница = поток завис/зациклен.
                frozen = float(np.abs(prev - cur).mean()) < settings.quality_frozen_diff
        except Exception:  # noqa: BLE001
            frozen = False

    # Вердикт по приоритету проблем
    if brightness < settings.quality_dark_threshold:
        verdict = Quality.DARK
    elif contrast < settings.quality_uniform_threshold:
        verdict = Quality.UNIFORM
    elif frozen:
        verdict = Quality.FROZEN
    elif sharpness < settings.quality_blur_threshold:
        verdict = Quality.BLURRY
    else:
        verdict = Quality.OK

    return QualityResult(verdict, brightness, contrast, sharpness, signature, frozen)


_VERDICT_RU = {
    Quality.DARK: "тёмный/чёрный кадр",
    Quality.UNIFORM: "однотонный кадр (залеплен/закрыт объектив)",
    Quality.BLURRY: "расфокус/размытие",
    Quality.FROZEN: "зависший поток (кадр не меняется)",
    Quality.ERROR: "не удалось получить кадр",
}


async def check_quality_all() -> None:
    async with SessionLocal() as session:
        ids = (
            await session.execute(select(Device.id).where(Device.enabled.is_(True)))
        ).scalars().all()
    for did in ids:
        await check_device_quality(did)


async def check_device_quality(device_id: int) -> None:
    """Снимает кадр с каждого online-канала и проверяет качество картинки."""
    async with SessionLocal() as session:
        device = (
            await session.execute(
                select(Device).where(Device.id == device_id).options(
                    selectinload(Device.channels)
                )
            )
        ).scalar_one_or_none()
        if device is None or not device.enabled:
            return

        client = build_client(device, semaphore=_semaphore)
        # Анализируем только каналы, которые NVR считает рабочими (там и прячется брак)
        channels = [
            c for c in device.channels
            if c.enabled is not False and c.status == ChannelState.ONLINE
        ]
        for ch in channels:
            scope = f"device:{device_id}:channel:{ch.channel_id}:quality"
            try:
                jpeg = await client.get_snapshot(ch.channel_id)
            except NVRError:
                continue  # снапшот недоступен — пропускаем, не флапаем
            try:
                res = analyze(jpeg, ch.frame_sig)
            except Exception as exc:  # noqa: BLE001
                log.warning("analyze %s/%s: %s", device_id, ch.channel_id, exc)
                continue

            ch.frame_sig = res.signature
            ch.quality = res.verdict
            ch.quality_checked_at = utcnow()
            ch.frozen_count = ch.frozen_count + 1 if res.frozen else 0

            if res.verdict == Quality.OK:
                await alerts.resolve_alert(
                    session, scope_key=scope, device_id=device_id, channel_id=ch.channel_id,
                    message=f"«{device.name}» канал {ch.channel_id} ({ch.name or '—'}): картинка в норме",
                )
            else:
                await alerts.raise_alert(
                    session, scope_key=scope, alert_type="bad_image",
                    severity=Severity.WARNING, device_id=device_id, channel_id=ch.channel_id,
                    message=(
                        f"«{device.name}» канал {ch.channel_id} ({ch.name or '—'}): "
                        f"{_VERDICT_RU.get(res.verdict, res.verdict)}"
                    ),
                    context={"verdict": res.verdict, "brightness": round(res.brightness, 1)},
                    photo=jpeg,  # прикладываем проблемный кадр в Telegram
                )
        await session.commit()
