"""Pembentuk pesan notifikasi dari objek domain bot.

Fungsi di sini menjaga agar modul lain (eksekusi, rekonsiliasi, CLI) tidak perlu
merangkai teks sendiri — satu tempat untuk format pesan berarti pesan selalu
konsisten dan selalu melewati penyaringan rahasia di
:class:`~volumatic_bot.notify.models.NotifyEvent`.
"""

from __future__ import annotations

from decimal import Decimal

from volumatic_bot.notify.models import NotifyEvent, NotifyKind, NotifyLevel
from volumatic_bot.positions.models import TrackedPosition
from volumatic_bot.risk.manager import TradeTicket
from volumatic_bot.signals.ranking import Candidate

__all__ = [
    "event_for_candidate",
    "event_for_entry",
    "event_for_exit",
    "event_for_incident",
    "event_for_rejected",
]


def _harga(nilai: Decimal | None) -> str:
    return "-" if nilai is None else f"{nilai}"


def event_for_candidate(candidate: Candidate) -> NotifyEvent:
    """Sinyal baru yang lolos filter (belum tentu dieksekusi)."""
    return NotifyEvent(
        kind=NotifyKind.SIGNAL,
        title=f"sinyal {candidate.direction.upper()} {candidate.symbol}",
        detail=(
            f"skor={candidate.score:.3f} (tren={candidate.trend_component:.2f} "
            f"volume={candidate.volume_component:.2f} buku={candidate.book_component:.2f})"
        ),
        level=NotifyLevel.INFO,
        symbol=candidate.symbol,
        dedupe_key=f"signal:{candidate.symbol}:{candidate.direction}",
    )


def event_for_entry(pos: TrackedPosition, *, mode: str | None = None) -> NotifyEvent:
    """Posisi berhasil dibuka dan sudah terlindungi."""
    return NotifyEvent(
        kind=NotifyKind.ENTRY,
        title=f"entry {pos.side.upper()} {pos.symbol}",
        detail=(
            f"qty={pos.qty} @ {pos.entry_price} | SL={pos.stop_price} TP1={pos.tp1_price} "
            f"| risiko≈{pos.r_multiple:.2f} USDT | mode={mode or pos.mode}"
        ),
        level=NotifyLevel.SUCCESS,
        symbol=pos.symbol,
        dedupe_key=f"entry:{pos.symbol}",
    )


def event_for_exit(pos: TrackedPosition) -> NotifyEvent:
    """Posisi tertutup (SL/TP/manual/likuidasi)."""
    realized = pos.realized_usdt
    if realized is None:
        hasil = "hasil bersih belum diketahui"
    elif realized > 0:
        hasil = f"laba bersih ≈{realized:.4f} USDT"
    elif realized < 0:
        hasil = f"rugi bersih ≈{abs(realized):.4f} USDT"
    else:
        hasil = "impas setelah biaya"
    return NotifyEvent(
        kind=NotifyKind.EXIT,
        title=f"keluar {pos.symbol} ({pos.close_reason or 'tanpa alasan tercatat'})",
        detail=(
            f"{hasil} | sisa={pos.remaining_qty} dari {pos.qty} | SL akhir={_harga(pos.stop_price)}"
        ),
        level=NotifyLevel.INFO,
        symbol=pos.symbol,
        dedupe_key=f"exit:{pos.symbol}",
    )


def event_for_rejected(ticket: TradeTicket) -> NotifyEvent:
    """Sinyal yang ditolak manajer risiko (tidak ada order dikirim)."""
    return NotifyEvent(
        kind=NotifyKind.REJECTED,
        title=f"trade {ticket.symbol} dilewati",
        detail="; ".join(ticket.reasons) or "tanpa alasan tercatat",
        level=NotifyLevel.WARNING,
        symbol=ticket.symbol,
        dedupe_key=f"rejected:{ticket.symbol}",
    )


def event_for_incident(
    *, kind: str, severity: str, detail: str, symbol: str | None = None
) -> NotifyEvent:
    """Insiden (SL gagal, posisi hantu, kill switch, ...)."""
    level = NotifyLevel.CRITICAL if severity in {"critical", "error"} else NotifyLevel.WARNING
    return NotifyEvent(
        kind=NotifyKind.INCIDENT,
        title=f"insiden: {kind}" + (f" [{symbol}]" if symbol else ""),
        detail=detail,
        level=level,
        symbol=symbol,
        dedupe_key=f"incident:{kind}:{symbol or '-'}",
    )
