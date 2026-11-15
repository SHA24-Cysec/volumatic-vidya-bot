"""Test penyusunan order proteksi + CLI eksekusi (``simulate-trade``/``positions``)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from volumatic_bot.cli import main
from volumatic_bot.execution.protect import build_protection_orders

ROOT = Path(__file__).parents[1]


# ------------------------------------------------------------------- proteksi
def test_sl_selalu_closeposition_tanpa_qty() -> None:
    hasil = build_protection_orders(
        symbol="DOGEUSDT",
        direction="long",
        qty=Decimal("133"),
        stop_price=Decimal("0.3444"),
        tp1_price=Decimal("0.3584"),
    )
    sl = hasil.stop_loss
    assert sl.type == "STOP_MARKET"
    assert sl.side == "SELL"
    assert sl.close_position is True
    assert sl.reduce_only is False  # kombinasi ini ditolak Binance
    assert sl.quantity is None
    assert sl.working_type == "CONTRACT_PRICE"


def test_tp_penuh_reduce_only_dengan_qty() -> None:
    hasil = build_protection_orders(
        symbol="DOGEUSDT",
        direction="long",
        qty=Decimal("133"),
        stop_price=Decimal("0.3444"),
        tp1_price=Decimal("0.3584"),
    )
    assert hasil.take_profit is not None
    tp = hasil.take_profit
    assert tp.type == "TAKE_PROFIT_MARKET"
    assert tp.quantity == Decimal("133")
    assert tp.reduce_only is True
    assert tp.close_position is False
    assert hasil.partial_tp is None


def test_short_memakai_side_buy() -> None:
    hasil = build_protection_orders(
        symbol="DOGEUSDT",
        direction="short",
        qty=Decimal("100"),
        stop_price=Decimal("0.36"),
        tp1_price=Decimal("0.34"),
    )
    assert hasil.stop_loss.side == "BUY"
    assert hasil.take_profit is not None and hasil.take_profit.side == "BUY"


def test_partial_tp_hanya_sebagian_yang_punya_tp() -> None:
    """Sisa posisi TIDAK diberi TP agar tidak keluar bersamaan di TP1."""
    hasil = build_protection_orders(
        symbol="DOGEUSDT",
        direction="long",
        qty=Decimal("133"),
        stop_price=Decimal("0.3444"),
        tp1_price=Decimal("0.3584"),
        partial_qty=Decimal("66"),
    )
    assert hasil.partial_tp is not None
    assert hasil.partial_tp.quantity == Decimal("66")
    assert hasil.partial_tp.reduce_only is True
    assert hasil.partial_tp.purpose == "partial_tp"
    assert hasil.take_profit is None  # sisa berjalan dengan BE + trailing
    assert len(hasil.all()) == 2  # SL + TP parsial
    assert "STOP_MARKET" in hasil.summary()


def test_partial_qty_tidak_valid_diabaikan() -> None:
    for qty_partial in (Decimal(0), Decimal("133"), Decimal("200")):
        hasil = build_protection_orders(
            symbol="DOGEUSDT",
            direction="long",
            qty=Decimal("133"),
            stop_price=Decimal("0.3444"),
            tp1_price=Decimal("0.3584"),
            partial_qty=qty_partial,
        )
        assert hasil.take_profit is not None  # jatuh ke TP penuh
        assert hasil.partial_tp is None


def test_validasi_input() -> None:
    with pytest.raises(ValueError, match="qty"):
        build_protection_orders(
            symbol="DOGEUSDT",
            direction="long",
            qty=Decimal(0),
            stop_price=Decimal("1"),
            tp1_price=Decimal("2"),
        )
    with pytest.raises(ValueError, match="arah"):
        build_protection_orders(
            symbol="DOGEUSDT",
            direction="naik",
            qty=Decimal(1),
            stop_price=Decimal("1"),
            tp1_price=Decimal("2"),
        )


def test_client_id_factory_dipakai() -> None:
    hasil = build_protection_orders(
        symbol="DOGEUSDT",
        direction="long",
        qty=Decimal("10"),
        stop_price=Decimal("0.34"),
        tp1_price=Decimal("0.36"),
        partial_qty=Decimal("5"),
        client_id_factory=lambda purpose: f"vbot-{purpose}",
    )
    assert hasil.stop_loss.client_order_id == "vbot-sl"
    assert hasil.partial_tp is not None
    assert hasil.partial_tp.client_order_id == "vbot-tp1"


# --------------------------------------------------------------------- CLI
def _config(tmp_path: Path) -> Path:
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["data"]["db_path"] = str(tmp_path / "uji.db")
    raw["ops"]["kill_switch_file"] = str(tmp_path / "KILL_SWITCH")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_simulate_trade_siklus_penuh(tmp_path: Path, capsys: Any) -> None:
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
            "--trail",
            "0.345",
            "--path",
            "0.3585,0.3455",
        ]
    )
    tangkapan = capsys.readouterr()
    assert kode == 0, tangkapan.out
    assert "Risiko: DISETUJUI" in tangkapan.out
    assert "order terpicu" in tangkapan.out
    assert "Status akhir" in tangkapan.out
    assert "closed" in tangkapan.out
    assert "State harian disimpan" in tangkapan.out


def test_simulate_trade_simbol_tidak_ada(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(tmp_path)
    kode = main(["--config", str(cfg), "simulate-trade", "TIDAKADAUSDT"])
    assert kode == 1
    assert "tidak ada di contoh exchangeInfo" in capsys.readouterr().err


def test_simulate_trade_ditolak_risiko(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(tmp_path)
    kode = main(
        [
            "--config",
            str(cfg),
            "simulate-trade",
            "BTCUSDT",
            "--equity",
            "100",
            "--entry",
            "90000",
            "--atr-pct",
            "1",
        ]
    )
    out = capsys.readouterr().out
    assert kode == 1
    assert "DITOLAK" in out


def test_positions_menampilkan_state_lokal(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(tmp_path)
    main(
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
    capsys.readouterr()
    kode = main(["--config", str(cfg), "positions"])
    out = capsys.readouterr().out
    assert kode == 0
    assert "Posisi aktif: 0" in out
    assert "DOGEUSDT" in out
    assert "closed" in out


def test_reconcile_menolak_mode_dry_run_tanpa_force(tmp_path: Path, capsys: Any) -> None:
    cfg = _config(tmp_path)
    kode = main(["--config", str(cfg), "reconcile"])
    assert kode == 2
    assert "dry_run" in capsys.readouterr().err
