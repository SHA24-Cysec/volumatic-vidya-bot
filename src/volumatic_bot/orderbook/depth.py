"""Sumber snapshot order book: REST (default) atau stream depth parsial.

Aturan hemat bandwidth yang dipegang:

1. Depth **hanya** diambil untuk simbol yang baru memberi sinyal.
2. Snapshot REST di-cache selama ``ttl`` detik; sinyal beruntun pada simbol yang
   sama tidak memicu permintaan baru (bobot depth = 2 untuk limit 5–50).
3. Mode stream (opsional) berlangganan ``<symbol>@depth<levels>@100ms`` hanya
   selama simbol itu menjadi kandidat, lalu berhenti berlangganan.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import aiohttp

from volumatic_bot.binance.depth_stream import DepthStreamClient
from volumatic_bot.binance.rest import BinanceREST
from volumatic_bot.metrics import Metrics
from volumatic_bot.orderbook.models import OrderBookSnapshot, parse_depth_event, parse_rest_depth

__all__ = [
    "DepthSource",
    "DepthUnavailable",
    "RestDepthSource",
    "StreamDepthSource",
]


class DepthUnavailable(RuntimeError):
    """Snapshot tidak tersedia (belum ada data, atau sudah terlalu tua)."""


@runtime_checkable
class DepthSource(Protocol):
    """Kontrak minimum sebuah sumber depth."""

    async def snapshot(self, symbol: str) -> OrderBookSnapshot:
        """Ambil snapshot terkini untuk ``symbol``."""


@dataclass(slots=True)
class RestDepthSource:
    """Snapshot via ``GET /fapi/v1/depth`` dengan cache TTL per simbol."""

    rest: BinanceREST
    limit: int = 20
    ttl_sec: float = 3.0
    # Jam dinding (bukan monotonic) supaya ``fetched_at`` sebanding dengan
    # parameter ``now`` di ``gate.evaluate`` dan timestamp REST Binance.
    clock: object = time.time
    requests: int = 0
    cache_hits: int = 0
    _cache: dict[str, OrderBookSnapshot] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.ttl_sec < 0:
            raise ValueError("ttl_sec tidak boleh negatif")

    def _now(self) -> float:
        return float(self.clock())  # type: ignore[operator]

    async def snapshot(self, symbol: str) -> OrderBookSnapshot:
        key = symbol.upper()
        now = self._now()
        cached = self._cache.get(key)
        if cached is not None and cached.age_sec(now) <= self.ttl_sec:
            self.cache_hits += 1
            return cached
        payload = await self.rest.depth(key, limit=self.limit)
        snapshot = parse_rest_depth(payload, key, fetched_at=now)
        self._cache[key] = snapshot
        self.requests += 1
        return snapshot

    def invalidate(self, symbol: str | None = None) -> None:
        """Buang cache satu simbol (atau semuanya)."""
        if symbol is None:
            self._cache.clear()
        else:
            self._cache.pop(symbol.upper(), None)

    @property
    def cached_symbols(self) -> list[str]:
        return sorted(self._cache)


@dataclass(slots=True)
class StreamDepthSource:
    """Snapshot dari stream depth parsial yang berlangganan sesuai kebutuhan."""

    client: DepthStreamClient
    max_age_sec: float = 3.0
    clock: object = time.time
    _latest: dict[str, OrderBookSnapshot] = field(default_factory=dict, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.client._on_depth = self.handle_event  # sambungkan callback

    @classmethod
    def build(
        cls,
        *,
        levels: int = 20,
        speed_ms: int = 100,
        base_url: str | None = None,
        testnet: bool = True,
        metrics: Metrics | None = None,
        max_age_sec: float = 3.0,
        clock: object = time.time,
        connect_factory: object | None = None,
    ) -> StreamDepthSource:
        client = DepthStreamClient(
            (),
            levels=levels,
            speed_ms=speed_ms,
            base_url=base_url,
            testnet=testnet,
            metrics=metrics,
            connect_factory=connect_factory,  # type: ignore[arg-type]  # injeksi uji
        )
        return cls(client=client, max_age_sec=max_age_sec, clock=clock)

    def _now(self) -> float:
        return float(self.clock())  # type: ignore[operator]

    async def handle_event(self, payload: dict[str, object]) -> None:
        """Callback dari klien stream; menyimpan snapshot terbaru per simbol."""
        try:
            snapshot = parse_depth_event(payload, fetched_at=self._now())
        except ValueError:
            return
        self._latest[snapshot.symbol] = snapshot

    async def subscribe(self, symbol: str) -> None:
        """Mulai berlangganan depth untuk satu simbol kandidat."""
        await self.client.set_symbols([*self.client.symbols, symbol.upper()])

    async def unsubscribe(self, symbol: str) -> None:
        symbol = symbol.upper()
        self._latest.pop(symbol, None)
        remaining = [s for s in self.client.symbols if s != symbol]
        await self.client.set_symbols(remaining)

    async def wait_first_event(
        self, symbol: str, *, max_wait_sec: float = 2.0, poll: float = 0.05
    ) -> None:
        """Tunggu sampai ada event depth pertama untuk ``symbol``."""
        deadline = self._now() + max_wait_sec
        while self._now() < deadline:
            if symbol.upper() in self._latest:
                return
            await asyncio.sleep(poll)
        raise DepthUnavailable(
            f"belum ada event depth untuk {symbol} dalam {max_wait_sec:.1f} detik"
        )

    async def snapshot(self, symbol: str) -> OrderBookSnapshot:
        key = symbol.upper()
        snapshot = self._latest.get(key)
        if snapshot is None:
            raise DepthUnavailable(f"belum ada snapshot depth untuk {key}")
        if snapshot.age_sec(self._now()) > self.max_age_sec:
            raise DepthUnavailable(
                f"snapshot depth {key} basi ({snapshot.age_sec(self._now()):.1f} detik)"
            )
        return snapshot

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.client.run())

    async def stop(self) -> None:
        self.client.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None


async def fetch_snapshot_safe(
    source: DepthSource, symbol: str
) -> tuple[OrderBookSnapshot | None, str | None]:
    """Ambil snapshot tanpa melempar: kembalikan ``(snapshot, alasan_gagal)``."""
    try:
        return await source.snapshot(symbol), None
    except DepthUnavailable as exc:
        return None, str(exc)
    except (aiohttp.ClientError, TimeoutError) as exc:
        return None, f"kegagalan jaringan saat mengambil depth: {type(exc).__name__}"
