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


def test_frozen_when_same_frame_twice():
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 256, size=(240, 320))
    jpeg = _jpeg(arr)
    first = analyze(jpeg)                       # первый кадр — подпись
    second = analyze(jpeg, first.signature)     # тот же кадр снова → фриз
    assert second.frozen is True
    assert second.verdict == Quality.FROZEN


def test_live_frames_not_frozen():
    rng = np.random.default_rng(3)
    a = analyze(_jpeg(rng.integers(0, 256, size=(240, 320))))
    b = analyze(_jpeg(rng.integers(0, 256, size=(240, 320))), a.signature)
    assert b.frozen is False
