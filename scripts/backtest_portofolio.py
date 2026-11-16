"""Simulasi portofolio multi-simbol untuk mengukur strategi sebagai SATU akun.

Kenapa bukan ``volumatic-bot backtest``: perintah itu mengukur satu simbol tanpa
batas jumlah posisi bersamaan. Hasil per simbol tidak bisa dijumlahkan begitu
saja karena (a) modal dipakai bersama, (b) jumlah posisi dibatasi, dan (c) satu
simbol yang untung tidak menutup simbol lain kalau slotnya sudah penuh.

Aturan yang dipakai (mendekati ``risk/sizing.py`` + ``risk/manager.py``):
* risiko per trade = ``risk_pct`` x equity saat entry, qty dihitung dari jarak SL;
* maksimal ``max_pos`` posisi bersamaan, satu posisi per simbol;
* margin total <= 60% equity, margin per simbol <= 30% equity (dikali leverage);
* notional minimum 20 USDT (minNotional Binance);
* fee taker 5 bps + slippage 2 bps dua sisi + funding historis asli;
* entry di open bar berikutnya, SL didahulukan bila SL & TP kena di bar sama.

Berkas data yang dibutuhkan: CSV/CSV.GZ kline (format data.binance.vision) dan
JSON funding (``scripts/unduh_funding.py``). Nama berkas: ``btcusdt_15m.csv.gz``
dan ``btcusdt_funding.json`` dalam satu folder.

Contoh:
    python scripts/backtest_portofolio.py --data-dir data/raw --tf 4h \
        --dist200-max -8 --max-pos 2 --risk-pct 1.5
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from volumatic_bot.backtest.costs import FundingSeries
from volumatic_bot.models import CandleSeries
from volumatic_bot.regime import build_regime_from_daily, daily_candles

MIN_NOTIONAL = 20.0
LEVERAGE = 10
MAX_MARGIN_PCT = 60.0
PER_SYMBOL_MARGIN_PCT = 30.0

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT",
    "LTCUSDT", "TRXUSDT", "AVAXUSDT", "ATOMUSDT", "NEARUSDT", "FILUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SUIUSDT", "INJUSDT",
]
#: tickSize, stepSize, minNotional (Binance USDT-M, Nov 2026, dibulatkan konservatif)
FILTERS = {
    "BTCUSDT": ("0.10", "0.001", "100"), "ETHUSDT": ("0.01", "0.001", "20"),
    "BNBUSDT": ("0.01", "0.01", "20"), "SOLUSDT": ("0.01", "0.01", "20"),
    "XRPUSDT": ("0.0001", "0.1", "20"), "DOGEUSDT": ("0.00001", "1", "20"),
    "ADAUSDT": ("0.0001", "1", "20"), "LINKUSDT": ("0.01", "0.01", "20"),
    "LTCUSDT": ("0.01", "0.01", "20"), "TRXUSDT": ("0.00001", "1", "20"),
    "AVAXUSDT": ("0.001", "0.01", "20"), "ATOMUSDT": ("0.001", "0.01", "20"),
    "NEARUSDT": ("0.001", "0.1", "20"), "FILUSDT": ("0.001", "0.1", "20"),
    "APTUSDT": ("0.001", "0.1", "20"), "ARBUSDT": ("0.0001", "0.1", "20"),
    "OPUSDT": ("0.0001", "0.1", "20"), "SUIUSDT": ("0.0001", "0.1", "20"),
    "INJUSDT": ("0.001", "0.1", "20"),
}


@dataclass
class Params:
    tf: str = "4h"
    sl_atr: float = 2.0
    tp_r: float = 2.0
    exit_on_flip: bool = True
    max_bars: int = 0
    vol_min: float = 0.0
    dist200_max: float = 1e9
    risk_pct: float = 1.5
    equity0: float = 500.0
    taker_bps: float = 5.0
    slip_bps: float = 2.0
    max_pos: int = 2


@dataclass
class SymbolData:
    series: CandleSeries
    ind: object
    funding: FundingSeries | None
    ema200: np.ndarray | None


def _ema(values: np.ndarray, length: int) -> np.ndarray:
    alpha = 2.0 / (length + 1.0)
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, values.size):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def _resample(series: CandleSeries, tf: str) -> CandleSeries:
    mult = {"15m": 1, "30m": 2, "1h": 4, "2h": 8, "4h": 16}[tf]
    if mult == 1:
        return series
    step = 900_000 * mult
    df = pd.DataFrame(
        dict(
            t=np.asarray(series.open_time, dtype=np.int64),
            o=np.asarray(series.open, dtype=float),
            h=np.asarray(series.high, dtype=float),
            l=np.asarray(series.low, dtype=float),
            c=np.asarray(series.close, dtype=float),
            v=np.asarray(series.volume, dtype=float),
        )
    )
    df["b"] = df.t // step * step
    g = df.groupby("b").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"),
                            c=("c", "last"), v=("v", "sum"), n=("t", "size")).reset_index()
    g = g[g.n == mult]
    return CandleSeries.from_columns(
        open_time=g.b.to_numpy(np.int64).tolist(), open=g.o.tolist(), high=g.h.tolist(),
        low=g.l.tolist(), close=g.c.tolist(), volume=g.v.tolist(),
    )


def _fitur(series: CandleSeries, params: Params) -> SymbolData:
    from volumatic_bot.config import load_config  # noqa: PLC0415
    from volumatic_bot.indicator import compute  # noqa: PLC0415

    cfg = load_config(Path(__file__).parents[1] / "config.yaml")
    ind = compute(series, cfg.indicator.to_params())
    ema200 = None
    if params.dist200_max < 1e8:
        try:
            harian = daily_candles(series)
        except ValueError:
            harian = None
        if harian is not None and len(harian) > 2:
            rezim = build_regime_from_daily(
                CandleSeries.from_columns(
                    open_time=[int(x) for x in harian.open_time[:-1]],
                    open=[float(x) for x in harian.open[:-1]],
                    high=[float(x) for x in harian.high[:-1]],
                    low=[float(x) for x in harian.low[:-1]],
                    close=[float(x) for x in harian.close[:-1]],
                    volume=[float(x) for x in harian.volume[:-1]],
                )
            )
            day = np.asarray(series.open_time, dtype=np.int64) // 86_400_000 * 86_400_000
            idx = np.searchsorted(rezim.open_time, day, side="left") - 1
            ema200 = np.where(idx >= 0, rezim.ema200[np.clip(idx, 0, None)], np.nan)
    return SymbolData(series=series, ind=ind, funding=None, ema200=ema200)


def _muat(data_dir: Path, tf: str, params: Params, symbols: list[str]) -> dict[str, SymbolData]:
    from volumatic_bot.data.loaders import load_candles_csv  # noqa: PLC0415

    out: dict[str, SymbolData] = {}
    for symbol in symbols:
        kline = data_dir / f"{symbol.lower()}_15m.csv.gz"
        if not kline.exists():
            kline = data_dir / f"{symbol.lower()}_15m.csv"
        if not kline.exists():
            continue
        series = _resample(load_candles_csv(kline), tf)
        data = _fitur(series, params)
        funding_file = data_dir / f"{symbol.lower()}_funding.json"
        if funding_file.exists():
            rows = [r for r in json.loads(funding_file.read_text()) if "fundingTime" in r]
            data.funding = FundingSeries.from_rows(rows)
        out[symbol] = data
    return out


def simulate(data: dict[str, SymbolData], p: Params, *, start_ms: int | None = None,
             end_ms: int | None = None):
    fee = p.taker_bps / 10_000.0
    slip = p.slip_bps / 10_000.0
    events = []
    for symbol, item in data.items():
        ot = np.asarray(item.series.open_time, dtype=np.int64)
        mask = np.ones(len(ot), dtype=bool)
        if start_ms is not None:
            mask &= ot >= start_ms
        if end_ms is not None:
            mask &= ot <= end_ms
        for i in np.flatnonzero(mask):
            events.append((int(ot[i]), symbol, int(i)))
    events.sort()

    eq = p.equity0
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    curve = [(events[0][0] if events else 0, eq)]
    last_close: dict[str, int] = {s: -10**9 for s in data}

    for ts, symbol, i in events:
        item = data[symbol]
        s = item.series
        high, low, close = float(s.high[i]), float(s.low[i]), float(s.close[i])
        px_open = float(s.open[i])
        atr = np.asarray(item.ind.atr)  # type: ignore[attr-defined]
        trend = np.asarray(item.ind.trend)  # type: ignore[attr-defined]
        cross_up = np.asarray(item.ind.cross_up)  # type: ignore[attr-defined]
        cross_down = np.asarray(item.ind.cross_down)  # type: ignore[attr-defined]
        vol = np.asarray(item.series.volume)

        pos = positions.get(symbol)
        if pos is not None:
            arah = 1.0 if pos["side"] == "long" else -1.0
            pos["bars"] += 1
            exit_price = reason = None
            if arah > 0:
                if low <= pos["stop"]:
                    exit_price, reason = pos["stop"], "sl"
                elif high >= pos["tp"]:
                    exit_price, reason = pos["tp"], "tp"
                elif p.exit_on_flip and trend[i] == -1:
                    exit_price, reason = close, "flip"
            else:
                if high >= pos["stop"]:
                    exit_price, reason = pos["stop"], "sl"
                elif low <= pos["tp"]:
                    exit_price, reason = pos["tp"], "tp"
                elif p.exit_on_flip and trend[i] == 1:
                    exit_price, reason = close, "flip"
            if exit_price is None and p.max_bars and pos["bars"] >= p.max_bars:
                exit_price, reason = close, "time"
            if exit_price is not None:
                fill = exit_price * (1 - arah * slip)
                gross = (fill - pos["entry"]) * arah * pos["qty"]
                n_in, n_out = pos["qty"] * pos["entry"], pos["qty"] * fill
                fees = (n_in + n_out) * fee
                fund = 0.0
                if item.funding is not None:
                    fund = item.funding.cost_usdt(
                        notional=n_in, side=pos["side"], start_ms=pos["ts_open"], end_ms=ts
                    ) or 0.0
                net = gross - fees - fund
                eq += net
                trades.append(dict(symbol=symbol, side=pos["side"], ts_open=pos["ts_open"],
                                   ts_close=ts, r=net / pos["risk_usdt"], net=net,
                                   reason=reason, bars=pos["bars"]))
                del positions[symbol]
                last_close[symbol] = i
                curve.append((ts, eq))
                continue

        if symbol in positions or len(positions) >= p.max_pos:
            continue
        j = i - 1
        if j < 1 or last_close[symbol] > j:
            continue
        direction = "long" if cross_up[j] else ("short" if cross_down[j] else None)
        if direction is None:
            continue
        if p.vol_min > 0:
            rata = float(np.mean(vol[j - 20 : j])) if j >= 20 else np.nan
            if not np.isfinite(rata) or rata <= 0 or vol[j] / rata < p.vol_min:
                continue
        a = float(atr[j])
        if not np.isfinite(a) or a <= 0:
            continue
        if item.ema200 is not None:
            ema = float(item.ema200[j])
            if not np.isfinite(ema) or ema <= 0:
                continue
            side_sign = 1.0 if direction == "long" else -1.0
            if (close / ema - 1.0) * 100.0 * side_sign > p.dist200_max:
                continue
        stop_dist = p.sl_atr * a
        qty = (eq * p.risk_pct / 100.0) / stop_dist
        notional = qty * px_open
        if notional > eq * PER_SYMBOL_MARGIN_PCT / 100.0 * LEVERAGE:
            notional = eq * PER_SYMBOL_MARGIN_PCT / 100.0 * LEVERAGE
            qty = notional / px_open
        if notional < MIN_NOTIONAL:
            continue
        entry = px_open * (1 + (slip if direction == "long" else -slip))
        positions[symbol] = dict(
            side=direction, entry=entry, qty=qty,
            stop=entry - stop_dist if direction == "long" else entry + stop_dist,
            tp=entry + p.tp_r * stop_dist if direction == "long" else entry - p.tp_r * stop_dist,
            risk_usdt=stop_dist * qty, bars=0, ts_open=ts,
        )
    return trades, curve


def report(tag: str, trades: list[dict], curve: list[tuple[int, float]], eq0: float) -> dict:
    if not trades:
        print(f"{tag:<34} 0 trade")
        return {}
    df = pd.DataFrame(trades).sort_values("ts_close")
    eq = eq0 + float(df.net.sum())
    peak, dd = eq0, 0.0
    for _, nilai in sorted(curve, key=lambda x: x[0]):
        peak = max(peak, nilai)
        dd = max(dd, (peak - nilai) / peak * 100.0)
    tahun = (df.ts_close.max() - df.ts_open.min()) / (365.25 * 86_400_000)
    total = (eq / eq0 - 1) * 100.0
    cagr = ((eq / eq0) ** (1 / tahun) - 1) * 100.0 if tahun > 0 and eq > 0 else -100.0
    untung = float(df.net[df.net > 0].sum())
    rugi = float(-df.net[df.net <= 0].sum())
    print(
        f"{tag:<34} trade={len(df):<4} akhir={eq:8.1f} tot={total:+7.1f}% CAGR={cagr:+6.1f}% "
        f"maxDD={dd:5.1f}% WR={100 * (df.net > 0).mean():5.1f}% PF={(untung / rugi if rugi else 0):.2f} "
        f"avgR={df.r.mean():+.3f}"
    )
    return dict(eq=eq, total=total, cagr=cagr, dd=dd, trades=len(df))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backtest portofolio multi-simbol")
    p.add_argument("--data-dir", default="data/raw")
    p.add_argument("--tf", default="4h")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--dist200-max", type=float, default=-8.0,
                   help="batas jarak ke EMA200 harian (persen, searah trade); 99 = filter mati")
    p.add_argument("--vol-min", type=float, default=0.0)
    p.add_argument("--max-pos", type=int, default=2)
    p.add_argument("--risk-pct", type=float, default=1.5)
    p.add_argument("--equity", type=float, default=500.0)
    p.add_argument("--sl-atr", type=float, default=2.0)
    p.add_argument("--tp-r", type=float, default=2.0)
    p.add_argument("--max-bars", type=int, default=0)
    p.add_argument("--no-flip", action="store_true", help="jangan keluar saat tren berganti")
    p.add_argument("--mulai", default=None, help="tanggal awal, mis. 2023-01-01")
    p.add_argument("--selesai", default=None, help="tanggal akhir (inklusif), mis. 2024-12-31")
    args = p.parse_args(argv)

    params = Params(
        tf=args.tf, dist200_max=args.dist200_max, vol_min=args.vol_min, max_pos=args.max_pos,
        risk_pct=args.risk_pct, equity0=args.equity, sl_atr=args.sl_atr, tp_r=args.tp_r,
        max_bars=args.max_bars, exit_on_flip=not args.no_flip,
    )
    data = _muat(Path(args.data_dir), args.tf, params, args.symbols or SYMBOLS)
    if not data:
        print("tidak ada data yang cocok di folder tersebut", file=sys.stderr)
        return 1
    print(f"{len(data)} simbol, timeframe {args.tf}, filter dist200 <= {args.dist200_max:+.1f}%")
    start_ms = int(pd.Timestamp(args.mulai, tz="UTC").value // 10**6) if args.mulai else None
    end_ms = (
        int(pd.Timestamp(args.selesai, tz="UTC").value // 10**6) + 86_400_000
        if args.selesai
        else None
    )
    trades, curve = simulate(data, params, start_ms=start_ms, end_ms=end_ms)
    report(
        f"{args.tf} maks {args.max_pos} posisi risiko {args.risk_pct}%",
        trades, curve, params.equity0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
