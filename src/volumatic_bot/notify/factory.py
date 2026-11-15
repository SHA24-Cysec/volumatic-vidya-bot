"""Pabrik notifier dari ``config.yaml``.

Satu tempat untuk menerjemahkan konfigurasi menjadi :class:`NotifierDispatcher`,
supaya CLI dan (nanti) mesin bot memakai jalur notifikasi yang sama.
"""

from __future__ import annotations

import logging

from volumatic_bot.config import AppConfig
from volumatic_bot.notify.dispatcher import ConsoleNotifier, NotifierDispatcher
from volumatic_bot.notify.models import NotifyLevel

__all__ = ["build_dispatcher"]


def build_dispatcher(
    config: AppConfig, *, target: logging.Logger | None = None
) -> NotifierDispatcher:
    """Bangun dispatcher sesuai bagian ``notify:`` di konfigurasi."""
    cfg = config.notify
    kanal = []
    if cfg.console_enabled:
        kanal.append(ConsoleNotifier(target=target))

    # Kanal yang diminta tetapi belum tersedia dicatat, tidak diabaikan diam-diam.
    dilewati = ["telegram"] if cfg.telegram_enabled else []

    return NotifierDispatcher(
        kanal,
        min_level=NotifyLevel(cfg.min_level),
        dedupe_sec=cfg.dedupe_sec,
        max_per_minute=cfg.max_per_minute,
        notify_on_trade=cfg.notify_on_trade,
        notify_on_error=cfg.notify_on_error,
        skipped_channels=dilewati,
    )
