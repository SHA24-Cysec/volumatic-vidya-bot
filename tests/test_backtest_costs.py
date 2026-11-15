"""Test model biaya backtest: fee, slippage, dan funding."""

from __future__ import annotations

import pytest

from volumatic_bot.backtest.costs import CostModel, FundingSeries, TradeCosts


def test_fee_dan_slippage_proporsional() -> None:
    biaya = CostModel(taker_bps=5.0, slippage_bps=2.0)
    assert biaya.fee(1000.0) == pytest.approx(0.5)
    assert biaya.slip(1000.0) == pytest.approx(0.2)
    assert biaya.entry_cost(1000.0) == pytest.approx(0.7)
    assert biaya.exit_cost(500.0) == pytest.approx(0.35)


def test_biaya_selalu_merugikan_arah_apa_pun() -> None:
    biaya = CostModel(taker_bps=5.0, slippage_bps=2.0)
    assert biaya.slip(-1000.0) > 0  # notional negatif tetap dikenai biaya
    assert biaya.fee(-1000.0) == pytest.approx(0.5)


def test_bps_nol_berarti_tanpa_biaya() -> None:
    gratis = CostModel(taker_bps=0.0, slippage_bps=0.0)
    assert gratis.entry_cost(1234.5) == 0.0
    assert gratis.fill_price(100.0, "long", entering=True) == 100.0


def test_harga_eksekusi_selalu_merugikan() -> None:
    biaya = CostModel(slippage_bps=10.0)  # 0,1%
    beli = biaya.fill_price(100.0, "long", entering=True)
    jual = biaya.fill_price(100.0, "long", entering=False)
    assert beli > 100.0 and jual < 100.0
    # short: masuk dengan jual (lebih murah), keluar dengan beli (lebih mahal)
    assert biaya.fill_price(100.0, "short", entering=True) < 100.0
    assert biaya.fill_price(100.0, "short", entering=False) > 100.0


def test_funding_long_membayar_short_menerima() -> None:
    biaya = CostModel(funding_rate_8h_pct=0.01, funding_interval_hours=8.0)
    delapan_jam_long = biaya.funding_payment(1000.0, "long", 8.0)
    delapan_jam_short = biaya.funding_payment(1000.0, "short", 8.0)
    assert delapan_jam_long == pytest.approx(0.1)  # 0,01% x 1000
    assert delapan_jam_short == pytest.approx(-0.1)
    assert biaya.funding_payment(1000.0, "long", 0.0) == 0.0
    assert biaya.funding_payment(1000.0, "long", 16.0) == pytest.approx(0.2)


def test_funding_negatif_berarti_long_menerima() -> None:
    biaya = CostModel(funding_rate_8h_pct=-0.02)
    assert biaya.funding_payment(1000.0, "long", 8.0) < 0


def test_validasi_parameter() -> None:
    with pytest.raises(ValueError, match="fee/slippage"):
        CostModel(taker_bps=-1)
    with pytest.raises(ValueError, match="funding_interval_hours"):
        CostModel(funding_interval_hours=0)


def test_trade_costs_total_dan_ringkasan() -> None:
    biaya = TradeCosts(fee_usdt=0.5, slippage_usdt=0.2, funding_usdt=-0.05)
    assert biaya.total_usdt == pytest.approx(0.65)
    teks = biaya.summary()
    assert "fee=0.5000" in teks and "funding=-0.0500" in teks


# ------------------------------------------------------------------ funding data
def test_funding_series_dari_baris_bursa() -> None:
    rows = [
        {"fundingTime": 2000, "fundingRate": 0.0002},
        {"fundingTime": 1000, "fundingRate": 0.0001},
    ]
    seri = FundingSeries.from_rows(rows)
    assert seri.timestamps_ms == [1000, 2000]
    assert seri.rates_pct == pytest.approx([0.01, 0.02])
    assert len(seri) == 2
    assert seri.source == "binance"


def test_funding_kumulatif_dalam_rentang() -> None:
    seri = FundingSeries(timestamps_ms=[1000, 2000, 3000], rates_pct=[0.01, 0.01, 0.01])
    assert seri.cumulative_pct(1000, 3000) == pytest.approx(0.02)  # batas awal eksklusif
    assert seri.cumulative_pct(500, 500) is None
    assert seri.cumulative_pct(0, 500) == 0.0
    assert seri.cumulative_pct(4000, 5000) == 0.0


def test_biaya_funding_usdt_ikut_arah_posisi() -> None:
    seri = FundingSeries(timestamps_ms=[1000, 2000], rates_pct=[0.01, 0.01])
    long = seri.cost_usdt(notional=1000.0, side="long", start_ms=0, end_ms=3000)
    short = seri.cost_usdt(notional=1000.0, side="short", start_ms=0, end_ms=3000)
    assert long == pytest.approx(0.2)
    assert short == pytest.approx(-0.2)
    assert seri.cost_usdt(notional=1000.0, side="long", start_ms=2000, end_ms=2000) is None


def test_funding_tetap_membangun_deret() -> None:
    seri = FundingSeries.from_constant(
        rate_8h_pct=0.01, start_ms=0, end_ms=24 * 3600 * 1000, interval_hours=8
    )
    assert len(seri) == 3
    assert all(abs(x - 0.01) < 1e-12 for x in seri.rates_pct)
    assert seri.source == "tetap"
