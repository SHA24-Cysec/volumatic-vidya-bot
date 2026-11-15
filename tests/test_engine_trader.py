"""Test mesin trading: alur sinyal, pagar, penutupan, dan batas harian."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest

from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.config import AppConfig, load_config
from volumatic_bot.dashboard.sources import LocalStateSource
from volumatic_bot.engine.trader import TradingEngine
from volumatic_bot.execution.broker import DryRunBroker
from volumatic_bot.execution.executor import TradeExecutor
from volumatic_bot.notify import NotifierDispatcher
from volumatic_bot.positions.models import PositionState
from volumatic_bot.positions.store import PositionStore
from volumatic_bot.risk import DailyState, RiskManager
from volumatic_bot.risk.limits import DailyLimits
from volumatic_bot.storage.db import SqliteDatabase
from volumatic_bot.streams.kline_feed import SignalEvent

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"


class Kanal:
    name = "uji"

    def __init__(self) -> None:
        self.pesan: list[Any] = []

    def send(self, event: Any) -> bool:
        self.pesan.append(event)
        return True


def exchange() -> Any:
    return parse_exchange_info(
        json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
    )


def config(**overrides: Any) -> AppConfig:
    cfg = load_config(ROOT / "config.yaml")
    if not overrides:
        return cfg
    dump = cfg.model_dump()
    for key, value in overrides.items():
        dump[key].update(value)
    return AppConfig(**dump)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    db = SqliteDatabase(tmp_path / "eng.db")
    db.open()
    yield PositionStore(db)
    db.close()


def sinyal(symbol: str = "DOGEUSDT", harga: float = 0.35, arah: str = "long") -> SignalEvent:
    return SignalEvent(
        symbol=symbol,
        direction=arah,
        open_time=1_700_000_000_000,
        close_price=harga,
        upper=harga * 1.02,
        lower=harga * 0.98,
        trail=harga * 0.995 if arah == "long" else harga * 1.005,
        atr=harga * 0.02,
        atr_pct=2.0,
        cmo=60.0,
        trend=1,
        vol_delta_pct=10.0,
        vol_delta_win_pct=12.0,
        zone_count=3.0,
        score=0.8,
        bars=900,
    )


def rakit(
    store: PositionStore,
    *,
    equity: float = 500.0,
    cfg: AppConfig | None = None,
    kill_file: str = "/tmp/kill-tidak-ada",
    kanal: Kanal | None = None,
) -> tuple[TradingEngine, DryRunBroker, Any, Kanal]:
    cfg = cfg or config(risk={"risk_per_trade_pct": 0.5})
    zona = ZoneInfo("Asia/Jakarta")
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
        clock=lambda: datetime(2026, 11, 10, 10, 0, tzinfo=zona),
        kill_switch_path=Path(kill_file),
    )
    broker = DryRunBroker(taker_bps=cfg.commission.taker_bps)
    broker.set_price("DOGEUSDT", 0.35)

    async def cepat(_: float) -> None:
        return None

    executor = TradeExecutor(
        cfg, broker, store, clock=lambda: 1_700_000_000_000, sleep=cepat, mode="dry_run"
    )
    k = kanal or Kanal()
    engine = TradingEngine(
        cfg,
        store=store,
        risk=risiko,
        executor=executor,
        broker=broker,
        notifier=NotifierDispatcher([k], dedupe_sec=0),
        dashboard=LocalStateSource(store, mode="dry_run"),
    )
    engine.set_symbol_info(exchange().get("DOGEUSDT"))
    return engine, broker, harian, k


# ------------------------------------------------------------------ sinyal
@pytest.mark.asyncio
async def test_sinyal_membuka_posisi(store: PositionStore) -> None:
    engine, broker, _, kanal = rakit(store)
    hasil = await engine.handle_signal(sinyal())
    assert hasil.action == "entered", hasil.reason
    pos = store.get_position("DOGEUSDT")
    assert pos is not None and pos.state is PositionState.OPEN
    assert await broker.position("DOGEUSDT") is not None
    assert engine.counters["entered"] == 1
    jenis = [p.kind for p in kanal.pesan]
    assert "entry" in jenis


@pytest.mark.asyncio
async def test_sinyal_ganda_diabaikan(store: PositionStore) -> None:
    engine, _, _, _ = rakit(store)
    await engine.handle_signal(sinyal())
    kedua = await engine.handle_signal(sinyal())
    assert kedua.action == "duplicate"


@pytest.mark.asyncio
async def test_kill_switch_memblokir_sebelum_broker(store: PositionStore, tmp_path: Path) -> None:
    kill = tmp_path / "KILL_SWITCH"
    kill.write_text("stop", encoding="utf-8")
    engine, broker, _, _ = rakit(store, kill_file=str(kill))
    hasil = await engine.handle_signal(sinyal())
    assert hasil.action == "blocked"
    assert "kill switch" in hasil.reason
    assert await broker.position("DOGEUSDT") is None
    assert store.get_position("DOGEUSDT") is None


@pytest.mark.asyncio
async def test_risiko_ditolak_tidak_mengirim_order(store: PositionStore) -> None:
    engine, broker, _, _kanal = rakit(store, equity=500.0)
    hasil = await engine.handle_signal(sinyal(harga=90000.0))  # bukan harga DOGE
    assert hasil.action in ("rejected", "skipped")
    assert store.get_position("DOGEUSDT") is None
    assert await broker.position("DOGEUSDT") is None


@pytest.mark.asyncio
async def test_tanpa_metadata_simbol_ditolak(store: PositionStore) -> None:
    engine, _, _, _ = rakit(store)
    engine.set_symbol_info(None)
    hasil = await engine.handle_signal(sinyal())
    assert hasil.action == "rejected"
    assert "metadata" in hasil.reason


# ------------------------------------------------------------------- harga
@pytest.mark.asyncio
async def test_harga_menutup_posisi_dan_mencatat_hasil(store: PositionStore) -> None:
    engine, _broker, harian, kanal = rakit(store, equity=500.0)
    assert (await engine.handle_signal(sinyal())).action == "entered"
    pos = store.get_position("DOGEUSDT")
    assert pos is not None

    # turunkan harga melewati SL
    await engine.handle_price("DOGEUSDT", float(pos.stop_price) * 0.999)
    terbaru = store.get_position("DOGEUSDT")
    assert terbaru is not None and not terbaru.active
    assert engine.counters["closed"] == 1
    assert harian.trades_today(engine.risk.now()) >= 1
    assert harian.current_equity != 500.0
    assert any(p.kind == "exit" for p in kanal.pesan)


@pytest.mark.asyncio
async def test_batas_harian_menutup_semua_posisi(store: PositionStore) -> None:
    cfg = config(risk={"daily_loss_limit_pct": 0.1, "risk_per_trade_pct": 0.5})
    engine, broker, harian, kanal = rakit(store, equity=100.0, cfg=cfg)
    assert (await engine.handle_signal(sinyal())).action == "entered"
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    await engine.handle_price("DOGEUSDT", float(pos.stop_price) * 0.99)
    assert harian.daily_stopped is True
    assert engine.stopped is True
    assert await broker.position("DOGEUSDT") is None
    assert any(p.kind == "daily_stop" for p in kanal.pesan)


@pytest.mark.asyncio
async def test_dashboard_state_mencerminkan_mesin(store: PositionStore) -> None:
    engine, _, _, _ = rakit(store, equity=500.0)
    state = engine.dashboard_state()
    assert state is not None
    assert state.status.running is True
    await engine.handle_signal(sinyal())
    await engine.handle_price("DOGEUSDT", 0.36)
    engine.dashboard_state()
    state = engine.dashboard_state()
    assert state is not None
    assert len(state.active_positions) == 1


@pytest.mark.asyncio
async def test_portofolio_dan_equity(store: PositionStore) -> None:
    engine, _, _, _ = rakit(store, equity=500.0)
    await engine.handle_signal(sinyal())
    porto = engine.portfolio()
    assert porto.open_count == 1
    assert porto.used_margin > 0
    assert engine.equity() == 500.0


@pytest.mark.asyncio
async def test_shutdown_menutup_posisi(store: PositionStore) -> None:
    engine, broker, _, _ = rakit(store, equity=500.0)
    await engine.handle_signal(sinyal())
    jumlah = await engine.shutdown(close_positions=True, reason="uji")
    assert jumlah == 1
    assert engine.stopped is True
    assert await broker.position("DOGEUSDT") is None


@pytest.mark.asyncio
async def test_setelah_dihentikan_sinyal_diblokir(store: PositionStore) -> None:
    engine, _, _, _ = rakit(store)
    engine.stop_session("selesai uji")
    hasil = await engine.handle_signal(sinyal())
    assert hasil.action == "blocked"
    assert "selesai uji" in hasil.reason


@pytest.mark.asyncio
async def test_periodic_tanpa_reconcile_aman(store: PositionStore) -> None:
    engine, _, _, _ = rakit(store)
    assert await engine.periodic() is None


@pytest.mark.asyncio
async def test_periodic_memanggil_reconcile(store: PositionStore) -> None:
    engine, _broker, _, _ = rakit(store)

    class Laporan:
        incidents: ClassVar[list[Any]] = []

    dipanggil: list[str] = []

    async def reconcile(**kwargs: Any) -> Any:
        dipanggil.append(kwargs["mode"])
        return Laporan()

    engine._reconcile = reconcile
    laporan = await engine.periodic()
    assert laporan is not None
    assert dipanggil == ["dry_run"]
