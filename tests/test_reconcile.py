"""Test rekonsiliasi: menyamakan catatan bot dengan posisi/order di bursa.

Kasus yang diuji adalah yang berbahaya di dunia nyata: posisi tanpa SL, posisi
"hantu" (ada di bursa, tidak di catatan), posisi yang sudah hilang, arah yang
tidak cocok, dan order proteksi menggantung tanpa posisi.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from volumatic_bot.execution.broker import DryRunBroker, ExchangePosition
from volumatic_bot.execution.reconcile import reconcile
from volumatic_bot.positions.models import PositionState, TrackedPosition
from volumatic_bot.positions.store import PositionStore
from volumatic_bot.storage.db import SqliteDatabase


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
        "risk_per_unit": Decimal("0.0056"),
        "leverage": 5,
        "mode": "dry_run",
        "opened_at": 1,
        "updated_at": 1,
    }
    base.update(kwargs)
    return TrackedPosition(**base)  # type: ignore[arg-type]


class BrokerPalsu(DryRunBroker):
    """Broker dengan posisi awal disuntikkan (meniru keadaan bursa)."""

    def __init__(self, posisi: ExchangePosition | None) -> None:
        super().__init__()
        if posisi is not None:
            self._positions[posisi.symbol] = posisi
            self.set_price(posisi.symbol, posisi.entry_price)


@pytest.mark.asyncio
async def test_rekonsiliasi_bersih(store: PositionStore) -> None:
    broker = BrokerPalsu(ExchangePosition("DOGEUSDT", Decimal("133"), Decimal("0.35")))
    store.save_position(position(sl_order_id=1))
    await broker.place(
        __import__("volumatic_bot.binance.trading", fromlist=["OrderRequest"]).OrderRequest(
            symbol="DOGEUSDT",
            side="SELL",
            type="STOP_MARKET",
            stop_price=Decimal("0.3444"),
            close_position=True,
            purpose="stop_loss",
        )
    )
    laporan = await reconcile(broker=broker, store=store)
    assert laporan.ok
    assert any(a.kind == "stop_loss_ok" for a in laporan.actions)


@pytest.mark.asyncio
async def test_posisi_tanpa_sl_dipasang_ulang(store: PositionStore) -> None:
    broker = BrokerPalsu(ExchangePosition("DOGEUSDT", Decimal("133"), Decimal("0.35")))
    store.save_position(position(sl_order_id=99))
    laporan = await reconcile(broker=broker, store=store)
    assert any(a.kind == "missing_stop_loss" for a in laporan.actions)
    assert any(a.kind == "rearm_stop_loss" for a in laporan.actions)
    assert any(i.kind == "missing_stop_loss" for i in store.incidents())
    open_orders = await broker.open_orders("DOGEUSDT")
    assert any(o.purpose == "stop_loss" for o in open_orders)
    pos = store.get_position("DOGEUSDT")
    assert pos is not None and pos.sl_order_id is not None


@pytest.mark.asyncio
async def test_posisi_hantu_ditutup(store: PositionStore) -> None:
    broker = BrokerPalsu(ExchangePosition("DOGEUSDT", Decimal("50"), Decimal("0.36")))
    laporan = await reconcile(broker=broker, store=store, symbols=["DOGEUSDT"])
    kinds = [a.kind for a in laporan.actions]
    assert "orphan_position" in kinds
    assert "close_unknown" in kinds
    assert any(i.kind == "orphan_position" for i in store.incidents())
    assert await broker.position("DOGEUSDT") is None


@pytest.mark.asyncio
async def test_posisi_hilang_di_bursa_ditutup_di_catatan(store: PositionStore) -> None:
    store.save_position(position())
    broker = BrokerPalsu(None)
    await broker.place(
        __import__("volumatic_bot.binance.trading", fromlist=["OrderRequest"]).OrderRequest(
            symbol="DOGEUSDT",
            side="SELL",
            type="STOP_MARKET",
            stop_price=Decimal("0.3444"),
            close_position=True,
            purpose="stop_loss",
        )
    )
    laporan = await reconcile(broker=broker, store=store)
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.state is PositionState.CLOSED
    assert pos.close_reason == "hilang_di_bursa"
    assert pos.closed_at is not None
    assert pos.updated_at >= pos.opened_at
    assert any(a.kind == "position_missing" for a in laporan.actions)
    assert any(a.kind == "cancel_orphan_order" for a in laporan.actions)


@pytest.mark.asyncio
async def test_arah_tidak_cocok_posisi_ditutup(store: PositionStore) -> None:
    store.save_position(position(side="long"))
    broker = BrokerPalsu(ExchangePosition("DOGEUSDT", Decimal("-133"), Decimal("0.36")))
    laporan = await reconcile(broker=broker, store=store)
    assert any(a.kind == "side_mismatch" for a in laporan.actions)
    assert any(i.kind == "side_mismatch" for i in store.incidents())
    pos = store.get_position("DOGEUSDT")
    assert pos is not None
    assert pos.state is PositionState.FAILED
    assert pos.close_reason == "side_mismatch"
    assert pos.closed_at is not None
    assert await broker.position("DOGEUSDT") is None


@pytest.mark.asyncio
async def test_order_menggantung_tanpa_posisi_dibatalkan(store: PositionStore) -> None:
    broker = BrokerPalsu(None)
    from volumatic_bot.binance.trading import OrderRequest

    await broker.place(
        OrderRequest(
            symbol="ETHUSDT",
            side="SELL",
            type="STOP_MARKET",
            stop_price=Decimal("3000"),
            close_position=True,
            purpose="stop_loss",
        )
    )
    laporan = await reconcile(broker=broker, store=store, symbols=["ETHUSDT"])
    assert any(a.kind == "cancel_orphan_order" for a in laporan.actions)
    assert await broker.open_orders("ETHUSDT") == []


@pytest.mark.asyncio
async def test_sl_gagal_dipasang_ulang_posisi_ditutup(store: PositionStore) -> None:
    class BrokenBroker(BrokerPalsu):
        async def place(self, order: Any) -> Any:
            if order.type == "STOP_MARKET":
                raise RuntimeError("Order would immediately trigger")
            return await super().place(order)

    broker = BrokenBroker(ExchangePosition("DOGEUSDT", Decimal("133"), Decimal("0.35")))
    store.save_position(position())
    laporan = await reconcile(broker=broker, store=store)
    kinds = [a.kind for a in laporan.actions]
    assert "missing_stop_loss" in kinds
    assert "close_without_stop" in kinds
    assert await broker.position("DOGEUSDT") is None
    assert any(i.kind == "rearm_failed" for i in store.incidents())


def test_ringkasan_tanpa_aksi() -> None:
    from volumatic_bot.execution.reconcile import ReconcileReport

    assert "tidak ada perbedaan" in ReconcileReport().summary()
