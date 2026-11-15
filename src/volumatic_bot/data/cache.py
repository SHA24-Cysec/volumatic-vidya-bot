"""Cache candle berbasis SQLite.

Tujuan: **restart tidak mengunduh ulang**. Data warm-up (default 1500 candle per
simbol) disimpan sekali, lalu pembaruan hanya mengambil bar yang belum ada.

Format baris: ``(open_time, open, high, low, close, volume)``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from volumatic_bot.models import CandleSeries
from volumatic_bot.storage.db import SqliteDatabase

__all__ = ["CacheStats", "KlineCache"]

_UPSERT = """
    INSERT INTO klines (symbol, interval, open_time, open, high, low, close, volume)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(symbol, interval, open_time) DO UPDATE SET
        open=excluded.open, high=excluded.high, low=excluded.low,
        close=excluded.close, volume=excluded.volume
"""


@dataclass(frozen=True, slots=True)
class CacheStats:
    symbols: int
    rows: int
    size_mb: float


class KlineCache:
    """Cache kline di SQLite (async, dibungkus thread)."""

    def __init__(self, db_path: str | Path, *, enabled: bool = True) -> None:
        self.db = SqliteDatabase(db_path)
        self.enabled = enabled
        self._opened = False

    async def open(self) -> None:
        if self._opened or not self.enabled:
            return
        await asyncio.to_thread(self.db.open)
        self._opened = True

    async def close(self) -> None:
        if not self._opened:
            return
        await asyncio.to_thread(self.db.close)
        self._opened = False

    # ------------------------------------------------------------------ tulis
    async def upsert(
        self,
        symbol: str,
        interval: str,
        rows: list[tuple[int, float, float, float, float, float]],
    ) -> int:
        """Simpan/perbarui baris candle. Baris duplikat akan ditimpa."""
        if not self.enabled or not rows:
            return 0
        await self.open()
        payload = [(symbol.upper(), interval, *row) for row in rows]
        written = await self.db.aexecutemany(_UPSERT, payload)
        last_open = max(row[0] for row in rows)
        now = int(time.time())
        await self.db.aexecute(
            """
            INSERT INTO symbol_meta (symbol, interval, last_open_time, rows, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                interval=excluded.interval,
                last_open_time=excluded.last_open_time,
                rows=excluded.rows,
                updated_at=excluded.updated_at
            """,
            (symbol.upper(), interval, last_open, len(rows), now),
        )
        return written

    # ------------------------------------------------------------------ baca
    async def load(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> list[tuple[int, float, float, float, float, float]]:
        """Ambil baris candle terurut naik (boleh dibatasi jendela waktu)."""
        if not self.enabled:
            return []
        await self.open()
        sql = (
            "SELECT open_time, open, high, low, close, volume FROM klines "
            "WHERE symbol=? AND interval=?"
        )
        params: list[object] = [symbol.upper(), interval]
        if since is not None:
            sql += " AND open_time>=?"
            params.append(since)
        if until is not None:
            sql += " AND open_time<=?"
            params.append(until)
        if limit is not None:
            # Ambil N terakhir, lalu balik urutannya agar naik.
            sql += " ORDER BY open_time DESC LIMIT ?"
            params.append(limit)
            rows = await self.db.aexecute(sql, params)
            return list(reversed(rows))
        sql += " ORDER BY open_time ASC"
        return await self.db.aexecute(sql, params)

    async def load_series(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int | None = None,
        drop_unclosed_after: int | None = None,
    ) -> CandleSeries:
        """Muat candle sebagai :class:`CandleSeries` (opsional buang candle berjalan)."""
        rows = await self.load(symbol, interval, limit=limit)
        if drop_unclosed_after is not None:
            rows = [r for r in rows if r[0] < drop_unclosed_after]
        if not rows:
            return CandleSeries.from_columns([], [], [], [], [], [])
        arr = np.array(rows, dtype=np.float64)
        return CandleSeries.from_columns(
            open_time=arr[:, 0].astype(np.int64),
            open=arr[:, 1],
            high=arr[:, 2],
            low=arr[:, 3],
            close=arr[:, 4],
            volume=arr[:, 5],
        )

    async def last_open_time(self, symbol: str, interval: str) -> int | None:
        if not self.enabled:
            return None
        await self.open()
        rows = await self.db.aexecute(
            "SELECT MAX(open_time) FROM klines WHERE symbol=? AND interval=?",
            (symbol.upper(), interval),
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    async def first_open_time(self, symbol: str, interval: str) -> int | None:
        if not self.enabled:
            return None
        await self.open()
        rows = await self.db.aexecute(
            "SELECT MIN(open_time) FROM klines WHERE symbol=? AND interval=?",
            (symbol.upper(), interval),
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    async def count(self, symbol: str, interval: str) -> int:
        if not self.enabled:
            return 0
        await self.open()
        rows = await self.db.aexecute(
            "SELECT COUNT(*) FROM klines WHERE symbol=? AND interval=?", (symbol.upper(), interval)
        )
        return int(rows[0][0]) if rows else 0

    # ------------------------------------------------------------------ utilitas
    async def stats(self) -> CacheStats:
        if not self.enabled:
            return CacheStats(symbols=0, rows=0, size_mb=0.0)
        await self.open()
        rows = await self.db.aexecute("SELECT COUNT(DISTINCT symbol), COUNT(*) FROM klines")
        symbols = int(rows[0][0]) if rows else 0
        total = int(rows[0][1]) if rows else 0
        size = 0.0
        try:
            size = self.db.path.stat().st_size / (1024 * 1024)
        except OSError:
            size = 0.0
        return CacheStats(symbols=symbols, rows=total, size_mb=round(size, 2))

    async def prune(self, *, keep_symbols: int, interval: str) -> int:
        """Buang simbol yang paling lama tidak diperbarui (batasi ukuran cache)."""
        if not self.enabled:
            return 0
        await self.open()
        rows = await self.db.aexecute(
            "SELECT symbol FROM symbol_meta WHERE interval=? "
            "ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
            (interval, keep_symbols),
        )
        victims = [r[0] for r in rows]
        for symbol in victims:
            await self.db.aexecute(
                "DELETE FROM klines WHERE symbol=? AND interval=?", (symbol, interval)
            )
            await self.db.aexecute("DELETE FROM symbol_meta WHERE symbol=?", (symbol,))
        return len(victims)
