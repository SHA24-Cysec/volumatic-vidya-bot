"""Klien WebSocket kline (combined stream) USDⓈ-M Futures.

Desain hemat bandwidth sesuai spesifikasi:

* **Satu** koneksi untuk semua simbol: ``wss://fstream.binance.com/stream?streams=``.
* Hanya stream ``<symbol>@kline_<interval>`` — tanpa bookTicker/aggTrade/depth.
* Konsumen hanya memakai event dengan ``x == true`` (candle tertutup).

Logika koneksi (reconnect, rotasi 24 jam, watchdog, SUBSCRIBE) ada di
:class:`~volumatic_bot.binance.stream_base.CombinedStreamClient`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from volumatic_bot.binance.kline_messages import KlineEvent, parse_kline_event
from volumatic_bot.binance.stream_base import CombinedStreamClient
from volumatic_bot.metrics import Metrics

__all__ = ["KlineStreamClient", "kline_stream_name"]

OnKline = Callable[[KlineEvent], Awaitable[None] | None]


def kline_stream_name(symbol: str, interval: str) -> str:
    """Nama stream kline (simbol selalu huruf kecil)."""
    return f"{symbol.lower()}@kline_{interval}"


class KlineStreamClient(CombinedStreamClient):
    """Klien stream kline dengan reconnect otomatis dan rotasi 24 jam."""

    def __init__(
        self,
        symbols: Iterable[str],
        interval: str,
        *,
        on_kline: OnKline,
        base_url: str | None = None,
        testnet: bool = True,
        metrics: Metrics | None = None,
        idle_timeout_sec: float = 120.0,
        rotate_after_sec: float = 23 * 3600.0,
        max_backoff_sec: float = 60.0,
        send_gap_sec: float = 0.12,
        connect_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        jitter: Callable[[], float] | None = None,
        max_streams: int = 1024,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        if wall_clock is not None:
            kwargs["wall_clock"] = wall_clock
        if jitter is not None:
            kwargs["jitter"] = jitter
        super().__init__(
            symbols,
            base_url=base_url,
            testnet=testnet,
            metrics=metrics,
            idle_timeout_sec=idle_timeout_sec,
            rotate_after_sec=rotate_after_sec,
            max_backoff_sec=max_backoff_sec,
            send_gap_sec=send_gap_sec,
            connect_factory=connect_factory,
            max_streams=max_streams,
            **kwargs,
        )
        self.interval = interval
        self.stream_suffix = f"@kline_{interval}"
        self._on_kline = on_kline

    def stream_name(self, symbol: str) -> str:
        return kline_stream_name(symbol, self.interval)

    async def handle_data(self, data: dict[str, Any]) -> None:
        if str(data.get("e", "")) != "kline":
            return  # stream campuran: abaikan tipe lain
        try:
            event = parse_kline_event(data)
        except ValueError:
            return
        result = self._on_kline(event)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]


def is_closed_event(event: KlineEvent) -> bool:
    """Pembantu eksplisit: hanya candle tertutup yang boleh memicu sinyal."""
    return event.is_closed
