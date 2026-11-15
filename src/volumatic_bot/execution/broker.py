"""Abstraksi broker: satu kontrak untuk eksekusi **nyata** dan **simulasi**.

Eksekutor tidak tahu apakah order benar-benar pergi ke Binance atau hanya ke
:class:`DryRunBroker`. Dengan begitu alur entry → proteksi → partial TP → BE →
trailing bisa diuji penuh **tanpa jaringan dan tanpa risiko**, lalu dipakai apa
adanya untuk testnet/live.

``DryRunBroker`` bukan emulator pasar: ia mengisi order pada harga yang diberikan
(best-effort) dan mengikuti aturan fill sederhana:

* ``MARKET`` langsung terisi pada harga acuan;
* ``LIMIT`` terisi bila harga acuan menyentuh harganya, selain itu tetap ``NEW``;
* ``STOP_MARKET``/``TAKE_PROFIT_MARKET`` terpicu saat :meth:`DryRunBroker.on_price`
  melewati ``stopPrice``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable

from volumatic_bot.binance.trading import OrderRequest, OrderResult, TradingAPI

__all__ = ["Broker", "DryRunBroker", "ExchangePosition", "LiveBroker"]


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    """Posisi menurut bursa (qty bertanda: + long, − short)."""

    symbol: str
    qty: Decimal
    entry_price: Decimal
    unrealized: float = 0.0
    leverage: int = 1
    margin_type: str = "isolated"

    @property
    def side(self) -> str | None:
        if self.qty > 0:
            return "long"
        if self.qty < 0:
            return "short"
        return None


@runtime_checkable
class Broker(Protocol):
    """Kontrak minimum yang dibutuhkan eksekutor."""

    name: str

    async def place(self, order: OrderRequest) -> OrderResult: ...

    async def status(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> OrderResult: ...

    async def cancel(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> bool: ...

    async def position(self, symbol: str) -> ExchangePosition | None: ...

    async def open_orders(self, symbol: str) -> list[OrderResult]: ...


class LiveBroker:
    """Broker nyata (testnet atau live) di atas :class:`TradingAPI`."""

    name = "live"

    def __init__(self, api: TradingAPI) -> None:
        self.api = api

    async def place(self, order: OrderRequest) -> OrderResult:
        return await self.api.place_order(order)

    async def status(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> OrderResult:
        payload = await self.api.query_order(
            symbol, order_id=order_id, client_order_id=client_order_id
        )
        from volumatic_bot.binance.trading import parse_order_result

        return parse_order_result(payload)

    async def cancel(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> bool:
        """Batalkan order — order biasa dan algo order dicoba dua-duanya.

        Sejak Algo Order API dipisah (2025-12-09), SL/TP hidup di
        ``/fapi/v1/algoOrder`` dan TIDAK terlihat oleh ``DELETE /fapi/v1/order``.
        Karena pemanggil hanya menyimpan ``order_id`` (algoId untuk order algo),
        di sini dicoba endpoint biasa dulu lalu endpoint algo; keduanya murah
        (bobot 1) dan kegagalan salah satunya normal.
        """
        try:
            await self.api.cancel_order(symbol, order_id=order_id, client_order_id=client_order_id)
            return True
        except Exception:
            pass
        try:
            await self.api.cancel_algo_order(algo_id=order_id, client_algo_id=client_order_id)
            return True
        except Exception:
            # Order mungkin sudah terisi/berakhir; itu bukan kegagalan fatal.
            return False

    async def position(self, symbol: str) -> ExchangePosition | None:
        rows = await self.api.position_risk(symbol)
        for row in rows:
            if str(row.get("symbol", "")).upper() != symbol.upper():
                continue
            qty = Decimal(str(row.get("positionAmt", "0")))
            if qty == 0:
                return None
            return ExchangePosition(
                symbol=symbol.upper(),
                qty=qty,
                entry_price=Decimal(str(row.get("entryPrice", "0"))),
                unrealized=float(row.get("unRealizedProfit", 0) or 0),
                leverage=int(float(row.get("leverage", 1) or 1)),
                margin_type=str(row.get("marginType", "isolated")).lower(),
            )
        return None

    async def open_orders(self, symbol: str) -> list[OrderResult]:
        """Order aktif: gabungan order biasa **dan** algo order.

        Rekonsiliasi memakai daftar ini untuk memutuskan apakah SL masih ada.
        Sejak SL pindah ke Algo Order API, membaca ``/fapi/v1/openOrders`` saja
        akan menyimpulkan "SL hilang" padahal SL-nya aktif — kegagalan yang
        berbahaya karena memicu pemasangan SL ganda.
        """
        from volumatic_bot.binance.trading import parse_order_result

        payload = await self.api.open_orders(symbol)
        hasil = [parse_order_result(row) for row in payload]
        try:
            algo = await self.api.open_algo_orders(symbol)
        except Exception:
            # Endpoint algo tidak tersedia (mis. bursa lama): abaikan.
            return hasil
        hasil.extend(parse_order_result(row) for row in algo)
        return hasil


@dataclass(slots=True)
class DryRunBroker:
    """Broker simulasi: order "dikirim" dan diisi di memori, tanpa jaringan."""

    name: str = "dry_run"
    taker_bps: float = 5.0
    maker_bps: float = 2.0
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1000))
    _orders: dict[int, OrderResult] = field(default_factory=dict)
    _positions: dict[str, ExchangePosition] = field(default_factory=dict)
    prices: dict[str, Decimal] = field(default_factory=dict)
    fees_usdt: float = 0.0
    realized_usdt: float = 0.0
    events: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ util
    def set_price(self, symbol: str, price: float | Decimal) -> None:
        self.prices[symbol.upper()] = Decimal(str(price))

    def _price(self, symbol: str) -> Decimal:
        return self.prices.get(symbol.upper(), Decimal(0))

    def _fee(self, notional: Decimal, *, maker: bool) -> Decimal:
        bps = self.maker_bps if maker else self.taker_bps
        fee = abs(notional) * Decimal(str(bps)) / Decimal(10_000)
        self.fees_usdt += float(fee)
        return fee

    def _next_id(self) -> int:
        return next(self._ids)

    # --------------------------------------------------------------- broker
    async def place(self, order: OrderRequest) -> OrderResult:
        order_id = self._next_id()
        cid = order.client_order_id or f"dry-{order_id}"
        symbol = order.symbol.upper()
        harga = self._price(symbol)
        qty = order.quantity or Decimal(0)

        if order.type == "MARKET":
            if harga <= 0:
                raise ValueError(f"harga {symbol} belum diset pada DryRunBroker")
            result = self._fill(
                order, order_id, cid, qty=qty, price=harga, maker=False, status="FILLED"
            )
            self.events.append(f"market {order.side} {qty} @ {harga}")
            return result

        if order.type == "LIMIT":
            limit = order.price or Decimal(0)
            crossed = harga > 0 and (
                (order.side == "BUY" and harga <= limit)
                or (order.side == "SELL" and harga >= limit)
            )
            if crossed:
                result = self._fill(
                    order, order_id, cid, qty=qty, price=limit, maker=True, status="FILLED"
                )
                self.events.append(f"limit {order.side} {qty} @ {limit}")
                return result
            return self._store(
                OrderResult(
                    symbol=symbol,
                    order_id=order_id,
                    client_order_id=cid,
                    side=order.side,
                    type=order.type,
                    status="NEW",
                    orig_qty=qty,
                    executed_qty=Decimal(0),
                    avg_price=Decimal(0),
                    stop_price=order.stop_price or Decimal(0),
                    reduce_only=order.reduce_only,
                    close_position=order.close_position,
                    purpose=order.purpose,
                )
            )

        # Order kondisional: disimpan sampai harga menyentuhnya.
        return self._store(
            OrderResult(
                symbol=symbol,
                order_id=order_id,
                client_order_id=cid,
                side=order.side,
                type=order.type,
                status="NEW",
                orig_qty=qty,
                executed_qty=Decimal(0),
                avg_price=Decimal(0),
                stop_price=order.stop_price or Decimal(0),
                reduce_only=order.reduce_only,
                close_position=order.close_position,
                purpose=order.purpose,
            )
        )

    def _store(self, result: OrderResult) -> OrderResult:
        self._orders[result.order_id] = result
        return result

    def _fill(
        self,
        order: OrderRequest,
        order_id: int,
        cid: str,
        *,
        qty: Decimal,
        price: Decimal,
        maker: bool,
        status: str,
    ) -> OrderResult:
        pos = self._positions.get(order.symbol.upper())
        reduce_only = order.reduce_only or order.close_position
        # Untuk order ``closePosition`` jumlahnya selalu = besar posisi (tanpa tanda),
        # karena perhitungan sisa posisi di bawah memakai besaran positif.
        close_qty = abs(pos.qty) if (order.close_position and pos is not None) else qty

        pnl = Decimal(0)
        if reduce_only:
            if pos is None:  # tidak ada posisi: order reduce-only diabaikan
                return self._store(
                    OrderResult(
                        symbol=order.symbol.upper(),
                        order_id=order_id,
                        client_order_id=cid,
                        side=order.side,
                        type=order.type,
                        status="EXPIRED",
                        orig_qty=close_qty,
                        executed_qty=Decimal(0),
                        avg_price=Decimal(0),
                        stop_price=order.stop_price or Decimal(0),
                        reduce_only=True,
                        close_position=order.close_position,
                        purpose=order.purpose,
                    )
                )
            self._fee(close_qty * price, maker=maker)
            sisa = pos.qty - close_qty if pos.qty > 0 else pos.qty + close_qty
            arah = 1 if pos.qty > 0 else -1
            pnl = (price - pos.entry_price) * close_qty * arah
            self.realized_usdt += float(pnl)
            if abs(sisa) < Decimal("1e-12"):
                self._positions.pop(order.symbol.upper(), None)
            else:
                self._positions[order.symbol.upper()] = ExchangePosition(
                    symbol=pos.symbol,
                    qty=sisa,
                    entry_price=pos.entry_price,
                    leverage=pos.leverage,
                )
        else:
            self._fee(close_qty * price, maker=maker)
            if pos is None:
                self._positions[order.symbol.upper()] = ExchangePosition(
                    symbol=order.symbol.upper(),
                    qty=close_qty if order.side == "BUY" else -close_qty,
                    entry_price=price,
                )
            else:
                tambah = close_qty if order.side == "BUY" else -close_qty
                total = pos.qty + tambah
                self._positions[order.symbol.upper()] = ExchangePosition(
                    symbol=pos.symbol, qty=total, entry_price=pos.entry_price, leverage=pos.leverage
                )

        return self._store(
            OrderResult(
                symbol=order.symbol.upper(),
                order_id=order_id,
                client_order_id=cid,
                side=order.side,
                type=order.type,
                status=status,
                orig_qty=close_qty,
                executed_qty=close_qty,
                avg_price=price,
                stop_price=order.stop_price or Decimal(0),
                reduce_only=order.reduce_only,
                close_position=order.close_position,
                purpose=order.purpose,
                realized_pnl=pnl,
            )
        )

    async def status(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> OrderResult:
        if order_id is not None and order_id in self._orders:
            return self._orders[order_id]
        if client_order_id is not None:
            for order in self._orders.values():
                if order.client_order_id == client_order_id:
                    return order
        raise KeyError(f"order tidak ditemukan: {order_id or client_order_id}")

    async def cancel(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> bool:
        target = None
        if order_id is not None:
            target = self._orders.get(order_id)
        elif client_order_id is not None:
            target = next(
                (o for o in self._orders.values() if o.client_order_id == client_order_id), None
            )
        if target is None or target.status != "NEW":
            return False
        self._orders[target.order_id] = OrderResult(
            symbol=target.symbol,
            order_id=target.order_id,
            client_order_id=target.client_order_id,
            side=target.side,
            type=target.type,
            status="CANCELED",
            orig_qty=target.orig_qty,
            executed_qty=target.executed_qty,
            avg_price=target.avg_price,
            stop_price=target.stop_price,
            reduce_only=target.reduce_only,
            close_position=target.close_position,
            purpose=target.purpose,
        )
        self.events.append(f"cancel {target.client_order_id}")
        return True

    async def position(self, symbol: str) -> ExchangePosition | None:
        return self._positions.get(symbol.upper())

    async def open_orders(self, symbol: str) -> list[OrderResult]:
        return [
            o for o in self._orders.values() if o.symbol == symbol.upper() and o.status == "NEW"
        ]

    # ------------------------------------------------------------- simulasi
    async def on_price(self, symbol: str, price: float | Decimal) -> list[OrderResult]:
        """Gerakkan harga: picu order kondisional yang tersentuh."""
        harga = Decimal(str(price))
        symbol = symbol.upper()
        self.set_price(symbol, harga)
        terpicu: list[OrderResult] = []
        for order in list(self._orders.values()):
            if order.symbol != symbol or order.status != "NEW":
                continue
            stop = order.stop_price
            if stop <= 0:
                continue
            if order.type == "STOP_MARKET":
                kena = harga <= stop if order.side == "SELL" else harga >= stop
            elif order.type == "TAKE_PROFIT_MARKET":
                kena = harga >= stop if order.side == "SELL" else harga <= stop
            else:
                kena = False
            if not kena:
                continue
            pos = self._positions.get(symbol)
            qty = pos.qty if (order.close_position and pos is not None) else order.orig_qty
            filled = self._fill(
                OrderRequest(
                    symbol=symbol,
                    side=order.side,
                    type=order.type,  # type: ignore[arg-type]
                    quantity=None if order.close_position else qty,
                    stop_price=stop,
                    reduce_only=order.reduce_only,
                    close_position=order.close_position,
                    client_order_id=order.client_order_id,
                    purpose=order.purpose,
                ),
                order.order_id,
                order.client_order_id,
                qty=abs(qty),
                price=stop,
                maker=False,
                status="FILLED",
            )
            terpicu.append(filled)
            self.events.append(f"{order.type} terpicu @ {stop}")
        # SL didahulukan bila beberapa order tersentuh pada tick yang sama:
        # asumsi pesimistis, sama seperti backtester.
        terpicu.sort(key=lambda o: 0 if o.type == "STOP_MARKET" else 1)
        return terpicu
