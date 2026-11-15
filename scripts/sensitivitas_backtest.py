#!/usr/bin/env python
"""Sensitivitas backtest: bagaimana pagar risiko mengubah jumlah trade & hasil.

Kenapa skrip ini ada: dengan konfigurasi **apa adanya** (modal 50 USDT, leverage 5x,
risiko 1,5%/trade, batas margin 30% per simbol), strategi ini **tidak menghasilkan
satu trade pun** pada BTCUSDT 15m — karena SL hasil ATR(~0,35%) membuat notional
yang dibutuhkan melebihi batas margin. Bot memilih **melewati** trade, bukan
membesarkan posisi.

Skrip ini menampilkan apa yang berubah bila pagar itu dilonggarkan, supaya
keputusan "longgarkan atau tidak" diambil dengan angka, bukan perasaan.

Contoh:
    python scripts/sensitivitas_backtest.py \\
        --candles tests/data/btcusdt_15m_5bulan.csv.gz \\
        --funding tests/data/btcusdt_funding_2025-07_2025-11.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from volumatic_bot.backtest import CostModel, run_backtest
from volumatic_bot.backtest.runner import muat_funding
from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.config import load_config
from volumatic_bot.data.loaders import load_candles_csv

SKENARIO: list[dict[str, object]] = [
    {
        "nama": "bawaan (5x, risiko 1,5%, margin/simbol 30%)",
        "leverage": 5,
        "risk_pct": 1.5,
        "cap": 30.0,
        "equity": 50.0,
    },
    {
        "nama": "modal 500 USDT, pagar sama",
        "leverage": 5,
        "risk_pct": 1.5,
        "cap": 30.0,
        "equity": 500.0,
    },
    {
        "nama": "leverage 10x (batas atas yang diizinkan)",
        "leverage": 10,
        "risk_pct": 1.5,
        "cap": 30.0,
        "equity": 500.0,
    },
    {
        "nama": "margin/simbol 60%",
        "leverage": 5,
        "risk_pct": 1.5,
        "cap": 60.0,
        "equity": 500.0,
    },
    {
        "nama": "risiko 0,5%/trade (paling konservatif)",
        "leverage": 5,
        "risk_pct": 0.5,
        "cap": 30.0,
        "equity": 500.0,
    },
    {
        "nama": "leverage 10x + margin 60%",
        "leverage": 10,
        "risk_pct": 1.5,
        "cap": 60.0,
        "equity": 500.0,
    },
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sensitivitas pagar risiko pada backtest")
    p.add_argument("--candles", required=True)
    p.add_argument("--symbol", default=None)
    p.add_argument("--funding", default=None, help="JSON funding historis")
    p.add_argument("--exchange-info", default="tests/data/exchange_info_sample.json")
    p.add_argument("--json", default=None, help="simpan tabel ke JSON")
    args = p.parse_args(argv)

    klines = load_candles_csv(args.candles)
    info_all = parse_exchange_info(json.loads(Path(args.exchange_info).read_text(encoding="utf-8")))
    simbol = (args.symbol or Path(args.candles).name.split("_")[0]).upper()
    info = info_all.get(simbol)
    dasar = load_config("config.yaml")
    funding = muat_funding(Path(args.funding)) if args.funding else None

    baris: list[dict[str, object]] = []
    for sk in SKENARIO:
        cfg = dasar.model_copy(
            update={
                "risk": dasar.risk.model_copy(
                    update={
                        "risk_per_trade_pct": sk["risk_pct"],
                        "max_per_symbol_margin_pct": sk["cap"],
                    }
                )
            }
        )
        hasil = run_backtest(
            klines,
            info,
            cfg,
            bt=_lewat(sk),
            costs=CostModel(
                taker_bps=cfg.commission.taker_bps,
                slippage_bps=cfg.commission.slippage_bps,
                funding_rate_8h_pct=cfg.commission.funding_rate_8h_pct,
            ),
            funding=funding,
            interval=cfg.scanner.timeframe,
        )
        baris.append(
            {
                "skenario": sk["nama"],
                "equity": sk["equity"],
                "trade": hasil.count,
                "win_rate_pct": round(hasil.win_rate, 1),
                "bersih_usdt": round(hasil.net_usdt, 2),
                "return_pct": round(hasil.return_pct, 2),
                "avg_r": round(hasil.avg_r, 2),
                "max_dd_pct": round(hasil.max_drawdown_pct, 2),
                "dilewati": len(hasil.skipped),
            }
        )

    kepala = (
        f"{'skenario':<46}{'equity':>8}{'trade':>7}{'WR%':>7}"
        f"{'bersih':>10}{'ret%':>8}{'avgR':>7}{'maxDD%':>8}{'lewati':>8}"
    )
    print(kepala)
    print("-" * len(kepala))
    for b in baris:
        print(
            f"{b['skenario']:<46}{b['equity']:>8.0f}{b['trade']:>7}{b['win_rate_pct']:>7.1f}"
            f"{b['bersih_usdt']:>10.2f}{b['return_pct']:>8.2f}{b['avg_r']:>7.2f}"
            f"{b['max_dd_pct']:>8.2f}{b['dilewati']:>8}"
        )
    print(
        "\nCatatan: angka ini hasil simulasi pada data yang diberikan; bukan janji kinerja. "
        "Skenario dengan 'trade 0' berarti pagar risiko memang menolak semua entry."
    )
    if args.json:
        Path(args.json).write_text(
            json.dumps(baris, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"Tabel disimpan: {args.json}")
    return 0


def _lewat(sk: dict[str, object]):  # type: ignore[no-untyped-def]
    from volumatic_bot.backtest import BacktestConfig

    return BacktestConfig(
        equity=float(sk["equity"]),  # type: ignore[arg-type]
        leverage=int(sk["leverage"]),  # type: ignore[arg-type]
    )


if __name__ == "__main__":
    raise SystemExit(main())
