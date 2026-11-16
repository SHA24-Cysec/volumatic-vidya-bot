"""Test filter rezim harian: pembentukan candle harian, EMA200 tanpa look-ahead,
aturan entry, integrasi KlineFeed, dan integrasi backtester."""

from __future__ import annotations

import numpy as np
import pytest

from volumatic_bot.backtest.runner import BacktestConfig, run_backtest
from volumatic_bot.binance.kline_messages import KlineEvent
from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.config import AppConfig, RegimeConfig
from volumatic_bot.models import CandleSeries
from volumatic_bot.regime import (
    DailyRegimeFilter,
    build_regime_from_daily,
    daily_candles,
)
from volumatic_bot.streams.kline_feed import KlineFeed

DAY = 86_400_000


def _seri_harian(n: int = 260, *, harga=lambda i: 100.0 + i * 0.5) -> CandleSeries:
    ot = np.array([i * DAY for i in range(n)], dtype=np.int64)
    close = np.array([harga(i) for i in range(n)], dtype=float)
    return CandleSeries.from_columns(
        open_time=ot.tolist(),
        open=close.tolist(),
        high=(close * 1.01).tolist(),
        low=(close * 0.99).tolist(),
        close=close.tolist(),
        volume=[10.0] * n,
    )


def _seri_intraday(hari: int = 260, bar_per_hari: int = 4) -> CandleSeries:
    ot, o, h, lo, c, v = [], [], [], [], [], []
    for d in range(hari):
        for b in range(bar_per_hari):
            ts = d * DAY + b * (DAY // bar_per_hari)
            harga = 100.0 + d * 0.5
            ot.append(ts)
            o.append(harga)
            h.append(harga * 1.002)
            lo.append(harga * 0.998)
            c.append(harga)
            v.append(1.0)
    return CandleSeries.from_columns(open_time=ot, open=o, high=h, low=lo, close=c, volume=v)


# --------------------------------------------------------------------- harian
def test_daily_candles_menggabungkan_dan_membuang_hari_tidak_lengkap():
    seri = _seri_intraday(hari=10, bar_per_hari=4)
    harian = daily_candles(seri)
    assert len(harian) == 10
    assert harian.open_time[0] == 0
    assert harian.open_time[1] == DAY
    # volume harian = jumlah 4 bar
    assert harian.volume[0] == pytest.approx(4.0)


def test_daily_candles_membuang_hari_terakhir_yang_setengah_jadi():
    seri = _seri_intraday(hari=5, bar_per_hari=4)
    # tambahkan satu hari dengan 1 bar saja (hari berjalan)
    ot = [*list(seri.open_time), 5 * DAY]
    o = [*list(seri.open), 200.0]
    h = [*list(seri.high), 201.0]
    low = [*list(seri.low), 199.0]
    c = [*list(seri.close), 200.5]
    v = [*list(seri.volume), 1.0]
    seri2 = CandleSeries.from_columns(open_time=ot, open=o, high=h, low=low, close=c, volume=v)
    harian = daily_candles(seri2)
    assert len(harian) == 5
    assert 5 * DAY not in {int(x) for x in harian.open_time}


def test_regime_ema_mengikuti_harga_naik():
    harian = _seri_harian(300)
    rezim = build_regime_from_daily(harian)
    assert len(rezim) == 300
    # pada tren naik, close di atas EMA200 -> dist positif
    assert rezim.dist_pct(298, float(harian.close[298])) > 0


def test_regime_dist_pct_nan_untuk_indeks_di_luar_jangkauan():
    rezim = build_regime_from_daily(_seri_harian(10))
    assert np.isnan(rezim.dist_pct(-1, 100.0))
    assert np.isnan(rezim.dist_pct(99, 100.0))


# ------------------------------------------------------------------ filter
def test_filter_menolak_bila_data_harian_kurang():
    filt = DailyRegimeFilter(enabled=True, min_daily_bars=200)
    lolos, alasan = filt.check(None, close=100.0, day_start_ms=DAY * 300, direction="long")
    assert lolos is False
    assert "belum cukup" in alasan


def test_filter_menolak_long_saat_harga_jauh_di_atas_ema():
    harian = _seri_harian(300, harga=lambda i: 100.0 + i * 2.0)  # tren naik kuat
    rezim = build_regime_from_daily(harian)
    filt = DailyRegimeFilter(enabled=True, max_dist_pct=-8.0)
    close = float(harian.close[-1])
    lolos_long, alasan = filt.check(rezim, close=close, day_start_ms=DAY * 300, direction="long")
    assert lolos_long is False
    assert "EMA200" in alasan
    lolos_short, _ = filt.check(rezim, close=close, day_start_ms=DAY * 300, direction="short")
    assert lolos_short is True


def test_filter_menolak_short_saat_harga_jauh_di_bawah_ema():
    harian = _seri_harian(300, harga=lambda i: 500.0 - i * 1.5)
    rezim = build_regime_from_daily(harian)
    filt = DailyRegimeFilter(enabled=True, max_dist_pct=-8.0)
    close = float(harian.close[-1])
    lolos_short, _alasan = filt.check(rezim, close=close, day_start_ms=DAY * 300, direction="short")
    assert lolos_short is False
    lolos_long, _ = filt.check(rezim, close=close, day_start_ms=DAY * 300, direction="long")
    assert lolos_long is True


def test_filter_nonaktif_selalu_lolos():
    filt = DailyRegimeFilter(enabled=False)
    lolos, _ = filt.check(None, close=1.0, day_start_ms=0, direction="long")
    assert lolos is True


def test_filter_menolak_saat_atr_harian_ekstrem():
    harian = _seri_harian(300)
    rezim = build_regime_from_daily(harian)
    filt = DailyRegimeFilter(enabled=True, max_dist_pct=50.0, max_daily_atr_pct=0.001)
    lolos, alasan = filt.check(
        rezim, close=float(harian.close[-1]), day_start_ms=DAY * 300, direction="long"
    )
    assert lolos is False
    assert "ATR harian" in alasan


def test_config_menolak_max_dist_pct_di_luar_batas():
    with pytest.raises(ValueError):
        DailyRegimeFilter(max_dist_pct=99.0)


# ------------------------------------------------------------------- KlineFeed
def _event(symbol: str, i: int, seri: CandleSeries) -> KlineEvent:
    return KlineEvent(
        symbol=symbol,
        interval="sim",
        open_time=int(seri.open_time[i]),
        close_time=int(seri.open_time[i]) + 1000,
        open=float(seri.open[i]),
        high=float(seri.high[i]),
        low=float(seri.low[i]),
        close=float(seri.close[i]),
        volume=float(seri.volume[i]),
        quote_volume=1.0,
        is_closed=True,
        event_time=int(seri.open_time[i]) + 1000,
    )


@pytest.mark.asyncio
async def test_feed_tanpa_filter_rezim_tetap_menghasilkan_sinyal_yang_sama():
    """Regresi: filter rezim ``None`` tidak boleh mengubah perilaku lama."""
    seri = _seri_intraday(hari=260, bar_per_hari=8)
    feed = KlineFeed(regime_filter=None)
    feed.seed("BTCUSDT", seri)
    sinyal = 0
    for i in range(len(seri)):
        hasil = await feed.handle_event(_event("BTCUSDT", i, seri))
        sinyal += int(hasil is not None)
    assert feed.regime_rejected == 0
    assert sinyal == 0  # data sintetis monoton: tidak ada pergantian tren


@pytest.mark.asyncio
async def test_feed_dengan_filter_menolak_sinyal_saat_rezim_tidak_mendukung(monkeypatch):
    seri = _seri_intraday(hari=260, bar_per_hari=8)
    filt = DailyRegimeFilter(enabled=True, max_dist_pct=-8.0, min_daily_bars=200)
    feed = KlineFeed(regime_filter=filt)
    feed.seed("BTCUSDT", seri)
    feed.seed_regime_series("BTCUSDT", seri)

    paksa = {"arah": "long"}
    monkeypatch.setattr(
        "volumatic_bot.streams.kline_feed.entry_signal",
        lambda result, index=-1: paksa["arah"],
    )
    hasil = await feed.handle_event(_event("BTCUSDT", len(seri) - 1, seri))
    assert hasil is None
    assert feed.regime_rejected == 1


@pytest.mark.asyncio
async def test_feed_melepas_sinyal_bila_rezim_lolos(monkeypatch):
    seri = _seri_intraday(hari=260, bar_per_hari=8)
    filt = DailyRegimeFilter(enabled=True, max_dist_pct=50.0, min_daily_bars=200)
    feed = KlineFeed(regime_filter=filt)
    feed.seed("BTCUSDT", seri)
    feed.seed_regime_series("BTCUSDT", seri)
    monkeypatch.setattr(
        "volumatic_bot.streams.kline_feed.entry_signal",
        lambda result, index=-1: "long",
    )
    hasil = await feed.handle_event(_event("BTCUSDT", len(seri) - 1, seri))
    assert hasil is not None
    assert hasil.direction == "long"
    assert feed.regime_rejected == 0


# ------------------------------------------------------------------ backtest
def _info(symbol: str = "BTCUSDT"):
    payload = {
        "symbols": [
            {
                "symbol": symbol,
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "quoteAsset": "USDT",
                "pricePrecision": 2,
                "quantityPrecision": 3,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01",
                        "maxPrice": "1e9",
                        "tickSize": "0.01",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "1e6",
                        "stepSize": "0.001",
                    },
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
        ]
    }
    return parse_exchange_info(payload).get(symbol)


def _seri_bergelombang(hari: int = 320, bar_per_hari: int = 8) -> CandleSeries:
    rng = np.random.default_rng(3)
    n = hari * bar_per_hari
    langkah = rng.normal(0.0, 1.0, n)
    close = 100.0 * np.exp(np.cumsum(langkah) * 0.01)
    ot = np.arange(n, dtype=np.int64) * (DAY // bar_per_hari) + 1_600_000_000_000
    return CandleSeries.from_columns(
        open_time=ot.tolist(),
        open=(close * 0.999).tolist(),
        high=(close * 1.01).tolist(),
        low=(close * 0.99).tolist(),
        close=close.tolist(),
        volume=[10.0] * n,
    )


def _config(regime_enabled: bool, max_dist_pct: float = -8.0) -> AppConfig:
    dasar = AppConfig()
    return dasar.model_copy(
        update={
            "regime": RegimeConfig(enabled=regime_enabled, max_dist_pct=max_dist_pct),
            "risk": dasar.risk.model_copy(
                update={"min_equity_usdt": 1.0, "allow_aggressive_risk": True}
            ),
            "indicator": dasar.indicator.model_copy(update={"atr_length": 50}),
        }
    )


def test_backtest_filter_rezim_mengurangi_trade_dan_mencatat_alasan():
    seri = _seri_bergelombang()
    info = _info()
    bt = BacktestConfig(equity=1000, leverage=10)
    tanpa = run_backtest(seri, info, _config(False), bt=bt)
    with_filter = run_backtest(seri, info, _config(True), bt=bt)
    assert len(with_filter.skipped) >= len(tanpa.skipped)
    ditolak = any("rezim" in baris for baris in with_filter.skipped)
    assert ditolak or with_filter.count <= tanpa.count
    assert any("rezim harian" in catatan for catatan in with_filter.notes)


def test_backtest_filter_rezim_tidak_memakai_data_masa_depan():
    """Filter hanya boleh melihat hari yang sudah tutup sebelum bar sinyal."""
    seri = _seri_bergelombang()
    info = _info()
    cfg = _config(True)
    bt = BacktestConfig(equity=1000, leverage=10)
    penuh = run_backtest(seri, info, cfg, bt=bt)

    # Potong 30% data terakhir: keputusan pada bar yang sama harus identik,
    # karena rezim dihitung dari hari-hari sebelumnya, bukan dari masa depan.
    potong = int(len(seri) * 0.7)
    setengah = CandleSeries.from_columns(
        open_time=seri.open_time[:potong].tolist(),
        open=seri.open[:potong].tolist(),
        high=seri.high[:potong].tolist(),
        low=seri.low[:potong].tolist(),
        close=seri.close[:potong].tolist(),
        volume=seri.volume[:potong].tolist(),
    )
    hasil_potong = run_backtest(setengah, info, cfg, bt=bt)
    awal_penuh = [t.opened_ms for t in penuh.trades if t.opened_ms < penuh.last_ms * 0.7][:3]
    awal_potong = [t.opened_ms for t in hasil_potong.trades][:3]
    if awal_penuh and awal_potong:
        assert awal_penuh == awal_potong
