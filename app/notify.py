"""Telling a human what happened.

The pipeline runs unattended, so every outcome that needs a person — a draft
waiting for approval, a run that failed — leaves here. Telegram is the first
implementation; the protocol is what the pipeline depends on, so adding SMS or
email later touches only this file.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from .config import NotifyConfig

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, text: str) -> bool: ...


class LogNotifier:
    """Fallback when no channel is configured. Never fails, never notifies."""

    def send(self, text: str) -> bool:
        logger.info("NOTIFY: %s", text)
        return True


class TelegramNotifier:
    def __init__(self, config: NotifyConfig) -> None:
        self.config = config

    def send(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.config.telegram_token}/sendMessage"
        try:
            response = httpx.post(
                url,
                json={
                    "chat_id": self.config.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=20.0,
            )
            response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 - a failed notice must not stop the run
            logger.error("Telegram notification failed: %s", exc)
            return False


def build_notifier(config: NotifyConfig) -> Notifier:
    if config.telegram_configured:
        return TelegramNotifier(config)
    logger.warning("No notification channel configured; writing notices to the log.")
    return LogNotifier()
