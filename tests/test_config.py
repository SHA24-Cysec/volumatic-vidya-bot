"""Test validasi konfigurasi dan penanganan rahasia."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from volumatic_bot.config import (
    AppConfig,
    ConfigError,
    Secrets,
    load_config,
    mask_secret,
)
from volumatic_bot.logging_setup import SecretRedactingFilter, sanitize


def test_config_yaml_proyek_valid() -> None:
    config = load_config("config.yaml")
    assert config.ops.mode == "dry_run"
    assert config.exchange.max_leverage <= 10
    assert config.indicator.to_params().vidya_length == 10
    assert len(config.fingerprint()) == 12


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_default_aman_tanpa_file(tmp_path: Path) -> None:
    """Semua default harus aman: dry-run, testnet, leverage kecil."""
    config = AppConfig()
    assert config.ops.mode == "dry_run"
    assert config.exchange.testnet is True
    assert config.exchange.default_leverage <= config.exchange.max_leverage


@pytest.mark.parametrize(
    ("section", "payload", "pesan"),
    [
        ("exchange", {"max_leverage": 11}, "max_leverage"),
        ("exchange", {"default_leverage": 20}, "default_leverage"),
        ("exchange", {"margin_mode": "cross"}, "margin_mode"),
        ("exchange", {"position_mode": "hedge"}, "position_mode"),
        ("risk", {"risk_per_trade_pct": 5.0}, "risk_per_trade_pct"),
        ("risk", {"risk_per_trade_pct": 0.0}, "risk_per_trade_pct"),
        ("risk", {"daily_loss_limit_pct": 50}, "daily_loss_limit_pct"),
        ("risk", {"max_concurrent_positions": 50}, "max_concurrent_positions"),
        ("trade", {"partial_tp_pct": 0}, "partial_tp_pct"),
        ("trade", {"sl_mode": "random"}, "sl_mode"),
        ("trade", {"trailing_mode": "magic"}, "trailing_mode"),
        ("scanner", {"timeframe": "7m"}, "timeframe"),
    ],
)
def test_nilai_berbahaya_ditolak(tmp_path: Path, section: str, payload: dict, pesan: str) -> None:
    path = _write(tmp_path, {section: payload})
    with pytest.raises(ConfigError, match=pesan):
        load_config(path)


def test_field_tidak_dikenal_ditolak(tmp_path: Path) -> None:
    path = _write(tmp_path, {"risk": {"risk_per_trade_pct": 1.0, "risiko_per_trade": 3}})
    with pytest.raises(ConfigError, match="risiko_per_trade"):
        load_config(path)


def test_mode_live_butuh_dua_pengaman(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="allow_live"):
        load_config(_write(tmp_path, {"ops": {"mode": "live"}}))

    with pytest.raises(ConfigError, match="live_confirmation"):
        load_config(_write(tmp_path, {"ops": {"mode": "live", "allow_live": True}}))

    path = _write(
        tmp_path,
        {
            "ops": {
                "mode": "live",
                "allow_live": True,
                "live_confirmation": "SAYA-MENGERTI-RISIKONYA",
            }
        },
    )
    assert load_config(path).ops.mode == "live"


def test_whitelist_dan_blacklist_bentrok(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="whitelist dan blacklist"):
        load_config(
            _write(tmp_path, {"scanner": {"whitelist": ["BTCUSDT"], "blacklist": ["BTCUSDT"]}})
        )


def test_atr_min_max_tidak_masuk_akal(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="min_atr_pct"):
        load_config(_write(tmp_path, {"scanner": {"min_atr_pct": 5.0, "max_atr_pct": 2.0}}))


def test_be_sebelum_tp(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="be_trigger_r"):
        load_config(_write(tmp_path, {"trade": {"tp1_r": 1.0, "be_trigger_r": 3.0}}))


def test_yaml_tidak_valid(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("risk: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(path)


def test_file_tidak_ada() -> None:
    with pytest.raises(ConfigError, match="tidak ditemukan"):
        load_config("config-yang-tidak-ada.yaml")


def test_secrets_dari_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # supaya .env proyek tidak terbaca
    monkeypatch.setenv("BINANCE_API_KEY", "kunci-uji-1234567890")
    monkeypatch.setenv("BINANCE_API_SECRET", "rahasia-uji-0987654321")
    secrets = Secrets(_env_file=None)
    assert secrets.has_api_credentials
    assert secrets.api_key() == "kunci-uji-1234567890"
    # SecretStr tidak boleh tampil polos saat di-print/repr.
    assert "kunci-uji-1234567890" not in repr(secrets)
    assert "rahasia-uji-0987654321" not in str(secrets.model_dump())


def test_secrets_kosong(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    secrets = Secrets(_env_file=None)
    assert not secrets.has_api_credentials
    with pytest.raises(ConfigError, match="BINANCE_API_KEY"):
        secrets.api_key()


def test_mask_secret() -> None:
    assert mask_secret(None) == "<kosong>"
    assert mask_secret("abc") == "***"
    assert mask_secret("abcdefghijkl") == "abcd...ijkl"


def test_log_tidak_membocorkan_secret(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    secret = "APIKEY-SANGAT-RAHASIA-123"
    logger = logging.getLogger("uji.bocor")
    logger.addFilter(SecretRedactingFilter([secret]))
    with caplog.at_level(logging.INFO, logger="uji.bocor"):
        logger.info("kunci saya %s dan signature=abcdef123456", secret)
    assert secret not in caplog.text
    assert "***REDACTED***" in caplog.text


def test_sanitize_pola_binance() -> None:
    teks = (
        "GET /fapi/v1/order?symbol=BTCUSDT&signature=deadbeefcafe1234 "
        "header X-MBX-APIKEY: abcdef123456 listenKey=abcdefghijklmnop"
    )
    hasil = sanitize(teks)
    assert "deadbeefcafe1234" not in hasil
    assert "abcdef123456" not in hasil
    assert "abcdefghijklmnop" not in hasil


def test_sanitize_pesan_berformat() -> None:
    assert sanitize("token=abc123") == "token=***REDACTED***"
    assert sanitize(textwrap.dedent("tidak ada rahasia")) == "tidak ada rahasia"
