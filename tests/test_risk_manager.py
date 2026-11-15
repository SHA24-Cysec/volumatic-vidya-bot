"""Test manajer risiko: urutan pemeriksaan, alasan penolakan, dan tiket lengkap."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.config import load_config
from volumatic_bot.risk import (
    DailyState,
    Portfolio,
    PositionExposure,
    RiskManager,
    TradeTicket,
)
from volumatic_bot.risk.limits import DailyLimits, TradeOutcome

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"
JKT = ZoneInfo("Asia/Jakarta")


def exchange() -> Any:
    payload = json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
    return parse_exchange_info(payload)


def config(**overrides: Any) -> Any:
    cfg = load_config(ROOT / "config.yaml")
    if overrides:
        dump = cfg.model_dump()
        for key, value in overrides.items():
            dump[key].update(value)
        from volumatic_bot.config import AppConfig

        cfg = AppConfig(**dump)
    return cfg


def daily_state(start_equity: float = 100.0, **kwargs: Any) -> DailyState:
    base: dict[str, Any] = {
        "daily_loss_limit_pct": 3.0,
        "max_trades_per_day": 20,
        "consecutive_loss_limit": 3,
        "cooldown_minutes": 120,
        "reset_hour_local": 0,
        "timezone": "Asia/Jakarta",
    }
    base.update(kwargs)
    return DailyState(
        limits=DailyLimits(**base), start_equity=start_equity, current_equity=start_equity
    )


def manager(
    cfg: Any | None = None,
    *,
    when: datetime | None = None,
    kill_file: Path | None = None,
    state: DailyState | None = None,
) -> RiskManager:
    waktu = when or datetime(2026, 11, 10, 9, 0, tzinfo=JKT)
    return RiskManager(
        cfg or config(),
        daily=state or daily_state(),
        clock=lambda: waktu,
        kill_switch_path=kill_file,
    )


def ticket(
    mgr: RiskManager,
    *,
    symbol: str = "DOGEUSDT",
    side: str = "long",
    entry: float = 0.35,
    atr: float = 0.007,  # 2% harga
    trail: float | None = 0.345,
    equity: float = 100.0,
    portfolio: Portfolio | None = None,
    open_positions: int = 0,
    info: Any | None = None,
    leverage: int | None = None,
) -> TradeTicket:
    ex = exchange()
    return mgr.evaluate(
        symbol=symbol,
        side=side,
        entry_price=entry,
        atr=atr,
        trail=trail,
        equity=equity,
        portfolio=portfolio or Portfolio(equity=equity, positions=()),
        info=info or ex.get(symbol),
        open_positions=open_positions,
        leverage=leverage,
    )


# ----------------------------------------------------------------- tiket sukses
def test_tiket_lengkap_saat_semua_pemeriksaan_lolos(tmp_path: Path) -> None:
    hasil = ticket(manager(kill_file=tmp_path / "TIDAK_ADA"))
    assert hasil.approved, hasil.reasons
    assert hasil.size is not None and hasil.size.approved
    assert hasil.plan is not None
    assert hasil.plan.stop_price < hasil.size.entry_price
    assert hasil.size.risk_pct <= 1.5 + 1e-9
    assert "DISETUJUI" in hasil.summary()
    assert "ukuran_posisi" in hasil.checks and "rencana_level" in hasil.checks
    row = hasil.as_row()
    assert row["approved"] is True
    assert {"qty", "notional", "margin", "risk_usdt", "stop", "tp1", "be"} <= set(row)


def test_risiko_absolut_sesuai_config(tmp_path: Path) -> None:
    mgr = manager(config(risk={"risk_per_trade_pct": 1.0}), kill_file=tmp_path / "x")
    hasil = ticket(mgr, equity=200.0)
    assert hasil.approved, hasil.reasons
    assert hasil.size is not None
    assert hasil.size.risk_usdt == pytest.approx(2.0, rel=0.25)  # target 1% x 200
    assert hasil.size.risk_usdt <= hasil.size.target_risk_usdt * 1.25


# ------------------------------------------------------------------ penolakan
def test_kill_switch_file_menolak(tmp_path: Path) -> None:
    kill = tmp_path / "KILL_SWITCH"
    kill.write_text("stop", encoding="utf-8")
    hasil = ticket(manager(kill_file=kill))
    assert hasil.rejected
    assert any("kill switch" in r for r in hasil.reasons)
    kill.unlink()
    assert ticket(manager(kill_file=kill)).approved, "kill switch dihapus harusnya boleh"


def test_kill_switch_internal_menolak_penutupan_tidak_diblokir(tmp_path: Path) -> None:
    st = daily_state()
    st.kill("insiden manual")
    hasil = ticket(manager(state=st, kill_file=tmp_path / "x"))
    assert hasil.rejected
    assert any("kill switch manual" in r for r in hasil.reasons)


def test_duplikat_simbol_ditolak(tmp_path: Path) -> None:
    porto = Portfolio(equity=100.0, positions=(PositionExposure("DOGEUSDT", 75.0, 15.0, 5),))
    hasil = ticket(manager(kill_file=tmp_path / "x"), portfolio=porto, open_positions=1)
    assert hasil.rejected
    assert any("sudah ada posisi" in r for r in hasil.reasons)


def test_slot_penuh_ditolak(tmp_path: Path) -> None:
    hasil = ticket(manager(kill_file=tmp_path / "x"), open_positions=2)
    assert hasil.rejected
    assert any("slot posisi penuh" in r for r in hasil.reasons)


def test_batas_rugi_harian_ditolak(tmp_path: Path) -> None:
    st = daily_state(100.0)
    st.roll(datetime(2026, 11, 10, 0, 30, tzinfo=JKT))
    st.record(
        TradeOutcome("BTCUSDT", -4.0, datetime(2026, 11, 10, 8, 0, tzinfo=JKT)),
        datetime(2026, 11, 10, 8, 0, tzinfo=JKT),
    )
    hasil = ticket(manager(state=st, kill_file=tmp_path / "x"))
    assert hasil.rejected
    assert any("rugi harian" in r for r in hasil.reasons)


def test_cooldown_ditolak(tmp_path: Path) -> None:
    st = daily_state(100.0)
    waktu = datetime(2026, 11, 10, 9, 0, tzinfo=JKT)
    for pnl in (-0.2, -0.2, -0.2):
        st.record(TradeOutcome("BTCUSDT", pnl, waktu), waktu)
    hasil = ticket(manager(state=st, kill_file=tmp_path / "x"))
    assert hasil.rejected
    assert any("cooldown" in r for r in hasil.reasons)


def test_leverage_di_atas_batas_config_ditolak(tmp_path: Path) -> None:
    hasil = ticket(manager(kill_file=tmp_path / "x"), leverage=20)
    assert hasil.rejected
    assert any("leverage" in r for r in hasil.reasons)


def test_equity_di_bawah_minimum_ditolak(tmp_path: Path) -> None:
    hasil = ticket(manager(kill_file=tmp_path / "x"), equity=10.0)
    assert hasil.rejected
    assert any("equity" in r for r in hasil.reasons)


def test_simbol_bukan_perpetual_usdt_ditolak(tmp_path: Path) -> None:
    ex = exchange()
    info = ex.get("BTCUSDC") if "BTCUSDC" in ex.symbols else ex.get("BTCUSDT_250926")
    hasil = ticket(manager(kill_file=tmp_path / "x"), info=info, symbol=info.symbol)
    assert hasil.rejected
    assert any("perpetual USDT" in r for r in hasil.reasons)


def test_margin_portofolio_habis_ditolak(tmp_path: Path) -> None:
    porto = Portfolio(equity=100.0, positions=(PositionExposure("ETHUSDT", 600.0, 120.0, 5),))
    hasil = ticket(manager(kill_file=tmp_path / "x"), portfolio=porto, open_positions=1)
    assert hasil.rejected
    assert any("margin" in r for r in hasil.reasons)


def test_side_tidak_valid_melempar(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="side"):
        ticket(manager(kill_file=tmp_path / "x"), side="BELI")


def test_atr_nol_ditolak_tanpa_crash(tmp_path: Path) -> None:
    """ATR 0 membuat jarak SL nol, jadi tiket harus DITOLAK, bukan meledak.

    Dulu jalur ini berakhir di ``build_trade_plan`` yang melempar ``ValueError``.
    Sejak pagar qty=0 dipasang (uji modal 7 USDT, 15 Nov 2026) penolakan
    dikembalikan sebagai tiket biasa supaya pemanggil di engine cukup melewati
    sinyal, bukan menangkap pengecualian.
    """
    hasil = ticket(manager(kill_file=tmp_path / "x"), atr=0.0)
    assert hasil.rejected
    assert hasil.size is not None and hasil.size.qty == 0
    assert hasil.plan is None
    assert any("SL" in r or "nol" in r for r in hasil.reasons)


def test_penolakan_tidak_membocorkan_tiket_setengah_jadi(tmp_path: Path) -> None:
    hasil = ticket(manager(kill_file=tmp_path / "x"), open_positions=5)
    assert hasil.rejected
    assert hasil.size is None
    assert hasil.plan is None
    assert hasil.as_row()["reasons"]


# --------------------------------------------------------------- ukuran kecil
def test_modal_kecil_menghasilkan_ukuran_kecil_dan_peringatan() -> None:
    hasil = ticket(manager(kill_file=Path("/tmp/tidak-ada-kill")), equity=45.0)
    assert hasil.approved, hasil.reasons
    assert hasil.size is not None
    assert float(hasil.size.notional) < 45.0 * 5  # jauh di bawah margin maksimum
    assert any("modal kecil" in n for n in hasil.notes)


def test_btcusdt_ditolak_untuk_modal_100_usdt(tmp_path: Path) -> None:
    """Kenyataan modal kecil: BTCUSDT punya minNotional 100 USDT + margin besar.

    Bot tidak boleh "membesarkan posisi" agar muat — harus melewati simbol ini.
    """
    hasil = ticket(
        manager(kill_file=tmp_path / "x"),
        symbol="BTCUSDT",
        entry=90_000.0,
        atr=900.0,
        trail=89_000.0,
        equity=100.0,
    )
    assert hasil.rejected
    teks = " ".join(hasil.reasons)
    assert "minNotional" in teks or "memaksa ukuran naik" in teks or "margin" in teks


def test_ethusdt_margin_menempel_batas_konsentrasi(tmp_path: Path) -> None:
    """Pada equity 100, margin ETHUSDT tepat menyentuh batas konsentrasi 30%."""
    hasil = ticket(
        manager(kill_file=tmp_path / "x"),
        symbol="ETHUSDT",
        entry=3_000.0,
        atr=30.0,
        trail=2_970.0,
        equity=100.0,
    )
    assert hasil.approved, hasil.reasons
    assert hasil.size is not None
    assert float(hasil.size.margin) == pytest.approx(30.0, rel=1e-6)


def test_ethusdt_lolos_dengan_equity_lebih_besar(tmp_path: Path) -> None:
    hasil = ticket(
        manager(kill_file=tmp_path / "x"),
        symbol="ETHUSDT",
        entry=3_000.0,
        atr=30.0,
        trail=2_970.0,
        equity=1000.0,
    )
    assert hasil.approved, hasil.reasons
    assert hasil.size is not None
    assert float(hasil.size.notional) >= 20.0
