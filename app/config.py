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

    # Контроль качества картинки (компьютерное зрение)
    quality_check_minutes: int = 0          # период проверки, 0 = выключено
    quality_dark_threshold: float = 30.0    # средняя яркость ниже → тёмный/чёрный кадр
    quality_uniform_threshold: float = 8.0  # контраст (ст.откл.) ниже → однотонный (залеплен)
    quality_blur_threshold: float = 15.0    # резкость (дисперсия лапласиана) ниже → расфокус
    quality_frozen_diff: float = 1.0        # отличие кадров меньше → кадр идентичен
    quality_frozen_checks: int = 3          # сколько идентичных проверок ПОДРЯД = фриз

    # Здоровье железа NVR
    temp_alert_celsius: float = 65.0        # температура выше → алерт
    cpu_alert_percent: float = 95.0         # загрузка CPU выше → алерт

    # Сеть
    default_http_timeout: float = 15.0
    default_http_retries: int = 2

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = True
    telegram_bot_enabled: bool = True  # двусторонний бот (команды/кнопки)

    # Watchdog (внешний «пульс»: healthchecks.io и т.п.)
    watchdog_url: str = ""
    watchdog_interval_minutes: int = 1

    # Вход в панель (если admin_password пуст — вход отключён, панель открыта)
    admin_username: str = "admin"
    admin_password: str = ""

    # Прочее
    log_level: str = "INFO"
    mock_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
