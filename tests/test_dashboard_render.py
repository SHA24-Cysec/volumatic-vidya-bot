"""Test render dashboard: panel, isi, dan perilaku saat data kosong/ekstrem."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from rich.console import Console

from volumatic_bot.dashboard import (
    BotStatus,
    CandidateRow,
    DashboardState,
    EventLine,
    PositionRow,
    demo_state,
    render_state,
)
from volumatic_bot.metrics import Metrics


def _teks(renderable: object, width: int = 120) -> str:
    console = Console(width=width, record=True, force_terminal=False)
    console.print(renderable)  # type: ignore[arg-type]
    return console.export_text()


def _posisi(**ubah: object) -> PositionRow:
    data: dict[str, object] = {
        "symbol": "DOGEUSDT",
        "side": "long",
        "state": "open",
        "qty": Decimal("133"),
        "remaining_qty": Decimal("133"),
        "entry": Decimal("0.3500"),
        "stop": Decimal("0.3444"),
        "tp1": Decimal("0.3584"),
        "mark": Decimal("0.3542"),
    }
    data.update(ubah)
    return PositionRow(**data)  # type: ignore[arg-type]


# ------------------------------------------------------------------ status
def test_status_panel_isi_penting() -> None:
    st = BotStatus(
        mode="dry_run",
        running=True,
        uptime_sec=3720,
        equity=48.9,
        start_equity=50.0,
        daily_loss_usdt=1.1,
        trades_today=4,
        max_trades=20,
        open_positions=1,
        max_positions=2,
        used_margin_pct=41.5,
    )
    teks = _teks(render_state(DashboardState(status=st)))
    assert "DRY_RUN" in teks
    assert "48.90 USDT" in teks
    assert "-1.10 USDT" in teks
    assert "1j 02m" in teks
    assert "41.5%" in teks
    assert "Trade" in teks and "4/20" in teks


def test_status_menampilkan_kill_switch_dan_batas_harian() -> None:
    kill = BotStatus(killed=True, kill_reason="operator", equity=50.0, start_equity=50.0)
    teks = _teks(render_state(DashboardState(status=kill)))
    assert "KILL SWITCH" in teks
    assert "alasan kill: operator" in teks

    harian = BotStatus(daily_stopped=True, equity=48.5, start_equity=50.0)
    assert "BATAS HARIAN" in _teks(render_state(DashboardState(status=harian)))


def test_batas_rugi_sadar_batas_persen() -> None:
    # rugi 1,5 USDT dari ekuitas 50 = 3% = batas penuh
    st = BotStatus(daily_loss_usdt=1.5, equity=48.5, start_equity=50.0, daily_loss_limit_pct=3.0)
    assert st.loss_pct_of_limit == 100.0
    assert st.blocked is False
    assert "100% dari 3%" in _teks(render_state(DashboardState(status=st)))


def test_cooldown_dihitung_dari_sekarang() -> None:
    sampai = datetime.now().astimezone() + timedelta(minutes=5)
    teks = _teks(render_state(DashboardState(status=BotStatus(cooldown_until=sampai))))
    assert "Cooldown" in teks
    assert "-" in teks  # tidak error, minimal kolom cooldown tercetak


def test_metrik_ditampilkan() -> None:
    metrics = Metrics(weight_limit=2400)
    metrics.record_http(rx_bytes=1000)
    metrics.record_used_weight(300)
    snap = metrics.snapshot()
    state = DashboardState(status=BotStatus(equity=50.0, start_equity=50.0), metrics=snap)
    teks = _teks(render_state(state))
    assert "Bandwidth" in teks
    assert "300/2400" in teks


# ------------------------------------------------------------------ posisi
def test_posisi_long_pnl() -> None:
    row = _posisi()
    assert round(row.pnl_usdt or 0, 4) == round((0.3542 - 0.35) * 133, 4)
    assert row.pnl_pct is not None and row.pnl_pct > 0
    teks = _teks(render_state(DashboardState(status=BotStatus(), positions=[row])))
    assert "DOGEUSDT" in teks and "LONG" in teks
    assert "+0.56" in teks


def test_posisi_short_pnl_bernilai_positif_saat_harga_turun() -> None:
    row = _posisi(
        side="short",
        entry=Decimal("3120.5"),
        mark=Decimal("3095.2"),
        remaining_qty=Decimal("0.12"),
        qty=Decimal("0.25"),
    )
    assert row.pnl_usdt is not None and row.pnl_usdt > 0
    assert row.pnl_pct is not None and row.pnl_pct > 0


def test_posisi_tanpa_harga_terakhir_tidak_mengarang() -> None:
    row = _posisi(mark=None)
    assert row.pnl_usdt is None and row.pnl_pct is None and row.dist_stop_pct is None
    teks = _teks(render_state(DashboardState(status=BotStatus(), positions=[row])))
    assert "DOGEUSDT" in teks


def test_tanda_be_dan_partial() -> None:
    assert _posisi(be_done=True).flags == "BE"
    assert _posisi(be_done=True, partial_done=True).flags == "BE partial"
    tutup = _posisi(state="closed", close_reason="stop_loss_be", realized_usdt=0.59)
    assert "stop_loss_be" in tutup.flags
    assert tutup.active is False


def test_tanpa_posisi_menampilkan_pesan_jujur() -> None:
    teks = _teks(render_state(DashboardState(status=BotStatus())))
    assert "tidak ada posisi terbuka" in teks


# ---------------------------------------------------------------- kandidat
def test_kandidat_dipotong_sesuai_batas() -> None:
    rows = [
        CandidateRow(f"SYM{i}USDT", "long", 0.5, 0.4, 0.3, 0.2, True, False, "slot penuh")
        for i in range(15)
    ]
    teks = _teks(render_state(DashboardState(status=BotStatus(), candidates=rows)))
    assert "maks 10" in teks
    assert "SYM0USDT" in teks
    assert "SYM11USDT" not in teks


def test_kandidat_terpilih_dan_alasan() -> None:
    rows = [
        CandidateRow("SOLUSDT", "long", 0.82, 0.71, 0.9, 0.85, True, True),
        CandidateRow("ADAUSDT", "short", 0.4, 0.3, 0.2, 0.1, False, False, "spread lebar"),
    ]
    teks = _teks(render_state(DashboardState(status=BotStatus(), candidates=rows)))
    assert "DIPILIH" in teks
    assert "spread lebar" in teks


# ----------------------------------------------------------------- kejadian
def test_insiden_dan_log() -> None:
    state = DashboardState(
        status=BotStatus(),
        incidents=[EventLine("10:00:00", "CRITICAL", "protection_failed [DOGEUSDT]: SL ditolak")],
        logs=[EventLine("10:00:01", "INFO", "rekonsiliasi bersih")],
    )
    teks = _teks(render_state(state))
    assert "INSIDEN" in teks and "protection_failed" in teks
    assert "LOG TERAKHIR" in teks and "rekonsiliasi bersih" in teks
    assert "1 insiden" in teks


def test_tanpa_insiden_panel_tetap_ada() -> None:
    teks = _teks(render_state(DashboardState(status=BotStatus())))
    assert "0 insiden" in teks
    assert "belum ada log" in teks


def test_log_dipotong_dari_belakang() -> None:
    logs = [EventLine(f"10:00:{i:02d}", "INFO", f"baris {i}") for i in range(30)]
    teks = _teks(render_state(DashboardState(status=BotStatus(), logs=logs)))
    assert "baris 29" in teks
    assert "baris 0\n" not in teks


# ------------------------------------------------------------------ umum
def test_galat_sumber_ditampilkan() -> None:
    teks = _teks(render_state(DashboardState(status=BotStatus(), error="db terkunci")))
    assert "galat dashboard: db terkunci" in teks


def test_compact_satu_panel() -> None:
    state = demo_state()
    teks = _teks(render_state(state, compact=True), width=100)
    assert "RINGKAS" in teks
    assert "posisi 2/2" in teks
    assert "DOGEUSDT LONG" in teks
    assert "STATUS BOT" not in teks


def test_compact_tanpa_posisi() -> None:
    teks = _teks(render_state(DashboardState(status=BotStatus()), compact=True))
    assert "tidak ada posisi" in teks


def test_demo_state_konsisten() -> None:
    state = demo_state()
    assert len(state.active_positions) == 2
    assert state.bandwidth_mb_per_hour > 0
    assert state.status.note.startswith("CONTOH TAMPILAN")
    assert state.realized_today == -1.83
    assert "CONTOH TAMPILAN" in _teks(render_state(state))


def test_lebar_sempit_tidak_error() -> None:
    teks = _teks(render_state(demo_state()), width=60)
    assert "STATUS BOT" in teks
