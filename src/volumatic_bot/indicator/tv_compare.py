"""Pembanding hasil hitungan Python vs ekspor TradingView.

Cara pakai (lihat docs/tahap-1-indikator.md untuk detail ekspor):

    volumatic-bot tv-compare --tv ~/unduhan/BTCUSDT_15m_TV.csv --candles data.csv

Format ekspor TradingView yang didukung: CSV dengan kolom waktu (``time`` atau
``open_time``) + OHLC, plus kolom plot indikator. Nama kolom dikenali fleksibel
(lihat ``COLUMN_ALIASES``). Kolom yang tidak ada akan dilewati.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from volumatic_bot.data.loaders import detect_header, to_epoch_ms
from volumatic_bot.indicator.core import IndicatorResult

__all__ = [
    "COLUMN_ALIASES",
    "ColumnComparison",
    "ComparisonReport",
    "compare_with_tradingview",
    "load_tradingview_export",
]

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "vidya": ("vidya", "vidya_line", "garis vidya"),
    "upper": ("upper", "upper_band", "upperband", "atas"),
    "lower": ("lower", "lower_band", "lowerband", "bawah"),
    "trail": ("trail", "trail_line", "trailing"),
    "atr": ("atr",),
    "cmo": ("cmo", "momo", "momentum"),
    "trend": ("trend", "state", "arah"),
    "vol_delta_pct": ("delta", "volume_delta", "deltapct", "vol_delta", "vol_delta_pct"),
}


@dataclass(frozen=True, slots=True)
class ColumnComparison:
    """Perbandingan satu kolom antara Python dan TradingView."""

    column: str
    compared: int
    max_abs_diff: float
    mean_abs_diff: float
    max_rel_diff: float
    mismatches: int
    tolerance: float
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.mismatches == 0


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """Hasil lengkap pembandingan."""

    bars_matched: int
    bars_only_python: int
    bars_only_tradingview: int
    columns: tuple[ColumnComparison, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return all(col.passed for col in self.columns) and self.bars_matched > 0

    def to_markdown(self) -> str:
        lines = [
            "## Hasil pembandingan Python vs TradingView",
            "",
            f"- Bar yang cocok berdasarkan waktu: **{self.bars_matched}**",
            f"- Bar hanya ada di Python: {self.bars_only_python}",
            f"- Bar hanya ada di TradingView: {self.bars_only_tradingview}",
            "",
            "| Kolom | Bar dibandingkan | Max abs diff | Mean abs diff | Max rel diff "
            "| Bar beda | Toleransi | Status |",
            "|---|---:|---:|---:|---:|---:|---:|:---:|",
        ]
        for col in self.columns:
            status = "LULUS" if col.passed else "GAGAL"
            lines.append(
                f"| {col.column} | {col.compared} | {col.max_abs_diff:.3e} | "
                f"{col.mean_abs_diff:.3e} | {col.max_rel_diff:.3e} | {col.mismatches} | "
                f"{col.tolerance:.1e} | {status} |"
            )
        if not self.columns:
            lines.append("| (tidak ada kolom yang bisa dibandingkan) | - | - | - | - | - | - | - |")
        return "\n".join(lines) + "\n"


def load_tradingview_export(path: str | Path) -> pd.DataFrame:
    """Baca ekspor TradingView menjadi DataFrame dengan kolom kanonik."""
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"file ekspor TradingView tidak ditemukan: {csv_path}")
    has_header = detect_header(csv_path)
    if not has_header:
        raise ValueError(
            "ekspor TradingView harus punya baris header berisi nama kolom "
            "(time, open, high, low, close, ...)"
        )
    frame = pd.read_csv(csv_path)
    original = [str(c) for c in frame.columns]
    frame.columns = [str(c).strip().lower().replace(" ", "_") for c in frame.columns]

    time_col = next(
        (c for c in ("time", "open_time", "timestamp", "date") if c in frame.columns), None
    )
    if time_col is None:
        raise ValueError(f"kolom waktu tidak ditemukan pada ekspor TradingView: {original}")
    frame["open_time"] = to_epoch_ms(frame[time_col])

    for canonical, aliases in COLUMN_ALIASES.items():
        if canonical in frame.columns:
            continue
        for alias in aliases:
            if alias in frame.columns:
                frame[canonical] = frame[alias]
                break
    return frame


def _align(result: IndicatorResult, tv: pd.DataFrame) -> tuple[np.ndarray, pd.DataFrame, int, int]:
    ours = pd.DataFrame({"open_time": result.open_time})
    merged = tv.merge(ours, on="open_time", how="outer", indicator=True)
    only_tv = int((merged["_merge"] == "left_only").sum())
    only_py = int((merged["_merge"] == "right_only").sum())
    both = merged[merged["_merge"] == "both"].sort_values("open_time").reset_index(drop=True)
    positions = np.searchsorted(result.open_time, both["open_time"].to_numpy())
    return positions, both, only_tv, only_py


def compare_with_tradingview(
    result: IndicatorResult,
    tv_path: str | Path,
    *,
    rel_tolerance: float = 1e-6,
    abs_tolerance: float = 1e-6,
    trend_tolerance: float = 0.5,
) -> ComparisonReport:
    """Bandingkan keluaran indikator Python dengan ekspor TradingView."""
    tv = load_tradingview_export(tv_path)
    positions, both, only_tv, only_py = _align(result, tv)

    comparisons: list[ColumnComparison] = []
    for column in ("vidya", "upper", "lower", "trail", "atr", "cmo", "vol_delta_pct", "trend"):
        if column not in both.columns:
            continue
        theirs = pd.to_numeric(both[column], errors="coerce").to_numpy(dtype=np.float64)
        if column == "trend":
            ours = np.asarray(result.trend[positions], dtype=np.float64)
        else:
            values = getattr(result, column)
            ours = np.asarray(values[positions], dtype=np.float64)

        if column == "trend":
            mask = np.isfinite(theirs) & np.isfinite(ours)
            diffs = np.abs(ours[mask] - theirs[mask])
            mismatches = int(np.count_nonzero(diffs > trend_tolerance))
            comparisons.append(
                ColumnComparison(
                    column=column,
                    compared=int(mask.sum()),
                    max_abs_diff=float(diffs.max()) if diffs.size else 0.0,
                    mean_abs_diff=float(diffs.mean()) if diffs.size else 0.0,
                    max_rel_diff=0.0,
                    mismatches=mismatches,
                    tolerance=trend_tolerance,
                )
            )
            continue

        mask = np.isfinite(theirs) & np.isfinite(ours)
        if not mask.any():
            comparisons.append(
                ColumnComparison(
                    column, 0, 0.0, 0.0, 0.0, 0, rel_tolerance, "tidak ada nilai valid"
                )
            )
            continue
        diff = np.abs(ours[mask] - theirs[mask])
        scale = np.maximum(np.abs(theirs[mask]), 1e-12)
        rel = diff / scale
        allowed = np.maximum(abs_tolerance, np.abs(theirs[mask]) * rel_tolerance)
        mismatches = int(np.count_nonzero(diff > allowed))
        comparisons.append(
            ColumnComparison(
                column=column,
                compared=int(mask.sum()),
                max_abs_diff=float(diff.max()),
                mean_abs_diff=float(diff.mean()),
                max_rel_diff=float(rel.max()),
                mismatches=mismatches,
                tolerance=rel_tolerance,
            )
        )

    return ComparisonReport(
        bars_matched=len(both),
        bars_only_python=only_py,
        bars_only_tradingview=only_tv,
        columns=tuple(comparisons),
    )
