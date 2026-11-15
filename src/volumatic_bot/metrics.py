"""Pengukur pemakaian jaringan: bandwidth, latensi, dan bobot rate limit.

Dipakai dashboard (TAHAP 6) dan sekarang untuk CLI/diagnostik. Semua metrik
diperbarui dari dalam event loop yang sama, jadi tidak butuh lock.

Estimasi bandwidth dihitung dari jendela bergulir (default 5 menit) berisi
pasangan ``(waktu, jumlah byte)``. Metode ini jujur: kalau belum ada trafik
sama sekali, hasilnya 0.0 MB/jam, bukan angka karangan.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["Metrics", "MetricsSnapshot"]


@dataclass(slots=True)
class MetricsSnapshot:
    http_requests: int
    http_retries: int
    http_errors: int
    http_rx_bytes: int
    http_tx_bytes: int
    ws_messages: int
    ws_rx_bytes: int
    ws_reconnects: int
    ws_latency_ms: float
    ws_latency_ema_ms: float
    used_weight: int
    weight_limit: int
    bandwidth_mb_per_hour: float
    uptime_sec: float

    @property
    def weight_pct(self) -> float:
        """Persentase bobot terpakai terhadap batas per menit."""
        if self.weight_limit <= 0:
            return 0.0
        return min(100.0, self.used_weight / self.weight_limit * 100.0)

    @property
    def total_bytes(self) -> int:
        return self.http_rx_bytes + self.http_tx_bytes + self.ws_rx_bytes


@dataclass(slots=True)
class Metrics:
    """Kumpulan metrik jaringan (tanpa I/O, aman dipanggil dari mana saja)."""

    weight_limit: int = 2400
    window_sec: float = 300.0
    latency_ema_alpha: float = 0.2
    clock: Callable[[], float] = field(default=time.monotonic)
    http_requests: int = 0
    http_retries: int = 0
    http_errors: int = 0
    http_rx_bytes: int = 0
    http_tx_bytes: int = 0
    ws_messages: int = 0
    ws_rx_bytes: int = 0
    ws_reconnects: int = 0
    ws_latency_ms: float = 0.0
    ws_latency_ema_ms: float = 0.0
    used_weight: int = 0
    _bytes_window: deque[tuple[float, int]] = field(default_factory=deque)
    _started: float | None = None

    # --- pencatatan ---------------------------------------------------------
    def record_http(
        self,
        *,
        rx_bytes: int = 0,
        tx_bytes: int = 0,
        retry: bool = False,
        error: bool = False,
    ) -> None:
        self.http_requests += 1
        if retry:
            self.http_retries += 1
        if error:
            self.http_errors += 1
        self.http_rx_bytes += max(0, rx_bytes)
        self.http_tx_bytes += max(0, tx_bytes)
        self._add_bytes(max(0, rx_bytes) + max(0, tx_bytes))

    def record_used_weight(self, used_weight: int, *, limit: int | None = None) -> None:
        if used_weight >= 0:
            self.used_weight = used_weight
        if limit is not None and limit > 0:
            self.weight_limit = limit

    def record_ws_message(self, size_bytes: int = 0) -> None:
        self.ws_messages += 1
        self.ws_rx_bytes += max(0, size_bytes)
        self._add_bytes(max(0, size_bytes))

    def record_ws_latency(self, latency_ms: float) -> None:
        if latency_ms < 0 or latency_ms > 600_000:
            return
        self.ws_latency_ms = latency_ms
        if self.ws_latency_ema_ms <= 0:
            self.ws_latency_ema_ms = latency_ms
        else:
            a = self.latency_ema_alpha
            self.ws_latency_ema_ms = a * latency_ms + (1 - a) * self.ws_latency_ema_ms

    def record_ws_reconnect(self) -> None:
        self.ws_reconnects += 1

    def _add_bytes(self, count: int) -> None:
        if count <= 0:
            return
        now = self.clock()
        if self._started is None:
            self._started = now
        self._bytes_window.append((now, count))
        self._prune(now)

    def _prune(self, now: float) -> None:
        while self._bytes_window and now - self._bytes_window[0][0] > self.window_sec:
            self._bytes_window.popleft()

    def reset_window(self) -> None:
        """Kosongkan jendela bandwidth (total byte tetap dihitung).

        Dipakai setelah warm-up: 1500 candle + exchangeInfo adalah biaya **sekali**
        yang kalau dibiarkan di jendela 5 menit membuat ``bandwidth_mb_per_hour``
        melaporkan angka jauh lebih besar dari kenyataan (mis. 72 MB/jam untuk
        satu simbol padahal itu hanya burst awal). Angka total tetap tersedia
        lewat :attr:`http_rx_bytes` dan :attr:`total_bytes`.
        """
        self._bytes_window.clear()

    # --- pembacaan ----------------------------------------------------------
    def bandwidth_mb_per_hour(self) -> float:
        """Estimasi pemakaian data berdasarkan jendela bergulir."""
        now = self.clock()
        self._prune(now)
        if not self._bytes_window:
            return 0.0
        total = sum(b for _, b in self._bytes_window)
        span = now - self._bytes_window[0][0]
        # Jendela < 5 detik: data belum cukup, jangan tebak angka (kembalikan 0).
        if span < 5.0:
            return 0.0
        return total / span * 3600.0 / (1024 * 1024)

    def snapshot(self) -> MetricsSnapshot:
        now = self.clock()
        return MetricsSnapshot(
            http_requests=self.http_requests,
            http_retries=self.http_retries,
            http_errors=self.http_errors,
            http_rx_bytes=self.http_rx_bytes,
            http_tx_bytes=self.http_tx_bytes,
            ws_messages=self.ws_messages,
            ws_rx_bytes=self.ws_rx_bytes,
            ws_reconnects=self.ws_reconnects,
            ws_latency_ms=self.ws_latency_ms,
            ws_latency_ema_ms=self.ws_latency_ema_ms,
            used_weight=self.used_weight,
            weight_limit=self.weight_limit,
            bandwidth_mb_per_hour=self.bandwidth_mb_per_hour(),
            uptime_sec=now - (self._started or now),
        )
