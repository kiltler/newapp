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
    auth_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    time_drift_seconds: Mapped[int | None] = mapped_column(Integer, default=None)

    # Здоровье железа (температура/нагрузка), если NVR отдаёт
    cpu_load: Mapped[float | None] = mapped_column(Float, default=None)
    memory_usage: Mapped[float | None] = mapped_column(Float, default=None)
    temperature: Mapped[float | None] = mapped_column(Float, default=None)

    # Координаты объекта (для карты)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)

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


class AuditLog(Base):
    """Журнал действий оператора в панели (кто, что, когда)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user: Mapped[str] = mapped_column(String(64), default="—")
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(255), default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


# ── Модуль «Автобусы» (ручной офлайн-учёт дисковой ротации) ──────────────────
class DiskStatus:
    INSTALLED = "installed"          # стоит в автобусе
    REMOVED_REVIEW = "removed_review"  # снят, ждёт просмотра (в очереди)
    REVIEWED = "reviewed"            # просмотрен, но ещё лежит у смотрящего
    READY = "ready"                  # вернули на полку — готов к установке (резерв)
    FAULTY = "faulty"                # неисправен (ещё не списан)
    WRITTEN_OFF = "written_off"      # списан (выведен из эксплуатации)


class DiskType:
    SSD = "SSD"
    HDD = "HDD"


class Bus(Base):
    """Автобусный видеорегистратор (только ручной учёт, без сети)."""

    __tablename__ = "buses"

    id: Mapped[int] = mapped_column(primary_key=True)
    bus_number: Mapped[str] = mapped_column(String(64))
    route: Mapped[str | None] = mapped_column(String(64), default=None)
    dvr_model: Mapped[str | None] = mapped_column(String(128), default=None)
    location: Mapped[str | None] = mapped_column(String(128), default=None)  # где стоит автобус
    installed_disk_id: Mapped[int | None] = mapped_column(Integer, default=None)
    installed_since: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    collect_weekday: Mapped[int | None] = mapped_column(Integer, default=None)  # день недели сбора 0=Пн
    problem_note: Mapped[str | None] = mapped_column(Text, default=None)
    has_problem: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Disk(Base):
    """Съёмный диск автобусного регистратора."""

    __tablename__ = "disks"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(8), default=DiskType.SSD)
    capacity_gb: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(20), default=DiskStatus.READY)
    assigned_bus_id: Mapped[int | None] = mapped_column(Integer, default=None)
    status_since: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    location: Mapped[str] = mapped_column(String(16), default="shelf")  # где физически
    last_audit_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    batch_id: Mapped[int | None] = mapped_column(Integer, default=None)        # из какой партии (asset_batches.id)
    warranty_until: Mapped[dt.date | None] = mapped_column(Date, default=None)  # гарантия до
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssetBatch(Base):
    """Поступление партии активов (диски/NVR/камеры). Единый учёт закупок."""

    __tablename__ = "asset_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="disk")  # disk | nvr | camera
    model: Mapped[str] = mapped_column(String(128))               # модель (SSD 1ТБ, DS-7616…)
    vendor: Mapped[str | None] = mapped_column(String(128), default=None)
    supplier: Mapped[str | None] = mapped_column(String(128), default=None)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    unit_cost: Mapped[float | None] = mapped_column(Float, default=None)
    warranty_until: Mapped[dt.date | None] = mapped_column(Date, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    user: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


ASSET_KINDS = {"disk": "Диски", "nvr": "Регистраторы", "camera": "Камеры"}


class AssetStatus:
    IN_STOCK = "in_stock"        # на складе (свободен)
    DEPLOYED = "deployed"        # установлен/в работе
    FAULTY = "faulty"            # неисправен
    WRITTEN_OFF = "written_off"  # списан


ASSET_STATUSES = {
    AssetStatus.IN_STOCK: "на складе",
    AssetStatus.DEPLOYED: "установлен",
    AssetStatus.FAULTY: "неисправен",
    AssetStatus.WRITTEN_OFF: "списан",
}


class Asset(Base):
    """Единица учёта NVR/камеры (склад → установка → неисправность → списание).

    Диски учитываются отдельной моделью Disk (они завязаны на ротацию автобусов);
    здесь — регистраторы и камеры как физические единицы с серийником/гарантией.
    """

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="nvr")  # nvr | camera
    label: Mapped[str] = mapped_column(String(64))                # инв. имя/метка
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    serial: Mapped[str | None] = mapped_column(String(128), default=None)
    vendor: Mapped[str | None] = mapped_column(String(128), default=None)
    status: Mapped[str] = mapped_column(String(16), default=AssetStatus.IN_STOCK)
    status_since: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    location: Mapped[str | None] = mapped_column(String(255), default=None)  # где/на каком объекте
    device_id: Mapped[int | None] = mapped_column(Integer, default=None)     # связь с мониторингом (NVR), опц.
    batch_id: Mapped[int | None] = mapped_column(Integer, default=None)
    warranty_until: Mapped[dt.date | None] = mapped_column(Date, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SwapLog(Base):
    """Журнал замен дисков по автобусам."""

    __tablename__ = "swap_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    bus_id: Mapped[int] = mapped_column(Integer)
    removed_disk_id: Mapped[int | None] = mapped_column(Integer, default=None)
    installed_disk_id: Mapped[int | None] = mapped_column(Integer, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    user: Mapped[str | None] = mapped_column(String(64), default=None)  # кто выполнил


class AppSetting(Base):
    """Простое хранилище настроек (ключ/значение), правится из UI."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    """Учётная запись панели. role: admin (всё) | bus (только вкладка «Автобусы»)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="bus")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DiskReview(Base):
    """Запись просмотра диска: что нашли (теги + заметка), кто и когда."""

    __tablename__ = "disk_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    disk_id: Mapped[int] = mapped_column(Integer)
    bus_id: Mapped[int | None] = mapped_column(Integer, default=None)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    user: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# Стандартные теги наблюдений при просмотре диска
OBSERVATION_TAGS = [
    "нет записи", "дыра в записи", "камера замазана",
    "нет звука", "засвет/темно", "ок",
]

# Причины, почему диск не удалось собрать в день сбора
COLLECTION_ISSUES = [
    "автобус не найден",
    "стоит в другом месте",
    "диск не сняли",
]


class DiskLocation:
    IN_BUS = "in_bus"        # в автобусе (в регистраторе)
    REVIEWER = "reviewer"    # у смотрящего (на просмотре)
    SHELF = "shelf"          # на полке (резерв)
    TRANSIT = "transit"      # в пути


DISK_LOCATIONS = {
    DiskLocation.IN_BUS: "в автобусе",
    DiskLocation.REVIEWER: "у смотрящего",
    DiskLocation.SHELF: "на полке",
    DiskLocation.TRANSIT: "в пути",
}

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
