"""Simulasi posisi tunggal di dalam backtest.

Aturan yang **sama** dengan eksekusi langsung (TAHAP 5), disederhanakan untuk
data OHLC:

1. entry di harga ``open`` candle sinyal (bukan ``close`` — sinyal baru diketahui
   setelah candle tertutup, jadi tidak ada *look-ahead*);
2. SL dan TP diperiksa **di dalam setiap candle** memakai ``high``/``low``;
3. bila SL dan TP tersentuh pada candle yang sama, dianggap **SL lebih dulu**
   (asumsi pesimistis — urutan intra-candle tidak diketahui dari OHLC);
4. TP parsial (TP1) menutup sebagian posisi lalu memindahkan SL ke BE;
5. trailing menggeser SL hanya searah, memakai ``trail`` (garis VIDYA) ±
   ``trailing_atr_mult`` x ATR, sama seperti ``TradeExecutor.update_trailing``;
6. SL long **tidak** pernah lebih tinggi dari harga eksekusi candle saat ini.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from volumatic_bot.backtest.costs import CostModel, FundingSeries, TradeCosts

__all__ = ["BacktestTrade", "OpenPosition", "TradeManager"]


@dataclass(slots=True)
class OpenPosition:
    """Posisi yang sedang berjalan di dalam backtest."""

    symbol: str
    side: str
    entry_price: float
    qty: float
    remaining_qty: float
    stop_price: float
    tp1_price: float
    be_price: float
    be_trigger_price: float
    trailing_activation_price: float
    trail_line: float
    atr: float
    opened_ms: int
    risk_per_unit: float
    trailing_atr_mult: float = 1.0
    partial_fraction: float = 0.5
    partial_done: bool = False
    be_done: bool = False
    entry_cost: float = 0.0
    realized_gross: float = 0.0
    realized_fees: float = 0.0
    realized_slip: float = 0.0

    @property
    def risk_usdt(self) -> float:
        return self.risk_per_unit * self.qty

    def unrealized(self, price: float) -> float:
        arah = 1.0 if self.side == "long" else -1.0
        return (price - self.entry_price) * self.remaining_qty * arah


@dataclass(slots=True)
class BacktestTrade:
    """Hasil satu trade tertutup (dipakai untuk statistik dan audit)."""

    symbol: str
    side: str
    qty: float
    entry_price: float
    exit_price: float
    opened_ms: int
    closed_ms: int
    reason: str
    gross_usdt: float
    costs: TradeCosts
    equity_after: float
    risk_usdt: float
    r_multiple: float
    partial_done: bool = False
    be_done: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def net_usdt(self) -> float:
        return self.gross_usdt - self.costs.total_usdt

    @property
    def notional(self) -> float:
        return self.qty * self.entry_price

    def as_row(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "masuk": self.entry_price,
            "keluar": self.exit_price,
            "qty": self.qty,
            "alasan": self.reason,
            "bruto": round(self.gross_usdt, 4),
            "fee": round(self.costs.fee_usdt, 4),
            "slippage": round(self.costs.slippage_usdt, 4),
            "funding": round(self.costs.funding_usdt, 4),
            "bersih": round(self.net_usdt, 4),
            "R": round(self.r_multiple, 3),
        }


class TradeManager:
    """Kelola satu posisi: buka, periksa candle, dan tutup."""

    def __init__(
        self,
        costs: CostModel,
        *,
        funding: FundingSeries | None = None,
        trailing_atr_mult: float = 2.0,
    ) -> None:
        self.costs = costs
        self.funding = funding
        self.trailing_atr_mult = trailing_atr_mult
        self.position: OpenPosition | None = None

    # ---------------------------------------------------------------- buka
    def open(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        stop_price: float,
        tp1_price: float,
        be_price: float,
        be_trigger_price: float,
        trailing_activation_price: float,
        trail_line: float,
        atr: float,
        open_ms: int,
        partial_fraction: float = 0.5,
    ) -> OpenPosition:
        """Buka posisi pada harga ``price`` (sudah termasuk slippage)."""
        if self.position is not None:
            raise RuntimeError("masih ada posisi terbuka; tutup dulu sebelum membuka lagi")
        if qty <= 0:
            raise ValueError("qty harus > 0")
        if side not in ("long", "short"):
            raise ValueError(f"side tidak dikenal: {side!r}")
        risk = abs(price - stop_price)
        if risk <= 0:
            raise ValueError("SL tidak boleh sama dengan harga entry")

        notional = qty * price
        entry_cost = self.costs.entry_cost(notional)
        # Biaya masuk dibebankan pada bagian yang benar-benar dipakai.
        pos = OpenPosition(
            symbol=symbol,
            side=side,
            entry_price=price,
            qty=qty,
            remaining_qty=qty,
            stop_price=stop_price,
            tp1_price=tp1_price,
            be_price=be_price,
            be_trigger_price=be_trigger_price,
            trailing_activation_price=trailing_activation_price,
            trail_line=trail_line,
            atr=atr,
            opened_ms=open_ms,
            risk_per_unit=risk,
            trailing_atr_mult=self.trailing_atr_mult,
            partial_fraction=max(0.0, min(1.0, partial_fraction)),
            entry_cost=entry_cost,
        )
        self.position = pos
        return pos

    # -------------------------------------------------------------- periksa
    def on_candle(
        self,
        *,
        high: float,
        low: float,
        close: float,
        trail: float,
        atr: float,
        ts_ms: int,
    ) -> str | None:
        """Proses satu candle untuk posisi terbuka.

        Kembalikan alasan penutupan (``stop_loss``/``stop_loss_be``/``take_profit``)
        bila posisi tertutup, atau ``None``. Perubahan SL ke BE/trailing
        diterapkan langsung pada posisi.
        """
        pos = self.position
        if pos is None:
            return None
        long = pos.side == "long"

        # --- 1) cek SL lebih dulu (asumsi pesimistis) --------------------
        if long and low <= pos.stop_price:
            return "stop_loss_be" if pos.be_done else "stop_loss"
        if not long and high >= pos.stop_price:
            return "stop_loss_be" if pos.be_done else "stop_loss"

        # --- 2) TP1 (parsial) --------------------------------------------
        if not pos.partial_done and 0 < pos.partial_fraction < 1:
            harga_tp = pos.tp1_price
            kena = high >= harga_tp if long else low <= harga_tp
            if kena:
                self._take_partial(harga_tp)

        # --- 3) sisa posisi kena TP penuh? (hanya bila tidak ada parsial) --
        if pos.partial_done and pos.remaining_qty <= 1e-12:
            return "take_profit"

        # --- 4) BE & trailing (berbasis candle tertutup) ------------------
        if long:
            if not pos.be_done and high >= pos.be_trigger_price:
                pos.stop_price = max(pos.stop_price, pos.be_price)
                pos.be_done = True
            if high >= pos.trailing_activation_price:
                baru = trail - atr * pos.trailing_atr_mult
                if baru > pos.stop_price:
                    pos.stop_price = min(baru, close)  # jangan di atas harga saat ini
        else:
            if not pos.be_done and low <= pos.be_trigger_price:
                pos.stop_price = min(pos.stop_price, pos.be_price)
                pos.be_done = True
            if low <= pos.trailing_activation_price:
                baru = trail + atr * pos.trailing_atr_mult
                if baru < pos.stop_price:
                    pos.stop_price = max(baru, close)
        return None

    def _take_partial(self, price: float) -> None:
        pos = self.position
        assert pos is not None
        qty = pos.qty * pos.partial_fraction
        self._realize(pos, qty, price)
        pos.partial_done = True
        # Setelah parsial, SL pindah ke BE (bila belum).
        if not pos.be_done:
            pos.stop_price = pos.be_price
            pos.be_done = True

    def _realize(self, pos: OpenPosition, qty: float, price: float) -> None:
        arah = 1.0 if pos.side == "long" else -1.0
        pos.realized_gross += (price - pos.entry_price) * qty * arah
        notional = qty * price
        pos.realized_fees += self.costs.fee(notional)
        pos.realized_slip += self.costs.slip(notional)
        pos.remaining_qty = max(0.0, pos.remaining_qty - qty)

    # ---------------------------------------------------------------- tutup
    def close(
        self, *, price: float, ts_ms: int, reason: str, equity_before: float
    ) -> BacktestTrade:
        """Tutup seluruh sisa posisi pada ``price`` dan kembalikan rekapnya."""
        pos = self.position
        if pos is None:
            raise RuntimeError("tidak ada posisi terbuka")
        if pos.remaining_qty > 1e-12:
            self._realize(pos, pos.remaining_qty, price)

        # Biaya masuk dialokasikan proporsional terhadap qty yang terealisasi.
        jam = max(0.0, (ts_ms - pos.opened_ms) / 3_600_000.0)
        notional_entry = pos.qty * pos.entry_price
        if self.funding is not None:
            funding = self.funding.cost_usdt(
                notional=notional_entry, side=pos.side, start_ms=pos.opened_ms, end_ms=ts_ms
            )
            funding_usdt = 0.0 if funding is None else funding
        else:
            funding_usdt = self.costs.funding_payment(notional_entry, pos.side, jam)

        costs = TradeCosts(
            fee_usdt=pos.realized_fees + self.costs.fee(notional_entry),
            slippage_usdt=pos.realized_slip + self.costs.slip(notional_entry),
            funding_usdt=funding_usdt,
        )
        net = pos.realized_gross - costs.total_usdt
        risiko = pos.risk_usdt if pos.risk_usdt > 0 else 1e-12
        trade = BacktestTrade(
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
            exit_price=price,
            opened_ms=pos.opened_ms,
            closed_ms=ts_ms,
            reason=reason,
            gross_usdt=pos.realized_gross,
            costs=costs,
            equity_after=equity_before + net,
            risk_usdt=pos.risk_usdt,
            r_multiple=net / risiko,
            partial_done=pos.partial_done,
            be_done=pos.be_done,
        )
        self.position = None
        return trade
