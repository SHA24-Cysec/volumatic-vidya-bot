"""Klien stream *partial book depth* (``<symbol>@depth<levels>@<speed>``).

Dipakai **hanya** untuk simbol kandidat (baru memberi sinyal) lalu berhenti
berlangganan — sesuai aturan hemat bandwidth.

Dokumentasi (snapshot 2026-02-13):
* Stream: ``<symbol>@depth<levels>``, ``@depth<levels>@500ms`` atau
  ``@depth<levels>@100ms``; ``<levels>`` valid 5, 10, atau 20.
* Payload: ``{"e":"depthUpdate","E":...,"T":...,"s":"BTCUSDT",
  "U":first,"u":final,"pu":prevFinal,"b":[[price,qty],...],"a":[...]}``.
* Level dengan kuantitas 0 berarti harga itu sudah tidak ada di buku.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from volumatic_bot.binance.stream_base import CombinedStreamClient
from volumatic_bot.metrics import Metrics

__all__ = ["VALID_DEPTH_LEVELS", "DepthStreamClient", "depth_stream_name"]

VALID_DEPTH_LEVELS = (5, 10, 20)
VALID_UPDATE_SPEEDS = (100, 250, 500)

OnDepth = Callable[[dict[str, Any]], Awaitable[None] | None]


def depth_stream_name(symbol: str, levels: int = 20, speed_ms: int = 100) -> str:
    """Nama stream depth parsial, mis. ``btcusdt@depth20@100ms``."""
    if levels not in VALID_DEPTH_LEVELS:
        raise ValueError(f"levels harus salah satu dari {VALID_DEPTH_LEVELS}, diterima {levels}")
    if speed_ms not in VALID_UPDATE_SPEEDS:
        raise ValueError(
            f"speed_ms harus salah satu dari {VALID_UPDATE_SPEEDS}, diterima {speed_ms}"
        )
    return f"{symbol.lower()}@depth{levels}@{speed_ms}ms"


class DepthStreamClient(CombinedStreamClient):
    """Stream depth parsial untuk sekumpulan simbol kandidat."""

    def __init__(
        self,
        symbols: Iterable[str] = (),
        *,
        levels: int = 20,
        speed_ms: int = 100,
        on_depth: OnDepth | None = None,
        base_url: str | None = None,
        testnet: bool = True,
        metrics: Metrics | None = None,
        idle_timeout_sec: float = 120.0,
        rotate_after_sec: float = 23 * 3600.0,
        send_gap_sec: float = 0.12,
        connect_factory: Callable[..., Any] | None = None,
        max_streams: int = 1024,
    ) -> None:
        super().__init__(
            symbols,
            base_url=base_url,
            testnet=testnet,
            metrics=metrics,
            idle_timeout_sec=idle_timeout_sec,
            rotate_after_sec=rotate_after_sec,
            send_gap_sec=send_gap_sec,
            connect_factory=connect_factory,
            max_streams=max_streams,
        )
        self.levels = levels
        self.speed_ms = speed_ms
        self.stream_suffix = f"@depth{levels}@{speed_ms}ms"
        self._on_depth = on_depth

    def stream_name(self, symbol: str) -> str:
        return depth_stream_name(symbol, self.levels, self.speed_ms)

    async def handle_data(self, data: dict[str, Any]) -> None:
        if str(data.get("e", "")) != "depthUpdate":
            return
        if self._on_depth is None:
            return
        result = self._on_depth(data)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]
