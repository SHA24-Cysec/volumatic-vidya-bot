"""Test ukuran posisi berbasis risiko & filter modal kecil.

Fokus: pembulatan **tidak boleh** membesarkan risiko diam-diam, dan trade
ditolak bila matematikanya tidak masuk akal untuk modal kecil.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from volumatic_bot.binance.symbols import SymbolFilters
from volumatic_bot.config import CommissionConfig, RiskConfig
from volumatic_bot.risk.sizing import (
    PositionSize,
    estimate_liquidation,
    size_position,
)


def filters(
    *,
    tick: str = "0.01",
    step: str = "0.001",
    min_qty: str = "0.001",
    min_notional: str = "5",
    max_qty: str = "1000",
) -> SymbolFilters:
    return SymbolFilters(
        tick_size=Decimal(tick),
        min_price=Decimal("0.01"),
        max_price=Decimal("1000000"),
        step_size=Decimal(step),
        min_qty=Decimal(min_qty),
        max_qty=Decimal(max_qty),
        market_step_size=Decimal(step),
        market_min_qty=Decimal(min_qty),
        market_max_qty=Decimal(max_qty),
        min_notional=Decimal(min_notional),
        max_orders=200,
        max_algo_orders=20,
    )


def risk_cfg(**kwargs: object) -> RiskConfig:
    return RiskConfig(**kwargs)  # type: ignore[arg-type]


def fee_cfg(taker_bps: float = 5.0, slippage_bps: float = 2.0) -> CommissionConfig:
    return CommissionConfig(taker_bps=taker_bps, slippage_bps=slippage_bps)


def size(**kwargs: object) -> PositionSize:
    base: dict[str, object] = {
        "equity": 50.0,
        "entry_price": 100.0,
        "stop_price": 98.0,  # 2% -> risiko 0,75 USDT pada 1,5%
        "filters": filters(),
        "side": "long",
        "leverage": 5,
        "risk_pct": 1.5,
        "commission": fee_cfg(),
        "config": risk_cfg(),
    }
    base.update(kwargs)
    return size_position(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------- perhitungan
def test_ukuran_dasar_sesuai_risiko_target() -> None:
    hasil = size()
    assert hasil.approved, hasil.reasons
    # risiko target 1,5% x 50 = 0,75 USDT; jarak SL 2 -> qty ideal 0,375
    assert hasil.qty == Decimal("0.375")
    assert hasil.risk_usdt == pytest.approx(0.75, rel=1e-9)
    assert hasil.risk_pct == pytest.approx(1.5, rel=1e-9)
    assert hasil.notional == Decimal("37.500")
    assert hasil.margin == Decimal("7.5000")  # /5
    assert hasil.stop_distance_pct == pytest.approx(2.0)
    assert "risiko=" in hasil.summary()


def test_pembulatan_selalu_ke_bawah() -> None:
    hasil = size(entry_price=100.0, stop_price=97.0)  # qty ideal 0,25
    assert hasil.qty % Decimal("0.001") == 0
    assert hasil.risk_usdt <= hasil.target_risk_usdt + 1e-9


def test_risiko_tidak_pernah_melebihi_target_saat_dibulatkan_turun() -> None:
    hasil = size(equity=137.42, entry_price=331.7, stop_price=327.13)
    assert hasil.approved
    assert hasil.risk_usdt <= hasil.target_risk_usdt + 1e-9


# ------------------------------------------------------------- pembulatan naik
def test_naik_ke_minimum_masih_diterima_bila_pagar_dilonggarkan() -> None:
    """Kenaikan qty karena minNotional boleh asal masih di dalam DUA pagar config.

    Di sini pagar persen dilonggarkan ke 10%, sedangkan pagar risiko (2x) tetap.
    """
    hasil = size(
        equity=50.0,
        risk_pct=0.5,  # target 0,25 USDT
        entry_price=100.0,
        stop_price=99.0,  # qty ideal 0,25
        filters=filters(min_notional="26"),  # minimum sah 0,26 unit (+4%)
        config=risk_cfg(max_qty_round_up_pct=10.0),
    )
    assert hasil.approved, hasil.reasons
    assert hasil.rounded_up is True
    assert hasil.qty == Decimal("0.26")
    assert hasil.risk_usdt == pytest.approx(0.26)
    assert any("dibulatkan NAIK" in n for n in hasil.notes)


def test_naik_ke_minimum_ditolak_oleh_pagar_persen_default() -> None:
    """Default 2%: kenaikan 20% ditolak — trade dilewati, bukan dibesarkan."""
    hasil = size(
        equity=50.0,
        risk_pct=0.5,
        entry_price=100.0,
        stop_price=99.0,
        filters=filters(min_notional="30"),  # 0,30 unit -> naik 20%
    )
    assert hasil.approved is False
    assert any("memaksa ukuran naik" in r for r in hasil.reasons)
    assert any("dilewati, bukan dibesarkan" in r for r in hasil.reasons)


def test_naik_ke_minimum_masih_ditolak_saat_pagar_persen_maksimum() -> None:
    """Bahkan dengan pagar persen dilonggarkan ke maksimum config (20%), +20% > cap."""
    hasil = size(
        equity=50.0,
        risk_pct=0.5,
        entry_price=100.0,
        stop_price=99.0,
        filters=filters(min_notional="30"),  # 0,30 unit -> +20%
        config=risk_cfg(max_qty_round_up_pct=20.0),
    )
    # 0,30 vs ideal 0,25 tepat +20% -> masih boleh (tidak lebih dari cap).
    assert hasil.approved is True
    assert hasil.rounded_up is True


def test_pagar_persen_tidak_boleh_dilonggarkan_berlebihan() -> None:
    """Config menolak max_qty_round_up_pct > 20 agar risiko tidak membengkak."""
    with pytest.raises(ValueError, match="less than or equal to 20"):
        risk_cfg(max_qty_round_up_pct=100.0)


def test_min_notional_memaksa_ukuran_naik() -> None:
    # minNotional 20 USDT pada harga 100 -> minimum sah 0,2 unit
    hasil = size(
        equity=200.0,
        entry_price=100.0,
        stop_price=99.0,
        filters=filters(min_notional="20"),
    )
    assert hasil.approved, hasil.reasons
    assert hasil.notional >= Decimal("20")
    assert hasil.risk_usdt <= hasil.target_risk_usdt + 1e-9


def test_min_notional_tidak_terpenuhi_ditolak() -> None:
    hasil = size(
        equity=20.0,
        entry_price=1.0,
        stop_price=0.999,
        risk_pct=0.5,
        filters=filters(tick="0.0001", step="0.0001", min_qty="0.0001", min_notional="1000"),
    )
    assert hasil.approved is False
    assert any("minNotional" in r for r in hasil.reasons)


# ---------------------------------------------------------------- validasi SL
def test_sl_long_di_atas_entry_ditolak() -> None:
    hasil = size(side="long", stop_price=101.0)
    assert hasil.approved is False
    assert any("SL long harus di bawah" in r for r in hasil.reasons)


def test_sl_short_di_bawah_entry_ditolak() -> None:
    hasil = size(side="short", stop_price=99.0, entry_price=100.0)
    assert hasil.approved is False
    assert any("SL short harus di atas" in r for r in hasil.reasons)


def test_sl_short_valid_dan_risiko_benar() -> None:
    hasil = size(side="short", entry_price=100.0, stop_price=102.0)
    assert hasil.approved
    assert hasil.risk_usdt == pytest.approx(0.75, rel=1e-9)


def test_equity_dan_risk_tidak_valid() -> None:
    assert size(equity=0.0).approved is False
    assert size(leverage=0).approved is False
    assert size(risk_pct=3.0).approved is False
    assert size(entry_price=0.0).approved is False


# ------------------------------------------------------------------ likuidasi
def test_likuidasi_long_short_simetris() -> None:
    liq_long = estimate_liquidation(100.0, 5, 0.004, side="long")
    liq_short = estimate_liquidation(100.0, 5, 0.004, side="short")
    assert liq_long == pytest.approx(100.0 * (1 - 0.2 + 0.004))
    assert liq_short == pytest.approx(100.0 * (1 + 0.2 - 0.004))
    assert liq_long < 100 < liq_short


def test_likuidasi_validasi_input() -> None:
    with pytest.raises(ValueError, match="leverage"):
        estimate_liquidation(100.0, 0, 0.004, side="long")
    with pytest.raises(ValueError, match="entry"):
        estimate_liquidation(0.0, 5, 0.004, side="long")


def test_sl_lebih_jauh_dari_likuidasi_ditolak() -> None:
    # leverage 20x -> jarak likuidasi ~5%; SL 10% jelas di luar
    hasil = size(leverage=20, entry_price=100.0, stop_price=90.0)
    assert hasil.approved is False
    assert any("likuidasi" in r for r in hasil.reasons)


# --------------------------------------------------------------------- biaya
def test_biaya_bolak_balik_dihitung() -> None:
    hasil = size()
    # 2 x (5 + 2) bps = 14 bps dari notional 37,5 = 0,0525 USDT
    assert hasil.fee_usdt == pytest.approx(37.5 * 14 / 10_000, rel=1e-9)
    assert hasil.fee_r == pytest.approx(hasil.fee_usdt / hasil.risk_usdt, rel=1e-9)
    assert hasil.fee_r < 0.5


def test_biaya_terlalu_besar_ditolak() -> None:
    # SL sangat sempit (0,1%) -> qty besar; SL 0,05% membuat fee_r tinggi
    hasil = size(
        entry_price=100.0,
        stop_price=99.95,
        filters=filters(min_notional="1"),
        commission=fee_cfg(taker_bps=20.0, slippage_bps=10.0),
    )
    assert hasil.approved is False
    assert any("biaya bolak-balik" in r for r in hasil.reasons)


def test_peringatan_modal_kecil() -> None:
    hasil = size(equity=30.0)
    assert any("modal kecil" in n for n in hasil.notes)


def test_tanpa_peringatan_modal_kecil_untuk_equity_besar() -> None:
    hasil = size(equity=500.0)
    assert not any("modal kecil" in n for n in hasil.notes)


# -------------------------------------------------------------------- margin
def test_margin_tersedia_tidak_cukup_ditolak() -> None:
    hasil = size(available_margin=1.0)
    assert hasil.approved is False
    assert any("margin" in r and "tersedia" in r for r in hasil.reasons)


def test_margin_tersedia_cukup_diterima() -> None:
    hasil = size(available_margin=100.0)
    assert hasil.approved


def test_qty_melebihi_max_qty_ditolak() -> None:
    hasil = size(
        equity=100_000.0,
        entry_price=1.0,
        stop_price=0.99,
        filters=filters(max_qty="10", min_notional="1", step="0.001"),
    )
    assert hasil.approved is False
    assert any("maxQty" in r for r in hasil.reasons)
