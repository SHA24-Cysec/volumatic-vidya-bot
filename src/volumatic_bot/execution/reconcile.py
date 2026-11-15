"""Rekonsiliasi: menyamakan catatan bot dengan kenyataan di bursa.

Kasus yang ditangani (semuanya pernah menjadi penyebab kerugian nyata di bot
ritel):

1. **Posisi ada di bursa, tidak ada di catatan** (mis. bot mati saat entry) →
   dicatat sebagai insiden; bila ada ``stop_price`` di tiket yang tersimpan,
   SL dipasang ulang, kalau tidak → posisi ditutup.
2. **Posisi di catatan tapi sudah tidak ada di bursa** (SL/TP kena saat bot
   mati) → posisi ditutup di catatan dengan alasan ``hilang_di_bursa``.
3. **Posisi ada, SL tidak ada** (paling berbahaya) → SL dipasang ulang segera;
   gagal → tutup posisi.
4. **Order proteksi menggantung tanpa posisi** → dibatalkan.
5. **Arah/qty berbeda** antara catatan dan bursa → insiden + tutup posisi
   (bot tidak menebak; posisi yang tidak dikenal tidak boleh dibiarkan).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from volumatic_bot.binance.trading import OrderRequest, OrderResult, side_for_exit
from volumatic_bot.execution.broker import Broker
from volumatic_bot.positions.models import PositionState, TrackedPosition
from volumatic_bot.positions.store import PositionStore

__all__ = ["ReconcileAction", "ReconcileReport", "reconcile"]


@dataclass(frozen=True, slots=True)
class ReconcileAction:
    kind: str
    symbol: str
    detail: str
    severity: str = "info"


@dataclass(slots=True)
class ReconcileReport:
    actions: list[ReconcileAction] = field(default_factory=list)

    @property
    def critical(self) -> list[ReconcileAction]:
        return [a for a in self.actions if a.severity == "critical"]

    @property
    def ok(self) -> bool:
        return not self.critical

    def summary(self) -> str:
        if not self.actions:
            return "rekonsiliasi: tidak ada perbedaan antara bot dan bursa"
        baris = [f"- [{a.severity}] {a.kind} {a.symbol}: {a.detail}" for a in self.actions]
        return "rekonsiliasi:\n" + "\n".join(baris)


def _qty_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


async def reconcile(
    *,
    broker: Broker,
    store: PositionStore,
    symbols: list[str] | None = None,
    mode: str = "dry_run",
) -> ReconcileReport:
    """Periksa semua posisi aktif + simbol yang diminta."""
    report = ReconcileReport()
    tersimpan = {p.symbol: p for p in store.active_positions()}
    daftar = sorted(set(tersimpan) | {s.upper() for s in (symbols or [])})

    for symbol in daftar:
        pos = tersimpan.get(symbol)
        bursa = await broker.position(symbol)
        order_aktif = await broker.open_orders(symbol)

        # 1) posisi tidak dikenal di bursa
        if pos is None and bursa is not None:
            detail = (
                f"bursa punya posisi {bursa.qty} @ {bursa.entry_price} yang tidak ada "
                "di catatan bot"
            )
            store.add_incident(
                kind="orphan_position", detail=detail, severity="critical", symbol=symbol
            )
            report.actions.append(ReconcileAction("orphan_position", symbol, detail, "critical"))
            report.actions.append(
                await _close_unknown(broker, store, symbol, bursa.qty, bursa.side or "long", mode)
            )
            continue

        # 2) posisi di catatan tapi hilang di bursa
        if pos is not None and bursa is None:
            pos.state = PositionState.CLOSED
            pos.remaining_qty = Decimal(0)
            pos.close_reason = "hilang_di_bursa"
            store.save_position(pos)
            detail = "posisi tidak ada lagi di bursa (SL/TP kemungkinan sudah tereksekusi)"
            store.add_incident(
                kind="position_missing", detail=detail, severity="warning", symbol=symbol
            )
            report.actions.append(ReconcileAction("position_missing", symbol, detail, "warning"))
            # order proteksi yang menggantung tanpa posisi harus dibatalkan
            for order in order_aktif:
                await broker.cancel(symbol, order_id=order.order_id)
                report.actions.append(
                    ReconcileAction(
                        "cancel_orphan_order",
                        symbol,
                        f"order {order.client_order_id} dibatalkan karena posisi tidak ada",
                    )
                )
            continue

        if pos is None:
            # tidak ada posisi di mana pun; pastikan tidak ada order menggantung
            for order in order_aktif:
                await broker.cancel(symbol, order_id=order.order_id)
                report.actions.append(
                    ReconcileAction(
                        "cancel_orphan_order",
                        symbol,
                        f"order {order.client_order_id} dibatalkan (tidak ada posisi)",
                    )
                )
            continue

        # 3) posisi ada di kedua sisi: periksa arah & qty
        if bursa is not None and (bursa.side != pos.side):
            detail = f"arah tidak cocok: catatan {pos.side}, bursa {bursa.side}"
            store.add_incident(
                kind="side_mismatch", detail=detail, severity="critical", symbol=symbol
            )
            report.actions.append(ReconcileAction("side_mismatch", symbol, detail, "critical"))
            await _close_unknown(broker, store, symbol, bursa.qty, bursa.side or "long", mode)
            continue

        # 4) SL wajib ada
        # Purpose order dari bursa TIDAK bisa diandalkan: ``LiveBroker.open_orders``
        # menghasilkan OrderResult dengan purpose default "entry" karena bursa
        # tidak menyimpan label internal bot. Kesimpulannya diambil dari tipe
        # order (STOP_MARKET = SL, TAKE_PROFIT_MARKET = TP), baik order biasa
        # maupun algo order. Tanpa ini, SL yang aktif dianggap hilang lalu
        # dipasang ulang → dua SL untuk satu posisi.
        def _purpose(o: OrderResult) -> str:
            if o.purpose in ("stop_loss", "take_profit", "partial_tp", "close"):
                return o.purpose
            if o.type == "STOP_MARKET":
                return "stop_loss"
            if o.type == "TAKE_PROFIT_MARKET":
                return "take_profit"
            return o.purpose

        punya_sl = any(_purpose(o) == "stop_loss" and o.active for o in order_aktif)
        if not punya_sl:
            detail = "posisi terbuka TANPA order stop loss"
            store.add_incident(
                kind="missing_stop_loss", detail=detail, severity="critical", symbol=symbol
            )
            report.actions.append(ReconcileAction("missing_stop_loss", symbol, detail, "critical"))
            ok = await _rearm_stop(broker, store, pos, mode=mode)
            report.actions.append(
                ReconcileAction(
                    "rearm_stop_loss" if ok else "close_without_stop",
                    symbol,
                    "SL dipasang ulang" if ok else "SL gagal dipasang ulang; posisi ditutup",
                    "warning" if ok else "critical",
                )
            )
        else:
            report.actions.append(
                ReconcileAction("stop_loss_ok", symbol, "posisi punya SL aktif", "info")
            )

    return report


async def _rearm_stop(
    broker: Broker, store: PositionStore, pos: TrackedPosition, *, mode: str
) -> bool:
    order = OrderRequest(
        symbol=pos.symbol,
        side=side_for_exit(pos.side),
        type="STOP_MARKET",
        stop_price=pos.stop_price,
        close_position=True,
        working_type="CONTRACT_PRICE",
        purpose="stop_loss",
    )
    try:
        hasil = await broker.place(order)
    except Exception as exc:
        store.add_incident(
            kind="rearm_failed",
            detail=f"gagal memasang ulang SL: {type(exc).__name__}: {exc}",
            severity="critical",
            symbol=pos.symbol,
        )
        await _close_unknown(broker, store, pos.symbol, pos.remaining_qty, pos.side, mode)
        return False
    if not (hasil.active or hasil.filled):
        await _close_unknown(broker, store, pos.symbol, pos.remaining_qty, pos.side, mode)
        return False
    pos.sl_order_id = hasil.order_id
    store.save_position(pos)
    store.log_order(
        symbol=pos.symbol,
        side=order.side,
        type=order.type,
        purpose="stop_loss",
        status=hasil.status,
        mode=mode,
        order_id=hasil.order_id,
        qty=str(pos.remaining_qty),
        stop_price=str(pos.stop_price),
    )
    return True


async def _close_unknown(
    broker: Broker,
    store: PositionStore,
    symbol: str,
    qty: Decimal,
    side: str,
    mode: str,
) -> ReconcileAction:
    """Tutup posisi yang tidak bisa dipercaya (qty bisa negatif untuk short)."""
    arah = "long" if Decimal(str(qty)) > 0 else "short"
    order = OrderRequest(
        symbol=symbol,
        side=side_for_exit(arah),
        type="MARKET",
        quantity=abs(Decimal(str(qty))),
        reduce_only=True,
        purpose="close",
    )
    try:
        hasil = await broker.place(order)
        ok = hasil.executed_qty > 0
        detail = (
            f"posisi {'ditolak' if ok else 'TIDAK'} ditutup market ({hasil.status})"
            if ok
            else f"gagal menutup posisi ({hasil.status})"
        )
        store.log_order(
            symbol=symbol,
            side=order.side,
            type=order.type,
            purpose="close",
            status=hasil.status,
            mode=mode,
            order_id=hasil.order_id,
            qty=str(order.quantity),
        )
        return ReconcileAction("close_unknown", symbol, detail, "warning" if ok else "critical")
    except Exception as exc:
        detail = f"gagal menutup posisi tidak dikenal: {type(exc).__name__}: {exc}"
        store.add_incident(kind="close_failed", detail=detail, severity="critical", symbol=symbol)
        return ReconcileAction("close_unknown", symbol, detail, "critical")
