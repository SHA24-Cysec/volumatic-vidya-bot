"""Penyesuai bentuk data: ``OrderResult`` → ``OrderUpdate``.

``DryRunBroker`` mengembalikan :class:`~volumatic_bot.binance.trading.OrderResult`
(hasil permintaan order), sedangkan alur produksi menerima
:class:`~volumatic_bot.binance.user_stream.OrderUpdate` dari User Data Stream.
Eksekutor hanya memahami bentuk kedua, jadi keduanya disamakan di satu tempat —
supaya simulasi dan produksi melewati kode yang sama.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from volumatic_bot.binance.user_stream import OrderUpdate

if TYPE_CHECKING:
    from volumatic_bot.binance.trading import OrderResult

__all__ = ["order_result_to_update"]


def order_result_to_update(order: OrderResult) -> OrderUpdate:
    """Bungkus hasil order menjadi event ``ORDER_TRADE_UPDATE`` tiruan."""
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
        realized_pnl=order.realized_pnl,
        is_maker=False,
        event_time_ms=0,
    )
