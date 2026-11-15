"""Basis klien WebSocket *combined stream* untuk Binance USDⓈ-M Futures.

Semua stream pasar Binance memakai pola yang sama: satu koneksi, banyak stream,
format ``{"stream": ..., "data": {...}}``, batas 24 jam, ping server tiap 3
menit, maksimal 1024 stream dan 10 pesan masuk per detik. Kelas ini memusatkan
logika tersebut supaya klien kline dan klien depth tidak menduplikasi kode
penanganan koneksi.

Dokumentasi yang dirujuk (snapshot arsip 2026):
* ``websocket-market-streams/Connect``
* ``websocket-market-streams/Kline-Candlestick-Streams`` (2026-03-05)
* ``websocket-market-streams/Partial-Book-Depth-Streams`` (2026-02-13)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from types import TracebackType
from typing import Any, Self

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect

from volumatic_bot.binance.urls import Endpoint
from volumatic_bot.logging_setup import sanitize
from volumatic_bot.metrics import Metrics

__all__ = ["CombinedStreamClient"]

ConnectFactory = Callable[..., Any]


class CombinedStreamClient:
    """Klien stream gabungan dengan reconnect, watchdog, dan rotasi 24 jam.

    Subkelas wajib mengimplementasikan :meth:`stream_name` dan :meth:`handle_data`.
    """

    #: Awalan nama stream, mis. ``@kline_15m`` atau ``@depth20@100ms``.
    stream_suffix: str = ""

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        base_url: str | None = None,
        testnet: bool = True,
        metrics: Metrics | None = None,
        idle_timeout_sec: float = 120.0,
        rotate_after_sec: float = 23 * 3600.0,
        max_backoff_sec: float = 60.0,
        send_gap_sec: float = 0.12,
        connect_factory: ConnectFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
        max_streams: int = 1024,
    ) -> None:
        self.base_url = (base_url or Endpoint.ws(testnet=testnet)).rstrip("/")
        self.metrics = metrics or Metrics()
        self.idle_timeout_sec = idle_timeout_sec
        self.rotate_after_sec = rotate_after_sec
        self.max_backoff_sec = max_backoff_sec
        self.send_gap_sec = send_gap_sec
        self.max_streams = max_streams
        self._connect = connect_factory or ws_connect
        self._clock = clock
        self._wall_clock = wall_clock
        self._jitter = jitter
        self._symbols: list[str] = sorted({s.upper() for s in symbols})
        if len(self._symbols) > max_streams:
            raise ValueError(
                f"jumlah stream {len(self._symbols)} melebihi batas {max_streams} per koneksi"
            )
        self._ws: ClientConnection | None = None
        self._stop = asyncio.Event()
        self._request_id = 0
        self._last_message_at: float = 0.0

    # ------------------------------------------------------------ untuk subkelas
    def stream_name(self, symbol: str) -> str:
        """Nama stream satu simbol, mis. ``btcusdt@kline_15m``."""
        return f"{symbol.lower()}{self.stream_suffix}"

    async def handle_data(self, data: dict[str, Any]) -> None:
        """Proses isi ``data`` satu event. Wajib diimplementasikan subkelas."""
        raise NotImplementedError

    # ------------------------------------------------------------------ info
    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    @property
    def url(self) -> str:
        streams = "/".join(self.stream_name(s) for s in self._symbols)
        if not streams:
            raise ValueError("tidak ada stream untuk dijahit ke URL")
        return f"{self.base_url}/stream?streams={streams}"

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # --------------------------------------------------------------- kontrol
    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().create_task(ws.close())

    async def set_symbols(self, symbols: Iterable[str]) -> None:
        """Ubah daftar simbol tanpa memutus koneksi (SUBSCRIBE/UNSUBSCRIBE)."""
        new = {s.upper() for s in symbols}
        if len(new) > self.max_streams:
            raise ValueError(f"jumlah stream {len(new)} melebihi batas {self.max_streams}")
        old = set(self._symbols)
        add = sorted(new - old)
        remove = sorted(old - new)
        if not add and not remove:
            return
        self._symbols = sorted(new)
        ws = self._ws
        if ws is None:
            return  # dipakai saat koneksi berikutnya dibuat
        await self._send_command(ws, "SUBSCRIBE", [self.stream_name(s) for s in add])
        await self._send_command(ws, "UNSUBSCRIBE", [self.stream_name(s) for s in remove])

    async def _send_command(
        self, ws: ClientConnection, method: str, params: list[str], *, quiet: bool = False
    ) -> None:
        if not params and quiet:
            return
        self._request_id += 1
        payload = {"method": method, "params": params, "id": self._request_id}
        await ws.send(json.dumps(payload))
        # Batas 10 pesan/detik: beri jarak antar perintah.
        await asyncio.sleep(self.send_gap_sec)

    # ------------------------------------------------------------------ inti
    async def run(self) -> None:
        """Jalankan sampai :meth:`stop` dipanggil. Tidak pernah melempar keluar."""
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._run_connection()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # apa pun selain pembatalan: reconnect
                if self._stop.is_set():
                    break
                self.metrics.record_ws_reconnect()
                delay = min(self.max_backoff_sec, 1.0 * (2**attempt))
                delay *= 0.75 + 0.5 * self._jitter()
                attempt = min(attempt + 1, 8)
                _ = sanitize(f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(max(0.1, delay))

    async def _run_connection(self) -> None:
        url = self.url
        connected_at = self._clock()
        self._last_message_at = connected_at
        async with self._connect(url, ping_interval=None, max_queue=256, open_timeout=10) as ws:
            self._ws = ws
            try:
                await self.on_connected(ws)
                while not self._stop.is_set():
                    # Rotasi sebelum batas 24 jam, tanpa bergantung ada/tidaknya pesan.
                    remaining = self.rotate_after_sec - (self._clock() - connected_at)
                    if remaining <= 0:
                        await ws.close()
                        return
                    timeout = min(self.idle_timeout_sec, remaining)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except TimeoutError as exc:
                        if self._clock() - connected_at >= self.rotate_after_sec:
                            await ws.close()
                            return
                        raise ConnectionError(
                            f"tidak ada pesan selama {self.idle_timeout_sec:.0f} detik"
                        ) from exc
                    self._last_message_at = self._clock()
                    await self._handle_raw(raw)
            finally:
                self._ws = None

    async def on_connected(self, ws: ClientConnection) -> None:
        """Hook opsional setelah koneksi terbuka (mis. mengirim SUBSCRIBE manual)."""
        return None

    async def _handle_raw(self, raw: str | bytes) -> None:
        size = len(raw)
        self.metrics.record_ws_message(size)
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        if "result" in payload and "id" in payload:
            return  # balasan SUBSCRIBE/UNSUBSCRIBE
        nested = payload.get("data")
        data: dict[str, Any] = nested if isinstance(nested, dict) else payload
        event_time = data.get("E")
        if isinstance(event_time, (int, float)) and event_time > 0:
            self.metrics.record_ws_latency(self._wall_clock() * 1000 - float(event_time))
        await self.handle_data(data)

    # ---------------------------------------------------------------- utilitas
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
        await asyncio.sleep(0)


async def _noop(_: Any) -> None:  # pragma: no cover - pembantu tipe
    return None


OnMessage = Callable[[dict[str, Any]], Awaitable[None] | None]
