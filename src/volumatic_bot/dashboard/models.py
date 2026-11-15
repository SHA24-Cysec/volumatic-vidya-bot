"""Model data dashboard.

Dashboard sengaja **tidak** menyentuh bursa sama sekali: ia hanya menampilkan
:class:`DashboardState` yang sudah disiapkan pemanggil. Konsekuensinya:

* render bisa diuji tanpa jaringan maupun terminal nyata;
* dashboard yang macet tidak pernah memblokir jalur order;
* sumber datanya bisa DB lokal (``LocalStateSource``) atau data contoh
  (``demo_state``) untuk melihat tampilan tanpa menunggu pasar.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from volumatic_bot.metrics import MetricsSnapshot
from volumatic_bot.positions.models import PositionState, TrackedPosition
from volumatic_bot.positions.store import IncidentRecord
from volumatic_bot.signals.ranking import Candidate

__all__ = [
    "BotStatus",
    "CandidateRow",
    "DashboardState",
    "EventLine",
    "PositionRow",
    "demo_state",
]


@dataclass(frozen=True, slots=True)
class BotStatus:
    """Ringkasan keadaan bot (satu baris informasi per fakta)."""

    mode: str = "dry_run"
    running: bool = True
    uptime_sec: float = 0.0
    equity: float = 0.0
    start_equity: float = 0.0
    daily_loss_usdt: float = 0.0
    daily_loss_limit_pct: float = 3.0
    trades_today: int = 0
    max_trades: int = 20
    open_positions: int = 0
    max_positions: int = 2
    used_margin_pct: float = 0.0
    max_margin_pct: float = 60.0
    killed: bool = False
    daily_stopped: bool = False
    cooldown_until: datetime | None = None
    note: str = ""
    kill_reason: str | None = None

    @property
    def pnl_usdt(self) -> float:
        return self.equity - self.start_equity

    @property
    def pnl_pct(self) -> float:
        if self.start_equity <= 0:
            return 0.0
        return self.pnl_usdt / self.start_equity * 100.0

    @property
    def loss_pct_of_limit(self) -> float:
        """Seberapa dekat kerugian harian dengan batasnya (0..>100)."""
        if self.daily_loss_limit_pct <= 0:
            return 0.0
        loss_pct = max(0.0, -self.pnl_pct)
        return loss_pct / self.daily_loss_limit_pct * 100.0

    @property
    def blocked(self) -> bool:
        return self.killed or self.daily_stopped


@dataclass(frozen=True, slots=True)
class PositionRow:
    """Satu posisi seperti yang perlu dilihat operator."""

    symbol: str
    side: str
    state: str
    qty: Decimal
    remaining_qty: Decimal
    entry: Decimal
    stop: Decimal
    tp1: Decimal
    mark: Decimal | None = None
    realized_usdt: float | None = None
    close_reason: str | None = None
    partial_done: bool = False
    be_done: bool = False
    mode: str = "dry_run"

    @classmethod
    def from_position(cls, pos: TrackedPosition, *, mark: Decimal | None = None) -> PositionRow:
        return cls(
            symbol=pos.symbol,
            side=pos.side,
            state=str(pos.state),
            qty=pos.qty,
            remaining_qty=pos.remaining_qty,
            entry=pos.entry_price,
            stop=pos.stop_price,
            tp1=pos.tp1_price,
            mark=mark,
            realized_usdt=pos.realized_usdt,
            close_reason=pos.close_reason,
            partial_done=pos.partial_done,
            be_done=pos.be_done,
            mode=pos.mode,
        )

    @property
    def active(self) -> bool:
        return PositionState(self.state).aktif

    @property
    def pnl_usdt(self) -> float | None:
        """Laba/rugi belum terealisasi dari harga terakhir yang diketahui."""
        if self.mark is None:
            return None
        arah = 1 if self.side == "long" else -1
        return float((self.mark - self.entry) * self.remaining_qty * arah)

    @property
    def pnl_pct(self) -> float | None:
        if self.mark is None or self.entry == 0:
            return None
        arah = 1 if self.side == "long" else -1
        return float((self.mark - self.entry) / self.entry * arah * 100)

    @property
    def dist_stop_pct(self) -> float | None:
        """Jarak harga terakhir ke SL (negatif = sudah melewati SL)."""
        if self.mark is None or self.entry == 0:
            return None
        return float((self.stop - self.mark) / self.mark * 100)

    #: Singkatan untuk layar sempit (terminal < 100 kolom).
    _SHORT_REASON: ClassVar[dict[str, str]] = {
        "stop_loss": "SL",
        "stop_loss_be": "SL-BE",
        "take_profit": "TP",
        "liquidated": "LIQ",
        "hilang_di_bursa": "HILANG",
        "protection_failed": "PROT",
        "kill_switch": "KILL",
        "manual": "MANUAL",
    }

    @property
    def flags_short(self) -> str:
        """Sama seperti :attr:`flags` tetapi memakai singkatan."""
        bagian: list[str] = []
        if not self.active:
            # Beri tanda tegas bahwa baris ini RIWAYAT, bukan posisi terbuka.
            # Tanpa ini, panel "POSISI (0 aktif)" tetap menampilkan baris
            # "SL" dan mudah disalahartikan sebagai posisi yang masih hidup.
            alasan = self.close_reason or self.state
            return "selesai:" + self._SHORT_REASON.get(alasan, alasan[:6])
        if self.be_done:
            bagian.append("BE")
        if self.partial_done:
            bagian.append("P")
        return " ".join(bagian) if bagian else "-"

    @property
    def flags(self) -> str:
        bagian: list[str] = []
        if not self.active:
            return f"selesai: {self.close_reason or self.state}"
        if self.be_done:
            bagian.append("BE")
        if self.partial_done:
            bagian.append("partial")
        return " ".join(bagian) if bagian else "-"


@dataclass(frozen=True, slots=True)
class CandidateRow:
    """Kandidat dari pemindai (TAHAP 3) dalam bentuk siap tampil."""

    symbol: str
    direction: str
    score: float
    trend: float
    volume: float
    book: float
    tradable: bool
    selected: bool
    reason: str | None = None

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> CandidateRow:
        return cls(
            symbol=candidate.symbol,
            direction=candidate.direction,
            score=candidate.score,
            trend=candidate.trend_component,
            volume=candidate.volume_component,
            book=candidate.book_component,
            tradable=candidate.tradable,
            selected=candidate.selected,
            reason=candidate.rejection_reason(),
        )


@dataclass(frozen=True, slots=True)
class EventLine:
    """Satu baris kejadian (log atau insiden) untuk panel bawah."""

    clock: str
    level: str
    text: str

    @classmethod
    def from_incident(cls, rec: IncidentRecord) -> EventLine:
        jam = time.strftime("%H:%M:%S", time.localtime(rec.created_at / 1000))
        simbol = f" [{rec.symbol}]" if rec.symbol else ""
        return cls(clock=jam, level=rec.severity.upper(), text=f"{rec.kind}{simbol}: {rec.detail}")

    @property
    def severity_rank(self) -> int:
        return {"DEBUG": 0, "INFO": 1, "SUCCESS": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 3}.get(
            self.level.upper(), 1
        )


@dataclass(slots=True)
class DashboardState:
    """Semua yang ditampilkan dashboard pada satu waktu."""

    status: BotStatus
    positions: list[PositionRow] = field(default_factory=list)
    candidates: list[CandidateRow] = field(default_factory=list)
    incidents: list[EventLine] = field(default_factory=list)
    logs: list[EventLine] = field(default_factory=list)
    metrics: MetricsSnapshot | None = None
    generated_at: float = field(default_factory=time.time)
    error: str | None = None

    @property
    def active_positions(self) -> list[PositionRow]:
        return [p for p in self.positions if p.active]

    @property
    def realized_today(self) -> float:
        return sum(p.realized_usdt or 0.0 for p in self.positions if p.realized_usdt is not None)

    @property
    def bandwidth_mb_per_hour(self) -> float:
        return self.metrics.bandwidth_mb_per_hour if self.metrics else 0.0


def demo_state(now: float | None = None) -> DashboardState:
    """Data contoh untuk melihat tampilan tanpa jaringan dan tanpa posisi nyata.

    Angka di sini **karangan dan hanya untuk menguji tampilan** — bukan hasil
    backtest, bukan sinyal pasar.
    """
    from decimal import Decimal as D

    stempel = now if now is not None else time.time()
    posisi = [
        PositionRow(
            symbol="DOGEUSDT",
            side="long",
            state="open",
            qty=D("133"),
            remaining_qty=D("133"),
            entry=D("0.3500"),
            stop=D("0.3506"),
            tp1=D("0.3584"),
            mark=D("0.3542"),
            be_done=True,
            mode="dry_run",
        ),
        PositionRow(
            symbol="ETHUSDT",
            side="short",
            state="partial",
            qty=D("0.25"),
            remaining_qty=D("0.12"),
            entry=D("3120.5"),
            stop=D("3120.5"),
            tp1=D("3080.0"),
            mark=D("3095.2"),
            partial_done=True,
            be_done=True,
            mode="dry_run",
        ),
        PositionRow(
            symbol="XRPUSDT",
            side="long",
            state="closed",
            qty=D("800"),
            remaining_qty=D("0"),
            entry=D("2.1500"),
            stop=D("2.1200"),
            tp1=D("2.2100"),
            mark=None,
            realized_usdt=-1.83,
            close_reason="stop_loss",
            mode="dry_run",
        ),
    ]
    kandidat = [
        CandidateRow("SOLUSDT", "long", 0.82, 0.71, 0.90, 0.85, True, True),
        CandidateRow("ADAUSDT", "long", 0.64, 0.55, 0.70, 0.68, True, False, "slot posisi penuh"),
        CandidateRow(
            "AVAXUSDT", "short", 0.51, 0.40, 0.62, 0.55, False, False, "spread 0,14% > 0,08%"
        ),
    ]
    insiden = [
        EventLine(
            "17:02:11", "WARNING", "tp_failed [SOLUSDT]: TP ditolak -2021, posisi tetap punya SL"
        ),
    ]
    log = [
        EventLine("17:03:02", "INFO", "scan 30 simbol: 2 sinyal baru, 1 kandidat dipilih"),
        EventLine("17:03:31", "INFO", "kline ditutup BNBUSDT 15m (tidak ada sinyal)"),
        EventLine("17:04:02", "WARNING", "reconnect stream kline (percobaan 2/5)"),
        EventLine("17:04:44", "INFO", "rekonsiliasi: tidak ada perbedaan"),
    ]
    return DashboardState(
        status=BotStatus(
            mode="dry_run",
            running=True,
            uptime_sec=3 * 3600 + 12 * 60,
            equity=48.90,
            start_equity=50.00,
            daily_loss_usdt=1.10,
            daily_loss_limit_pct=3.0,
            trades_today=4,
            max_trades=20,
            open_positions=2,
            max_positions=2,
            used_margin_pct=41.5,
            max_margin_pct=60.0,
            note="CONTOH TAMPILAN (--demo), bukan data pasar nyata",
        ),
        positions=posisi,
        candidates=kandidat,
        incidents=insiden,
        logs=log,
        metrics=MetricsSnapshot(
            http_requests=142,
            http_retries=3,
            http_errors=1,
            http_rx_bytes=612_000,
            http_tx_bytes=48_000,
            ws_messages=18_420,
            ws_rx_bytes=9_800_000,
            ws_reconnects=1,
            ws_latency_ms=180.0,
            ws_latency_ema_ms=165.0,
            used_weight=210,
            weight_limit=2400,
            bandwidth_mb_per_hour=11.4,
            uptime_sec=3 * 3600 + 12 * 60,
        ),
        generated_at=stempel,
    )
