"""Sumber data dashboard yang memakai state lokal (SQLite + memori).

``LocalStateSource`` tidak mengirim permintaan jaringan apa pun: semua angka
diambil dari
* tabel posisi/insiden (``PositionStore``) yang sudah diisi modul eksekusi,
* snapshot harian terakhir,
* metrik jaringan yang diisi klien REST/WS (kalau ada),
* nilai yang disuntikkan bot saat berjalan (harga terakhir, kandidat terbaru).

Kalau none dari itu belum ada (mis. bot baru dijalankan), dashboard menampilkan
angka nol dan baris "belum ada ...", bukan angka karangan.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from volumatic_bot.dashboard.models import (
    BotStatus,
    CandidateRow,
    DashboardState,
    EventLine,
    PositionRow,
)
from volumatic_bot.positions.store import PositionStore

if TYPE_CHECKING:
    from collections.abc import Iterable

    from volumatic_bot.metrics import Metrics
    from volumatic_bot.risk.limits import DailyState
    from volumatic_bot.signals.ranking import Candidate

__all__ = ["LocalStateSource"]


class LocalStateSource:
    """Rakit :class:`DashboardState` dari data lokal bot."""

    def __init__(
        self,
        store: PositionStore,
        *,
        mode: str = "dry_run",
        max_positions: int = 2,
        max_trades: int = 20,
        daily_loss_limit_pct: float = 3.0,
        max_margin_pct: float = 60.0,
        metrics: Metrics | None = None,
        history_limit: int = 10,
        incident_limit: int = 5,
        timezone: str = "Asia/Jakarta",
        reset_hour_local: int = 0,
    ) -> None:
        self._store = store
        self._mode = mode
        self._max_positions = max_positions
        self._max_trades = max_trades
        self._daily_loss_limit_pct = daily_loss_limit_pct
        self._max_margin_pct = max_margin_pct
        self._metrics = metrics
        self._tz = ZoneInfo(timezone)
        self._reset_hour = int(reset_hour_local) % 24
        self._history = max(0, history_limit)
        self._incident_limit = max(0, incident_limit)

        self.started_at = time.time()
        self.running = True
        self.marks: dict[str, Decimal] = {}
        self.candidates: list[Candidate] = []
        self.log_lines: list[EventLine] = []
        self.daily_state: DailyState | None = None
        self.note = ""

    # ------------------------------------------------------ suntikan bot
    def set_mark(self, symbol: str, price: Decimal) -> None:
        self.marks[symbol] = price

    def set_candidates(self, candidates: Iterable[Candidate]) -> None:
        self.candidates = list(candidates)

    def set_logs(self, lines: Iterable[EventLine]) -> None:
        self.log_lines = list(lines)

    # --------------------------------------------------------------- ambil
    def __call__(self) -> DashboardState:
        return self.snapshot()

    def snapshot(self) -> DashboardState:
        posisi_aktif = self._store.active_positions()
        riwayat = self._store.all_positions(limit=max(self._history, len(posisi_aktif) + 1))
        terlihat = {p.symbol for p in riwayat}
        baris: list[PositionRow] = []
        for pos in riwayat:
            baris.append(PositionRow.from_position(pos, mark=self.marks.get(pos.symbol)))
        for pos in posisi_aktif:
            if pos.symbol not in terlihat:  # jaga-jaga bila riwayat terpotong
                baris.append(PositionRow.from_position(pos, mark=self.marks.get(pos.symbol)))

        catatan = self._store.incidents(limit=self._incident_limit)
        insiden = [EventLine.from_incident(rec) for rec in catatan]
        status = self._status(len(posisi_aktif), baris)
        return DashboardState(
            status=status,
            positions=baris,
            candidates=[CandidateRow.from_candidate(c) for c in self.candidates],
            incidents=insiden,
            logs=list(self.log_lines),
            metrics=self._metrics.snapshot() if self._metrics is not None else None,
        )

    def _status(self, jumlah_aktif: int, baris: list[PositionRow]) -> BotStatus:
        ekuitas, kerugian, trades = self._daily()
        ds = self.daily_state
        margin_terpakai = 0.0
        if ekuitas > 0:
            margin = 0.0
            for row in baris:
                if not row.active or row.mark is None:
                    continue
                notional = abs(float(row.mark * row.remaining_qty))
                margin += notional / 5.0  # leverage default bila tak diketahui
            margin_terpakai = margin / ekuitas * 100.0
        return BotStatus(
            mode=self._mode,
            running=self.running,
            uptime_sec=time.time() - self.started_at,
            equity=ekuitas,
            start_equity=ekuitas + kerugian,
            daily_loss_usdt=kerugian,
            daily_loss_limit_pct=self._daily_loss_limit_pct,
            trades_today=trades,
            max_trades=self._max_trades,
            open_positions=jumlah_aktif,
            max_positions=self._max_positions,
            used_margin_pct=round(margin_terpakai, 1),
            max_margin_pct=self._max_margin_pct,
            killed=bool(ds.killed) if ds is not None else False,
            daily_stopped=bool(ds.daily_stopped) if ds is not None else False,
            cooldown_until=ds.cooldown_until if ds is not None else None,
            kill_reason=(ds.kill_reason if ds is not None else None),
            note=self.note,
        )

    def trading_day(self, now: datetime | None = None) -> str:
        """Tanggal trading (memakai zona waktu & jam reset yang sama dengan risiko)."""
        lokal = (now or datetime.now(tz=self._tz)).astimezone(self._tz)
        if lokal.hour < self._reset_hour:
            lokal -= timedelta(days=1)
        return lokal.date().isoformat()

    def _daily(self) -> tuple[float, float, int]:
        """Ekuitas, kerugian, dan jumlah trade hari ini dari snapshot terakhir."""
        snap = self._store.load_daily(self.trading_day())
        if snap is not None:
            return snap.equity, snap.lost_usdt, snap.trades
        ds = self.daily_state
        if ds is not None:
            now = datetime.now(tz=self._tz)
            return ds.current_equity, max(0.0, ds.loss_today(now)), ds.trades_today(now)
        return 0.0, 0.0, 0
