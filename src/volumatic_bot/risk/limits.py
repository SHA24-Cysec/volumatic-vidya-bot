"""Batas harian: rugi harian, jumlah trade, rugi beruntun, cooldown, kill switch.

Semua keputusan di sini **konservatif**: bila ragu, tolak membuka posisi baru.
Penutupan posisi yang sudah berjalan **tidak** pernah diblokir oleh modul ini —
memblokir exit justru berbahaya.

Jam "hari" bisa diatur (``risk.daily_loss_reset_hour_local``): pukul 00:00 lokal
menurut zona waktu di ``app.timezone``. Perhitungan dilakukan dengan
``datetime`` yang sadar zona, bukan aritmetika detik, supaya perubahan DST
(untuk zona yang memakainya) tidak menggeser batas hari.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = ["DailyLimits", "DailyState", "LimitDecision", "TradeOutcome"]


@dataclass(frozen=True, slots=True)
class DailyLimits:
    """Batas yang berasal dari :class:`volumatic_bot.config.RiskConfig`."""

    daily_loss_limit_pct: float = 3.0
    max_trades_per_day: int = 20
    consecutive_loss_limit: int = 3
    cooldown_minutes: int = 120
    reset_hour_local: int = 0
    timezone: str = "Asia/Jakarta"

    def __post_init__(self) -> None:
        if self.daily_loss_limit_pct <= 0:
            raise ValueError("daily_loss_limit_pct harus > 0")
        if self.max_trades_per_day <= 0:
            raise ValueError("max_trades_per_day harus > 0")
        if self.consecutive_loss_limit <= 0:
            raise ValueError("consecutive_loss_limit harus > 0")
        if not 0 <= self.reset_hour_local <= 23:
            raise ValueError("reset_hour_local harus 0..23")


@dataclass(frozen=True, slots=True)
class TradeOutcome:
    """Hasil satu trade yang sudah ditutup (dipakai untuk statistik harian)."""

    symbol: str
    pnl_usdt: float  # sudah bersih biaya
    closed_at: datetime

    def __post_init__(self) -> None:
        if self.closed_at.tzinfo is None:
            raise ValueError("closed_at harus sadar zona waktu (tz-aware)")


@dataclass(frozen=True, slots=True)
class LimitDecision:
    """Boleh/tidak membuka posisi baru, beserta alasannya."""

    allowed: bool
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def summary(self) -> str:
        if self.allowed:
            return "boleh membuka posisi baru" + (
                f" ({'; '.join(self.notes)})" if self.notes else ""
            )
        return "DIBLOKIR: " + "; ".join(self.reasons)


@dataclass(slots=True)
class DailyState:
    """Status harian bot: ekuitas awal hari, daftar hasil trade, kill switch.

    Ada **dua** penghentian yang dibedakan dengan sengaja:

    * ``daily_stopped`` — otomatis, karena batas rugi harian tercapai. Hilang
      sendiri saat hari trading berganti.
    * ``killed`` — penghentian yang dipicu operator atau insiden lain. **Tidak**
      hilang saat hari berganti; hanya ``reset_kill()`` yang menghapusnya, supaya
      bot tidak diam-diam hidup lagi besok pagi.
    """

    limits: DailyLimits
    start_equity: float
    current_equity: float
    day: date | None = None
    killed: bool = False
    kill_reason: str | None = None
    daily_stopped: bool = False
    cooldown_until: datetime | None = None
    consecutive_losses: int = 0
    outcomes: deque[TradeOutcome] = field(default_factory=lambda: deque(maxlen=500))
    #: Kerugian & jumlah trade yang dipulihkan dari database setelah restart.
    carried_loss_usdt: float = 0.0
    carried_trades: int = 0
    #: Saldo awal sesi ini. Disimpan terpisah dari ``start_equity`` yang berubah
    #: saat hari berganti, supaya kurva ekuitas backtest/laporan tetap utuh.
    seed_equity: float = 0.0

    # ------------------------------------------------------------------ hari
    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.limits.timezone)

    def trading_day(self, now: datetime) -> date:
        """Tanggal "hari trading" untuk ``now`` (memperhitungkan jam reset)."""
        local = now.astimezone(self._tz())
        if local.hour < self.limits.reset_hour_local:
            return (local - timedelta(days=1)).date()
        return local.date()

    def roll(self, now: datetime) -> bool:
        """Mulai hari trading baru bila perlu. True bila hari berganti."""
        day = self.trading_day(now)
        if self.day == day:
            return False
        self.day = day
        self.start_equity = self.current_equity
        self.consecutive_losses = 0
        self.cooldown_until = None
        self.daily_stopped = False
        self.carried_loss_usdt = 0.0
        self.carried_trades = 0
        return True

    # --------------------------------------------------------------- statistik
    def loss_today(self, now: datetime) -> float:
        """Total kerugian hari ini (USDT, hanya nilai negatif yang dihitung).

        Termasuk ``carried_loss_usdt`` yang dipulihkan dari database.
        """
        day = self.trading_day(now)
        dari_memori = -sum(
            o.pnl_usdt
            for o in self.outcomes
            if o.pnl_usdt < 0 and self.trading_day(o.closed_at) == day
        )
        return self.carried_loss_usdt + dari_memori

    def trades_today(self, now: datetime) -> int:
        day = self.trading_day(now)
        dari_memori = sum(1 for o in self.outcomes if self.trading_day(o.closed_at) == day)
        return self.carried_trades + dari_memori

    def daily_loss_pct(self, now: datetime) -> float:
        if self.start_equity <= 0:
            return 0.0
        return self.loss_today(now) / self.start_equity * 100.0

    # ------------------------------------------------------------------ event
    def record(self, outcome: TradeOutcome, now: datetime) -> None:
        """Catat hasil trade dan perbarui cooldown/kill switch."""
        self.roll(now)
        self.outcomes.append(outcome)
        self.current_equity += outcome.pnl_usdt
        if outcome.pnl_usdt < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.limits.consecutive_loss_limit:
                self.cooldown_until = now + timedelta(minutes=self.limits.cooldown_minutes)
        elif outcome.pnl_usdt > 0:
            self.consecutive_losses = 0
        if self.daily_loss_pct(now) >= self.limits.daily_loss_limit_pct:
            # Penghentian otomatis: hilang sendiri saat hari trading berganti.
            self.daily_stopped = True

    def kill(self, reason: str) -> None:
        """Kill switch manual: hentikan pembukaan posisi baru sampai direset."""
        self.killed = True
        self.kill_reason = reason

    @property
    def stopped(self) -> bool:
        """True bila ada penghentian aktif (manual **atau** batas harian)."""
        return self.killed or self.daily_stopped

    def reset_kill(self) -> None:
        """Reset manual (dipakai operator setelah memeriksa keadaan)."""
        self.killed = False
        self.kill_reason = None

    # --------------------------------------------------------------- keputusan
    def can_open(self, now: datetime, *, open_positions: int, max_positions: int) -> LimitDecision:
        """Boleh membuka posisi baru? Tidak pernah memblokir penutupan posisi."""
        self.roll(now)
        reasons: list[str] = []
        notes: list[str] = []

        if self.killed:
            reasons.append(
                f"kill switch manual aktif: {self.kill_reason or 'tanpa alasan tercatat'} "
                "(hapus file kill switch / panggil reset_kill)"
            )
        if self.daily_stopped:
            reasons.append("batas rugi harian tercapai; pembukaan posisi dihentikan hari ini")
        loss_pct = self.daily_loss_pct(now)
        if loss_pct >= self.limits.daily_loss_limit_pct:
            reasons.append(
                f"rugi harian {loss_pct:.2f}% >= batas {self.limits.daily_loss_limit_pct:.2f}%"
            )
        trades = self.trades_today(now)
        if trades >= self.limits.max_trades_per_day:
            reasons.append(
                f"jumlah trade hari ini {trades} >= batas {self.limits.max_trades_per_day}"
            )
        if self.cooldown_until is not None and now < self.cooldown_until:
            sisa = (self.cooldown_until - now).total_seconds() / 60.0
            reasons.append(
                f"cooldown setelah {self.consecutive_losses} rugi beruntun; sisa {sisa:.0f} menit"
            )
        if open_positions >= max_positions:
            reasons.append(f"slot posisi penuh ({open_positions}/{max_positions})")
        if not reasons:
            notes.append(
                f"rugi harian {loss_pct:.2f}% dari batas {self.limits.daily_loss_limit_pct:.2f}% | "
                f"trade {trades}/{self.limits.max_trades_per_day}"
            )
        return LimitDecision(allowed=not reasons, reasons=tuple(reasons), notes=tuple(notes))
