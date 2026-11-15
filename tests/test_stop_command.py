"""Test perintah ``stop`` dan berkas PID.

Latar belakang: setelah bot dijalankan, pengguna harus bisa menghentikannya tanpa
``ps -ef | grep``. Sebelumnya tidak ada cara resmi, sehingga orang menebak-nebak
apakah bot benar-benar berhenti.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from volumatic_bot.cli import _berkas_pid, _proses_hidup, main
from volumatic_bot.config import load_config

ROOT = Path(__file__).parents[1]


@pytest.fixture()
def cfg(tmp_path: Path) -> Path:
    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["ops"]["mode"] = "testnet"
    dump["ops"]["kill_switch_file"] = str(tmp_path / "KILL_SWITCH")
    dump["data"]["db_path"] = str(tmp_path / "bot.db")
    berkas = tmp_path / "cfg.yaml"
    berkas.write_text(yaml.safe_dump(dump, allow_unicode=True), encoding="utf-8")
    return berkas


def test_pid_file_mengikuti_mode(tmp_path: Path) -> None:
    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["data"]["db_path"] = str(tmp_path / "bot.db")

    from volumatic_bot.config import AppConfig

    dump["ops"]["mode"] = "testnet"
    assert AppConfig(**dump).pid_file().endswith("bot-testnet.pid")
    dump["ops"]["mode"] = "live"
    dump["ops"]["allow_live"] = True
    dump["ops"]["live_confirmation"] = "SAYA-MENGERTI-RISIKONYA"
    assert AppConfig(**dump).pid_file().endswith("bot-live.pid")


def _proses_tiruan() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def test_proses_hidup_mendeteksi_pid(cfg: Path) -> None:
    p = _proses_tiruan()
    try:
        assert _proses_hidup(p.pid) is True
    finally:
        p.terminate()
        p.wait(timeout=10)
    assert _proses_hidup(p.pid) is False


def test_stop_menghentikan_proses_dan_menghapus_berkas(cfg: Path, capsys: Any) -> None:
    p = _proses_tiruan()
    jalur_pid = Path(str(load_config(str(cfg)).db_posisi()).replace(".db", ".pid"))
    jalur_pid.write_text(f"{p.pid} {int(time.time())}\n", encoding="utf-8")
    try:
        kode = main(["--config", str(cfg), "stop", "--timeout", "10"])
        keluaran = capsys.readouterr().out
        assert kode == 0
        assert "HENTIKAN" in keluaran
        assert "berhenti." in keluaran
        assert not jalur_pid.exists()
        assert _proses_hidup(p.pid) is False
    finally:
        if p.poll() is None:  # pragma: no cover - jaring pengaman
            p.kill()
            p.wait(timeout=10)


def test_stop_membersihkan_berkas_pid_usang(cfg: Path, capsys: Any) -> None:
    p = _proses_tiruan()
    p.terminate()
    p.wait(timeout=10)
    jalur_pid = Path(str(load_config(str(cfg)).db_posisi()).replace(".db", ".pid"))
    jalur_pid.write_text(f"{p.pid} {int(time.time())}\n", encoding="utf-8")

    kode = main(["--config", str(cfg), "stop", "--timeout", "1"])
    keluaran = capsys.readouterr().out
    assert kode == 0
    assert "usang" in keluaran
    assert not jalur_pid.exists()


def test_stop_tanpa_proses_berjalan(cfg: Path, capsys: Any) -> None:
    kode = main(["--config", str(cfg), "stop"])
    assert kode == 1
    assert "tidak ada berkas PID" in capsys.readouterr().out


def test_stop_close_memasang_kill_switch(cfg: Path, capsys: Any) -> None:
    p = _proses_tiruan()
    jalur_pid = Path(str(load_config(str(cfg)).db_posisi()).replace(".db", ".pid"))
    jalur_pid.write_text(f"{p.pid} {int(time.time())}\n", encoding="utf-8")
    try:
        main(["--config", str(cfg), "stop", "--close", "--timeout", "10"])
        capsys.readouterr()
        assert Path(str(cfg.with_name("KILL_SWITCH"))).exists()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)


def test_berkas_pid_all_modes(cfg: Path, tmp_path: Path) -> None:
    config = load_config(str(cfg))
    (tmp_path / "bot-dry-run.pid").write_text("1 0\n", encoding="utf-8")
    (tmp_path / "bot-testnet.pid").write_text("2 0\n", encoding="utf-8")
    nama = {p.name for p in _berkas_pid(config, semua=True)}
    assert {"bot-dry-run.pid", "bot-testnet.pid"} <= nama


@pytest.mark.asyncio
async def test_run_menolak_dua_sesi_pada_mode_sama(tmp_path: Path) -> None:
    """PID file mencegah dua bot jalan bersamaan di mode yang sama."""
    from volumatic_bot.engine.live import _tulis_pid

    p = _proses_tiruan()
    try:
        jalur = tmp_path / "bot-testnet.pid"
        jalur.write_text(f"{p.pid} {int(time.time())}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="sudah berjalan"):
            _tulis_pid(jalur)
    finally:
        p.terminate()
        p.wait(timeout=10)
