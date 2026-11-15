"""Parsing pesan kline dari REST dan WebSocket Binance.

Dokumentasi kline stream (snapshot 2026-03-05) menyatakan event dikirim setiap
250 ms untuk candle yang sedang berjalan, dengan field ``x`` = "Is this kline
closed?". Bot **hanya** boleh memakai candle dengan ``x == true`` agar tidak
repainting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["KlineEvent", "parse_kline_event", "parse_rest_kline"]


@dataclass(frozen=True, slots=True)
class KlineEvent:
    """Satu event kline dari WebSocket."""

    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    is_closed: bool
    event_time: int
    first_trade_id: int | None = None
    last_trade_id: int | None = None
    trades: int | None = None

    @property
    def stream(self) -> str:
        return f"{self.symbol.lower()}@kline_{self.interval}"


def _f(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_kline_event(payload: dict[str, Any]) -> KlineEvent:
    """Ubah payload event ``kline`` menjadi :class:`KlineEvent`.

    Melempar ``ValueError`` bila struktur wajib tidak ada; pemanggil boleh
    mengabaikan event yang tidak dikenal (mis. event lain di stream campuran).
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        raise ValueError("payload kline bukan objek")
    if str(data.get("e", "kline")) != "kline":
        raise ValueError(f"bukan event kline: e={data.get('e')!r}")
    k = data.get("k")
    if not isinstance(k, dict):
        raise ValueError("payload kline tanpa field 'k'")
    for field in ("t", "o", "h", "l", "c"):
        if field not in k:
            raise ValueError(f"field kline '{field}' tidak ada")
    return KlineEvent(
        symbol=str(k.get("s", data.get("s", ""))).upper(),
        interval=str(k.get("i", "")),
        open_time=int(k["t"]),
        close_time=int(k.get("T", 0) or 0),
        open=_f(k.get("o")),
        high=_f(k.get("h")),
        low=_f(k.get("l")),
        close=_f(k.get("c")),
        volume=_f(k.get("v")),
        quote_volume=_f(k.get("q")),
        # Field 'x' bertipe bool di dokumentasi; beberapa klien mengirim string.
        is_closed=bool(k.get("x", False)),
        event_time=_i(data.get("E")) or 0,
        first_trade_id=_i(k.get("f")),
        last_trade_id=_i(k.get("L")),
        trades=_i(k.get("n")),
    )


@dataclass(frozen=True, slots=True)
class KlineRow:
    """Baris candle hasil REST ``/fapi/v1/klines``."""

    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float

    def as_tuple(self) -> tuple[int, float, float, float, float, float]:
        return (self.open_time, self.open, self.high, self.low, self.close, self.volume)


def parse_rest_kline(raw: list[Any]) -> KlineRow:
    """Baris kline REST: ``[openTime, o, h, l, c, v, closeTime, quoteVolume, ...]``."""
    if not isinstance(raw, list) or len(raw) < 7:
        raise ValueError(f"baris kline REST tidak valid: {raw!r}")
    return KlineRow(
        open_time=int(raw[0]),
        open=_f(raw[1]),
        high=_f(raw[2]),
        low=_f(raw[3]),
        close=_f(raw[4]),
        volume=_f(raw[5]),
        close_time=int(raw[6]),
        quote_volume=_f(raw[7]) if len(raw) > 7 else 0.0,
    )
