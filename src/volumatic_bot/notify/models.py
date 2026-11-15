"""Model notifikasi: level, jenis peristiwa, dan format pesan.

Notifikasi di proyek ini **tidak** memakai layanan luar secara bawaan
(pilihan pengguna: cukup konsol + log). Semua pesan tetap melewati
:func:`volumatic_bot.logging_setup.sanitize` supaya API key, listenKey, dan
signature tidak pernah ikut tercetak.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from volumatic_bot.logging_setup import sanitize

__all__ = ["NotifyEvent", "NotifyKind", "NotifyLevel"]


class NotifyLevel(StrEnum):
    """Tingkat kepentingan pesan (urut naik)."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _LEVEL_RANK[self]

    def __lt__(self, other: NotifyLevel) -> bool:  # type: ignore[override]
        return self.rank < other.rank


_LEVEL_RANK: dict[NotifyLevel, int] = {
    NotifyLevel.INFO: 10,
    NotifyLevel.SUCCESS: 20,
    NotifyLevel.WARNING: 30,
    NotifyLevel.CRITICAL: 40,
}


class NotifyKind(StrEnum):
    """Jenis peristiwa yang bisa diberitakan."""

    STARTUP = "startup"
    SIGNAL = "signal"
    ENTRY = "entry"
    PARTIAL_TP = "partial_tp"
    BREAK_EVEN = "break_even"
    TRAILING = "trailing"
    EXIT = "exit"
    REJECTED = "rejected"
    ERROR = "error"
    INCIDENT = "incident"
    DAILY_STOP = "daily_stop"
    KILL_SWITCH = "kill_switch"


@dataclass(frozen=True, slots=True)
class NotifyEvent:
    """Satu peristiwa yang layak diketahui operator.

    ``detail`` boleh memuat nilai dari bursa; nilainya dibersihkan dari pola
    rahasia saat objek dibuat (bukan saat dicetak), sehingga tidak ada jalur
    yang lupa menyanitasi.
    """

    kind: NotifyKind
    title: str
    detail: str = ""
    level: NotifyLevel = NotifyLevel.INFO
    symbol: str | None = None
    created_at: float = field(default_factory=time.time)
    #: Tag untuk menggabungkan pesan berulang (mis. ``error:rest``).
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", sanitize(self.title))
        object.__setattr__(self, "detail", sanitize(self.detail))

    @property
    def key(self) -> str:
        """Kunci penggabungan pesan berulang."""
        return self.dedupe_key or f"{self.kind}:{self.symbol or '-'}"

    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.created_at))

    def format_line(self) -> str:
        """Satu baris ringkas untuk log/dashboard."""
        bagian = [self.clock(), self.level.value.upper().ljust(8), self.title]
        if self.detail:
            bagian.append("- " + self.detail)
        return " ".join(bagian)

    def format_block(self) -> str:
        """Beberapa baris (untuk konsol notifikasi)."""
        baris = [f"[{self.clock()}] {self.level.value.upper()}: {self.title}"]
        if self.symbol:
            baris.append(f"  simbol : {self.symbol}")
        if self.detail:
            baris.extend(f"  {line}" for line in self.detail.splitlines())
        return "\n".join(baris)

    def as_row(self) -> dict[str, Any]:
        return {
            "jam": self.clock(),
            "level": self.level.value,
            "jenis": self.kind.value,
            "simbol": self.symbol or "-",
            "judul": self.title,
            "detail": self.detail,
        }
