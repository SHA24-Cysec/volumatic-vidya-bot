"""Test siklus eksekusi penuh di ``DryRunBroker`` (tanpa jaringan, tanpa risiko).

Yang dibuktikan di sini:

1. SL dipasang **segera** setelah entry dan selalu ``closePosition``.
2. Bila pemasangan SL gagal → posisi ditutup market + insiden ``protection_failed``.
3. TP parsial terisi → SL digeser ke BE (yang sudah memperhitungkan biaya).
4. Trailing menggeser SL hanya searah, tidak pernah mundur.
5. Semua langkah tercatat di SQLite.
6. Tiket yang belum disetujui manajer risiko tidak bisa dieksekusi.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.config import load_config
from volumatic_bot.execution.broker import DryRunBroker
from volumatic_bot.execution.executor import TradeExecutor
from volumatic_bot.positions.models import PositionState
from volumatic_bot.positions.store import PositionStore
from volumatic_bot.risk import DailyState, Portfolio, RiskManager
from volumatic_bot.risk.limits import DailyLimits
from volumatic_bot.storage.db import SqliteDatabase

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"


def exchange() -> Any:
    import json

    return parse_exchange_info(
        json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
    )


def config(**overrides: Any) -> Any:
    cfg = load_config(ROOT / "config.yaml")
    if overrides:
        dump = cfg.model_dump()
        for key, value in overrides.items():
            dump[key].update(value)
        from volumatic_bot.config import AppConfig

        cfg = AppConfig(**dump)
    return cfg


@pytest.fixture
def store(tmp_path: Path) -> PositionStore:
    db = SqliteDatabase(tmp_path / "uji.db")
    db.open()
    try:
        yield PositionStore(db)
    finally:
        db.close()


def manager(cfg: Any, *, equity: float = 50.0) -> RiskManager:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    limits = DailyLimits(
        daily_loss_limit_pct=cfg.risk.daily_loss_limit_pct,
        max_trades_per_day=cfg.risk.max_trades_per_day,
        consecutive_loss_limit=cfg.risk.consecutive_loss_limit,
        cooldown_minutes=cfg.risk.cooldown_minutes,
        reset_hour_local=cfg.risk.daily_loss_reset_hour_local,
        timezone=cfg.app.timezone,
    )
    return RiskManager(
        cfg,
        daily=DailyState(limits=limits, start_equity=equity, current_equity=equity),
        clock=lambda: datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("Asia/Jakarta")),
        kill_switch_path=Path("/tmp/tidak-ada-kill-eksekusi"),
    )


def ticket(cfg: Any, *, equity: float = 50.0, trail: float = 0.345) -> Any:
    return manager(cfg, equity=equity).evaluate(
        symbol="DOGEUSDT",
        side="long",
        entry_price=0.35,
        atr=0.007,
        trail=trail,
        equity=equity,
        portfolio=Portfolio(equity=equity, positions=()),
        info=exchange().get("DOGEUSDT"),
        open_positions=0,
    )


def executor(cfg: Any, store: PositionStore, broker: DryRunBroker) -> TradeExecutor:
    async def cepat(_: float) -> None:
        return None

    return TradeExecutor(
        cfg, broker, store, clock=lambda: 1_700_000_000_000, sleep=cepat, mode="dry_run"
    )


def update_from(order: Any) -> Any:
    from volumatic_bot.binance.user_stream import OrderUpdate

    return OrderUpdate(
        symbol=order.symbol,
        client_order_id=order.client_order_id,
        side=order.side,
        order_type=order.type,
        status=order.status,
        execution_type="TRADE",
        order_id=order.order_id,
        orig_qty=order.orig_qty,
        last_filled_qty=order.executed_qty,
        filled_qty=order.executed_qty,
        last_price=order.avg_price,
        avg_price=order.avg_price,
        stop_price=order.stop_price,
        reduce_only=order.reduce_only,
        close_position=order.close_position,
        commission=Decimal(0),
        commission_asset="USDT",
        realized_pnl=Decimal(0),
        is_maker=False,
        event_time_ms=0,
    )


# --------------------------------------------------------------- entry + SL
@pytest.mark.asyncio
async def test_entry_diikuti_sl_dan_tp_segera(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker(taker_bps=cfg.commission.taker_bps)
    broker.set_price("DOGEUSDT", 0.35)
    ex = executor(cfg, store, broker)

    laporan = await ex.open_trade(ticket(cfg), atr=0.007)
    assert laporan.ok, laporan.incidents
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.state is PositionState.OPEN
    assert pos.sl_order_id is not None
    assert pos.stop_price < pos.entry_price

    orders = {o.purpose: o for o in await broker.open_orders("DOGEUSDT")}
    assert "stop_loss" in orders
    sl = orders["stop_loss"]
    assert sl.close_position is True
    assert sl.reduce_only is False  # closePosition + reduceOnly ditolak bursa
    assert sl.orig_qty == 0  # closePosition tidak mengirim qty
    assert "take_profit" in orders or "partial_tp" in orders
    # jejak order tercatat
    tercatat = {o.purpose for o in store.orders_for("DOGEUSDT")}
    assert {"entry", "stop_loss"} <= tercatat


@pytest.mark.asyncio
async def test_tiket_belum_disetujui_tidak_dieksekusi(store: PositionStore) -> None:
    cfg = config(risk={"min_equity_usdt": 1000.0})
    broker = DryRunBroker()
    ex = executor(cfg, store, broker)
    laporan = await ex.open_trade(ticket(cfg), atr=0.007)
    assert laporan.ok is False
    assert "manajer risiko" in laporan.incidents[0]
    assert store.get_position("DOGEUSDT") is None


@pytest.mark.asyncio
async def test_entry_ditolak_bila_posisi_sudah_ada(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker()
    broker.set_price("DOGEUSDT", 0.35)
    ex = executor(cfg, store, broker)
    await ex.open_trade(ticket(cfg), atr=0.007)
    kedua = await ex.open_trade(ticket(cfg), atr=0.007)
    assert kedua.ok is False
    assert "sudah ada" in kedua.incidents[0]
    insiden = store.incidents()
    assert any(i.kind == "duplicate_position" for i in insiden)


# ------------------------------------------------------------------ SL gagal
class BrokenProtectionBroker(DryRunBroker):
    """Broker yang menolak SEMUA order stop/take profit (meniru -2021)."""

    async def place(self, order: Any) -> Any:
        if order.type in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            raise RuntimeError("Order would immediately trigger")
        return await super().place(order)


@pytest.mark.asyncio
async def test_sl_gagal_posisi_ditutup_market_dan_dicatat(store: PositionStore) -> None:
    cfg = config()
    broker = BrokenProtectionBroker()
    broker.set_price("DOGEUSDT", 0.35)
    ex = executor(cfg, store, broker)

    laporan = await ex.open_trade(ticket(cfg), atr=0.007)
    assert laporan.ok is False
    assert any("SL gagal" in i for i in laporan.incidents)

    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.state is PositionState.FAILED
    assert pos.close_reason == "protection_failed"
    # posisi benar-benar ditutup di broker simulasi
    assert await broker.position("DOGEUSDT") is None
    insiden = {i.kind for i in store.incidents()}
    assert "protection_failed" in insiden


# ------------------------------------------------------ partial TP → BE
@pytest.mark.asyncio
async def test_partial_tp_menggeser_sl_ke_break_even(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker(taker_bps=cfg.commission.taker_bps)
    broker.set_price("DOGEUSDT", 0.35)
    ex = executor(cfg, store, broker)
    laporan = await ex.open_trade(ticket(cfg), atr=0.007)
    assert laporan.ok
    pos_awal = store.get_position("DOGEUSDT")
    assert pos_awal is not None
    tp_harga = float(pos_awal.tp1_price)

    terpicu = await broker.on_price("DOGEUSDT", tp_harga)
    assert terpicu, "TP parsial seharusnya terpicu"
    for order in terpicu:
        await ex.on_order_update(update_from(order))

    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.partial_done is True
    assert pos.be_done is True
    assert pos.state is PositionState.PARTIAL
    assert pos.stop_price == pos.be_price
    assert Decimal(str(pos.stop_price)) > Decimal("0.35")  # BE di atas entry (long)
    # sisa posisi masih terbuka dan masih punya SL
    assert pos.remaining_qty > 0
    orders = await broker.open_orders("DOGEUSDT")
    assert any(o.purpose == "stop_loss" for o in orders)


@pytest.mark.asyncio
async def test_sl_setelah_be_menutup_sisa_posisi(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker(taker_bps=cfg.commission.taker_bps)
    broker.set_price("DOGEUSDT", 0.35)
    ex = executor(cfg, store, broker)
    await ex.open_trade(ticket(cfg), atr=0.007)
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    for order in await broker.on_price("DOGEUSDT", float(pos.tp1_price)):
        await ex.on_order_update(update_from(order))
    pos = store.get_position("DOGEUSDT")
    assert pos is not None and pos.be_price is not None

    for order in await broker.on_price("DOGEUSDT", float(pos.be_price) - 0.0001):
        await ex.on_order_update(update_from(order))
    akhir = store.get_position("DOGEUSDT")
    assert akhir is not None
    assert akhir.state is PositionState.CLOSED
    assert akhir.close_reason == "stop_loss_be"
    assert await broker.position("DOGEUSDT") is None


# ------------------------------------------------------------------ trailing
@pytest.mark.asyncio
async def test_trailing_menggeser_sl_hanya_searah(store: PositionStore) -> None:
    cfg = config(trade={"trailing_atr_mult": 0.5})
    broker = DryRunBroker(taker_bps=cfg.commission.taker_bps)
    broker.set_price("DOGEUSDT", 0.35)
    ex = executor(cfg, store, broker)
    await ex.open_trade(ticket(cfg), atr=0.007)
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    aktivasi = Decimal("0.3556")

    # harga belum mencapai aktivasi → tidak ada perubahan
    assert (
        await ex.update_trailing("DOGEUSDT", price=0.354, atr=0.007, activation_price=aktivasi)
        is False
    )
    # harga melewati aktivasi → SL naik
    assert (
        await ex.update_trailing("DOGEUSDT", price=0.360, atr=0.007, activation_price=aktivasi)
        is True
    )
    naik = store.get_position("DOGEUSDT")
    assert naik is not None
    assert naik.stop_price > pos.stop_price
    # harga turun lagi → SL tidak boleh mundur
    assert (
        await ex.update_trailing("DOGEUSDT", price=0.352, atr=0.007, activation_price=aktivasi)
        is False
    )
    tetap = store.get_position("DOGEUSDT")
    assert tetap is not None
    assert tetap.stop_price == naik.stop_price


@pytest.mark.asyncio
async def test_trailing_atr_nol_diabaikan(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker()
    broker.set_price("DOGEUSDT", 0.35)
    ex = executor(cfg, store, broker)
    await ex.open_trade(ticket(cfg), atr=0.007)
    assert (
        await ex.update_trailing("DOGEUSDT", price=0.36, atr=0.0, activation_price=Decimal("0.35"))
        is False
    )


# --------------------------------------------------------------- tutup posisi
@pytest.mark.asyncio
async def test_close_position_manual(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker(taker_bps=cfg.commission.taker_bps)
    broker.set_price("DOGEUSDT", 0.35)
    ex = executor(cfg, store, broker)
    await ex.open_trade(ticket(cfg), atr=0.007)
    assert await ex.close_position("DOGEUSDT", reason="kill_switch") is True
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.state is PositionState.CLOSED
    assert pos.close_reason == "kill_switch"
    assert await ex.close_position("DOGEUSDT", reason="lagi") is False


# ---------------------------------------------------------------- likuidasi
@pytest.mark.asyncio
async def test_event_likuidasi_dicatat_kritis(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker(taker_bps=cfg.commission.taker_bps)
    broker.set_price("DOGEUSDT", 0.35)
    ex = executor(cfg, store, broker)
    await ex.open_trade(ticket(cfg), atr=0.007)
    pos = store.get_position("DOGEUSDT")
    assert pos is not None

    from volumatic_bot.binance.user_stream import OrderUpdate

    update = OrderUpdate(
        symbol="DOGEUSDT",
        client_order_id="autoclose-999",
        side="SELL",
        order_type="LIQUIDATION",
        status="NEW",
        execution_type="CALCULATED",
        order_id=999,
        orig_qty=Decimal("67"),
        last_filled_qty=Decimal("67"),
        filled_qty=Decimal("67"),
        last_price=Decimal("0.28"),
        avg_price=Decimal("0.28"),
        stop_price=Decimal(0),
        reduce_only=True,
        close_position=True,
        commission=Decimal(0),
        commission_asset="USDT",
        realized_pnl=Decimal("-4.7"),
        is_maker=False,
        event_time_ms=0,
    )
    await ex.on_order_update(update)
    akhir = store.get_position("DOGEUSDT")
    assert akhir is not None
    assert akhir.close_reason == "liquidated"
    assert any(i.kind == "liquidation" for i in store.incidents())


# ------------------------------------------------------------- limit fallback
@pytest.mark.asyncio
async def test_entry_limit_lalu_fallback_market(store: PositionStore) -> None:
    """Harga tidak menyentuh limit → order limit dibatalkan, lalu market."""
    cfg = config(trade={"entry_mode": "limit_fallback", "entry_limit_timeout_sec": 0.2})
    broker = DryRunBroker(taker_bps=cfg.commission.taker_bps)
    broker.set_price("DOGEUSDT", 0.351)  # di atas harga limit 0.35 → tidak terisi
    ex = executor(cfg, store, broker)
    laporan = await ex.open_trade(ticket(cfg), atr=0.007)
    assert laporan.ok, laporan.incidents
    jenis = [o.type for o in laporan.orders]
    assert "MARKET" in jenis or any("market" in e for e in broker.events)
    pos = store.get_position("DOGEUSDT")
    assert pos is not None and pos.entry_order_id is not None
