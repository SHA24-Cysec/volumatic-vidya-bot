"""Persistensi state harian ke SQLite (menutup lubang TAHAP 4).

Tanpa ini, ``Ctrl+C`` atau crash menghapus catatan rugi hari ini dan bot bisa
lanjut trading seolah tidak terjadi apa-apa. Fungsi di sini menyimpan ringkasan
(ekuitas awal hari, total kerugian, jumlah trade) per tanggal trading, lalu
memulihkannya saat bot start.

Yang **tidak** dipulihkan: cooldown dan ``kill switch`` manual — keduanya
sengaja tidak disimpan, karena setelah restart operator harus memeriksa dulu.
"""

from __future__ import annotations

from datetime import datetime

from volumatic_bot.positions.store import DailySnapshot, PositionStore
from volumatic_bot.risk.limits import DailyState

__all__ = ["restore_daily_state", "save_daily_state"]


def save_daily_state(store: PositionStore, state: DailyState, now: datetime) -> DailySnapshot:
    """Simpan ringkasan hari berjalan (idempoten; dipanggil tiap trade tutup)."""
    day = state.trading_day(now)
    snap = DailySnapshot(
        day=day.isoformat(),
        equity=state.current_equity,
        lost_usdt=state.loss_today(now),
        trades=state.trades_today(now),
    )
    store.save_daily(snap)
    return snap


def restore_daily_state(
    store: PositionStore, state: DailyState, now: datetime
) -> DailySnapshot | None:
    """Pulihkan kerugian hari ini dari database (bila ada)."""
    day = state.trading_day(now)
    snap = store.load_daily(day.isoformat())
    if snap is None:
        state.roll(now)
        return None
    state.day = day
    state.start_equity = snap.equity + snap.lost_usdt
    state.seed_equity = state.start_equity
    state.current_equity = snap.equity
    state.carried_loss_usdt = snap.lost_usdt
    state.carried_trades = snap.trades
    rugi_pct = snap.lost_usdt / state.start_equity * 100 if state.start_equity > 0 else 0.0
    if rugi_pct >= state.limits.daily_loss_limit_pct:
        state.daily_stopped = True
    return snap
