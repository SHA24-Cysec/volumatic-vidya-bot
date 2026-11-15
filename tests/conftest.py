"""Fixture bersama untuk seluruh test.

Data yang dipakai adalah kline **asli** BTCUSDT perpetual 15m dari
``data.binance.vision`` (Juli–November 2025, 14.688 candle). Tidak ada data
sintetis untuk test indikator utama supaya hasilnya representatif.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from volumatic_bot.data.loaders import load_candles_csv
from volumatic_bot.models import CandleSeries

DATA_DIR = Path(__file__).parent / "data"
REAL_CSV = DATA_DIR / "btcusdt_15m_5bulan.csv.gz"


@pytest.fixture(scope="session")
def real_candles() -> CandleSeries:
    """Seluruh data uji (14.688 candle) -- dipakai test konvergensi."""
    if not REAL_CSV.exists():  # pragma: no cover
        pytest.skip(f"data uji tidak ada: {REAL_CSV}")
    return load_candles_csv(REAL_CSV)


@pytest.fixture(scope="session")
def sample_candles(real_candles: CandleSeries) -> CandleSeries:
    """Potongan 3.000 candle terakhir -- cukup untuk semua test cepat."""
    return real_candles.tail(3000)


@pytest.fixture(scope="session")
def synthetic_candles() -> CandleSeries:
    """Data sintetis kecil untuk kasus tepi yang sulit ditemukan di data nyata."""
    from volumatic_bot.models import CandleSeries

    open_time = [1_700_000_000_000 + i * 900_000 for i in range(8)]
    open_ = [100.0] * 8
    high = [101.0] * 8
    low = [99.0] * 8
    close = [100.5] * 8
    volume = [10.0] * 8
    return CandleSeries.from_columns(open_time, open_, high, low, close, volume)
