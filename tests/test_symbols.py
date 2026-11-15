"""Test parsing ``exchangeInfo`` dan aturan pembulatan/filter simbol."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from volumatic_bot.binance.symbols import (
    parse_exchange_info,
    round_down_step,
    round_nearest_step,
)

DATA = Path(__file__).parent / "data" / "exchange_info_sample.json"


@pytest.fixture(scope="module")
def exchange():
    return parse_exchange_info(json.loads(DATA.read_text(encoding="utf-8")))


def test_hanya_perpetual_usdt_trading(exchange) -> None:
    symbols = {s.symbol for s in exchange.trading_usdt_perpetuals}
    assert symbols == {"BTCUSDT", "ETHUSDT", "DOGEUSDT", "BIGUSDT"}
    # Kontrak kuartalan, simbol non-TRADING, dan pasangan USDC harus tersaring.
    assert "BTCUSDT_250926" in exchange.symbols
    assert not exchange.get("BTCUSDT_250926").is_perpetual_usdt
    assert not exchange.get("NEWUSDT").is_perpetual_usdt
    assert not exchange.get("BTCUSDC").is_perpetual_usdt


def test_rate_limit_dibaca(exchange) -> None:
    assert exchange.weight_limit_per_min == 2400
    assert any(lim["rateLimitType"] == "ORDERS" for lim in exchange.rate_limits)


def test_filter_terbaca_benar(exchange) -> None:
    btc = exchange.get("BTCUSDT").filters
    assert btc.tick_size == Decimal("0.10")
    assert btc.step_size == Decimal("0.001")
    assert btc.min_qty == Decimal("0.001")
    assert btc.min_notional == Decimal("100")
    assert btc.market_step_size == Decimal("0.001")
    # decimals() dipakai untuk memformat string order.
    assert btc.decimals(kind="price") == 2
    assert btc.decimals(kind="qty") == 3


def test_pembulatan_harga_ke_tick(exchange) -> None:
    btc = exchange.get("BTCUSDT")
    assert btc.format_price(123.456789) == "123.50"  # tick 0.10 -> 2 desimal
    assert btc.format_price(91_234.567) == "91234.60"
    doge = exchange.get("DOGEUSDT")
    assert doge.format_price(0.12345678) == "0.123460"  # kelipatan 1e-5 terdekat


def test_pembulatan_jumlah_ke_stepsize_tidak_pernah_melebihi(exchange) -> None:
    btc = exchange.get("BTCUSDT")
    # qty selalu dibulatkan KE BAWAH supaya tidak melebihi risiko yang dihitung.
    assert btc.format_qty(0.0123456) == "0.012"
    assert btc.format_qty(0.0129999) == "0.012"
    doge = exchange.get("DOGEUSDT")
    assert doge.format_qty(12.9) == "12"  # stepSize 1
    assert doge.format_qty(0.5) == "0"


def test_validasi_qty(exchange) -> None:
    btc = exchange.get("BTCUSDT").filters
    assert btc.qty_is_valid(Decimal("0.012"))
    assert not btc.qty_is_valid(Decimal("0.0001"))  # < minQty
    assert not btc.qty_is_valid(Decimal("0.0125"))  # bukan kelipatan step
    assert not btc.qty_is_valid(Decimal("0"))


def test_min_notional_dan_margin_minimum(exchange) -> None:
    btc = exchange.get("BTCUSDT")
    assert btc.filters.notional_ok(90_000, Decimal("0.002"))  # 180 USDT >= 100
    assert not btc.filters.notional_ok(90_000, Decimal("0.001"))  # 90 USDT < 100
    # Modal kecil: minNotional 100 USDT @5x butuh margin 20 USDT.
    assert btc.min_margin_required(5) == Decimal("20")
    assert btc.min_margin_required(10) == Decimal("10")
    with pytest.raises(ValueError, match="leverage"):
        btc.min_margin_required(0)


def test_simbol_dengan_min_notional_besar_butuh_margin_besar(exchange) -> None:
    big = exchange.get("BIGUSDT")
    assert big.min_margin_required(5) == Decimal("100")
    doge = exchange.get("DOGEUSDT")
    assert doge.min_margin_required(5) == Decimal("1")


def test_round_helpers() -> None:
    assert round_down_step(1.239, Decimal("0.01")) == Decimal("1.23")
    assert round_nearest_step(1.236, Decimal("0.01")) == Decimal("1.24")
    # step 0 -> nilai apa adanya (tidak membagi nol)
    assert round_down_step(1.239, Decimal("0")) == Decimal("1.239")


def test_payload_rusak_ditolak() -> None:
    with pytest.raises(ValueError, match="tidak valid"):
        parse_exchange_info({"bukan": "exchangeInfo"})


def test_simbol_tanpa_filter_dilewati_bukan_crash() -> None:
    payload = {
        "symbols": [
            {
                "symbol": "TANPAFILTER",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "quoteAsset": "USDT",
                "filters": [],
            },
            {"contractType": "PERPETUAL"},  # tanpa symbol -> dilewati
        ]
    }
    info = parse_exchange_info(payload)
    assert list(info.symbols) == ["TANPAFILTER"]
    assert info.get("TANPAFILTER").filters.min_notional == Decimal(0)


def test_simbol_tidak_ada_melempar_keyerror(exchange) -> None:
    with pytest.raises(KeyError, match="XXXXUSDT"):
        exchange.get("XXXXUSDT")
