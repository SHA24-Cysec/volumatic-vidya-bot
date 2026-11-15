"""Penyusun tampilan dashboard dengan ``rich``.

Semua fungsi di sini **murni**: masukan :class:`DashboardState`, keluaran objek
``rich`` (``Panel``/``Table``/``Group``). Tidak ada I/O, tidak ada jam sistem
selain dari state, sehingga hasilnya bisa diuji sebagai teks biasa.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from volumatic_bot.dashboard.models import (
    BotStatus,
    CandidateRow,
    DashboardState,
    EventLine,
    PositionRow,
)
from volumatic_bot.metrics import MetricsSnapshot

__all__ = [
    "candidates_panel",
    "events_panel",
    "positions_panel",
    "render_state",
    "status_panel",
]

_MODE_STYLE = {
    "backtest": "magenta",
    "dry_run": "cyan",
    "testnet": "yellow",
    "live": "bold red",
}
_LEVEL_STYLE = {
    "DEBUG": "dim",
    "INFO": "white",
    "SUCCESS": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold red",
}
#: Warna laba/rugi: hijau untung, merah rugi, abu-abu belum diketahui.
_UP = "green"
_DOWN = "red"
_FLAT = "dim"


def _warna_pnl(nilai: float | None) -> str:
    if nilai is None or nilai == 0:
        return _FLAT
    return _UP if nilai > 0 else _DOWN


def _uptime(detik: float) -> str:
    total = max(0, int(detik))
    jam, sisa = divmod(total, 3600)
    menit, detik2 = divmod(sisa, 60)
    if jam:
        return f"{jam}j {menit:02d}m"
    if menit:
        return f"{menit}m {detik2:02d}s"
    return f"{detik2}s"


def _angka(nilai: float | None, *, satuan: str = "", desimal: int = 2) -> str:
    if nilai is None:
        return "-"
    tanda = "+" if nilai > 0 else ""
    return f"{tanda}{nilai:.{desimal}f}{satuan}"


def _desimal(nilai: object, *, maks: int = 8) -> str:
    """Tampilkan Decimal tanpa nol berekor berlebihan (0.3500 -> 0.35)."""
    teks = str(nilai)
    if "." not in teks:
        return teks
    bulat, pecahan = teks.split(".", 1)
    pecahan = pecahan[:maks].rstrip("0")
    return f"{bulat}.{pecahan}" if pecahan else bulat


# --------------------------------------------------------------- panel status
def status_panel(
    status: BotStatus, metrics: MetricsSnapshot | None = None, *, wide: bool = True
) -> Panel:
    """Panel 1: mode, ekuitas, batas harian, margin, dan pemakaian jaringan."""
    mode_style = _MODE_STYLE.get(status.mode, "white")
    judul = Text("STATUS BOT", style="bold")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left", no_wrap=True)
    grid.add_column(justify="left", no_wrap=True)
    grid.add_column(justify="left", no_wrap=True)
    grid.add_column(justify="left", no_wrap=True)

    mode = Text(f"{status.mode.upper()}", style=mode_style)
    keadaan = Text("BERJALAN", style="green") if status.running else Text("BERHENTI", style="red")
    if status.killed:
        keadaan = Text("KILL SWITCH", style="bold red")
    elif status.daily_stopped:
        keadaan = Text("BATAS HARIAN", style="bold yellow")

    grid.add_row("Mode", mode, "Status", keadaan)
    grid.add_row(
        "Ekuitas",
        Text(f"{status.equity:.2f} USDT"),
        "Uptime",
        Text(_uptime(status.uptime_sec)),
    )
    grid.add_row(
        "Hari ini",
        Text(
            f"{_angka(status.pnl_usdt, satuan=' USDT')} ({_angka(status.pnl_pct, satuan='%')})",
            style=_warna_pnl(status.pnl_usdt),
        ),
        "Batas rugi",
        _loss_bar(status),
    )
    grid.add_row(
        "Trade",
        Text(f"{status.trades_today}/{status.max_trades}"),
        "Posisi",
        Text(f"{status.open_positions}/{status.max_positions}"),
    )
    grid.add_row(
        "Margin",
        Text(f"{status.used_margin_pct:.1f}% / {status.max_margin_pct:.0f}%"),
        "Cooldown",
        Text(_cooldown_text(status.cooldown_until)),
    )
    if metrics is not None:
        grid.add_row(
            "Bandwidth",
            Text(f"{metrics.bandwidth_mb_per_hour:.2f} MB/jam"),
            "Bobot",
            Text(f"{metrics.used_weight}/{metrics.weight_limit} ({metrics.weight_pct:.0f}%)"),
        )
        if wide:
            grid.add_row(
                "HTTP",
                Text(f"{metrics.http_requests} req, {metrics.http_errors} err"),
                "WS",
                Text(
                    f"{metrics.ws_messages} pesan, {metrics.ws_reconnects} reconnect, "
                    f"latensi {metrics.ws_latency_ema_ms:.0f} ms"
                ),
            )
        else:
            grid.add_row(
                "Jaringan",
                Text(f"{metrics.http_requests} req / {metrics.ws_messages} pesan WS"),
                "Latensi",
                Text(f"{metrics.ws_latency_ema_ms:.0f} ms"),
            )
    isi: list[RenderableType] = [grid]
    if status.note:
        isi.append(Text(status.note, style="dim italic"))
    if status.kill_reason:
        isi.append(Text(f"alasan kill: {status.kill_reason}", style="red"))
    return Panel(Group(*isi), title=judul, border_style=mode_style, box=box.ROUNDED)


def _loss_bar(status: BotStatus) -> Text:
    pct = status.loss_pct_of_limit
    isi = min(10, max(0, round(pct / 10)))
    bar = "█" * isi + "░" * (10 - isi)
    warna = "green" if pct < 50 else "yellow" if pct < 100 else "red"
    return Text.assemble((bar, warna), (f" {pct:.0f}% dari {status.daily_loss_limit_pct:.0f}%", ""))


def _cooldown_text(sampai: datetime | None) -> str:
    if sampai is None:
        return "-"
    try:
        sisa = (sampai - datetime.now().astimezone()).total_seconds()
    except TypeError:  # pragma: no cover - datetime tanpa zona waktu
        return str(sampai)
    if sisa <= 0:
        return "-"
    return f"{int(sisa // 60)}m {int(sisa % 60):02d}s"


# ------------------------------------------------------------- panel posisi
def positions_panel(
    rows: Iterable[PositionRow], *, show_history: bool = False, wide: bool = True
) -> Panel:
    """Panel 2: posisi aktif (dan opsional yang sudah tertutup).

    ``wide=False`` (terminal sempit) memakai kolom lebih sedikit supaya nama
    simbol tidak terpotong: menghilangkan TP1 dan P/L persen.
    """
    tabel = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    tabel.add_column("Simbol", no_wrap=True)
    tabel.add_column("Arah", no_wrap=True)
    tabel.add_column("Qty sisa", justify="right", no_wrap=True)
    tabel.add_column("Entry", justify="right", no_wrap=True)
    tabel.add_column("Harga", justify="right", no_wrap=True)
    tabel.add_column("SL", justify="right", no_wrap=True)
    if wide:
        tabel.add_column("TP1", justify="right", no_wrap=True)
    tabel.add_column("P/L USDT", justify="right", no_wrap=True)
    if wide:
        tabel.add_column("P/L %", justify="right", no_wrap=True)
    tabel.add_column("Tanda", overflow="fold")

    jumlah = 0
    aktif = 0
    for row in rows:
        if not row.active and not show_history:
            continue
        jumlah += 1
        aktif += 1 if row.active else 0
        arah = Text(row.side.upper(), style=_UP if row.side == "long" else _DOWN)
        pnl = row.pnl_usdt if row.active else row.realized_usdt
        persen = row.pnl_pct if row.active else None
        sel: list[Any] = [
            row.symbol,
            arah,
            _desimal(row.remaining_qty) + "/" + _desimal(row.qty),
            _desimal(row.entry),
            "-" if row.mark is None else _desimal(row.mark),
            _desimal(row.stop),
        ]
        if wide:
            sel.append(_desimal(row.tp1))
        sel.append(Text(_angka(pnl, desimal=2), style=_warna_pnl(pnl)))
        if wide:
            sel.append(Text(_angka(persen, satuan="%", desimal=2), style=_warna_pnl(persen)))
        sel.append(Text(row.flags if wide else row.flags_short, style="dim"))
        # Riwayat ditampilkan lebih redup supaya tidak dikira posisi terbuka.
        tabel.add_row(*sel, style=None if row.active else "dim")
    if not jumlah:
        isi: RenderableType = Text("tidak ada posisi terbuka", style="dim")
    else:
        isi = tabel
    riwayat = jumlah - aktif
    judul = f"POSISI ({aktif} aktif" + (f", {riwayat} riwayat)" if riwayat else ")")
    return Panel(isi, title=judul, border_style="blue", box=box.ROUNDED)


# ----------------------------------------------------------- panel kandidat
def candidates_panel(
    rows: Iterable[CandidateRow], *, max_rows: int = 10, wide: bool = True
) -> Panel:
    """Panel 3: peringkat kandidat dari pemindai + alasan tidak dipakai."""
    daftar = list(rows)[:max_rows]
    tabel = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    tabel.add_column("Simbol", no_wrap=True)
    tabel.add_column("Arah", no_wrap=True)
    tabel.add_column("Skor", justify="right", no_wrap=True)
    if wide:
        tabel.add_column("Tren", justify="right", no_wrap=True)
        tabel.add_column("Vol", justify="right", no_wrap=True)
        tabel.add_column("Buku", justify="right", no_wrap=True)
    tabel.add_column("Keterangan", overflow="ellipsis", no_wrap=True)

    for row in daftar:
        if row.selected:
            keterangan = Text("DIPILIH", style="bold green")
        elif row.reason:
            keterangan = Text(row.reason, style="dim")
        else:
            keterangan = Text("menunggu", style="dim")
        sel: list[Any] = [
            row.symbol,
            Text(row.direction.upper(), style=_UP if row.direction == "long" else _DOWN),
            f"{row.score:.2f}",
        ]
        if wide:
            sel.extend([f"{row.trend:.2f}", f"{row.volume:.2f}", f"{row.book:.2f}"])
        sel.append(keterangan)
        tabel.add_row(*sel)
    isi: RenderableType = tabel if daftar else Text("belum ada kandidat", style="dim")
    return Panel(isi, title=f"KANDIDAT (maks {max_rows})", border_style="magenta", box=box.ROUNDED)


# ------------------------------------------------------ panel insiden + log
def _event_table(baris: Iterable[EventLine], *, kosong: str) -> RenderableType:
    daftar = list(baris)
    if not daftar:
        return Text(kosong, style="dim")
    tabel = Table(box=None, expand=True, pad_edge=False, show_header=False)
    tabel.add_column("Jam", no_wrap=True, style="dim")
    tabel.add_column("Level", no_wrap=True)
    tabel.add_column("Pesan", overflow="ellipsis", no_wrap=True)
    for item in daftar:
        tabel.add_row(
            item.clock,
            Text(item.level, style=_LEVEL_STYLE.get(item.level.upper(), "white")),
            Text(item.text),
        )
    return tabel


def events_panel(
    incidents: Iterable[EventLine],
    logs: Iterable[EventLine],
    *,
    max_logs: int = 12,
    max_incidents: int = 4,
) -> Panel:
    """Panel 4: insiden terbaru (paling penting) + ekor log."""
    insiden = list(incidents)[:max_incidents]
    log = list(logs)[-max_logs:] if max_logs else []
    isi: list[RenderableType] = []
    if insiden:
        isi.append(Text("INSIDEN (perlu perhatian)", style="bold yellow"))
        isi.append(_event_table(insiden, kosong="tidak ada insiden"))
        isi.append(Text(""))
    isi.append(Text("LOG TERAKHIR", style="bold"))
    isi.append(_event_table(log, kosong="belum ada log"))
    judul = f"KEJADIAN ({len(insiden)} insiden)"
    warna = "yellow" if insiden else "white"
    return Panel(Group(*isi), title=judul, border_style=warna, box=box.ROUNDED)


# ----------------------------------------------------------------- gabungan
def render_state(
    state: DashboardState, *, compact: bool = False, width: int | None = None
) -> RenderableType:
    """Susun semua panel menjadi satu renderable.

    ``compact=True`` memakai satu panel: satu baris status + ringkasan posisi.
    Berguna untuk layar sempit atau terminal yang di-log ke berkas.
    """
    if compact:
        return _compact(state)

    lebar = width if width is not None else 120
    lebar_cukup = lebar >= 100
    bagian: list[RenderableType] = [
        status_panel(state.status, state.metrics, wide=lebar_cukup),
        positions_panel(state.positions, show_history=True, wide=lebar_cukup),
        candidates_panel(state.candidates, wide=lebar_cukup),
        events_panel(state.incidents, state.logs),
    ]
    if state.error:
        bagian.insert(0, Text(f"galat dashboard: {state.error}", style="bold red"))
    jam = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.generated_at))
    judul = Text(f"Volumatic VIDYA Bot — {jam}", style="bold")
    return Group(judul, *bagian)


def _compact(state: DashboardState) -> RenderableType:
    s = state.status
    baris = Text()
    baris.append("[")
    baris.append(s.mode.upper(), style=_MODE_STYLE.get(s.mode, "white"))
    baris.append("] ")
    baris.append(f"{s.equity:.2f} USDT ")
    baris.append(
        f"{_angka(s.pnl_usdt, satuan=' USDT')} ({_angka(s.pnl_pct, satuan='%')}) ",
        style=_warna_pnl(s.pnl_usdt),
    )
    baris.append(f"posisi {s.open_positions}/{s.max_positions} ")
    baris.append(f"trade {s.trades_today}/{s.max_trades} ")
    if state.metrics is not None:
        baris.append(f"{state.metrics.bandwidth_mb_per_hour:.1f} MB/jam ")
    if s.killed:
        baris.append("KILL SWITCH", style="bold red")
    elif s.daily_stopped:
        baris.append("BATAS HARIAN", style="bold yellow")

    aktif = state.active_positions
    teks_posisi = (
        " | ".join(
            f"{p.symbol} {p.side.upper()} {_angka(p.pnl_usdt, satuan='U')} "
            f"({_angka(p.pnl_pct, satuan='%')})"
            for p in aktif
        )
        or "tidak ada posisi"
    )
    return Panel(
        Group(baris, Text(teks_posisi)),
        title="RINGKAS",
        border_style=_MODE_STYLE.get(s.mode, "white"),
        box=box.ROUNDED,
    )
