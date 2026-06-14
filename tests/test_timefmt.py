"""Показ времени в местном поясе (хранится в UTC)."""
import datetime as dt

from app import templatefilters
from app.config import settings


def test_localtime_utc_to_local(monkeypatch):
    val = dt.datetime(2026, 6, 14, 9, 50, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(settings, "timezone", "Asia/Vladivostok")  # UTC+10
    assert templatefilters.localtime(val, "%H:%M") == "19:50"


def test_localtime_naive_treated_as_utc(monkeypatch):
    naive = dt.datetime(2026, 6, 14, 9, 50)  # без tz → считаем UTC
    monkeypatch.setattr(settings, "timezone", "Asia/Vladivostok")
    assert templatefilters.localtime(naive, "%H:%M") == "19:50"


def test_localtime_utc_default(monkeypatch):
    val = dt.datetime(2026, 6, 14, 9, 50, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(settings, "timezone", "UTC")
    assert templatefilters.localtime(val, "%H:%M") == "09:50"


def test_localtime_none():
    assert templatefilters.localtime(None) == "—"


def test_localtime_bad_tz_falls_back(monkeypatch):
    val = dt.datetime(2026, 6, 14, 9, 50, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(settings, "timezone", "Not/AZone")
    assert templatefilters.localtime(val, "%H:%M") == "09:50"  # фолбэк на UTC
