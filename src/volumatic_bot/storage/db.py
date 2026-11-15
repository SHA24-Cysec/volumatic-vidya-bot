"""Lapisan SQLite tipis (dijalankan di thread agar tidak memblokir event loop).

Catatan kompatibilitas Python 3.14: atribut ``sqlite3.version`` dan
``sqlite3.version_info`` **dihapus**; gunakan ``sqlite3.sqlite_version`` untuk
versi pustaka SQLite yang sebenarnya (lihat ``docs/kompatibilitas-python.md``).

Modul ini sengaja hanya bergantung pada ``sqlite3`` standar: tidak ada dependensi
tambahan (``aiosqlite``) sehingga instalasi tetap ringan dan CI cepat.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Self

__all__ = ["SqliteDatabase", "sqlite_runtime_version"]

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS klines (
        symbol    TEXT    NOT NULL,
        interval  TEXT    NOT NULL,
        open_time INTEGER NOT NULL,
        open      REAL    NOT NULL,
        high      REAL    NOT NULL,
        low       REAL    NOT NULL,
        close     REAL    NOT NULL,
        volume    REAL    NOT NULL,
        PRIMARY KEY (symbol, interval, open_time)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS symbol_meta (
        symbol        TEXT PRIMARY KEY,
        interval      TEXT NOT NULL,
        last_open_time INTEGER NOT NULL,
        rows          INTEGER NOT NULL DEFAULT 0,
        updated_at    INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    # ---------------------------------------------------------------- TAHAP 5
    """
    CREATE TABLE IF NOT EXISTS positions (
        symbol         TEXT PRIMARY KEY,
        side           TEXT    NOT NULL,
        state          TEXT    NOT NULL,
        qty            TEXT    NOT NULL,
        remaining_qty  TEXT    NOT NULL,
        entry_price    TEXT    NOT NULL,
        stop_price     TEXT    NOT NULL,
        tp1_price      TEXT    NOT NULL,
        be_price       TEXT,
        risk_per_unit  TEXT    NOT NULL,
        leverage       INTEGER NOT NULL,
        entry_order_id INTEGER,
        sl_order_id    INTEGER,
        tp_order_id    INTEGER,
        mode           TEXT    NOT NULL,
        opened_at      INTEGER NOT NULL,
        updated_at     INTEGER NOT NULL,
        partial_done   INTEGER NOT NULL DEFAULT 0,
        be_done        INTEGER NOT NULL DEFAULT 0,
        closed_at      INTEGER,
        close_reason   TEXT,
        realized_usdt  REAL,
        payload        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT    NOT NULL,
        order_id        INTEGER,
        client_order_id TEXT,
        side            TEXT    NOT NULL,
        type            TEXT    NOT NULL,
        purpose         TEXT    NOT NULL,
        status          TEXT    NOT NULL,
        qty             TEXT,
        price           TEXT,
        stop_price      TEXT,
        reduce_only     INTEGER NOT NULL DEFAULT 0,
        mode            TEXT    NOT NULL,
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL,
        raw             TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS incidents (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol     TEXT,
        kind       TEXT    NOT NULL,
        severity   TEXT    NOT NULL,
        detail     TEXT    NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_state (
        day        TEXT PRIMARY KEY,
        equity     REAL NOT NULL,
        lost_usdt  REAL NOT NULL DEFAULT 0,
        trades     INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL
    )
    """,
)


def sqlite_runtime_version() -> str:
    """Versi pustaka SQLite runtime (bukan versi modul)."""
    return sqlite3.sqlite_version


class SqliteDatabase:
    """Pembungkus koneksi SQLite tunggal dengan akses serialisasi lewat lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        for statement in SCHEMA:
            conn.execute(statement)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database belum dibuka; panggil open() lebih dulu")
        return self._conn

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --------------------------------------------------------------- sinkron
    def execute(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            cursor = self.connection.execute(sql, params)
            rows = cursor.fetchall()
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
                self.connection.commit()
            return [tuple(r) for r in rows]

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self._lock:
            cursor = self.connection.executemany(sql, rows)
            self.connection.commit()
            return int(cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0)

    def executescript(self, script: str) -> None:
        with self._lock:
            self.connection.executescript(script)
            self.connection.commit()

    # ------------------------------------------------------------------ async
    async def aexecute(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return await asyncio.to_thread(self.execute, sql, params)

    async def aexecutemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        materialised = list(rows)
        return await asyncio.to_thread(self.executemany, sql, materialised)
