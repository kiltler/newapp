"""Тесты расчёта покрытия архива (compute_coverage) и суточной проверки."""
import datetime as dt

from app.drivers.base import ArchiveSegment
from app.models import ArchiveState
from app.services.archive import compute_coverage

DAY = dt.date(2026, 6, 10)
START = dt.datetime.combine(DAY, dt.time.min)
END = dt.datetime.combine(DAY, dt.time.max).replace(microsecond=0)


def _seg(h1, h2):
    return ArchiveSegment(START + dt.timedelta(hours=h1), START + dt.timedelta(hours=h2))


def test_full_day():
    segs = [_seg(0, 24)]
    status, recorded, largest, gaps = compute_coverage(segs, START, END, 60)
    assert status == ArchiveState.FULL
    assert recorded >= 23 * 60
    assert gaps == []


def test_no_recording():
    status, recorded, largest, gaps = compute_coverage([], START, END, 60)
    assert status == ArchiveState.NONE
    assert recorded == 0
    assert largest > 60


def test_partial_with_gap():
    # запись 0–2 и 5–24, дыра 2–5 (3 часа)
    segs = [_seg(0, 2), _seg(5, 24)]
    status, recorded, largest, gaps = compute_coverage(segs, START, END, 60)
    assert status == ArchiveState.PARTIAL
    assert largest >= 170  # ~180 минут
    assert len(gaps) == 1


def test_small_gap_still_full():
    # дыра всего 30 минут (< порога 60) → FULL
    segs = [_seg(0, 12), ArchiveSegment(START + dt.timedelta(hours=12, minutes=30), END)]
    status, recorded, largest, gaps = compute_coverage(segs, START, END, 60)
    assert status == ArchiveState.FULL


def test_overlapping_segments_merge():
    segs = [_seg(0, 10), _seg(8, 24)]  # перекрытие
    status, recorded, largest, gaps = compute_coverage(segs, START, END, 60)
    assert status == ArchiveState.FULL
    assert recorded <= 24 * 60  # не считаем перекрытие дважды
