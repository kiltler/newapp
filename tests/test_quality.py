"""Тесты контроля качества картинки (компьютерное зрение)."""
import io

import numpy as np
from PIL import Image

from app.models import Quality
from app.services.quality import analyze


def _jpeg(arr: np.ndarray) -> bytes:
    img = Image.fromarray(arr.astype("uint8"), mode="L")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def test_black_frame_is_dark():
    arr = np.zeros((240, 320), dtype="uint8")  # полностью чёрный
    res = analyze(_jpeg(arr))
    assert res.verdict == Quality.DARK


def test_uniform_grey_is_uniform():
    arr = np.full((240, 320), 128, dtype="uint8")  # ровная заливка (залеплен)
    res = analyze(_jpeg(arr))
    assert res.verdict == Quality.UNIFORM


def test_dim_uniform_is_not_covered():
    # Тёмная комната без ИК: ровный тусклый кадр — это НЕ «залеплен».
    arr = np.full((240, 320), 40, dtype="uint8")
    res = analyze(_jpeg(arr))
    assert res.verdict != Quality.UNIFORM


def test_sharp_noise_is_ok():
    rng = np.random.default_rng(42)
    arr = rng.integers(0, 256, size=(240, 320))  # резкий шум — резкость высокая
    res = analyze(_jpeg(arr))
    assert res.verdict == Quality.OK


def test_blurry_is_detected():
    rng = np.random.default_rng(1)
    base = rng.integers(0, 256, size=(240, 320)).astype("uint8")
    blurred = Image.fromarray(base, mode="L").resize((20, 15)).resize((320, 240))
    res = analyze(_jpeg(np.asarray(blurred)))
    assert res.verdict in (Quality.BLURRY, Quality.UNIFORM)  # сильно размытый


def test_same_frame_flags_frozen_but_not_verdict():
    # Идентичный кадр помечается frozen=True, но вердикт пока НЕ «фриз»
    # (это решает персистентность во вызывающем коде, а не один кадр).
    rng = np.random.default_rng(7)
    jpeg = _jpeg(rng.integers(0, 256, size=(240, 320)))
    first = analyze(jpeg)
    second = analyze(jpeg, first.signature)
    assert second.frozen is True
    assert second.verdict == Quality.OK  # один совпавший кадр ещё не «зависание»


def test_live_frames_not_frozen():
    rng = np.random.default_rng(3)
    a = analyze(_jpeg(rng.integers(0, 256, size=(240, 320))))
    b = analyze(_jpeg(rng.integers(0, 256, size=(240, 320))), a.signature)
    assert b.frozen is False


async def test_frozen_requires_persistence(db, monkeypatch):
    """Статичная живая сцена не должна попадать во «фриз» — нужен ряд проверок."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Channel, ChannelState, Device
    from app.services import quality as q

    rng = np.random.default_rng(11)
    static = _jpeg(rng.integers(0, 256, size=(240, 320)))  # один и тот же кадр всегда

    class FakeClient:
        async def get_snapshot(self, channel_id):
            return static

    monkeypatch.setattr(q, "build_client", lambda *a, **k: FakeClient())

    async with SessionLocal() as s:
        d = Device(name="D", host="h", username="a", api_type="hikvision", capabilities={})
        s.add(d)
        await s.commit()
        did = d.id
        s.add(Channel(device_id=did, channel_id=1, status=ChannelState.ONLINE, enabled=True))
        await s.commit()

    async def _quality():
        async with SessionLocal() as s:
            return (await s.execute(select(Channel))).scalars().first().quality

    await q.check_device_quality(did)   # baseline
    await q.check_device_quality(did)   # count=1
    await q.check_device_quality(did)   # count=2 — ещё не фриз
    assert await _quality() != Quality.FROZEN
    await q.check_device_quality(did)   # count=3 — теперь фриз
    assert await _quality() == Quality.FROZEN
