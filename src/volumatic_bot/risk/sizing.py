"""Ukuran posisi berbasis risiko (position sizing) untuk modal kecil.

Prinsip yang dipegang (sesuai keputusan pengguna):

1. **Risiko per trade dibatasi** (default 1,5% equity, batas keras 2%).
   Ukuran dihitung dari jarak Stop Loss, bukan dari "perasaan":
   ``qty = (equity x risk%) / |entry - stop|``.
2. **Pembulatan tidak boleh membesarkan risiko diam-diam.** Bila ``stepSize``
   atau ``minNotional`` memaksa ukuran naik melebihi
   ``risk.max_qty_round_up_pct`` **atau** melebihi
   ``risk.max_risk_overshoot_factor`` kali risiko target, trade **DITOLAK**.
3. **Biaya dihitung**, bukan diabaikan: fee taker dua sisi + asumsi slippage
   dinyatakan dalam USDT dan dalam satuan R, supaya jelas berapa bagian dari
   risiko yang habis hanya untuk biaya.
4. **Perkiraan likuidasi** ikut diperiksa: SL wajib berada **sebelum** harga
   likuidasi. Angka likuidasi di sini adalah pendekatan (isolated margin),
   bukan angka resmi Binance — angka resmi dibaca dari API di TAHAP 5.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from volumatic_bot.binance.symbols import SymbolFilters
from volumatic_bot.config import CommissionConfig, RiskConfig

__all__ = ["PositionSize", "estimate_liquidation", "size_position"]

Side = Literal["long", "short"]


def _dec(value: float | Decimal | int) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensif
        raise ValueError(f"nilai tidak bisa dijadikan Decimal: {value!r}") from exc


def estimate_liquidation(entry: float, leverage: int, mmr: float, *, side: Side) -> float:
    """Perkiraan harga likuidasi untuk **isolated margin** (pendekatan kasar).

    Rumus sederhana yang dipakai Binance untuk posisi kecil (satu tier MMR):

    * long : ``liq ~ entry x (1 - 1/leverage + MMR)``
    * short: ``liq ~ entry x (1 + 1/leverage - MMR)``

    Rumus ini **mengabaikan** biaya, funding, dan tier MMR berjenjang, sehingga
    harga likuidasi sebenarnya bisa sedikit lebih dekat daripada hasil ini.
    Karena itu margin pemakaiannya harus konservatif.
    """
    if leverage <= 0:
        raise ValueError("leverage harus > 0")
    if entry <= 0:
        raise ValueError("entry harus > 0")
    factor = 1.0 / leverage - mmr
    if side == "long":
        return max(0.0, entry * (1.0 - factor))
    return entry * (1.0 + factor)


@dataclass(frozen=True, slots=True)
class PositionSize:
    """Hasil perhitungan ukuran posisi beserta alasannya."""

    approved: bool
    side: Side
    entry_price: Decimal
    stop_price: Decimal
    qty: Decimal
    notional: Decimal
    margin: Decimal
    leverage: int
    stop_distance: Decimal
    stop_distance_pct: float
    target_risk_usdt: float
    risk_usdt: float
    risk_pct: float
    fee_usdt: float
    fee_r: float
    liq_price: float
    liq_distance_pct: float
    rounded_up: bool
    reasons: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def risk_per_unit(self) -> Decimal:
        return self.stop_distance

    def summary(self) -> str:
        """Satu baris ringkas untuk log/dashboard."""
        if not self.approved:
            return "DITOLAK: " + "; ".join(self.reasons)
        return (
            f"qty={self.qty} notional={self.notional:.2f} USDT "
            f"margin={self.margin:.2f} @{self.leverage}x "
            f"risiko={self.risk_usdt:.3f} USDT ({self.risk_pct:.2f}% equity) "
            f"SL={self.stop_distance_pct:.2f}% biaya={self.fee_usdt:.3f} USDT "
            f"({self.fee_r:.2f}R) liq~{self.liq_distance_pct:.2f}%"
        )


def _reject(
    *,
    side: Side,
    entry: Decimal,
    stop: Decimal,
    stop_distance: Decimal,
    target_risk: float,
    reasons: list[str],
    notes: list[str],
    leverage: int,
) -> PositionSize:
    return PositionSize(
        approved=False,
        side=side,
        entry_price=entry,
        stop_price=stop,
        qty=Decimal(0),
        notional=Decimal(0),
        margin=Decimal(0),
        leverage=leverage,
        stop_distance=stop_distance,
        stop_distance_pct=float(stop_distance / entry * 100) if entry > 0 else 0.0,
        target_risk_usdt=target_risk,
        risk_usdt=0.0,
        risk_pct=0.0,
        fee_usdt=0.0,
        fee_r=0.0,
        liq_price=0.0,
        liq_distance_pct=0.0,
        rounded_up=False,
        reasons=tuple(reasons),
        notes=tuple(notes),
    )


def size_position(
    *,
    equity: float,
    entry_price: float | Decimal,
    stop_price: float | Decimal,
    filters: SymbolFilters,
    side: Side,
    leverage: int,
    risk_pct: float,
    commission: CommissionConfig,
    config: RiskConfig,
    available_margin: float | None = None,
) -> PositionSize:
    """Hitung ukuran posisi yang aman untuk satu trade.

    ``available_margin`` (opsional) adalah margin yang **masih boleh** dipakai
    (sudah memperhitungkan posisi terbuka). Bila tidak diberikan, fungsi ini
    tidak memeriksa batas margin portofolio.
    """
    entry = _dec(entry_price)
    stop = _dec(stop_price)
    reasons: list[str] = []
    notes: list[str] = []

    if equity <= 0:
        reasons.append(f"equity tidak valid: {equity}")
    if leverage < 1:
        reasons.append(f"leverage tidak valid: {leverage}")
    if entry <= 0:
        reasons.append(f"harga entry tidak valid: {entry}")
    # Batas atas mengikuti config (`risk.risk_per_trade_pct` maks 25%, dan di atas
    # 2% hanya boleh dengan `allow_aggressive_risk`). Angka 2% yang dulu ditulis
    # mati di sini membuat setelan agresif tidak pernah menghasilkan ukuran.
    batas_atas = 25.0 if config.allow_aggressive_risk else 2.0
    if not 0 < risk_pct <= batas_atas:
        reasons.append(
            f"risiko per trade {risk_pct}% di luar batas 0–{batas_atas:g}% "
            "(naikkan risk.allow_aggressive_risk bila disengaja)"
        )

    target_risk = equity * risk_pct / 100.0
    stop_distance = abs(entry - stop)

    # SL harus berada di sisi yang benar, kalau tidak R tidak bermakna.
    if side == "long" and stop >= entry:
        reasons.append("SL long harus di bawah harga entry")
    if side == "short" and stop <= entry:
        reasons.append("SL short harus di atas harga entry")
    if stop_distance <= 0:
        reasons.append("jarak SL nol")

    if reasons:
        return _reject(
            side=side,
            entry=entry,
            stop=stop,
            stop_distance=stop_distance,
            target_risk=target_risk,
            reasons=reasons,
            notes=notes,
            leverage=leverage,
        )

    # ---- ukuran ideal & pembulatan ke bawah (selalu aman) -------------------
    qty_ideal = _dec(target_risk) / stop_distance
    qty_down = filters.round_qty(qty_ideal, mode="down")

    # ---- ukuran minimum yang masih sah diperdagangkan ----------------------
    qty_for_notional = Decimal(0)
    if filters.min_notional > 0 and entry > 0:
        # Bulatkan KE ATAS ke kelipatan stepSize: qty yang lebih kecil akan
        # ditolak bursa karena notional-nya di bawah minNotional.
        qty_for_notional = filters.round_qty(filters.min_notional / entry, mode="up")
    qty_min = max(filters.min_qty, qty_for_notional, filters.step_size or Decimal("1e-8"))
    qty_min = filters.round_qty(qty_min, mode="up")

    rounded_up = False
    qty = qty_down
    if qty_down < qty_min:
        # Terpaksa naik ke ukuran minimum. Ini SATU-SATUNYA alasan boleh membesar,
        # dan hanya bila masih di dalam dua pagar berikut.
        rounded_up = True
        qty = qty_min
        allowed_max = qty_ideal * _dec(1.0 + config.max_qty_round_up_pct / 100.0)
        if qty > allowed_max:
            naik_pct = float(qty / qty_ideal - 1) * 100.0 if qty_ideal > 0 else float("inf")
            reasons.append(
                f"pembulatan stepSize/minNotional memaksa ukuran naik {naik_pct:.1f}% "
                f"(batas {config.max_qty_round_up_pct:.1f}%) — trade dilewati, bukan dibesarkan"
            )
        notes.append(
            f"qty dibulatkan NAIK ke minimum sah {qty} (ideal {qty_ideal:.8f}) "
            f"demi memenuhi minQty/minNotional"
        )

    if filters.max_qty > 0 and qty > filters.max_qty:
        reasons.append(f"qty {qty} melebihi maxQty simbol {filters.max_qty}")

    notional = qty * entry
    if filters.min_notional > 0 and notional < filters.min_notional:
        reasons.append(f"notional {notional:.4f} USDT < minNotional {filters.min_notional} USDT")

    # ---- pagar risiko -----------------------------------------------------
    risk_usdt = float(qty) * float(stop_distance)
    if risk_usdt > target_risk * config.max_risk_overshoot_factor:
        reasons.append(
            f"risiko hasil pembulatan {risk_usdt:.4f} USDT > "
            f"{config.max_risk_overshoot_factor:.1f}x target {target_risk:.4f} USDT"
        )
    risk_realized_pct = risk_usdt / equity * 100.0 if equity > 0 else float("inf")

    # ---- margin & likuidasi ----------------------------------------------
    margin = notional / Decimal(max(leverage, 1))
    if available_margin is not None and float(margin) > available_margin + 1e-9:
        reasons.append(
            f"margin {float(margin):.2f} USDT > margin tersedia {available_margin:.2f} USDT"
        )

    liq_price = estimate_liquidation(
        float(entry), leverage, config.maintenance_margin_rate, side=side
    )
    liq_distance_pct = abs(float(entry) - liq_price) / float(entry) * 100.0
    stop_pct = float(stop_distance / entry * 100)
    if stop_pct >= liq_distance_pct:
        reasons.append(
            f"SL {stop_pct:.2f}% lebih jauh dari perkiraan likuidasi "
            f"{liq_distance_pct:.2f}% — SL bisa tidak pernah kena sebelum likuidasi"
        )

    # ---- biaya ------------------------------------------------------------
    round_trip_bps = 2.0 * (commission.taker_bps + commission.slippage_bps)
    fee_usdt = float(notional) * round_trip_bps / 10_000.0
    fee_r = fee_usdt / risk_usdt if risk_usdt > 0 else float("inf")

    if equity < config.small_capital_usdt:
        notes.append(
            f"modal kecil ({equity:.2f} USDT < {config.small_capital_usdt:.0f} USDT): "
            f"biaya bolak-balik = {fee_r:.2f}R dari risiko, pastikan target TP > 1R"
        )
    if fee_r >= 0.5:
        reasons.append(
            f"biaya bolak-balik {fee_r:.2f}R terlalu besar dibanding risiko "
            "(target TP akan habis oleh biaya)"
        )
    if math.isfinite(liq_distance_pct):
        notes.append(
            f"perkiraan likuidasi {liq_price:,.4f} ({liq_distance_pct:.2f}% dari entry, "
            f"pendekatan MMR {config.maintenance_margin_rate:.2%})"
        )

    return PositionSize(
        approved=not reasons,
        side=side,
        entry_price=entry,
        stop_price=stop,
        qty=qty,
        notional=notional,
        margin=margin,
        leverage=leverage,
        stop_distance=stop_distance,
        stop_distance_pct=stop_pct,
        target_risk_usdt=target_risk,
        risk_usdt=risk_usdt,
        risk_pct=risk_realized_pct,
        fee_usdt=fee_usdt,
        fee_r=fee_r,
        liq_price=liq_price,
        liq_distance_pct=liq_distance_pct,
        rounded_up=rounded_up,
        reasons=tuple(reasons),
        notes=tuple(notes),
    )
