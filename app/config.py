"""Конфигурация приложения из переменных окружения / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # БД
    database_url: str = "sqlite+aiosqlite:///./data/nvrmon.db"

    # Шифрование
    secret_key: str = ""

    # Планировщик
    poll_interval_minutes: int = 5
    archive_check_hour: int = 9
    archive_check_minute: int = 0
    max_concurrent_polls: int = 10
    nvr_unreachable_threshold: int = 3
    camera_offline_alert_minutes: int = 10
    time_drift_alert_minutes: int = 5
    archive_gap_alert_minutes: int = 60
    hdd_usage_alert_percent: int = 0

    # Сеть
    default_http_timeout: float = 15.0
    default_http_retries: int = 2

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = True

    # Прочее
    log_level: str = "INFO"
    mock_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
