"""Implementasi referensi (murni Python) untuk verifikasi silang.

Tujuan modul ini **bukan** untuk dipakai di produksi (jauh lebih lambat), tapi
sebagai "oracle" yang sedekat mungkin dengan semantik Pine Script:

* semua akumulasi dilakukan inkremental (tambah/kurangi) seperti Pine,
* ``None`` dipakai sebagai pengganti ``na``,
* tidak ada trik vektorisasi yang bisa menyembunyikan bug logika.

Hasil ``core.compute()`` (vektor) dan :func:`reference.compute_reference`
(loop) dibandingkan di test. Selisih keduanya menunjukkan galat pembulatan
floating point, bukan perbedaan algoritma.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from volumatic_bot.indicator.params import IndicatorParams

__all__ = ["ReferenceOutput", "compute_reference"]

_FloatOrNone = float | None


@dataclass(frozen=True, slots=True)
class ReferenceOutput:
    """Keluaran referensi dalam bentuk list Python biasa."""

    source: list[float]
    cmo: list[_FloatOrNone]
    vidya_raw: list[_FloatOrNone]
    vidya: list[_FloatOrNone]
    atr: list[_FloatOrNone]
    upper: list[_FloatOrNone]
    lower: list[_FloatOrNone]
    trend: list[int]
    trail: list[_FloatOrNone]
    cross_up: list[bool]
    cross_down: list[bool]
    vol_delta_pct: list[_FloatOrNone]
    vol_delta_win_pct: list[_FloatOrNone]

    def as_dict(self) -> dict[str, list[Any]]:
        return {
            "source": self.source,
            "cmo": self.cmo,
            "vidya_raw": self.vidya_raw,
            "vidya": self.vidya,
            "atr": self.atr,
            "upper": self.upper,
            "lower": self.lower,
            "trend": self.trend,
            "trail": self.trail,
            "cross_up": self.cross_up,
            "cross_down": self.cross_down,
            "vol_delta_pct": self.vol_delta_pct,
            "vol_delta_win_pct": self.vol_delta_win_pct,
        }


def _cmos(src: list[float], length: int) -> list[_FloatOrNone]:
    """CMO dengan akumulasi inkremental (meniru ``math.sum`` di Pine)."""
    n = len(src)
    out: list[_FloatOrNone] = [None] * n
    momentum = [0.0] * n
    for i in range(1, n):
        momentum[i] = src[i] - src[i - 1]

    sum_pos = 0.0
    sum_neg = 0.0
    for i in range(n):
        m = momentum[i]
        if m > 0:
            sum_pos += m
        elif m < 0:
            sum_neg += -m
        if i >= length:
            old = momentum[i - length]
            if old > 0:
                sum_pos -= old
            elif old < 0:
                sum_neg -= -old
        if i >= length - 1:
            total = sum_pos + sum_neg
            if total == 0:
                out[i] = 0.0
            else:
                out[i] = abs(100.0 * (sum_pos - sum_neg) / total)
    return out


def _vidya(
    src: list[float], cmo: list[_FloatOrNone], params: IndicatorParams
) -> list[_FloatOrNone]:
    alpha = params.alpha
    out: list[_FloatOrNone] = [None] * len(src)
    prev = 0.0
    for i, (s, c) in enumerate(zip(src, cmo, strict=True)):
        if c is None:
            continue
        k = alpha * abs(c) / 100.0
        prev = k * s + (1.0 - k) * prev
        out[i] = prev
    return out


def _sma(values: list[_FloatOrNone], length: int) -> list[_FloatOrNone]:
    out: list[_FloatOrNone] = [None] * len(values)
    for i in range(len(values)):
        if i < length - 1:
            continue
        window = values[i - length + 1 : i + 1]
        if any(v is None for v in window):
            continue
        total = 0.0
        for v in window:
            assert v is not None
            total += v
        out[i] = total / length
    return out


def _atr(
    high: list[float], low: list[float], close: list[float], length: int
) -> list[_FloatOrNone]:
    n = len(close)
    tr: list[_FloatOrNone] = [None] * n
    for i in range(1, n):
        prev_close = close[i - 1]
        tr[i] = max(high[i], prev_close) - min(low[i], prev_close)

    out: list[_FloatOrNone] = [None] * n
    seed: list[float] = []
    prev: _FloatOrNone = None
    for i in range(n):
        value = tr[i]
        if value is None:
            out[i] = prev
            continue
        if prev is None:
            seed.append(value)
            if len(seed) == length:
                total = 0.0
                for v in seed:
                    total += v
                prev = total / length
                out[i] = prev
            continue
        prev = (value + (length - 1) * prev) / length
        out[i] = prev
    return out


def compute_reference(
    open_time: list[int],
    open_: list[float],
    high: list[float],
    low: list[float],
    close: list[float],
    volume: list[float],
    params: IndicatorParams | None = None,
) -> ReferenceOutput:
    """Hitung indikator dengan gaya penulisan imperatif murni Python."""
    params = params or IndicatorParams()
    n = len(close)

    if params.source == "close":
        src = [float(x) for x in close]
    elif params.source == "hl2":
        src = [(high[i] + low[i]) / 2.0 for i in range(n)]
    else:
        src = [(open_[i] + high[i] + low[i] + close[i]) / 4.0 for i in range(n)]

    cmo = _cmos(src, params.vidya_momentum)
    vidya_raw = _vidya(src, cmo, params)
    vidya = _sma(vidya_raw, params.sma_smooth)
    atr = _atr(high, low, close, params.atr_length)

    upper: list[_FloatOrNone] = [None] * n
    lower: list[_FloatOrNone] = [None] * n
    for i in range(n):
        v, a = vidya[i], atr[i]
        if v is None or a is None:
            continue
        upper[i] = v + a * params.band_distance
        lower[i] = v - a * params.band_distance

    trend = [0] * n
    cross_up = [False] * n
    cross_down = [False] * n
    current = 0
    for i in range(n):
        u, lo = upper[i], lower[i]
        if u is not None and lo is not None:
            if src[i] > u:
                current = 1
            elif src[i] < lo:
                current = -1
        trend[i] = current
        if i > 0 and current != 0 and trend[i - 1] != 0 and current != trend[i - 1]:
            if current == 1:
                cross_up[i] = True
            else:
                cross_down[i] = True

    trail: list[_FloatOrNone] = [None] * n
    for i in range(n):
        if cross_up[i] or cross_down[i]:
            continue
        trail[i] = lower[i] if trend[i] == 1 else upper[i] if trend[i] == -1 else None

    vol_delta: list[_FloatOrNone] = [None] * n
    buy = 0.0
    sell = 0.0
    active = False
    for i in range(n):
        if cross_up[i] or cross_down[i]:
            buy = 0.0
            sell = 0.0
            active = True
        if not active:
            continue
        if close[i] > open_[i]:
            buy += volume[i]
        elif close[i] < open_[i]:
            sell += volume[i]
        denom = (buy + sell) / 2.0
        vol_delta[i] = 0.0 if denom == 0 else (buy - sell) / denom * 100.0

    # Delta% jendela bergulir (dihitung terpisah, gaya loop sederhana).
    vol_delta_win: list[_FloatOrNone] = [None] * n
    win = params.volume_delta_window
    for i in range(n):
        start = i - win + 1
        if start < 0:
            continue
        buy_w = 0.0
        sell_w = 0.0
        for j in range(start, i + 1):
            if close[j] > open_[j]:
                buy_w += volume[j]
            elif close[j] < open_[j]:
                sell_w += volume[j]
        denom_w = (buy_w + sell_w) / 2.0
        vol_delta_win[i] = 0.0 if denom_w == 0 else (buy_w - sell_w) / denom_w * 100.0

    return ReferenceOutput(
        source=src,
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
        vol_delta_pct=vol_delta,
        vol_delta_win_pct=vol_delta_win,
    )
