"""Model posisi yang dikelola bot (satu posisi per simbol, one-way mode).

State disimpan eksplisit supaya setiap langkah bisa dilacak dan dipulihkan
setelah restart/crash — hal yang **tidak** dimiliki versi TAHAP 4:

``PENDING_ENTRY`` → ``OPEN`` → ``PARTIAL`` → ``CLOSED``
                                     ↘ ``FAILED`` (entry/proteksi gagal)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

__all__ = ["PositionState", "TrackedPosition"]


class PositionState(StrEnum):
    """Siklus hidup posisi."""

    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    PARTIAL = "partial"  # TP1 sudah kena sebagian, sisa berjalan dengan SL di BE
    CLOSED = "closed"
    FAILED = "failed"  # entry/proteksi gagal; posisi sudah ditutup bila ada

    @property
    def aktif(self) -> bool:
        return self in (PositionState.PENDING_ENTRY, PositionState.OPEN, PositionState.PARTIAL)


@dataclass(slots=True)
class TrackedPosition:
    """Posisi yang sedang (atau pernah) dikelola bot."""

    symbol: str
    side: str  # "long" | "short"
    state: PositionState
    qty: Decimal
    remaining_qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    tp1_price: Decimal
    risk_per_unit: Decimal
    leverage: int
    mode: str  # dry_run | testnet | live
    opened_at: int
    updated_at: int
    be_price: Decimal | None = None
    entry_order_id: int | None = None
    sl_order_id: int | None = None
    tp_order_id: int | None = None
    partial_done: bool = False
    be_done: bool = False
    closed_at: int | None = None
    close_reason: str | None = None
    realized_usdt: float | None = None

    @property
    def active(self) -> bool:
        return self.state.aktif

    @property
    def r_multiple(self) -> float:
        """Risiko 1R dalam USDT (jarak SL x qty awal)."""
        return float(self.risk_per_unit) * float(self.qty)

    def risk_usdt(self) -> float:
        return float(self.risk_per_unit) * float(self.remaining_qty)

    def to_row(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "state": self.state.value,
            "qty": str(self.qty),
            "sisa": str(self.remaining_qty),
            "entry": str(self.entry_price),
            "sl": str(self.stop_price),
            "tp1": str(self.tp1_price),
            "partial": self.partial_done,
            "be": self.be_done,
            "mode": self.mode,
        }

    def summary(self) -> str:
        arah = "LONG" if self.side == "long" else "SHORT"
        return (
            f"{self.symbol} {arah} {self.state.value} qty={self.remaining_qty}/{self.qty} "
            f"entry={self.entry_price} SL={self.stop_price} TP1={self.tp1_price} "
            f"{'partial ' if self.partial_done else ''}{'BE ' if self.be_done else ''}"
            f"R={self.r_multiple:.3f} USDT"
        )
