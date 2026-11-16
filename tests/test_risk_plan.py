"""Test rencana level trade: SL, TP1, break-even sadar biaya, trailing, partial."""

from __future__ import annotations

from decimal import Decimal

import pytest

from volumatic_bot.binance.symbols import SymbolFilters
from volumatic_bot.config import CommissionConfig, TradeConfig
from volumatic_bot.risk.plan import build_trade_plan, round_trip_cost_pct


def filters(tick: str = "0.01", step: str = "0.001") -> SymbolFilters:
    return SymbolFilters(
        tick_size=Decimal(tick),
        min_price=Decimal("0.01"),
        max_price=Decimal("1000000"),
        step_size=Decimal(step),
        min_qty=Decimal(step),
        max_qty=Decimal("1000"),
        market_step_size=Decimal(step),
        market_min_qty=Decimal(step),
        market_max_qty=Decimal("1000"),
        min_notional=Decimal("5"),
        max_orders=200,
        max_algo_orders=20,
    )


def commission(taker_bps: float = 5.0, slippage_bps: float = 2.0) -> CommissionConfig:
    return CommissionConfig(taker_bps=taker_bps, slippage_bps=slippage_bps)


def plan(**kwargs: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "side": "long",
        "entry_price": 100.0,
        "atr": 1.0,
        "trail": 97.0,
        "qty": 1.0,
        "filters": filters(),
        "trade": TradeConfig(),
        "commission": commission(),
    }
    base.update(kwargs)
    return build_trade_plan(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------- biaya & BE
def test_biaya_bolak_balik_persen() -> None:
    assert round_trip_cost_pct(commission()) == pytest.approx(0.14)  # 2 x (5+2) bps
    assert round_trip_cost_pct(commission(taker_bps=0.0, slippage_bps=0.0)) == 0.0


def test_break_even_long_di_atas_entry_dan_menutup_biaya() -> None:
    hasil = plan(side="long")
    biaya = round_trip_cost_pct(commission()) + TradeConfig().be_buffer_pct
    assert hasil.be_price > hasil.entry_price
    assert float(hasil.be_price) >= 100.0 * (1 + biaya / 100.0)
    assert any("BE digeser" in n for n in hasil.notes)


def test_break_even_short_di_bawah_entry() -> None:
    hasil = plan(side="short", trail=103.0)
    assert hasil.be_price < hasil.entry_price


def test_break_even_memakai_biaya_lebih_besar_bila_fee_naik() -> None:
    murah = plan(commission=commission(taker_bps=2.0, slippage_bps=1.0))
    mahal = plan(commission=commission(taker_bps=10.0, slippage_bps=5.0))
    assert float(mahal.be_price) > float(murah.be_price)


# --------------------------------------------------------------------- SL
def test_sl_atr_long_dibulatkan_ke_bawah() -> None:
    hasil = plan(side="long", trade=TradeConfig(sl_mode="atr", sl_atr_mult=1.5))
    # 100 - 1,5 x 1,0 = 98,5 -> tick 0,01 tetap 98,50
    assert hasil.stop_price == Decimal("98.50")
    assert hasil.sl_source == "atr"
    assert hasil.risk_per_unit == Decimal("1.50")
    assert hasil.stop_distance_pct == pytest.approx(1.5)


def test_sl_atr_short_dibulatkan_ke_atas() -> None:
    hasil = plan(side="short", trail=103.0, trade=TradeConfig(sl_mode="atr"))
    assert hasil.stop_price == Decimal("101.50")


def test_sl_dibulatkan_menjauhi_entry_saat_tick_kasar() -> None:
    hasil = plan(
        side="long",
        atr=1.1,
        filters=filters(tick="0.3"),
        trade=TradeConfig(sl_mode="atr", sl_atr_mult=1.0, sl_min_atr_mult=0.1),
    )
    # 100 - 1,1 = 98,9 -> dibulatkan KE BAWAH ke kelipatan 0,3 -> 98,7
    assert hasil.stop_price == Decimal("98.7")
    assert hasil.risk_per_unit >= Decimal("1.1")  # jarak tidak menyusut


def test_mode_vidya_trail_memakai_garis_indikator() -> None:
    hasil = plan(trail=99.2, trade=TradeConfig(sl_mode="vidya_trail", sl_min_atr_mult=0.1))
    assert hasil.sl_source == "vidya_trail"
    assert hasil.stop_price == Decimal("99.20")


def test_mode_nearest_memilih_yang_terdekat() -> None:
    dekat_trail = plan(trail=99.2, trade=TradeConfig(sl_mode="nearest", sl_min_atr_mult=0.1))
    assert dekat_trail.sl_source == "vidya_trail"
    # trail jauh (jarak 5) kalah dari SL ATR (jarak 1,5) -> mode nearest pilih ATR
    dekat_atr = plan(trail=95.0, trade=TradeConfig(sl_mode="nearest", sl_min_atr_mult=0.1))
    assert dekat_atr.sl_source == "atr"
    assert dekat_atr.risk_per_unit == Decimal("1.50")


def test_sl_dilebarkan_bila_lebih_sempit_dari_min_atr() -> None:
    hasil = plan(
        trail=99.9,
        trade=TradeConfig(sl_mode="vidya_trail", sl_atr_mult=1.5, sl_min_atr_mult=0.8),
    )
    assert hasil.risk_per_unit >= Decimal("0.8")
    assert hasil.sl_source.endswith("+min_atr")
    assert any("dilebarkan" in n for n in hasil.notes)


def test_trail_di_sisi_salah_diabaikan() -> None:
    hasil = plan(side="long", trail=101.0, trade=TradeConfig(sl_mode="nearest"))
    assert hasil.sl_source == "atr"
    assert any("sisi yang salah" in n for n in hasil.notes)


def test_sl_long_harus_di_bawah_entry() -> None:
    hasil = plan(side="long")
    assert hasil.stop_price < hasil.entry_price
    hasil_short = plan(side="short", trail=103.0)
    assert hasil_short.stop_price > hasil_short.entry_price


def test_jarak_sl_lebih_kecil_dari_satu_tick_dinaikkan() -> None:
    hasil = plan(
        side="long",
        atr=0.0001,
        filters=filters(tick="1"),
        trade=TradeConfig(sl_mode="atr", sl_atr_mult=1.0, sl_min_atr_mult=0.00001),
    )
    assert hasil.risk_per_unit == Decimal("1")


# ------------------------------------------------------------- TP & trailing
def test_tp1_setara_r_yang_dikonfigurasi() -> None:
    hasil = plan(trade=TradeConfig(tp1_r=1.5, sl_mode="atr", sl_atr_mult=1.0))
    assert hasil.tp1_price == Decimal("101.50")  # entry 100 + 1,5 x 1,0
    assert hasil.target_price(2.0) == Decimal("102.00")
    short = plan(side="short", trail=103.0, trade=TradeConfig(tp1_r=1.5, sl_atr_mult=1.0))
    assert short.target_price(2.0) == Decimal("98.00")


def test_trailing_activation_dan_parameter() -> None:
    hasil = plan(trade=TradeConfig(trailing_activation_r=1.0, sl_mode="atr", sl_atr_mult=1.0))
    assert hasil.trailing_activation_price == Decimal("101.00")
    assert hasil.trailing_callback_pct == TradeConfig().trailing_callback_rate_pct
    assert hasil.trailing_atr_mult == TradeConfig().trailing_atr_mult


def test_be_trigger_lebih_dekat_dari_tp1() -> None:
    trade = TradeConfig(be_trigger_r=1.0, tp1_r=1.5, sl_mode="atr", sl_atr_mult=1.0)
    hasil = plan(trade=trade)
    assert hasil.be_trigger_price < hasil.tp1_price
    assert hasil.be_trigger_price == Decimal("101.00")


# ------------------------------------------------------------- partial TP
def test_partial_tp_membagi_qty() -> None:
    hasil = plan(qty=1.0, trade=TradeConfig(partial_tp_enabled=True, partial_tp_pct=50.0))
    assert hasil.partial_qty == Decimal("0.500")
    assert hasil.remaining_qty == Decimal("0.500")


def test_partial_tp_dimatikan() -> None:
    hasil = plan(qty=1.0, trade=TradeConfig(partial_tp_enabled=False))
    assert hasil.partial_qty == Decimal("0")
    assert hasil.remaining_qty == Decimal("1.000")


def test_partial_tp_dilewati_bila_qty_terlalu_kecil() -> None:
    hasil = plan(qty=0.001, trade=TradeConfig(partial_tp_pct=50.0))
    assert hasil.partial_qty == Decimal("0")
    assert any("partial TP dilewati" in n for n in hasil.notes)


# ----------------------------------------------------------------- validasi
def test_atr_nol_ditolak() -> None:
    with pytest.raises(ValueError, match="atr"):
        plan(atr=0.0)


def test_qty_nol_ditolak() -> None:
    with pytest.raises(ValueError, match="qty"):
        plan(qty=0.0)


def test_summary_berisi_level_penting() -> None:
    teks = plan().summary()
    for kata in ("BELI", "entry=", "SL=", "TP1=", "BE=", "trail_aktif="):
        assert kata in teks
    assert "JUAL" in plan(side="short", trail=103.0).summary()


# ------------------------------------------- SL lebih kecil dari satu tick
def test_sl_tidak_menempel_di_entry_bila_tick_lebih_besar_dari_jarak_sl():
    """Regresi: ATR sangat kecil + tickSize besar membuat SL == entry.

    Ditemukan saat backtest 4h TRXUSDT/ARBUSDT: ``round_price`` menghasilkan SL
    sama dengan harga entry, lalu simulator melempar "SL tidak boleh sama dengan
    harga entry". Order seperti itu juga pasti ditolak bursa.
    """
    f = filters(tick="0.000010")  # TRXUSDT
    hasil = plan(
        entry_price=0.101234,
        atr=0.000001,  # ATR 0.000001, tick 0.00001 -> jarak SL < 1 tick
        trail=0.09,
        filters=f,
        trade=TradeConfig(sl_mode="atr", sl_atr_mult=2.0, sl_min_atr_mult=2.0),
    )
    assert hasil.risk_per_unit > 0
    assert hasil.stop_price < hasil.entry_price
    assert hasil.entry_price - hasil.stop_price >= f.tick_size


def test_sl_short_tidak_menempel_di_entry_bila_tick_lebih_besar_dari_jarak_sl():
    f = filters(tick="0.000010")
    hasil = plan(
        side="short",
        entry_price=0.101234,
        atr=0.000001,
        trail=0.11,
        filters=f,
        trade=TradeConfig(sl_mode="atr", sl_atr_mult=2.0, sl_min_atr_mult=2.0),
    )
    assert hasil.risk_per_unit > 0
    assert hasil.stop_price > hasil.entry_price
    assert hasil.stop_price - hasil.entry_price >= f.tick_size
