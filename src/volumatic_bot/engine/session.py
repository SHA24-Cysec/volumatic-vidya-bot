"""Sesi offline: menjalankan **mesin penuh** pada candle historis.

Bedanya dengan ``backtest``:

* ``backtest`` memakai simulator OHLC yang cepat (tanpa order, tanpa SQLite);
* ``simulate-session`` memakai **mesin produksi** — ``RiskManager``,
  ``TradeExecutor``, ``DryRunBroker``, ``PositionStore``, dan notifikasi — jadi
  jalur yang diuji benar-benar jalur yang akan dipakai untuk testnet/live.

Harga disimulasikan bertahap: setiap candle dipecah menjadi beberapa tick
(``open → high → low → close`` untuk long/short yang sama-sama mungkin kena),
sehingga SL/TP bisa terpicu **di dalam** candle seperti di bursa.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from volumatic_bot.binance.kline_messages import KlineEvent
from volumatic_bot.models import CandleSeries
from volumatic_bot.streams.kline_feed import KlineFeed

if TYPE_CHECKING:
    from volumatic_bot.binance.symbols import SymbolInfo
    from volumatic_bot.config import AppConfig
    from volumatic_bot.engine.trader import TradingEngine
    from volumatic_bot.execution.broker import DryRunBroker
    from volumatic_bot.positions.store import PositionStore

__all__ = ["run_offline_session", "session_report"]


def _event(series: CandleSeries, i: int, symbol: str) -> KlineEvent:
    return KlineEvent(
        symbol=symbol,
        interval="sim",
        open_time=int(series.open_time[i]),
        close_time=int(series.open_time[i]) + 900_000 - 1,
        open=float(series.open[i]),
        high=float(series.high[i]),
        low=float(series.low[i]),
        close=float(series.close[i]),
        volume=float(series.volume[i]),
        quote_volume=float(series.volume[i]) * float(series.close[i]),
        is_closed=True,
        event_time=int(series.open_time[i]) + 900_000 - 1,
    )


def _jalur_harga(
    buka: float, tinggi: float, rendah: float, tutup: float, *, long_dulu: bool
) -> list[float]:
    """Urutan sentuhan harga di dalam satu candle (asumsi konservatif)."""
    pertama, kedua = (rendah, tinggi) if long_dulu else (tinggi, rendah)
    return [buka, pertama, kedua, tutup]


@dataclass(slots=True)
class SessionReport:
    """Ringkasan satu sesi simulasi mesin."""

    symbol: str
    bars: int
    signals: int = 0
    entered: int = 0
    rejected: int = 0
    blocked: int = 0
    closed: int = 0
    positions: list[dict[str, object]] = field(default_factory=list)
    equity_awal: float = 0.0
    equity_akhir: float = 0.0
    stopped: bool = False
    stop_reason: str | None = None
    incidents: list[dict[str, object]] = field(default_factory=list)
    durasi_sec: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "simbol": self.symbol,
            "bar": self.bars,
            "sinyal": self.signals,
            "posisi_dibuka": self.entered,
            "ditolak_risiko": self.rejected,
            "diblokir": self.blocked,
            "posisi_ditutup": self.closed,
            "ekuitas": {"awal": self.equity_awal, "akhir": round(self.equity_akhir, 4)},
            "sesi_dihentikan": {"ya": self.stopped, "alasan": self.stop_reason},
            "insiden": self.incidents,
            "posisi": self.positions,
            "durasi_detik": round(self.durasi_sec, 2),
        }


async def run_offline_session_async(
    *,
    series: CandleSeries,
    config: AppConfig,
    engine: TradingEngine,
    store: PositionStore,
    broker: DryRunBroker,
    info: SymbolInfo,
    limit_bars: int | None = None,
    warmup_bars: int = 260,
    step_pct: float = 0.5,
) -> SessionReport:
    """Replay candle ke mesin penuh. Semua I/O hanya ke SQLite (tidak ada jaringan)."""
    mulai = time.perf_counter()
    report = SessionReport(
        symbol=info.symbol,
        bars=len(series),
        equity_awal=engine.equity(),
    )

    async def _on_signal(signal: Any) -> None:
        await engine.handle_signal(signal)

    feed = KlineFeed(config.indicator.to_params(), max_bars=1700, on_signal=_on_signal)
    awal = max(warmup_bars, 1)
    feed.seed(
        info.symbol,
        CandleSeries.from_columns(
            open_time=[int(x) for x in series.open_time[:awal]],
            open=[float(x) for x in series.open[:awal]],
            high=[float(x) for x in series.high[:awal]],
            low=[float(x) for x in series.low[:awal]],
            close=[float(x) for x in series.close[:awal]],
            volume=[float(x) for x in series.volume[:awal]],
        ),
    )

    akhir = len(series) if limit_bars is None else min(len(series), warmup_bars + limit_bars)
    # Metadata simbol wajib tersedia sebelum bar pertama diproses: sinyal pada
    # bar tepat setelah warm-up butuh tickSize/stepSize untuk tiket risiko.
    engine.set_symbol_info(info)
    for i in range(max(warmup_bars, 1), akhir):
        event = _event(series, i, info.symbol)
        if feed.handle_event is not None:
            await feed.handle_event(event)
        if engine.stopped:
            report.blocked += 1
            break
        # Jalankan harga bertahap untuk memicu SL/TP di dalam candle.
        long_dulu = float(series.close[i]) >= float(series.open[i])
        for harga in _jalur_harga(
            float(series.open[i]),
            float(series.high[i]),
            float(series.low[i]),
            float(series.close[i]),
            long_dulu=long_dulu,
        ):
            broker.set_price(info.symbol, harga)
            engine.set_atr(info.symbol, _atr_dari(series, i))
            await engine.handle_price(info.symbol, harga)

    report.signals = engine.counters["signals"]
    report.entered = engine.counters["entered"]
    report.rejected = engine.counters["rejected"]
    report.closed = engine.counters["closed"]
    report.blocked = max(report.blocked, engine.counters["blocked"])
    report.equity_akhir = engine.equity()
    report.stopped = engine.stopped
    report.stop_reason = engine.stop_reason
    report.positions = [
        {
            "simbol": p.symbol,
            "status": str(p.state),
            "qty": str(p.qty),
            "sisa": str(p.remaining_qty),
            "masuk": str(p.entry_price),
            "sl_akhir": str(p.stop_price),
            "alasan_tutup": p.close_reason,
            "realisasi_usdt": p.realized_usdt,
        }
        for p in store.all_positions(limit=1000)
    ]
    report.incidents = [
        {"jenis": x.kind, "severity": x.severity, "detail": x.detail, "simbol": x.symbol}
        for x in store.incidents(limit=50)
    ]
    report.durasi_sec = time.perf_counter() - mulai
    return report


def _atr_dari(series: CandleSeries, i: int) -> float:
    """ATR kasar dari candle berjalan (cukup untuk trailing simulasi)."""
    tinggi = float(series.high[i])
    rendah = float(series.low[i])
    return max((tinggi - rendah) * 0.5, 1e-12)


def run_offline_session(**kwargs: object) -> dict[str, object]:
    """Versi sinkron dari :func:`run_offline_session_async` (untuk CLI)."""
    import asyncio

    hasil = asyncio.run(run_offline_session_async(**kwargs))  # type: ignore[arg-type]
    return hasil.as_dict()


def session_report(hasil: dict[str, object]) -> str:
    """Rangkuman teks satu sesi simulasi."""
    ek = hasil["ekuitas"]
    assert isinstance(ek, dict)
    baris = [
        f"Sesi {hasil['simbol']} selesai: {hasil['bar']} bar, "
        f"{hasil['sinyal']} sinyal, {hasil['posisi_dibuka']} entry, "
        f"{hasil['posisi_ditutup']} ditutup, {hasil['ditolak_risiko']} ditolak risiko",
        f"Ekuitas {ek['awal']:.2f} → {ek['akhir']:.2f} USDT ({ek['akhir'] - ek['awal']:+.4f})",
        f"Durasi {hasil['durasi_detik']} detik",
    ]
    insiden = hasil["insiden"]
    if isinstance(insiden, list) and insiden:
        baris.append(f"{len(insiden)} insiden tercatat (lihat `positions`)")
    return "\n".join(baris)


def print_session_report(hasil: dict[str, object]) -> None:
    print(session_report(hasil))
    posisi = hasil["posisi"]
    if isinstance(posisi, list) and posisi:
        print(f"\n{len(posisi)} posisi tercatat di SQLite (contoh terakhir):")
        for p in posisi[-5:]:
            assert isinstance(p, dict)
            print(
                f"  {p['simbol']} {p['status']} qty={p['qty']} sisa={p['sisa']} "
                f"masuk={p['masuk']} sl={p['sl_akhir']} alasan={p['alasan_tutup']} "
                f"realisasi={p['realisasi_usdt']}"
            )
