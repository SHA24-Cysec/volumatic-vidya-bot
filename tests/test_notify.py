"""Test notifikasi (TAHAP 6): level, penyaringan rahasia, penggabungan, konfigurasi."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import pytest

from volumatic_bot.config import AppConfig
from volumatic_bot.notify import (
    ConsoleNotifier,
    NotifierDispatcher,
    NotifyEvent,
    NotifyKind,
    NotifyLevel,
    event_for_candidate,
    event_for_entry,
    event_for_exit,
    event_for_incident,
    event_for_rejected,
)
from volumatic_bot.notify.factory import build_dispatcher
from volumatic_bot.positions.models import PositionState, TrackedPosition
from volumatic_bot.risk.manager import TradeTicket
from volumatic_bot.signals.ranking import Candidate


class KanalPalsu:
    """Kanal uji yang menampung pesan di memori."""

    name = "uji"

    def __init__(self, *, hasil: bool = True, rusak: bool = False) -> None:
        self.pesan: list[NotifyEvent] = []
        self._hasil = hasil
        self._rusak = rusak

    def send(self, event: NotifyEvent) -> bool:
        if self._rusak:
            raise RuntimeError("kanal mati")
        self.pesan.append(event)
        return self._hasil


def _posisi(**ubah: Any) -> TrackedPosition:
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


# ------------------------------------------------------------------ level
def test_urut_level() -> None:
    assert NotifyLevel.INFO < NotifyLevel.WARNING < NotifyLevel.CRITICAL
    assert NotifyLevel.SUCCESS.rank == 20
    assert NotifyLevel.CRITICAL.rank == 40


def test_format_baris_dan_blok() -> None:
    ev = NotifyEvent(
        kind=NotifyKind.ENTRY,
        title="entry LONG DOGEUSDT",
        detail="qty=133",
        level=NotifyLevel.SUCCESS,
        symbol="DOGEUSDT",
    )
    baris = ev.format_line()
    assert "SUCCESS" in baris and "entry LONG DOGEUSDT" in baris and "qty=133" in baris
    blok = ev.format_block()
    assert "simbol : DOGEUSDT" in blok
    assert ev.as_row()["jenis"] == "entry"


# ----------------------------------------------------- penyaringan rahasia
def test_rahasia_disaring_saat_objek_dibuat() -> None:
    ev = NotifyEvent(
        kind=NotifyKind.ERROR,
        title="request gagal signature=abc123def456",
        detail="X-MBX-APIKEY: RAHASIA123 listenKey=abcdef123456",
        level=NotifyLevel.CRITICAL,
    )
    assert "abc123def456" not in ev.title
    assert "RAHASIA123" not in ev.detail
    assert "abcdef123456" not in ev.detail
    assert "***REDACTED***" in ev.title
    assert "***REDACTED***" in ev.detail


# ------------------------------------------------------------- dispatcher
def test_emit_ke_semua_kanal() -> None:
    a, b = KanalPalsu(), KanalPalsu()
    d = NotifierDispatcher([a, b], dedupe_sec=0)
    ev = NotifyEvent(kind=NotifyKind.ENTRY, title="x", level=NotifyLevel.INFO)
    assert d.emit(ev) is True
    assert len(a.pesan) == len(b.pesan) == 1
    assert d.channels == ("uji", "uji")
    assert d.sent == 1


def test_min_level_menyaring() -> None:
    kanal = KanalPalsu()
    d = NotifierDispatcher([kanal], min_level=NotifyLevel.WARNING, dedupe_sec=0)
    info = NotifyEvent(kind=NotifyKind.SIGNAL, title="info", level=NotifyLevel.INFO)
    assert d.emit(info) is False
    assert d.dropped == 1
    kritis = NotifyEvent(kind=NotifyKind.ERROR, title="err", level=NotifyLevel.CRITICAL)
    assert d.emit(kritis) is True
    assert len(kanal.pesan) == 1


def test_notify_on_trade_dan_on_error() -> None:
    kanal = KanalPalsu()
    d = NotifierDispatcher([kanal], dedupe_sec=0, notify_on_trade=False, notify_on_error=False)
    entry = NotifyEvent(kind=NotifyKind.ENTRY, title="entry", level=NotifyLevel.INFO)
    assert d.emit(entry) is False
    sinyal = NotifyEvent(kind=NotifyKind.SIGNAL, title="s", level=NotifyLevel.INFO)
    assert d.emit(sinyal) is False
    assert len(kanal.pesan) == 0
    kanal2 = KanalPalsu()
    d2 = NotifierDispatcher([kanal2], dedupe_sec=0, notify_on_trade=False)
    keluar = NotifyEvent(kind=NotifyKind.EXIT, title="keluar", level=NotifyLevel.INFO)
    assert d2.emit(keluar) is False
    galat = NotifyEvent(kind=NotifyKind.ERROR, title="err", level=NotifyLevel.CRITICAL)
    assert d2.emit(galat) is True


def test_dedupe_menahan_pesan_berulang_lalu_menghitungnya() -> None:
    jam = {"t": 1000.0}
    kanal = KanalPalsu()
    d = NotifierDispatcher([kanal], dedupe_sec=60, clock=lambda: jam["t"])
    ev = NotifyEvent(kind=NotifyKind.ERROR, title="reconnect", level=NotifyLevel.WARNING)
    assert d.emit(ev) is True
    for _ in range(4):
        jam["t"] += 5
        assert d.emit(ev) is False
    assert d.suppressed == 4 and len(kanal.pesan) == 1
    jam["t"] += 100
    assert d.emit(ev) is True
    assert "sebelumnya: 4x" in kanal.pesan[-1].detail


def test_batas_per_menit() -> None:
    jam = {"t": 0.0}
    kanal = KanalPalsu()
    d = NotifierDispatcher([kanal], dedupe_sec=0, max_per_minute=3, clock=lambda: jam["t"])
    for i in range(5):
        jam["t"] += 0.1
        d.emit(NotifyEvent(kind=NotifyKind.SIGNAL, title=f"pesan {i}", level=NotifyLevel.INFO))
    assert len(kanal.pesan) == 3
    assert d.dropped == 2
    jam["t"] += 61  # jendela 1 menit lewat
    assert d.emit(NotifyEvent(kind=NotifyKind.SIGNAL, title="baru", level=NotifyLevel.INFO)) is True


def test_kanal_rusak_tidak_mematikan_bot() -> None:
    baik = KanalPalsu()
    rusak = KanalPalsu(rusak=True)
    d = NotifierDispatcher([rusak, baik], dedupe_sec=0)
    assert d.emit(NotifyEvent(kind=NotifyKind.ENTRY, title="x", level=NotifyLevel.INFO)) is True
    assert len(baik.pesan) == 1


def test_tanpa_kanal_tidak_error() -> None:
    d = NotifierDispatcher([], dedupe_sec=0)
    assert d.emit(NotifyEvent(kind=NotifyKind.ENTRY, title="x", level=NotifyLevel.INFO)) is False


def test_console_notifier_menulis_ke_logger(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="volumatic_bot.notify")
    kanal = ConsoleNotifier()
    ev = NotifyEvent(
        kind=NotifyKind.EXIT, title="keluar DOGEUSDT", level=NotifyLevel.WARNING, symbol="DOGEUSDT"
    )
    assert kanal.send(ev) is True
    assert any("[NOTIF]" in rec.message or "[NOTIF]" in rec.getMessage() for rec in caplog.records)


# ------------------------------------------------------------------ event
def test_event_entry_dan_exit() -> None:
    ev = event_for_entry(_posisi())
    assert ev.kind == NotifyKind.ENTRY and ev.level == NotifyLevel.SUCCESS
    assert "qty=133" in ev.detail and "SL=0.3444" in ev.detail

    tutup = _posisi(
        state=PositionState.CLOSED,
        remaining_qty=Decimal(0),
        realized_usdt=-1.25,
        close_reason="stop_loss",
    )
    keluar = event_for_exit(tutup)
    assert "stop_loss" in keluar.title
    assert "rugi bersih" in keluar.detail
    tanpa = event_for_exit(_posisi(state=PositionState.CLOSED, close_reason=None))
    assert "tanpa alasan tercatat" in tanpa.title
    impas = event_for_exit(_posisi(state=PositionState.CLOSED, realized_usdt=0.0))
    assert "impas" in impas.detail


def test_event_rejected_dan_incident() -> None:
    tiket = TradeTicket(symbol="BTCUSDT", side="long", approved=False, reasons=("margin kurang",))
    ev = event_for_rejected(tiket)
    assert ev.level == NotifyLevel.WARNING and "margin kurang" in ev.detail

    kritis = event_for_incident(
        kind="protection_failed", severity="critical", detail="SL ditolak", symbol="DOGEUSDT"
    )
    assert kritis.level == NotifyLevel.CRITICAL
    assert "protection_failed" in kritis.title
    biasa = event_for_incident(kind="tp_failed", severity="warning", detail="x")
    assert biasa.level == NotifyLevel.WARNING


def test_event_candidate() -> None:
    cand = Candidate(
        signal=_sinyal(),
        book=None,
        trend_component=0.71,
        volume_component=0.9,
        book_component=0.5,
        score=0.8,
    )
    ev = event_for_candidate(cand)
    assert ev.kind == NotifyKind.SIGNAL
    assert "skor=0.800" in ev.detail
    assert ev.dedupe_key == "signal:DOGEUSDT:long"


def _sinyal() -> Any:
    from volumatic_bot.streams.kline_feed import SignalEvent

    return SignalEvent(
        symbol="DOGEUSDT",
        direction="long",
        open_time=1_700_000_000_000,
        close_price=0.35,
        upper=0.36,
        lower=0.34,
        trail=0.345,
        atr=0.007,
        atr_pct=2.0,
        cmo=60.0,
        trend=1,
        vol_delta_pct=10.0,
        vol_delta_win_pct=12.0,
        zone_count=3.0,
        score=0.8,
        bars=1500,
    )


# --------------------------------------------------------------- konfigurasi
def test_factory_kanal_konsol() -> None:
    cfg = AppConfig()
    d = build_dispatcher(cfg)
    assert d.channels == ("console",)


def test_factory_telegram_belum_ada_diberi_peringatan(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="volumatic_bot.notify")
    cfg = AppConfig()
    cfg = cfg.model_copy(
        update={
            "notify": cfg.notify.model_copy(
                update={"telegram_enabled": True, "telegram_chat_id": "123"}
            )
        }
    )
    d = build_dispatcher(cfg)
    assert d.channels == ("console",)
    assert any("belum diimplementasikan" in rec.getMessage() for rec in caplog.records)


def test_factory_menghormati_console_disabled() -> None:
    cfg = AppConfig()
    cfg = cfg.model_copy(
        update={"notify": cfg.notify.model_copy(update={"console_enabled": False})}
    )
    assert build_dispatcher(cfg).channels == ()
