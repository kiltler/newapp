"""Драйверы NVR: общий интерфейс NVRClient и реализации."""
from app.drivers.base import (
    ArchiveSegment,
    Capabilities,
    ChannelStatus,
    DeviceInfo,
    DeviceTime,
    HddInfo,
    HealthInfo,
    NVRClient,
    NVRAuthError,
    NVRConnectionError,
    NVRError,
)
from app.drivers.detect import detect_api_type
from app.drivers.factory import build_client

__all__ = [
    "NVRClient",
    "NVRError",
    "NVRConnectionError",
    "NVRAuthError",
    "ChannelStatus",
    "HddInfo",
    "ArchiveSegment",
    "Capabilities",
    "DeviceTime",
    "DeviceInfo",
    "HealthInfo",
    "build_client",
    "detect_api_type",
]
