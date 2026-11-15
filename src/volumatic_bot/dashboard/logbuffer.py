"""Penangkap log terakhir untuk panel dashboard.

Handler ini menyimpan ``maxlen`` catatan terakhir di memori (tanpa berkas, tanpa
jaringan) dan sudah melewatkan pesan melalui filter rahasia yang sama dengan
handler konsol, jadi API key/listenKey tidak bisa muncul di panel.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Iterable

from volumatic_bot.dashboard.models import EventLine
from volumatic_bot.logging_setup import SecretRedactingFilter

__all__ = ["RingBufferHandler"]


class RingBufferHandler(logging.Handler):
    """Simpan ``maxlen`` pesan log terakhir sebagai :class:`EventLine`."""

    def __init__(self, *, maxlen: int = 200, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._buffer: deque[EventLine] = deque(maxlen=max(1, maxlen))
        self.addFilter(SecretRedactingFilter(secrets, name="dashboard-buffer"))
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            pesan = self.format(record)
        except Exception:  # pragma: no cover - formatter rusak
            self.handleError(record)
            return
        self._buffer.append(
            EventLine(
                clock=time.strftime("%H:%M:%S", time.localtime(record.created)),
                level=record.levelname,
                text=pesan,
            )
        )

    def lines(self, *, limit: int | None = None) -> list[EventLine]:
        """Ambil log terakhir (urut lama → baru)."""
        data = list(self._buffer)
        if limit is not None and limit >= 0:
            data = data[-limit:] if limit else []
        return data

    def clear(self) -> None:
        self._buffer.clear()

    def __len__(self) -> int:
        return len(self._buffer)
