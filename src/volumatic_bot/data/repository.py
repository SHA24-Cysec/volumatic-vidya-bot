"""Penyedia riwayat candle: cache SQLite + REST paginasi.

Alur kerja (hemat bandwidth):

1. Hitung jendela yang dibutuhkan: ``bars`` candle terakhir yang **sudah tertutup**.
2. Bandingkan dengan isi cache.
3. Unduh hanya bagian yang belum ada:
   * *back-fill* untuk bagian lama (bila cache belum cukup panjang),
   * *forward-fill* untuk bar baru sejak cache terakhir.
4. Simpan hasil unduhan ke cache, lalu layani dari cache.

Dengan cara ini restart berikutnya **tidak mengunduh ulang** warm-up 1500 candle.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from volumatic_bot.binance.kline_messages import parse_rest_kline
from volumatic_bot.binance.rest import BinanceREST
from volumatic_bot.binance.urls import interval_to_ms
from volumatic_bot.data.cache import KlineCache
from volumatic_bot.models import CandleSeries

__all__ = ["HistoryResult", "KlineRepository", "latest_closed_open_time"]

KlineRowTuple = tuple[int, float, float, float, float, float]


def latest_closed_open_time(now_ms: int, interval_ms: int) -> int:
    """``open_time`` candle terakhir yang sudah tertutup pada waktu ``now_ms``."""
    return (now_ms // interval_ms) * interval_ms - interval_ms


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """Hasil penyediaan riwayat untuk satu simbol."""

    symbol: str
    series: CandleSeries
    from_cache: int
    from_rest: int
    requests: int

    @property
    def total(self) -> int:
        return len(self.series)


class KlineRepository:
    """Ambil riwayat candle dari cache dan/atau REST."""

    def __init__(
        self,
        rest: BinanceREST,
        cache: KlineCache,
        interval: str,
        *,
        warmup_bars: int = 1500,
        kline_rest_limit: int = 1500,
        now_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], object] | None = None,
        max_pages: int = 40,
        pace_sec: float = 0.05,
    ) -> None:
        if not 100 <= kline_rest_limit <= 1500:
            raise ValueError("kline_rest_limit harus 100..1500")
        self.rest = rest
        self.cache = cache
        self.interval = interval
        self.interval_ms = interval_to_ms(interval)
        self.warmup_bars = warmup_bars
        self.kline_rest_limit = kline_rest_limit
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._sleep = sleep or asyncio.sleep
        self.max_pages = max_pages
        self.pace_sec = pace_sec

    # ------------------------------------------------------------------ publik
    async def history(
        self, symbol: str, bars: int | None = None, *, force_refresh: bool = False
    ) -> HistoryResult:
        """Riwayat ``bars`` candle tertutup terakhir untuk ``symbol``."""
        symbol = symbol.upper()
        wanted = bars or self.warmup_bars
        if wanted < 1:
            raise ValueError("bars harus >= 1")

        now = self._now_ms()
        last_closed = latest_closed_open_time(now, self.interval_ms)
        target_start = last_closed - (wanted - 1) * self.interval_ms

        fetched: list[KlineRowTuple] = []
        requests = 0

        if not force_refresh and self.cache.enabled:
            first = await self.cache.first_open_time(symbol, self.interval)
            last = await self.cache.last_open_time(symbol, self.interval)
            if first is None or last is None:
                # Cache kosong: unduh seluruh jendela sekali.
                rows, req = await self._fetch_range(symbol, target_start, last_closed)
                requests += req
                fetched.extend(rows)
                await self.cache.upsert(symbol, self.interval, rows)
            else:
                if first > target_start:
                    start = target_start
                    end = min(first - self.interval_ms, last_closed)
                    if start <= end:
                        rows, req = await self._fetch_range(symbol, start, end)
                        requests += req
                        fetched.extend(rows)
                        await self.cache.upsert(symbol, self.interval, rows)
                if last < last_closed:
                    start = last + self.interval_ms
                    if start <= last_closed:
                        rows, req = await self._fetch_range(symbol, start, last_closed)
                        requests += req
                        fetched.extend(rows)
                        await self.cache.upsert(symbol, self.interval, rows)
        else:
            rows, req = await self._fetch_range(symbol, target_start, last_closed)
            requests += req
            fetched.extend(rows)
            if self.cache.enabled:
                await self.cache.upsert(symbol, self.interval, rows)

        series = (
            await self.cache.load_series(
                symbol,
                self.interval,
                limit=wanted,
                drop_unclosed_after=last_closed + 1,
            )
            if self.cache.enabled
            else CandleSeries.from_columns([], [], [], [], [], [])
        )
        if len(series) == 0:
            # Cache mati/kosong: layani langsung dari hasil unduhan.
            if not fetched:
                fetched, req = await self._fetch_range(symbol, target_start, last_closed)
                requests += req
            series = _rows_to_series(fetched, drop_after=last_closed)
            return HistoryResult(symbol, series, 0, len(series), requests)

        if len(series) > wanted:
            series = series.tail(wanted)
        return HistoryResult(
            symbol=symbol,
            series=series,
            from_cache=max(0, len(series) - len(fetched)),
            from_rest=len(fetched),
            requests=requests,
        )

    async def sync_recent(self, symbol: str, *, lookback: int = 3) -> list[KlineRowTuple]:
        """Ambil ``lookback`` candle terakhir (untuk menyusul setelah cache basi)."""
        now = self._now_ms()
        last_closed = latest_closed_open_time(now, self.interval_ms)
        start = last_closed - (lookback - 1) * self.interval_ms
        rows, _ = await self._fetch_range(symbol.upper(), start, last_closed)
        if rows and self.cache.enabled:
            await self.cache.upsert(symbol.upper(), self.interval, rows)
        return rows

    # ------------------------------------------------------------------ internal
    async def _fetch_range(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> tuple[list[KlineRowTuple], int]:
        """Unduh candle pada rentang ``[start_ms, end_ms]`` dengan paginasi."""
        out: list[KlineRowTuple] = []
        requests = 0
        start = start_ms
        for _ in range(self.max_pages):
            if start > end_ms:
                break
            remaining = (end_ms - start) // self.interval_ms + 1
            limit = int(min(self.kline_rest_limit, max(1, remaining)))
            raw = await self.rest.klines(
                symbol, self.interval, start_time=start, end_time=end_ms, limit=limit
            )
            requests += 1
            if not raw:
                break
            parsed = [parse_rest_kline(item) for item in raw]
            out.extend(row.as_tuple() for row in parsed)
            if len(parsed) < limit:
                break
            start = parsed[-1].open_time + self.interval_ms
            if self.pace_sec > 0:
                result = self._sleep(self.pace_sec)
                if asyncio.iscoroutine(result):
                    await result
        return out, requests


def _rows_to_series(rows: list[KlineRowTuple], *, drop_after: int) -> CandleSeries:
    """Konversi baris menjadi ``CandleSeries`` (hanya candle tertutup)."""
    filtered = [r for r in rows if r[0] <= drop_after]
    if not filtered:
        return CandleSeries.from_columns([], [], [], [], [], [])
    return CandleSeries.from_columns(
        open_time=[r[0] for r in filtered],
        open=[r[1] for r in filtered],
        high=[r[2] for r in filtered],
        low=[r[3] for r in filtered],
        close=[r[4] for r in filtered],
        volume=[r[5] for r in filtered],
    )
