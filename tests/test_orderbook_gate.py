"""Test aturan filter order book, skor, peringkat, dan pipeline sinyal.

Semua angka ambang diambil dari :class:`OrderBookConfig`, jadi test ini sekaligus
dokumentasi perilaku default yang dipakai bot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from volumatic_bot.binance.symbols import parse_exchange_info
from volumatic_bot.config import AppConfig, OrderBookConfig, ScoringConfig, load_config
from volumatic_bot.orderbook.depth import DepthUnavailable
from volumatic_bot.orderbook.gate import evaluate
from volumatic_bot.orderbook.models import BookLevel, OrderBookSnapshot
from volumatic_bot.signals.pipeline import SignalGate, summarize
from volumatic_bot.signals.ranking import (
    Candidate,
    book_score,
    combine,
    rank_candidates,
    trend_strength,
    volume_score,
)
from volumatic_bot.streams.kline_feed import SignalEvent

DATA = Path(__file__).parent / "data"


def book(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    fetched_at: float = 0.0,
    symbol: str = "BTCUSDT",
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol=symbol,
        bids=tuple(BookLevel(p, q) for p, q in bids),
        asks=tuple(BookLevel(p, q) for p, q in asks),
        last_update_id=1,
        event_time_ms=0,
        source="rest",
        fetched_at=fetched_at,
    )


def cfg(**kwargs: Any) -> OrderBookConfig:
    return OrderBookConfig(**kwargs)


def signal(
    *,
    symbol: str = "BTCUSDT",
    direction: str = "long",
    close: float = 100.0,
    atr: float = 0.5,
    cmo: float = 40.0,
    vol_delta_win_pct: float = 30.0,
) -> SignalEvent:
    return SignalEvent(
        symbol=symbol,
        direction=direction,
        open_time=1_764_500_000_000,
        close_price=close,
        upper=close + 1,
        lower=close - 1,
        trail=close,
        atr=atr,
        atr_pct=atr / close * 100,
        cmo=cmo,
        trend=1 if direction == "long" else -1,
        vol_delta_pct=1.0,
        vol_delta_win_pct=vol_delta_win_pct,
        zone_count=3.0,
        score=0.5,
        bars=1500,
    )


# ------------------------------------------------------------------ perilaku
def test_filter_mati_selalu_netral() -> None:
    decision = evaluate(
        None,
        direction="long",
        tick_size=0.1,
        target_qty=1.0,
        atr=0.5,
        config=cfg(enabled=False),
        now=0.0,
    )
    assert decision.enabled is False
    assert decision.accepted is False
    assert decision.score == 0.5
    assert "dimatikan" in decision.summary()
    assert book_score(decision) == 0.5  # netral untuk skor gabungan


def test_depth_tidak_tersedia_ditolak() -> None:
    decision = evaluate(
        None,
        direction="long",
        tick_size=0.1,
        target_qty=1.0,
        atr=0.5,
        config=cfg(reject_if_unavailable=True),
        now=0.0,
        unavailable_reason="belum ada snapshot depth",
    )
    assert decision.accepted is False
    assert decision.score == 0.0
    assert "depth tidak tersedia" in decision.reasons[0]
    assert "belum ada snapshot depth" in decision.reasons[0]


def test_depth_tidak_tersedia_diloloskan_bila_config_mengizinkan() -> None:
    decision = evaluate(
        None,
        direction="long",
        tick_size=0.1,
        target_qty=1.0,
        atr=0.5,
        config=cfg(reject_if_unavailable=False),
        now=0.0,
        unavailable_reason="timeout",
    )
    assert decision.accepted is True
    assert decision.score == 0.5
    assert decision.metrics is None
    assert "reject_if_unavailable=false" in decision.notes[0]


def test_buku_bersilangan_ditolak() -> None:
    decision = evaluate(
        book([(101.0, 1.0)], [(100.0, 1.0)]),
        direction="long",
        tick_size=0.1,
        target_qty=1.0,
        atr=0.5,
        config=cfg(),
        now=0.0,
    )
    assert decision.accepted is False
    assert "buku rusak" in decision.reasons[0]
    assert decision.metrics is None


def test_spread_terlalu_lebar_ditolak() -> None:
    # spread 1.0% jauh di atas batas default 0.05%
    decision = evaluate(
        book([(99.0, 5.0)], [(100.0, 5.0)]),
        direction="long",
        tick_size=0.1,
        target_qty=1.0,
        atr=0.5,
        config=cfg(),
        now=0.0,
    )
    assert decision.accepted is False
    assert any("spread" in r for r in decision.reasons)


def test_snapshot_basi_ditolak() -> None:
    decision = evaluate(
        book([(99.95, 5.0)], [(100.05, 5.0)], fetched_at=100.0),
        direction="long",
        tick_size=0.1,
        target_qty=1.0,
        atr=0.5,
        config=cfg(depth_snapshot_ttl_sec=3.0),
        now=110.0,
    )
    assert decision.accepted is False
    assert any("basi" in r for r in decision.reasons)


def test_sinyal_diterima_saat_buku_sehat_dan_imbalance_mendukung() -> None:
    decision = evaluate(
        book([(99.995, 6.0), (99.99, 4.0)], [(100.005, 1.0), (100.01, 1.0)]),
        direction="long",
        tick_size=0.001,
        target_qty=1.0,
        atr=0.5,
        config=cfg(),
        now=0.0,
    )
    assert decision.accepted is True
    assert decision.reasons == ()
    assert decision.score > 0.7  # bid jauh lebih tebal -> mendukung long
    assert any("mendukung" in n for n in decision.notes)
    assert "DITERIMA" in decision.summary()


def test_imbalance_melawan_arah_menurunkan_skor() -> None:
    lawan = evaluate(
        book([(99.95, 1.0), (99.90, 1.0)], [(100.05, 6.0), (100.10, 4.0)]),
        direction="long",
        tick_size=0.01,
        target_qty=1.0,
        atr=0.5,
        config=cfg(),
        now=0.0,
    )
    assert lawan.score < 0.5
    assert any("melawan" in n for n in lawan.notes)


def test_skor_minimum_dapat_menolak() -> None:
    decision = evaluate(
        book([(99.995, 1.0), (99.99, 1.0)], [(100.005, 6.0), (100.01, 4.0)]),
        direction="long",
        tick_size=0.001,
        target_qty=1.0,
        atr=0.5,
        config=cfg(min_score_to_enter=0.9),
        now=0.0,
    )
    assert decision.accepted is False
    assert any("ambang minimum" in r for r in decision.reasons)


def test_wall_di_depan_arah_long_ditolak() -> None:
    decision = evaluate(
        book(
            [(99.995, 5.0)],
            [(100.005, 0.5), (100.01, 0.5), (100.02, 30.0)],
        ),
        direction="long",
        tick_size=0.001,
        target_qty=0.2,
        atr=1.0,  # wall 0.15 dari mid = 0.15 ATR -> dalam ambang 1.0 ATR
        config=cfg(),
        now=0.0,
    )
    assert decision.accepted is False
    assert any("wall" in r and "menghalangi" in r for r in decision.reasons)


def test_wall_jauh_hanya_catatan() -> None:
    decision = evaluate(
        book(
            [(99.995, 5.0)],
            [(100.005, 0.5), (100.01, 0.5), (100.02, 30.0)],
        ),
        direction="long",
        tick_size=0.001,
        target_qty=0.2,
        atr=100.0,  # wall hanya 0.0015 ATR dari mid? tidak: 0.15/100
        config=cfg(wall_proximity_atr=0.0001),
        now=0.0,
    )
    assert any("di luar ambang" in n for n in decision.notes)


def test_likuiditas_tidak_cukup_untuk_ukuran_order() -> None:
    decision = evaluate(
        book([(99.995, 5.0)], [(100.005, 0.10), (100.01, 0.10)]),
        direction="long",
        tick_size=0.001,
        target_qty=5.0,
        atr=10.0,
        config=cfg(max_slippage_pct=5.0, wall_proximity_atr=9.0),
        now=0.0,
    )
    assert decision.accepted is False
    assert any("likuiditas tidak cukup" in r for r in decision.reasons)


def test_slippage_melebihi_batas_ditolak() -> None:
    # qty 2 unit menghabiskan level pertama tipis lalu level kedua 0.5% lebih tinggi
    decision = evaluate(
        book([(99.995, 50.0)], [(100.005, 0.2), (100.60, 50.0)]),
        direction="long",
        tick_size=0.001,
        target_qty=2.0,
        atr=10.0,
        config=cfg(max_slippage_pct=0.08, wall_proximity_atr=9.0),
        now=0.0,
    )
    assert decision.accepted is False
    assert any("slippage" in r and "batas" in r for r in decision.reasons)


def test_target_qty_tidak_valid_melempar() -> None:
    with pytest.raises(ValueError, match="target_qty"):
        evaluate(
            book([(99.995, 1.0)], [(100.005, 1.0)]),
            direction="long",
            tick_size=0.001,
            target_qty=0.0,
            atr=1.0,
            config=cfg(),
            now=0.0,
        )


# -------------------------------------------------------------- komponen skor
def test_trend_strength_dinormalisasi() -> None:
    assert trend_strength(0.0) == 0.0
    assert trend_strength(-25.0) == pytest.approx(0.5)
    assert trend_strength(80.0) == pytest.approx(1.0)  # dipotong di 1
    assert trend_strength(float("nan")) == 0.0
    with pytest.raises(ValueError, match="reference"):
        trend_strength(10.0, reference=0.0)


def test_volume_score_searah_dan_berlawanan() -> None:
    assert volume_score(100.0, "long") == pytest.approx(1.0)
    assert volume_score(-100.0, "long") == pytest.approx(0.0)
    assert volume_score(0.0, "long") == pytest.approx(0.5)
    assert volume_score(-100.0, "short") == pytest.approx(1.0)
    assert volume_score(250.0, "long") == pytest.approx(1.0)  # dipotong
    assert volume_score(float("nan"), "long") == pytest.approx(0.5)


def test_combine_bobot_dinormalisasi() -> None:
    scoring = ScoringConfig(weight_trend=0.5, weight_volume=0.25, weight_book=0.25)
    weights = scoring.normalized
    assert weights == pytest.approx((0.5, 0.25, 0.25))
    assert combine(trend=1.0, volume=1.0, book=1.0, weights=weights) == pytest.approx(1.0)
    assert combine(trend=0.0, volume=0.0, book=0.0, weights=weights) == pytest.approx(0.0)
    assert combine(trend=0.5, volume=0.5, book=0.5, weights=weights) == pytest.approx(0.5)


def test_scoring_bobot_nol_ditolak() -> None:
    with pytest.raises(ValueError, match="bobot"):
        ScoringConfig(weight_trend=0, weight_volume=0, weight_book=0)


# ------------------------------------------------------------------ peringkat
def _candidate(symbol: str, score: float, *, tradable: bool = True) -> Candidate:
    return Candidate(
        signal=signal(symbol=symbol),
        book=None if tradable else _tolak(),
        trend_component=0.5,
        volume_component=0.5,
        book_component=0.5,
        score=score,
    )


def _tolak() -> Any:
    return evaluate(
        book([(101.0, 1.0)], [(100.0, 1.0)]),
        direction="long",
        tick_size=0.1,
        target_qty=1.0,
        atr=1.0,
        config=cfg(),
        now=0.0,
    )


def test_rank_mengutamakan_yang_lolos_lalu_skor() -> None:
    a = _candidate("AAAUSDT", 0.9, tradable=False)
    b = _candidate("BBBUSDT", 0.4)
    c = _candidate("CCCUSDT", 0.8)
    ranked = rank_candidates([a, b, c])
    assert [x.symbol for x in ranked] == ["CCCUSDT", "BBBUSDT", "AAAUSDT"]


def test_rank_tie_diurutkan_abjad() -> None:
    ranked = rank_candidates([_candidate("ZZZUSDT", 0.5), _candidate("AAAUSDT", 0.5)])
    assert [x.symbol for x in ranked] == ["AAAUSDT", "ZZZUSDT"]


def test_candidate_alasan_penolakan() -> None:
    kalah_slot = _candidate("BBBUSDT", 0.4)
    assert "slot posisi" in (kalah_slot.rejection_reason() or "")
    ditolak = _candidate("AAAUSDT", 0.9, tradable=False)
    assert "buku rusak" in (ditolak.rejection_reason() or "")
    assert ditolak.as_row()["tradable"] is False


# ------------------------------------------------------------------- pipeline
def _exchange_info() -> Any:
    payload = json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
    return parse_exchange_info(payload)


class FakeDepth:
    """Sumber depth palsu: satu snapshot per simbol atau melempar DepthUnavailable."""

    def __init__(self, snapshots: dict[str, OrderBookSnapshot], gagal: set[str]) -> None:
        self.snapshots = snapshots
        self.gagal = gagal
        self.calls: list[str] = []

    async def snapshot(self, symbol: str) -> OrderBookSnapshot:
        self.calls.append(symbol)
        if symbol.upper() in self.gagal:
            raise DepthUnavailable("belum ada snapshot depth")
        return self.snapshots[symbol.upper()]


def _app_config() -> AppConfig:
    return load_config(Path(__file__).parents[1] / "config.yaml")


@pytest.mark.asyncio
async def test_gate_mengevaluasi_dan_memilih_teratas() -> None:
    info = _exchange_info()
    depth = FakeDepth(
        {
            "BTCUSDT": book([(99.995, 8.0)], [(100.005, 1.0)], symbol="BTCUSDT"),
            "ETHUSDT": book([(99.995, 1.0)], [(100.005, 8.0)], symbol="ETHUSDT"),
        },
        gagal=set(),
    )
    gate = SignalGate(_app_config(), depth, info.get, clock=lambda: 0.0)
    result = await gate.evaluate_many(
        [
            signal(symbol="BTCUSDT", cmo=60.0, vol_delta_win_pct=80.0),
            signal(symbol="ETHUSDT", cmo=10.0, vol_delta_win_pct=5.0),
        ],
        slots=1,
    )
    assert len(result.selected) == 1
    assert result.selected[0].symbol == "BTCUSDT"  # skor lebih tinggi
    assert result.tradable == result.candidates  # keduanya lolos filter buku
    assert len(depth.calls) == 2


@pytest.mark.asyncio
async def test_gate_slot_nol_tidak_memilih_apa_pun() -> None:
    info = _exchange_info()
    depth = FakeDepth({"BTCUSDT": book([(99.995, 5.0)], [(100.005, 5.0)])}, gagal=set())
    gate = SignalGate(_app_config(), depth, info.get, clock=lambda: 0.0)
    result = await gate.evaluate_many([signal()], slots=0)
    assert result.selected == []
    assert "slot" in (result.candidates[0].rejection_reason() or "")


@pytest.mark.asyncio
async def test_gate_menolak_simbol_tanpa_metadata() -> None:
    info = _exchange_info()
    depth = FakeDepth({}, gagal=set())
    gate = SignalGate(_app_config(), depth, info.get, clock=lambda: 0.0)
    candidate = await gate.evaluate(signal(symbol="TIDAKADAUSDT"))
    assert candidate.tradable is False
    assert "exchangeInfo" in (candidate.rejection_reason() or "")
    assert depth.calls == []  # tidak ada permintaan depth sia-sia


@pytest.mark.asyncio
async def test_gate_meneruskan_kegagalan_depth() -> None:
    info = _exchange_info()
    depth = FakeDepth({}, gagal={"BTCUSDT"})
    gate = SignalGate(_app_config(), depth, info.get, clock=lambda: 0.0)
    candidate = await gate.evaluate(signal())
    assert candidate.tradable is False
    assert "depth tidak tersedia" in (candidate.rejection_reason() or "")


@pytest.mark.asyncio
async def test_gate_memakai_target_qty_dari_config() -> None:
    info = _exchange_info()
    depth = FakeDepth({"BTCUSDT": book([(99.995, 500.0)], [(100.005, 500.0)])}, gagal=set())
    config = _app_config()
    gate = SignalGate(config, depth, info.get, clock=lambda: 0.0)
    candidate = await gate.evaluate(signal(close=100.0))
    assert candidate.tradable
    # target notional default 20 USDT pada harga 100 -> 0.2 unit; qty minimum
    # BTCUSDT di data uji biasanya lebih kecil, jadi cukup pastikan > 0.
    assert candidate.book is not None
    assert candidate.book.metrics is not None
    assert candidate.book.metrics.fill_complete


def test_summarize_membuat_satu_baris_per_kandidat() -> None:
    rows = summarize([_candidate("BBBUSDT", 0.4), _candidate("AAAUSDT", 0.9, tradable=False)])
    assert len(rows) == 2
    assert rows[0].startswith("BBBUSDT")
    assert "DITOLAK" not in rows[0]
    assert "buku rusak" in rows[1]
