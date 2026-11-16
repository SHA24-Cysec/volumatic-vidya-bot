"""Penyimpanan posisi, order, insiden, dan state harian di SQLite.

Ini menutup lubang keamanan TAHAP 4: sebelumnya state harian (rugi hari ini,
cooldown, jumlah trade) hanya hidup di memori, sehingga ``Ctrl+C`` atau crash
menghapusnya dan bot bisa lanjut trading seolah tidak terjadi apa-apa.

Semua penulisan memakai transaksi SQLite biasa; modul ini dipanggil dari kode
async lewat ``asyncio.to_thread`` (lihat ``positions/store.py`` pemakaian di
eksekutor) supaya event loop tidak diblokir.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from volumatic_bot.positions.models import PositionState, TrackedPosition
from volumatic_bot.storage.db import SqliteDatabase

__all__ = ["DailySnapshot", "IncidentRecord", "PositionStore", "StoredOrder"]


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class StoredOrder:
    """Catatan order yang pernah dikirim bot (untuk audit & rekonsiliasi)."""

    symbol: str
    order_id: int | None
    client_order_id: str | None
    side: str
    type: str
    purpose: str
    status: str
    qty: str | None
    price: str | None
    stop_price: str | None
    reduce_only: bool
    mode: str
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    """Insiden yang wajib terlihat operator (mis. SL gagal dipasang)."""

    kind: str
    severity: str
    detail: str
    created_at: int
    symbol: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class DailySnapshot:
    """State harian yang dipersistensi agar restart tidak mereset kerugian."""

    day: str
    equity: float
    lost_usdt: float
    trades: int


class PositionStore:
    """Akses SQLite untuk posisi/order/insiden/state harian."""

    def __init__(self, db: SqliteDatabase) -> None:
        self.db = db

    # -------------------------------------------------------------- posisi
    # ------------------------------------------------------------- key-value
    def set_state(self, key: str, value: str) -> None:
        """Simpan satu nilai kecil (dipakai untuk status IP egress)."""
        self.db.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now_ms()),
        )

    def get_state(self, key: str) -> str | None:
        """Baca nilai yang disimpan :meth:`set_state` (``None`` bila belum ada)."""
        rows = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,))
        return str(rows[0][0]) if rows else None

    def save_position(self, pos: TrackedPosition) -> None:
        self.db.execute(
            """
            INSERT INTO positions (
                symbol, side, state, qty, remaining_qty, entry_price, stop_price,
                tp1_price, be_price, risk_per_unit, leverage, entry_order_id,
                sl_order_id, tp_order_id, mode, opened_at, updated_at,
                partial_done, be_done, closed_at, close_reason, realized_usdt
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
                side=excluded.side, state=excluded.state, qty=excluded.qty,
                remaining_qty=excluded.remaining_qty, entry_price=excluded.entry_price,
                stop_price=excluded.stop_price, tp1_price=excluded.tp1_price,
                be_price=excluded.be_price, risk_per_unit=excluded.risk_per_unit,
                leverage=excluded.leverage, entry_order_id=excluded.entry_order_id,
                sl_order_id=excluded.sl_order_id, tp_order_id=excluded.tp_order_id,
                mode=excluded.mode, updated_at=excluded.updated_at,
                partial_done=excluded.partial_done, be_done=excluded.be_done,
                closed_at=excluded.closed_at, close_reason=excluded.close_reason,
                realized_usdt=excluded.realized_usdt
            """,
            (
                pos.symbol,
                pos.side,
                pos.state.value,
                str(pos.qty),
                str(pos.remaining_qty),
                str(pos.entry_price),
                str(pos.stop_price),
                str(pos.tp1_price),
                str(pos.be_price) if pos.be_price is not None else None,
                str(pos.risk_per_unit),
                pos.leverage,
                pos.entry_order_id,
                pos.sl_order_id,
                pos.tp_order_id,
                pos.mode,
                pos.opened_at,
                pos.updated_at,
                1 if pos.partial_done else 0,
                1 if pos.be_done else 0,
                pos.closed_at,
                pos.close_reason,
                pos.realized_usdt,
            ),
        )

    def get_position(self, symbol: str) -> TrackedPosition | None:
        rows = self.db.execute("SELECT * FROM positions WHERE symbol = ?", (symbol.upper(),))
        return self._row_to_position(rows[0]) if rows else None

    def active_positions(self) -> list[TrackedPosition]:
        rows = self.db.execute(
            "SELECT * FROM positions WHERE state IN (?,?,?) ORDER BY opened_at",
            (
                PositionState.PENDING_ENTRY.value,
                PositionState.OPEN.value,
                PositionState.PARTIAL.value,
            ),
        )
        return [self._row_to_position(r) for r in rows]

    def all_positions(self, limit: int = 50) -> list[TrackedPosition]:
        rows = self.db.execute("SELECT * FROM positions ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [self._row_to_position(r) for r in rows]

    @staticmethod
    def _row_to_position(row: tuple[Any, ...]) -> TrackedPosition:
        # Urutan kolom mengikuti SELECT * pada skema positions.
        (
            symbol,
            side,
            state,
            qty,
            remaining_qty,
            entry_price,
            stop_price,
            tp1_price,
            be_price,
            risk_per_unit,
            leverage,
            entry_order_id,
            sl_order_id,
            tp_order_id,
            mode,
            opened_at,
            updated_at,
            partial_done,
            be_done,
            closed_at,
            close_reason,
            realized_usdt,
            *_sisa,
        ) = row
        return TrackedPosition(
            symbol=str(symbol),
            side=str(side),
            state=PositionState(str(state)),
            qty=Decimal(str(qty)),
            remaining_qty=Decimal(str(remaining_qty)),
            entry_price=Decimal(str(entry_price)),
            stop_price=Decimal(str(stop_price)),
            tp1_price=Decimal(str(tp1_price)),
            be_price=Decimal(str(be_price)) if be_price is not None else None,
            risk_per_unit=Decimal(str(risk_per_unit)),
            leverage=int(leverage),
            entry_order_id=int(entry_order_id) if entry_order_id is not None else None,
            sl_order_id=int(sl_order_id) if sl_order_id is not None else None,
            tp_order_id=int(tp_order_id) if tp_order_id is not None else None,
            mode=str(mode),
            opened_at=int(opened_at),
            updated_at=int(updated_at),
            partial_done=bool(partial_done),
            be_done=bool(be_done),
            closed_at=int(closed_at) if closed_at is not None else None,
            close_reason=str(close_reason) if close_reason is not None else None,
            realized_usdt=float(realized_usdt) if realized_usdt is not None else None,
        )

    # --------------------------------------------------------------- order
    def log_order(
        self,
        *,
        symbol: str,
        side: str,
        type: str,
        purpose: str,
        status: str,
        mode: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
        qty: str | None = None,
        price: str | None = None,
        stop_price: str | None = None,
        reduce_only: bool = False,
        raw: dict[str, Any] | None = None,
    ) -> None:
        now = _now_ms()
        self.db.execute(
            """
            INSERT INTO orders (symbol, order_id, client_order_id, side, type, purpose,
                                status, qty, price, stop_price, reduce_only, mode,
                                created_at, updated_at, raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                symbol.upper(),
                order_id,
                client_order_id,
                side,
                type,
                purpose,
                status,
                qty,
                price,
                stop_price,
                1 if reduce_only else 0,
                mode,
                now,
                now,
                json.dumps(raw, ensure_ascii=False) if raw else None,
            ),
        )

    def orders_for(self, symbol: str, limit: int = 20) -> list[StoredOrder]:
        rows = self.db.execute(
            """
            SELECT symbol, order_id, client_order_id, side, type, purpose, status,
                   qty, price, stop_price, reduce_only, mode, created_at, updated_at
            FROM orders WHERE symbol = ? ORDER BY id DESC LIMIT ?
            """,
            (symbol.upper(), limit),
        )
        return [
            StoredOrder(
                symbol=str(r[0]),
                order_id=int(r[1]) if r[1] is not None else None,
                client_order_id=str(r[2]) if r[2] is not None else None,
                side=str(r[3]),
                type=str(r[4]),
                purpose=str(r[5]),
                status=str(r[6]),
                qty=str(r[7]) if r[7] is not None else None,
                price=str(r[8]) if r[8] is not None else None,
                stop_price=str(r[9]) if r[9] is not None else None,
                reduce_only=bool(r[10]),
                mode=str(r[11]),
                created_at=int(r[12]),
                updated_at=int(r[13]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------ insiden
    def add_incident(
        self, *, kind: str, detail: str, severity: str = "error", symbol: str | None = None
    ) -> int:
        self.db.execute(
            "INSERT INTO incidents (symbol, kind, severity, detail, created_at) VALUES (?,?,?,?,?)",
            (symbol.upper() if symbol else None, kind, severity, detail, _now_ms()),
        )
        rows = self.db.execute("SELECT last_insert_rowid()")
        return int(rows[0][0]) if rows else 0

    def incidents(self, limit: int = 20) -> list[IncidentRecord]:
        rows = self.db.execute(
            "SELECT id, symbol, kind, severity, detail, created_at "
            "FROM incidents ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            IncidentRecord(
                id=int(r[0]),
                symbol=str(r[1]) if r[1] is not None else None,
                kind=str(r[2]),
                severity=str(r[3]),
                detail=str(r[4]),
                created_at=int(r[5]),
            )
            for r in rows
        ]

    # --------------------------------------------------------- state harian
    def save_daily(self, snapshot: DailySnapshot) -> None:
        self.db.execute(
            """
            INSERT INTO daily_state (day, equity, lost_usdt, trades, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(day) DO UPDATE SET
                equity=excluded.equity, lost_usdt=excluded.lost_usdt,
                trades=excluded.trades, updated_at=excluded.updated_at
            """,
            (snapshot.day, snapshot.equity, snapshot.lost_usdt, snapshot.trades, _now_ms()),
        )

    def load_daily(self, day: str) -> DailySnapshot | None:
        rows = self.db.execute(
            "SELECT day, equity, lost_usdt, trades FROM daily_state WHERE day = ?", (day,)
        )
        if not rows:
            return None
        r = rows[0]
        return DailySnapshot(
            day=str(r[0]), equity=float(r[1]), lost_usdt=float(r[2]), trades=int(r[3])
        )
