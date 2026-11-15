"""Test pemisahan database per mode (posisi vs cache)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from volumatic_bot.config import AppConfig, load_config

ROOT = Path(__file__).parents[1]


def _config(mode: str, **data: Any) -> AppConfig:
    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["ops"]["mode"] = mode
    if mode == "live":
        dump["ops"]["allow_live"] = True
        dump["ops"]["live_confirmation"] = "SAYA-MENGERTI-RISIKONYA"
    dump["data"].update(data)
    return AppConfig(**dump)


@pytest.mark.parametrize(
    ("mode", "berkas"),
    [
        ("dry_run", "var/bot-dry-run.db"),
        ("backtest", "var/bot-backtest.db"),
        ("testnet", "var/bot-testnet.db"),
        ("live", "var/bot-live.db"),
    ],
)
def test_berkas_posisi_berbeda_per_mode(mode: str, berkas: str) -> None:
    assert _config(mode).db_posisi() == berkas


def test_cache_dipakai_bersama_semua_mode() -> None:
    """Cache candle sengaja tidak dipisah: data pasar sama untuk semua mode."""
    jalur = {_config(m).db_cache() for m in ("dry_run", "backtest", "testnet", "live")}
    assert jalur == {"var/cache.db"}


def test_pemisahan_bisa_dimatikan() -> None:
    """Kompatibilitas: siapa pun yang ingin satu berkas tetap bisa."""
    config = _config("testnet", separate_by_mode=False)
    assert config.db_posisi() == "var/bot.db"


def test_nama_berkas_tanpa_ekstensi() -> None:
    config = _config("testnet", db_path="var/bot")
    assert config.db_posisi() == "var/bot-testnet"


def test_cache_kosong_ikut_berkas_posisi() -> None:
    config = _config("dry_run", cache_db_path="")
    assert config.db_cache() == "var/bot-dry-run.db"


def test_posisi_tidak_tercampur_antar_mode(tmp_path: Path) -> None:
    """Posisi yang ditulis di mode dry_run tidak boleh terbaca di mode testnet."""
    from decimal import Decimal

    from volumatic_bot.positions.models import PositionState, TrackedPosition
    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.storage.db import SqliteDatabase

    kering = _config("dry_run", db_path=str(tmp_path / "bot.db"))
    uji = _config("testnet", db_path=str(tmp_path / "bot.db"))
    assert kering.db_posisi() != uji.db_posisi()

    db = SqliteDatabase(kering.db_posisi())
    db.open()
    try:
        store = PositionStore(db)
        store.save_position(
            TrackedPosition(
                symbol="DOGEUSDT",
                side="long",
                state=PositionState.OPEN,
                qty=Decimal("100"),
                remaining_qty=Decimal("100"),
                entry_price=Decimal("0.35"),
                stop_price=Decimal("0.34"),
                tp1_price=Decimal("0.37"),
                risk_per_unit=Decimal("0.01"),
                leverage=5,
                mode="dry_run",
                opened_at=1_700_000_000_000,
                updated_at=1_700_000_000_000,
            )
        )
    finally:
        db.close()

    db2 = SqliteDatabase(uji.db_posisi())
    db2.open()
    try:
        assert PositionStore(db2).active_positions() == []
    finally:
        db2.close()


def test_preflight_menampilkan_kedua_berkas(tmp_path: Path, capsys: Any) -> None:
    from volumatic_bot.cli import main

    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["data"]["db_path"] = str(tmp_path / "bot.db")
    dump["data"]["cache_db_path"] = str(tmp_path / "cache.db")
    berkas = tmp_path / "cfg.yaml"
    berkas.write_text(yaml.safe_dump(dump, allow_unicode=True), encoding="utf-8")

    assert main(["--config", str(berkas), "preflight", "--equity", "50"]) == 0
    keluaran = capsys.readouterr().out
    assert "bot-dry-run.db" in keluaran
    assert "cache.db" in keluaran
