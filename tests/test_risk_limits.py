"""Test batas harian, cooldown, kill switch, dan ringkasan portofolio."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from volumatic_bot.risk.limits import DailyLimits, DailyState, TradeOutcome
from volumatic_bot.risk.portfolio import Portfolio, PositionExposure

JKT = ZoneInfo("Asia/Jakarta")


def limits(**kwargs: object) -> DailyLimits:
    base: dict[str, object] = {
        "daily_loss_limit_pct": 3.0,
        "max_trades_per_day": 20,
        "consecutive_loss_limit": 3,
        "cooldown_minutes": 120,
        "reset_hour_local": 0,
        "timezone": "Asia/Jakarta",
    }
    base.update(kwargs)
    return DailyLimits(**base)  # type: ignore[arg-type]


def state(start_equity: float = 100.0, **kwargs: object) -> DailyState:
    return DailyState(
        limits=limits(**kwargs), start_equity=start_equity, current_equity=start_equity
    )


def at(hour: int, minute: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=JKT)


def outcome(pnl: float, when: datetime, symbol: str = "BTCUSDT") -> TradeOutcome:
    return TradeOutcome(symbol=symbol, pnl_usdt=pnl, closed_at=when)


# ---------------------------------------------------------------- hari trading
def test_hari_trading_dengan_jam_reset_nol() -> None:
    st = state()
    assert st.trading_day(at(0, 5)) == at(0).date()
    assert st.trading_day(at(23, 59)) == at(0).date()


def test_hari_trading_dengan_jam_reset_tujuh() -> None:
    st = state(reset_hour_local=7)
    # pukul 06:00 masih milik hari sebelumnya
    assert st.trading_day(at(6, 0, day=11)) == at(0, day=10).date()
    assert st.trading_day(at(7, 0, day=11)) == at(0, day=11).date()


def test_roll_mengganti_hari_dan_mereset_ekuitas_awal() -> None:
    st = state()
    st.roll(at(10))
    st.current_equity = 95.0
    st.consecutive_losses = 2
    st.cooldown_until = at(23)
    assert st.roll(at(10, day=11)) is True
    assert st.day == at(0, day=11).date()
    assert st.start_equity == 95.0
    assert st.consecutive_losses == 0
    assert st.cooldown_until is None
    assert st.roll(at(11, day=11)) is False  # hari sama


def test_kill_manual_tetap_aktif_walau_hari_berganti() -> None:
    """Kill switch manual TIDAK boleh hilang sendiri besok pagi."""
    st = state()
    st.roll(at(10))
    st.kill("uji")
    assert st.roll(at(10, day=11)) is True
    assert st.killed is True
    assert st.stopped is True
    st.reset_kill()
    assert st.stopped is False


def test_penghentian_karena_rugi_harian_hilang_saat_hari_baru() -> None:
    st = state(100.0)
    st.roll(at(0, 5))  # awal hari trading 10 Nov
    st.record(outcome(-4.0, at(10)), at(10))
    assert st.daily_stopped is True
    assert st.stopped is True
    st.roll(at(0, 5, day=11))
    assert st.daily_stopped is False
    assert st.stopped is False


def test_naive_datetime_ditolak() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        TradeOutcome(symbol="BTCUSDT", pnl_usdt=1.0, closed_at=datetime(2026, 11, 10, 10, 0))


# --------------------------------------------------------------- statistik
def test_rugi_harian_hanya_menghitung_kerugian_hari_ini() -> None:
    st = state()
    st.record(outcome(-1.5, at(9)), at(9))
    st.record(outcome(2.0, at(10)), at(10))  # profit tidak menutup kerugian
    st.record(outcome(-0.5, at(11)), at(11))
    st.record(outcome(-9.0, at(10, day=9)), at(10))  # hari lain
    assert st.loss_today(at(12)) == pytest.approx(2.0)
    assert st.trades_today(at(12)) == 3
    assert st.daily_loss_pct(at(12)) == pytest.approx(2.0)


def test_ekuitas_berjalan_diperbarui() -> None:
    st = state(100.0)
    st.record(outcome(-2.0, at(9)), at(9))
    st.record(outcome(3.0, at(10)), at(10))
    assert st.current_equity == pytest.approx(101.0)


# ------------------------------------------------------------- keputusan
def test_boleh_buka_posisi_kondisi_normal() -> None:
    st = state()
    keputusan = st.can_open(at(9), open_positions=0, max_positions=2)
    assert keputusan.allowed
    assert "rugi harian" in keputusan.summary()


def test_blokir_karena_rugi_harian() -> None:
    st = state(100.0)
    st.record(outcome(-3.5, at(9)), at(9))  # -3,5%
    keputusan = st.can_open(at(10), open_positions=0, max_positions=2)
    assert keputusan.blocked
    assert any("rugi harian" in r for r in keputusan.reasons)
    assert st.daily_stopped is True
    assert st.killed is False  # bukan kill manual: hilang sendiri besok


def test_blokir_karena_slot_penuh() -> None:
    st = state()
    keputusan = st.can_open(at(9), open_positions=2, max_positions=2)
    assert keputusan.blocked
    assert any("slot posisi penuh" in r for r in keputusan.reasons)


def test_blokir_karena_jumlah_trade_harian() -> None:
    st = state(100.0, max_trades_per_day=2)
    st.record(outcome(0.5, at(9)), at(9))
    st.record(outcome(0.5, at(10)), at(10))
    keputusan = st.can_open(at(11), open_positions=0, max_positions=2)
    assert keputusan.blocked
    assert any("jumlah trade hari ini" in r for r in keputusan.reasons)


def test_rugi_beruntun_memicu_cooldown() -> None:
    st = state(100.0)
    for jam in (9, 10, 11):
        st.record(outcome(-0.5, at(jam)), at(jam))
    assert st.consecutive_losses == 3
    assert st.cooldown_until == at(11) + timedelta(minutes=120)
    keputusan = st.can_open(at(12), open_positions=0, max_positions=2)
    assert keputusan.blocked
    assert any("cooldown" in r for r in keputusan.reasons)
    # setelah cooldown lewat
    assert st.can_open(at(14), open_positions=0, max_positions=2).allowed


def test_profit_mereset_rugi_beruntun() -> None:
    st = state(100.0)
    st.record(outcome(-0.5, at(9)), at(9))
    st.record(outcome(-0.5, at(10)), at(10))
    st.record(outcome(1.0, at(11)), at(11))
    assert st.consecutive_losses == 0
    assert st.cooldown_until is None


def test_kill_dan_reset_manual() -> None:
    st = state(100.0)
    st.kill("operator menghentikan bot")
    assert st.can_open(at(9), open_positions=0, max_positions=2).blocked
    st.reset_kill()
    assert st.can_open(at(9), open_positions=0, max_positions=2).allowed


def test_validasi_batas() -> None:
    with pytest.raises(ValueError, match="daily_loss_limit_pct"):
        limits(daily_loss_limit_pct=0)
    with pytest.raises(ValueError, match="max_trades_per_day"):
        limits(max_trades_per_day=0)
    with pytest.raises(ValueError, match="consecutive_loss_limit"):
        limits(consecutive_loss_limit=0)
    with pytest.raises(ValueError, match="reset_hour_local"):
        limits(reset_hour_local=24)


# --------------------------------------------------------------- portofolio
def portfolio(equity: float = 100.0, positions: tuple[PositionExposure, ...] = ()) -> Portfolio:
    return Portfolio(equity=equity, positions=positions, max_total_margin_pct=60.0)


def test_portofolio_kosong() -> None:
    p = portfolio()
    assert p.open_count == 0
    assert p.used_margin == 0.0
    assert p.free_margin == pytest.approx(60.0)
    assert "posisi=0" in p.summary()


def test_margin_terpakai_dan_bebas() -> None:
    p = portfolio(
        100.0,
        (
            PositionExposure("BTCUSDT", notional=200.0, margin=40.0, leverage=5),
            PositionExposure("ETHUSDT", notional=50.0, margin=10.0, leverage=5),
        ),
    )
    assert p.open_count == 2
    assert p.used_margin == pytest.approx(50.0)
    assert p.used_margin_pct == pytest.approx(50.0)
    assert p.free_margin == pytest.approx(10.0)  # 60 - 50
    assert p.has_symbol("BTCUSDT") is True
    assert p.has_symbol("SOLUSDT") is False
    assert p.symbol_margin("BTCUSDT") == pytest.approx(40.0)
    assert p.symbol_margin_limit() == pytest.approx(30.0)
    assert len(p.to_rows()) == 2


def test_margin_bebas_tidak_negatif() -> None:
    p = portfolio(100.0, (PositionExposure("BTCUSDT", 1000.0, 200.0, 5),))
    assert p.free_margin == 0.0


def test_equity_nol_tidak_menghasilkan_pembagian_nol() -> None:
    p = portfolio(0.0, (PositionExposure("BTCUSDT", 10.0, 2.0, 5),))
    assert p.used_margin_pct == 0.0
    assert p.free_margin == 0.0


def test_equity_negatif_ditolak() -> None:
    with pytest.raises(ValueError, match="equity"):
        Portfolio(equity=-1.0, positions=())
