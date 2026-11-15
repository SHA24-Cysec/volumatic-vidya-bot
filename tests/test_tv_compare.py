"""Test pembanding ekspor TradingView.

Karena saya tidak punya ekspor TradingView asli saat pengembangan, test di sini
memakai dua jenis data:
1. "ekspor sintetis" yang dibuat dari hasil hitungan Python sendiri (lalu
   dibulatkan seperti TradingView) -> memverifikasi alat pembandingnya benar,
2. ekspor yang sengaja dirusak -> memverifikasi alat ini bisa GAGAL dengan jujur.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from volumatic_bot.indicator import IndicatorParams, compute
from volumatic_bot.indicator.tv_compare import compare_with_tradingview, load_tradingview_export
from volumatic_bot.models import CandleSeries


def _write_tv_export(
    candles: CandleSeries,
    result,
    path: Path,
    *,
    shift: float = 0.0,
    rows: int = 500,  # type: ignore[no-untyped-def]
) -> Path:
    """Tulis CSV meniru ekspor TradingView (dibulatkan 8 desimal, waktu ISO)."""
    frame = pd.DataFrame(
        {
            "time": pd.to_datetime(candles.open_time, unit="ms", utc=True).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "open": candles.open,
            "high": candles.high,
            "low": candles.low,
            "close": candles.close,
            "VIDYA": np.round(result.vidya, 8),
            "Upper Band": np.round(result.upper + shift, 8),
            "Lower Band": np.round(result.lower, 8),
            "Trail": np.round(result.trail, 8),
            "ATR": np.round(result.atr, 8),
            "Trend": result.trend,
        }
    )
    frame.tail(rows).to_csv(path, index=False)
    return path


@pytest.fixture()
def tv_export(tmp_path: Path, sample_candles: CandleSeries) -> tuple[Path, object]:
    """Ekspor tiruan berisi 500 bar terakhir dari hasil hitungan Python sendiri."""
    result = compute(sample_candles, IndicatorParams())
    export = _write_tv_export(sample_candles, result, tmp_path / "tv.csv", rows=500)
    return export, result


def test_ekspor_tradingview_terbaca(tv_export: tuple[Path, object]) -> None:
    export, _ = tv_export
    frame = load_tradingview_export(export)
    assert "vidya" in frame.columns
    assert "upper" in frame.columns
    assert "lower" in frame.columns
    assert "trail" in frame.columns
    assert "trend" in frame.columns
    assert frame["open_time"].dtype == np.int64


def test_perbandingan_lulus_untuk_data_identik(tv_export: tuple[Path, object]) -> None:
    export, result = tv_export
    report = compare_with_tradingview(result, export)
    assert report.bars_matched == 500
    assert report.passed, report.to_markdown()
    markdown = report.to_markdown()
    assert "Hasil pembandingan" in markdown
    assert "LULUS" in markdown


def test_perbandingan_gagal_bila_ada_selisih(tmp_path: Path, sample_candles: CandleSeries) -> None:
    result = compute(sample_candles, IndicatorParams())
    export = _write_tv_export(
        sample_candles, result, tmp_path / "tv_geser.csv", shift=5.0, rows=500
    )
    report = compare_with_tradingview(result, export)
    assert not report.passed
    upper = next(col for col in report.columns if col.column == "upper")
    assert upper.max_abs_diff == pytest.approx(5.0, abs=1e-6)
    assert "GAGAL" in report.to_markdown()


def test_ekspor_tanpa_header_ditolak(tmp_path: Path, sample_candles: CandleSeries) -> None:
    path = tmp_path / "tanpa_header.csv"
    path.write_text("1700000000000,1,2,0.5,1.5,10\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        load_tradingview_export(path)


def test_ekspor_tanpa_kolom_waktu_ditolak(tmp_path: Path) -> None:
    path = tmp_path / "tanpa_waktu.csv"
    path.write_text("open,high,low,close\n1,2,0.5,1.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="waktu"):
        load_tradingview_export(path)
