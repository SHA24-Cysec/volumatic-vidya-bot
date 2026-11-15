"""Port Python indikator "Volumatic VIDYA" (konsep BigBeluga) untuk USDT-M Futures.

Implementasi ditulis ulang dari nol berdasarkan *deskripsi logika* indikator,
bukan salinan kode Pine. Lisensi skrip asli: CC BY-NC-SA 4.0 (c) BigBeluga,
sehingga karya turunan ini bersifat **non-komersial** dan **share-alike**.
Lihat README bagian "Atribusi & lisensi".

Semantik yang dipertahankan dari Pine Script:
* ``na`` direpresentasikan sebagai ``numpy.nan``.
* CMO memakai magnitudo momentum negatif (bukan nilai bertanda).
* VIDYA rekursif dengan nilai awal 0, lalu dihaluskan SMA.
* ATR memakai RMA (Wilder) yang di-seed SMA.
* Tren hanya berubah lewat crossover/under harga terhadap band.
* Bar tepat saat tren berganti: garis trail bernilai ``na``.

Indikator ini **tidak boleh** dipakai pada candle yang belum tertutup.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Final

import numpy as np
from numpy.typing import NDArray

from volumatic_bot.indicator.numerics import (
    FloatArray,
    as_float_array,
    rma,
    rolling_mean_full,
    rolling_sum,
    true_range,
)
from volumatic_bot.indicator.params import IndicatorParams
from volumatic_bot.models import CandleSeries

__all__ = [
    "IndicatorResult",
    "LiquidityZone",
    "TrendState",
    "compute",
    "compute_series",
    "entry_signal",
]

TREND_UP: Final[int] = 1
TREND_DOWN: Final[int] = -1


class TrendState(IntEnum):
    """Status tren pada satu bar."""

    DOWN = -1
    FLAT = 0  # belum ada data cukup / belum pernah menyentuh band
    UP = 1


@dataclass(frozen=True, slots=True)
class LiquidityZone:
    """Satu garis zona likuiditas (pivot yang belum tersentuh garis trail)."""

    kind: str  # "low" (di bawah, relevan saat tren naik) atau "high"
    price: float
    pivot_index: int  # indeks bar tempat pivot berada
    confirm_index: int  # indeks bar saat pivot terkonfirmasi (pivot_right bar kemudian)
    touched_index: int | None  # indeks bar saat garis trail memotong zona (None = belum)

    @property
    def touched(self) -> bool:
        return self.touched_index is not None


@dataclass(slots=True)
class _ActiveZone:
    """Zona yang sedang "hidup" saat iterasi bar (status internal)."""

    idx: int  # posisi zona di daftar hasil
    touched: int | None = None


@dataclass(frozen=True, slots=True)
class IndicatorResult:
    """Hasil perhitungan indikator untuk satu rangkaian candle (vectorized output)."""

    params: IndicatorParams
    open_time: NDArray[np.int64]
    cmo: FloatArray
    vidya_raw: FloatArray
    vidya: FloatArray
    atr: FloatArray
    upper: FloatArray
    lower: FloatArray
    trend: NDArray[np.int8]
    trail: FloatArray
    cross_up: NDArray[np.bool_]
    cross_down: NDArray[np.bool_]
    vol_buy: FloatArray
    vol_sell: FloatArray
    vol_delta_pct: FloatArray
    vol_delta_win_pct: FloatArray
    zone_count: FloatArray
    zones: tuple[LiquidityZone, ...]

    def __len__(self) -> int:
        return int(self.trend.size)

    @property
    def trend_state(self) -> TrendState:
        """Tren pada bar terakhir (berguna untuk sinyal paling baru)."""
        if self.trend.size == 0:
            return TrendState.FLAT
        return TrendState(int(self.trend[-1]))

    def is_warm(self, index: int = -1) -> bool:
        """True bila nilai indikator pada ``index`` sudah valid (bukan nan)."""
        idx = index if index >= 0 else len(self) + index
        if not 0 <= idx < len(self):
            return False
        return bool(np.isfinite(self.vidya[idx]) and np.isfinite(self.atr[idx]))

    def as_dict(self, index: int) -> dict[str, float | bool | str]:
        """Ambil satu bar dalam bentuk dict (untuk log/dashboard)."""
        return {
            "open_time": int(self.open_time[index]),
            "cmo": float(self.cmo[index]),
            "vidya": float(self.vidya[index]),
            "atr": float(self.atr[index]),
            "upper": float(self.upper[index]),
            "lower": float(self.lower[index]),
            "trail": float(self.trail[index]),
            "trend": TrendState(int(self.trend[index])).name,
            "cross_up": bool(self.cross_up[index]),
            "cross_down": bool(self.cross_down[index]),
            "vol_delta_pct": float(self.vol_delta_pct[index]),
            "vol_delta_win_pct": float(self.vol_delta_win_pct[index]),
        }


def _select_source(candles: CandleSeries, source: str) -> FloatArray:
    if source == "close":
        return as_float_array(candles.close, name="close")
    if source == "hl2":
        high = as_float_array(candles.high, name="high")
        low = as_float_array(candles.low, name="low")
        return (high + low) / 2.0
    if source == "ohlc4":
        return (
            as_float_array(candles.open, name="open")
            + as_float_array(candles.high, name="high")
            + as_float_array(candles.low, name="low")
            + as_float_array(candles.close, name="close")
        ) / 4.0
    raise ValueError(f"source tidak dikenal: {source!r}")


def _chande_momentum_oscillator(source: FloatArray, length: int) -> FloatArray:
    """CMO absolut seperti pada indikator (0..100).

    ``sumPos`` = jumlah momentum positif, ``sumNeg`` = jumlah magnitudo momentum
    negatif dalam jendela. Bila ``sumPos + sumNeg == 0`` (harga benar-benar
    datar), CMO didefinisikan 0 -- pembagian nol tidak dibiarkan menghasilkan
    ``inf``/``nan`` yang akan merusak rekurens VIDYA.
    """
    n = source.size
    momentum = np.zeros(n, dtype=np.float64)
    if n >= 2:
        momentum[1:] = np.diff(source)
    positive = np.where(momentum > 0.0, momentum, 0.0)
    negative = np.where(momentum < 0.0, -momentum, 0.0)

    sum_pos = rolling_sum(positive, length)
    sum_neg = rolling_sum(negative, length)
    total = sum_pos + sum_neg

    out = np.full(n, np.nan, dtype=np.float64)
    valid = np.isfinite(total)
    zero = valid & (total <= 0.0)
    nonzero = valid & (total > 0.0)
    out[zero] = 0.0
    out[nonzero] = np.abs(100.0 * (sum_pos[nonzero] - sum_neg[nonzero]) / total[nonzero])
    return out


def _vidya_recursive(source: FloatArray, cmo: FloatArray, params: IndicatorParams) -> FloatArray:
    """VIDYA rekursif: ``v[t] = k*src[t] + (1-k)*v[t-1]`` dengan ``k = alpha*CMO/100``."""
    n = source.size
    out = np.full(n, np.nan, dtype=np.float64)
    alpha = params.alpha
    prev = 0.0  # nilai awal 0 sesuai spesifikasi
    for i in range(n):
        c = cmo[i]
        if not np.isfinite(c):
            continue  # belum warm-up -> tetap na
        k = alpha * abs(float(c)) / 100.0
        prev = k * float(source[i]) + (1.0 - k) * prev
        out[i] = prev
    return out


def _trend_and_crosses(
    source: FloatArray, upper: FloatArray, lower: FloatArray
) -> tuple[NDArray[np.int8], NDArray[np.bool_], NDArray[np.bool_]]:
    """Tentukan status tren dan sinyal pergantian tren.

    Aturan: naik saat ``source`` menembus ``upper``, turun saat menembus
    ``lower``, selain itu pertahankan tren sebelumnya. Sinyal hanya keluar bila
    tren sebelumnya sudah pasti (bukan kondisi awal/flat), meniru perilaku
    ``ta.change(trend)`` yang bernilai ``na`` pada bar pertama.
    """
    n = source.size
    trend = np.zeros(n, dtype=np.int8)
    cross_up = np.zeros(n, dtype=bool)
    cross_down = np.zeros(n, dtype=bool)
    current = 0
    for i in range(n):
        u = upper[i]
        lo = lower[i]
        if np.isfinite(u) and np.isfinite(lo):
            if source[i] > u:
                current = TREND_UP
            elif source[i] < lo:
                current = TREND_DOWN
        trend[i] = current
        if i > 0 and current != 0 and trend[i - 1] != 0 and current != trend[i - 1]:
            if current == TREND_UP:
                cross_up[i] = True
            else:
                cross_down[i] = True
    return trend, cross_up, cross_down


def _trail_line(
    trend: NDArray[np.int8],
    upper: FloatArray,
    lower: FloatArray,
    cross_up: NDArray[np.bool_],
    cross_down: NDArray[np.bool_],
) -> FloatArray:
    """Garis trail: lower band saat naik, upper band saat turun, ``na`` saat ganti tren."""
    trail = np.full(trend.size, np.nan, dtype=np.float64)
    up = trend == TREND_UP
    down = trend == TREND_DOWN
    trail[up] = lower[up]
    trail[down] = upper[down]
    flip = cross_up | cross_down
    trail[flip] = np.nan
    return trail


def _volume_delta(
    candles: CandleSeries,
    trend: NDArray[np.int8],
    cross_up: NDArray[np.bool_],
    cross_down: NDArray[np.bool_],
    window: int,
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
    """Akumulasi volume beli/jual sejak tren berganti, lalu hitung delta%.

    Candle bullish (close > open) dihitung "buy", bearish "sell", doji (close ==
    open) tidak masuk ke keduanya. Delta% = (buy - sell) / ((buy + sell)/2) * 100.
    """
    n = trend.size
    vol = as_float_array(candles.volume, name="volume")
    opens = as_float_array(candles.open, name="open")
    closes = as_float_array(candles.close, name="close")
    flip = cross_up | cross_down

    vol_buy = np.full(n, np.nan, dtype=np.float64)
    vol_sell = np.full(n, np.nan, dtype=np.float64)
    buy_acc = 0.0
    sell_acc = 0.0
    active = False
    for i in range(n):
        if flip[i]:
            buy_acc = 0.0
            sell_acc = 0.0
            active = True
        if not active:
            continue
        if closes[i] > opens[i]:
            buy_acc += float(vol[i])
        elif closes[i] < opens[i]:
            sell_acc += float(vol[i])
        vol_buy[i] = buy_acc
        vol_sell[i] = sell_acc

    delta = np.full(n, np.nan, dtype=np.float64)
    denom = (vol_buy + vol_sell) / 2.0
    valid = np.isfinite(denom) & (denom > 0.0)
    delta[valid] = (vol_buy[valid] - vol_sell[valid]) / denom[valid] * 100.0
    zero = np.isfinite(denom) & (denom <= 0.0)
    delta[zero] = 0.0

    # Delta% jendela bergulir: dipakai untuk filter entry (lihat params.py).
    buy_per_bar = np.where(closes > opens, vol, 0.0)
    sell_per_bar = np.where(closes < opens, vol, 0.0)
    win_buy = rolling_sum(buy_per_bar, window)
    win_sell = rolling_sum(sell_per_bar, window)
    win_denom = (win_buy + win_sell) / 2.0
    delta_win = np.full(n, np.nan, dtype=np.float64)
    w_valid = np.isfinite(win_denom) & (win_denom > 0.0)
    delta_win[w_valid] = (win_buy[w_valid] - win_sell[w_valid]) / win_denom[w_valid] * 100.0
    delta_win[np.isfinite(win_denom) & (win_denom <= 0.0)] = 0.0
    return vol_buy, vol_sell, delta, delta_win


def _pivot_flags(values: FloatArray, left: int, right: int, *, kind: str) -> NDArray[np.bool_]:
    """Tandai bar yang merupakan pivot high/low dengan ``left``/``right`` tetangga.

    Pivot low: nilainya lebih kecil dari seluruh tetangga (kiri & kanan).
    Pivot high: nilainya lebih besar dari seluruh tetangga.
    """
    n = values.size
    flags = np.zeros(n, dtype=bool)
    if kind not in ("low", "high"):
        raise ValueError(f"kind tidak dikenal: {kind!r}")
    for i in range(left, n - right):
        v = values[i]
        if not np.isfinite(v):
            continue
        ok = True
        for j in range(1, left + 1):
            left_val = values[i - j]
            if not np.isfinite(left_val) or not (left_val > v if kind == "low" else left_val < v):
                ok = False
                break
        if ok:
            for j in range(1, right + 1):
                right_val = values[i + j]
                if not np.isfinite(right_val) or not (
                    right_val > v if kind == "low" else right_val < v
                ):
                    ok = False
                    break
        if ok:
            flags[i] = True
    return flags


def _build_zones(
    candles: CandleSeries, trend: NDArray[np.int8], trail: FloatArray, params: IndicatorParams
) -> tuple[NDArray[np.float64], tuple[LiquidityZone, ...]]:
    """Bangun garis zona likuiditas dan hitung berapa zona aktif per bar.

    Sesuai spesifikasi: saat tren naik, **pivot low yang berada di ATAS garis
    trail** dianggap zona (likuiditas yang belum tersapu), dan saat tren turun
    **pivot high di BAWAH garis trail**. Zona "tersentuh" ketika garis trail
    memotong harganya: pada tren naik trail bergerak naik lalu melewati harga
    pivot low; pada tren turun trail bergerak turun lalu melewati harga pivot
    high. Zona dihapus setelah ``zone_lookback_bars`` bar tanpa sentuhan.
    """
    n = trend.size
    low_series = (
        as_float_array(candles.close, name="close")
        if params.pivot_low_source == "close"
        else as_float_array(candles.low, name="low")
    )
    high_series = (
        as_float_array(candles.high, name="high")
        if params.pivot_high_source == "high"
        else as_float_array(candles.close, name="close")
    )
    low_flags = _pivot_flags(low_series, params.pivot_left, params.pivot_right, kind="low")
    high_flags = _pivot_flags(high_series, params.pivot_left, params.pivot_right, kind="high")

    zone_count = np.zeros(n, dtype=np.float64)
    zones: list[LiquidityZone] = []
    active: list[_ActiveZone] = []

    low_indices = np.flatnonzero(low_flags)
    high_indices = np.flatnonzero(high_flags)
    low_cursor = 0
    high_cursor = 0

    for i in range(n):
        # 1) Konfirmasi pivot yang jatuh tepat `pivot_right` bar lalu.
        pivot_index = i - params.pivot_right
        if pivot_index >= 0:
            if low_cursor < low_indices.size and low_indices[low_cursor] == pivot_index:
                low_cursor += 1
                price = float(low_series[pivot_index])
                if trend[i] == TREND_UP and np.isfinite(trail[i]) and price > trail[i]:
                    zones.append(LiquidityZone("low", price, int(pivot_index), i, None))
                    active.append(_ActiveZone(idx=len(zones) - 1))
            if high_cursor < high_indices.size and high_indices[high_cursor] == pivot_index:
                high_cursor += 1
                price = float(high_series[pivot_index])
                if trend[i] == TREND_DOWN and np.isfinite(trail[i]) and price < trail[i]:
                    zones.append(LiquidityZone("high", price, int(pivot_index), i, None))
                    active.append(_ActiveZone(idx=len(zones) - 1))

        # 2) Cek sentuhan: garis trail memotong harga zona pada bar ini.
        if np.isfinite(trail[i]):
            for item in active:
                if item.touched is not None:
                    continue
                zone_obj = zones[item.idx]
                # Tren naik: trail naik lalu memotong pivot low dari bawah.
                # Tren turun: trail turun lalu memotong pivot high dari atas.
                crossed = zone_obj.kind == "low" and trail[i] >= zone_obj.price
                crossed = crossed or (zone_obj.kind == "high" and trail[i] <= zone_obj.price)
                if crossed:
                    item.touched = i

        # 3) Buang zona yang sudah tersentuh atau terlalu tua.
        still_active = []
        for item in active:
            index = item.idx
            zone_obj = zones[index]
            touched = item.touched
            if touched is not None:
                zones[index] = LiquidityZone(
                    zone_obj.kind,
                    zone_obj.price,
                    zone_obj.pivot_index,
                    zone_obj.confirm_index,
                    int(touched),
                )
                continue
            if i - zone_obj.confirm_index > params.zone_lookback_bars:
                continue
            still_active.append(item)
        active = still_active
        zone_count[i] = float(len(active))

    return zone_count, tuple(zones)


def compute(candles: CandleSeries, params: IndicatorParams | None = None) -> IndicatorResult:
    """Hitung seluruh keluaran indikator untuk ``candles``.

    ``candles`` harus berisi candle yang sudah tertutup dan terurut naik.
    """
    params = params or IndicatorParams()
    if len(candles) == 0:
        raise ValueError("candles kosong")

    source = _select_source(candles, params.source)
    cmo = _chande_momentum_oscillator(source, params.vidya_momentum)
    vidya_raw = _vidya_recursive(source, cmo, params)
    vidya = rolling_mean_full(vidya_raw, params.sma_smooth)

    atr = rma(true_range(candles.high, candles.low, candles.close), params.atr_length)
    upper = vidya + atr * float(params.band_distance)
    lower = vidya - atr * float(params.band_distance)

    trend, cross_up, cross_down = _trend_and_crosses(source, upper, lower)
    trail = _trail_line(trend, upper, lower, cross_up, cross_down)
    vol_buy, vol_sell, vol_delta, vol_delta_win = _volume_delta(
        candles, trend, cross_up, cross_down, params.volume_delta_window
    )
    zone_count, zones = _build_zones(candles, trend, trail, params)

    return IndicatorResult(
        params=params,
        open_time=np.asarray(candles.open_time, dtype=np.int64).copy(),
        cmo=cmo,
        vidya_raw=vidya_raw,
        vidya=vidya,
        atr=atr,
        upper=upper,
        lower=lower,
        trend=trend,
        trail=trail,
        cross_up=cross_up,
        cross_down=cross_down,
        vol_buy=vol_buy,
        vol_sell=vol_sell,
        vol_delta_pct=vol_delta,
        vol_delta_win_pct=vol_delta_win,
        zone_count=zone_count,
        zones=zones,
    )


def compute_series(candles: CandleSeries, params: IndicatorParams | None = None) -> IndicatorResult:
    """Alias eksplisit untuk :func:`compute` (nama dipakai di dokumentasi)."""
    return compute(candles, params)


def entry_signal(result: IndicatorResult, index: int = -1) -> str | None:
    """Kembalikan ``"long"``/``"short"`` bila bar ``index`` adalah sinyal entry.

    Filter volume delta (opsional) ikut diterapkan di sini agar logika sinyal
    hanya ada di satu tempat: sinyal **long** butuh delta >= ambang, sinyal
    **short** butuh delta <= -ambang (tekanan jual dominan). Filter order book
    **tidak** di sini -- itu tugas modul ``orderbook``.
    """
    idx = index if index >= 0 else len(result) + index
    if not 0 <= idx < len(result):
        return None
    params = result.params
    if result.cross_up[idx]:
        direction = "long"
    elif result.cross_down[idx]:
        direction = "short"
    else:
        return None
    if params.volume_filter:
        if params.volume_filter_source == "rolling":
            delta = result.vol_delta_win_pct[idx]
        else:
            delta = result.vol_delta_pct[idx]
        if not np.isfinite(delta):
            return None
        threshold = float(params.volume_delta_min_pct)
        if direction == "long" and delta < threshold:
            return None
        if direction == "short" and delta > -threshold:
            return None
    return direction
