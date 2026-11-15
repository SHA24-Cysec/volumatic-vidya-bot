"""Test runner backtest pada data sintetis + pemeriksaan statistik."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from volumatic_bot.backtest import BacktestConfig, CostModel, run_backtest
from volumatic_bot.backtest.runner import muat_funding
from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.config import load_config
from volumatic_bot.data.loaders import load_candles_csv
from volumatic_bot.models import CandleSeries

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"
INTERVAL_MS = 15 * 60 * 1000


def exchange() -> Any:
    return parse_exchange_info(json.loads((DATA / "exchange_info_sample.json").read_text("utf-8")))


def sinyal_series(n: int = 900, seed: int = 7) -> CandleSeries:
    """Deret dengan tren naik-turun bergantian supaya ada persilangan VIDYA."""
    rng = np.random.default_rng(seed)
    harga: list[float] = []
    level = 1.0
    for i in range(n):
        arah = 1.0 if (i // 60) % 2 == 0 else -1.0
        level *= 1.0 + arah * 0.004 + rng.normal(0, 0.0008)
        harga.append(max(level, 0.1))
    close = np.array(harga, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.002, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.002, n))
    volume = rng.uniform(1000, 3000, n)
    return CandleSeries.from_columns(
        open_time=[1_700_000_000_000 + i * INTERVAL_MS for i in range(n)],
        open=open_.tolist(),
        high=high.tolist(),
        low=low.tolist(),
        close=close.tolist(),
        volume=volume.tolist(),
    )


def config(**overrides: Any) -> Any:
    cfg = load_config(ROOT / "config.yaml")
    if not overrides:
        return cfg
    dump = cfg.model_dump()
    for key, value in overrides.items():
        dump[key].update(value)
    from volumatic_bot.config import AppConfig

    return AppConfig(**dump)


def test_data_pendek_dilaporkan_jujur() -> None:
    seri = sinyal_series(100)
    hasil = run_backtest(
        seri, exchange().get("DOGEUSDT"), config(), bt=BacktestConfig(equity=500.0)
    )
    assert hasil.count == 0
    assert any("terlalu pendek" in n for n in hasil.notes)
    assert hasil.equity_curve  # tetap ada titik kurva


def test_biaya_nol_menghasilkan_hasil_berbeda_dari_berbiaya() -> None:
    seri = sinyal_series()
    info = exchange().get("DOGEUSDT")
    cfg = config(risk={"risk_per_trade_pct": 0.5})
    bt = BacktestConfig(equity=500.0)
    dengan = run_backtest(seri, info, cfg, bt=bt, costs=CostModel(taker_bps=5, slippage_bps=2))
    tanpa = run_backtest(seri, info, cfg, bt=bt, costs=CostModel(taker_bps=0, slippage_bps=0))
    if dengan.count and tanpa.count:
        # dengan biaya tidak mungkin lebih untung daripada tanpa biaya
        assert dengan.net_usdt < tanpa.net_usdt
    assert dengan.fees_usdt >= 0 and tanpa.fees_usdt == 0.0


def test_statistik_konsisten() -> None:
    seri = sinyal_series()
    cfg = config(risk={"risk_per_trade_pct": 0.5, "max_per_symbol_margin_pct": 60.0})
    hasil = run_backtest(
        seri, exchange().get("DOGEUSDT"), cfg, bt=BacktestConfig(equity=500.0, leverage=10)
    )
    assert hasil.wins + hasil.losses <= hasil.count
    assert 0 <= hasil.win_rate <= 100
    assert hasil.total_costs == hasil.fees_usdt + hasil.slippage_usdt + hasil.funding_usdt
    assert sum(hasil.reasons().values()) == hasil.count
    if hasil.count:
        assert hasil.equity_curve[-1][1] == hasil.end_equity or hasil.end_equity > 0


def test_pagar_margin_dihormati_dan_menolak_sebagian_entry() -> None:
    """Margin tiap trade tidak boleh melebihi batas; entry yang terlalu besar dilewati."""
    seri = sinyal_series()
    ketat = run_backtest(
        seri,
        exchange().get("DOGEUSDT"),
        config(risk={"risk_per_trade_pct": 1.5, "max_per_symbol_margin_pct": 15.0}),
        bt=BacktestConfig(equity=200.0),
    )
    longgar = run_backtest(
        seri,
        exchange().get("DOGEUSDT"),
        # margin/simbol 80% > 60% kini butuh saklar agresif (docs/modal-kecil.md);
        # uji ini hanya ingin pagar margin yang longgar, bukan risiko besar.
        config(
            risk={
                "risk_per_trade_pct": 1.5,
                "max_per_symbol_margin_pct": 80.0,
                "allow_aggressive_risk": True,
            }
        ),
        bt=BacktestConfig(equity=200.0),
    )
    # batas lebih ketat tidak boleh menghasilkan lebih banyak trade
    assert ketat.count <= longgar.count
    assert ketat.skipped_by_margin() or ketat.count == 0
    for trade in longgar.trades:
        margin = trade.notional / 5.0  # leverage baku 5x
        assert margin <= 0.80 * 200.0 + 1e-9


def test_funding_dari_berkas_menandai_data_tersedia(tmp_path: Path) -> None:
    berkas = tmp_path / "funding.json"
    berkas.write_text(
        json.dumps(
            [
                {"fundingTime": 1_700_000_000_000 + i * 8 * 3600_000, "fundingRate": 0.0001}
                for i in range(50)
            ]
        ),
        encoding="utf-8",
    )
    seri = muat_funding(berkas)
    assert len(seri) == 50
    assert seri.source == "binance"

    hasil = run_backtest(
        sinyal_series(),
        exchange().get("DOGEUSDT"),
        config(risk={"risk_per_trade_pct": 0.5}),
        bt=BacktestConfig(equity=500.0),
        funding=seri,
    )
    assert hasil.funding_available is True
    assert not any("funding tidak tersedia" in n for n in hasil.notes)


def test_tanpa_funding_menyatakan_asumsi() -> None:
    hasil = run_backtest(
        sinyal_series(), exchange().get("DOGEUSDT"), config(), bt=BacktestConfig(equity=500.0)
    )
    assert hasil.funding_available is False
    assert any("funding tidak tersedia" in n for n in hasil.notes)


def test_posisi_terbuka_di_akhir_data_ditutup() -> None:
    seri = sinyal_series()
    cfg = config(risk={"risk_per_trade_pct": 0.5, "max_per_symbol_margin_pct": 60.0})
    hasil = run_backtest(
        seri, exchange().get("DOGEUSDT"), cfg, bt=BacktestConfig(equity=500.0, leverage=10)
    )
    if hasil.count:
        assert hasil.trades[-1].reason in ("akhir_data", "stop_loss", "stop_loss_be", "take_profit")


def test_statistik_hasil_kosong_aman() -> None:
    dari = run_backtest(sinyal_series(120), exchange().get("DOGEUSDT"), config())
    assert dari.win_rate == 0.0
    assert dari.avg_r == 0.0
    assert dari.profit_factor == 0.0
    assert dari.max_drawdown_pct >= 0.0
    assert "0 trade" in dari.summary()


def test_data_nyata_lima_bulan_jalan() -> None:
    """Uji asap pada data Binance asli yang disimpan di repo (tanpa jaringan)."""
    berkas = DATA / "btcusdt_15m_5bulan.csv.gz"
    if not berkas.exists():  # pragma: no cover - data uji opsional
        return
    seri = load_candles_csv(berkas, max_rows=2000)
    hasil = run_backtest(seri, exchange().get("BTCUSDT"), config(), bt=BacktestConfig(equity=50.0))
    assert hasil.bars == seri.open_time.size
    assert hasil.count >= 0
    assert hasil.summary().startswith("BTCUSDT")
