"""Test modal sangat kecil: equity 5 USDT (minimum Binance) dan profil `modal-5usdt`.

Kenyataan yang diuji di sini:

* pada equity **5,00 USDT** bot masih bisa membuka posisi (minNotional 5 USDT,
  margin 0,50 USDT di 10x);
* pada profil `modal-kecil` (lantai 5 USDT) satu kerugian 5% saja sudah
  menjatuhkan equity ke 4,75 → bot **berhenti membuka posisi baru**. Ini
  perilaku yang disengaja, bukan crash;
* profil `modal-5usdt` menurunkan lantai ke 2 USDT dan risiko ke 3% sehingga
  modal 5 USDT masih punya ruang bernapas.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.config import RiskConfig, load_config
from volumatic_bot.risk import DailyState, Portfolio, RiskManager, TradeTicket
from volumatic_bot.risk.limits import DailyLimits

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"
JKT = ZoneInfo("Asia/Jakarta")


def exchange() -> Any:
    payload = json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
    return parse_exchange_info(payload)


def tiket(profil: str, equity: float, *, symbol: str = "DOGEUSDT") -> TradeTicket:
    cfg = load_config(ROOT / "config.yaml", profil=profil)
    daily = DailyState(
        limits=DailyLimits(
            daily_loss_limit_pct=cfg.risk.daily_loss_limit_pct,
            max_trades_per_day=cfg.risk.max_trades_per_day,
            consecutive_loss_limit=cfg.risk.consecutive_loss_limit,
            cooldown_minutes=cfg.risk.cooldown_minutes,
            reset_hour_local=0,
            timezone="Asia/Jakarta",
        ),
        start_equity=equity,
        current_equity=equity,
    )
    mgr = RiskManager(
        cfg,
        daily=daily,
        clock=lambda: datetime(2026, 11, 16, 10, 0, tzinfo=JKT),
        kill_switch_path=Path("/tmp/tidak-ada-kill-5usdt"),
    )
    return mgr.evaluate(
        symbol=symbol,
        side="long",
        entry_price=0.35,
        atr=0.007,  # SL ~2% harga
        trail=0.345,
        equity=equity,
        portfolio=Portfolio(
            equity=equity,
            positions=(),
            max_total_margin_pct=cfg.risk.max_total_margin_pct,
            max_per_symbol_margin_pct=cfg.risk.max_per_symbol_margin_pct,
        ),
        info=exchange().get(symbol),
        open_positions=0,
    )


# ----------------------------------------------------------------- profil baru
def test_profil_modal_5usdt_dimuat() -> None:
    cfg = load_config(ROOT / "config.yaml", profil="modal-5usdt")
    assert cfg.risk.risk_per_trade_pct == 3.0
    assert cfg.risk.min_equity_usdt == 2.0
    assert cfg.risk.daily_loss_limit_pct == 6.0
    assert cfg.risk.max_trades_per_day == 4
    assert cfg.risk.consecutive_loss_limit == 2
    assert cfg.risk.max_concurrent_positions == 1
    assert cfg.exchange.default_leverage == 10
    assert cfg.scanner.max_min_notional_usdt == 5.0
    assert cfg.ops.mode == "dry_run"


def test_lantai_equity_tidak_boleh_nol() -> None:
    """Batas bawah tetap ada: equity 0/negatif tidak masuk akal."""
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        RiskConfig(min_equity_usdt=0.0, allow_aggressive_risk=True)
    assert RiskConfig(min_equity_usdt=1.0, allow_aggressive_risk=True).min_equity_usdt == 1.0


def test_lantai_di_bawah_20_tetap_butuh_saklar() -> None:
    with pytest.raises(ValidationError, match="equity minimum"):
        RiskConfig(min_equity_usdt=2.0)
    assert RiskConfig(min_equity_usdt=2.0, allow_aggressive_risk=True).min_equity_usdt == 2.0


# ------------------------------------------------------------- equity 5 USDT
def test_equity_5_dengan_profil_modal_kecil_bisa_trading() -> None:
    hasil = tiket("modal-kecil", 5.0)
    assert hasil.approved, hasil.reasons
    assert hasil.size is not None and hasil.plan is not None
    assert hasil.size.risk_usdt == pytest.approx(0.25, abs=0.02)  # 5% x 5 USDT
    assert float(hasil.size.notional) >= 5.0  # tidak di bawah minNotional
    assert float(hasil.size.margin) <= 5.0 * 0.8


def test_satu_kerugian_pada_modal_kecil_menghentikan_entry_baru() -> None:
    """Ini inti jawaban untuk modal 5 USDT: lantai 5 USDT = sekali kalah, stop."""
    hasil = tiket("modal-kecil", 4.75)  # 5,00 - 0,25 (satu SL penuh)
    assert hasil.rejected
    assert hasil.size is None and hasil.plan is None
    pesan = " ".join(hasil.reasons)
    assert "minimum 5.00 USDT" in pesan
    assert "modal-kecil.md" in pesan  # petunjuk jalan keluar ada di pesan
    assert "allow_aggressive_risk" in pesan


def test_equity_499_ditolak_tanpa_crash() -> None:
    hasil = tiket("modal-kecil", 4.99)
    assert hasil.rejected and hasil.plan is None


def test_profil_5usdt_tetap_bisa_trading_setelah_satu_kerugian() -> None:
    for equity in (5.0, 4.5, 3.0):
        hasil = tiket("modal-5usdt", equity)
        assert hasil.approved, (equity, hasil.reasons)
        assert hasil.size is not None
        assert hasil.size.risk_usdt == pytest.approx(equity * 0.03, abs=0.02)


def test_di_bawah_2_usdt_pembulatan_mulai_menolak() -> None:
    """Bukan lantai config, tapi pembulatan: qty minimum memaksa risiko lebih besar.

    Dengan minNotional 5 USDT dan SL ~2%, risiko minimum ~0,10 USDT. Pada equity
    2,1 USDT target 3% hanya 0,063 USDT, jadi pembulatan menaikkan risiko >33%
    dan bot memilih **melewati** trade (tidak membesarkannya diam-diam).
    """
    hasil = tiket("modal-5usdt", 2.1)
    assert hasil.rejected
    assert any("pembulatan" in r for r in hasil.reasons)


def test_profil_5usdt_berhenti_di_bawah_lantainya_sendiri() -> None:
    hasil = tiket("modal-5usdt", 1.9)
    assert hasil.rejected
    assert "minimum 2.00 USDT" in " ".join(hasil.reasons)


def test_risiko_tetap_di_bawah_batas_tidak_melebihi_min_notional() -> None:
    """Notional tidak boleh lebih kecil dari minNotional 5 USDT."""
    hasil = tiket("modal-5usdt", 5.0)
    assert hasil.size is not None
    assert float(hasil.size.notional) >= 5.0
