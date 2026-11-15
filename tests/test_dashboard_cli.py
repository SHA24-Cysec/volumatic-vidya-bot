"""Test perintah CLI ``dashboard`` dan integrasi notifikasi ``simulate-trade``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from volumatic_bot.cli import main

ROOT = Path(__file__).parents[1]


def _config(tmp_path: Path, **ubah: Any) -> Path:
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["data"]["db_path"] = str(tmp_path / "dash_cli.db")
    raw["ops"]["kill_switch_file"] = str(tmp_path / "KILL_SWITCH")
    raw.setdefault("dashboard", {}).update(ubah.get("dashboard", {}))
    if "notify" in ubah:
        raw.setdefault("notify", {}).update(ubah["notify"])
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def _simulasi(cfg: Path) -> None:
    kode = main(
        [
            "--config",
            str(cfg),
            "simulate-trade",
            "DOGEUSDT",
            "--equity",
            "50",
            "--entry",
            "0.35",
            "--atr-pct",
            "2",
            "--trail",
            "0.345",
            "--path",
            "0.3585,0.3455",
        ]
    )
    assert kode == 0


def test_dashboard_demo(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(tmp_path)
    kode = main(["--config", str(cfg), "dashboard", "--demo", "--once"])
    out = capsys.readouterr().out
    assert kode == 0, out
    assert "STATUS BOT" in out
    assert "CONTOH TAMPILAN" in out
    assert "KANDIDAT" in out


def test_dashboard_demo_compact(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(tmp_path)
    assert main(["--config", str(cfg), "dashboard", "--demo", "--compact", "--once"]) == 0
    out = capsys.readouterr().out
    assert "RINGKAS" in out
    assert "DOGEUSDT LONG" in out


def test_dashboard_membaca_state_lokal(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(tmp_path)
    _simulasi(cfg)
    capsys.readouterr()
    kode = main(["--config", str(cfg), "dashboard", "--once"])
    out = capsys.readouterr().out
    assert kode == 0, out
    assert "DOGEUSDT" in out
    assert "POSISI" in out
    assert "trade 1/20" in out or "1/20" in out


def test_dashboard_dimatikan_di_config(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(tmp_path, dashboard={"enabled": False})
    kode = main(["--config", str(cfg), "dashboard", "--once"])
    assert kode == 1
    assert "dimatikan" in capsys.readouterr().err


def test_dashboard_tidak_meninggalkan_handler(tmp_path: Path, capsys: Any) -> None:
    from volumatic_bot.dashboard import RingBufferHandler

    cfg = _config(tmp_path)
    logger = logging.getLogger("volumatic_bot")
    main(["--config", str(cfg), "dashboard", "--once"])
    assert not any(isinstance(h, RingBufferHandler) for h in logger.handlers)
    capsys.readouterr()


def test_simulate_trade_mengirim_notifikasi(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(tmp_path)
    _simulasi(cfg)
    # Notifikasi konsol ditulis lewat logging → keluar di stderr.
    hasil = capsys.readouterr()
    gabung = hasil.out + hasil.err
    assert "[NOTIF]" in gabung
    assert "entry LONG DOGEUSDT" in gabung
    assert "keluar DOGEUSDT" in gabung
    assert "dilewati" not in gabung  # tiket disetujui, jadi tidak ada pesan "dilewati"


def test_simulate_trade_no_notify(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(tmp_path)
    kode = main(
        [
            "--config",
            str(cfg),
            "simulate-trade",
            "DOGEUSDT",
            "--equity",
            "50",
            "--entry",
            "0.35",
            "--atr-pct",
            "2",
            "--no-notify",
        ]
    )
    hasil = capsys.readouterr()
    assert kode == 0, hasil.out
    assert "[NOTIF]" not in hasil.out + hasil.err


def test_notify_dimatikan_lewat_config(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(
        tmp_path,
        notify={"notify_on_trade": False, "notify_on_error": False},
    )
    _simulasi(cfg)
    hasil = capsys.readouterr()
    assert "[NOTIF]" not in hasil.out + hasil.err


@pytest.mark.parametrize("level", ["warning", "critical"])
def test_min_level_memfilter_notifikasi_info(tmp_path: Path, capsys: Any, level: str) -> None:
    cfg = _config(tmp_path, notify={"min_level": level})
    _simulasi(cfg)
    hasil = capsys.readouterr()
    assert "[NOTIF]" not in hasil.out + hasil.err
