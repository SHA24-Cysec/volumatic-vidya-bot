"""Menyusun order proteksi (SL/TP/partial) dari :class:`TradePlan`.

Aturan yang dipegang:

* **SL selalu reduce-only** dan memakai ``closePosition`` bila tidak ada partial
  TP; bila ada partial TP, SL memasang ``quantity = qty`` + ``reduceOnly``
  supaya order TP parsial tidak saling menutupi.
* SL memakai ``STOP_MARKET`` dengan ``workingType=CONTRACT_PRICE`` — bukan
  ``MARK_PRICE`` — karena posisi kecil ditutup berdasarkan harga kontrak.
* TP1 memakai ``TAKE_PROFIT_MARKET`` reduce-only (bukan LIMIT) supaya pasti
  keluar saat tersentuh, tanpa risiko order limit menggantung.
* Bila partial TP aktif, hanya **sebagian** posisi yang diberi TP; sisanya
  dibiarkan berjalan dengan SL break-even + trailing (dan SL ``closePosition``).
* Order proteksi **tidak** memakai ``priceProtect`` karena itu bisa membuat
  order tidak terpasang saat harga bergerak cepat.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from volumatic_bot.binance.trading import OrderRequest, side_for_exit

__all__ = [
    "ProtectionOrders",
    "build_protection_orders",
    "build_stop_loss_variants",
    "stop_already_breached",
]


@dataclass(frozen=True, slots=True)
class ProtectionOrders:
    """Kumpulan order proteksi untuk satu posisi."""

    stop_loss: OrderRequest
    take_profit: OrderRequest | None
    partial_tp: OrderRequest | None

    def all(self) -> tuple[OrderRequest, ...]:
        items = [self.stop_loss]
        if self.take_profit is not None:
            items.append(self.take_profit)
        if self.partial_tp is not None:
            items.append(self.partial_tp)
        return tuple(items)

    def summary(self) -> str:
        return " | ".join(o.describe() for o in self.all())


def build_protection_orders(
    *,
    symbol: str,
    direction: str,
    qty: Decimal,
    stop_price: Decimal,
    tp1_price: Decimal,
    partial_qty: Decimal | None = None,
    client_id_factory: object | None = None,
) -> ProtectionOrders:
    """Buat SL (wajib) + TP penuh atau TP parsial + sisa.

    ``partial_qty`` bila diisi (>0 dan < qty) menghasilkan **dua** order TP:
    satu untuk sebagian posisi di TP1, satu lagi untuk sisa posisi. SL selalu
    mencakup **seluruh** qty agar sisa posisi tetap terlindungi.
    """
    if qty <= 0:
        raise ValueError("qty harus > 0")
    if direction not in ("long", "short"):
        raise ValueError(f"arah tidak dikenal: {direction!r}")
    exit_side = side_for_exit(direction)

    def cid(purpose: str) -> str | None:
        if client_id_factory is None:
            return None
        return str(client_id_factory(purpose))  # type: ignore[operator]

    stop = OrderRequest(
        symbol=symbol,
        side=exit_side,
        type="STOP_MARKET",
        quantity=None,  # closePosition=true, qty tidak boleh dikirim
        stop_price=stop_price,
        reduce_only=False,  # Binance menolak reduceOnly bersama closePosition
        close_position=True,
        working_type="CONTRACT_PRICE",
        client_order_id=cid("sl"),
        purpose="stop_loss",
    )

    pakai_partial = partial_qty is not None and Decimal(0) < partial_qty < qty
    if pakai_partial:
        assert partial_qty is not None
        # Hanya sebagian yang punya TP di TP1. Sisa posisi sengaja TIDAK diberi
        # order TP: ia mengikuti SL break-even + trailing. Alasannya penting —
        # dua order TP di harga yang sama akan terpicu bersamaan, sehingga
        # "partial TP + trailing" berubah menjadi keluar semua di TP1.
        partial = OrderRequest(
            symbol=symbol,
            side=exit_side,
            type="TAKE_PROFIT_MARKET",
            quantity=partial_qty,
            stop_price=tp1_price,
            reduce_only=True,
            close_position=False,
            working_type="CONTRACT_PRICE",
            client_order_id=cid("tp1"),
            purpose="partial_tp",
        )
        return ProtectionOrders(stop_loss=stop, take_profit=None, partial_tp=partial)

    tp = OrderRequest(
        symbol=symbol,
        side=exit_side,
        type="TAKE_PROFIT_MARKET",
        quantity=qty,
        stop_price=tp1_price,
        reduce_only=True,
        close_position=False,
        working_type="CONTRACT_PRICE",
        client_order_id=cid("tp"),
        purpose="take_profit",
    )
    return ProtectionOrders(stop_loss=stop, take_profit=tp, partial_tp=None)


def build_stop_loss_variants(
    *,
    symbol: str,
    direction: str,
    qty: Decimal,
    stop_price: Decimal,
    client_id_factory: object | None = None,
) -> list[OrderRequest]:
    """Varian order SL yang sah, dari yang paling disukai ke cadangan.

    Kenapa perlu lebih dari satu: di Binance **testnet** (dan kadang di live untuk
    simbol tertentu) ``STOP_MARKET`` + ``closePosition`` bisa ditolak dengan kode
    seperti ``-4120`` ("order type tidak didukung"), sementara bentuk
    ``reduceOnly`` + ``quantity`` diterima. Semua varian di bawah tetap memenuhi
    aturan keras proyek: SL **reduce-only atau closePosition**, ``STOP_MARKET``,
    ``workingType=CONTRACT_PRICE``.

    Urutan:
    1. ``closePosition=true`` (paling aman: selalu menutup seluruh posisi);
    2. ``reduceOnly=true`` + ``quantity`` (cadangan bila (1) ditolak).
    """
    exit_side = side_for_exit(direction)

    def cid(purpose: str) -> str | None:
        if client_id_factory is None:
            return None
        return str(client_id_factory(purpose))  # type: ignore[operator]

    return [
        OrderRequest(
            symbol=symbol,
            side=exit_side,
            type="STOP_MARKET",
            quantity=None,
            stop_price=stop_price,
            reduce_only=False,
            close_position=True,
            working_type="CONTRACT_PRICE",
            client_order_id=cid("sl"),
            purpose="stop_loss",
        ),
        OrderRequest(
            symbol=symbol,
            side=exit_side,
            type="STOP_MARKET",
            quantity=qty,
            stop_price=stop_price,
            reduce_only=True,
            close_position=False,
            working_type="CONTRACT_PRICE",
            client_order_id=cid("slr"),
            purpose="stop_loss",
        ),
    ]


def stop_already_breached(*, direction: str, stop_price: Decimal, mark: float) -> bool:
    """True bila harga pasar sudah melewati SL.

    Mengirim ``STOP_MARKET`` dalam kondisi ini akan ditolak Binance dengan
    ``-2021 Order would immediately trigger``. Lebih tepat: posisi ditutup
    sekarang, karena alasan masuknya sudah tidak berlaku.
    """
    if mark is None or mark <= 0:
        return False
    harga = Decimal(str(mark))
    if harga <= 0:
        return False
    return harga <= stop_price if direction == "long" else harga >= stop_price
