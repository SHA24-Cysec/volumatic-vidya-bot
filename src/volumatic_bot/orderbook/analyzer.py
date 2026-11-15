"""Analisis order book: spread, imbalance, wall, dan estimasi slippage.

Semua fungsi di sini **murni** (tanpa I/O, tanpa state) sehingga mudah diuji dan
dipakai ulang oleh backtester (TAHAP 7).

Keterbatasan yang harus diingat (diulang di dokumentasi & dashboard):
order book bisa **spoofing** — order besar bisa dipasang lalu dibatalkan dalam
milidetik. Semua metrik di sini karena itu bersifat *tambahan* untuk menyaring
sinyal, **bukan** sinyal utama.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Literal

from volumatic_bot.orderbook.models import BookLevel, OrderBookSnapshot, Wall

__all__ = [
    "BookMetrics",
    "FillEstimate",
    "analyze",
    "band_volume",
    "detect_wall",
    "estimate_fill",
    "imbalance_pct",
    "levels_within",
    "limit_price_for_slippage",
]

TradeSide = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class FillEstimate:
    """Perkiraan eksekusi market order terhadap snapshot buku."""

    side: TradeSide
    requested_qty: float
    filled_qty: float
    notional: float
    avg_price: float
    reference_price: float
    slippage_pct: float
    slippage_ticks: float
    levels_used: int
    complete: bool

    @property
    def unfilled_qty(self) -> float:
        return max(0.0, self.requested_qty - self.filled_qty)


@dataclass(frozen=True, slots=True)
class BookMetrics:
    """Ringkasan kondisi buku untuk satu arah trade."""

    mid: float
    spread_pct: float
    spread_ticks: float
    imbalance_pct: float  # + = bid lebih tebal, − = ask lebih tebal
    bid_volume: float
    ask_volume: float
    band_pct: float
    wall: Wall | None  # wall terdekat di sisi yang melawan arah entry
    slippage_pct: float
    slippage_ticks: float
    fill_complete: bool
    levels_used: int
    age_sec: float
    bid_levels: int
    ask_levels: int

    @property
    def depth_levels(self) -> int:
        return self.bid_levels + self.ask_levels


def levels_within(
    levels: tuple[BookLevel, ...], mid: float, range_pct: float
) -> tuple[BookLevel, ...]:
    """Level yang harganya berada dalam ``range_pct`` persen dari mid.

    Dipakai agar imbalance/wall hanya dihitung dari likuiditas yang relevan,
    bukan dari level jauh yang tidak akan tersentuh order kecil.
    """
    if mid <= 0 or range_pct <= 0:
        return ()
    low = mid * (1.0 - range_pct / 100.0)
    high = mid * (1.0 + range_pct / 100.0)
    return tuple(level for level in levels if low <= level.price <= high)


def band_volume(snapshot: OrderBookSnapshot, range_pct: float) -> tuple[float, float, int, int]:
    """Total volume bid & ask di dalam pita ``range_pct`` dari mid."""
    mid = snapshot.mid
    bids = levels_within(snapshot.bids, mid, range_pct)
    asks = levels_within(snapshot.asks, mid, range_pct)
    bid_vol = math.fsum(level.qty for level in bids)
    ask_vol = math.fsum(level.qty for level in asks)
    return bid_vol, ask_vol, len(bids), len(asks)


def imbalance_pct(snapshot: OrderBookSnapshot, range_pct: float) -> float:
    """Imbalance bid/ask dalam pita (%), positif = sisi bid lebih tebal.

    ``(bid - ask) / (bid + ask) * 100``. Bila kedua sisi kosong, hasilnya 0
    (tidak ada informasi), bukan pembagian nol.
    """
    bid_vol, ask_vol, _, _ = band_volume(snapshot, range_pct)
    total = bid_vol + ask_vol
    if total <= 0:
        return 0.0
    return (bid_vol - ask_vol) / total * 100.0


def detect_wall(
    snapshot: OrderBookSnapshot,
    side: Literal["bid", "ask"],
    *,
    range_pct: float,
    multiplier: float,
) -> Wall | None:
    """Cari "wall": satu level yang jauh lebih besar dari median level di sisinya.

    Ambang: ``qty >= multiplier * median(qty)`` pada level-level di dalam pita.
    Bila beberapa level memenuhi, dipilih yang **terdekat ke mid** karena itulah
    yang paling mungkin menghalangi harga.

    Mengembalikan ``None`` bila median 0 (semua level kosong) atau tidak ada
    level yang memenuhi ambang.
    """
    if multiplier <= 0:
        raise ValueError("multiplier harus > 0")
    mid = snapshot.mid
    levels = levels_within(snapshot.levels_for(side), mid, range_pct)
    if not levels:
        return None
    median_qty = median(level.qty for level in levels)
    if median_qty <= 0:
        return None
    threshold = multiplier * median_qty
    candidates = [level for level in levels if level.qty >= threshold]
    if not candidates:
        return None
    best = min(candidates, key=lambda level: abs(level.price - mid))
    distance_pct = abs(best.price - mid) / mid * 100.0 if mid > 0 else float("nan")
    return Wall(
        side=side,
        price=best.price,
        qty=best.qty,
        ratio_to_median=best.qty / median_qty,
        distance_pct=distance_pct,
    )


def estimate_fill(
    snapshot: OrderBookSnapshot,
    side: TradeSide,
    qty: float,
    *,
    tick_size: float = 0.0,
) -> FillEstimate:
    """Perkirakan harga rata-rata & slippage bila mengeksekusi ``qty`` dengan market.

    ``buy`` memakan level ask dari terendah; ``sell`` memakan level bid dari
    tertinggi. Slippage diukur terhadap **mid** snapshot, dalam persen dan tick.
    """
    if qty <= 0:
        raise ValueError("qty harus > 0")
    mid = snapshot.mid
    levels = snapshot.asks if side == "buy" else snapshot.bids
    remaining = qty
    filled = 0.0
    notional = 0.0
    used = 0
    for level in levels:
        if remaining <= 0:
            break
        take = min(remaining, level.qty)
        filled += take
        notional += take * level.price
        remaining -= take
        used += 1
    avg_price = notional / filled if filled > 0 else float("nan")
    if filled > 0 and mid > 0:
        if side == "buy":
            slippage_pct = (avg_price - mid) / mid * 100.0
        else:
            slippage_pct = (mid - avg_price) / mid * 100.0
    else:
        slippage_pct = float("inf")
    slippage_ticks = slippage_pct / 100.0 * mid / tick_size if tick_size > 0 else float("nan")
    return FillEstimate(
        side=side,
        requested_qty=qty,
        filled_qty=filled,
        notional=notional,
        avg_price=avg_price,
        reference_price=mid,
        slippage_pct=slippage_pct,
        slippage_ticks=slippage_ticks,
        levels_used=used,
        complete=remaining <= 1e-12,
    )


def limit_price_for_slippage(
    snapshot: OrderBookSnapshot,
    side: TradeSide,
    max_slippage_pct: float,
    *,
    tick_size: float = 0.0,
) -> float:
    """Harga batas (limit) terburuk yang masih dapat diterima untuk slippage tertentu.

    Dipakai TAHAP 5: order ``limit`` dengan harga ini tidak akan terisi lebih
    buruk dari toleransi slippage yang diizinkan.
    """
    mid = snapshot.mid
    if mid <= 0:
        return 0.0
    delta = mid * max_slippage_pct / 100.0
    raw = mid + delta if side == "buy" else mid - delta
    if tick_size > 0:
        steps = round(raw / tick_size)
        raw = steps * tick_size
    return raw


def analyze(
    snapshot: OrderBookSnapshot,
    *,
    direction: Literal["long", "short"],
    tick_size: float,
    target_qty: float,
    atr: float,
    band_pct: float,
    wall_multiplier: float,
    now: float,
) -> BookMetrics:
    """Hitung seluruh metrik buku dari sudut pandang satu arah trade.

    Untuk **long**: pasar harus menembus sisi **ask**, jadi wall yang "melawan"
    adalah wall di sisi ask. Untuk **short**: wall di sisi bid.
    """
    opposing: Literal["bid", "ask"] = "ask" if direction == "long" else "bid"
    trade_side: TradeSide = "buy" if direction == "long" else "sell"

    bid_vol, ask_vol, bid_levels, ask_levels = band_volume(snapshot, band_pct)
    wall = detect_wall(snapshot, opposing, range_pct=band_pct, multiplier=wall_multiplier)
    if wall is not None and atr > 0 and snapshot.mid > 0:
        distance_abs = abs(wall.price - snapshot.mid)
        wall = Wall(
            side=wall.side,
            price=wall.price,
            qty=wall.qty,
            ratio_to_median=wall.ratio_to_median,
            distance_pct=wall.distance_pct,
            distance_atr=distance_abs / atr,
        )

    fill = estimate_fill(snapshot, trade_side, target_qty, tick_size=tick_size)
    return BookMetrics(
        mid=snapshot.mid,
        spread_pct=snapshot.spread_pct,
        spread_ticks=snapshot.spread_ticks(tick_size),
        imbalance_pct=imbalance_pct(snapshot, band_pct),
        bid_volume=bid_vol,
        ask_volume=ask_vol,
        band_pct=band_pct,
        wall=wall,
        slippage_pct=fill.slippage_pct,
        slippage_ticks=fill.slippage_ticks,
        fill_complete=fill.complete,
        levels_used=fill.levels_used,
        age_sec=snapshot.age_sec(now),
        bid_levels=bid_levels,
        ask_levels=ask_levels,
    )
