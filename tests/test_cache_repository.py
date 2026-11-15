"""Test cache SQLite + penyedia riwayat (KlineRepository).

REST dipalsukan dengan kelas yang meniru ``BinanceREST.klines`` dan mencatat
setiap panggilan, sehingga bisa dibuktikan bahwa restart **tidak** mengunduh ulang.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from volumatic_bot.binance.urls import interval_to_ms
from volumatic_bot.data.cache import KlineCache
from volumatic_bot.data.repository import KlineRepository, latest_closed_open_time
from volumatic_bot.storage.db import sqlite_runtime_version

INTERVAL = "15m"
INTERVAL_MS = interval_to_ms(INTERVAL)


def _rest_rows(start_open_time: int, count: int) -> list[list[Any]]:
    """Baris tiruan berbentuk respons ``/fapi/v1/klines``."""
    rows = []
    for i in range(count):
        t = start_open_time + i * INTERVAL_MS
        price = 100.0 + i * 0.5
        rows.append(
            [
                t,
                f"{price}",
                f"{price + 1}",
                f"{price - 1}",
                f"{price + 0.2}",
                "10.0",
                t + INTERVAL_MS - 1,
                "1000.0",
                5,
                "4.0",
                "400.0",
                "0",
            ]
        )
    return rows


class FakeREST:
    """Meniru ``BinanceREST`` hanya untuk ``klines``."""

    def __init__(self, all_rows: list[list[Any]]) -> None:
        self.all_rows = all_rows
        self.calls: list[dict[str, Any]] = []

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        self.calls.append({"symbol": symbol, "start": start_time, "end": end_time, "limit": limit})
        selected = [
            row
            for row in self.all_rows
            if (start_time is None or row[0] >= start_time)
            and (end_time is None or row[0] <= end_time)
        ]
        return selected[:limit]


@pytest.fixture()
def history_rows() -> list[list[Any]]:
    start = 1_764_000_000_000 // INTERVAL_MS * INTERVAL_MS
    return _rest_rows(start, 4000)


@pytest.fixture()
def now_ms(history_rows: list[list[Any]]) -> int:
    """Waktu "sekarang" = candle berjalan setelah 2000 bar pertama."""
    return history_rows[2000][0] + INTERVAL_MS + 60_000


@pytest.mark.asyncio
async def test_cache_upsert_dan_load(tmp_path: Path) -> None:
    cache = KlineCache(tmp_path / "c.db")
    await cache.open()
    try:
        rows = [(t, 1.0, 2.0, 0.5, 1.5, 10.0) for t in (3000, 1000, 2000)]
        written = await cache.upsert("BTCUSDT", INTERVAL, rows)
        assert written == 3
        loaded = await cache.load("BTCUSDT", INTERVAL)
        assert [r[0] for r in loaded] == [1000, 2000, 3000]  # terurut naik
        # Upsert ulang tidak menggandakan baris.
        await cache.upsert("BTCUSDT", INTERVAL, [(2000, 9.0, 9.0, 9.0, 9.0, 1.0)])
        loaded = await cache.load("BTCUSDT", INTERVAL)
        assert len(loaded) == 3
        assert loaded[1][1] == 9.0
        stats = await cache.stats()
        assert stats.rows == 3 and stats.symbols == 1
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_cache_limit_mengambil_terakhir(tmp_path: Path) -> None:
    cache = KlineCache(tmp_path / "c.db")
    await cache.open()
    try:
        rows = [(t, 1.0, 2.0, 0.5, 1.5, 10.0) for t in range(0, 1000, 100)]
        await cache.upsert("BTCUSDT", INTERVAL, rows)
        loaded = await cache.load("BTCUSDT", INTERVAL, limit=3)
        assert [r[0] for r in loaded] == [700, 800, 900]
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_repository_cold_start_mengunduh_lalu_memakai_cache(
    tmp_path: Path, history_rows: list[list[Any]], now_ms: int
) -> None:
    rest = FakeREST(history_rows)
    cache = KlineCache(tmp_path / "r.db")
    await cache.open()
    try:
        repo = KlineRepository(
            rest, cache, INTERVAL, warmup_bars=500, kline_rest_limit=1000, now_ms=lambda: now_ms
        )
        first = await repo.history("BTCUSDT", 500)
        assert first.total >= 400
        assert first.requests >= 1
        assert rest.calls, "cold start harus memanggil REST"
        # Candle berjalan (belum tertutup) tidak boleh ikut.
        last_closed = latest_closed_open_time(now_ms, INTERVAL_MS)
        assert int(first.series.open_time[-1]) <= last_closed

        calls_before = len(rest.calls)
        second = await repo.history("BTCUSDT", 500)
        assert len(rest.calls) == calls_before, "cache harus dipakai, tanpa REST baru"
        assert second.requests == 0
        assert second.from_rest == 0
        assert list(second.series.close) == list(first.series.close)
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_repository_forward_fill_hanya_bar_baru(
    tmp_path: Path, history_rows: list[list[Any]], now_ms: int
) -> None:
    rest = FakeREST(history_rows)
    cache = KlineCache(tmp_path / "f.db")
    await cache.open()
    try:
        repo = KlineRepository(rest, cache, INTERVAL, warmup_bars=200, now_ms=lambda: now_ms)
        await repo.history("BTCUSDT", 200)
        calls_before = len(rest.calls)

        # Waktu maju 3 candle -> hanya 3 bar baru yang perlu diunduh.
        later = now_ms + 3 * INTERVAL_MS
        repo._now_ms = lambda: later  # type: ignore[assignment]
        result = await repo.history("BTCUSDT", 200)
        new_calls = rest.calls[calls_before:]
        assert new_calls, "harus ada unduhan untuk bar baru"
        assert all(call["limit"] <= 4 for call in new_calls), "jangan unduh ulang 200 bar"
        assert result.from_rest == 3
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_repository_backfill_saat_cache_terlalu_pendek(
    tmp_path: Path, history_rows: list[list[Any]], now_ms: int
) -> None:
    rest = FakeREST(history_rows)
    cache = KlineCache(tmp_path / "b.db")
    await cache.open()
    try:
        repo = KlineRepository(rest, cache, INTERVAL, warmup_bars=100, now_ms=lambda: now_ms)
        short = await repo.history("BTCUSDT", 100)
        assert short.total <= 100
        repo.warmup_bars = 500
        longer = await repo.history("BTCUSDT", 500)
        assert longer.total > short.total
        assert any(call["end"] is not None for call in rest.calls[1:])
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_repository_paginasi_menghormati_limit_per_request(
    tmp_path: Path, history_rows: list[list[Any]], now_ms: int
) -> None:
    rest = FakeREST(history_rows)
    cache = KlineCache(tmp_path / "p.db")
    await cache.open()
    try:
        repo = KlineRepository(
            rest, cache, INTERVAL, warmup_bars=3000, kline_rest_limit=1000, now_ms=lambda: now_ms
        )
        result = await repo.history("BTCUSDT", 2000)
        assert all(call["limit"] <= 1000 for call in rest.calls)
        assert result.requests >= 2
        assert result.total >= 1500
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_repository_tanpa_cache_tetap_jalan(
    history_rows: list[list[Any]], now_ms: int
) -> None:
    rest = FakeREST(history_rows)
    cache = KlineCache(":memory:", enabled=False)
    repo = KlineRepository(rest, cache, INTERVAL, warmup_bars=300, now_ms=lambda: now_ms)
    result = await repo.history("BTCUSDT", 300)
    assert result.total == 300
    assert result.from_rest == 300
    assert result.from_cache == 0


@pytest.mark.asyncio
async def test_force_refresh_mengunduh_ulang(
    tmp_path: Path, history_rows: list[list[Any]], now_ms: int
) -> None:
    rest = FakeREST(history_rows)
    cache = KlineCache(tmp_path / "x.db")
    await cache.open()
    try:
        repo = KlineRepository(rest, cache, INTERVAL, warmup_bars=100, now_ms=lambda: now_ms)
        await repo.history("BTCUSDT", 100)
        calls_before = len(rest.calls)
        await repo.history("BTCUSDT", 100, force_refresh=True)
        assert len(rest.calls) > calls_before
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_prune_membatasi_ukuran_cache(tmp_path: Path) -> None:
    cache = KlineCache(tmp_path / "pr.db")
    await cache.open()
    try:
        for i, symbol in enumerate(["AAAUSDT", "BBBUSDT", "CCCUSDT"]):
            await cache.upsert(symbol, INTERVAL, [(1000 + i, 1.0, 1.0, 1.0, 1.0, 1.0)])
        removed = await cache.prune(keep_symbols=1, interval=INTERVAL)
        assert removed == 2
        stats = await cache.stats()
        assert stats.symbols == 1
    finally:
        await cache.close()


def test_versi_sqlite_runtime_tersedia() -> None:
    # Python 3.14 menghapus sqlite3.version; yang dipakai harus sqlite_version.
    assert sqlite_runtime_version()[0].isdigit()


def test_latest_closed_open_time() -> None:
    assert latest_closed_open_time(INTERVAL_MS * 10 + 500, INTERVAL_MS) == INTERVAL_MS * 9
    assert latest_closed_open_time(INTERVAL_MS * 10, INTERVAL_MS) == INTERVAL_MS * 9
