"""Test jalur risiko "fleksibel" untuk modal kecil (permintaan modal 7 USDT).

Yang dijaga di sini bukan angka profit, tetapi **pagar**: setelan agresif harus
ditolak secara bawaan, hanya boleh lewat saklar ``allow_aggressive_risk``, dan
tidak boleh pernah sampai ke 100% ("all in"). Semua uji memakai angka nyata dari
``exchangeInfo`` testnet 15 Nov 2026 (minNotional 5 USDT, qty step kecil).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from volumatic_bot.binance.symbols import SymbolFilters, parse_exchange_info
from volumatic_bot.config import AppConfig, CommissionConfig, RiskConfig, load_config
from volumatic_bot.risk import DailyState, Portfolio, RiskManager, TradeTicket
from volumatic_bot.risk.limits import DailyLimits
from volumatic_bot.risk.sizing import size_position

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"
JKT = ZoneInfo("Asia/Jakarta")

# Setelan "fleksibel" yang direkomendasikan di docs/modal-kecil.md.
FLEKSIBEL: dict[str, float | int | bool] = {
    "risk_per_trade_pct": 5.0,
    "max_per_symbol_margin_pct": 100.0,
    "max_total_margin_pct": 80.0,
    "min_equity_usdt": 5.0,
    "allow_aggressive_risk": True,
}


def exchange() -> Any:
    payload = json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
    return parse_exchange_info(payload)


def config(**overrides: Any) -> AppConfig:
    cfg = load_config(ROOT / "config.yaml")
    dump = cfg.model_dump()
    for key, value in overrides.items():
        dump[key].update(value)
    return AppConfig(**dump)


def filters(*, step: str = "1", min_qty: str = "1", min_notional: str = "5") -> SymbolFilters:
    return SymbolFilters(
        tick_size=Decimal("0.0001"),
        min_price=Decimal("0.0001"),
        max_price=Decimal("1000000"),
        step_size=Decimal(step),
        min_qty=Decimal(min_qty),
        max_qty=Decimal("1000000"),
        market_step_size=Decimal(step),
        market_min_qty=Decimal(min_qty),
        market_max_qty=Decimal("1000000"),
        min_notional=Decimal(min_notional),
        max_orders=200,
        max_algo_orders=20,
    )


def ukuran(risk_pct: float, cfg: RiskConfig, *, equity: float = 7.0) -> Any:
    return size_position(
        equity=equity,
        entry_price=0.35,
        stop_price=0.343,  # SL 2%
        filters=filters(),
        side="long",
        leverage=10,
        risk_pct=risk_pct,
        commission=CommissionConfig(),
        config=cfg,
        available_margin=equity,
    )


# ------------------------------------------------------------ pagar konfigurasi
def test_bawaan_menolak_risiko_di_atas_2_persen() -> None:
    with pytest.raises(ValidationError, match="allow_aggressive_risk"):
        RiskConfig(risk_per_trade_pct=5.0)


def test_saklar_agresif_mengizinkan_risiko_5_persen() -> None:
    cfg = RiskConfig(**FLEKSIBEL)  # type: ignore[arg-type]
    assert cfg.risk_per_trade_pct == 5.0
    assert cfg.allow_aggressive_risk is True


def test_batas_keras_25_persen_tidak_bisa_dilewati_walau_agresif() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 25"):
        RiskConfig(risk_per_trade_pct=100.0, allow_aggressive_risk=True)


def test_margin_total_95_persen_butuh_saklar() -> None:
    with pytest.raises(ValidationError, match="margin total"):
        RiskConfig(max_total_margin_pct=95.0)
    assert RiskConfig(max_total_margin_pct=95.0, allow_aggressive_risk=True)


def test_equity_minimum_di_bawah_20_butuh_saklar() -> None:
    with pytest.raises(ValidationError, match="equity minimum"):
        RiskConfig(min_equity_usdt=7.0)
    assert RiskConfig(min_equity_usdt=7.0, allow_aggressive_risk=True)


# ------------------------------------------------------------------- ukuran qty
def test_sizing_bawaan_tetap_menolak_risiko_besar() -> None:
    hasil = ukuran(5.0, RiskConfig())
    assert not hasil.approved
    assert any("batas 0–2%" in r for r in hasil.reasons)


def test_sizing_agresif_menerima_risiko_5_persen() -> None:
    """Dengan saklar aktif, 5% harus benar-benar menghasilkan ukuran.

    Ini yang dulu mustahil: ``config`` sudah mengizinkan tetapi ``sizing.py``
    masih memakai plafon 2% yang ditulis mati, sehingga semua trade ditolak.
    """
    hasil = ukuran(5.0, RiskConfig(**FLEKSIBEL))  # type: ignore[arg-type]
    assert hasil.approved, hasil.reasons
    assert hasil.risk_pct == pytest.approx(5.0, abs=0.05)
    assert hasil.risk_usdt == pytest.approx(0.35, rel=0.05)  # 5% x 7 USDT
    assert hasil.margin <= 7.0


def test_sizing_agresif_tetap_menolak_di_atas_25_persen() -> None:
    hasil = ukuran(30.0, RiskConfig(allow_aggressive_risk=True))
    assert not hasil.approved
    assert any("batas 0–25%" in r for r in hasil.reasons)


# --------------------------------------------------- manajer risiko modal kecil
def daily(equity: float) -> DailyState:
    return DailyState(
        limits=DailyLimits(
            daily_loss_limit_pct=10.0,
            max_trades_per_day=20,
            consecutive_loss_limit=3,
            cooldown_minutes=120,
            reset_hour_local=0,
            timezone="Asia/Jakarta",
        ),
        start_equity=equity,
        current_equity=equity,
    )


def tiket(equity: float, **risk_over: Any) -> TradeTicket:
    cfg = config(
        risk={**FLEKSIBEL, **risk_over}, exchange={"default_leverage": 10, "max_leverage": 10}
    )
    mgr = RiskManager(
        cfg,
        daily=daily(equity),
        clock=lambda: datetime(2026, 11, 15, 10, 0, tzinfo=JKT),
        kill_switch_path=Path("/tmp/tidak-ada-kill-agresif"),
    )
    return mgr.evaluate(
        symbol="DOGEUSDT",
        side="long",
        entry_price=0.35,
        atr=0.007,
        trail=0.345,
        equity=equity,
        portfolio=Portfolio(equity=equity, positions=()),
        info=exchange().get("DOGEUSDT"),
        open_positions=0,
    )


def test_evaluate_modal_7_tidak_crash_dan_ditolak_oleh_pagar_likuidasi() -> None:
    """Dulu jalur ini meledak ``ValueError("qty harus > 0")`` di build_trade_plan."""
    hasil = tiket(7.0)
    assert hasil.rejected
    assert hasil.plan is None
    assert hasil.reasons  # ada alasan yang bisa dibaca manusia


def test_evaluate_modal_7_dengan_risiko_normal_tetap_tidak_crash() -> None:
    """Risiko kecil pada modal 7 USDT boleh ditolak atau diterima — yang penting konsisten.

    DOGE lolos di 1,5% (qty step kecil), jadi uji ini tidak memaksa penolakan;
    ia hanya memastikan tiket tidak pernah setengah jadi: disetujui berarti
    rencana level ada, ditolak berarti rencana tidak ada.
    """
    hasil = tiket(7.0, risk_per_trade_pct=1.5)
    assert isinstance(hasil, TradeTicket)
    if hasil.approved:
        assert hasil.plan is not None and hasil.size is not None
        assert hasil.size.risk_pct <= 1.5 + 1e-6
    else:
        assert hasil.plan is None
        assert hasil.reasons
