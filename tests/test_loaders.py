"""Test pemuat data candle lokal."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from volumatic_bot.data.loaders import candles_to_frame, load_candles_csv, resample_candles
from volumatic_bot.models import CandleSeries


def test_baca_csv_data_vision_tanpa_header(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT-15m.csv"
    path.write_text(
        "1751328000000,100.0,101.0,99.0,100.5,10.0,1751328899999,1000.0,5,4.0,400.0,0\n"
        "1751328900000,100.5,102.0,100.0,101.5,12.0,1751329799999,1200.0,6,5.0,500.0,0\n",
        encoding="utf-8",
    )
    candles = load_candles_csv(path)
    assert len(candles) == 2
    assert candles.open[0] == pytest.approx(100.0)
    assert candles.close[1] == pytest.approx(101.5)
    assert candles.open_time[1] - candles.open_time[0] == 900_000


def test_baca_csv_dengan_header_dan_waktu_iso(tmp_path: Path) -> None:
    path = tmp_path / "tv.csv"
    path.write_text(
        "time,open,high,low,close,volume\n"
        "2025-01-01T00:00:00Z,1,2,0.5,1.5,100\n"
        "2025-01-01T00:15:00Z,1.5,2.5,1.0,2.0,120\n",
        encoding="utf-8",
    )
    candles = load_candles_csv(path)
    assert len(candles) == 2
    assert candles.open_time[0] == 1_735_689_600_000


def test_urutan_dan_duplikat_dirapikan(tmp_path: Path) -> None:
    path = tmp_path / "acak.csv"
    path.write_text(
        "open_time,open,high,low,close,volume\n3000,3,3,3,3,1\n1000,1,1,1,1,1\n1000,1,1,1,1.5,1\n",
        encoding="utf-8",
    )
    candles = load_candles_csv(path)
    assert list(candles.open_time) == [1000, 3000]
    # Duplikat: baris terakhir yang dipakai (close 1.5, volume 1).
    assert candles.close[0] == pytest.approx(1.5)
    assert candles.volume[0] == pytest.approx(1.0)


def test_kolom_wajib_hilang(tmp_path: Path) -> None:
    path = tmp_path / "rusak.csv"
    path.write_text("time,open,high,close\n1,1,2,1.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="kolom wajib"):
        load_candles_csv(path)


def test_file_tidak_ada() -> None:
    with pytest.raises(FileNotFoundError):
        load_candles_csv("tidak-ada.csv")


def test_roundtrip_ke_dataframe(sample_candles: CandleSeries) -> None:
    frame = candles_to_frame(sample_candles)
    assert list(frame.columns) == ["open_time", "open", "high", "low", "close", "volume"]
    assert len(frame) == len(sample_candles)
    assert frame["open_time"].to_numpy().dtype == np.int64


def test_candle_series_menolak_waktu_tidak_urut() -> None:
    with pytest.raises(ValueError, match="naik ketat"):
        CandleSeries.from_columns([2, 1], [1, 1], [1, 1], [1, 1], [1, 1], [1, 1])


def test_candle_series_menolak_panjang_beda() -> None:
    with pytest.raises(ValueError, match="tidak konsisten"):
        CandleSeries.from_columns([1, 2], [1], [1, 1], [1, 1], [1, 1], [1, 1])


def _seri_15m(n: int = 32):
    from volumatic_bot.models import CandleSeries

    awal = 1_700_000_000_000 - (1_700_000_000_000 % 14_400_000)
    waktu = [awal + i * 900_000 for i in range(n)]
    harga = [100.0 + i for i in range(n)]
    return CandleSeries.from_columns(
        open_time=waktu, open=harga, high=[h + 1 for h in harga],
        low=[h - 1 for h in harga], close=harga, volume=[10.0] * n,
    )


def test_resample_15m_ke_4h_menggabungkan_16_bar() -> None:
    seri = _seri_15m(32)
    hasil = resample_candles(seri, "4h")
    assert len(hasil) == 2
    assert hasil.open[0] == seri.open[0]
    assert hasil.high[0] == max(seri.high[:16])
    assert hasil.low[1] == min(seri.low[16:32])
    assert hasil.close[1] == seri.close[-1]
    assert hasil.volume[0] == sum(seri.volume[:16])


def test_resample_membuang_ember_setengah_jadi_di_ujung() -> None:
    hasil = resample_candles(_seri_15m(20), "4h")
    assert len(hasil) == 1


def test_resample_menolak_data_yang_lebih_besar_dari_target() -> None:
    with pytest.raises(ValueError, match="tidak bisa di-resample"):
        resample_candles(_seri_15m(32), "5m")
