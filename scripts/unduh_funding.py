#!/usr/bin/env python
"""Unduh **data funding rate** historis dari data.binance.vision.

Kenapa perlu: backtest yang jujur harus memperhitungkan funding, bukan hanya fee
dan slippage. Binance menerbitkan arsip funding rate per bulan (interval 8 jam)
di ``data/futures/um/monthly/fundingRate/<SYMBOL>/``.

Keluaran: berkas JSON (``[{fundingTime, fundingRate}, ...]``) yang bisa dipakai
``volumatic-bot backtest --funding-file``.

Contoh:
    python scripts/unduh_funding.py --symbol BTCUSDT --dari 2025-07 --sampai 2025-11
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate/{symbol}/"


def _bulan(dari: str, sampai: str) -> list[str]:
    awal = date.fromisoformat(dari + "-01")
    akhir = date.fromisoformat(sampai + "-01")
    hasil: list[str] = []
    tahun, bulan = awal.year, awal.month
    while (tahun, bulan) <= (akhir.year, akhir.month):
        hasil.append(f"{tahun:04d}-{bulan:02d}")
        bulan += 1
        if bulan > 12:
            tahun, bulan = tahun + 1, 1
    return hasil


def _unduh(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Unduh funding rate historis Binance")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--dari", required=True, help="bulan awal, format YYYY-MM")
    p.add_argument("--sampai", required=True, help="bulan akhir, format YYYY-MM")
    p.add_argument(
        "--keluaran",
        default=None,
        help="berkas JSON tujuan (default: tests/data/<symbol>_funding_<dari>_<sampai>.json)",
    )
    args = p.parse_args(argv)

    simbol = args.symbol.upper()
    tujuan = Path(
        args.keluaran or f"tests/data/{simbol.lower()}_funding_{args.dari}_{args.sampai}.json"
    )
    baris: list[dict[str, object]] = []
    for bulan in _bulan(args.dari, args.sampai):
        nama = f"{simbol}-fundingRate-{bulan}.zip"
        data = _unduh(BASE.format(symbol=simbol) + nama)
        if data is None:
            print(f"  ! {bulan}: tidak ada arsip (dilewati)")
            continue
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            isi = zf.read(zf.namelist()[0]).decode("utf-8")
        pembaca = csv.DictReader(io.StringIO(isi))
        jumlah = 0
        for row in pembaca:
            waktu = row.get("calc_time") or row.get("fundingTime")
            rate = row.get("last_funding_rate") or row.get("fundingRate")
            if not waktu or not rate:
                continue
            baris.append({"fundingTime": int(waktu), "fundingRate": float(rate)})
            jumlah += 1
        print(f"  {bulan}: {jumlah} catatan funding")

    if not baris:
        print("tidak ada data funding yang terunduh", file=sys.stderr)
        return 1
    baris.sort(key=lambda r: int(r["fundingTime"]))  # type: ignore[arg-type]
    tujuan.parent.mkdir(parents=True, exist_ok=True)
    tujuan.write_text(json.dumps(baris, indent=1), encoding="utf-8")
    print(f"{len(baris)} catatan funding disimpan di {tujuan}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
