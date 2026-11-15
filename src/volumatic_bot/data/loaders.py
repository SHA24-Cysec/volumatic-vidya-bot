"""Pembaca data candle dari file lokal (CSV / CSV.GZ).

Mendukung dua format:
1. Ekspor Binance Data Vision (``BTCUSDT-15m-2025-07.csv``): boleh dengan atau
   tanpa baris header, waktu dalam milidetik epoch.
2. Ekspor TradingView (``time,open,high,low,close,volume``): waktu boleh ISO-8601
   atau epoch detik/milidetik.

Catatan: modul ini hanya membaca file lokal. Pengambilan data lewat REST ada di
tahap berikutnya (``streams``/``data``).
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import IO, Any

import numpy as np
import pandas as pd

from volumatic_bot.models import CandleSeries

__all__ = ["candles_to_frame", "detect_header", "load_candles_csv", "to_epoch_ms"]

_REQUIRED = ("open", "high", "low", "close", "volume")


def open_text(path: Path) -> IO[Any]:
    if path.suffix in {".gz", ".gzip"}:
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8", newline="")


def detect_header(path: Path) -> bool:
    with open_text(path) as handle:
        first = handle.readline()
    first_cell = first.split(",", 1)[0].strip().strip('"')
    try:
        float(first_cell)
    except ValueError:
        return True
    return False


def to_epoch_ms(values: pd.Series) -> pd.Series:
    """Normalisasi waktu ke milidetik epoch UTC.

    Menerima angka (ns/ms/detik) maupun string tanggal ISO-8601. Catatan
    kompatibilitas: pandas 3.x memakai resolusi datetime mikrodetik, bukan
    nanodetik seperti pandas 2.x, sehingga konversi dilakukan eksplisit ke
    resolusi nanodetik dulu (lihat docs/kompatibilitas-python.md).
    """
    if pd.api.types.is_numeric_dtype(values):
        as_float = values.astype("float64")
        ns = as_float[as_float > 1e17]
        seconds = as_float[(as_float > 1e9) & (as_float < 1e11)]
        ms = as_float.copy()
        if len(ns):
            ms.loc[ns.index] = ns / 1_000_000.0
        if len(seconds):
            ms.loc[seconds.index] = seconds * 1_000.0
        return ms.astype("int64")
    parsed = pd.to_datetime(values, utc=True, format="mixed")
    return parsed.dt.as_unit("ns").astype("int64") // 1_000_000


def load_candles_csv(path: str | Path, *, max_rows: int | None = None) -> CandleSeries:
    """Muat candle dari file CSV lokal menjadi :class:`CandleSeries`."""
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"file candle tidak ditemukan: {csv_path}")

    has_header = detect_header(csv_path)
    if has_header:
        frame = pd.read_csv(csv_path, compression="infer")
        frame.columns = [str(c).strip().lower() for c in frame.columns]
    else:
        frame = pd.read_csv(
            csv_path,
            header=None,
            names=["open_time", "open", "high", "low", "close", "volume"],
            usecols=[0, 1, 2, 3, 4, 5],
            compression="infer",
        )

    missing = [c for c in _REQUIRED if c not in frame.columns]
    if missing:
        raise ValueError(f"kolom wajib tidak ada di {csv_path.name}: {missing}")

    time_col = next(
        (c for c in ("open_time", "time", "timestamp", "date", "datetime") if c in frame.columns),
        None,
    )
    if time_col is None:
        # Tanpa kolom waktu: buat indeks sintetis agar tetap bisa diuji.
        frame["open_time"] = np.arange(len(frame), dtype="int64") * 60_000
    else:
        frame["open_time"] = to_epoch_ms(frame[time_col])

    frame = frame.sort_values("open_time").drop_duplicates("open_time", keep="last")
    frame = frame.loc[:, ["open_time", *_REQUIRED]]
    if max_rows is not None:
        frame = frame.iloc[-max_rows:]

    return CandleSeries.from_columns(
        open_time=frame["open_time"].to_numpy(dtype=np.int64),
        open=frame["open"].to_numpy(dtype=np.float64),
        high=frame["high"].to_numpy(dtype=np.float64),
        low=frame["low"].to_numpy(dtype=np.float64),
        close=frame["close"].to_numpy(dtype=np.float64),
        volume=frame["volume"].to_numpy(dtype=np.float64),
    )


def candles_to_frame(candles: CandleSeries) -> pd.DataFrame:
    """Konversi balik ke DataFrame (untuk ekspor/diagnostik)."""
    return pd.DataFrame(
        {
            "open_time": candles.open_time,
            "open": candles.open,
            "high": candles.high,
            "low": candles.low,
            "close": candles.close,
            "volume": candles.volume,
        }
    )
