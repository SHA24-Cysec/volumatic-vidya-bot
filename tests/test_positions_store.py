"""Test penyimpanan posisi/order/insiden/state harian di SQLite.

Ini menutup lubang TAHAP 4: state harian harus selamat dari restart.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from volumatic_bot.positions.models import PositionState, TrackedPosition
from volumatic_bot.positions.store import DailySnapshot, PositionStore
from volumatic_bot.risk import restore_daily_state, save_daily_state
from volumatic_bot.risk.limits import DailyLimits, DailyState, TradeOutcome
from volumatic_bot.storage.db import SqliteDatabase

JKT = ZoneInfo("Asia/Jakarta")


@pytest.fixture
def store(tmp_path: Path) -> PositionStore:
    db = SqliteDatabase(tmp_path / "uji.db")
    db.open()
    try:
        yield PositionStore(db)
    finally:
        db.close()


def position(**kwargs: object) -> TrackedPosition:
    base: dict[str, object] = {
        "symbol": "DOGEUSDT",
        "side": "long",
        "state": PositionState.OPEN,
        "qty": Decimal("133"),
        "remaining_qty": Decimal("133"),
        "entry_price": Decimal("0.35"),
        "stop_price": Decimal("0.3444"),
        "tp1_price": Decimal("0.3584"),
        "be_price": Decimal("0.3506"),
        "risk_per_unit": Decimal("0.0056"),
        "leverage": 5,
        "mode": "dry_run",
        "opened_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
        "sl_order_id": 11,
        "tp_order_id": 12,
        "entry_order_id": 10,
    }
    base.update(kwargs)
    return TrackedPosition(**base)  # type: ignore[arg-type]


# -------------------------------------------------------------------- posisi
def test_simpan_dan_baca_posisi(store: PositionStore) -> None:
    pos = position()
    store.save_position(pos)
    balik = store.get_position("dogeusdt")
    assert balik is not None
    assert balik.symbol == "DOGEUSDT"
    assert balik.qty == Decimal("133")
    assert balik.state is PositionState.OPEN
    assert balik.sl_order_id == 11
    assert balik.be_price == Decimal("0.3506")
    assert balik.active is True
    assert balik.r_multiple == pytest.approx(0.7448, rel=1e-3)


def test_update_posisi_menimpa_bukan_menggandakan(store: PositionStore) -> None:
    store.save_position(position())
    pos = position(state=PositionState.PARTIAL, remaining_qty=Decimal("67"), partial_done=True)
    store.save_position(pos)
    assert len(store.all_positions()) == 1
    balik = store.get_position("DOGEUSDT")
    assert balik is not None
    assert balik.state is PositionState.PARTIAL
    assert balik.remaining_qty == Decimal("67")
    assert balik.partial_done is True


def test_posisi_aktif_dan_riwayat(store: PositionStore) -> None:
    store.save_position(position(symbol="DOGEUSDT"))
    store.save_position(position(symbol="ETHUSDT", state=PositionState.CLOSED, closed_at=1))
    aktif = store.active_positions()
    assert [p.symbol for p in aktif] == ["DOGEUSDT"]
    assert len(store.all_positions()) == 2
    assert store.get_position("TIDAKADA") is None


def test_posisi_tanpa_be_dan_order_id(store: PositionStore) -> None:
    store.save_position(
        position(be_price=None, sl_order_id=None, tp_order_id=None, entry_order_id=None)
    )
    balik = store.get_position("DOGEUSDT")
    assert balik is not None
    assert balik.be_price is None
    assert balik.sl_order_id is None


# --------------------------------------------------------------------- order
def test_catat_order_dan_baca(store: PositionStore) -> None:
    store.log_order(
        symbol="DOGEUSDT",
        side="SELL",
        type="STOP_MARKET",
        purpose="stop_loss",
        status="NEW",
        mode="dry_run",
        order_id=11,
        client_order_id="vbot-sl",
        qty="133",
        stop_price="0.3444",
        reduce_only=True,
        raw={"x": 1},
    )
    orders = store.orders_for("dogeusdt")
    assert len(orders) == 1
    assert orders[0].purpose == "stop_loss"
    assert orders[0].reduce_only is True
    assert orders[0].stop_price == "0.3444"


# ------------------------------------------------------------------ insiden
def test_insiden_tersimpan_dan_terurut(store: PositionStore) -> None:
    store.add_incident(kind="protection_failed", detail="SL ditolak", symbol="DOGEUSDT")
    store.add_incident(kind="close_failed", detail="tutup gagal", severity="critical")
    insiden = store.incidents()
    assert len(insiden) == 2
    assert insiden[0].kind == "close_failed"  # terbaru dulu
    assert insiden[1].symbol == "DOGEUSDT"


# ------------------------------------------------------------- state harian
def test_state_harian_roundtrip(store: PositionStore) -> None:
    store.save_daily(DailySnapshot(day="2026-11-10", equity=95.0, lost_usdt=5.0, trades=3))
    snap = store.load_daily("2026-11-10")
    assert snap is not None
    assert snap.equity == pytest.approx(95.0)
    assert snap.lost_usdt == pytest.approx(5.0)
    assert snap.trades == 3
    assert store.load_daily("2026-11-11") is None


def daily_state() -> DailyState:
    limits = DailyLimits(
        daily_loss_limit_pct=3.0,
        max_trades_per_day=20,
        consecutive_loss_limit=3,
        cooldown_minutes=120,
        reset_hour_local=0,
        timezone="Asia/Jakarta",
    )
    return DailyState(limits=limits, start_equity=100.0, current_equity=100.0)


def test_state_harian_disimpan_dan_dipulihkan(store: PositionStore) -> None:
    """Restart bot tidak boleh menghapus catatan rugi harian."""
    now = datetime(2026, 11, 10, 12, 0, tzinfo=JKT)
    state = daily_state()
    state.roll(now)
    state.record(TradeOutcome("DOGEUSDT", -2.0, now), now)
    state.record(TradeOutcome("ETHUSDT", 0.5, now), now)
    save_daily_state(store, state, now)

    baru = daily_state()  # simulasi proses baru
    snap = restore_daily_state(store, baru, now)
    assert snap is not None
    assert baru.loss_today(now) == pytest.approx(2.0)
    assert baru.current_equity == pytest.approx(98.5)
    assert baru.trades_today(now) == 2
    assert baru.can_open(now, open_positions=0, max_positions=2).allowed is True


def test_rugi_harian_tercapai_tetap_terkunci_setelah_restart(store: PositionStore) -> None:
    now = datetime(2026, 11, 10, 12, 0, tzinfo=JKT)
    state = daily_state()
    state.roll(now)
    state.record(TradeOutcome("DOGEUSDT", -4.0, now), now)
    save_daily_state(store, state, now)

    baru = daily_state()
    restore_daily_state(store, baru, now)
    assert baru.daily_stopped is True
    keputusan = baru.can_open(now, open_positions=0, max_positions=2)
    assert keputusan.blocked
    assert any("rugi harian" in r for r in keputusan.reasons)


def test_restore_tanpa_data_tidak_mengubah_state(store: PositionStore) -> None:
    now = datetime(2026, 11, 10, 12, 0, tzinfo=JKT)
    baru = daily_state()
    assert restore_daily_state(store, baru, now) is None
    assert baru.day == now.date()
    assert baru.can_open(now, open_positions=0, max_positions=2).allowed
