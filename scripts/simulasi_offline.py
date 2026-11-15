#!/usr/bin/env python3
"""Simulasi jalur data TAHAP 2 tanpa jaringan.

Memutar ulang candle historis (file CSV lokal) melalui :class:`KlineFeed` persis
seperti event WebSocket asli: satu per satu, hanya candle tertutup, lalu mencetak
sinyal yang muncul. Berguna untuk memverifikasi bahwa pipeline berjalan di mesin
Anda walau API Binance tidak dapat diakses (mis. diblokir / 451).

Contoh:
    python scripts/simulasi_offline.py \
        --candles tests/data/btcusdt_15m_5bulan.csv.gz \
        --seed 1500 --feed 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from volumatic_bot.binance.kline_messages import KlineEvent
from volumatic_bot.config import load_config
from volumatic_bot.data.loaders import load_candles_csv
from volumatic_bot.indicator import compute, entry_signal
from volumatic_bot.models import CandleSeries
from volumatic_bot.streams.kline_feed import KlineFeed, SignalEvent


def event_from(series: CandleSeries, index: int, symbol: str, interval: str) -> KlineEvent:
    open_ms = int(series.open_time[index])
    return KlineEvent(
        symbol=symbol,
        interval=interval,
        open_time=open_ms,
        close_time=open_ms + 899_999,
        open=float(series.open[index]),
        high=float(series.high[index]),
        low=float(series.low[index]),
        close=float(series.close[index]),
        volume=float(series.volume[index]),
        quote_volume=0.0,
        is_closed=True,
        event_time=open_ms + 900_000,
    )


def perkiraan_ukuran_event(symbol: str = "BTCUSDT", interval: str = "15m") -> int:
    """Ukuran satu pesan kline (byte) untuk memperkirakan pemakaian bandwidth."""
    payload = {
        "stream": f"{symbol.lower()}@kline_{interval}",
        "data": {
            "e": "kline",
            "E": 1764546300000,
            "s": symbol,
            "k": {
                "t": 1764545400000,
                "T": 1764546299999,
                "s": symbol,
                "i": interval,
                "f": 123456789,
                "L": 123457890,
                "o": "90615.60",
                "c": "90320.60",
                "h": "90800.00",
                "l": "90100.00",
                "v": "2343.354",
                "n": 12345,
                "x": True,
                "q": "212345678.90",
                "V": "1120.5",
                "Q": "101234567.8",
                "B": "0",
            },
        },
    }
    return len(json.dumps(payload, separators=(",", ":")).encode())


async def main_async(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    params = config.indicator.to_params()
    candles = load_candles_csv(args.candles)
    if len(candles) <= args.seed:
        print(f"Data terlalu pendek: {len(candles)} bar, butuh > {args.seed}", file=sys.stderr)
        return 2

    window = candles.tail(args.seed + args.feed)
    seed_part = window.head(args.seed)
    print(
        f"Simulasi {args.symbol} {config.scanner.timeframe}: seed {len(seed_part)} bar, "
        f"mengalirkan {args.feed} candle tertutup"
    )

    signals: list[SignalEvent] = []
    feed = KlineFeed(
        params,
        max_bars=len(window),
        on_signal=signals.append,
        offload=True,
        scores={args.symbol: 0.42},
    )
    feed.seed(args.symbol, seed_part)

    mulai = time.perf_counter()
    for i in range(args.seed, len(window)):
        await feed.handle_event(event_from(window, i, args.symbol, config.scanner.timeframe))
    durasi = time.perf_counter() - mulai

    print(
        f"Selesai dalam {durasi * 1000:.0f} ms | event diproses: {feed.processed} | "
        f"sinyal: {len(signals)}"
    )
    for s in signals:
        waktu = time.strftime("%Y-%m-%d %H:%M", time.gmtime(s.open_time / 1000))
        print(
            f"  {waktu} UTC  {s.direction.upper():<5} close={s.close_price:,.2f} "
            f"ATR={s.atr_pct:.2f}% trail={s.trail:,.2f} cmo={s.cmo:.1f} "
            f"deltaWin={s.vol_delta_win_pct:+.1f}%"
        )

    # Verifikasi silang dengan hitungan penuh: harus identik.
    full = compute(window, params)
    expected = [
        (int(full.open_time[i]), d)
        for i in range(args.seed, len(window))
        if (d := entry_signal(full, i)) is not None
    ]
    got = [(s.open_time, s.direction) for s in signals]
    print(f"Verifikasi silang dengan hitungan penuh: {'COCOK' if got == expected else 'BEDA'}")
    if got != expected:
        print(f"  diharapkan {expected}")
        print(f"  diperoleh  {got}")
        return 1

    ukuran = perkiraan_ukuran_event(args.symbol)
    print(f"\nPerkiraan ukuran satu pesan kline: {ukuran} byte ({ukuran / 1024:.2f} KB)")
    print(
        "Perkiraan bandwidth WebSocket = ukuran pesan x jumlah update/detik x "
        "jumlah simbol (ukur angka sebenarnya di mode listen)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulasi pipeline kline tanpa jaringan")
    parser.add_argument("--candles", default="tests/data/btcusdt_15m_5bulan.csv.gz")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--seed", type=int, default=1500)
    parser.add_argument("--feed", type=int, default=500)
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
