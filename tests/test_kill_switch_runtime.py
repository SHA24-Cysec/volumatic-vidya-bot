"""Test kill switch saat bot **berjalan** (bukan hanya saat start).

Bug yang dijaga: file kill switch dulu hanya diperiksa di ``is_blocked()``
(dipanggil ketika ada sinyal) dan saat start. ``touch KILL_SWITCH`` pada bot yang
sedang berjalan tidak melakukan apa pun sampai sinyal berikutnya datang — padahal
config menjanjikan "tutup semua & hentikan".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from volumatic_bot.binance.trading import OrderResult
from volumatic_bot.config import AppConfig, load_config
from volumatic_bot.engine.trader import TradingEngine
from volumatic_bot.execution.broker import ExchangePosition
from volumatic_bot.execution.executor import TradeExecutor
from volumatic_bot.positions.models import PositionState, TrackedPosition
from volumatic_bot.positions.store import PositionStore
from volumatic_bot.risk import DailyState, RiskManager
from volumatic_bot.risk.limits import DailyLimits
from volumatic_bot.storage.db import SqliteDatabase

ROOT = Path(__file__).parents[1]


class _Broker:
    name = "palsu"

    def __init__(self) -> None:
        self.ditutup: list[str] = []
        self.gagal_tutup = False

    async def place(self, order: Any) -> OrderResult:
        if order.purpose == "close":
            if self.gagal_tutup:
                raise RuntimeError("bursa menolak")
            self.ditutup.append(order.symbol)
            return OrderResult(
                symbol=order.symbol,
                order_id=1,
                client_order_id="x",
                side=order.side,
                type=order.type,
                status="FILLED",
                orig_qty=order.quantity or Decimal(0),
                executed_qty=order.quantity or Decimal(0),
                avg_price=Decimal("100"),
                stop_price=Decimal(0),
                reduce_only=True,
                close_position=False,
                purpose="close",
            )
        return OrderResult(
            symbol=order.symbol,
            order_id=1,
            client_order_id="x",
            side=order.side,
            type=order.type,
            status="NEW",
            orig_qty=Decimal(0),
            executed_qty=Decimal(0),
            avg_price=Decimal(0),
            stop_price=Decimal(0),
            reduce_only=False,
            close_position=False,
        )

    async def position(self, symbol: str) -> ExchangePosition | None:
        return None

    async def cancel(self, symbol: str, **_: Any) -> bool:
        return True

    async def status(self, symbol: str, **_: Any) -> OrderResult:
        raise KeyError(symbol)


@pytest.fixture()
def rakit(tmp_path: Path) -> Any:
    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["ops"]["mode"] = "testnet"
    dump["ops"]["kill_switch_file"] = str(tmp_path / "KILL_SWITCH")
    dump["data"]["db_path"] = str(tmp_path / "bot.db")
    config = AppConfig(**dump)

    db = SqliteDatabase(config.db_posisi())
    db.open()
    store = PositionStore(db)
    broker = _Broker()
    harian = DailyState(
        limits=DailyLimits(
            daily_loss_limit_pct=3.0,
            max_trades_per_day=20,
            consecutive_loss_limit=3,
            cooldown_minutes=120,
            reset_hour_local=0,
            timezone="Asia/Jakarta",
        ),
        start_equity=5000.0,
        current_equity=5000.0,
    )
    engine = TradingEngine(
        config,
        store=store,
        risk=RiskManager(config, daily=harian),
        executor=TradeExecutor(
            config, broker, store, clock=lambda: 1_700_000_000_000, mode="testnet"
        ),
        broker=broker,
    )
    yield config, store, engine, broker, Path(config.ops.kill_switch_file)
    db.close()


def _buka(store: PositionStore) -> None:
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
            mode="testnet",
            opened_at=1,
            updated_at=1,
        )
    )


@pytest.mark.asyncio
async def test_kill_switch_menutup_posisi_dan_menghentikan_sesi(rakit: Any) -> None:
    _, store, engine, broker, kunci = rakit
    _buka(store)
    kunci.write_text("stop\n", encoding="utf-8")

    assert await engine.check_kill_switch() is True
    assert engine.stopped is True
    assert "kill switch" in (engine.stop_reason or "")
    assert broker.ditutup == ["DOGEUSDT"]
    assert store.active_positions() == []
    assert any(i.kind == "kill_switch" for i in store.incidents(limit=5))


@pytest.mark.asyncio
async def test_tanpa_file_sesi_jalan_terus(rakit: Any) -> None:
    _, store, engine, broker, _ = rakit
    _buka(store)
    assert await engine.check_kill_switch() is False
    assert engine.stopped is False
    assert broker.ditutup == []


@pytest.mark.asyncio
async def test_kill_switch_gagal_tutup_tetap_menghentikan_sesi(rakit: Any) -> None:
    """Penutupan gagal tidak boleh membuat bot lanjut trading."""
    _, store, engine, broker, kunci = rakit
    _buka(store)
    broker.gagal_tutup = True
    kunci.write_text("stop\n", encoding="utf-8")

    assert await engine.check_kill_switch() is True
    assert engine.stopped is True


@pytest.mark.asyncio
async def test_periodic_ikut_memeriksa_kill_switch(rakit: Any) -> None:
    """Jaring pengaman: periodic() pun memeriksa, bukan hanya loop utama."""
    _, _store, engine, _, kunci = rakit
    kunci.write_text("stop\n", encoding="utf-8")
    await engine.periodic()
    assert engine.stopped is True


def test_konfigurasi_poll_kill_switch_masuk_akal() -> None:
    config = load_config(ROOT / "config.yaml")
    assert 1.0 <= config.ops.kill_switch_poll_sec <= 60.0


# ------------------------------------------------------------------ perintah CLI
def test_cli_pasang_dan_hapus_kill_switch(tmp_path: Path, capsys: Any) -> None:
    import yaml

    from volumatic_bot.cli import main

    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["ops"]["kill_switch_file"] = str(tmp_path / "KILL_SWITCH")
    dump["data"]["db_path"] = str(tmp_path / "bot.db")
    berkas = tmp_path / "cfg.yaml"
    berkas.write_text(yaml.safe_dump(dump, allow_unicode=True), encoding="utf-8")

    assert main(["--config", str(berkas), "kill-switch", "--status"]) == 0
    assert "tidak aktif" in capsys.readouterr().out

    assert main(["--config", str(berkas), "kill-switch"]) == 0
    assert (tmp_path / "KILL_SWITCH").exists()
    assert "DIPASANG" in capsys.readouterr().out

    assert main(["--config", str(berkas), "kill-switch", "--status"]) == 1
    assert "AKTIF" in capsys.readouterr().out

    assert main(["--config", str(berkas), "kill-switch", "--clear"]) == 0
    assert not (tmp_path / "KILL_SWITCH").exists()
    assert "dihapus" in capsys.readouterr().out
