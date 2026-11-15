"""Penyiapan logging dengan penyaringan rahasia.

Aturan keras: API key, API secret, listenKey, dan signature **tidak boleh**
pernah muncul di log, dashboard, atau database. Filter di modul ini mengganti
semua kemunculannya dengan ``***REDACTED***`` sebelum record keluar ke handler
mana pun.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

__all__ = ["SecretRedactingFilter", "sanitize", "setup_logging"]

REDACTED = "***REDACTED***"

# Pola umum yang sering membawa rahasia di URL/header Binance.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(signature=)[^&\s\"']+"),
    re.compile(r"(?i)(X-MBX-APIKEY[\"']?\s*[:=]\s*)[^\s\"',]+"),
    re.compile(r"(?i)(listenkey[\"']?\s*[:=]\s*)[^\s\"',]+"),
    re.compile(r"(?i)(apikey[\"']?\s*[:=]\s*)[^\s\"',]+"),
    re.compile(r"(?i)(api_key[\"']?\s*[:=]\s*)[^\s\"',]+"),
    re.compile(r"(?i)(api_secret[\"']?\s*[:=]\s*)[^\s\"',]+"),
    re.compile(r"(?i)(secret[\"']?\s*[:=]\s*)[^\s\"',]+"),
    re.compile(r"(?i)(token[\"']?\s*[:=]\s*)[^\s\"',]+"),
    re.compile(r"(?i)(bot\d{6,}:)[A-Za-z0-9_-]{20,}"),
)


def sanitize(text: str, secrets: Iterable[str] = ()) -> str:
    """Hapus nilai rahasia dan pola sensitif dari sebuah teks."""
    cleaned = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            cleaned = cleaned.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        cleaned = pattern.sub(lambda m: m.group(1) + REDACTED, cleaned)
    return cleaned


class SecretRedactingFilter(logging.Filter):
    """Filter logging yang menyanitasi pesan (termasuk argumen format)."""

    def __init__(self, secrets: Iterable[str] = (), *, name: str = "") -> None:
        super().__init__(name)
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - argumen rusak
            message = str(record.msg)
        record.msg = sanitize(message, self._secrets)
        record.args = ()
        if record.exc_info and record.exc_info[0] is not None:
            # Jejak error bisa memuat URL bertanda tangan; simpan versi bersih.
            record.msg = sanitize(record.msg, self._secrets)
        return True


def setup_logging(
    level: str = "INFO",
    *,
    secrets: Iterable[str] = (),
    log_file: str | None = None,
) -> logging.Logger:
    """Pasang handler konsol (dan opsional file) dengan filter rahasia."""
    logger = logging.getLogger("volumatic_bot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    redactor = SecretRedactingFilter(secrets)

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(redactor)
    logger.addHandler(stream)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        logger.addHandler(file_handler)

    return logger
