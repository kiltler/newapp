"""Отправка уведомлений в Telegram (главный канал алертов)."""
from __future__ import annotations

import html
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_API = "https://api.telegram.org"


def is_configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def make_client(timeout) -> httpx.AsyncClient:
    """httpx-клиент для Telegram с учётом прокси (если задан TELEGRAM_PROXY)."""
    proxy = settings.telegram_proxy or None
    return httpx.AsyncClient(timeout=timeout, proxy=proxy)


async def send_message(
    text: str, *, chat_id: str | None = None, reply_markup: dict | None = None
) -> bool:
    """Отправляет HTML-сообщение. Возвращает True при успехе.

    Глобально управляется TELEGRAM_ENABLED; молча пропускает, если не настроено.
    """
    if not settings.telegram_enabled:
        log.debug("Telegram отключён (TELEGRAM_ENABLED=false)")
        return False
    if not is_configured():
        log.warning("Telegram не настроен (нет токена/chat_id) — алерт не отправлен")
        return False

    url = f"{_API}/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id or settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        async with make_client(15.0) as http:
            resp = await http.post(url, json=payload)
            if resp.status_code != 200:
                log.error("Telegram API %s: %s", resp.status_code, resp.text[:200])
                return False
            return True
    except httpx.HTTPError as exc:
        log.error("Не удалось отправить в Telegram: %s", exc)
        return False


async def send_photo(image: bytes, caption: str = "", *, chat_id: str | None = None) -> bool:
    """Отправляет фото (например, проблемный кадр с камеры) в Telegram."""
    if not settings.telegram_enabled or not is_configured():
        return False
    url = f"{_API}/bot{settings.telegram_bot_token}/sendPhoto"
    data = {
        "chat_id": chat_id or settings.telegram_chat_id,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }
    files = {"photo": ("snapshot.jpg", image, "image/jpeg")}
    try:
        async with make_client(20.0) as http:
            resp = await http.post(url, data=data, files=files)
            if resp.status_code != 200:
                log.error("Telegram sendPhoto %s: %s", resp.status_code, resp.text[:200])
                return False
            return True
    except httpx.HTTPError as exc:
        log.error("Не удалось отправить фото в Telegram: %s", exc)
        return False


def esc(value: object) -> str:
    """Экранирует текст для HTML-разметки Telegram."""
    return html.escape(str(value))
