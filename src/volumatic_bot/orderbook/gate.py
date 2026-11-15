"""Aturan penerimaan/penolakan sinyal berdasarkan order book.

Semua ambang berasal dari config (``orderbook.*``) — tidak ada angka ajaib di
kode. Setiap penolakan menyertakan **alasan yang bisa dibaca manusia**, karena
alasan itu yang akan tampil di log dan dashboard.

Urutan pemeriksaan disusun dari yang paling fatal:

1. data tersedia dan masih segar,
2. buku tidak rusak (bid < ask, kedua sisi ada isinya),
3. spread wajar,
4. tidak ada wall besar tepat di depan arah trade,
5. estimasi slippage untuk ukuran order bot masih dalam batas,
6. skor akhir memenuhi ambang minimum.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from volumatic_bot.config import OrderBookConfig
from volumatic_bot.orderbook.analyzer import BookMetrics, analyze
from volumatic_bot.orderbook.models import OrderBookSnapshot

__all__ = ["BookDecision", "evaluate"]

Direction = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class BookDecision:
    """Hasil evaluasi order book untuk satu sinyal."""

    accepted: bool
    score: float  # 0..1, makin tinggi makin mendukung arah entry
    reasons: tuple[str, ...]  # alasan penolakan (kosong bila diterima)
    notes: tuple[str, ...]  # informasi pendukung (diterima maupun ditolak)
    metrics: BookMetrics | None
    enabled: bool = True

    @property
    def rejected(self) -> bool:
        return not self.accepted

    def summary(self) -> str:
        """Satu baris ringkas untuk log/dashboard."""
        if not self.enabled:
            return "order book: filter dimatikan (skor netral 0.50)"
        head = "DITERIMA" if self.accepted else "DITOLAK"
        parts = [f"{head} skor={self.score:.2f}"]
        if self.metrics is not None:
            m = self.metrics
            parts.append(f"spread={m.spread_pct:.4f}%")
            parts.append(f"imbalance={m.imbalance_pct:+.1f}%")
            parts.append(f"slippage={m.slippage_pct:.4f}%")
            if m.wall is not None:
                parts.append(
                    f"wall {m.wall.side}@{m.wall.price:g} "
                    f"({m.wall.ratio_to_median:.1f}x, {m.wall.distance_atr:.2f} ATR)"
                )
        if self.reasons:
            parts.append("alasan: " + "; ".join(self.reasons))
        return " | ".join(parts)


def _neutral(notes: tuple[str, ...], *, reasons: tuple[str, ...] = ()) -> BookDecision:
    return BookDecision(
        accepted=False,
        score=0.5,
        reasons=reasons,
        notes=notes,
        metrics=None,
        enabled=False,
    )


def evaluate(
    snapshot: OrderBookSnapshot | None,
    *,
    direction: Direction,
    tick_size: float,
    target_qty: float,
    atr: float,
    config: OrderBookConfig,
    now: float,
    unavailable_reason: str | None = None,
) -> BookDecision:
    """Terapkan aturan order book. Tidak pernah melempar untuk data buruk."""
    if not config.enabled:
        return _neutral(("filter order book dimatikan di config",))

    if snapshot is None:
        reason = unavailable_reason or "snapshot order book tidak tersedia"
        if config.reject_if_unavailable:
            return BookDecision(
                accepted=False,
                score=0.0,
                reasons=(f"depth tidak tersedia: {reason}",),
                notes=(),
                metrics=None,
            )
        return BookDecision(
            accepted=True,
            score=0.5,
            reasons=(),
            notes=(
                f"depth tidak tersedia ({reason}); diloloskan karena reject_if_unavailable=false",
            ),
            metrics=None,
        )

    if not snapshot.is_usable():
        return BookDecision(
            accepted=False,
            score=0.0,
            reasons=("buku rusak: sisi bid/ask kosong atau harga bersilangan",),
            notes=(),
            metrics=None,
        )

    if target_qty <= 0:
        raise ValueError("target_qty harus > 0 (estimasi slippage butuh ukuran order)")

    metrics = analyze(
        snapshot,
        direction=direction,
        tick_size=tick_size,
        target_qty=target_qty,
        atr=atr,
        band_pct=config.imbalance_range_pct,
        wall_multiplier=config.wall_multiplier,
        now=now,
    )

    reasons: list[str] = []
    notes: list[str] = []

    # 1) Kesegaran data.
    if metrics.age_sec > config.depth_snapshot_ttl_sec:
        reasons.append(
            f"snapshot depth basi {metrics.age_sec:.1f}s > {config.depth_snapshot_ttl_sec}s"
        )

    # 2) Spread.
    if metrics.spread_pct > config.max_spread_pct:
        reasons.append(
            f"spread {metrics.spread_pct:.4f}% > batas {config.max_spread_pct:.4f}% "
            f"({metrics.spread_ticks:.1f} tick)"
        )
    else:
        notes.append(f"spread {metrics.spread_pct:.4f}% ({metrics.spread_ticks:.1f} tick)")

    # 3) Wall di depan arah trade.
    wall = metrics.wall
    if wall is not None:
        limit_atr = config.wall_proximity_atr
        within = math.isfinite(wall.distance_atr) and wall.distance_atr <= limit_atr
        if within:
            reasons.append(
                f"wall {wall.side} {wall.ratio_to_median:.1f}x median di harga {wall.price:g} "
                f"({wall.distance_atr:.2f} ATR <= {limit_atr:.2f} ATR) menghalangi arah {direction}"
            )
        else:
            notes.append(
                f"wall {wall.side} {wall.ratio_to_median:.1f}x median di {wall.price:g} "
                f"({wall.distance_atr:.2f} ATR, di luar ambang {limit_atr:.2f} ATR)"
            )

    # 4) Slippage & kecukupan likuiditas untuk ukuran order bot.
    if not metrics.fill_complete:
        reasons.append(
            f"likuiditas tidak cukup untuk {target_qty:g} unit dalam "
            f"{metrics.depth_levels} level yang diambil"
        )
    elif metrics.slippage_pct > config.max_slippage_pct:
        reasons.append(
            f"estimasi slippage {metrics.slippage_pct:.4f}% > batas "
            f"{config.max_slippage_pct:.4f}% ({metrics.slippage_ticks:.1f} tick)"
        )
    else:
        notes.append(
            f"estimasi slippage {metrics.slippage_pct:.4f}% ({metrics.slippage_ticks:.1f} tick, "
            f"{metrics.levels_used} level)"
        )

    # 5) Skor: imbalance searah trade menaikkan skor.
    sign = 1.0 if direction == "long" else -1.0
    support = max(-1.0, min(1.0, sign * metrics.imbalance_pct / 100.0))
    score = 0.5 + 0.5 * support
    if metrics.wall is not None and not math.isfinite(metrics.wall.distance_atr):
        score -= 0.05  # wall ada tapi jaraknya tak terukur: sedikit penalti
    if metrics.imbalance_pct * sign > 0:
        notes.append(f"imbalance {metrics.imbalance_pct:+.1f}% mendukung {direction}")
    else:
        notes.append(f"imbalance {metrics.imbalance_pct:+.1f}% melawan {direction}")
    score = max(0.0, min(1.0, score))

    if not reasons and score < config.min_score_to_enter:
        reasons.append(
            f"skor order book {score:.2f} < ambang minimum {config.min_score_to_enter:.2f}"
        )

    return BookDecision(
        accepted=not reasons,
        score=score,
        reasons=tuple(reasons),
        notes=tuple(notes),
        metrics=metrics,
    )
