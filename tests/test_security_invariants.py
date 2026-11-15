"""Test invarian keamanan (bagian dari audit TAHAP 7).

Setiap test di sini menjaga **satu aturan yang tidak boleh dilanggar**, bukan
detail implementasi. Kalau salah satunya gagal, jangan pakai bot ini dengan uang
sungguhan sebelum diperbaiki.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.binance.trading import OrderRequest, TradingAPI
from volumatic_bot.cli import main
from volumatic_bot.config import AppConfig, ConfigError, load_config
from volumatic_bot.execution.protect import build_protection_orders
from volumatic_bot.logging_setup import sanitize, setup_logging

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"
SRC = ROOT / "src" / "volumatic_bot"


# ---------------------------------------------------------------- rahasia
def test_rahasia_disaring_di_log() -> None:
    rahasia = "AbCdEf1234567890SECRET"
    logger = setup_logging("INFO", secrets=[rahasia])
    for handler in logger.handlers:
        for filter_ in handler.filters:
            record = logging.LogRecord(
                name="x",
                level=logging.INFO,
                pathname="",
                lineno=1,
                msg=f"api_secret={rahasia} signature=abcdef",
                args=(),
                exc_info=None,
            )
            filter_.filter(record)
            assert rahasia not in record.getMessage()
            assert "abcdef" not in record.getMessage()


def test_pola_sensitif_disaring() -> None:
    teks = (
        "POST /fapi/v1/order?signature=deadbeef listenKey=AbC123 token=xyz "
        "X-MBX-APIKEY: kuncirahasia bot123456789:AAF-abcdefghijklmnop"
    )
    bersih = sanitize(teks)
    for bocor in ("deadbeef", "AbC123", "xyz", "kuncirahasia", "AAF-abcdefghijklmnop"):
        assert bocor not in bersih
    assert bersih.count("***REDACTED***") >= 4


def test_tidak_ada_kredensial_tertanam_di_kode() -> None:
    """Cari pola panjang yang mirip API key di dalam sumber (bukan di test)."""
    mencurigakan: list[str] = []
    for berkas in SRC.rglob("*.py"):
        for nomor, baris in enumerate(berkas.read_text(encoding="utf-8").splitlines(), 1):
            if len(baris) > 200:
                mencurigakan.append(f"{berkas}:{nomor} baris sangat panjang")
            for pola in ("api_key=", "api_secret="):
                mencurigakan_flag = (
                    pola in baris
                    and '"' in baris
                    and "secrets" not in baris
                    and "def " not in baris
                    and "config" not in baris
                )
                if mencurigakan_flag:
                    mencurigakan.append(f"{berkas}:{nomor} {pola}")
    assert not mencurigakan, mencurigakan


def test_secrets_tidak_punya_nilai_bawaan() -> None:
    from volumatic_bot.config import Secrets

    secrets = Secrets(_env_file=None)
    assert secrets.binance_api_key is None
    assert secrets.has_api_credentials is False
    with pytest.raises(ConfigError):
        secrets.api_key()


# ------------------------------------------------------------------ mode live
def test_mode_live_butuh_dua_pagar() -> None:
    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["ops"]["mode"] = "live"
    with pytest.raises(Exception, match="allow_live"):
        AppConfig(**dump)

    dump["ops"]["allow_live"] = True
    with pytest.raises(Exception, match="live_confirmation"):
        AppConfig(**dump)

    dump["ops"]["live_confirmation"] = "SAYA-MENGERTI-RISIKONYA"
    assert AppConfig(**dump).ops.mode == "live"


def test_mode_bawaan_dry_run() -> None:
    assert load_config(ROOT / "config.yaml").ops.mode == "dry_run"


def test_leverage_dibatasi() -> None:
    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["exchange"]["default_leverage"] = 20
    with pytest.raises(Exception, match="leverage"):
        AppConfig(**dump)


def test_risiko_dibatasi_dua_persen() -> None:
    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["risk"]["risk_per_trade_pct"] = 5.0
    with pytest.raises(Exception, match="risk_per_trade_pct"):
        AppConfig(**dump)


# ---------------------------------------------------------------- order nyata
def test_sl_tidak_pernah_reduce_only_bersama_close_position() -> None:
    """Binance menolak ``closePosition`` + ``reduceOnly``; keduanya tidak boleh bersamaan."""
    for partial in (None, Decimal("50")):
        hasil = build_protection_orders(
            symbol="DOGEUSDT",
            direction="long",
            qty=Decimal("100"),
            stop_price=Decimal("0.34"),
            tp1_price=Decimal("0.36"),
            partial_qty=partial,
        )
        for order in hasil.all():
            assert not (order.close_position and order.reduce_only)
            if order.close_position:
                assert order.quantity is None


def test_sl_selalu_ada_dan_memakai_contract_price() -> None:
    hasil = build_protection_orders(
        symbol="DOGEUSDT",
        direction="short",
        qty=Decimal("10"),
        stop_price=Decimal("0.36"),
        tp1_price=Decimal("0.34"),
    )
    assert hasil.stop_loss.close_position is True
    assert hasil.stop_loss.working_type == "CONTRACT_PRICE"
    assert hasil.stop_loss.type == "STOP_MARKET"


def test_order_tidak_memakai_position_side_hedge() -> None:
    """Sistem memakai one-way mode: ``positionSide`` harus BOTH (tidak dikirim)."""
    order = OrderRequest(symbol="DOGEUSDT", side="BUY", type="MARKET", quantity=Decimal("1"))
    payload = order.to_params()
    assert payload.get("positionSide", "BOTH") in ("BOTH", None)


def test_client_order_id_maksimal_36_karakter() -> None:
    class Rest:
        def now_ms(self) -> int:
            return 1_700_000_000_000

    api = TradingAPI(Rest())  # type: ignore[arg-type]
    cid = api.client_order_id("stop_loss", tag="DOGEUSDT-0123456789")
    assert len(cid) <= 36
    assert cid.startswith("vbot") or cid.startswith("vb")


def test_preflight_menolak_tanpa_kredensial_di_mode_testnet(tmp_path: Path, capsys: Any) -> None:
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["ops"]["mode"] = "testnet"
    raw["ops"]["kill_switch_file"] = str(tmp_path / "KILL")
    raw["data"]["db_path"] = str(tmp_path / "b.db")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    kode = main(["--config", str(cfg), "preflight"])
    assert kode != 0
    capsys.readouterr()


# ------------------------------------------------------------------ perilaku
def test_min_equity_dijaga_di_config() -> None:
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.risk.min_equity_usdt <= 1000
    assert cfg.risk.max_total_margin_pct <= 80
    assert cfg.risk.max_concurrent_positions <= 5


def test_symbol_info_memakai_sample_resmi() -> None:
    info = parse_exchange_info(
        __import__("json").loads((DATA / "exchange_info_sample.json").read_text("utf-8"))
    )
    doge = info.get("DOGEUSDT")
    assert doge.is_perpetual_usdt
    assert float(doge.filters.min_notional) > 0
