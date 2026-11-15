"""Studi konvergensi warm-up indikator.

VIDYA bersifat rekursif dan ATR memakai RMA (rata-rata Wilder). Keduanya punya
"memori" panjang: nilai pada satu bar bergantung pada seluruh riwayat
sebelumnya. Praktisnya muncul pertanyaan: berapa candle minimum supaya nilai
kita sama dengan TradingView?

Modul ini menjawabnya secara empiris dan bisa diulang siapa pun:

1. Hitung indikator pada seluruh data (dianggap "kebenaran").
2. Hitung ulang hanya dengan ``W`` candle terakhir.
3. Ukur selisih keduanya pada bar-bar terakhir, diambil dari **beberapa titik
   akhir** berbeda supaya hasilnya tidak bergantung pada satu segmen pasar.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from volumatic_bot.indicator.core import compute
from volumatic_bot.indicator.params import IndicatorParams
from volumatic_bot.models import CandleSeries

__all__ = ["ConvergenceRow", "convergence_report", "recommended_warmup", "to_markdown_table"]

DEFAULT_WINDOWS: tuple[int, ...] = (250, 300, 400, 500, 750, 1000, 1500, 2000)


@dataclass(frozen=True, slots=True)
class ConvergenceRow:
    """Satu baris hasil studi: panjang warm-up vs besar selisih."""

    window: int
    bars_compared: int
    samples: int
    atr_rel_max: float
    vidya_rel_max: float
    band_rel_max: float
    trail_abs_max: float
    trend_mismatch: int
    zone_count_mismatch: int

    @property
    def converged(self) -> bool:
        """Ambang konvergensi: galat relatif < 0.01% dan tidak ada beda tren.

        Ambang 1e-4 (0.01%) dipilih karena perubahan harga sekecil itu tidak
        mengubah keputusan trading (band bergeser < 1 tick pada BTC 90k).
        """
        return (
            self.atr_rel_max < 1e-4
            and self.vidya_rel_max < 1e-4
            and self.band_rel_max < 1e-4
            and self.trend_mismatch == 0
        )


def _rel_max(a: np.ndarray, b: np.ndarray) -> float:
    mask = np.isfinite(a) & np.isfinite(b) & (np.abs(b) > 0)
    if not mask.any():
        return 0.0
    return float(np.max(np.abs(a[mask] - b[mask]) / np.abs(b[mask])))


def _abs_max(a: np.ndarray, b: np.ndarray) -> float:
    mask = np.isfinite(a) & np.isfinite(b)
    if not mask.any():
        return 0.0
    return float(np.max(np.abs(a[mask] - b[mask])))


def _sample_ends(total: int, window: int, tail_bars: int, samples: int) -> list[int]:
    """Titik akhir evaluasi (indeks eksklusif), tersebar dari ujung data ke belakang."""
    first = max(window, tail_bars) + 1
    if total <= first:
        raise ValueError(
            f"data terlalu pendek ({total} candle) untuk warm-up {window} + {tail_bars} bar"
        )
    candidates = np.linspace(total, first, num=min(samples, total - first), dtype=int)
    return sorted({int(x) for x in candidates})


def convergence_report(
    candles: CandleSeries,
    params: IndicatorParams | None = None,
    *,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    tail_bars: int = 200,
    samples: int = 6,
) -> list[ConvergenceRow]:
    """Bandingkan hasil "data penuh" vs "hanya W candle terakhir"."""
    params = params or IndicatorParams()
    total = len(candles)
    if total <= max(windows) + tail_bars:
        raise ValueError(
            f"data terlalu pendek: butuh > {max(windows) + tail_bars} candle, ada {total}"
        )

    truth = compute(candles, params)
    rows: list[ConvergenceRow] = []
    for window in windows:
        ends = _sample_ends(total, window, tail_bars, samples)
        atr_err = vidya_err = band_err = trail_err = 0.0
        trend_bad = zone_bad = 0
        bars = 0
        for end in ends:
            subset = compute(candles.head(end).tail(window), params)
            compare_bars = min(tail_bars, window)
            bars = compare_bars
            truth_slice = slice(end - compare_bars, end)
            sub_slice = slice(window - compare_bars, window)
            atr_err = max(atr_err, _rel_max(subset.atr[sub_slice], truth.atr[truth_slice]))
            vidya_err = max(vidya_err, _rel_max(subset.vidya[sub_slice], truth.vidya[truth_slice]))
            band_err = max(
                band_err,
                _rel_max(subset.upper[sub_slice], truth.upper[truth_slice]),
                _rel_max(subset.lower[sub_slice], truth.lower[truth_slice]),
            )
            trail_err = max(trail_err, _abs_max(subset.trail[sub_slice], truth.trail[truth_slice]))
            trend_bad = max(
                trend_bad,
                int(np.count_nonzero(subset.trend[sub_slice] != truth.trend[truth_slice])),
            )
            zone_bad = max(
                zone_bad,
                int(
                    np.count_nonzero(subset.zone_count[sub_slice] != truth.zone_count[truth_slice])
                ),
            )
        rows.append(
            ConvergenceRow(
                window=window,
                bars_compared=bars,
                samples=len(ends),
                atr_rel_max=atr_err,
                vidya_rel_max=vidya_err,
                band_rel_max=band_err,
                trail_abs_max=trail_err,
                trend_mismatch=trend_bad,
                zone_count_mismatch=zone_bad,
            )
        )
    return rows


def recommended_warmup(rows: list[ConvergenceRow], *, safety_factor: float = 1.0) -> int:
    """Candle warm-up terkecil yang sudah konvergen (opsional dikali faktor aman)."""
    for row in sorted(rows, key=lambda r: r.window):
        if row.converged:
            return round(row.window * safety_factor)
    return round(max(r.window for r in rows) * safety_factor)


def to_markdown_table(rows: list[ConvergenceRow]) -> str:
    """Render hasil studi sebagai tabel Markdown (dipakai di dokumentasi)."""
    header = (
        "| Warm-up (candle) | Bar dibandingkan | Titik sampel | ATR rel. error max "
        "| VIDYA rel. error max | Band rel. error max | Trail abs. error max "
        "| Beda tren (bar) | Beda zona (bar) | Konvergen |\n"
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|\n"
    )
    lines = []
    for row in rows:
        lines.append(
            f"| {row.window} | {row.bars_compared} | {row.samples} | {row.atr_rel_max:.3e} | "
            f"{row.vidya_rel_max:.3e} | {row.band_rel_max:.3e} | {row.trail_abs_max:.6f} | "
            f"{row.trend_mismatch} | {row.zone_count_mismatch} | "
            f"{'ya' if row.converged else 'belum'} |"
        )
    return header + "\n".join(lines) + "\n"
