"""Test jalur darurat proteksi (dari insiden testnet 15 Nov 2026).

Insiden nyata: SL gagal dipasang (BinanceAPIError pada /fapi/v1/order) lalu
penutupan market juga gagal → posisi tanpa SL. Test di sini menjaga:
varian SL cadangan, deteksi stop yang sudah terlampaui, penanganan -2022, dan
penanda ``naked`` yang membuat mesin berhenti.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from volumatic_bot.binance.errors import BinanceAPIError
from volumatic_bot.binance.trading import OrderResult
from volumatic_bot.config import load_config
from volumatic_bot.execution.broker import ExchangePosition
from volumatic_bot.execution.executor import TradeExecutor
from volumatic_bot.execution.protect import build_stop_loss_variants, stop_already_breached
from volumatic_bot.positions.models import PositionState, TrackedPosition
from volumatic_bot.positions.store import PositionStore
from volumatic_bot.storage.db import SqliteDatabase

ROOT = Path(__file__).parents[1]


# ------------------------------------------------------------------ varian SL
def test_varian_sl_pertama_close_position_lalu_reduce_only() -> None:
    varian = build_stop_loss_variants(
        symbol="DOGEUSDT", direction="short", qty=Decimal("100"), stop_price=Decimal("0.36")
    )
    assert len(varian) == 2
    assert varian[0].close_position is True and varian[0].reduce_only is False
    assert varian[0].quantity is None
    assert varian[1].reduce_only is True and varian[1].close_position is False
    assert varian[1].quantity == Decimal("100")
    for v in varian:
        assert v.type == "STOP_MARKET"
        assert v.working_type == "CONTRACT_PRICE"


@pytest.mark.parametrize(
    ("arah", "stop", "mark", "kena"),
    [
        ("long", Decimal("100"), 99.0, True),  # harga sudah di bawah SL
        ("long", Decimal("100"), 100.0, True),
        ("long", Decimal("100"), 101.0, False),
        ("short", Decimal("100"), 101.0, True),  # harga sudah di atas SL
        ("short", Decimal("100"), 99.0, False),
        ("long", Decimal("100"), 0.0, False),  # harga tidak valid -> abaikan
    ],
)
def test_deteksi_stop_terlampaui(arah: str, stop: Decimal, mark: float, kena: bool) -> None:
    assert stop_already_breached(direction=arah, stop_price=stop, mark=mark) is kena


# ---------------------------------------------------------------- eksekutor
class _Broker:
    """Broker palsu: perilaku per jenis order diatur lewat ``mode``."""

    name = "palsu"

    def __init__(self, *, sl_gagal: set[int] | None = None, tutup_error: int | None = None):
        self.sl_gagal = sl_gagal or set()
        self.tutup_error = tutup_error
        self.dikirim: list[str] = []
        self.tutup_dikirim = 0

    async def place(self, order: Any) -> OrderResult:
        self.dikirim.append(order.describe())
        if order.purpose == "stop_loss":
            urutan = sum(1 for d in self.dikirim if "STOP_MARKET" in d)
            if urutan in self.sl_gagal:
                raise BinanceAPIError("Order type not supported", status=400, code=-4120)
            return self._hasil(order, status="NEW")
        if order.purpose == "close":
            self.tutup_dikirim += 1
            if self.tutup_error is not None:
                raise BinanceAPIError(
                    "ReduceOnly Order is rejected", status=400, code=self.tutup_error
                )
            return self._hasil(order, status="FILLED", executed=order.quantity or Decimal(0))
        return self._hasil(order, status="FILLED", executed=order.quantity or Decimal(0))

    @staticmethod
    def _hasil(order: Any, *, status: str, executed: Decimal | None = None) -> OrderResult:
        return OrderResult(
            symbol=order.symbol,
            order_id=1,
            client_order_id="x",
            side=order.side,
            type=order.type,
            status=status,
            orig_qty=order.quantity or Decimal(0),
            executed_qty=executed or Decimal(0),
            avg_price=order.price or Decimal("100"),
            stop_price=order.stop_price or Decimal(0),
            reduce_only=order.reduce_only,
            close_position=order.close_position,
            purpose=order.purpose,
        )

    async def position(self, symbol: str) -> ExchangePosition | None:
        return None

    async def cancel(self, symbol: str, **_: Any) -> bool:
        return True

    async def status(self, symbol: str, **_: Any) -> OrderResult:
        raise KeyError(symbol)


@pytest.fixture()
def store(tmp_path: Path) -> PositionStore:
    db = SqliteDatabase(str(tmp_path / "uji.db"))
    db.open()
    yield PositionStore(db)
    db.close()


def _pos() -> TrackedPosition:
    return TrackedPosition(
        symbol="DOGEUSDT",
        side="long",
        state=PositionState.OPEN,
        qty=Decimal("100"),
        remaining_qty=Decimal("100"),
        entry_price=Decimal("0.35"),
        stop_price=Decimal("0.34"),
        tp1_price=Decimal("0.37"),
        risk_per_unit=Decimal("0.01"),
        leverage=5,
        mode="testnet",
        opened_at=1,
        updated_at=1,
    )


def _executor(broker: Any, store: PositionStore, mark: float | None = None) -> TradeExecutor:
    async def harga(_: str) -> float | None:
        return mark

    return TradeExecutor(
        load_config(ROOT / "config.yaml"),
        broker,
        store,
        clock=lambda: 1_700_000_000_000,
        mark_price=harga,
    )


@pytest.mark.asyncio
async def test_varian_cadangan_dipakai_saat_close_position_ditolak(
    store: PositionStore,
) -> None:
    """Bila varian 1 ditolak (-4120), varian 2 (reduceOnly) dipakai."""
    broker = _Broker(sl_gagal={1})
    laporan = await _executor(broker, store, mark=0.35)._arm_protection(_pos(), partial_qty=None)
    assert laporan.ok is True
    assert any("varian 2" in d for d in broker.dikirim) or len(broker.dikirim) >= 2
    insiden = [i for i in store.incidents(limit=10) if i.kind == "sl_fallback"]
    assert insiden, "insiden sl_fallback harus dicatat"


@pytest.mark.asyncio
async def test_semua_varian_gagal_posisi_ditutup_dan_tidak_telanjang(
    store: PositionStore,
) -> None:
    broker = _Broker(sl_gagal={1, 2})
    laporan = await _executor(broker, store, mark=0.35)._arm_protection(_pos(), partial_qty=None)
    assert laporan.ok is False
    assert broker.tutup_dikirim == 1
    assert laporan.naked is False


@pytest.mark.asyncio
async def test_kalau_tutup_juga_gagal_ditandai_telanjang(store: PositionStore) -> None:
    """Insiden nyata: SL gagal + close gagal -> posisi telanjang, harus menyalak."""
    broker = _Broker(sl_gagal={1, 2}, tutup_error=-2021)
    laporan = await _executor(broker, store, mark=0.35)._arm_protection(_pos(), partial_qty=None)
    assert laporan.ok is False
    assert laporan.naked is True


@pytest.mark.asyncio
async def test_minus_2022_dianggap_sudah_rata(store: PositionStore) -> None:
    """-2022 (tidak ada posisi untuk dikurangi) bukan kegagalan bila bursa rata."""
    broker = _Broker(tutup_error=-2022)
    pos = _pos()
    ok = await _executor(broker, store)._close_market(pos, reason="protection_failed")
    assert ok is True
    assert pos.state == PositionState.CLOSED


@pytest.mark.asyncio
async def test_sl_tidak_dikirim_bila_harga_sudah_melewati_stop(store: PositionStore) -> None:
    """Hindari -2021: bila harga sudah lewat SL, posisi ditutup, SL tidak dikirim."""
    broker = _Broker()
    laporan = await _executor(broker, store, mark=0.33)._arm_protection(_pos(), partial_qty=None)
    assert laporan.ok is False
    assert not any("STOP_MARKET" in d for d in broker.dikirim)
    assert broker.tutup_dikirim == 1
    assert any(i.kind == "stop_already_breached" for i in store.incidents(limit=10))
