"""Rencana trade: level SL, TP, break-even sadar-biaya, dan pemicu trailing.

Modul ini **hanya menghitung harga level**, belum memasang order (itu TAHAP 5).
Semua harga dibulatkan ke ``tickSize`` simbol dengan arah yang **aman**:

* SL long dibulatkan **ke bawah**, SL short **ke atas** → jarak SL tidak pernah
  menyusut karena pembulatan.
* Break-even long dibulatkan **ke atas**, short **ke bawah** → BE selalu cukup
  menutup biaya, tidak pernah kurang.

Break-even di sini benar-benar memperhitungkan biaya bolak-balik
(``2 x (taker_bps + slippage_bps)``) ditambah ``trade.be_buffer_pct``, bukan
sekadar harga entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from volumatic_bot.binance.symbols import SymbolFilters
from volumatic_bot.config import CommissionConfig, TradeConfig

__all__ = ["TradePlan", "build_trade_plan", "round_trip_cost_pct"]

Side = Literal["long", "short"]


def round_trip_cost_pct(commission: CommissionConfig) -> float:
    """Biaya bolak-balik dalam persen harga (taker dua sisi + slippage dua sisi)."""
    return 2.0 * (commission.taker_bps + commission.slippage_bps) / 100.0


@dataclass(frozen=True, slots=True)
class TradePlan:
    """Rencana satu trade setelah level-levelnya dihitung."""

    side: Side
    entry_price: Decimal
    stop_price: Decimal
    risk_per_unit: Decimal
    sl_source: str
    tp1_price: Decimal
    be_price: Decimal
    be_trigger_price: Decimal
    trailing_activation_price: Decimal
    trailing_callback_pct: float
    trailing_atr_mult: float
    partial_qty: Decimal
    remaining_qty: Decimal
    notes: tuple[str, ...]

    @property
    def stop_distance_pct(self) -> float:
        return float(self.risk_per_unit / self.entry_price * 100)

    def target_price(self, r_multiple: float) -> Decimal:
        """Harga setara ``r_multiple`` R dari entry (positif = searah trade)."""
        delta = self.risk_per_unit * Decimal(str(r_multiple))
        return self.entry_price + delta if self.side == "long" else self.entry_price - delta

    def summary(self) -> str:
        arah = "BELI" if self.side == "long" else "JUAL"
        return (
            f"{arah} entry={self.entry_price} SL={self.stop_price} "
            f"({self.sl_source}, {self.stop_distance_pct:.2f}%) "
            f"TP1={self.tp1_price} BE={self.be_price} "
            f"trail_aktif={self.trailing_activation_price}"
        )


def build_trade_plan(
    *,
    side: Side,
    entry_price: float | Decimal,
    atr: float,
    trail: float | None,
    qty: float | Decimal,
    filters: SymbolFilters,
    trade: TradeConfig,
    commission: CommissionConfig,
) -> TradePlan:
    """Susun level SL/TP/BE/trailing untuk satu posisi.

    ``trail`` adalah garis VIDYA dari sinyal (dipakai bila ``trade.sl_mode`` =
    ``vidya_trail`` atau ``nearest``). ``atr`` dalam satuan harga (bukan persen).
    """
    entry = Decimal(str(entry_price))
    qty_dec = Decimal(str(qty))
    notes: list[str] = []
    sign = 1 if side == "long" else -1

    def to_price(value: float) -> Decimal:
        """Bulatkan ke tick, arah menjauhi entry (aman untuk SL)."""
        mode = "down" if side == "long" else "up"
        return filters.round_price(value, mode=mode)

    def to_price_toward(value: float) -> Decimal:
        """Bulatkan ke tick, arah mendekati entry (aman untuk BE/TP long)."""
        mode = "up" if side == "long" else "down"
        return filters.round_price(value, mode=mode)

    if atr <= 0:
        raise ValueError("atr harus > 0 untuk menghitung level SL/TP")
    if qty_dec <= 0:
        raise ValueError("qty harus > 0")

    # ---- pilih stop -------------------------------------------------------
    atr_distance = trade.sl_atr_mult * atr
    atr_stop = float(entry) - sign * atr_distance
    min_distance = trade.sl_min_atr_mult * atr
    candidates: list[tuple[str, float, float]] = [("atr", atr_stop, atr_distance)]

    trail_valid = (
        trail is not None
        and trail > 0
        and (
            (side == "long" and trail < float(entry)) or (side == "short" and trail > float(entry))
        )
    )
    if trail is not None and trail > 0 and not trail_valid:
        notes.append("garis VIDYA berada di sisi yang salah; diabaikan sebagai SL")
    if trail_valid and trail is not None:
        candidates.append(("vidya_trail", float(trail), abs(float(entry) - float(trail))))

    if trade.sl_mode == "vidya_trail":
        chosen = next((c for c in candidates if c[0] == "vidya_trail"), candidates[0])
    elif trade.sl_mode == "nearest":
        chosen = min(candidates, key=lambda c: c[2])
    else:  # "atr"
        chosen = candidates[0]

    source, stop_raw, distance = chosen
    if distance < min_distance:
        notes.append(
            f"SL {source} hanya {distance:.6g} (< {trade.sl_min_atr_mult}x ATR = "
            f"{min_distance:.6g}); dilebarkan agar tidak kena noise"
        )
        stop_raw = float(entry) - sign * min_distance
        source = f"{source}+min_atr"
    stop_price = filters.stop_outside_entry(
        entry, to_price(stop_raw), side=side, mode="down" if side == "long" else "up"
    )
    risk_per_unit = abs(entry - stop_price)
    if risk_per_unit <= 0:  # pragma: no cover - dijaga stop_outside_entry
        tick = filters.tick_size if filters.tick_size > 0 else Decimal("0.00000001")
        risk_per_unit = tick
        stop_price = entry - sign * tick

    # ---- TP1 --------------------------------------------------------------
    tp1_raw = float(entry) + sign * trade.tp1_r * float(risk_per_unit)
    tp1_price = filters.round_price(tp1_raw)

    # ---- break-even sadar biaya ------------------------------------------
    cost_pct = round_trip_cost_pct(commission)
    be_pct = cost_pct + trade.be_buffer_pct
    be_raw = float(entry) * (1.0 + sign * be_pct / 100.0)
    be_price = to_price_toward(be_raw)
    be_trigger_raw = float(entry) + sign * trade.be_trigger_r * float(risk_per_unit)
    be_trigger_price = filters.round_price(be_trigger_raw)

    # ---- trailing ---------------------------------------------------------
    trail_activation_raw = float(entry) + sign * trade.trailing_activation_r * float(risk_per_unit)
    trail_activation = filters.round_price(trail_activation_raw)

    # ---- partial TP -------------------------------------------------------
    if trade.partial_tp_enabled:
        partial_raw = qty_dec * Decimal(str(trade.partial_tp_pct)) / 100
        partial_qty = filters.round_qty(partial_raw, mode="down")
        if partial_qty <= 0 or partial_qty >= qty_dec:
            notes.append(
                "partial TP dilewati: pembulatan membuat qty parsial tidak sah "
                f"({partial_qty} dari {qty_dec})"
            )
            partial_qty = Decimal(0)
    else:
        partial_qty = Decimal(0)
    remaining_qty = qty_dec - partial_qty

    notes.append(
        f"biaya bolak-balik {cost_pct:.4f}% harga; BE digeser {be_pct:.4f}% "
        f"supaya benar-benar impas setelah fee+slippage"
    )
    return TradePlan(
        side=side,
        entry_price=entry,
        stop_price=stop_price,
        risk_per_unit=risk_per_unit,
        sl_source=source,
        tp1_price=tp1_price,
        be_price=be_price,
        be_trigger_price=be_trigger_price,
        trailing_activation_price=trail_activation,
        trailing_callback_pct=trade.trailing_callback_rate_pct,
        trailing_atr_mult=trade.trailing_atr_mult,
        partial_qty=partial_qty,
        remaining_qty=remaining_qty,
        notes=tuple(notes),
    )
