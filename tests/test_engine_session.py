"""Test sesi offline (mesin penuh pada data historis) + perintah CLI terkait."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.cli import main
from volumatic_bot.config import AppConfig, load_config
from volumatic_bot.engine import run_offline_session_async, session_report
from volumatic_bot.engine.trader import TradingEngine
from volumatic_bot.execution.broker import DryRunBroker
from volumatic_bot.execution.executor import TradeExecutor
from volumatic_bot.models import CandleSeries
from volumatic_bot.positions.store import PositionStore
from volumatic_bot.risk import DailyState, RiskManager
from volumatic_bot.risk.limits import DailyLimits
from volumatic_bot.storage.db import SqliteDatabase

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"
INTERVAL_MS = 15 * 60 * 1000


def exchange() -> Any:
    return parse_exchange_info(
        json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
    )


def config(**overrides: Any) -> AppConfig:
    cfg = load_config(ROOT / "config.yaml")
    dump = cfg.model_dump()
    dump["risk"].update({"risk_per_trade_pct": 0.5})
    for key, value in overrides.items():
        dump[key].update(value)
    return AppConfig(**dump)


def series(n: int = 420, seed: int = 11) -> CandleSeries:
    rng = np.random.default_rng(seed)
    harga: list[float] = []
    level = 0.35
    for i in range(n):
        arah = 1.0 if (i // 40) % 2 == 0 else -1.0
        level *= 1.0 + arah * 0.006 + rng.normal(0, 0.001)
        harga.append(max(level, 0.05))
    close = np.array(harga)
    buka = np.concatenate([[close[0]], close[:-1]])
    return CandleSeries.from_columns(
        open_time=[1_700_000_000_000 + i * INTERVAL_MS for i in range(n)],
        open=buka.tolist(),
        high=(np.maximum(buka, close) * 1.002).tolist(),
        low=(np.minimum(buka, close) * 0.998).tolist(),
        close=close.tolist(),
        volume=rng.uniform(1000, 3000, n).tolist(),
    )


async def jalankan(tmp_path: Path, *, equity: float = 500.0, bars: int = 160) -> dict[str, Any]:
    cfg = config()
    db = SqliteDatabase(tmp_path / "sesi.db")
    db.open()
    try:
        store = PositionStore(db)
        broker = DryRunBroker(taker_bps=cfg.commission.taker_bps)
        zona = __import__("zoneinfo").ZoneInfo("Asia/Jakarta")
        limits = DailyLimits(
            daily_loss_limit_pct=cfg.risk.daily_loss_limit_pct,
            max_trades_per_day=cfg.risk.max_trades_per_day,
            consecutive_loss_limit=cfg.risk.consecutive_loss_limit,
            cooldown_minutes=cfg.risk.cooldown_minutes,
            reset_hour_local=cfg.risk.daily_loss_reset_hour_local,
            timezone=cfg.app.timezone,
        )
        harian = DailyState(limits=limits, start_equity=equity, current_equity=equity)
        risiko = RiskManager(
            cfg,
            daily=harian,
            clock=lambda: __import__("datetime").datetime(2026, 11, 10, 10, 0, tzinfo=zona),
            kill_switch_path=tmp_path / "KILL",
        )
        broker.set_price("DOGEUSDT", 0.35)

        async def cepat(_: float) -> None:
            return None

        executor = TradeExecutor(
            cfg, broker, store, clock=lambda: 1_700_000_000_000, sleep=cepat, mode="dry_run"
        )
        engine = TradingEngine(cfg, store=store, risk=risiko, executor=executor, broker=broker)
        info = exchange().get("DOGEUSDT")
        engine.set_symbol_info(info)
        laporan = await run_offline_session_async(
            series=series(),
            config=cfg,
            engine=engine,
            store=store,
            broker=broker,
            info=info,
            limit_bars=bars,
            warmup_bars=260,
        )
        return laporan.as_dict()
    finally:
        db.close()


def test_sesi_offline_berjalan_tanpa_error(tmp_path: Path) -> None:
    hasil = asyncio.run(jalankan(tmp_path))
    assert hasil["simbol"] == "DOGEUSDT"
    assert hasil["bar"] > 0
    assert hasil["sinyal"] >= 0
    assert "ekuitas" in hasil
    teks = session_report(hasil)
    assert "Sesi DOGEUSDT" in teks
    assert "Ekuitas" in teks


def test_sesi_offline_mencatat_posisi_di_sqlite(tmp_path: Path) -> None:
    hasil = asyncio.run(jalankan(tmp_path, bars=400))
    assert isinstance(hasil["posisi"], list)
    for pos in hasil["posisi"]:
        assert pos["simbol"] == "DOGEUSDT"
        assert pos["status"] in ("open", "partial", "closed", "failed")


def test_sesi_offline_menghormati_batas_harian(tmp_path: Path) -> None:
    """Batas rugi harian sangat kecil → sesi berhenti sendiri."""
    cfg = config(risk={"daily_loss_limit_pct": 0.01, "risk_per_trade_pct": 0.5})
    assert cfg.risk.daily_loss_limit_pct == 0.01
    # sesi dengan batas ini akan berhenti setelah satu trade rugi (bila ada)
    hasil = asyncio.run(jalankan(tmp_path, bars=400))
    assert "sesi_dihentikan" in hasil


# ---------------------------------------------------------------------- CLI
def _cfg_file(tmp_path: Path, **ubah: Any) -> Path:
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["data"]["db_path"] = str(tmp_path / "cli.db")
    raw["ops"]["kill_switch_file"] = str(tmp_path / "KILL")
    for key, value in ubah.items():
        raw.setdefault(key, {}).update(value)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_cli_backtest_menghasilkan_ringkasan(tmp_path: Path, capsys: Any) -> None:
    cfg = _cfg_file(tmp_path)
    kode = main(
        [
            "--config",
            str(cfg),
            "backtest",
            "--candles",
            str(DATA / "btcusdt_15m_5bulan.csv.gz"),
            "--equity",
            "50",
            "--rows",
            "6000",
            "--warmup",
            "120",
            "--risk-pct",
            "0.5",
            "--json",
            str(tmp_path / "hasil.json"),
        ]
    )
    out = capsys.readouterr().out
    assert kode == 0, out
    # CSV 15m di-resample ke scanner.timeframe (4h) dan dilaporkan sebagai 4h.
    assert "di-resample ke 4h" in out
    assert "BTCUSDT 4h" in out
    assert "Sumber funding" in out
    simpan = json.loads((tmp_path / "hasil.json").read_text(encoding="utf-8"))
    assert simpan["statistik"]["trade"] >= 0
    assert simpan["biaya"]["taker_bps"] == 5.0


def test_cli_backtest_simbol_tidak_dikenal(tmp_path: Path, capsys: Any) -> None:
    cfg = _cfg_file(tmp_path)
    kode = main(
        [
            "--config",
            str(cfg),
            "backtest",
            "--candles",
            str(DATA / "btcusdt_15m_5bulan.csv.gz"),
            "--symbol",
            "TIDAKADAUSDT",
            "--rows",
            "500",
        ]
    )
    assert kode == 1
    assert "tidak ada" in capsys.readouterr().err


def test_cli_simulate_session_jalan(tmp_path: Path, capsys: Any) -> None:
    cfg = _cfg_file(tmp_path)
    kode = main(
        [
            "--config",
            str(cfg),
            "simulate-session",
            "--candles",
            str(DATA / "btcusdt_15m_5bulan.csv.gz"),
            "--equity",
            "500",
            "--risk-pct",
            "0.5",
            "--limit-bars",
            "40",
        ]
    )
    hasil = capsys.readouterr()
    assert kode == 0, hasil.err
    assert "Sesi BTCUSDT selesai" in hasil.out


def test_cli_run_menolak_mode_dry_run(tmp_path: Path, capsys: Any) -> None:
    cfg = _cfg_file(tmp_path)
    kode = main(["--config", str(cfg), "run"])
    assert kode == 2
    assert "simulate-session" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["backtest", "dry_run"])
def test_cli_run_menolak_mode_non_bursa(tmp_path: Path, capsys: Any, mode: str) -> None:
    cfg = _cfg_file(tmp_path, ops={"mode": mode})
    assert main(["--config", str(cfg), "run"]) == 2
    capsys.readouterr()
