"""Test studi konvergensi warm-up memakai data nyata (ditandai ``slow``)."""

from __future__ import annotations

import pytest

from volumatic_bot.indicator.params import IndicatorParams
from volumatic_bot.indicator.warmup import (
    convergence_report,
    recommended_warmup,
    to_markdown_table,
)
from volumatic_bot.models import CandleSeries


@pytest.mark.slow
def test_konvergensi_warmup_pada_data_nyata(real_candles: CandleSeries) -> None:
    """Mengunci hasil studi: 2000 bar konvergen, 250 bar tidak.

    Angka di test ini berasal dari ``docs/studi-warmup.md`` yang dihasilkan
    perintah ``volumatic-bot warmup-study``. Kalau algoritma berubah, test ini
    akan gagal dan dokumentasi harus diperbarui.
    """
    rows = convergence_report(real_candles, IndicatorParams())
    by_window = {row.window: row for row in rows}

    # Warm-up pendek: nilai ATR masih jauh (RMA punya memori panjang).
    assert by_window[250].atr_rel_max > 1e-2
    assert by_window[250].trend_mismatch > 0

    # 500 bar: tren sudah identik di semua titik sampel.
    assert by_window[500].trend_mismatch == 0
    assert by_window[500].vidya_rel_max < 1e-3

    # 2000 bar: konvergen menurut ambang yang didokumentasikan.
    assert by_window[2000].converged

    # Kesimpulan yang dipakai sebagai default config.
    assert recommended_warmup(rows) == 2000


@pytest.mark.slow
def test_hasil_warmup_monoton_membaik(real_candles: CandleSeries) -> None:
    """Menambah warm-up tidak boleh membuat galat membesar secara umum."""
    rows = convergence_report(real_candles, IndicatorParams())
    atr_errors = [row.atr_rel_max for row in rows]
    # Bandingkan 250 vs 2000 (bukan tiap langkah, karena bisa naik-turun kecil).
    assert atr_errors[-1] < atr_errors[0]
    # Selisih awal vs akhir minimal 100x lebih kecil.
    assert atr_errors[0] / max(atr_errors[-1], 1e-12) > 100

    table = to_markdown_table(rows)
    assert "| 2000 |" in table
    assert table.count("\n") >= len(rows)


def test_data_terlalu_pendek_ditolak(sample_candles: CandleSeries) -> None:
    pendek = sample_candles.tail(1500)
    with pytest.raises(ValueError, match="terlalu pendek"):
        convergence_report(pendek, IndicatorParams(), windows=(250, 400, 2000))
