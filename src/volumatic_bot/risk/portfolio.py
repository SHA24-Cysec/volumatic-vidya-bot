"""Portofolio: hitung margin bebas, margin terpakai, dan batas konsentrasi.

Angka di sini adalah **perkiraan** dari data yang dicatat bot sendiri
(notional posisi terbuka). Binance tetap sumber kebenaran; TAHAP 5 akan
merekonsiliasi angka ini dengan saldo/posisi nyata dari REST.

Posisi selalu margin **isolated** (keputusan pengguna), sehingga margin terpakai
tiap posisi = ``notional / leverage``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Portfolio", "PositionExposure"]


@dataclass(frozen=True, slots=True)
class PositionExposure:
    """Satu posisi terbuka seperti yang dicatat bot."""

    symbol: str
    notional: float
    margin: float
    leverage: int


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Ringkasan paparan portofolio pada satu waktu."""

    equity: float
    positions: tuple[PositionExposure, ...]
    max_total_margin_pct: float = 60.0
    max_per_symbol_margin_pct: float = 30.0

    def __post_init__(self) -> None:
        if self.equity < 0:
            raise ValueError("equity tidak boleh negatif")

    @property
    def open_count(self) -> int:
        return len(self.positions)

    @property
    def used_margin(self) -> float:
        return sum(p.margin for p in self.positions)

    @property
    def used_margin_pct(self) -> float:
        if self.equity <= 0:
            return 0.0
        return self.used_margin / self.equity * 100.0

    @property
    def free_margin(self) -> float:
        """Margin yang masih boleh dipakai sesuai ``risk.max_total_margin_pct``."""
        if self.equity <= 0:
            return 0.0
        cap = self.equity * self.max_total_margin_pct / 100.0
        return max(0.0, cap - self.used_margin)

    def has_symbol(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.positions)

    def symbol_margin(self, symbol: str) -> float:
        return sum(p.margin for p in self.positions if p.symbol == symbol)

    def symbol_margin_limit(self) -> float:
        """Batas margin per simbol (konsentrasi), dalam USDT."""
        return self.equity * self.max_per_symbol_margin_pct / 100.0

    def to_rows(self) -> list[dict[str, object]]:
        """Bentuk siap tampil di dashboard/log."""
        return [
            {
                "symbol": p.symbol,
                "notional": round(p.notional, 4),
                "margin": round(p.margin, 4),
                "leverage": p.leverage,
            }
            for p in self.positions
        ]

    def summary(self) -> str:
        return (
            f"equity={self.equity:.2f} USDT | posisi={self.open_count} | "
            f"margin terpakai={self.used_margin:.2f} ({self.used_margin_pct:.1f}% "
            f"dari batas {self.max_total_margin_pct:.0f}%) | margin bebas={self.free_margin:.2f}"
        )
