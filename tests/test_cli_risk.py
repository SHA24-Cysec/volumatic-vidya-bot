"""Test CLI TAHAP 4 (``preflight`` & ``sizing``) dan sambungan sinyal → risiko.

``sizing`` butuh exchangeInfo, jadi dipakai server HTTP lokal (bukan Binance asli
yang diblokir 451 dari sandbox).
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from aiohttp import web

from volumatic_bot.cli import main
from volumatic_bot.config import load_config
from volumatic_bot.risk import DailyState, Portfolio, PositionExposure, RiskManager
from volumatic_bot.risk.limits import DailyLimits
from volumatic_bot.signals.pipeline import SignalGate
from volumatic_bot.streams.kline_feed import SignalEvent

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"
JKT = ZoneInfo("Asia/Jakarta")


# --------------------------------------------------------------- server lokal
def _serve_exchange_info() -> tuple[str, threading.Event, Any]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def serve() -> None:
        asyncio.set_event_loop(loop)

        async def exchange_info(_: web.Request) -> web.Response:
            return web.json_response(
                json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
            )

        async def server_time(_: web.Request) -> web.Response:
            return web.json_response({"serverTime": 1_700_000_000_000})

        app = web.Application()
        app.router.add_get("/fapi/v1/exchangeInfo", exchange_info)
        app.router.add_get("/fapi/v1/time", server_time)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr,index]
        holder["url"] = f"http://127.0.0.1:{port}"
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(timeout=10), "server uji gagal menyala"
    return holder["url"], ready, loop


@pytest.fixture(scope="module")
def fake_binance() -> Any:
    url, _ready, loop = _serve_exchange_info()
    try:
        yield url
    finally:
        loop.call_soon_threadsafe(loop.stop)


def write_config(tmp_path: Path, base_url: str, **overrides: Any) -> Path:
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["net"]["rest_base_url"] = base_url
    raw.setdefault("data", {})["db_path"] = str(tmp_path / "uji.db")
    raw["ops"]["kill_switch_file"] = str(tmp_path / "KILL_SWITCH")
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


# ------------------------------------------------------------------ preflight
def test_preflight_lolos_pada_config_default(tmp_path: Path, capsys: Any) -> None:
    cfg = write_config(
        tmp_path, "http://127.0.0.1:1", profil_otomatis={"aktif": False}
    )  # tidak dipakai (tanpa jaringan)
    code = main(["--config", str(cfg), "preflight", "--equity", "40"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Pemeriksaan awal" in out
    assert "modal kecil" in out
    assert "BUKAN jaminan profit" in out


def test_preflight_gagal_bila_kill_switch_ada(tmp_path: Path, capsys: Any) -> None:
    cfg = write_config(tmp_path, "http://127.0.0.1:1")
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    code = main(["--config", str(cfg), "preflight"])
    out = capsys.readouterr().out
    assert code == 1
    assert "[CEK] kill switch" in out


def test_preflight_menolak_mode_live_tanpa_konfirmasi_flag(tmp_path: Path, capsys: Any) -> None:
    """Mode live lolos config pun masih ditolak preflight tanpa flag konfirmasi."""
    cfg = write_config(
        tmp_path,
        "http://127.0.0.1:1",
        ops={
            "mode": "live",
            "allow_live": True,
            "live_confirmation": "SAYA-MENGERTI-RISIKONYA",
        },
    )
    code = main(["--config", str(cfg), "preflight"])
    out = capsys.readouterr().out
    assert code == 1
    assert "[CEK] mode operasi" in out


def test_preflight_menolak_equity_di_bawah_minimum(tmp_path: Path, capsys: Any) -> None:
    cfg = write_config(tmp_path, "http://127.0.0.1:1", profil_otomatis={"aktif": False})
    code = main(["--config", str(cfg), "preflight", "--equity", "5"])
    assert code == 1
    assert "[CEK] equity" in capsys.readouterr().out


def test_preflight_menerapkan_profil_otomatis(tmp_path: Path, capsys: Any) -> None:
    """Equity di bawah ambang -> preflight memeriksa config dengan profil kecil."""
    cfg = write_config(tmp_path, "http://127.0.0.1:1")
    code = main(["--config", str(cfg), "preflight", "--equity", "40"])
    out = capsys.readouterr().out
    assert "modal-kecil (otomatis dari equity)" in out
    assert "risiko per trade" in out
    assert code == 1


def test_preflight_profil_manual_menang_atas_otomatis(tmp_path: Path, capsys: Any) -> None:
    cfg = write_config(tmp_path, "http://127.0.0.1:1")
    code = main(["--profil", "modal-5usdt", "--config", str(cfg), "preflight", "--equity", "40"])
    out = capsys.readouterr().out
    assert "modal-5usdt" in out
    assert "otomatis" not in out.split("profil", 1)[1].split("\n")[0]
    assert code in (0, 1)


# --------------------------------------------------------------------- sizing
def test_sizing_menampilkan_ukuran_dan_level(
    fake_binance: str, tmp_path: Path, capsys: Any
) -> None:
    cfg = write_config(tmp_path, fake_binance)
    code = main(
        [
            "--config",
            str(cfg),
            "sizing",
            "DOGEUSDT",
            "--equity",
            "50",
            "--entry",
            "0.35",
            "--atr-pct",
            "2",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "DOGEUSDT" in out
    assert "Level      :" in out
    assert "SL=" in out and "TP1=" in out and "BE=" in out
    assert "Partial TP" in out


def test_sizing_menolak_simbol_terlalu_besar_untuk_modal(
    fake_binance: str, tmp_path: Path, capsys: Any
) -> None:
    cfg = write_config(tmp_path, fake_binance)
    code = main(
        [
            "--config",
            str(cfg),
            "sizing",
            "BTCUSDT",
            "--equity",
            "100",
            "--entry",
            "90000",
            "--atr-pct",
            "1",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "DITOLAK" in out


def test_sizing_simbol_tidak_ada(fake_binance: str, tmp_path: Path, capsys: Any) -> None:
    cfg = write_config(tmp_path, fake_binance)
    code = main(["--config", str(cfg), "sizing", "TIDAKADAUSDT", "--equity", "50", "--entry", "1"])
    assert code == 1
    assert "tidak ada di exchangeInfo" in capsys.readouterr().err


def test_sizing_short(fake_binance: str, tmp_path: Path, capsys: Any) -> None:
    cfg = write_config(tmp_path, fake_binance)
    code = main(
        [
            "--config",
            str(cfg),
            "sizing",
            "DOGEUSDT",
            "--equity",
            "100",
            "--entry",
            "0.40",
            "--atr-pct",
            "3",
            "--side",
            "short",
            "--trail",
            "0.42",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "(short)" in out


# ------------------------------------------------- sinyal → order book → risiko
class FakeDepth:
    def __init__(self, snapshot: Any) -> None:
        self.snapshot_data = snapshot

    async def snapshot(self, symbol: str) -> Any:
        return self.snapshot_data


def _signal(direction: str = "long") -> SignalEvent:
    return SignalEvent(
        symbol="DOGEUSDT",
        direction=direction,
        open_time=1_764_500_000_000,
        close_price=0.35,
        upper=0.36,
        lower=0.34,
        trail=0.345 if direction == "long" else 0.355,
        atr=0.007,
        atr_pct=2.0,
        cmo=45.0,
        trend=1 if direction == "long" else -1,
        vol_delta_pct=1.0,
        vol_delta_win_pct=60.0,
        zone_count=3.0,
        score=0.5,
        bars=1500,
    )


@pytest.mark.asyncio
async def test_rantai_sinyal_orderbook_risiko() -> None:
    """Sinyal indikator → gate order book → manajer risiko: harus konsisten."""
    from volumatic_bot.orderbook.models import BookLevel, OrderBookSnapshot

    cfg = load_config(ROOT / "config.yaml")
    info = json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
    from volumatic_bot.binance.symbols import parse_exchange_info

    exchange = parse_exchange_info(info)
    book = OrderBookSnapshot(
        symbol="DOGEUSDT",
        bids=tuple(BookLevel(0.34999 - i * 0.00001, 5000.0) for i in range(5)),
        asks=tuple(BookLevel(0.35001 + i * 0.00001, 4000.0) for i in range(5)),
        last_update_id=1,
        event_time_ms=0,
        source="rest",
        fetched_at=datetime.now().timestamp(),
    )
    gate = SignalGate(
        cfg, FakeDepth(book), exchange.symbols.get, clock=lambda: datetime.now().timestamp()
    )
    candidate = await gate.evaluate(_signal())
    assert candidate.tradable, candidate.rejection_reason()

    limits = DailyLimits(
        daily_loss_limit_pct=cfg.risk.daily_loss_limit_pct,
        max_trades_per_day=cfg.risk.max_trades_per_day,
        consecutive_loss_limit=cfg.risk.consecutive_loss_limit,
        cooldown_minutes=cfg.risk.cooldown_minutes,
        reset_hour_local=cfg.risk.daily_loss_reset_hour_local,
        timezone=cfg.app.timezone,
    )
    mgr = RiskManager(
        cfg,
        daily=DailyState(limits=limits, start_equity=100.0, current_equity=100.0),
        clock=lambda: datetime(2026, 11, 10, 10, 0, tzinfo=JKT),
        kill_switch_path=Path("/tmp/tidak-ada-kill-tahap4"),
    )
    ticket = mgr.evaluate(
        symbol=candidate.symbol,
        side=candidate.direction,
        entry_price=candidate.signal.close_price,
        atr=candidate.signal.atr,
        trail=candidate.signal.trail,
        equity=100.0,
        portfolio=Portfolio(equity=100.0, positions=()),
        info=exchange.get("DOGEUSDT"),
        open_positions=0,
    )
    assert ticket.approved, ticket.reasons
    assert ticket.plan is not None
    assert ticket.plan.stop_price < ticket.plan.entry_price  # long
    assert ticket.size is not None and ticket.size.risk_pct <= 2.0


@pytest.mark.asyncio
async def test_rantai_berhenti_bila_orderbook_menolak() -> None:
    from volumatic_bot.orderbook.models import BookLevel, OrderBookSnapshot

    cfg = load_config(ROOT / "config.yaml")
    from volumatic_bot.binance.symbols import parse_exchange_info

    exchange = parse_exchange_info(
        json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
    )
    # spread lebar -> gate menolak, manajer risiko tidak boleh dipanggil
    lebar = OrderBookSnapshot(
        symbol="DOGEUSDT",
        bids=(BookLevel(0.34, 100.0),),
        asks=(BookLevel(0.36, 100.0),),
        last_update_id=1,
        event_time_ms=0,
        source="rest",
        fetched_at=datetime.now().timestamp(),
    )
    gate = SignalGate(
        cfg, FakeDepth(lebar), exchange.symbols.get, clock=lambda: datetime.now().timestamp()
    )
    candidate = await gate.evaluate(_signal())
    assert candidate.tradable is False
    assert "spread" in (candidate.rejection_reason() or "")


def test_portofolio_menghitung_ulang_setelah_satu_posisi() -> None:
    porto = Portfolio(
        equity=100.0,
        positions=(PositionExposure("DOGEUSDT", 75.0, 15.0, 5),),
        max_total_margin_pct=60.0,
        max_per_symbol_margin_pct=30.0,
    )
    assert porto.free_margin == pytest.approx(45.0)
    assert porto.has_symbol("DOGEUSDT")
    assert porto.to_rows()[0]["symbol"] == "DOGEUSDT"
