"""Test sistem profil di dalam **satu** `config.yaml`.

Profil dibuat supaya tidak perlu file config kedua: semua setelan tetap tinggal
di `config.yaml`, dan bagian ``profil.<nama>`` hanya **menimpa** nilai biasa
bila dipilih (lewat ``profil_aktif:`` atau ``--profil``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from volumatic_bot.config import (
    AppConfig,
    ConfigError,
    baca_yaml,
    daftar_profil,
    load_config,
    profil_aktif_dari,
    terapkan_profil,
)

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config.yaml"


def _tulis(tmp_path: Path, isi: dict[str, Any]) -> Path:
    jalur = tmp_path / "config.yaml"
    jalur.write_text(yaml.safe_dump(isi, allow_unicode=True), encoding="utf-8")
    return jalur


def test_config_bawaan_tetap_dry_run_dan_tanpa_profil() -> None:
    """Permintaan pengguna: satu file, dan bawaannya mode aman."""
    raw = baca_yaml(CONFIG)
    assert raw["profil_aktif"] == ""
    assert set(daftar_profil(raw)) == {"modal-kecil", "modal-5usdt"}
    cfg = load_config(CONFIG)
    assert cfg.ops.mode == "dry_run"
    # profil tidak aktif -> nilai biasa yang dipakai
    assert cfg.risk.risk_per_trade_pct == 1.5
    assert cfg.exchange.default_leverage == 5
    assert cfg.scanner.max_min_notional_usdt == 0.0


def test_profil_modal_kecil_lewat_argumen() -> None:
    cfg = load_config(CONFIG, profil="modal-kecil")
    assert isinstance(cfg, AppConfig)
    assert cfg.risk.risk_per_trade_pct == 5.0
    assert cfg.risk.allow_aggressive_risk is True
    assert cfg.risk.max_concurrent_positions == 1
    assert cfg.risk.max_per_symbol_margin_pct == 100.0
    assert cfg.risk.max_total_margin_pct == 80.0
    assert cfg.risk.min_equity_usdt == 5.0
    assert cfg.exchange.default_leverage == 10
    assert cfg.exchange.max_leverage == 10
    assert cfg.scanner.max_min_notional_usdt == 5.0
    # nilai yang tidak disebut profil tetap memakai isi file
    assert cfg.scanner.timeframe == load_config(CONFIG).scanner.timeframe
    assert cfg.trade == load_config(CONFIG).trade


def test_profil_aktif_di_file_mengaktifkan_tanpa_argumen() -> None:
    raw = baca_yaml(CONFIG)
    raw["profil_aktif"] = "modal-kecil"
    rapi, nama = terapkan_profil(raw, None)
    assert nama == "modal-kecil"
    cfg = AppConfig.model_validate(rapi)
    assert cfg.risk.risk_per_trade_pct == 5.0


def test_argumen_menimpa_profil_aktif_di_file() -> None:
    raw = baca_yaml(CONFIG)
    raw["profil_aktif"] = "modal-kecil"
    assert profil_aktif_dari(raw, None) == "modal-kecil"
    # string kosong dari CLI diperlakukan sebagai "ikut file", bukan "matikan"
    assert profil_aktif_dari(raw, "") == "modal-kecil"
    _rapi, nama = terapkan_profil(raw, "")
    assert nama == "modal-kecil"


def test_kunci_profil_tidak_bocor_ke_appconfig() -> None:
    """``profil``/``profil_aktif`` milik lapisan pemilihan, bukan AppConfig."""
    rapi, _ = terapkan_profil(baca_yaml(CONFIG), None)
    assert "profil" not in rapi
    assert "profil_aktif" not in rapi


def test_profil_tidak_dikenal_menyebutkan_pilihan(tmp_path: Path) -> None:
    raw = baca_yaml(CONFIG)
    with pytest.raises(ConfigError, match="modal-kecil"):
        terapkan_profil(raw, "modal-kecil-typо")  # salah ketik
    jalur = tmp_path / "config.yaml"
    jalur.write_text(yaml.safe_dump({**raw, "profil": {}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="tidak ada"):
        load_config(jalur, profil="apa-saja")


def test_bentuk_profil_salah_ditolak() -> None:
    with pytest.raises(ConfigError, match="mapping"):
        daftar_profil({"profil": ["modal-kecil"]})
    with pytest.raises(ConfigError, match="mapping"):
        daftar_profil({"profil": {"modal-kecil": 5}})


def test_salah_ketik_di_dalam_profil_tetap_ditolak(tmp_path: Path) -> None:
    """extra=forbid berlaku juga untuk nilai yang datang dari profil."""
    raw = baca_yaml(CONFIG)
    raw["profil"] = {"rusak": {"risk": {"risk_per_trad_pct": 5.0}}}  # typo
    jalur = tmp_path / "config.yaml"
    jalur.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="konfigurasi tidak valid"):
        load_config(jalur, profil="rusak")


def test_profil_tidak_bisa_membuka_mode_live(tmp_path: Path) -> None:
    """Profil tidak boleh jadi celah: pagar live tetap berlaku."""
    raw = baca_yaml(CONFIG)
    raw["profil"] = {"nakal": {"ops": {"mode": "live"}}}
    jalur = tmp_path / "config.yaml"
    jalur.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="konfigurasi tidak valid"):
        load_config(jalur, profil="nakal")


def test_profil_tetap_tunduk_pada_plafon_25_persen(tmp_path: Path) -> None:
    raw = baca_yaml(CONFIG)
    raw["profil"] = {
        "all-in": {"risk": {"risk_per_trade_pct": 100.0, "allow_aggressive_risk": True}}
    }
    jalur = tmp_path / "config.yaml"
    jalur.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="less than or equal to 25"):
        load_config(jalur, profil="all-in")


def test_profil_bisa_dimatikan_dari_config_yang_sama() -> None:
    """Saklar bisa dicabut: profil agresif tanpa allow_aggressive_risk ditolak."""
    raw = baca_yaml(CONFIG)
    raw["profil"]["modal-kecil"]["risk"]["allow_aggressive_risk"] = False
    with pytest.raises(ValidationError, match="allow_aggressive_risk"):
        AppConfig.model_validate(terapkan_profil(raw, "modal-kecil")[0])
