"""ORM-модели: устройства, каналы, HDD, события, архив, состояние алертов."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ── Типы API / статусы (строковые константы, чтобы не зависеть от Enum в БД) ──
class ApiType:
    HIKVISION = "hikvision"
    DAHUA = "dahua"
    ONVIF = "onvif"
    AUTO = "auto"
    UNKNOWN = "unknown"


class ChannelState:
    ONLINE = "online"
    OFFLINE = "offline"
    NO_VIDEO = "no_video"
    UNKNOWN = "unknown"


class HddState:
    OK = "ok"
    ERROR = "error"
    NO_DISK = "no_disk"
    UNKNOWN = "unknown"


class ArchiveState:
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Severity:
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Group(Base):
    """Объект / клиент для группировки регистраторов."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="group")


class Device(Base):
    """Видеорегистратор (NVR)."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), default=None)
    address: Mapped[str | None] = mapped_column(String(512), default=None)  # адрес объекта/клиента

    host: Mapped[str] = mapped_column(String(255))
    http_port: Mapped[int] = mapped_column(Integer, default=80)
    use_https: Mapped[bool] = mapped_column(Boolean, default=False)
    username: Mapped[str] = mapped_column(String(255), default="admin")
    password_enc: Mapped[str] = mapped_column(Text, default="")

    api_type: Mapped[str] = mapped_column(String(32), default=ApiType.AUTO)
    auth_scheme: Mapped[str] = mapped_column(String(16), default="digest")  # digest|basic

    # Идентификация (заполняется при тесте/определении)
    model: Mapped[str | None] = mapped_column(String(255), default=None)
    firmware: Mapped[str | None] = mapped_column(String(255), default=None)
    serial: Mapped[str | None] = mapped_column(String(255), default=None)

    # Сетевые параметры
    timeout: Mapped[float] = mapped_column(Float, default=15.0)
    retries: Mapped[int] = mapped_column(Integer, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Возможности (результат capability-check): {"channels": true, "hdd": ..., "archive": ..., "time": ...}
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)

    # Рантайм-состояние
    reachable: Mapped[bool] = mapped_column(Boolean, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    time_drift_seconds: Mapped[int | None] = mapped_column(Integer, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    group: Mapped["Group | None"] = relationship(back_populates="devices")
    channels: Mapped[list["Channel"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    hdds: Mapped[list["Hdd"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("device_id", "channel_id", name="uq_device_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(Integer)  # номер канала на NVR
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    kind: Mapped[str] = mapped_column(String(16), default="ip")  # ip|analog
    status: Mapped[str] = mapped_column(String(16), default=ChannelState.UNKNOWN)
    last_status_change: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # False = не мониторить (заглушка)
    # Реальная глубина архива по каналу (дней), измеряется суточной задачей.
    archive_depth_days: Mapped[int | None] = mapped_column(Integer, default=None)

    # Контроль качества картинки (компьютерное зрение по снапшоту)
    quality: Mapped[str | None] = mapped_column(String(16), default=None)  # ok|dark|uniform|blurry|frozen|error
    quality_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    frame_sig: Mapped[str | None] = mapped_column(Text, default=None)  # подпись кадра (для детекта фриза)
    frozen_count: Mapped[int] = mapped_column(Integer, default=0)

    device: Mapped["Device"] = relationship(back_populates="channels")


class Quality:
    OK = "ok"
    DARK = "dark"
    UNIFORM = "uniform"
    BLURRY = "blurry"
    FROZEN = "frozen"
    ERROR = "error"


class Hdd(Base):
    __tablename__ = "hdds"
    __table_args__ = (UniqueConstraint("device_id", "hdd_id", name="uq_device_hdd"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    hdd_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    capacity_mb: Mapped[int] = mapped_column(Integer, default=0)
    free_mb: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=HddState.UNKNOWN)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    device: Mapped["Device"] = relationship(back_populates="hdds")

    @property
    def used_mb(self) -> int:
        return max(self.capacity_mb - self.free_mb, 0)

    @property
    def usage_percent(self) -> float:
        return round(self.used_mb / self.capacity_mb * 100, 1) if self.capacity_mb else 0.0


class ArchiveCoverage(Base):
    """Покрытие архива по каналу за конкретный день."""

    __tablename__ = "archive_coverage"
    __table_args__ = (
        UniqueConstraint("device_id", "channel_id", "day", name="uq_archive_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(Integer)
    day: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default=ArchiveState.UNKNOWN)
    recorded_minutes: Mapped[int] = mapped_column(Integer, default=0)
    largest_gap_minutes: Mapped[int] = mapped_column(Integer, default=0)
    gaps: Mapped[list] = mapped_column(JSON, default=list)  # [["HH:MM","HH:MM"], ...]
    checked_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """Лог событий и история изменений статуса."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), default=None
    )
    channel_id: Mapped[int | None] = mapped_column(Integer, default=None)
    type: Mapped[str] = mapped_column(String(64))  # camera_offline, nvr_unreachable, hdd_error, ...
    severity: Mapped[str] = mapped_column(String(16), default=Severity.INFO)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Note(Base):
    """Журнал обслуживания: заметки по устройству/каналу."""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    channel_id: Mapped[int | None] = mapped_column(Integer, default=None)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanMarker(Base):
    """Точка камеры/устройства на плане объекта (координаты в процентах 0–100)."""

    __tablename__ = "plan_markers"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    channel_id: Mapped[int | None] = mapped_column(Integer, default=None)
    label: Mapped[str | None] = mapped_column(String(255), default=None)
    x: Mapped[float] = mapped_column(Float, default=50.0)
    y: Mapped[float] = mapped_column(Float, default=50.0)


class AlertState(Base):
    """Активное состояние проблемы для анти-спама и уведомлений 'восстановлено'."""

    __tablename__ = "alert_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(255), unique=True)  # уникальный ключ проблемы
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), default=None
    )
    alert_type: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_notified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
