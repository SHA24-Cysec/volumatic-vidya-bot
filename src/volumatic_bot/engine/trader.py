"""Mesin trading: satu loop yang menyatukan semua modul bot.

Alur lengkap (satu arah, tanpa jalur pintas):

```
kline tertutup ──► KlineFeed ──► SignalEvent
                                    │
                          SignalGate (order book)         ← opsional
                                    │
                            RiskManager (tiket)           ← pagar keras
                                    │
                          TradeExecutor (order nyata)     ← broker
                                    │
                    PositionStore (SQLite) + Notifier + Dashboard
```

Yang **tidak** dilakukan mesin ini: membuka koneksi jaringan. Kline feed, depth
source, broker, dan user stream disuntikkan dari luar (``cli run`` untuk produksi,
``DryRunBroker`` + feed palsu untuk uji). Dengan begitu seluruh logika keputusan
bisa diuji tanpa Binance — penting karena sandbox pengembangan tidak bisa
mencapai API Binance.

Pagar yang dipegang mesin:

* satu posisi per simbol — sinyal baru untuk simbol yang sudah punya posisi
  langsung dibuang sebelum menyentuh broker;
* kill switch dan batas rugi harian diperiksa **sebelum** order apa pun;
* saat batas rugi harian tercapai dan ``risk.close_all_on_daily_limit`` aktif:
  semua posisi ditutup, sesi berhenti sampai hari trading berikutnya;
* sinyal ganda (transmisi ulang WebSocket setelah reconnect) diabaikan.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from volumatic_bot.dashboard.models import DashboardState
from volumatic_bot.notify.dispatcher import NotifierDispatcher
from volumatic_bot.notify.events import (
    event_for_candidate,
    event_for_entry,
    event_for_exit,
    event_for_incident,
    event_for_rejected,
)
from volumatic_bot.notify.models import NotifyEvent, NotifyKind, NotifyLevel
from volumatic_bot.positions.store import PositionStore
from volumatic_bot.risk.portfolio import Portfolio, PositionExposure

if TYPE_CHECKING:
    from volumatic_bot.config import AppConfig
    from volumatic_bot.dashboard.sources import LocalStateSource
    from volumatic_bot.execution.broker import Broker
    from volumatic_bot.execution.executor import ExecutionReport, TradeExecutor
    from volumatic_bot.execution.reconcile import ReconcileReport
    from volumatic_bot.risk.manager import RiskManager, TradeTicket
    from volumatic_bot.signals.pipeline import SignalGate
    from volumatic_bot.streams.kline_feed import SignalEvent

__all__ = ["EngineResult", "TradingEngine"]

logger = logging.getLogger("volumatic_bot.engine")


@dataclass(slots=True)
class EngineResult:
    """Hasil satu peristiwa yang diproses mesin (untuk log, dashboard, dan uji)."""

    action: str  # "entered" | "skipped" | "rejected" | "blocked" | "duplicate"
    symbol: str
    reason: str = ""
    ticket: TradeTicket | None = None
    report: ExecutionReport | None = None

    def summary(self) -> str:
        return f"{self.action} {self.symbol}" + (f": {self.reason}" if self.reason else "")


@dataclass(slots=True)
class _Marks:
    """Harga terakhir per simbol (diisi feed harga / hasil order)."""

    values: dict[str, Decimal] = field(default_factory=dict)

    def set(self, symbol: str, price: float | Decimal) -> None:
        self.values[symbol.upper()] = Decimal(str(price))


class TradingEngine:
    """Orkestrator: sinyal → risiko → eksekusi → pencatatan."""

    def __init__(
        self,
        config: AppConfig,
        *,
        store: PositionStore,
        risk: RiskManager,
        executor: TradeExecutor,
        broker: Broker,
        gate: SignalGate | None = None,
        notifier: NotifierDispatcher | None = None,
        dashboard: LocalStateSource | None = None,
        reconcile_fn: Any = None,
        clock: Any = None,
    ) -> None:
        self.config = config
        self.store = store
        self.risk = risk
        self.executor = executor
        self.broker = broker
        self.gate = gate
        self.notifier = notifier
        self.dashboard = dashboard
        self._reconcile = reconcile_fn
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.marks = _Marks()
        self.seen: set[tuple[str, str, int]] = set()
        self._recorded_outcomes: set[tuple[str, int]] = set()
        self._enforcing_limit = False
        self.stopped = False
        self.stop_reason: str | None = None
        self.counters: dict[str, int] = {
            "signals": 0,
            "entered": 0,
            "skipped": 0,
            "rejected": 0,
            "blocked": 0,
            "closed": 0,
        }

    # ------------------------------------------------------------------ util
    def _notify(self, event: NotifyEvent) -> None:
        if self.notifier is not None:
            self.notifier.emit(event)

    def _incident(self, kind: str, detail: str, *, symbol: str | None = None) -> None:
        self.store.add_incident(kind=kind, detail=detail, severity="critical", symbol=symbol)
        self._notify(
            event_for_incident(kind=kind, severity="critical", detail=detail, symbol=symbol)
        )
        logger.error("[INSIDEN] %s %s: %s", kind, symbol or "-", detail)

    def equity(self) -> float:
        """Ekuitas yang dipakai untuk sizing (dari state harian)."""
        return float(self.risk.daily.current_equity)

    def portfolio(self) -> Portfolio:
        posisi = []
        for pos in self.store.active_positions():
            harga = self.marks.values.get(pos.symbol, pos.entry_price)
            notional = abs(float(harga * pos.remaining_qty))
            posisi.append(
                PositionExposure(
                    symbol=pos.symbol,
                    notional=notional,
                    margin=notional / max(1, pos.leverage),
                    leverage=pos.leverage,
                )
            )
        return Portfolio(
            equity=self.equity(),
            positions=tuple(posisi),
            max_total_margin_pct=self.config.risk.max_total_margin_pct,
            max_per_symbol_margin_pct=self.config.risk.max_per_symbol_margin_pct,
        )

    def is_blocked(self) -> tuple[bool, str]:
        """Cek kill switch / batas harian sebelum menyentuh broker."""
        if self.stopped:
            return True, self.stop_reason or "sesi dihentikan"
        if self.risk.kill_switch_active():
            return True, "kill switch aktif"
        now = self.risk.now()
        batas = self.risk.daily.can_open(
            now,
            open_positions=len(self.store.active_positions()),
            max_positions=self.config.risk.max_concurrent_positions,
        )
        if batas.blocked:
            return True, batas.summary()
        return False, ""

    # --------------------------------------------------------------- sinyal
    async def handle_signal(self, signal: SignalEvent) -> EngineResult:
        """Proses satu sinyal: filter buku → risiko → eksekusi."""
        self.counters["signals"] += 1
        kunci = (signal.symbol, signal.direction, int(signal.open_time))
        if kunci in self.seen:
            return EngineResult("duplicate", signal.symbol, "sinyal yang sama sudah diproses")
        if len(self.seen) >= 8192:
            # Sinyal ganda hanya mungkin datang beberapa detik setelah yang asli
            # (reconnect WebSocket), jadi membersihkan riwayat lama aman dan
            # mencegah set ini tumbuh tanpa batas pada sesi berhari-hari.
            self.seen.clear()
        self.seen.add(kunci)
        if signal.atr > 0:
            self.set_atr(signal.symbol, signal.atr)

        diblokir, alasan = self.is_blocked()
        if diblokir:
            self.counters["blocked"] += 1
            logger.info("sinyal %s dilewati: %s", signal.symbol, alasan)
            return EngineResult("blocked", signal.symbol, alasan)

        # 1) filter order book (bila ada)
        candidate = None
        if self.gate is not None:
            candidate = await self.gate.evaluate(signal)
            if not candidate.tradable:
                self.counters["skipped"] += 1
                alasan = candidate.rejection_reason() or "ditolak filter order book"
                logger.info("kandidat %s ditolak: %s", signal.symbol, alasan)
                self._notify(event_for_candidate(candidate))
                return EngineResult("skipped", signal.symbol, alasan)
            self._notify(event_for_candidate(candidate))

        # 2) tiket risiko
        info = self._symbol_info(signal.symbol)
        if info is None:
            self.counters["rejected"] += 1
            return EngineResult("rejected", signal.symbol, "metadata simbol tidak ada")
        tiket = self.risk.evaluate(
            symbol=signal.symbol,
            side=signal.direction,
            entry_price=signal.close_price,
            atr=signal.atr,
            trail=signal.trail,
            equity=self.equity(),
            portfolio=self.portfolio(),
            info=info,
            open_positions=len(self.store.active_positions()),
        )
        if tiket.rejected:
            self.counters["rejected"] += 1
            logger.info("tiket %s ditolak: %s", signal.symbol, "; ".join(tiket.reasons))
            self._notify(event_for_rejected(tiket))
            return EngineResult("rejected", signal.symbol, "; ".join(tiket.reasons), ticket=tiket)

        # 3) eksekusi
        report = await self.executor.open_trade(tiket, atr=signal.atr)
        if report.naked:
            # Posisi tanpa SL dan gagal ditutup: berhenti total. Melanjutkan
            # trading dalam kondisi ini berarti menumpuk risiko tanpa pagar.
            self._incident(
                "naked_position",
                "posisi tanpa SL dan gagal ditutup otomatis; sesi dihentikan",
                symbol=signal.symbol,
            )
            self.stop_session(
                f"posisi TANPA SL gagal ditutup ({signal.symbol}); periksa bursa lalu "
                "hentikan/kembalikan secara manual"
            )
        pos = self.store.get_position(signal.symbol)
        if not report.ok or pos is None:
            self.counters["rejected"] += 1
            alasan = "; ".join(report.incidents) or "eksekusi gagal"
            for insiden in report.incidents:
                self._incident("execution_failed", insiden, symbol=signal.symbol)
            return EngineResult("rejected", signal.symbol, alasan, ticket=tiket, report=report)

        self.counters["entered"] += 1
        self.marks.set(signal.symbol, pos.entry_price)
        self._notify(event_for_entry(pos))
        logger.info("[MASUK] %s", pos.summary())
        return EngineResult("entered", signal.symbol, pos.summary(), ticket=tiket, report=report)

    def _symbol_info(self, symbol: str) -> Any:
        sumber = getattr(self.gate, "symbol_info", None)
        if sumber is None:
            return getattr(self, "_info_fallback", None)
        try:
            return sumber(symbol)
        except KeyError:
            return None

    def set_symbol_info(self, info: Any) -> None:
        """Sediakan metadata simbol langsung (dipakai bila tanpa ``SignalGate``)."""
        self._info_fallback = info

    # ---------------------------------------------------------------- harga
    async def handle_price(self, symbol: str, price: float) -> list[str]:
        """Harga terbaru: picu SL/TP (simulasi), trailing, dan update dashboard."""
        symbol = symbol.upper()
        self.marks.set(symbol, price)
        if self.dashboard is not None:
            self.dashboard.set_mark(symbol, Decimal(str(price)))
        pesan: list[str] = []
        sebelum = self.store.get_position(symbol)

        on_price = getattr(self.broker, "on_price", None)
        if on_price is not None:
            from volumatic_bot.execution.updates import order_result_to_update

            terpicu = await on_price(symbol, price)
            for order in terpicu:
                await self.executor.on_order_update(order_result_to_update(order))
                pesan.append(f"{symbol}: {order.describe()}")

        pos = self.store.get_position(symbol)
        if pos is not None and pos.active:
            atr = self._atr_of(symbol)
            if atr > 0:
                await self.executor.update_trailing(
                    symbol,
                    price=price,
                    atr=atr,
                    activation_price=self._activation_of(pos),
                )
        terbaru = self.store.get_position(symbol)
        if terbaru is not None and sebelum is not None and sebelum.active and not terbaru.active:
            await self._record_outcome_once(terbaru)
        return pesan

    def _atr_of(self, symbol: str) -> float:
        return float(getattr(self, "_atr_cache", {}).get(symbol.upper(), 0.0))

    def set_atr(self, symbol: str, atr: float) -> None:
        """Simpan ATR terakhir per simbol (dipakai trailing)."""
        cache = getattr(self, "_atr_cache", None)
        if cache is None:
            cache = {}
            self._atr_cache = cache
        cache[symbol.upper()] = atr

    def _activation_of(self, pos: Any) -> Decimal:
        """Harga aktivasi trailing sesuai ``trade.trailing_activation_r``."""
        entry = Decimal(str(pos.entry_price))
        risiko = Decimal(str(pos.risk_per_unit))
        r = Decimal(str(self.config.trade.trailing_activation_r))
        return entry + risiko * r if pos.side == "long" else entry - risiko * r

    # --------------------------------------------------------------- order
    async def handle_order_update(self, update: Any) -> None:
        """Teruskan event User Data Stream ke eksekutor, lalu tangani penutupan."""
        await self.executor.on_order_update(update)
        pos = self.store.get_position(update.symbol)
        if pos is None:
            return
        if not pos.active:
            await self._record_outcome_once(pos)
        elif (
            update.status == "FILLED"
            and getattr(update, "reduce_only", False)
            and getattr(update, "is_trade", False)
            and update.filled_qty < update.orig_qty
        ):
            self._notify(
                NotifyEvent(
                    kind=NotifyKind.PARTIAL_TP,
                    title=f"TP parsial {pos.symbol}",
                    detail=f"terisi {update.last_filled_qty} @ {update.avg_price}",
                    level=NotifyLevel.SUCCESS,
                    symbol=pos.symbol,
                )
            )

    async def handle_algo_update(self, update: Any) -> None:
        """Teruskan event ``ALGO_UPDATE`` (order kondisional) ke eksekutor."""
        await self.executor.on_algo_update(update)

    async def _record_outcome_once(self, pos: Any) -> None:
        """Catat hasil trade tepat satu kali per posisi (kunci: simbol+waktu buka).

        Tanpa kunci ini, event order yang datang setelah posisi tertutup
        (mis. pembatalan TP setelah SL kena) akan mencatat hasil yang sama dua
        kali — PnL, jumlah trade hari ini, dan rugi beruntun ikut dobel.
        """
        kunci = (pos.symbol, int(pos.opened_at))
        if kunci in self._recorded_outcomes:
            return
        if pos.realized_usdt is None:
            return
        self._recorded_outcomes.add(kunci)
        self.counters["closed"] += 1
        self._notify(event_for_exit(pos))
        await self._record_outcome(pos)

    async def _record_outcome(self, pos: Any) -> None:
        """Catat hasil trade ke batas harian, lalu tegakkan batas rugi harian."""
        from volumatic_bot.risk.limits import TradeOutcome

        if pos.realized_usdt is None:
            return
        now = self.risk.now()
        if now.tzinfo is None:
            from zoneinfo import ZoneInfo

            now = now.replace(tzinfo=ZoneInfo(self.config.app.timezone))
        pnl = float(pos.realized_usdt)
        self.risk.daily.record(
            TradeOutcome(symbol=pos.symbol, pnl_usdt=pnl, closed_at=now),
            now,
        )
        await self.enforce_daily_limit()

    async def check_kill_switch(self) -> bool:
        """Kill switch **saat berjalan**: tutup semua posisi lalu hentikan sesi.

        Bug yang diperbaiki di sini (15 Nov 2026): file kill switch hanya
        diperiksa di :meth:`is_blocked` (dipanggil saat ada sinyal) dan saat
        start. Akibatnya ``touch KILL_SWITCH`` pada bot yang sedang berjalan
        **tidak melakukan apa pun** sampai sinyal berikutnya datang — padahal
        config menjanjikan "tutup semua & hentikan".

        Dipanggil berkala (tiap beberapa detik) dari loop sesi live.
        """
        if self.stopped or not self.risk.kill_switch_active():
            return False
        aktif = self.store.active_positions()
        self._incident(
            "kill_switch",
            f"file {self.config.ops.kill_switch_file} terdeteksi; menutup "
            f"{len(aktif)} posisi lalu menghentikan sesi",
        )
        logger.warning("KILL SWITCH: menutup %s posisi lalu berhenti", len(aktif))
        ditutup: list[str] = []
        for pos in aktif:
            ok = await self.executor.close_position(pos.symbol, reason="kill_switch")
            if ok:
                self.counters["closed"] += 1
                ditutup.append(pos.symbol)
            else:
                logger.error("kill switch: gagal menutup %s — tutup manual di bursa", pos.symbol)
        for symbol in ditutup:
            pos_tutup = self.store.get_position(symbol)
            if pos_tutup is not None:
                await self._record_outcome_once(pos_tutup)
        self.stop_session(
            f"kill switch ({self.config.ops.kill_switch_file}) — hapus file itu untuk "
            "mengizinkan sesi baru"
        )
        return True

    async def enforce_daily_limit(self) -> bool:
        """Tutup semua posisi bila batas rugi harian tercapai (sesuai config)."""
        if not self.risk.daily.daily_stopped:
            return False
        if self._enforcing_limit:
            return False
        if not self.config.risk.close_all_on_daily_limit:
            self.stop_session("batas rugi harian tercapai")
            return False
        self._enforcing_limit = True
        try:
            ditutup: list[str] = []
            for pos in self.store.active_positions():
                if await self.executor.close_position(pos.symbol, reason="daily_limit"):
                    self.counters["closed"] += 1
                    ditutup.append(pos.symbol)
            for symbol in ditutup:
                pos_tutup = self.store.get_position(symbol)
                if pos_tutup is not None:
                    await self._record_outcome_once(pos_tutup)
            self.stop_session("batas rugi harian tercapai; semua posisi ditutup")
        finally:
            self._enforcing_limit = False
        self._notify(
            NotifyEvent(
                kind=NotifyKind.DAILY_STOP,
                title="batas rugi harian tercapai",
                detail=f"kerugian {self.risk.daily.loss_today(self.risk.now()):.2f} USDT; "
                "sesi berhenti sampai hari trading berikutnya",
                level=NotifyLevel.CRITICAL,
            )
        )
        return True

    def stop_session(self, reason: str) -> None:
        """Hentikan sesi (tidak menghapus kill switch; hanya menghentikan loop)."""
        if not self.stopped:
            logger.warning("sesi dihentikan: %s", reason)
        self.stopped = True
        self.stop_reason = reason

    async def shutdown(self, *, close_positions: bool = False, reason: str = "shutdown") -> int:
        """Hentikan mesin; opsional tutup semua posisi."""
        jumlah = 0
        if close_positions:
            ditutup: list[str] = []
            for pos in self.store.active_positions():
                if await self.executor.close_position(pos.symbol, reason="manual"):
                    jumlah += 1
                    ditutup.append(pos.symbol)
            for symbol in ditutup:
                pos_tutup = self.store.get_position(symbol)
                if pos_tutup is not None:
                    await self._record_outcome_once(pos_tutup)
        self.stop_session(reason)
        return jumlah

    # ---------------------------------------------------------- rekonsiliasi
    async def periodic(self) -> ReconcileReport | None:
        """Tugas berkala: kill switch, rekonsiliasi, penegakan batas harian."""
        if await self.check_kill_switch():
            return None
        await self.enforce_daily_limit()
        if self._reconcile is None:
            return None
        laporan: ReconcileReport = await self._reconcile(
            broker=self.broker, store=self.store, mode=self.config.ops.mode
        )
        for aksi in getattr(laporan, "actions", ()):
            if getattr(aksi, "severity", "info") not in ("warning", "critical"):
                continue
            self._notify(
                event_for_incident(
                    kind=getattr(aksi, "kind", "reconcile"),
                    severity=getattr(aksi, "severity", "warning"),
                    detail=getattr(aksi, "detail", str(aksi)),
                    symbol=getattr(aksi, "symbol", None),
                )
            )
        return laporan

    # ------------------------------------------------------------- dashboard
    def dashboard_state(self) -> DashboardState | None:
        """Ambil state dashboard terbaru (bila dashboard dipasang)."""
        if self.dashboard is None:
            return None
        if self.dashboard.daily_state is None:
            self.dashboard.daily_state = self.risk.daily
        for symbol, harga in self.marks.values.items():
            self.dashboard.set_mark(symbol, harga)
        state = self.dashboard.snapshot()
        from dataclasses import replace as _replace

        state.status = _replace(
            state.status,
            running=not self.stopped,
            note=self.stop_reason or state.status.note,
        )
        return state
