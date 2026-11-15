"""Jembatan sinyal indikator → filter order book → peringkat kandidat.

Modul ini **tidak** mengirim order (itu TAHAP 5) dan tidak menghitung ukuran
posisi berbasis risiko (TAHAP 4). Perannya: memutuskan *sinyal mana yang layak
diantrikan*, lengkap dengan alasannya.

Alur satu sinyal:

``SignalEvent`` → metadata simbol (tickSize) → snapshot depth → analisis &
aturan → skor gabungan → ``Candidate``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from volumatic_bot.binance.symbols import SymbolInfo
from volumatic_bot.config import AppConfig
from volumatic_bot.orderbook.depth import DepthSource, fetch_snapshot_safe
from volumatic_bot.orderbook.gate import BookDecision, evaluate
from volumatic_bot.signals.ranking import (
    Candidate,
    book_score,
    combine,
    rank_candidates,
    trend_strength,
    volume_score,
)
from volumatic_bot.streams.kline_feed import SignalEvent

__all__ = ["GateResult", "SignalGate"]


@dataclass(frozen=True, slots=True)
class GateResult:
    """Hasil satu putaran evaluasi (bisa berisi beberapa sinyal)."""

    candidates: list[Candidate]

    @property
    def selected(self) -> list[Candidate]:
        return [c for c in self.candidates if c.selected]

    @property
    def tradable(self) -> list[Candidate]:
        return [c for c in self.candidates if c.tradable]

    @property
    def rejected(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.tradable]


class SignalGate:
    """Evaluasi sinyal dengan order book dan beri peringkat."""

    def __init__(
        self,
        config: AppConfig,
        depth_source: DepthSource,
        symbol_info: Callable[[str], SymbolInfo | None],
        *,
        clock: Callable[[], float],
    ) -> None:
        self.config = config
        self.depth_source = depth_source
        self.symbol_info = symbol_info
        self.clock = clock

    # ------------------------------------------------------------------ satu
    async def evaluate(self, signal: SignalEvent, *, target_qty: float | None = None) -> Candidate:
        """Evaluasi satu sinyal (selalu mengembalikan Candidate, tidak melempar)."""
        try:
            info = self.symbol_info(signal.symbol)
        except KeyError:  # ExchangeInfo.get melempar bila simbol tidak ada
            info = None
        if info is None:
            decision = BookDecision(
                accepted=False,
                score=0.0,
                reasons=(f"metadata simbol {signal.symbol} tidak ditemukan di exchangeInfo",),
                notes=(),
                metrics=None,
            )
            return self._build(signal, decision)

        if target_qty is None:
            notional = self.config.orderbook.target_notional_usdt
            price = signal.close_price if signal.close_price > 0 else float(info.filters.max_price)
            raw_qty = max(float(info.filters.min_qty), notional / max(price, 1e-12))
            rounded = float(info.filters.round_qty(raw_qty, mode="down"))
            # Pembulatan bisa menghasilkan 0 (mis. stepSize 1 dan qty 0.4); pakai
            # minQty agar estimasi slippage tetap punya ukuran bermakna.
            target_qty = rounded if rounded > 0 else float(info.filters.min_qty)

        snapshot, failure = await fetch_snapshot_safe(self.depth_source, signal.symbol)
        decision = evaluate(
            snapshot,
            direction=signal.direction,  # type: ignore[arg-type]
            tick_size=float(info.filters.tick_size),
            target_qty=target_qty,
            atr=signal.atr,
            config=self.config.orderbook,
            now=self.clock(),
            unavailable_reason=failure,
        )
        return self._build(signal, decision)

    # ---------------------------------------------------------------- banyak
    async def evaluate_many(
        self,
        signals: Iterable[SignalEvent],
        *,
        slots: int,
        target_qty: float | None = None,
    ) -> GateResult:
        """Evaluasi banyak sinyal, urutkan, lalu tandai ``slots`` teratas sebagai terpilih."""
        candidates = [await self.evaluate(signal, target_qty=target_qty) for signal in signals]
        ranked = rank_candidates(candidates)
        chosen: list[Candidate] = []
        remaining = max(0, slots)
        for candidate in ranked:
            if remaining > 0 and candidate.tradable:
                chosen.append(
                    Candidate(
                        signal=candidate.signal,
                        book=candidate.book,
                        trend_component=candidate.trend_component,
                        volume_component=candidate.volume_component,
                        book_component=candidate.book_component,
                        score=candidate.score,
                        selected=True,
                    )
                )
                remaining -= 1
            else:
                chosen.append(candidate)
        return GateResult(candidates=chosen)

    # --------------------------------------------------------------- internal
    def _build(self, signal: SignalEvent, decision: BookDecision) -> Candidate:
        trend = trend_strength(signal.cmo, self.config.scoring.trend_cmo_ref)
        volume = volume_score(signal.vol_delta_win_pct, signal.direction)  # type: ignore[arg-type]
        book = book_score(decision)
        total = combine(
            trend=trend,
            volume=volume,
            book=book,
            weights=self.config.scoring.normalized,
        )
        return Candidate(
            signal=signal,
            book=decision,
            trend_component=trend,
            volume_component=volume,
            book_component=book,
            score=total,
        )


def summarize(candidates: Sequence[Candidate]) -> list[str]:
    """Ringkasan satu baris per kandidat (untuk log/dashboard)."""
    lines = []
    for candidate in candidates:
        row = candidate.as_row()
        lines.append(
            f"{row['symbol']:<12} {str(row['direction']).upper():<5} "
            f"skor={row['score']:.3f} (tren {row['trend']:.2f} / vol {row['volume']:.2f} / "
            f"book {row['book']:.2f}) "
            f"{'DIPILIH' if row['selected'] else (row['reason'] or 'menunggu slot')}"
        )
    return lines
