"""Test umpan kline (WebSocket -> indikator -> sinyal) memakai data nyata.

Ini uji integrasi paling penting di TAHAP 2: memastikan sinyal yang dipancarkan
saat bar demi bar mengalir **sama persis** dengan sinyal hasil hitungan penuh
atas data historis. Kalau berbeda, ada lookahead atau salah penanganan warm-up.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from volumatic_bot.binance.kline_messages import KlineEvent
from volumatic_bot.indicator import IndicatorParams, compute, entry_signal
from volumatic_bot.models import CandleSeries
from volumatic_bot.streams.kline_feed import KlineFeed

SEED_BARS = 1100
WINDOW_BARS = 1300
PARAMS = IndicatorParams()


@pytest.fixture(scope="module")
def window(real_candles: CandleSeries) -> CandleSeries:
    return real_candles.tail(WINDOW_BARS)


@pytest.fixture(scope="module")
def full(window: CandleSeries):
    return compute(window, PARAMS)


@pytest.fixture(scope="module")
def flip_indexes(window: CandleSeries, full) -> list[tuple[int, str]]:
    """Indeks (di dalam jendela uji) yang menghasilkan sinyal setelah SEED_BARS."""
    out = [
        (i, direction)
        for i in range(SEED_BARS, len(window))
        if (direction := entry_signal(full, i)) is not None
    ]
    assert out, "data uji harus punya sinyal di rentang uji"
    return out


def event_from(series: CandleSeries, index: int, *, closed: bool = True) -> KlineEvent:
    return KlineEvent(
        symbol="BTCUSDT",
        interval="15m",
        open_time=int(series.open_time[index]),
        close_time=int(series.open_time[index]) + 899_999,
        open=float(series.open[index]),
        high=float(series.high[index]),
        low=float(series.low[index]),
        close=float(series.close[index]),
        volume=float(series.volume[index]),
        quote_volume=1000.0,
        is_closed=closed,
        event_time=int(series.open_time[index]) + 900_000,
    )


async def feed_upto(feed: KlineFeed, series: CandleSeries, stop_index: int) -> None:
    for i in range(len(feed.series("BTCUSDT")), stop_index + 1):
        await feed.handle_event(event_from(series, i))


@pytest.mark.asyncio
async def test_sinyal_sama_dengan_hitungan_penuh(window: CandleSeries, full) -> None:
    signals: list = []
    feed = KlineFeed(PARAMS, max_bars=len(window), on_signal=signals.append, offload=False)
    feed.seed("BTCUSDT", window.head(SEED_BARS))
    await feed_upto(feed, window, len(window) - 1)

    expected = [
        (int(full.open_time[i]), direction)
        for i in range(SEED_BARS, len(window))
        if (direction := entry_signal(full, i)) is not None
    ]
    got = [(s.open_time, s.direction) for s in signals]
    assert got == expected


@pytest.mark.asyncio
async def test_nilai_sinyal_cocok_dengan_indikator(
    window: CandleSeries, full, flip_indexes: list[tuple[int, str]]
) -> None:
    signals: list = []
    feed = KlineFeed(PARAMS, max_bars=len(window), on_signal=signals.append, offload=False)
    feed.seed("BTCUSDT", window.head(SEED_BARS))
    first_flip = flip_indexes[0][0]
    await feed_upto(feed, window, first_flip)

    assert len(signals) == 1
    signal = signals[0]
    idx = first_flip
    assert signal.close_price == pytest.approx(float(window.close[idx]))
    assert signal.atr == pytest.approx(float(full.atr[idx]), rel=1e-12)
    assert signal.upper == pytest.approx(float(full.upper[idx]), rel=1e-12)
    assert signal.lower == pytest.approx(float(full.lower[idx]), rel=1e-12)
    assert signal.cmo == pytest.approx(float(full.cmo[idx]), rel=1e-12)
    assert signal.trend == int(full.trend[idx])
    assert signal.vol_delta_win_pct == pytest.approx(float(full.vol_delta_win_pct[idx]))
    assert signal.atr_pct == pytest.approx(
        float(full.atr[idx]) / float(window.close[idx]) * 100.0, rel=1e-12
    )


@pytest.mark.asyncio
async def test_candle_belum_tertutup_tidak_diproses(window: CandleSeries) -> None:
    feed = KlineFeed(PARAMS, max_bars=len(window), offload=False)
    feed.seed("BTCUSDT", window.head(SEED_BARS))
    result = await feed.handle_event(event_from(window, SEED_BARS, closed=False))
    assert result is None
    assert feed.ignored_unclosed == 1
    assert feed.processed == 0
    assert feed.bars("BTCUSDT") == SEED_BARS


@pytest.mark.asyncio
async def test_event_duplikat_tidak_menghasilkan_sinyal_ganda(
    window: CandleSeries, flip_indexes: list[tuple[int, str]]
) -> None:
    flip = flip_indexes[0][0]
    feed = KlineFeed(PARAMS, max_bars=len(window), offload=False)
    feed.seed("BTCUSDT", window.head(SEED_BARS))
    await feed_upto(feed, window, flip)
    assert feed.processed == flip - SEED_BARS + 1
    # Event yang sama dikirim ulang: tidak diproses lagi, tidak ada sinyal kedua.
    again = await feed.handle_event(event_from(window, flip))
    assert again is None
    assert feed.processed == flip - SEED_BARS + 1


@pytest.mark.asyncio
async def test_event_usang_diabaikan(window: CandleSeries) -> None:
    feed = KlineFeed(PARAMS, max_bars=len(window), offload=False)
    feed.seed("BTCUSDT", window.head(SEED_BARS))
    await feed.handle_event(event_from(window, SEED_BARS))
    stale = await feed.handle_event(event_from(window, SEED_BARS - 50))
    assert stale is None
    assert feed.bars("BTCUSDT") == SEED_BARS + 1


@pytest.mark.asyncio
async def test_simbol_tak_dikenal_diabaikan(window: CandleSeries) -> None:
    feed = KlineFeed(PARAMS, max_bars=len(window), offload=False)
    event = replace(event_from(window, SEED_BARS), symbol="TIDAKADA")
    assert await feed.handle_event(event) is None
    assert feed.processed == 0


@pytest.mark.asyncio
async def test_offload_thread_tidak_mengubah_hasil(window: CandleSeries) -> None:
    results = {}
    for offload in (False, True):
        signals: list = []
        feed = KlineFeed(PARAMS, max_bars=len(window), on_signal=signals.append, offload=offload)
        feed.seed("BTCUSDT", window.head(SEED_BARS))
        await feed_upto(feed, window, SEED_BARS + 120)
        results[offload] = [(s.open_time, s.direction) for s in signals]
    assert results[False] == results[True]


@pytest.mark.asyncio
async def test_riwayat_dipotong_sesuai_max_bars(window: CandleSeries) -> None:
    max_bars = SEED_BARS + 50
    feed = KlineFeed(PARAMS, max_bars=max_bars, offload=True)
    feed.seed("BTCUSDT", window.head(SEED_BARS))
    await feed_upto(feed, window, SEED_BARS + 120)
    assert feed.bars("BTCUSDT") == max_bars
    series = feed.series("BTCUSDT")
    assert int(series.open_time[-1]) == int(window.open_time[SEED_BARS + 120])
    assert int(series.open_time[0]) == int(window.open_time[SEED_BARS + 121 - max_bars])


@pytest.mark.asyncio
async def test_skor_diteruskan_ke_sinyal(
    window: CandleSeries, flip_indexes: list[tuple[int, str]]
) -> None:
    signals: list = []
    feed = KlineFeed(
        PARAMS,
        max_bars=len(window),
        on_signal=signals.append,
        offload=False,
        scores={"BTCUSDT": 0.87},
    )
    feed.seed("BTCUSDT", window.head(SEED_BARS))
    await feed_upto(feed, window, flip_indexes[0][0])
    assert signals and all(s.score == pytest.approx(0.87) for s in signals)


@pytest.mark.asyncio
async def test_callback_async_didukung(
    window: CandleSeries, flip_indexes: list[tuple[int, str]]
) -> None:
    seen: list[int] = []

    async def on_signal(signal) -> None:  # type: ignore[no-untyped-def]
        seen.append(signal.open_time)

    flip = flip_indexes[0][0]
    feed = KlineFeed(PARAMS, max_bars=len(window), on_signal=on_signal, offload=False)
    feed.seed("BTCUSDT", window.head(SEED_BARS))
    await feed_upto(feed, window, flip)
    assert seen == [int(window.open_time[flip])]


def test_series_kosong_untuk_simbol_belum_diseed() -> None:
    feed = KlineFeed(PARAMS)
    assert len(feed.series("BTCUSDT")) == 0
    assert feed.bars("BTCUSDT") == 0
