"""Pydantic-схемы для API."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


# ── Группы ────────────────────────────────────────────────────────────────────
class GroupCreate(BaseModel):
    name: str
    description: str | None = None


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None = None


# ── Устройства ──────────────────────────────────────────────────────────────────
class DeviceCreate(BaseModel):
    name: str
    host: str
    http_port: int = 80
    use_https: bool = False
    username: str = "admin"
    password: str = ""
    api_type: str = "auto"
    auth_scheme: str = "digest"
    group_id: int | None = None
    timeout: float = 15.0
    retries: int = 2
    enabled: bool = True
    latitude: float | None = None
    longitude: float | None = None


class DeviceUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    http_port: int | None = None
    use_https: bool | None = None
    username: str | None = None
    password: str | None = None  # пусто => не менять
    api_type: str | None = None
    auth_scheme: str | None = None
    group_id: int | None = None
    timeout: float | None = None
    retries: int | None = None
    enabled: bool | None = None
    latitude: float | None = None
    longitude: float | None = None


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    channel_id: int
    name: str | None = None
    kind: str
    status: str
    enabled: bool = True
    archive_depth_days: int | None = None
    quality: str | None = None
    quality_checked_at: dt.datetime | None = None
    last_status_change: dt.datetime | None = None
    last_seen: dt.datetime | None = None


class HddOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    hdd_id: str
    name: str | None = None
    capacity_mb: int
    free_mb: int
    status: str


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    host: str
    http_port: int
    api_type: str
    group_id: int | None = None
    address: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    enabled: bool
    reachable: bool
    consecutive_failures: int
    capabilities: dict = Field(default_factory=dict)
    last_seen: dt.datetime | None = None
    last_error: str | None = None
    time_drift_seconds: int | None = None
    latitude: float | None = None
    longitude: float | None = None


class DeviceDetail(DeviceOut):
    channels: list[ChannelOut] = Field(default_factory=list)
    hdds: list[HddOut] = Field(default_factory=list)


# ── Тест соединения / автоопределение ──────────────────────────────────────────
class TestConnectionRequest(BaseModel):
    host: str
    http_port: int = 80
    use_https: bool = False
    username: str = "admin"
    password: str = ""
    api_type: str = "auto"  # auto => автоопределение
    auth_scheme: str = "digest"
    timeout: float = 10.0


class TestConnectionResult(BaseModel):
    ok: bool
    api_type: str
    auth_scheme: str
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    capabilities: dict = Field(default_factory=dict)
    detail: str = ""


# ── События / архив ─────────────────────────────────────────────────────────────
class NoteCreate(BaseModel):
    text: str
    channel_id: int | None = None


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int
    channel_id: int | None = None
    text: str
    created_at: dt.datetime


class MarkerCreate(BaseModel):
    device_id: int
    channel_id: int | None = None
    label: str | None = None
    x: float = 50.0
    y: float = 50.0


class MarkerMove(BaseModel):
    x: float
    y: float


# ── Автобусы / диски ────────────────────────────────────────────────────────────
class BusCreate(BaseModel):
    bus_number: str
    route: str | None = None
    dvr_model: str | None = None


class BusUpdate(BaseModel):
    bus_number: str | None = None
    route: str | None = None
    dvr_model: str | None = None
    problem_note: str | None = None
    has_problem: bool | None = None
    collect_weekday: int | None = None


class DiskCreate(BaseModel):
    label: str
    type: str = "SSD"
    capacity_gb: int | None = None
    assigned_bus_id: int | None = None
    status: str = "ready"
    note: str | None = None


class DiskUpdate(BaseModel):
    label: str | None = None
    type: str | None = None
    capacity_gb: int | None = None
    assigned_bus_id: int | None = None
    note: str | None = None
    location: str | None = None


class AuditRequest(BaseModel):
    present_ids: list[int] = []


class SwapRequest(BaseModel):
    installed_disk_id: int | None = None  # какой ставим (None = снять без замены)
    note: str | None = None
    force: bool = False                   # подтвердить установку чужого диска


class FaultyRequest(BaseModel):
    note: str | None = None


class NotCollectedRequest(BaseModel):
    reason: str


class ReviewRequest(BaseModel):
    tags: list[str] = []
    note: str | None = None
    finish: bool = False  # пометить диск готовым (в резерв) после записи наблюдения


class BusSettings(BaseModel):
    swap_days: int = 14
    review_days: int = 7


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "bus"


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int | None = None
    channel_id: int | None = None
    type: str
    severity: str
    message: str
    created_at: dt.datetime


class ArchiveCoverageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    channel_id: int
    day: dt.date
    status: str
    recorded_minutes: int
    largest_gap_minutes: int
    gaps: list = Field(default_factory=list)
