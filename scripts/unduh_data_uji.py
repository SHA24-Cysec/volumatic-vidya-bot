#!/usr/bin/env python3
"""Unduh data kline asli dari data.binance.vision untuk data uji/test.

Kenapa skrip terpisah: test harus bisa dijalankan offline, jadi data uji
disimpan di ``tests/data/``. Skrip ini membuat ulang file tersebut secara
deterministik (sumber arsip resmi Binance, bukan API yang butuh kredensial).

Contoh:
    python scripts/unduh_data_uji.py --symbol BTCUSDT --interval 15m \
        --bulan 2025-07 2025-08 2025-09 2025-10 2025-11 \
        --out tests/data/btcusdt_15m_5bulan.csv.gz

Catatan: koneksi ke fapi.binance.com diblokir (HTTP 451) di sandbox
pengembangan, tetapi data.binance.vision dapat diakses.
"""

from __future__ import annotations

import argparse
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
COLUMNS = ["open_time", "open", "high", "low", "close", "volume"]


def unduh_bulan(symbol: str, interval: str, bulan: str) -> pd.DataFrame:
    nama = f"{symbol}-{interval}-{bulan}.zip"
    url = f"{BASE}/{symbol}/{interval}/{nama}"
    print(f"  mengambil {url}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        payload = resp.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        csv_name = zf.namelist()[0]
        with zf.open(csv_name) as handle:
            frame = pd.read_csv(handle)
    if "open_time" not in frame.columns:
        frame = frame.iloc[:, :12]
        frame.columns = [
            *COLUMNS,
            "close_time",
            "quote_volume",
            "count",
            "taker_buy_volume",
            "taker_buy_quote_volume",
            "ignore",
        ]
    return frame.loc[:, COLUMNS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unduh data kline Binance untuk test")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--bulan", nargs="+", required=True, help="format YYYY-MM")
    parser.add_argument("--out", default="tests/data/btcusdt_15m_5bulan.csv.gz")
    args = parser.parse_args(argv)

    frames = []
    for bulan in args.bulan:
        try:
            frames.append(unduh_bulan(args.symbol, args.interval, bulan))
        except Exception as exc:
            print(f"  GAGAL {bulan}: {exc}", file=sys.stderr)
    if not frames:
        print("Tidak ada data yang berhasil diunduh.", file=sys.stderr)
        return 1

    frame = (
        pd.concat(frames)
        .drop_duplicates("open_time")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out, index=False, compression="gzip" if out.suffix == ".gz" else None)
    awal = pd.to_datetime(frame.open_time.min(), unit="ms", utc=True)
    akhir = pd.to_datetime(frame.open_time.max(), unit="ms", utc=True)
    print(f"Tersimpan: {out} | {len(frame)} candle | {awal} -> {akhir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
