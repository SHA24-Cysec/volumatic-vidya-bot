"""Eksekutor trade: dari :class:`TradeTicket` (TAHAP 4) menjadi posisi terlindungi.

Urutan yang **tidak boleh diubah** (semuanya demi keselamatan):

1. pastikan belum ada posisi di simbol itu;
2. kirim order entry (``limit_fallback``: limit dulu, lalu market bila lewat
   batas waktu);
3. **segera** pasang SL (``closePosition``/``reduceOnly``) dan TP;
4. bila pemasangan SL gagal → tutup posisi dengan market + catat insiden
   (``protection_failed``) dan tandai posisi ``FAILED``;
5. setelah TP parsial terisi → geser SL ke break-even sadar biaya;
6. trailing (buatan bot) diperbarui dari harga terakhir;
7. setiap langkah ditulis ke SQLite supaya restart tidak menghapus jejak.

Modul ini **tidak** memutuskan boleh/tidak trading — itu tugas
:class:`volumatic_bot.risk.RiskManager`. Eksekutor menolak tiket yang belum
disetujui (``approved=False``).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from volumatic_bot.binance.trading import OrderRequest, OrderResult, side_for_entry, side_for_exit
from volumatic_bot.config import AppConfig
from volumatic_bot.execution.broker import Broker, ExchangePosition
from volumatic_bot.execution.protect import (
    build_protection_orders,
    build_stop_loss_variants,
    stop_already_breached,
)
from volumatic_bot.positions.models import PositionState, TrackedPosition
from volumatic_bot.positions.store import PositionStore
from volumatic_bot.risk.manager import TradeTicket

__all__ = ["ExecutionReport", "TradeExecutor"]

Logger = Callable[[str], None]


@dataclass(slots=True)
class ExecutionReport:
    """Hasil satu upaya eksekusi."""

    symbol: str
    ok: bool
    position: TrackedPosition | None = None
    orders: list[OrderResult] = field(default_factory=list)
    incidents: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: True bila ada posisi terbuka yang **tidak terlindungi SL** dan tidak bisa
    #: ditutup otomatis. Mesin wajib berhenti: jangan lanjut trading.
    naked: bool = False

    def summary(self) -> str:
        if self.position is None:
            return f"eksekusi gagal: {'; '.join(self.incidents) or 'tanpa posisi'}"
        status = "OK" if self.ok else "GAGAL"
        return f"{status} {self.position.summary()}"


class TradeExecutor:
    """Jalankan satu tiket risiko menjadi posisi nyata/simulasi."""

    def __init__(
        self,
        config: AppConfig,
        broker: Broker,
        store: PositionStore,
        *,
        clock: Callable[[], int],
        logger: Logger | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        mode: str | None = None,
        mark_price: Callable[[str], Awaitable[float | None]] | None = None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.store = store
        self.clock = clock
        self.log = logger or (lambda pesan: None)
        self.sleep = sleep
        self.mode = mode or config.ops.mode
        #: Sumber harga terakhir (opsional). Dipakai untuk mendeteksi SL yang
        #: sudah terlampaui sebelum order dikirim (menghindari galat -2021).
        self._mark_price = mark_price

    # ------------------------------------------------------------------ entry
    async def open_trade(self, ticket: TradeTicket, *, atr: float) -> ExecutionReport:
        """Buka posisi + proteksi. Kembalikan laporan yang selalu bisa dicatat."""
        if not ticket.approved or ticket.size is None or ticket.plan is None:
            return ExecutionReport(
                symbol=ticket.symbol,
                ok=False,
                incidents=["tiket belum disetujui manajer risiko: " + "; ".join(ticket.reasons)],
            )

        ada = await self.broker.position(ticket.symbol)
        if ada is not None:
            pesan = f"posisi {ticket.symbol} sudah ada di bursa ({ada.qty}); entry dibatalkan"
            self.store.add_incident(
                kind="duplicate_position", detail=pesan, severity="warning", symbol=ticket.symbol
            )
            return ExecutionReport(symbol=ticket.symbol, ok=False, incidents=[pesan])

        size = ticket.size
        plan = ticket.plan
        direction = ticket.side
        qty = size.qty

        # ---- 1) entry ------------------------------------------------------
        entry_order = await self._send_entry(ticket, qty=qty)
        if entry_order is None:
            return ExecutionReport(symbol=ticket.symbol, ok=False, incidents=["entry tidak terisi"])
        self._log_order(entry_order)

        terisi = entry_order.executed_qty if entry_order.executed_qty > 0 else qty
        harga_entry = entry_order.avg_price if entry_order.avg_price > 0 else plan.entry_price

        pos = TrackedPosition(
            symbol=ticket.symbol,
            side=direction,
            state=PositionState.OPEN,
            qty=terisi,
            remaining_qty=terisi,
            entry_price=harga_entry,
            stop_price=plan.stop_price,
            tp1_price=plan.tp1_price,
            be_price=plan.be_price,
            risk_per_unit=plan.risk_per_unit,
            leverage=size.leverage,
            mode=self.mode,
            opened_at=self.clock(),
            updated_at=self.clock(),
            entry_order_id=entry_order.order_id,
        )
        self.store.save_position(pos)
        self.log(f"[EKSEKUSI] {ticket.symbol} entry terisi {terisi} @ {harga_entry}")

        # ---- 2) proteksi SEGERA -------------------------------------------
        laporan = await self._arm_protection(pos, partial_qty=plan.partial_qty)
        return laporan

    async def _send_entry(self, ticket: TradeTicket, *, qty: Decimal) -> OrderResult | None:
        """Kirim entry; ``limit_fallback`` mencoba limit lalu market."""
        assert ticket.plan is not None
        side = side_for_entry(ticket.side)
        trade = self.config.trade
        symbol = ticket.symbol

        if trade.entry_mode == "market":
            order = OrderRequest(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty,
                client_order_id=None,
                # RESULT: qty terisi langsung dikembalikan bursa (bukan ACK).
                new_order_resp_type="RESULT",
                purpose="entry",
            )
            return await self.broker.place(order)

        limit_price = ticket.plan.entry_price
        order = OrderRequest(
            symbol=symbol,
            side=side,
            type="LIMIT",
            quantity=qty,
            price=limit_price,
            time_in_force="IOC" if trade.entry_limit_timeout_sec <= 1 else "GTC",
            client_order_id=None,
            purpose="entry",
        )
        hasil = await self.broker.place(order)
        self._log_order(hasil)
        if hasil.filled:
            return hasil

        # Tunggu sebentar, lalu batalkan dan pakai market (fallback).
        batas = trade.entry_limit_timeout_sec
        langkah = min(1.0, max(0.05, batas / 3))
        lewat = 0.0
        while lewat < batas:
            await self.sleep(langkah)
            lewat += langkah
            try:
                terkini = await self.broker.status(symbol, order_id=hasil.order_id)
            except KeyError:
                break
            if terkini.filled:
                return terkini
        await self.broker.cancel(symbol, order_id=hasil.order_id)
        fallback = OrderRequest(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty,
            client_order_id=None,
            new_order_resp_type="RESULT",
            purpose="entry",
        )
        return await self.broker.place(fallback)

    # -------------------------------------------------------------- proteksi
    async def _arm_protection(
        self, pos: TrackedPosition, *, partial_qty: Decimal | None
    ) -> ExecutionReport:
        orders: list[OrderResult] = []
        incidents: list[str] = []
        try:
            proteksi = build_protection_orders(
                symbol=pos.symbol,
                direction=pos.side,
                qty=pos.remaining_qty,
                stop_price=pos.stop_price,
                tp1_price=pos.tp1_price,
                partial_qty=partial_qty if partial_qty and partial_qty > 0 else None,
            )
        except ValueError as exc:  # konfigurasi/qty tidak masuk akal
            return await self._fail_protection(pos, f"proteksi tidak bisa disusun: {exc}", orders)

        # SL dulu, baru TP: tanpa SL posisi tidak boleh dibiarkan terbuka.
        #
        # (a) Bila harga sudah melewati SL, mengirim STOP_MARKET akan ditolak
        #     Binance dengan -2021 ("would immediately trigger"). Dalam kondisi
        #     itu alasan masuknya sudah gugur: tutup posisi sekarang.
        mark = await self._harga_mark(pos.symbol)
        if mark is not None and stop_already_breached(
            direction=pos.side, stop_price=pos.stop_price, mark=mark
        ):
            alasan = (
                f"harga {mark} sudah melewati SL {pos.stop_price} sebelum SL terpasang; "
                "SL tidak dikirim (akan langsung terpicu/-2021), posisi ditutup market"
            )
            self.store.add_incident(
                kind="stop_already_breached", detail=alasan, severity="critical", symbol=pos.symbol
            )
            self.log(f"[DARURAT] {pos.symbol}: {alasan}")
            return await self._fail_protection(pos, alasan, orders)

        # (b) Tangga varian: closePosition dulu, lalu reduceOnly+quantity.
        #     Testnet menolak beberapa bentuk order untuk simbol tertentu; semua
        #     varian tetap reduce-only/closePosition sesuai aturan proyek.
        sl, galat_sl = await self._pasang_sl(pos, qty=pos.remaining_qty)
        if sl is None:
            return await self._fail_protection(pos, galat_sl, orders)
        orders.append(sl)
        self._log_order(sl)
        if not sl.active and not sl.filled:
            return await self._fail_protection(
                pos, f"SL ditolak bursa (status={sl.status})", orders
            )
        pos.sl_order_id = sl.order_id

        for extra in (proteksi.partial_tp, proteksi.take_profit):
            if extra is None:
                continue
            try:
                hasil = await self.broker.place(extra)
            except Exception as exc:
                incidents.append(f"TP gagal dipasang: {type(exc).__name__}: {exc}")
                self.store.add_incident(
                    kind="tp_failed",
                    detail=incidents[-1],
                    severity="warning",
                    symbol=pos.symbol,
                )
                continue
            orders.append(hasil)
            self._log_order(hasil)
            if hasil.purpose == "take_profit" and hasil.active:
                pos.tp_order_id = hasil.order_id
            if hasil.purpose == "partial_tp" and hasil.active:
                pos.tp_order_id = hasil.order_id
        pos.updated_at = self.clock()
        self.store.save_position(pos)
        self.log(f"[PROTEKSI] {pos.symbol} SL={pos.stop_price} TP={pos.tp1_price}")
        return ExecutionReport(
            symbol=pos.symbol, ok=True, position=pos, orders=orders, incidents=incidents
        )

    async def _harga_mark(self, symbol: str) -> float | None:
        """Harga terakhir untuk simbol (None bila tidak tersedia)."""
        if self._mark_price is None:
            return None
        try:
            return await self._mark_price(symbol)
        except Exception as exc:  # jangan gagalkan proteksi hanya karena harga
            self.log(f"[PERINGATAN] harga {symbol} tidak terbaca: {type(exc).__name__}")
            return None

    @staticmethod
    def _rincian_galat(exc: BaseException, order: OrderRequest) -> str:
        """Pesan galat lengkap: kode bursa + parameter order yang dikirim.

        Tanpa ini, insiden hanya berbunyi "BinanceAPIError" dan tidak ada cara
        mengetahui parameter mana yang ditolak.
        """
        kode = getattr(exc, "code", None)
        status = getattr(exc, "status", None)
        bagian = [f"{type(exc).__name__}"]
        if kode is not None:
            bagian.append(f"kode={kode}")
        if status is not None:
            bagian.append(f"HTTP={status}")
        return f"{' '.join(bagian)} | {exc} | order: {order.describe()}"

    async def _pasang_sl(
        self, pos: TrackedPosition, *, qty: Decimal
    ) -> tuple[OrderResult | None, str]:
        """Pasang SL memakai varian pertama yang diterima bursa.

        Mengembalikan ``(hasil, galat)``; ``hasil`` None berarti semua varian
        ditolak dan pemanggil wajib menutup posisi.
        """
        galat: list[str] = []
        for nomor, varian in enumerate(
            build_stop_loss_variants(
                symbol=pos.symbol,
                direction=pos.side,
                qty=qty,
                stop_price=pos.stop_price,
            ),
            start=1,
        ):
            try:
                hasil = await self.broker.place(varian)
            except Exception as exc:
                rincian = self._rincian_galat(exc, varian)
                galat.append(f"varian {nomor}: {rincian}")
                self.log(f"[SL GAGAL] {pos.symbol} {rincian}")
                continue
            if nomor > 1:
                # Berguna untuk audit: bentuk cadangan yang akhirnya dipakai.
                self.store.add_incident(
                    kind="sl_fallback",
                    detail=f"SL terpasang memakai varian {nomor} ({varian.describe()})",
                    severity="warning",
                    symbol=pos.symbol,
                )
            return hasil, ""
        return None, "SL gagal dipasang. " + " || ".join(galat)

    async def _fail_protection(
        self, pos: TrackedPosition, alasan: str, orders: list[OrderResult]
    ) -> ExecutionReport:
        """SL gagal → tutup posisi market SEKARANG, catat insiden, jangan diam."""
        self.store.add_incident(
            kind="protection_failed", detail=alasan, severity="critical", symbol=pos.symbol
        )
        self.log(f"[DARURAT] {pos.symbol}: {alasan} → menutup posisi market")
        tutup_ok = await self._close_market(pos, reason="protection_failed")
        pos.state = PositionState.FAILED if tutup_ok else PositionState.OPEN
        pos.close_reason = "protection_failed"
        pos.updated_at = self.clock()
        if tutup_ok:
            pos.state = PositionState.FAILED
            pos.closed_at = self.clock()
        self.store.save_position(pos)
        if not tutup_ok:
            self.store.add_incident(
                kind="close_failed",
                detail="posisi TIDAK berhasil ditutup otomatis; periksa manual sekarang",
                severity="critical",
                symbol=pos.symbol,
            )
        # Posisi yang tidak terlindungi DAN tidak bisa ditutup = bahaya nyata.
        # Mesin akan menghentikan sesi memakai penanda ini.
        telanjang = not tutup_ok
        if telanjang:
            self.log(
                f"[KRITIS] {pos.symbol}: posisi TANPA SL dan gagal ditutup otomatis. "
                "Tutup manual di bursa, lalu hentikan bot."
            )
        return ExecutionReport(
            symbol=pos.symbol,
            ok=False,
            position=pos,
            orders=orders,
            incidents=[alasan, *(["posisi ditutup market"] if tutup_ok else ["TUTUP GAGAL"])],
            naked=telanjang,
        )

    async def _close_market(self, pos: TrackedPosition, *, reason: str) -> bool:
        order = OrderRequest(
            symbol=pos.symbol,
            side=side_for_exit(pos.side),
            type="MARKET",
            quantity=pos.remaining_qty,
            reduce_only=True,
            # RESULT: bursa langsung mengembalikan qty terisi. Tanpa ini respons
            # bisa ACK (executedQty=0) walau order sudah terisi, dan penutupan
            # darurat dilaporkan GAGAL padahal berhasil (insiden 16 Nov 2026).
            new_order_resp_type="RESULT",
            purpose="close",
        )
        try:
            hasil = await self.broker.place(order)
        except Exception as exc:
            rincian = self._rincian_galat(exc, order)
            kode = getattr(exc, "code", None)
            if kode == -2022:
                # "ReduceOnly Order is rejected" = tidak ada posisi tersisa.
                # Cek ke bursa: kalau memang sudah rata, ini bukan kegagalan.
                sisa = await self.broker.position(pos.symbol)
                if sisa is None or sisa.qty == 0:
                    self.log(f"[TUTUP] {pos.symbol} sudah rata di bursa (tidak ada qty)")
                    pos.remaining_qty = Decimal(0)
                    pos.state = PositionState.CLOSED
                    pos.closed_at = self.clock()
                    pos.close_reason = reason
                    pos.updated_at = self.clock()
                    self.store.save_position(pos)
                    return True
            self.store.add_incident(
                kind="close_failed",
                detail=f"gagal menutup posisi: {rincian}",
                severity="critical",
                symbol=pos.symbol,
            )
            self.log(f"[TUTUP GAGAL] {pos.symbol} {rincian}")
            return False
        self._log_order(hasil)
        if hasil.executed_qty > 0 or hasil.filled:
            pos.remaining_qty = Decimal(0)
            pos.state = PositionState.CLOSED
            pos.closed_at = self.clock()
            pos.close_reason = reason
            pos.updated_at = self.clock()
            self.store.save_position(pos)
            self.log(f"[TUTUP] {pos.symbol} {reason} @ {hasil.avg_price}")
            return True

        # Respons tanpa qty terisi belum tentu berarti gagal (ACK). Tanya bursa:
        # kalau posisinya sudah nol, penutupan sebenarnya BERHASIL dan sesi tidak
        # perlu dinyatakan berbahaya.
        sisa = await self.broker.position(pos.symbol)
        if sisa is None or sisa.qty == 0:
            pos.remaining_qty = Decimal(0)
            pos.state = PositionState.CLOSED
            pos.closed_at = self.clock()
            pos.close_reason = reason
            pos.updated_at = self.clock()
            self.store.save_position(pos)
            self.log(
                f"[TUTUP] {pos.symbol} {reason} — dikonfirmasi lewat posisi bursa "
                f"(status order {hasil.status})"
            )
            return True
        self.log(
            f"[TUTUP GAGAL] {pos.symbol} order {hasil.status} tanpa qty terisi; "
            f"posisi di bursa masih {sisa.qty}"
        )
        return False

    async def close_position(self, symbol: str, *, reason: str) -> bool:
        """Tutup posisi aktif (dipakai kill switch / batas harian / trend flip)."""
        pos = self.store.get_position(symbol)
        if pos is None or not pos.active:
            return False
        await self._cancel_protection(pos)
        return await self._close_market(pos, reason=reason)

    async def _cancel_protection(self, pos: TrackedPosition) -> None:
        for order_id in (pos.sl_order_id, pos.tp_order_id):
            if order_id is None:
                continue
            with contextlib.suppress(Exception):
                await self.broker.cancel(pos.symbol, order_id=order_id)

    # ------------------------------------------------------------ event order
    async def on_order_update(self, update: Any) -> None:
        """Tangani ``ORDER_TRADE_UPDATE`` dari User Data Stream."""
        pos = self.store.get_position(update.symbol)
        if pos is None or not pos.active:
            return
        self.store.log_order(
            symbol=update.symbol,
            side=update.side,
            type=update.order_type,
            purpose=self._purpose_of(pos, update.order_id),
            status=update.status,
            mode=self.mode,
            order_id=update.order_id,
            client_order_id=update.client_order_id,
            qty=str(update.orig_qty),
            price=str(update.last_price),
            stop_price=str(update.stop_price),
            reduce_only=update.reduce_only,
        )

        if update.is_liquidation:
            pos.state = PositionState.CLOSED
            pos.closed_at = self.clock()
            pos.close_reason = "liquidated"
            pos.updated_at = self.clock()
            self.store.save_position(pos)
            self.store.add_incident(
                kind="liquidation",
                detail=f"posisi {update.symbol} terlikuidasi oleh bursa",
                severity="critical",
                symbol=update.symbol,
            )
            return

        # SL terkena → posisi selesai.
        if update.order_id == pos.sl_order_id and update.filled:
            pos.state = PositionState.CLOSED
            pos.remaining_qty = Decimal(0)
            pos.closed_at = self.clock()
            pos.close_reason = "stop_loss" if not pos.be_done else "stop_loss_be"
            pos.realized_usdt = float(update.realized_pnl)
            pos.updated_at = self.clock()
            self.store.save_position(pos)
            self.log(f"[SL] {pos.symbol} stop loss kena @ {update.avg_price}")
            return

        # TP parsial terisi → geser SL ke break-even.
        if update.is_trade and update.filled and (pos.tp_order_id == update.order_id):
            if not pos.partial_done and update.orig_qty < pos.qty:
                pos.partial_done = True
                pos.remaining_qty = max(Decimal(0), pos.qty - update.orig_qty)
                pos.state = PositionState.PARTIAL
                pos.updated_at = self.clock()
                self.store.save_position(pos)
                self.log(f"[TP1] {pos.symbol} sebagian ditutup {update.orig_qty}")
                await self.move_to_break_even(pos)
                return
            if update.orig_qty >= pos.remaining_qty:
                pos.state = PositionState.CLOSED
                pos.remaining_qty = Decimal(0)
                pos.closed_at = self.clock()
                pos.close_reason = "take_profit"
                pos.realized_usdt = float(update.realized_pnl)
                pos.updated_at = self.clock()
                self.store.save_position(pos)
                self.log(f"[TP] {pos.symbol} take profit kena @ {update.avg_price}")

    @staticmethod
    def _purpose_of(pos: TrackedPosition, order_id: int) -> str:
        if order_id == pos.sl_order_id:
            return "stop_loss"
        if order_id == pos.tp_order_id:
            return "take_profit"
        if order_id == pos.entry_order_id:
            return "entry"
        return "unknown"

    # ------------------------------------------------------------------- BE
    async def move_to_break_even(self, pos: TrackedPosition) -> bool:
        """Ganti SL dengan harga BE (sudah termasuk fee+slippage) — reduce-only."""
        if pos.be_price is None:
            return False
        await self._cancel_protection_sl(pos)
        order = OrderRequest(
            symbol=pos.symbol,
            side=side_for_exit(pos.side),
            type="STOP_MARKET",
            stop_price=pos.be_price,
            close_position=True,
            working_type="CONTRACT_PRICE",
            purpose="stop_loss",
        )
        try:
            hasil = await self.broker.place(order)
        except Exception as exc:
            self.store.add_incident(
                kind="be_failed",
                detail=f"gagal memasang SL break-even: {type(exc).__name__}: {exc}",
                severity="critical",
                symbol=pos.symbol,
            )
            return False
        self._log_order(hasil)
        pos.stop_price = Decimal(str(pos.be_price))
        pos.sl_order_id = hasil.order_id
        pos.be_done = True
        pos.updated_at = self.clock()
        self.store.save_position(pos)
        self.log(f"[BE] {pos.symbol} SL digeser ke {pos.stop_price}")
        return True

    async def _cancel_protection_sl(self, pos: TrackedPosition) -> None:
        if pos.sl_order_id is None:
            return
        with contextlib.suppress(Exception):
            await self.broker.cancel(pos.symbol, order_id=pos.sl_order_id)

    # -------------------------------------------------------------- trailing
    async def update_trailing(
        self, symbol: str, *, price: float, atr: float, activation_price: Decimal
    ) -> bool:
        """Trailing buatan bot: geser SL bila harga sudah melewati pemicu."""
        pos = self.store.get_position(symbol)
        if pos is None or not pos.active or atr <= 0:
            return False
        harga = Decimal(str(price))
        if pos.side == "long":
            if harga < activation_price:
                return False
            kandidat = harga - Decimal(str(atr * self.config.trade.trailing_atr_mult))
            if kandidat <= pos.stop_price:
                return False
        else:
            if harga > activation_price:
                return False
            kandidat = harga + Decimal(str(atr * self.config.trade.trailing_atr_mult))
            if kandidat >= pos.stop_price:
                return False

        await self._cancel_protection_sl(pos)
        order = OrderRequest(
            symbol=pos.symbol,
            side=side_for_exit(pos.side),
            type="STOP_MARKET",
            stop_price=kandidat,
            close_position=True,
            working_type="CONTRACT_PRICE",
            purpose="stop_loss",
        )
        try:
            hasil = await self.broker.place(order)
        except Exception as exc:
            self.store.add_incident(
                kind="trailing_failed",
                detail=f"gagal menggeser trailing: {type(exc).__name__}: {exc}",
                severity="critical",
                symbol=pos.symbol,
            )
            return False
        self._log_order(hasil)
        pos.stop_price = kandidat
        pos.sl_order_id = hasil.order_id
        pos.updated_at = self.clock()
        self.store.save_position(pos)
        self.log(f"[TRAIL] {pos.symbol} SL → {kandidat}")
        return True

    # ------------------------------------------------------------------ util
    def _log_order(self, order: OrderResult) -> None:
        self.store.log_order(
            symbol=order.symbol,
            side=order.side,
            type=order.type,
            purpose=order.purpose,
            status=order.status,
            mode=self.mode,
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            qty=str(order.orig_qty),
            price=str(order.avg_price),
            stop_price=str(order.stop_price),
            reduce_only=order.reduce_only,
        )

    async def sync_position(self, symbol: str) -> ExchangePosition | None:
        """Bandingkan catatan lokal dengan bursa (dipakai rekonsiliasi)."""
        return await self.broker.position(symbol)
