"""Test regresi untuk perbaikan audit lanjutan (2026-11-15).

Yang dibuktikan:

1. Isi SL/TP hasil pemicu algo order (``orderId`` baru) tetap menutup posisi —
   lewat keterkaitan ``ALGO_UPDATE`` maupun pencocokan jenis order.
2. ``ALGO_UPDATE`` dengan alasan penolakan memasang ulang SL.
3. PnL penutupan market (kill switch) tercatat, dan hasil satu posisi tidak
   dicatat dua kali oleh event order yang datang terlambat.
4. Trailing membulatkan kandidat SL ke ``tickSize`` dan SL lama hanya dibatalkan
   setelah SL baru diterima; bila SL baru gagal, SL lama tetap aktif.
5. Entry ``limit_fallback`` yang terisi sebagian hanya melanjutkan sisanya ke
   market (bukan seluruh qty awal).
6. ``KlineFeed`` menyimpan ATR terakhir per simbol.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.binance.trading import OrderRequest, OrderResult
from volumatic_bot.binance.user_stream import (
    ListenKeyManager,
    OrderUpdate,
    UserDataStream,
    parse_algo_update,
)
from volumatic_bot.config import AppConfig, load_config
from volumatic_bot.engine.trader import TradingEngine
from volumatic_bot.execution.broker import DryRunBroker
from volumatic_bot.execution.executor import TradeExecutor
from volumatic_bot.positions.models import PositionState
from volumatic_bot.positions.store import PositionStore
from volumatic_bot.risk import DailyState, RiskManager
from volumatic_bot.risk.limits import DailyLimits
from volumatic_bot.risk.portfolio import Portfolio
from volumatic_bot.storage.db import SqliteDatabase
from volumatic_bot.streams.kline_feed import KlineFeed

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"


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
def store(tmp_path: Path) -> PositionStore:
    db = SqliteDatabase(tmp_path / "regresi.db")
    db.open()
    try:
        yield PositionStore(db)
    finally:
        db.close()


def manager(cfg: AppConfig, *, equity: float = 50.0) -> RiskManager:
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
        clock=lambda: datetime(2026, 11, 15, 10, 0, tzinfo=ZoneInfo("Asia/Jakarta")),
        kill_switch_path=Path("/tmp/kill-tidak-ada-regresi"),
    )


def ticket(cfg: AppConfig, *, equity: float = 50.0) -> Any:
    return manager(cfg, equity=equity).evaluate(
        symbol="DOGEUSDT",
        side="long",
        entry_price=0.35,
        atr=0.007,
        trail=0.345,
        equity=equity,
        portfolio=Portfolio(equity=equity, positions=()),
        info=exchange().get("DOGEUSDT"),
        open_positions=0,
    )


def executor(
    cfg: AppConfig, store: PositionStore, broker: DryRunBroker, *, filters: bool = True
) -> TradeExecutor:
    async def cepat(_: float) -> None:
        return None

    info = exchange()

    def filters_of(symbol: str) -> Any:
        if not filters:
            return None
        item = info.symbols.get(symbol.upper())
        return item.filters if item is not None else None

    return TradeExecutor(
        cfg,
        broker,
        store,
        clock=lambda: 1_700_000_000_000,
        sleep=cepat,
        mode="dry_run",
        filters_of=filters_of,
    )


def update(
    pos: Any,
    *,
    order_id: int,
    order_type: str = "STOP_MARKET",
    status: str = "FILLED",
    execution: str = "TRADE",
    qty: str | None = None,
    filled: str | None = None,
    price: str = "0.3444",
    pnl: str = "-0.7",
    close_position: bool = True,
) -> OrderUpdate:
    jumlah = qty if qty is not None else str(pos.qty)
    terisi = filled if filled is not None else jumlah
    return OrderUpdate(
        symbol=pos.symbol,
        client_order_id=f"vbot-uji-{order_id}",
        side="SELL",
        order_type=order_type,
        status=status,
        execution_type=execution,
        order_id=order_id,
        orig_qty=Decimal(jumlah),
        last_filled_qty=Decimal(terisi),
        filled_qty=Decimal(terisi),
        last_price=Decimal(price),
        avg_price=Decimal(price),
        stop_price=Decimal(price),
        reduce_only=not close_position,
        close_position=close_position,
        commission=Decimal("0.01"),
        commission_asset="USDT",
        realized_pnl=Decimal(pnl),
        is_maker=False,
        event_time_ms=1_700_000_000_001,
    )


async def buka_posisi(
    cfg: AppConfig, store: PositionStore, broker: DryRunBroker
) -> tuple[TradeExecutor, Any]:
    broker.set_price("DOGEUSDT", Decimal("0.35"))
    ex = executor(cfg, store, broker)
    tiket = ticket(cfg)
    assert tiket.approved, tiket.reasons
    laporan = await ex.open_trade(tiket, atr=0.007)
    assert laporan.ok, laporan.incidents
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    return ex, pos


# ---------------------------------------------------- 1) isi SL/TP algo order
@pytest.mark.asyncio
async def test_sl_hasil_pemicu_algo_menutup_posisi(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker()
    ex, pos = await buka_posisi(cfg, store, broker)
    id_algo = pos.sl_order_id
    assert id_algo is not None
    spawned = 9_999_999
    algo = parse_algo_update(
        {
            "e": "ALGO_UPDATE",
            "E": 1_700_000_000_001,
            "o": {
                "s": "DOGEUSDT",
                "aid": id_algo,
                "caid": "vbot-sl",
                "S": "SELL",
                "o": "STOP_MARKET",
                "X": "TRIGGERED",
                "ai": spawned,
                "aq": str(pos.qty),
                "ap": "0.3444",
                "rm": "",
            },
        }
    )
    assert algo.spawned_order_id == spawned
    await ex.on_algo_update(algo)
    await ex.on_order_update(update(pos, order_id=spawned, order_type="STOP_MARKET"))
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.state is PositionState.CLOSED
    assert pos.close_reason == "stop_loss"
    assert pos.realized_usdt == -0.7


@pytest.mark.asyncio
async def test_sl_tanpa_id_dikenali_dari_jenis_order(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker()
    ex, pos = await buka_posisi(cfg, store, broker)
    await ex.on_order_update(update(pos, order_id=123_456, order_type="STOP_MARKET"))
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.state is PositionState.CLOSED
    assert pos.close_reason == "stop_loss"


@pytest.mark.asyncio
async def test_tp_tanpa_id_dikenali_dari_jenis_order(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker()
    ex, pos = await buka_posisi(cfg, store, broker)
    await ex.on_order_update(
        update(pos, order_id=123_456, order_type="TAKE_PROFIT_MARKET", pnl="0.9")
    )
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.state is PositionState.CLOSED
    assert pos.close_reason == "take_profit"
    assert pos.realized_usdt == 0.9


# ------------------------------------------- 2) ALGO_UPDATE: penolakan pemicu
@pytest.mark.asyncio
async def test_algo_ditolak_memasang_ulang_sl(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker()
    ex, pos = await buka_posisi(cfg, store, broker)
    id_lama = pos.sl_order_id
    algo = parse_algo_update(
        {
            "e": "ALGO_UPDATE",
            "E": 1_700_000_000_001,
            "o": {
                "s": "DOGEUSDT",
                "aid": id_lama,
                "caid": "vbot-sl",
                "S": "SELL",
                "o": "STOP_MARKET",
                "X": "FINISHED",
                "ai": 0,
                "aq": "0",
                "ap": "0",
                "rm": "would immediately trigger",
            },
        }
    )
    assert algo.rejected
    await ex.on_algo_update(algo)
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.active
    assert pos.sl_order_id is not None
    assert pos.sl_order_id != id_lama


# ------------------------------------- 3) pencatatan hasil sekali & PnL close
@pytest.mark.asyncio
async def test_kill_switch_pnl_tercatat_dan_tidak_dobel(store: PositionStore) -> None:
    cfg = config(risk={"risk_per_trade_pct": 0.5})
    broker = DryRunBroker()
    ex, pos = await buka_posisi(cfg, store, broker)
    risiko = manager(cfg)
    mesin = TradingEngine(cfg, store=store, risk=risiko, executor=ex, broker=broker)

    broker.set_price("DOGEUSDT", Decimal("0.345"))
    assert await ex.close_position("DOGEUSDT", reason="kill_switch")
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.state is PositionState.CLOSED
    assert pos.realized_usdt is not None

    await mesin.handle_order_update(update(pos, order_id=777, order_type="MARKET", pnl="0.5"))
    await mesin.handle_order_update(update(pos, order_id=778, order_type="MARKET", pnl="0.5"))
    assert mesin.counters["closed"] == 1
    assert risiko.daily.trades_today(risiko.now()) == 1


@pytest.mark.asyncio
async def test_event_order_late_setelah_sl_tidak_mencatat_dobel(store: PositionStore) -> None:
    cfg = config(risk={"risk_per_trade_pct": 0.5})
    broker = DryRunBroker()
    ex, pos = await buka_posisi(cfg, store, broker)
    risiko = manager(cfg)
    mesin = TradingEngine(cfg, store=store, risk=risiko, executor=ex, broker=broker)

    await mesin.handle_order_update(update(pos, order_id=pos.sl_order_id or 0))
    assert mesin.counters["closed"] == 1
    assert risiko.daily.trades_today(risiko.now()) == 1
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    await mesin.handle_order_update(
        update(pos, order_id=888, order_type="MARKET", status="CANCELED", execution="CANCELED")
    )
    assert mesin.counters["closed"] == 1
    assert risiko.daily.trades_today(risiko.now()) == 1


# ------------------------------------------------- 4) trailing & penggantian SL
@pytest.mark.asyncio
async def test_trailing_membulatkan_tick_dan_sl_lama_dibatalkan(store: PositionStore) -> None:
    cfg = config()
    broker = DryRunBroker()
    ex, pos = await buka_posisi(cfg, store, broker)
    id_lama = pos.sl_order_id
    harga = 0.362333
    kandidat_kasar = Decimal(str(harga)) - Decimal(str(0.007 * cfg.trade.trailing_atr_mult))
    assert (kandidat_kasar % Decimal("0.00001")) != 0
    bergerak = await ex.update_trailing(
        "DOGEUSDT",
        price=harga,
        atr=0.007,
        activation_price=Decimal("0.35"),
    )
    assert bergerak
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert (pos.stop_price % Decimal("0.00001")) == 0
    assert pos.stop_price > Decimal("0.3444")
    assert pos.sl_order_id is not None and pos.sl_order_id != id_lama
    assert broker._orders[id_lama].status == "CANCELED"
    assert broker._orders[pos.sl_order_id].status == "NEW"


@pytest.mark.asyncio
async def test_gagal_ganti_sl_sl_lama_tetap_aktif(store: PositionStore) -> None:
    cfg = config()

    class BrokerTolakSL(DryRunBroker):
        def __init__(self) -> None:
            super().__init__()
            self.tolak = False

        async def place(self, order: OrderRequest) -> Any:
            if self.tolak and order.type == "STOP_MARKET":
                raise RuntimeError("galat sengaja")
            return await super().place(order)

    broker = BrokerTolakSL()
    ex, pos = await buka_posisi(cfg, store, broker)
    id_lama = pos.sl_order_id
    broker.tolak = True
    bergerak = await ex.update_trailing(
        "DOGEUSDT",
        price=0.3623,
        atr=0.007,
        activation_price=Decimal("0.35"),
    )
    assert not bergerak
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.active
    assert pos.sl_order_id == id_lama
    assert pos.stop_price == Decimal("0.3444")
    assert broker._orders[id_lama].status == "NEW"


# ------------------------------------- 5) entry limit sebagian isi: sisa market
@pytest.mark.asyncio
async def test_entry_limit_sebagian_isi_melanjutkan_sisa(store: PositionStore) -> None:
    cfg = config(trade={"entry_mode": "limit_fallback", "entry_limit_timeout_sec": 0.15})

    class BrokerIsiSebagian(DryRunBroker):
        async def place(self, order: OrderRequest) -> Any:
            hasil = await super().place(order)
            if order.type == "LIMIT":
                terisi = (order.quantity or Decimal(0)) * Decimal("0.4")
                hasil_sebagian = OrderResult(
                    symbol=hasil.symbol,
                    order_id=hasil.order_id,
                    client_order_id=hasil.client_order_id,
                    side=hasil.side,
                    type=hasil.type,
                    status="PARTIALLY_FILLED",
                    orig_qty=hasil.orig_qty,
                    executed_qty=terisi,
                    avg_price=Decimal(0),
                    stop_price=hasil.stop_price,
                    reduce_only=hasil.reduce_only,
                    close_position=hasil.close_position,
                    purpose=hasil.purpose,
                )
                self._orders[hasil.order_id] = hasil_sebagian
                return hasil_sebagian
            return hasil

    broker = BrokerIsiSebagian()
    broker.set_price("DOGEUSDT", Decimal("0.36"))
    ex = executor(cfg, store, broker)
    tiket = ticket(cfg)
    assert tiket.approved, tiket.reasons
    laporan = await ex.open_trade(tiket, atr=0.007)
    assert laporan.ok, laporan.incidents
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    market_entry = [
        o for o in broker._orders.values() if o.type == "MARKET" and o.purpose == "entry"
    ]
    assert market_entry, "fallback market harus terkirim"
    assert market_entry[0].orig_qty == pos.qty
    assert pos.qty < tiket.size.qty


# ------------------------------------------------------------- 6) ATR feed
@pytest.mark.asyncio
async def test_kline_feed_menyimpan_atr_terakhir() -> None:
    from volumatic_bot.binance.kline_messages import KlineEvent
    from volumatic_bot.models import CandleSeries

    feed = KlineFeed(max_bars=300)
    n = 260
    series = CandleSeries.from_columns(
        open_time=list(range(n)),
        open=[10.0 + (i % 7) * 0.1 for i in range(n)],
        high=[11.0 + (i % 5) * 0.2 for i in range(n)],
        low=[9.5 - (i % 3) * 0.1 for i in range(n)],
        close=[10.5 + (i % 11) * 0.15 for i in range(n)],
        volume=[100.0 + (i % 13) for i in range(n)],
    )
    feed.seed("BTCUSDT", series)
    event = KlineEvent(
        symbol="BTCUSDT",
        interval="15m",
        open_time=n,
        close_time=n + 899_999,
        open=10.5,
        high=11.5,
        low=10.1,
        close=11.2,
        volume=120.0,
        quote_volume=1344.0,
        is_closed=True,
        event_time=n + 899_999,
    )
    await feed.handle_event(event)
    atr = feed.last_atr.get("BTCUSDT")
    assert atr is not None and atr > 0


# ------------------------------------------------ 7) user stream: ALGO_UPDATE
class FakeAPI:
    async def create_listen_key(self) -> str:
        return "kunci-1"

    async def keepalive_listen_key(self, key: str) -> None:
        return None

    async def close_listen_key(self, key: str) -> None:
        return None


@pytest.mark.asyncio
async def test_user_stream_meneruskan_algo_update() -> None:
    algo_masuk: list[Any] = []

    async def on_algo(algo: Any) -> None:
        algo_masuk.append(algo)

    stream = UserDataStream(
        ListenKeyManager(FakeAPI(), keepalive_sec=600),  # type: ignore[arg-type]
        on_algo=on_algo,
    )
    await stream.keys.ensure()
    await stream._handle_raw(
        json.dumps(
            {
                "e": "ALGO_UPDATE",
                "E": 1_700_000_000_001,
                "o": {
                    "s": "DOGEUSDT",
                    "aid": 2146760,
                    "caid": "vbot-sl-1",
                    "S": "SELL",
                    "o": "STOP_MARKET",
                    "X": "TRIGGERED",
                    "ai": 987654,
                    "aq": "133",
                    "ap": "0.3444",
                    "rm": "",
                },
            }
        )
    )
    assert len(algo_masuk) == 1
    assert algo_masuk[0].algo_id == 2146760
    assert algo_masuk[0].spawned_order_id == 987654


def test_parse_algo_update_rejected() -> None:
    algo = parse_algo_update(
        {
            "e": "ALGO_UPDATE",
            "E": 1,
            "o": {
                "s": "DOGEUSDT",
                "aid": 5,
                "caid": "vbot-sl-1",
                "S": "SELL",
                "o": "STOP_MARKET",
                "X": "FINISHED",
                "ai": 0,
                "aq": "0",
                "ap": "0",
                "rm": "would immediately trigger",
            },
        }
    )
    assert algo.rejected
    assert not algo.triggered
