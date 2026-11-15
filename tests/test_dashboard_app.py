"""Test aplikasi dashboard + sumber data lokal + penangkap log."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from volumatic_bot.dashboard import (
    BotStatus,
    DashboardApp,
    DashboardState,
    EventLine,
    LocalStateSource,
    PositionRow,
    RingBufferHandler,
    demo_state,
)
from volumatic_bot.positions.models import PositionState, TrackedPosition
from volumatic_bot.positions.store import DailySnapshot, PositionStore
from volumatic_bot.storage.db import SqliteDatabase


@pytest.fixture
def store(tmp_path: Path) -> Any:
    db = SqliteDatabase(tmp_path / "dash.db")
    db.open()
    yield PositionStore(db)
    db.close()


def _pos(**ubah: Any) -> TrackedPosition:
    data: dict[str, Any] = {
        "symbol": "DOGEUSDT",
        "side": "long",
        "state": PositionState.OPEN,
        "qty": Decimal("133"),
        "remaining_qty": Decimal("133"),
        "entry_price": Decimal("0.35"),
        "stop_price": Decimal("0.3444"),
        "tp1_price": Decimal("0.3584"),
        "risk_per_unit": Decimal("0.0056"),
        "leverage": 5,
        "mode": "dry_run",
        "opened_at": 1,
        "updated_at": 1,
    }
    data.update(ubah)
    return TrackedPosition(**data)


# ------------------------------------------------------------ log buffer
def test_ring_buffer_menyimpan_dan_membatasi() -> None:
    h = RingBufferHandler(maxlen=3)
    log = logging.getLogger("uji.buffer")
    log.handlers = [h]
    log.propagate = False
    log.setLevel(logging.INFO)
    for i in range(5):
        log.info("pesan %d", i)
    baris = h.lines()
    assert [x.text for x in baris] == ["pesan 2", "pesan 3", "pesan 4"]
    assert len(h) == 3
    assert h.lines(limit=1)[0].text == "pesan 4"
    assert h.lines(limit=0) == []
    h.clear()
    assert h.lines() == []


def test_ring_buffer_menyaring_rahasia() -> None:
    h = RingBufferHandler(maxlen=5, secrets=("SUPERSECRET123",))
    log = logging.getLogger("uji.buffer2")
    log.handlers = [h]
    log.propagate = False
    log.setLevel(logging.INFO)
    log.info("kirim api_secret=SUPERSECRET123 ke bursa")
    teks = h.lines()[-1].text
    assert "SUPERSECRET123" not in teks
    assert "***REDACTED***" in teks


def test_ring_buffer_level_tercatat() -> None:
    h = RingBufferHandler(maxlen=5)
    log = logging.getLogger("uji.buffer3")
    log.handlers = [h]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    log.error("gagal")
    assert h.lines()[-1].level == "ERROR"


# -------------------------------------------------------------- sumber data
def test_sumber_lokal_kosong_jujur(store: PositionStore) -> None:
    source = LocalStateSource(store)
    state = source.snapshot()
    assert state.positions == []
    assert state.status.equity == 0.0
    assert state.status.open_positions == 0
    assert state.status.trades_today == 0
    assert state.metrics is None


def test_sumber_lokal_membaca_posisi_dan_insiden(store: PositionStore) -> None:
    store.save_position(_pos())
    store.add_incident(
        kind="protection_failed", detail="SL ditolak", severity="critical", symbol="DOGEUSDT"
    )
    store.save_daily(DailySnapshot(day=_hari_ini(), equity=51.12, lost_usdt=0.0, trades=1))
    source = LocalStateSource(store, max_positions=2, mode="testnet")
    source.set_mark("DOGEUSDT", Decimal("0.3542"))

    state = source.snapshot()
    assert state.status.mode == "testnet"
    assert len(state.active_positions) == 1
    row = state.active_positions[0]
    assert row.mark == Decimal("0.3542")
    assert row.pnl_usdt is not None and row.pnl_usdt > 0
    assert state.status.equity == 51.12
    assert state.status.trades_today == 1
    assert state.status.used_margin_pct > 0
    assert len(state.incidents) == 1
    assert state.incidents[0].level == "CRITICAL"
    assert "protection_failed" in state.incidents[0].text


def test_sumber_lokal_menghormati_hari_trading_dan_zona_waktu(store: PositionStore) -> None:
    source = LocalStateSource(store, timezone="Asia/Jakarta", reset_hour_local=0)
    hari = source.trading_day(datetime.fromisoformat("2026-11-14T17:30:00+00:00"))
    assert hari == "2026-11-15"  # 00:30 WIB tanggal 15

    source2 = LocalStateSource(store, timezone="Asia/Jakarta", reset_hour_local=8)
    hari2 = source2.trading_day(datetime.fromisoformat("2026-11-14T17:30:00+00:00"))
    assert hari2 == "2026-11-14"  # sebelum jam reset 08:00 WIB


def test_sumber_lokal_menerima_kandidat_dan_log(store: PositionStore) -> None:
    source = LocalStateSource(store)
    source.set_logs([EventLine("10:00:00", "INFO", "halo")])
    assert source.snapshot().logs[0].text == "halo"
    assert source.snapshot().candidates == []


def test_daily_state_dipakai_bila_snapshot_tidak_ada(store: PositionStore) -> None:
    from volumatic_bot.risk.limits import DailyLimits, DailyState

    ds = DailyState(
        limits=DailyLimits(timezone="Asia/Jakarta"),
        start_equity=50.0,
        current_equity=48.6,
    )
    source = LocalStateSource(store)
    source.daily_state = ds
    state = source.snapshot()
    assert state.status.equity == 48.6
    assert state.status.killed is False


# ------------------------------------------------------------------- aplikasi
def test_render_once_mengembalikan_teks() -> None:
    console = Console(width=100, record=True, force_terminal=False)
    app = DashboardApp(lambda: demo_state(), console=console)
    teks = app.render_once()
    assert "STATUS BOT" in teks and "POSISI" in teks
    assert app.frames == 1
    assert app.errors == 0


def test_galat_sumber_tidak_melempar() -> None:
    def rusak() -> DashboardState:
        raise RuntimeError("database terkunci")

    console = Console(width=100, force_terminal=False)
    app = DashboardApp(rusak, console=console)
    state = app.take()
    assert state.error == "database terkunci"
    assert state.status.running is False
    assert "sumber data gagal" in state.status.note
    assert app.errors == 1


def test_refresh_harus_positif() -> None:
    with pytest.raises(ValueError, match="refresh_sec"):
        DashboardApp(lambda: demo_state(), refresh_sec=0)


def test_run_dengan_batas_iterasi() -> None:
    console = Console(width=100, force_terminal=False)
    app = DashboardApp(lambda: demo_state(), console=console, refresh_sec=0.01)
    frames = asyncio.run(app.run(iterations=3))
    assert frames == 3
    assert app.frames == 3


def test_run_berhenti_lewat_stop_event() -> None:
    async def skenario() -> int:
        stop = asyncio.Event()
        app = DashboardApp(
            lambda: demo_state(), console=Console(force_terminal=False), refresh_sec=0.01
        )
        task = asyncio.create_task(app.run(stop_event=stop))
        await asyncio.sleep(0.05)
        stop.set()
        return await task

    assert asyncio.run(skenario()) >= 1


def test_renderable_compact() -> None:
    console = Console(width=100, force_terminal=False)
    app = DashboardApp(lambda: demo_state(), console=console, compact=True)
    assert "RINGKAS" in app.text_of(demo_state())
    assert "STATUS BOT" not in app.text_of(demo_state())


def test_position_row_tanpa_harga_aman() -> None:
    row = PositionRow(
        symbol="X",
        side="long",
        state="open",
        qty=Decimal(1),
        remaining_qty=Decimal(1),
        entry=Decimal(1),
        stop=Decimal("0.9"),
        tp1=Decimal("1.1"),
        mark=None,
    )
    assert row.pnl_usdt is None
    assert row.dist_stop_pct is None
    assert row.flags == "-"
    assert BotStatus().pnl_usdt == 0.0


def _hari_ini() -> str:
    source = LocalStateSource.__new__(LocalStateSource)
    source._tz = __import__("zoneinfo").ZoneInfo("Asia/Jakarta")
    source._reset_hour = 0
    return source.trading_day()
