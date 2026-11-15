"""Skor & peringkat kandidat sinyal.

Saat beberapa simbol memberi sinyal pada candle yang sama, kita tidak mungkin
(yang tidak boleh) membuka semuanya: batas slot posisi dan batas margin berlaku.
Urutan prioritas memakai tiga komponen yang semuanya sudah dihitung:

1. **Kekuatan tren** — ``|CMO|`` relatif terhadap ``scoring.trend_cmo_ref``.
2. **Delta volume** searah trade — ``±vol_delta_win_pct`` (jendela bergulir).
3. **Skor order book** — dari :class:`BookDecision`.

Bobot dan normalisasinya transparan (``config.scoring``), sehingga Anda bisa
mengubah prioritas tanpa menyentuh kode. Skor **tidak** menjamin profit; ia
hanya menentukan urutan saat slot terbatas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from volumatic_bot.orderbook.gate import BookDecision
from volumatic_bot.streams.kline_feed import SignalEvent

__all__ = [
    "Candidate",
    "book_score",
    "combine",
    "rank_candidates",
    "trend_strength",
    "volume_score",
]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def trend_strength(cmo: float, reference: float = 50.0) -> float:
    """Kekuatan tren 0..1 dari nilai CMO absolut."""
    if reference <= 0:
        raise ValueError("reference harus > 0")
    if cmo != cmo:  # NaN
        return 0.0
    return _clamp(abs(cmo) / reference, 0.0, 1.0)


def volume_score(delta_win_pct: float, direction: Literal["long", "short"]) -> float:
    """Delta volume 0..1, searah trade.

    Delta +100% berarti seluruh volume jendela adalah candle naik. Untuk short
    nilainya dibalik, sehingga ``0.5`` berarti netral.
    """
    if delta_win_pct != delta_win_pct:  # NaN
        return 0.5
    sign = 1.0 if direction == "long" else -1.0
    aligned = _clamp(sign * delta_win_pct / 100.0, -1.0, 1.0)
    return (aligned + 1.0) / 2.0


def book_score(decision: BookDecision | None) -> float:
    """Skor order book; 0.5 (netral) bila filter dimatikan/tidak dipakai."""
    if decision is None or not decision.enabled:
        return 0.5
    return _clamp(decision.score, 0.0, 1.0)


def combine(
    *,
    trend: float,
    volume: float,
    book: float,
    weights: tuple[float, float, float],
) -> float:
    """Gabungkan tiga komponen (0..1) menjadi skor total, bobot sudah dinormalisasi."""
    w_trend, w_volume, w_book = weights
    total = w_trend * _clamp(trend, 0.0, 1.0)
    total += w_volume * _clamp(volume, 0.0, 1.0)
    total += w_book * _clamp(book, 0.0, 1.0)
    return _clamp(total, 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class Candidate:
    """Sinyal + hasil filter order book + skor akhir."""

    signal: SignalEvent
    book: BookDecision | None
    trend_component: float
    volume_component: float
    book_component: float
    score: float
    #: True bila kandidat ini kebagian slot posisi yang tersedia.
    selected: bool = False

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def direction(self) -> str:
        return self.signal.direction

    @property
    def tradable(self) -> bool:
        """Lolos filter order book (bukan berarti otomatis entry: slot bisa habis)."""
        return self.book is None or self.book.accepted

    def rejection_reason(self) -> str | None:
        if self.book is not None and not self.book.accepted:
            return "; ".join(self.book.reasons)
        if not self.selected:
            return "slot posisi penuh / kalah prioritas skor"
        return None

    def as_row(self) -> dict[str, object]:
        """Bentuk siap tampil di dashboard/log."""
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "score": round(self.score, 3),
            "trend": round(self.trend_component, 3),
            "volume": round(self.volume_component, 3),
            "book": round(self.book_component, 3),
            "tradable": self.tradable,
            "selected": self.selected,
            "reason": self.rejection_reason(),
        }


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Urutkan kandidat: yang lolos filter lebih dulu, lalu skor tertinggi."""
    return sorted(
        candidates,
        key=lambda c: (not c.tradable, -c.score, c.symbol),
    )
