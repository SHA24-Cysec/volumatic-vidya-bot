"""Manajer risiko: satu pintu masuk sebelum posisi boleh dibuka.

Dipanggil **setelah** sinyal lolos filter order book (TAHAP 3) dan **sebelum**
order dikirim (TAHAP 5). Urutannya sengaja dari yang paling murah/cepat ke yang
paling mahal:

1. mode/izin (dry-run vs live, konfirmasi) — TAHAP 5 wajib memeriksa lagi;
2. kill switch (file ``ops.kill_switch_file`` atau flag internal);
3. slot posisi & duplikat simbol (maks 1 posisi per simbol);
4. batas harian (rugi, jumlah trade, cooldown);
5. portofolio (margin terpakai, konsentrasi per simbol);
6. ukuran posisi berbasis risiko (+ filter modal kecil);
7. rencana level SL/TP/BE/trailing.

Hasilnya :class:`TradeTicket` yang **lengkap atau ditolak** — tidak ada state
setengah jadi. Bila ditolak, ``reasons`` berisi alasan berbahasa Indonesia yang
bisa langsung ditampilkan di log/dashboard.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from volumatic_bot.binance.symbols import SymbolInfo
from volumatic_bot.config import AppConfig
from volumatic_bot.risk.limits import DailyState
from volumatic_bot.risk.plan import TradePlan, build_trade_plan
from volumatic_bot.risk.portfolio import Portfolio
from volumatic_bot.risk.sizing import PositionSize, size_position

__all__ = ["RiskManager", "TradeTicket"]


@dataclass(frozen=True, slots=True)
class TradeTicket:
    """Hasil keputusan manajer risiko untuk satu sinyal."""

    approved: bool
    symbol: str
    side: str
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    size: PositionSize | None = None
    plan: TradePlan | None = None
    checks: tuple[str, ...] = field(default=())

    @property
    def rejected(self) -> bool:
        return not self.approved

    def summary(self) -> str:
        if not self.approved:
            return f"DITOLAK {self.symbol}: " + "; ".join(self.reasons)
        assert self.size is not None and self.plan is not None
        return f"DISETUJUI {self.symbol}: {self.size.summary()} | {self.plan.summary()}"

    def as_row(self) -> dict[str, object]:
        row: dict[str, object] = {
            "symbol": self.symbol,
            "side": self.side,
            "approved": self.approved,
            "reasons": list(self.reasons),
        }
        if self.size is not None:
            row.update(
                {
                    "qty": str(self.size.qty),
                    "notional": round(float(self.size.notional), 4),
                    "margin": round(float(self.size.margin), 4),
                    "risk_usdt": round(self.size.risk_usdt, 4),
                    "risk_pct": round(self.size.risk_pct, 3),
                }
            )
        if self.plan is not None:
            row.update(
                {
                    "stop": str(self.plan.stop_price),
                    "tp1": str(self.plan.tp1_price),
                    "be": str(self.plan.be_price),
                }
            )
        return row


class RiskManager:
    """Penjaga tunggal sebelum order dikirim."""

    def __init__(
        self,
        config: AppConfig,
        *,
        daily: DailyState,
        clock: object = datetime.now,
        kill_switch_path: str | Path | None = None,
    ) -> None:
        self.config = config
        self.daily = daily
        self._clock = clock
        self.kill_switch_path = (
            Path(kill_switch_path)
            if kill_switch_path is not None
            else Path(config.ops.kill_switch_file)
        )

    # ------------------------------------------------------------------ util
    def now(self) -> datetime:
        """Waktu sekarang (bisa diinjeksi pada test)."""
        value = self._clock()  # type: ignore[operator]
        if not isinstance(value, datetime):  # pragma: no cover - defensif
            raise TypeError("clock harus mengembalikan datetime")
        return value

    def kill_switch_active(self) -> bool:
        """True bila file kill switch ada **atau** state internal sudah killed."""
        try:
            if self.kill_switch_path.exists():
                return True
        except OSError:  # pragma: no cover - path aneh
            return False
        return self.daily.stopped

    # ------------------------------------------------------------------ utama
    def evaluate(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        atr: float,
        trail: float | None,
        equity: float,
        portfolio: Portfolio,
        info: SymbolInfo,
        open_positions: int,
        leverage: int | None = None,
    ) -> TradeTicket:
        """Nilai satu sinyal; kembalikan tiket lengkap atau penolakan beralasan."""
        if side not in ("long", "short"):
            raise ValueError(f"side tidak dikenal: {side!r}")
        now = self.now()
        lev = leverage if leverage is not None else self.config.exchange.default_leverage
        reasons: list[str] = []
        notes: list[str] = []
        checks: list[str] = []

        # 1) kill switch -----------------------------------------------------
        checks.append("kill_switch")
        if self.kill_switch_active():
            alasan = (
                f"file kill switch ditemukan ({self.kill_switch_path})"
                if self.kill_switch_path.exists()
                else "kill switch internal aktif (batas harian tercapai)"
            )
            reasons.append(alasan)

        # 2) duplikat simbol & slot -----------------------------------------
        checks.append("slot_posisi")
        if portfolio.has_symbol(symbol):
            reasons.append(f"sudah ada posisi terbuka di {symbol} (maks 1 posisi per simbol)")
        max_pos = self.config.risk.max_concurrent_positions
        if open_positions >= max_pos:
            reasons.append(f"slot posisi penuh ({open_positions}/{max_pos})")

        # 3) batas harian ----------------------------------------------------
        checks.append("batas_harian")
        daily = self.daily.can_open(now, open_positions=open_positions, max_positions=max_pos)
        reasons.extend(daily.reasons)
        notes.extend(daily.notes)

        # 4) leverage --------------------------------------------------------
        checks.append("leverage")
        if lev > self.config.exchange.max_leverage:
            reasons.append(
                f"leverage {lev}x melebihi batas config {self.config.exchange.max_leverage}x"
            )
        if lev > 10:
            reasons.append(f"leverage {lev}x > 10x butuh konfirmasi eksplisit pengguna")

        # 5) equity ----------------------------------------------------------
        checks.append("equity")
        if equity < self.config.risk.min_equity_usdt:
            # Pesan ini sengaja menjelaskan jalan keluarnya: pada modal 5-10 USDT
            # satu kerugian saja sudah bisa menjatuhkan equity di bawah lantai,
            # dan pengguna berhak tahu itu setelan, bukan bug.
            reasons.append(
                f"equity {equity:.2f} USDT < minimum "
                f"{self.config.risk.min_equity_usdt:.2f} USDT — modal tambah, atau "
                "turunkan risk.min_equity_usdt (wajib allow_aggressive_risk: true) "
                "bila memang ingin lanjut; lihat docs/modal-kecil.md §9"
            )
        if not info.is_perpetual_usdt:
            reasons.append(f"{symbol} bukan perpetual USDT yang sedang TRADING")

        if reasons:
            return TradeTicket(
                approved=False,
                symbol=symbol,
                side=side,
                reasons=tuple(reasons),
                notes=tuple(notes),
                checks=tuple(checks),
            )

        # 6) ukuran posisi ---------------------------------------------------
        checks.append("ukuran_posisi")
        available = min(
            portfolio.free_margin,
            portfolio.symbol_margin_limit() - portfolio.symbol_margin(symbol),
        )
        size = size_position(
            equity=equity,
            entry_price=entry_price,
            stop_price=self._preliminary_stop(entry_price, atr, trail, side, info),
            filters=info.filters,
            side=side,  # type: ignore[arg-type]
            leverage=lev,
            risk_pct=self.config.risk.risk_per_trade_pct,
            commission=self.config.commission,
            config=self.config.risk,
            available_margin=available,
        )

        # 7) rencana level ---------------------------------------------------
        # Penting: bila ukuran tidak disetujui (qty 0), rencana tidak bisa disusun
        # (`build_trade_plan` melempar ValueError "qty harus > 0"). Tanpa pagar
        # ini, penolakan karena modal kecil bukan "dilewati" tetapi CRASH — bug
        # yang muncul saat menguji modal 7 USDT (15 Nov 2026).
        if not size.approved or size.qty <= 0:
            reasons.extend(size.reasons or ["ukuran posisi tidak disetujui"])
            return TradeTicket(
                symbol=symbol,
                side=side,
                approved=False,
                size=size,
                plan=None,
                reasons=tuple(reasons),
                notes=tuple(notes),
                checks=tuple(checks),
            )

        checks.append("rencana_level")
        plan = build_trade_plan(
            side=side,  # type: ignore[arg-type]
            entry_price=entry_price,
            atr=atr,
            trail=trail,
            qty=size.qty if size.qty > 0 else 0,
            filters=info.filters,
            trade=self.config.trade,
            commission=self.config.commission,
        )

        # Ulangi ukuran memakai SL final dari rencana, supaya risiko yang
        # dilaporkan benar-benar sesuai jarak SL yang akan dipasang. Hasil
        # ulangan bisa saja kembali 0 (mis. SL final lebih lebar sehingga qty
        # terpotong di bawah stepSize), jadi diperiksa lagi sebelum dipakai —
        # `build_trade_plan` menolak qty 0 dengan ValueError.
        size = size_position(
            equity=equity,
            entry_price=entry_price,
            stop_price=plan.stop_price,
            filters=info.filters,
            side=side,  # type: ignore[arg-type]
            leverage=lev,
            risk_pct=self.config.risk.risk_per_trade_pct,
            commission=self.config.commission,
            config=self.config.risk,
            available_margin=available,
        )
        if not size.approved or size.qty <= 0:
            reasons.extend(size.reasons or ["ukuran posisi final tidak disetujui"])
            return TradeTicket(
                symbol=symbol,
                side=side,
                approved=False,
                size=size,
                plan=None,
                reasons=tuple(reasons),
                notes=tuple(notes),
                checks=tuple(checks),
            )

        plan = build_trade_plan(
            side=side,  # type: ignore[arg-type]
            entry_price=entry_price,
            atr=atr,
            trail=trail,
            qty=size.qty,
            filters=info.filters,
            trade=self.config.trade,
            commission=self.config.commission,
        )

        reasons.extend(size.reasons)
        notes.extend(size.notes)
        notes.extend(plan.notes)

        # 8) pagar tambahan yang butuh angka final --------------------------
        checks.append("likuidasi")
        if size.approved and not math.isfinite(size.liq_distance_pct):
            reasons.append("perkiraan jarak likuidasi tidak terhingga; perhitungan tidak valid")
        if size.approved and size.liq_distance_pct < self.config.risk.min_liq_distance_pct:
            reasons.append(
                f"jarak likuidasi {size.liq_distance_pct:.2f}% < "
                f"minimum {self.config.risk.min_liq_distance_pct:.2f}%"
            )
        checks.append("biaya")
        if size.approved and size.fee_r >= self.config.trade.max_fee_r:
            reasons.append(f"biaya {size.fee_r:.2f}R >= batas {self.config.trade.max_fee_r:.2f}R")

        return TradeTicket(
            approved=not reasons,
            symbol=symbol,
            side=side,
            reasons=tuple(reasons),
            notes=tuple(notes),
            size=size,
            plan=plan,
            checks=tuple(checks),
        )

    # ------------------------------------------------------------- internal
    def _preliminary_stop(
        self, entry: float, atr: float, trail: float | None, side: str, info: SymbolInfo
    ) -> float:
        """Perkiraan SL untuk menghitung ukuran; SL final ditentukan ``build_trade_plan``."""
        trade = self.config.trade
        distance = trade.sl_atr_mult * atr
        if trail is not None and trail > 0:
            trail_ok = (side == "long" and trail < entry) or (side == "short" and trail > entry)
            if trail_ok and trade.sl_mode in ("vidya_trail", "nearest"):
                distance = min(distance, abs(entry - trail))
        distance = max(distance, trade.sl_min_atr_mult * atr)
        stop = entry - distance if side == "long" else entry + distance
        return float(info.filters.round_price(stop, mode="down" if side == "long" else "up"))
