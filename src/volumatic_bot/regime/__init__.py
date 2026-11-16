"""Filter entry berbasis rezim harian (EMA200 + ATR daily).

Kenapa ada: backtest multi-simbol 2021-2026 (19 simbol USDT-M, timeframe 4h)
menunjukkan sinyal Volumatic VIDYA menghasilkan expectancy negatif kalau dipakai
apa adanya, terutama di timeframe rendah (15m: -0.12 R per trade). Satu filter
yang bertahan uji out-of-sample, uji silang antar-simbol, dan sensitif terhadap
ambangnya adalah **jarak harga terhadap EMA200 harian, diukur searah trade**:

* ``dist200_side = (close - EMA200_harian) / EMA200_harian * 100 * arah``
* hanya entry bila ``dist200_side <= max_dist_pct`` (bawaan -8%).

Artinya bot hanya masuk ketika harga sudah terentang **minimal 8% melawan arah
trade** terhadap EMA200 harian (short saat harga jauh di atas EMA200, long saat
harga jauh di bawahnya). Ini adalah filter *mean-reversion*, bukan konfirmasi
tren: sinyal VIDYA yang searah tren besar justru punya expectancy terburuk
(``dist200_side > 0`` menghasilkan -0.06 s/d -0.07 R).

Modul ini bebas jaringan dan tidak menyentuh data yang belum tertutup:
EMA200 harian dihitung dari candle harian yang **sudah selesai** sebelum bar
sinyal, ATR daily dipakai hanya untuk validasi kewajaran data.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from volumatic_bot.models import CandleSeries

__all__ = ["DailyRegime", "DailyRegimeFilter", "build_regime_from_daily", "daily_candles"]

_DAY_MS = 86_400_000


def daily_candles(series: CandleSeries, *, min_ratio: float = 0.6) -> CandleSeries:
    """Gabungkan candle intraday menjadi candle harian (UTC).

    Hari yang jumlah barnya kurang dari ``min_ratio`` kali jumlah bar hari
    normal (mis. hari pertama data, atau hari terakhir yang belum selesai)
    **dibuang** supaya EMA tidak dihitung dari candle setengah jadi.
    """
    ot = np.asarray(series.open_time, dtype=np.int64)
    if ot.size == 0:
        raise ValueError("series kosong")
    open_ = np.asarray(series.open, dtype=np.float64)
    high = np.asarray(series.high, dtype=np.float64)
    low = np.asarray(series.low, dtype=np.float64)
    close = np.asarray(series.close, dtype=np.float64)
    volume = np.asarray(series.volume, dtype=np.float64)

    bucket = ot // _DAY_MS * _DAY_MS
    uniq, start, counts = np.unique(bucket, return_index=True, return_counts=True)
    normal = int(np.median(counts[counts >= 1]))
    batas = max(1, int(np.floor(normal * min_ratio)))
    keep = counts >= batas

    uniq, start, counts = uniq[keep], start[keep], counts[keep]
    # ``start`` menandai bar pertama tiap hari; hari bersambung sehingga index
    # akhir hari = start hari berikutnya - 1.
    end = np.append(start[1:], ot.size) - 1
    d_high = np.maximum.reduceat(high, start) if start.size else np.array([])
    d_low = np.minimum.reduceat(low, start) if start.size else np.array([])
    d_vol = np.add.reduceat(volume, start) if start.size else np.array([])
    return CandleSeries(
        open_time=uniq,
        open=open_[start],
        high=d_high,
        low=d_low,
        close=close[end],
        volume=d_vol,
    )


def _ema(values: NDArray[np.float64], length: int) -> NDArray[np.float64]:
    alpha = 2.0 / (length + 1.0)
    out = np.empty_like(values)
    if values.size == 0:
        return out
    out[0] = values[0]
    for i in range(1, values.size):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def _rma(values: NDArray[np.float64], length: int) -> NDArray[np.float64]:
    out = np.full(values.size, np.nan, dtype=np.float64)
    if values.size < length:
        return out
    out[length - 1] = float(np.mean(values[:length]))
    beta = 1.0 / length
    for i in range(length, values.size):
        out[i] = out[i - 1] + beta * (values[i] - out[i - 1])
    return out


@dataclass(frozen=True, slots=True)
class DailyRegime:
    """Nilai rezim harian pada **open** hari tertentu."""

    ema_length: int
    atr_length: int
    open_time: NDArray[np.int64]
    ema200: NDArray[np.float64]
    atr_pct: NDArray[np.float64]

    def __len__(self) -> int:
        return int(self.open_time.size)

    def dist_pct(self, day_index: int, close: float) -> float:
        """Jarak close terhadap EMA200 harian, persen (bisa nan)."""
        if not 0 <= day_index < len(self):
            return float("nan")
        ema = float(self.ema200[day_index])
        if not np.isfinite(ema) or ema <= 0 or not np.isfinite(close):
            return float("nan")
        return (close - ema) / ema * 100.0

    def slice_until(self, day_start_ms: int) -> DailyRegime:
        """Bagian rezim yang **sudah selesai** sebelum ``day_start_ms``."""
        cut = int(np.searchsorted(self.open_time, day_start_ms, side="left"))
        return DailyRegime(
            ema_length=self.ema_length,
            atr_length=self.atr_length,
            open_time=self.open_time[:cut],
            ema200=self.ema200[:cut],
            atr_pct=self.atr_pct[:cut],
        )


def build_regime_from_daily(
    daily: CandleSeries, *, ema_length: int = 200, atr_length: int = 14
) -> DailyRegime:
    """Hitung EMA200 dan ATR% dari candle harian yang sudah tertutup."""
    close = np.asarray(daily.close, dtype=np.float64)
    high = np.asarray(daily.high, dtype=np.float64)
    low = np.asarray(daily.low, dtype=np.float64)
    ema = _ema(close, ema_length)
    prev_close = np.concatenate(([close[0]], close[:-1])) if close.size else np.array([])
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    atr = _rma(tr, atr_length)
    with np.errstate(invalid="ignore", divide="ignore"):
        atr_pct = 100.0 * atr / close
    return DailyRegime(
        ema_length=ema_length,
        atr_length=atr_length,
        open_time=np.asarray(daily.open_time, dtype=np.int64),
        ema200=ema,
        atr_pct=atr_pct,
    )


@dataclass(frozen=True, slots=True)
class DailyRegimeFilter:
    """Aturan entry: ``dist200_side <= max_dist_pct`` (dan data harus wajar)."""

    enabled: bool = True
    max_dist_pct: float = -8.0
    min_daily_bars: int = 200
    max_daily_atr_pct: float = 25.0

    def __post_init__(self) -> None:
        if self.max_dist_pct > 50.0 or self.max_dist_pct < -50.0:
            raise ValueError("max_dist_pct harus di antara -50 dan 50")
        if self.min_daily_bars < 1:
            raise ValueError("min_daily_bars harus >= 1")

    def check(
        self,
        regime: DailyRegime | None,
        *,
        close: float,
        day_start_ms: int,
        direction: str,
    ) -> tuple[bool, str]:
        """Kembalikan ``(lolos, alasan)``; alasan dipakai log/dashboard."""
        if not self.enabled:
            return True, "filter rezim harian nonaktif"
        if regime is None or len(regime) < self.min_daily_bars:
            return False, (
                f"data harian belum cukup ({0 if regime is None else len(regime)} bar, "
                f"butuh {self.min_daily_bars})"
            )
        idx = int(np.searchsorted(regime.open_time, day_start_ms, side="left")) - 1
        if idx < 0:
            return False, "data harian belum mencakup tanggal sinyal"
        dist = regime.dist_pct(idx, close)
        if not np.isfinite(dist):
            return False, "EMA200 harian belum terbentuk"
        atr_pct = float(regime.atr_pct[idx])
        if np.isfinite(atr_pct) and atr_pct > self.max_daily_atr_pct:
            return False, (
                f"ATR harian {atr_pct:.1f}% > {self.max_daily_atr_pct:.1f}% (pasar terlalu liar)"
            )
        side = 1.0 if direction == "long" else -1.0
        nilai = dist * side
        if nilai > self.max_dist_pct:
            return False, (
                f"jarak ke EMA200 harian {nilai:+.1f}% > batas {self.max_dist_pct:+.1f}% "
                f"(harga belum terentang melawan arah {direction})"
            )
        return True, f"jarak ke EMA200 harian {nilai:+.1f}% lolos batas {self.max_dist_pct:+.1f}%"
