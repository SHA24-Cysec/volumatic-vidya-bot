"""Kontainer candle yang sudah tertutup (closed candle) + validasi data.

Modul ini kecil tetapi penting: indikator hanya boleh diberi candle yang sudah
CLOSED, sehingga seluruh pipeline memakai kontainer yang sama.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]

__all__ = ["CandleSeries"]


@dataclass(frozen=True, slots=True)
class CandleSeries:
    """Kumpulan candle OHLCV yang sudah tertutup dan terurut naik menurut waktu.

    ``open_time`` disimpan dalam milidetik epoch (UTC), sama seperti Binance.
    """

    open_time: IntArray
    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray
    volume: FloatArray

    def __post_init__(self) -> None:
        lengths = {
            "open_time": len(self.open_time),
            "open": len(self.open),
            "high": len(self.high),
            "low": len(self.low),
            "close": len(self.close),
            "volume": len(self.volume),
        }
        if len(set(lengths.values())) != 1:
            raise ValueError(f"panjang kolom candle tidak konsisten: {lengths}")
        if len(self.open_time) and not np.all(np.diff(self.open_time) > 0):
            raise ValueError("open_time harus naik ketat (tanpa duplikat/berbalik)")
        if len(self.close) and not np.all(np.isfinite(self.close)):
            raise ValueError("harga close mengandung NaN/inf")
        if len(self.volume) and np.any(np.asarray(self.volume) < 0):
            raise ValueError("volume tidak boleh negatif")

    @classmethod
    def from_columns(
        cls,
        open_time: ArrayLike,
        open: ArrayLike,
        high: ArrayLike,
        low: ArrayLike,
        close: ArrayLike,
        volume: ArrayLike,
    ) -> CandleSeries:
        return cls(
            open_time=np.asarray(open_time, dtype=np.int64),
            open=np.asarray(open, dtype=np.float64),
            high=np.asarray(high, dtype=np.float64),
            low=np.asarray(low, dtype=np.float64),
            close=np.asarray(close, dtype=np.float64),
            volume=np.asarray(volume, dtype=np.float64),
        )

    def __len__(self) -> int:
        return int(self.close.size)

    def head(self, count: int) -> CandleSeries:
        """Ambil ``count`` candle paling awal (dipakai uji anti-lookahead)."""
        if count < 0:
            raise ValueError("count tidak boleh negatif")
        if count == 0:
            return CandleSeries.from_columns([], [], [], [], [], [])
        return CandleSeries.from_columns(
            self.open_time[:count],
            self.open[:count],
            self.high[:count],
            self.low[:count],
            self.close[:count],
            self.volume[:count],
        )

    def tail(self, count: int) -> CandleSeries:
        """Ambil ``count`` candle terakhir (dipakai untuk rolling window)."""
        if count < 0:
            raise ValueError("count tidak boleh negatif")
        if count == 0:
            return CandleSeries.from_columns([], [], [], [], [], [])
        return CandleSeries.from_columns(
            self.open_time[-count:],
            self.open[-count:],
            self.high[-count:],
            self.low[-count:],
            self.close[-count:],
            self.volume[-count:],
        )
