"""Test simulator posisi backtest: urutan SL/TP, partial TP, trailing, biaya."""

from __future__ import annotations

import pytest

from volumatic_bot.backtest.costs import CostModel, FundingSeries
from volumatic_bot.backtest.engine import TradeManager

BIAYA = CostModel(taker_bps=5.0, slippage_bps=0.0, funding_rate_8h_pct=0.0)


def manager(**kwargs: object) -> TradeManager:
    return TradeManager(BIAYA, **kwargs)  # type: ignore[arg-type]


def buka(mgr: TradeManager, **ubah: object) -> None:
    data: dict[str, object] = {
        "symbol": "DOGEUSDT",
        "side": "long",
        "qty": 100.0,
        "price": 1.00,
        "stop_price": 0.98,
        "tp1_price": 1.03,
        "be_price": 1.001,
        "be_trigger_price": 1.02,
        "trailing_activation_price": 1.02,
        "trail_line": 0.99,
        "atr": 0.01,
        "open_ms": 1_000_000,
        "partial_fraction": 0.5,
    }
    data.update(ubah)
    mgr.open(**data)  # type: ignore[arg-type]


def test_validasi_pembukaan() -> None:
    mgr = manager()
    with pytest.raises(ValueError, match="qty"):
        buka(mgr, qty=0)
    with pytest.raises(ValueError, match="side"):
        buka(mgr, side="naik")
    with pytest.raises(ValueError, match="SL"):
        buka(mgr, stop_price=1.00)


def test_tidak_boleh_dua_posisi_sekaligus() -> None:
    mgr = manager()
    buka(mgr)
    with pytest.raises(RuntimeError, match="posisi terbuka"):
        buka(mgr)


def test_sl_lebih_dulu_bila_sl_dan_tp_tersentuh_bersamaan() -> None:
    """Asumsi pesimistis: bila high dan low menembus TP dan SL, SL yang dihitung."""
    mgr = manager()
    buka(mgr)
    alasan = mgr.on_candle(high=1.05, low=0.97, close=0.99, trail=0.98, atr=0.01, ts_ms=2_000_000)
    assert alasan == "stop_loss"
    trade = mgr.close(price=0.98, ts_ms=2_000_000, reason=alasan, equity_before=1000.0)
    assert trade.net_usdt < 0
    assert trade.r_multiple == pytest.approx(-1.0, abs=0.05)


def test_tp_parsial_lalu_sl_break_even() -> None:
    mgr = manager()
    buka(mgr)
    # TP1 kena: high 1.04 >= 1.03
    assert (
        mgr.on_candle(high=1.04, low=1.0, close=1.03, trail=1.0, atr=0.01, ts_ms=2_000_000) is None
    )
    pos = mgr.position
    assert pos is not None
    assert pos.partial_done is True
    assert pos.remaining_qty == pytest.approx(50.0)
    assert pos.stop_price == pytest.approx(1.001)  # pindah ke BE
    # kemudian harga kembali ke BE → alasan stop_loss_be
    alasan = mgr.on_candle(high=1.02, low=0.999, close=1.0, trail=1.0, atr=0.01, ts_ms=3_000_000)
    assert alasan == "stop_loss_be"
    trade = mgr.close(price=1.001, ts_ms=3_000_000, reason=alasan, equity_before=1000.0)
    assert trade.partial_done is True
    assert trade.be_done is True
    # untung dari 50% di TP1 (1,03) dikurangi biaya, rugi tipis dari sisa di BE
    assert trade.gross_usdt > 0
    assert trade.costs.fee_usdt > 0


def test_trailing_hanya_searah_dan_tidak_di_atas_harga() -> None:
    mgr = manager()
    buka(mgr, partial_fraction=1.0)  # tanpa parsial: TP penuh
    mgr.on_candle(high=1.05, low=1.01, close=1.04, trail=1.02, atr=0.01, ts_ms=2_000_000)
    pos = mgr.position
    assert pos is not None
    pertama = pos.stop_price
    assert pertama > 0.98
    # harga turun: SL tidak boleh mundur
    mgr.on_candle(high=1.04, low=1.02, close=1.02, trail=0.99, atr=0.01, ts_ms=3_000_000)
    assert pos.stop_price >= pertama
    # SL tidak pernah di atas harga penutupan (long)
    assert pos.stop_price <= 1.04


def test_short_trailing_dan_sl() -> None:
    mgr = manager()
    buka(
        mgr,
        side="short",
        stop_price=1.02,
        tp1_price=0.97,
        be_price=0.999,
        be_trigger_price=0.98,
        trailing_activation_price=0.98,
        partial_fraction=1.0,
    )
    mgr.on_candle(high=1.0, low=0.95, close=0.96, trail=0.98, atr=0.01, ts_ms=2_000_000)
    pos = mgr.position
    assert pos is not None
    assert pos.stop_price < 1.02
    alasan = mgr.on_candle(high=1.03, low=0.97, close=1.02, trail=0.99, atr=0.01, ts_ms=3_000_000)
    assert alasan in ("stop_loss", "stop_loss_be")


def test_biaya_masuk_dan_keluar_dihitung() -> None:
    mgr = TradeManager(CostModel(taker_bps=5.0, slippage_bps=2.0, funding_rate_8h_pct=0.0))
    buka(mgr, partial_fraction=1.0, be_trigger_price=99.0, trailing_activation_price=99.0)
    alasan = mgr.on_candle(high=1.5, low=1.4, close=1.45, trail=1.4, atr=0.01, ts_ms=2_000_000)
    assert alasan is None  # TP1 tidak tersentuh, SL tidak kena
    trade = mgr.close(price=1.45, ts_ms=2_000_000, reason="akhir_data", equity_before=1000.0)
    assert trade.costs.fee_usdt == pytest.approx(100 * 1.0 * 5 / 10_000 + 100 * 1.45 * 5 / 10_000)
    assert trade.costs.slippage_usdt > 0
    assert trade.costs.funding_usdt == 0.0
    assert trade.net_usdt == pytest.approx(trade.gross_usdt - trade.costs.total_usdt)


def test_funding_dari_deret_data() -> None:
    seri = FundingSeries(
        timestamps_ms=[1_000_000 + 8 * 3600_000],
        rates_pct=[0.05],  # 0,05%
    )
    mgr = TradeManager(BIAYA, funding=seri)
    buka(mgr, partial_fraction=1.0, be_trigger_price=99.0, trailing_activation_price=99.0)
    trade = mgr.close(
        price=1.0, ts_ms=1_000_000 + 9 * 3600_000, reason="akhir_data", equity_before=1000.0
    )
    # long membayar 0,05% x notional 100 = 0,05 USDT
    assert trade.costs.funding_usdt == pytest.approx(0.05)


def test_funding_tetap_dipakai_bila_tanpa_data() -> None:
    mgr = TradeManager(CostModel(funding_rate_8h_pct=0.02))
    buka(mgr, partial_fraction=1.0, be_trigger_price=99.0, trailing_activation_price=99.0)
    trade = mgr.close(
        price=1.0, ts_ms=1_000_000 + 8 * 3600_000, reason="akhir_data", equity_before=1000.0
    )
    assert trade.costs.funding_usdt == pytest.approx(0.02)


def test_trade_rekap_dan_as_row() -> None:
    mgr = manager()
    buka(mgr)
    alasan = mgr.on_candle(high=1.05, low=0.97, close=0.99, trail=0.98, atr=0.01, ts_ms=2_000_000)
    trade = mgr.close(
        price=0.98, ts_ms=2_000_000, reason=alasan or "stop_loss", equity_before=1000.0
    )
    baris = trade.as_row()
    assert baris["alasan"] == "stop_loss"
    assert baris["R"] < 0
    assert trade.equity_after == pytest.approx(1000.0 + trade.net_usdt)
    assert mgr.position is None


def test_tutup_tanpa_posisi_error() -> None:
    with pytest.raises(RuntimeError, match="tidak ada posisi"):
        manager().close(price=1.0, ts_ms=1, reason="x", equity_before=1000.0)
