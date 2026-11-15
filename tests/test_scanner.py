"""Test pemindai multi-simbol (filter, alasan penolakan, kuota, skor)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from volumatic_bot.config import ScannerConfig
from volumatic_bot.models import CandleSeries
from volumatic_bot.scanner.scanner import SymbolScanner, atr_percent, spread_pct

DATA = Path(__file__).parent / "data" / "exchange_info_sample.json"


def make_series(n: int = 400, price: float = 100.0, tr_pct: float = 1.0) -> CandleSeries:
    """Deret dengan true range konstan sehingga ATR% == tr_pct (dalam persen)."""
    half = price * (tr_pct / 100.0) / 2.0
    open_time = [1_700_000_000_000 + i * 900_000 for i in range(n)]
    close = np.full(n, price)
    return CandleSeries.from_columns(
        open_time=open_time,
        open=np.full(n, price),
        high=np.full(n, price + half),
        low=np.full(n, price - half),
        close=close,
        volume=np.full(n, 100.0),
    )


@dataclass
class FakeRepo:
    series: dict[str, CandleSeries]
    requests: int = 1

    async def history(self, symbol: str, bars: int | None = None) -> Any:
        class Result:
            def __init__(self, series: CandleSeries, requests: int) -> None:
                self.series = series
                self.requests = requests
                self.total = len(series)

        return Result(self.series.get(symbol.upper(), make_series()), self.requests)


class FakeREST:
    def __init__(self, tickers: list[dict[str, Any]], books: list[dict[str, Any]]) -> None:
        self._exchange = json.loads(DATA.read_text(encoding="utf-8"))
        self._tickers = tickers
        self._books = books

    async def exchange_info(self) -> dict[str, Any]:
        return self._exchange

    async def ticker_24hr(self) -> list[dict[str, Any]]:
        return self._tickers

    async def book_ticker(self) -> list[dict[str, Any]]:
        return self._books


def ticker(symbol: str, price: float = 100.0, volume_jt: float = 100.0) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "lastPrice": f"{price}",
        "quoteVolume": f"{volume_jt * 1e6}",
        "priceChangePercent": "1.5",
    }


def book(symbol: str, bid: float, ask: float) -> dict[str, Any]:
    return {"symbol": symbol, "bidPrice": f"{bid}", "askPrice": f"{ask}"}


def default_rest(
    volumes: dict[str, float] | None = None, spreads: dict[str, float] | None = None
) -> FakeREST:
    volumes = volumes or {}
    spreads = spreads or {}
    symbols = ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "BIGUSDT"]
    tickers = [ticker(s, volume_jt=volumes.get(s, 100.0)) for s in symbols]
    books = []
    for s in symbols:
        sp = spreads.get(s, 0.001)  # default 0.001% spread
        mid = 100.0
        books.append(book(s, mid * (1 - sp / 200), mid * (1 + sp / 200)))
    return FakeREST(tickers, books)


def make_scanner(rest: FakeREST, repo: FakeRepo | None = None, **overrides: Any) -> SymbolScanner:
    base: dict[str, Any] = {
        "timeframe": "15m",
        "max_symbols": 10,
        "min_quote_volume_24h_usdt": 50e6,
        "max_spread_pct": 0.05,
        "min_atr_pct": 0.25,
        "max_atr_pct": 6.0,
        "warmup_bars": 400,
        "blacklist": [],
    }
    base.update(overrides)
    config = ScannerConfig(**base)
    return SymbolScanner(rest, repo or FakeRepo({}), config, leverage=5)  # type: ignore[arg-type]


def test_spread_pct() -> None:
    assert spread_pct(100.0, 100.1) == pytest.approx(0.09995, rel=1e-3)
    assert spread_pct(0.0, 1.0) == float("inf")
    assert spread_pct(101.0, 100.0) == float("inf")  # bid > ask: data rusak


def test_atr_percent_sebanding_true_range() -> None:
    series = make_series(400, price=100.0, tr_pct=2.0)
    assert atr_percent(series, 200) == pytest.approx(2.0, rel=1e-3)
    assert math.isnan(atr_percent(make_series(1), 200))  # data terlalu pendek


@pytest.mark.asyncio
async def test_simbol_lolos_filter() -> None:
    scanner = make_scanner(default_rest())
    result = await scanner.scan()
    assert set(result.accepted_symbols) == {"BTCUSDT", "ETHUSDT", "DOGEUSDT", "BIGUSDT"}
    assert result.dropped_by_cap == 0
    for row in result.accepted:
        assert 0.0 <= row.score <= 1.0
        assert row.bars >= 200


@pytest.mark.asyncio
async def test_volume_kurang_ditolak_dengan_alasan() -> None:
    scanner = make_scanner(default_rest(volumes={"DOGEUSDT": 10.0}))
    result = await scanner.scan()
    assert "DOGEUSDT" not in result.accepted_symbols
    rejection = next(r for r in result.rejected if r.symbol == "DOGEUSDT")
    assert rejection.stage == "volume"
    assert "jt" in rejection.reason


@pytest.mark.asyncio
async def test_spread_terlalu_lebar_ditolak() -> None:
    scanner = make_scanner(default_rest(spreads={"ETHUSDT": 0.2}))
    result = await scanner.scan()
    assert "ETHUSDT" not in result.accepted_symbols
    rejection = next(r for r in result.rejected if r.symbol == "ETHUSDT")
    assert rejection.stage == "spread"
    assert "%" in rejection.reason


@pytest.mark.asyncio
async def test_blacklist_dan_whitelist() -> None:
    scanner = make_scanner(default_rest(), blacklist=["BTCUSDT"])
    result = await scanner.scan()
    assert "BTCUSDT" not in result.accepted_symbols
    assert any(r.stage == "blacklist" for r in result.rejected)

    scanner = make_scanner(default_rest(), whitelist=["ETHUSDT"])
    result = await scanner.scan()
    assert result.accepted_symbols == ["ETHUSDT"]


@pytest.mark.asyncio
async def test_filter_modal_kecil_memakai_min_notional() -> None:
    """BIGUSDT minNotional 500 USDT: @5x butuh margin 100 USDT -> ditolak."""
    scanner = make_scanner(default_rest())
    result = await scanner.scan(max_margin_per_trade=50.0)
    assert "BIGUSDT" not in result.accepted_symbols
    rejection = next(r for r in result.rejected if r.symbol == "BIGUSDT")
    assert rejection.stage == "modal"
    assert "margin" in rejection.reason

    # Tanpa batas margin (None) simbol itu tetap lolos.
    result = await scanner.scan(max_margin_per_trade=None)
    assert "BIGUSDT" in result.accepted_symbols


@pytest.mark.asyncio
async def test_filter_min_notional_untuk_modal_kecil() -> None:
    """Penyaring modal kecil: simbol mahal dibuang TANPA perlu --equity.

    Data contoh: BTC minNotional 100, ETH 20, DOGE 5, BIG 500. Dengan batas 5
    USDT hanya DOGEUSDT yang layak — inilah yang membuat $7 tidak dipaksa
    masuk ke BTC (satu SL 2% di sana = 14% equity).
    """
    scanner = make_scanner(default_rest(), max_min_notional_usdt=5.0)
    result = await scanner.scan()
    assert result.accepted_symbols == ["DOGEUSDT"]
    ditolak = {r.symbol: r for r in result.rejected if r.stage == "min_notional"}
    assert set(ditolak) == {"BTCUSDT", "ETHUSDT", "BIGUSDT"}
    assert "minNotional" in ditolak["ETHUSDT"].reason

    # 0 = nonaktif -> semua simbol lolos lagi (perilaku bawaan config.yaml).
    longgar = make_scanner(default_rest(), max_min_notional_usdt=0.0)
    assert len((await longgar.scan()).accepted_symbols) == 4


@pytest.mark.asyncio
async def test_kuota_simbol_teratas_berdasarkan_volume() -> None:
    volumes = {"BTCUSDT": 500.0, "ETHUSDT": 400.0, "DOGEUSDT": 300.0, "BIGUSDT": 200.0}
    scanner = make_scanner(default_rest(volumes=volumes), max_symbols=2)
    result = await scanner.scan()
    assert result.accepted_symbols == ["BTCUSDT", "ETHUSDT"]  # volume terbesar
    assert result.dropped_by_cap == 2
    assert any(r.stage == "kuota" for r in result.rejected)


@pytest.mark.asyncio
async def test_filter_atr_minimum_dan_maksimum() -> None:
    repo = FakeRepo(
        {
            "BTCUSDT": make_series(400, tr_pct=1.0),
            "ETHUSDT": make_series(400, tr_pct=0.01),  # terlalu datar
            "DOGEUSDT": make_series(400, tr_pct=12.0),  # terlalu liar
            "BIGUSDT": make_series(400, tr_pct=1.5),
        }
    )
    scanner = make_scanner(default_rest(), repo)
    result = await scanner.scan()
    assert set(result.accepted_symbols) == {"BTCUSDT", "BIGUSDT"}
    reasons = {r.symbol: r.reason for r in result.rejected if r.stage == "atr"}
    assert "terlalu datar" in reasons["ETHUSDT"]
    assert "terlalu liar" in reasons["DOGEUSDT"]


@pytest.mark.asyncio
async def test_data_candle_tidak_cukup_ditolak() -> None:
    repo = FakeRepo({"BTCUSDT": make_series(10, tr_pct=1.0)})
    scanner = make_scanner(default_rest(), repo)
    result = await scanner.scan()
    assert "BTCUSDT" not in result.accepted_symbols
    assert any(r.stage == "data" for r in result.rejected if r.symbol == "BTCUSDT")


@pytest.mark.asyncio
async def test_skor_menghargai_volume_dan_atr() -> None:
    repo = FakeRepo(
        {"BTCUSDT": make_series(400, tr_pct=2.0), "ETHUSDT": make_series(400, tr_pct=0.5)}
    )
    rest = default_rest(volumes={"BTCUSDT": 900.0, "ETHUSDT": 60.0})
    scanner = make_scanner(rest, repo)
    result = await scanner.scan()
    scores = {row.symbol: row.score for row in result.accepted}
    assert scores["BTCUSDT"] > scores["ETHUSDT"]  # volume besar + ATR lebih tinggi


@pytest.mark.asyncio
async def test_hasil_menyertakan_series_untuk_dipakai_ulang() -> None:
    scanner = make_scanner(default_rest())
    result = await scanner.scan()
    for row in result.accepted:
        assert row.series is not None
        assert len(row.series) >= 400
