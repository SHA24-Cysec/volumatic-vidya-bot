"""Model data order book.

Representasi yang dipakai konsisten untuk dua sumber:

* **REST** ``GET /fapi/v1/depth`` — snapshot penuh ``limit`` level teratas
  (bobot 2 untuk limit 5–50 menurut dokumentasi 2026).
* **Stream** ``<symbol>@depth<levels>@<speed>ms`` — level teratas yang
  diperbarui terus-menerus (``levels`` valid 5, 10, 20).

Keduanya menghasilkan :class:`OrderBookSnapshot` yang sama, sehingga analisis
dan aturan filter tidak perlu tahu asal datanya.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "BookLevel",
    "OrderBookSnapshot",
    "Wall",
    "parse_depth_event",
    "parse_rest_depth",
]

Side = Literal["bid", "ask"]


@dataclass(frozen=True, slots=True)
class BookLevel:
    """Satu level harga di order book."""

    price: float
    qty: float

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """Snapshot order book beserta waktu pengambilannya.

    ``bids`` terurut dari harga tertinggi (best bid) ke bawah, ``asks`` terurut
    dari harga terendah (best ask) ke atas.
    """

    symbol: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    last_update_id: int
    event_time_ms: int
    source: Literal["rest", "stream"]
    fetched_at: float  # jam monotonic (time.monotonic) saat snapshot diambil

    # ------------------------------------------------------------------ dasar
    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def mid(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_abs(self) -> float:
        if not self.bids or not self.asks:
            return float("inf")
        return self.best_ask - self.best_bid

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        if mid <= 0:
            return float("inf")
        return self.spread_abs / mid * 100.0

    def spread_ticks(self, tick_size: float) -> float:
        """Spread dalam jumlah tick (0.5 = setengah tick, artinya tick lebih besar dari spread)."""
        if tick_size <= 0:
            return float("nan")
        return self.spread_abs / tick_size

    def age_sec(self, now: float) -> float:
        return max(0.0, now - self.fetched_at)

    def is_usable(self) -> bool:
        """Sisi bid & ask tidak kosong dan tidak bersilangan (crossed book)."""
        return bool(self.bids) and bool(self.asks) and self.best_ask > self.best_bid > 0

    def levels_for(self, side: Side) -> tuple[BookLevel, ...]:
        return self.bids if side == "bid" else self.asks

    def __len__(self) -> int:
        return len(self.bids) + len(self.asks)


@dataclass(frozen=True, slots=True)
class Wall:
    """Level yang jauh lebih besar dari level sekitarnya ("dinding")."""

    side: Side
    price: float
    qty: float
    ratio_to_median: float
    distance_pct: float  # jarak dari mid dalam persen
    distance_atr: float = float("nan")  # diisi analisis bila ATR diketahui

    @property
    def notional(self) -> float:
        return self.price * self.qty


def _levels(raw: Any) -> tuple[BookLevel, ...]:
    """Ubah ``[["harga","qty"], ...]`` menjadi tuple level, buang qty nol/rusak."""
    out: list[BookLevel] = []
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            price = float(item[0])
            qty = float(item[1])
        except (TypeError, ValueError):
            continue
        if price <= 0 or qty <= 0:
            continue  # qty 0 berarti level sudah hilang dari buku
        out.append(BookLevel(price=price, qty=qty))
    return tuple(out)


def parse_rest_depth(
    payload: dict[str, Any],
    symbol: str,
    *,
    fetched_at: float,
) -> OrderBookSnapshot:
    """Parse respons ``GET /fapi/v1/depth``.

    Dokumentasi 2026: ``{"lastUpdateId":..., "E":..., "T":..., "bids":[["p","q"]],
    "asks":[["p","q"]]}``. Urutan harga diurutkan ulang secara defensif.
    """
    if not isinstance(payload, dict) or "bids" not in payload or "asks" not in payload:
        raise ValueError("payload depth REST tidak valid (bids/asks tidak ada)")
    bids = tuple(sorted(_levels(payload.get("bids")), key=lambda level: level.price, reverse=True))
    asks = tuple(sorted(_levels(payload.get("asks")), key=lambda level: level.price))
    try:
        last_update_id = int(payload.get("lastUpdateId", 0) or 0)
        event_time = int(payload.get("E") or payload.get("T") or 0)
    except (TypeError, ValueError):
        last_update_id, event_time = 0, 0
    return OrderBookSnapshot(
        symbol=symbol.upper(),
        bids=bids,
        asks=asks,
        last_update_id=last_update_id,
        event_time_ms=event_time,
        source="rest",
        fetched_at=fetched_at,
    )


def parse_depth_event(payload: dict[str, Any], *, fetched_at: float) -> OrderBookSnapshot:
    """Parse event ``depthUpdate`` dari stream depth parsial.

    Untuk stream parsial, ``b`` dan ``a`` memuat level teratas **terkini**
    (bukan delta kumulatif), jadi keduanya langsung dipakai sebagai snapshot.
    Level dengan kuantitas 0 dibuang (artinya harga tersebut sudah tidak ada).
    """
    if not isinstance(payload, dict):
        raise ValueError("payload depth stream bukan objek")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict) or str(data.get("e", "")) != "depthUpdate":
        raise ValueError("bukan event depthUpdate")
    symbol = str(data.get("s", ""))
    if not symbol:
        raise ValueError("event depth tanpa simbol")
    bids = tuple(sorted(_levels(data.get("b")), key=lambda level: level.price, reverse=True))
    asks = tuple(sorted(_levels(data.get("a")), key=lambda level: level.price))
    try:
        final_update = int(data.get("u", 0) or 0)
        event_time = int(data.get("E") or data.get("T") or 0)
    except (TypeError, ValueError):
        final_update, event_time = 0, 0
    return OrderBookSnapshot(
        symbol=symbol.upper(),
        bids=bids,
        asks=asks,
        last_update_id=final_update,
        event_time_ms=event_time,
        source="stream",
        fetched_at=fetched_at,
    )
