"""Metadata simbol & filter dari ``GET /fapi/v1/exchangeInfo``.

Semua filter dibaca dari exchange (LOT_SIZE, MARKET_LOT_SIZE, PRICE_FILTER,
MIN_NOTIONAL) — tidak ada yang di-hardcode — lalu dipakai untuk pembulatan
harga/jumlah yang sah dan untuk menilai kelayakan modal kecil.

Pembulatan memakai :class:`decimal.Decimal` supaya tidak terkena galat floating
point (mis. ``0.1 + 0.2``), dan hasil akhir selalu dikembalikan sebagai *string*
karena Binance mewajibkan format desimal eksplisit di parameter order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any

__all__ = [
    "ExchangeInfo",
    "SymbolFilters",
    "SymbolInfo",
    "parse_exchange_info",
    "round_down_step",
    "round_nearest_step",
    "round_to_tick",
]


def _dec(value: Any, *, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _filter_map(filters: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(f.get("filterType", "")): f for f in filters}


def _min_notional_value(raw: dict[str, Any] | None) -> Decimal:
    """USD-M memakai ``notional``; sebagian respons lama memakai ``minNotional``."""
    if not raw:
        return Decimal(0)
    for key in ("notional", "minNotional"):
        if key in raw:
            return _dec(raw[key])
    return Decimal(0)


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    """Filter perdagangan satu simbol, sudah dalam ``Decimal``."""

    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    max_orders: int
    max_algo_orders: int

    # --- pembulatan --------------------------------------------------------
    def round_price(self, price: float | Decimal, *, mode: str = "nearest") -> Decimal:
        """Bulatkan harga ke kelipatan ``tickSize`` yang sah."""
        value = _dec(price)
        if self.tick_size <= 0:
            return value
        steps = value / self.tick_size
        if mode == "down":
            rounded = steps.to_integral_value(rounding=ROUND_DOWN)
        elif mode == "up":
            rounded = steps.to_integral_value(rounding=ROUND_UP)
        else:
            rounded = steps.to_integral_value()
        return (rounded * self.tick_size).quantize(self.tick_size)

    def round_qty(self, qty: float | Decimal, *, mode: str = "down") -> Decimal:
        """Bulatkan jumlah ke kelipatan ``stepSize`` (default ke bawah = aman)."""
        value = _dec(qty)
        if self.step_size <= 0:
            return value
        steps = value / self.step_size
        if mode == "up":
            rounded = steps.to_integral_value(rounding=ROUND_UP)
        elif mode == "nearest":
            rounded = steps.to_integral_value()
        else:
            rounded = steps.to_integral_value(rounding=ROUND_DOWN)
        return (rounded * self.step_size).quantize(self.step_size)

    def qty_is_valid(self, qty: float | Decimal) -> bool:
        value = _dec(qty)
        if value <= 0:
            return False
        if self.min_qty > 0 and value < self.min_qty:
            return False
        if self.max_qty > 0 and value > self.max_qty:
            return False
        return not (self.step_size > 0 and (value % self.step_size) != 0)

    def notional_ok(self, price: float | Decimal, qty: float | Decimal) -> bool:
        if self.min_notional <= 0:
            return True
        return _dec(price) * _dec(qty) >= self.min_notional

    def decimals(self, *, kind: str) -> int:
        """Jumlah angka desimal yang harus ditulis untuk ``kind`` = price/qty."""
        step = self.tick_size if kind == "price" else self.step_size
        if step <= 0:
            return 8
        exponent = step.as_tuple().exponent
        return max(0, -int(exponent))


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    """Metadata satu simbol perpetual USDT-M."""

    symbol: str
    pair: str
    contract_type: str
    status: str
    base_asset: str
    quote_asset: str
    price_precision: int
    quantity_precision: int
    order_types: tuple[str, ...]
    filters: SymbolFilters

    @property
    def is_perpetual_usdt(self) -> bool:
        return (
            self.contract_type == "PERPETUAL"
            and self.quote_asset == "USDT"
            and self.status == "TRADING"
        )

    def min_margin_required(self, leverage: int) -> Decimal:
        """Margin minimum agar satu order memenuhi ``minNotional`` di leverage ini."""
        if leverage <= 0:
            raise ValueError("leverage harus > 0")
        return self.filters.min_notional / Decimal(leverage)

    def format_price(self, price: float | Decimal) -> str:
        return f"{self.filters.round_price(price):.{self.filters.decimals(kind='price')}f}"

    def format_qty(self, qty: float | Decimal) -> str:
        return f"{self.filters.round_qty(qty):.{self.filters.decimals(kind='qty')}f}"


@dataclass(frozen=True, slots=True)
class ExchangeInfo:
    """Hasil parsing ``exchangeInfo``."""

    symbols: dict[str, SymbolInfo]
    rate_limits: tuple[dict[str, Any], ...]
    server_time: int | None = None

    def get(self, symbol: str) -> SymbolInfo:
        try:
            return self.symbols[symbol]
        except KeyError as exc:
            raise KeyError(f"simbol tidak ada di exchangeInfo: {symbol}") from exc

    @property
    def trading_usdt_perpetuals(self) -> list[SymbolInfo]:
        return [s for s in self.symbols.values() if s.is_perpetual_usdt]

    @property
    def weight_limit_per_min(self) -> int | None:
        for limit in self.rate_limits:
            if limit.get("rateLimitType") == "REQUEST_WEIGHT" and limit.get("interval") == "MINUTE":
                try:
                    return int(limit["limit"])
                except (KeyError, TypeError, ValueError):
                    return None
        return None


def parse_exchange_info(payload: dict[str, Any]) -> ExchangeInfo:
    """Ubah payload ``exchangeInfo`` menjadi objek yang tervalidasi.

    Simbol yang tidak lengkap filternya dilewati dengan aman (bukan bikin crash
    seluruh bot) — lebih baik kehilangan satu simbol daripada berhenti.
    """
    if not isinstance(payload, dict) or "symbols" not in payload:
        raise ValueError("payload exchangeInfo tidak valid (tidak ada 'symbols')")

    parsed: dict[str, SymbolInfo] = {}
    for raw in payload.get("symbols", []):
        try:
            symbol = str(raw["symbol"])
            filters = _filter_map(raw.get("filters", []))
            lot = filters.get("LOT_SIZE", {})
            market_lot = filters.get("MARKET_LOT_SIZE", lot)
            price_filter = filters.get("PRICE_FILTER", {})
            info = SymbolInfo(
                symbol=symbol,
                pair=str(raw.get("pair", symbol)),
                contract_type=str(raw.get("contractType", "")),
                status=str(raw.get("status", "")),
                base_asset=str(raw.get("baseAsset", "")),
                quote_asset=str(raw.get("quoteAsset", "")),
                price_precision=int(raw.get("pricePrecision", 8)),
                quantity_precision=int(raw.get("quantityPrecision", 8)),
                order_types=tuple(raw.get("orderTypes", ())),
                filters=SymbolFilters(
                    tick_size=_dec(price_filter.get("tickSize", "0")),
                    min_price=_dec(price_filter.get("minPrice", "0")),
                    max_price=_dec(price_filter.get("maxPrice", "0")),
                    step_size=_dec(lot.get("stepSize", "0")),
                    min_qty=_dec(lot.get("minQty", "0")),
                    max_qty=_dec(lot.get("maxQty", "0")),
                    market_step_size=_dec(market_lot.get("stepSize", lot.get("stepSize", "0"))),
                    market_min_qty=_dec(market_lot.get("minQty", lot.get("minQty", "0"))),
                    market_max_qty=_dec(market_lot.get("maxQty", lot.get("maxQty", "0"))),
                    min_notional=_min_notional_value(filters.get("MIN_NOTIONAL")),
                    max_orders=int(filters.get("MAX_NUM_ORDERS", {}).get("limit", 0)),
                    max_algo_orders=int(filters.get("MAX_NUM_ALGO_ORDERS", {}).get("limit", 0)),
                ),
            )
        except (KeyError, TypeError, ValueError):
            continue
        parsed[symbol] = info

    rate_limits = tuple(payload.get("rateLimits", ()) or ())
    server_time = payload.get("serverTime")
    return ExchangeInfo(
        symbols=parsed,
        rate_limits=rate_limits,
        server_time=int(server_time) if isinstance(server_time, (int, float)) else None,
    )


def round_down_step(value: float, step: Decimal) -> Decimal:
    """Bulatkan ke bawah ke kelipatan ``step``."""
    v = _dec(value)
    if step <= 0:
        return v
    return (v / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_nearest_step(value: float, step: Decimal) -> Decimal:
    v = _dec(value)
    if step <= 0:
        return v
    return (v / step).to_integral_value() * step


def round_to_tick(price: float, tick: Decimal) -> Decimal:
    """Bulatkan harga ke tick terdekat (dipakai untuk TP/SL)."""
    return round_nearest_step(price, tick)
