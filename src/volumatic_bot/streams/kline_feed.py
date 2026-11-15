"""Umpan kline: dari event WebSocket menjadi sinyal entry.

Aturan penting:
* **Hanya candle tertutup** (``x == true``) yang masuk ke riwayat dan memicu
  perhitungan sinyal. Event candle berjalan diabaikan total.
* Riwayat per simbol dibatasi ``max_bars`` bar terakhir agar memori dan waktu
  hitung tetap stabil walau bot berjalan berhari-hari.
* Perhitungan indikator bisa dilempar ke thread (``offload=True``) karena pada
  batas candle 15m, puluhan simbol memberi sinyal/hitungan hampir bersamaan;
  tanpa offload, event loop bisa tersendat beberapa ratus milidetik.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from volumatic_bot.binance.kline_messages import KlineEvent
from volumatic_bot.indicator.core import IndicatorResult, compute, entry_signal
from volumatic_bot.indicator.params import IndicatorParams
from volumatic_bot.models import CandleSeries

__all__ = ["KlineFeed", "SignalEvent"]

OnSignal = Callable[["SignalEvent"], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """Sinyal entry hasil pergantian tren pada candle tertutup."""

    symbol: str
    direction: str  # "long" | "short"
    open_time: int
    close_price: float
    upper: float
    lower: float
    trail: float
    atr: float
    atr_pct: float
    cmo: float
    trend: int
    vol_delta_pct: float
    vol_delta_win_pct: float
    zone_count: float
    score: float
    bars: int

    @property
    def is_long(self) -> bool:
        return self.direction == "long"


class _SymbolBuffer:
    """Penyimpan riwayat satu simbol (list, dipotong berkala)."""

    __slots__ = ("close", "high", "low", "max_bars", "open", "open_time", "volume")

    def __init__(self, max_bars: int) -> None:
        self.max_bars = max_bars
        self.open_time: list[int] = []
        self.open: list[float] = []
        self.high: list[float] = []
        self.low: list[float] = []
        self.close: list[float] = []
        self.volume: list[float] = []

    @classmethod
    def from_series(cls, series: CandleSeries, max_bars: int) -> _SymbolBuffer:
        buffer = cls(max_bars)
        buffer.open_time = [int(t) for t in series.open_time]
        buffer.open = [float(v) for v in series.open]
        buffer.high = [float(v) for v in series.high]
        buffer.low = [float(v) for v in series.low]
        buffer.close = [float(v) for v in series.close]
        buffer.volume = [float(v) for v in series.volume]
        buffer._trim()
        return buffer

    def _trim(self) -> None:
        excess = len(self.open_time) - self.max_bars
        if excess > 0:
            for column in (
                self.open_time,
                self.open,
                self.high,
                self.low,
                self.close,
                self.volume,
            ):
                del column[:excess]

    def apply(self, event: KlineEvent) -> bool:
        """Terapkan satu candle tertutup. Mengembalikan True bila riwayat berubah."""
        if not event.is_closed:
            return False
        if self.open_time and event.open_time < self.open_time[-1]:
            return False  # event usang: abaikan
        if self.open_time and event.open_time == self.open_time[-1]:
            # Transmisi ulang bar yang sama: perbarui nilainya saja (idempoten).
            self.open[-1] = event.open
            self.high[-1] = event.high
            self.low[-1] = event.low
            self.close[-1] = event.close
            self.volume[-1] = event.volume
            return True
        self.open_time.append(event.open_time)
        self.open.append(event.open)
        self.high.append(event.high)
        self.low.append(event.low)
        self.close.append(event.close)
        self.volume.append(event.volume)
        self._trim()
        return True

    def to_series(self) -> CandleSeries:
        return CandleSeries.from_columns(
            open_time=self.open_time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class KlineFeed:
    """Jembatan WebSocket -> indikator -> sinyal."""

    def __init__(
        self,
        params: IndicatorParams | None = None,
        *,
        max_bars: int = 1700,
        on_signal: OnSignal | None = None,
        offload: bool = True,
        scores: Mapping[str, float] | None = None,
    ) -> None:
        self.params = params or IndicatorParams()
        self.max_bars = max_bars
        self._on_signal = on_signal
        self._offload = offload
        self._buffers: dict[str, _SymbolBuffer] = {}
        self._scores: dict[str, float] = dict(scores or {})
        # Bar tertutup terakhir yang sudah diproses per simbol: mencegah sinyal
        # ganda bila event yang sama terkirim ulang (mis. setelah reconnect).
        self._last_processed: dict[str, int] = {}
        self.processed = 0
        self.ignored_unclosed = 0
        self.signals = 0

    # ------------------------------------------------------------------ seeding
    def seed(self, symbol: str, series: CandleSeries) -> None:
        """Isi riwayat awal satu simbol (hasil warm-up REST/cache)."""
        self._buffers[symbol.upper()] = _SymbolBuffer.from_series(series, self.max_bars)

    def seed_many(self, series_map: Mapping[str, CandleSeries]) -> None:
        for symbol, series in series_map.items():
            self.seed(symbol, series)

    def set_score(self, symbol: str, score: float) -> None:
        self._scores[symbol.upper()] = score

    @property
    def symbols(self) -> list[str]:
        return sorted(self._buffers)

    def series(self, symbol: str) -> CandleSeries:
        buffer = self._buffers.get(symbol.upper())
        if buffer is None:
            return CandleSeries.from_columns([], [], [], [], [], [])
        return buffer.to_series()

    def bars(self, symbol: str) -> int:
        buffer = self._buffers.get(symbol.upper())
        return 0 if buffer is None else len(buffer.open_time)

    # ------------------------------------------------------------------ inti
    async def handle_event(self, event: KlineEvent) -> SignalEvent | None:
        """Proses satu event kline. Non-candle-closed tidak pernah memicu sinyal."""
        if not event.is_closed:
            self.ignored_unclosed += 1
            return None
        symbol = event.symbol.upper()
        buffer = self._buffers.get(symbol)
        if buffer is None:
            return None  # simbol belum di-warm-up
        if self._last_processed.get(symbol) == event.open_time:
            return None  # duplikat: jangan hitung/hasilkan sinyal dua kali
        if not buffer.apply(event):
            return None
        self._last_processed[symbol] = event.open_time
        self.processed += 1
        series = buffer.to_series()
        result = await self._compute(series)
        direction = entry_signal(result, -1)
        if direction is None:
            return None
        signal = self._build_signal(event, result, direction)
        self.signals += 1
        if self._on_signal is not None:
            outcome = self._on_signal(signal)
            if asyncio.iscoroutine(outcome):
                await outcome
        return signal

    async def _compute(self, series: CandleSeries) -> IndicatorResult:
        if self._offload:
            return await asyncio.to_thread(compute, series, self.params)
        return compute(series, self.params)

    def _build_signal(
        self, event: KlineEvent, result: IndicatorResult, direction: str
    ) -> SignalEvent:
        idx = len(result) - 1
        atr = float(result.atr[idx])
        close_price = float(result.open_time.size and event.close)
        atr_pct = (atr / close_price * 100.0) if close_price > 0 else float("nan")
        return SignalEvent(
            symbol=event.symbol.upper(),
            direction=direction,
            open_time=event.open_time,
            close_price=close_price,
            upper=float(result.upper[idx]),
            lower=float(result.lower[idx]),
            trail=float(result.trail[idx]),
            atr=atr,
            atr_pct=atr_pct,
            cmo=float(result.cmo[idx]),
            trend=int(result.trend[idx]),
            vol_delta_pct=float(result.vol_delta_pct[idx]),
            vol_delta_win_pct=float(result.vol_delta_win_pct[idx]),
            zone_count=float(result.zone_count[idx]),
            score=float(self._scores.get(event.symbol.upper(), 0.0)),
            bars=len(result),
        )
