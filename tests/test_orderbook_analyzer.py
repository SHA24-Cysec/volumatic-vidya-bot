"""Test analisis order book: parsing, spread, imbalance, wall, slippage."""

from __future__ import annotations

import math

import pytest

from volumatic_bot.orderbook.analyzer import (
    analyze,
    band_volume,
    detect_wall,
    estimate_fill,
    imbalance_pct,
    levels_within,
    limit_price_for_slippage,
)
from volumatic_bot.orderbook.models import (
    BookLevel,
    OrderBookSnapshot,
    parse_depth_event,
    parse_rest_depth,
)

TICK = 0.1


def make_book(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    fetched_at: float = 100.0,
    symbol: str = "BTCUSDT",
    source: str = "rest",
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol=symbol,
        bids=tuple(sorted((BookLevel(p, q) for p, q in bids), key=lambda lv: -lv.price)),
        asks=tuple(sorted((BookLevel(p, q) for p, q in asks), key=lambda lv: lv.price)),
        last_update_id=1,
        event_time_ms=1_700_000_000_000,
        source=source,  # type: ignore[arg-type]
        fetched_at=fetched_at,
    )


# ------------------------------------------------------------------ parsing
def test_parse_rest_depth_membuang_qty_nol_dan_mengurutkan() -> None:
    payload = {
        "lastUpdateId": 1027024,
        "E": 1589436922972,
        "T": 1589436922959,
        "bids": [["99.5", "2"], ["100.0", "1.5"], ["99.0", "0"]],
        "asks": [["100.3", "1"], ["100.1", "2.5"], ["100.2", "0"]],
    }
    snap = parse_rest_depth(payload, "btcusdt", fetched_at=0.0)
    assert snap.symbol == "BTCUSDT"
    assert snap.source == "rest"
    assert [lv.price for lv in snap.bids] == [100.0, 99.5]  # qty 0 dibuang, urut turun
    assert [lv.price for lv in snap.asks] == [100.1, 100.3]  # urut naik
    assert snap.last_update_id == 1027024
    assert snap.best_bid == 100.0 and snap.best_ask == 100.1
    assert snap.mid == pytest.approx(100.05)


def test_parse_rest_depth_payload_rusak() -> None:
    with pytest.raises(ValueError, match="tidak valid"):
        parse_rest_depth({"bids": []}, "BTCUSDT", fetched_at=0.0)


def test_parse_depth_event_stream() -> None:
    payload = {
        "stream": "btcusdt@depth20@100ms",
        "data": {
            "e": "depthUpdate",
            "E": 1571889248277,
            "T": 1571889248276,
            "s": "BTCUSDT",
            "U": 390497796,
            "u": 390497878,
            "pu": 390497794,
            "b": [["7403.89", "0.002"], ["7403.90", "3.906"], ["7404.00", "0"]],
            "a": [["7405.96", "3.340"], ["7406.63", "4.525"]],
        },
    }
    snap = parse_depth_event(payload, fetched_at=5.0)
    assert snap.source == "stream"
    assert snap.best_bid == 7403.9
    assert snap.best_ask == 7405.96
    assert snap.last_update_id == 390497878
    assert len(snap.bids) == 2  # qty 0 dibuang


def test_parse_depth_event_bukan_depth_update() -> None:
    with pytest.raises(ValueError, match="depthUpdate"):
        parse_depth_event({"e": "kline"}, fetched_at=0.0)


# ------------------------------------------------------------- spread & buku
def test_spread_persen_dan_tick() -> None:
    book = make_book([(100.0, 1.0)], [(100.1, 1.0)])
    assert book.spread_pct == pytest.approx(0.1 / 100.05 * 100, rel=1e-12)
    assert book.spread_ticks(TICK) == pytest.approx(1.0)
    assert book.spread_ticks(0.0) != book.spread_ticks(0.0)  # NaN bila tick 0


def test_buku_kosong_atau_bersilangan_tidak_terpakai() -> None:
    assert not make_book([], [(100.0, 1.0)]).is_usable()
    assert not make_book([(101.0, 1.0)], [(100.0, 1.0)]).is_usable()  # crossed
    assert make_book([(99.9, 1.0)], [(100.1, 1.0)]).is_usable()


def test_age_dan_levels_for() -> None:
    book = make_book([(99.9, 1.0)], [(100.1, 1.0)], fetched_at=100.0)
    assert book.age_sec(103.5) == pytest.approx(3.5)
    assert book.age_sec(90.0) == 0.0  # jam mundur tidak menghasilkan negatif
    assert len(book.levels_for("bid")) == 1
    assert len(book) == 2


# ---------------------------------------------------------------- imbalance
def test_levels_within_pita() -> None:
    book = make_book([(99.9, 1.0), (98.0, 5.0)], [(100.2, 1.0), (103.0, 5.0)])
    within = levels_within(book.bids, book.mid, 0.3)
    assert [lv.price for lv in within] == [99.9]


def test_imbalance_positif_saat_bid_tebal() -> None:
    book = make_book([(99.95, 3.0), (99.90, 1.0)], [(100.05, 1.0), (100.10, 1.0)])
    value = imbalance_pct(book, 0.3)
    assert value == pytest.approx((4.0 - 2.0) / 6.0 * 100.0, rel=1e-12)
    assert imbalance_pct(make_book([(99.9, 1.0)], [(100.1, 3.0)]), 0.3) == pytest.approx(-50.0)


def test_imbalance_nol_saat_tidak_ada_level_relevan() -> None:
    book = make_book([(90.0, 5.0)], [(110.0, 5.0)])  # jauh di luar pita 0.3%
    bid_vol, ask_vol, n_bid, n_ask = band_volume(book, 0.3)
    assert (bid_vol, ask_vol, n_bid, n_ask) == (0.0, 0.0, 0, 0)
    assert imbalance_pct(book, 0.3) == 0.0  # bukan ZeroDivisionError


# --------------------------------------------------------------------- wall
def test_detect_wall_menemukan_level_terbesar() -> None:
    asks = [(100.1, 1.0), (100.15, 1.2), (100.2, 20.0), (100.25, 0.9)]
    book = make_book([(99.9, 1.0)], asks)
    wall = detect_wall(book, "ask", range_pct=0.5, multiplier=8.0)
    assert wall is not None
    assert wall.price == pytest.approx(100.2)
    assert wall.qty == pytest.approx(20.0)
    assert wall.ratio_to_median > 8.0
    assert wall.distance_pct == pytest.approx(abs(100.2 - book.mid) / book.mid * 100)


def test_detect_wall_tidak_ada_bila_merata() -> None:
    book = make_book([(99.9, 1.0)], [(100.1, 1.0), (100.2, 1.1), (100.3, 0.9)])
    assert detect_wall(book, "ask", range_pct=0.5, multiplier=8.0) is None


def test_detect_wall_validasi_multiplier() -> None:
    book = make_book([(99.9, 1.0)], [(100.1, 1.0)])
    with pytest.raises(ValueError, match="multiplier"):
        detect_wall(book, "ask", range_pct=0.5, multiplier=0)


def test_detect_wall_sisi_kosong_atau_tanpa_level_relevan() -> None:
    # Sisi bid kosong -> tidak ada yang bisa dibandingkan.
    book = make_book([], [(100.1, 1.0)])
    assert detect_wall(book, "bid", range_pct=0.5, multiplier=8.0) is None
    # Satu-satunya level ask tidak bisa disebut "jauh di atas median" dirinya sendiri.
    assert detect_wall(book, "ask", range_pct=0.5, multiplier=8.0) is None
    # Semua level di luar pita -> tidak ada kandidat.
    jauh = make_book([(50.0, 10.0)], [(200.0, 10.0)])
    assert detect_wall(jauh, "ask", range_pct=0.5, multiplier=2.0) is None


# ----------------------------------------------------------------- slippage
def test_estimasi_fill_beli_melewati_beberapa_level() -> None:
    book = make_book([(99.9, 5.0)], [(100.1, 1.0), (100.2, 1.0), (100.3, 5.0)])
    fill = estimate_fill(book, "buy", 2.5, tick_size=TICK)
    assert fill.complete
    assert fill.levels_used == 3
    expected_avg = (1.0 * 100.1 + 1.0 * 100.2 + 0.5 * 100.3) / 2.5
    assert fill.avg_price == pytest.approx(expected_avg)
    assert fill.slippage_pct > 0
    assert fill.slippage_pct == pytest.approx((expected_avg - book.mid) / book.mid * 100)


def test_estimasi_fill_jual_memakai_sisi_bid() -> None:
    book = make_book([(99.9, 1.0), (99.8, 5.0)], [(100.1, 1.0)])
    fill = estimate_fill(book, "sell", 2.0, tick_size=TICK)
    assert fill.avg_price < book.mid
    assert fill.slippage_pct > 0  # jual di bawah mid = slippage


def test_estimasi_fill_likuiditas_tidak_cukup() -> None:
    book = make_book([(99.9, 1.0)], [(100.1, 0.5)])
    fill = estimate_fill(book, "buy", 5.0, tick_size=TICK)
    assert not fill.complete
    assert fill.filled_qty == pytest.approx(0.5)
    assert fill.unfilled_qty == pytest.approx(4.5)


def test_estimasi_fill_qty_tidak_valid() -> None:
    book = make_book([(99.9, 1.0)], [(100.1, 1.0)])
    with pytest.raises(ValueError, match="qty"):
        estimate_fill(book, "buy", 0.0)


def test_limit_price_untuk_batas_slippage() -> None:
    book = make_book([(99.9, 1.0)], [(100.1, 1.0)])
    price = limit_price_for_slippage(book, "buy", 0.1, tick_size=TICK)
    assert price == pytest.approx(100.1)  # mid 100.05 + 0.1% -> 100.15 -> 1 tick 100.1/100.2
    sell = limit_price_for_slippage(book, "sell", 0.1, tick_size=TICK)
    assert sell < book.mid


# ------------------------------------------------------------------ analyze
def test_analyze_arah_long_memakai_sisi_ask() -> None:
    book = make_book(
        [(99.95, 1.0)],
        [(100.05, 1.0), (100.10, 1.0), (100.15, 20.0)],
        fetched_at=10.0,
    )
    metrics = analyze(
        book,
        direction="long",
        tick_size=TICK,
        target_qty=1.0,
        atr=0.5,
        band_pct=0.5,
        wall_multiplier=8.0,
        now=11.0,
    )
    assert metrics.wall is not None
    assert metrics.wall.side == "ask"
    assert metrics.wall.price == pytest.approx(100.15)
    # jarak 100.15 - mid(100.0) = 0.15 -> 0.15 / ATR 0.5 = 0.3 ATR
    assert metrics.wall.distance_atr == pytest.approx(0.3, rel=1e-9)
    assert metrics.age_sec == pytest.approx(1.0)
    assert metrics.fill_complete


def test_analyze_arah_short_memakai_sisi_bid() -> None:
    book = make_book([(99.90, 20.0), (99.85, 1.0), (99.80, 1.0)], [(100.05, 1.0)], fetched_at=10.0)
    metrics = analyze(
        book,
        direction="short",
        tick_size=TICK,
        target_qty=1.0,
        atr=0.5,
        band_pct=0.5,
        wall_multiplier=8.0,
        now=10.0,
    )
    assert metrics.wall is not None
    assert metrics.wall.side == "bid"
    assert metrics.slippage_pct >= 0  # jual menghabiskan bid
    assert math.isfinite(metrics.spread_pct)
