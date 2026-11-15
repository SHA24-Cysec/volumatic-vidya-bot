"""User Data Stream: status order & posisi tanpa polling REST.

Kenapa ini penting untuk bot modal kecil sekaligus hemat bandwidth:
tanpa stream ini, bot harus bertanya "order saya sudah terisi belum?" berkali-kali
lewat REST. Dengan stream ini, satu koneksi WebSocket memberi tahu kita **saat
itu juga**.

Aturan resmi yang dipegang (dokumentasi *User Data Streams / Connect*, 2025-01):

* ``listenKey`` berlaku **60 menit**; ``PUT`` memperpanjang 60 menit; ``POST``
  mengembalikan key yang sama bila masih aktif; ``DELETE`` menutup.
* Stream dibuka di ``/ws/<listenKey>``.
* Satu koneksi hanya valid **24 jam** → klien ini berotasi sebelum itu.
* Bila server mengirim ``listenKeyExpired``, key harus dibuat ulang dan koneksi
  disambung ulang; kita tidak berpura-pura stream masih hidup.

Payload penting yang diurai: ``ORDER_TRADE_UPDATE``, ``ACCOUNT_UPDATE``,
``MARGIN_CALL``, ``listenKeyExpired``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect

from volumatic_bot.binance.trading import TradingAPI
from volumatic_bot.binance.urls import Endpoint
from volumatic_bot.metrics import Metrics

__all__ = [
    "AccountUpdate",
    "ListenKeyManager",
    "OrderUpdate",
    "UserDataStream",
    "parse_account_update",
    "parse_order_update",
]

OrderHandler = Callable[["OrderUpdate"], Awaitable[None] | None]
AccountHandler = Callable[["AccountUpdate"], Awaitable[None] | None]
RawHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # pragma: no cover - defensif
        return Decimal(0)


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    """Satu kejadian ``ORDER_TRADE_UPDATE`` yang sudah dinormalkan."""

    symbol: str
    client_order_id: str
    side: str
    order_type: str
    status: str
    execution_type: str
    order_id: int
    orig_qty: Decimal
    last_filled_qty: Decimal
    filled_qty: Decimal
    last_price: Decimal
    avg_price: Decimal
    stop_price: Decimal
    reduce_only: bool
    close_position: bool
    commission: Decimal
    commission_asset: str
    realized_pnl: Decimal
    is_maker: bool
    event_time_ms: int

    @property
    def filled(self) -> bool:
        return self.status == "FILLED"

    @property
    def partially_filled(self) -> bool:
        return self.status == "PARTIALLY_FILLED"

    @property
    def cancelled(self) -> bool:
        return self.status in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH")

    @property
    def is_trade(self) -> bool:
        return self.execution_type == "TRADE"

    @property
    def is_liquidation(self) -> bool:
        return self.client_order_id.startswith(("autoclose-", "adl_autoclose"))


@dataclass(frozen=True, slots=True)
class AccountUpdate:
    """Ringkasan ``ACCOUNT_UPDATE``: perubahan saldo & posisi."""

    event_time_ms: int
    reason: str
    balances: dict[str, Decimal] = field(default_factory=dict)
    positions: tuple[dict[str, Any], ...] = ()


def parse_order_update(payload: dict[str, Any]) -> OrderUpdate:
    """Urai event ``ORDER_TRADE_UPDATE`` (field pendek sesuai dokumentasi)."""
    data = payload.get("o")
    if not isinstance(data, dict):
        raise ValueError("payload bukan ORDER_TRADE_UPDATE (tidak ada field 'o')")
    return OrderUpdate(
        symbol=str(data.get("s", "")),
        client_order_id=str(data.get("c", "")),
        side=str(data.get("S", "")),
        order_type=str(data.get("o", "")),
        status=str(data.get("X", "")),
        execution_type=str(data.get("x", "")),
        order_id=int(data.get("i", 0) or 0),
        orig_qty=_dec(data.get("q", 0)),
        last_filled_qty=_dec(data.get("l", 0)),
        filled_qty=_dec(data.get("z", 0)),
        last_price=_dec(data.get("L", 0)),
        avg_price=_dec(data.get("ap", 0)),
        stop_price=_dec(data.get("sp", 0)),
        reduce_only=bool(data.get("R", False)),
        close_position=bool(data.get("cp", False)),
        commission=_dec(data.get("n", 0)),
        commission_asset=str(data.get("N", "")),
        realized_pnl=_dec(data.get("rp", 0)),
        is_maker=bool(data.get("m", False)),
        event_time_ms=int(payload.get("E", 0) or 0),
    )


def parse_account_update(payload: dict[str, Any]) -> AccountUpdate:
    data = payload.get("a")
    if not isinstance(data, dict):
        raise ValueError("payload bukan ACCOUNT_UPDATE (tidak ada field 'a')")
    balances: dict[str, Decimal] = {}
    for item in data.get("B", []) or []:
        if isinstance(item, dict):
            balances[str(item.get("a", ""))] = _dec(item.get("wb", 0))
    positions = tuple(dict(p) for p in (data.get("P", []) or []) if isinstance(p, dict))
    return AccountUpdate(
        event_time_ms=int(payload.get("E", 0) or 0),
        reason=str(data.get("m", "")),
        balances=balances,
        positions=positions,
    )


class ListenKeyManager:
    """Kelola umur ``listenKey``: buat, perpanjang, hapus."""

    def __init__(self, api: TradingAPI, *, keepalive_sec: float = 1800.0) -> None:
        if not 60 <= keepalive_sec <= 3540:
            raise ValueError("keepalive_sec harus 60..3540 detik (listenKey berlaku 60 menit)")
        self.api = api
        self.keepalive_sec = keepalive_sec
        self.key: str | None = None
        self.creations = 0
        self.extends = 0

    async def create(self) -> str:
        self.key = await self.api.create_listen_key()
        self.creations += 1
        return self.key

    async def ensure(self) -> str:
        """Buat key bila belum ada."""
        if self.key is None:
            return await self.create()
        return self.key

    async def extend(self) -> bool:
        """Perpanjang 60 menit; buat ulang bila key sudah tidak dikenal (-1125)."""
        if self.key is None:
            await self.create()
            return True
        try:
            await self.api.keepalive_listen_key(self.key)
            self.extends += 1
            return True
        except Exception:
            # Key kedaluwarsa/dihapus: jangan dipakai lagi, buat yang baru.
            await self.create()
            return False

    async def close(self) -> None:
        if self.key is None:
            return
        with contextlib.suppress(Exception):
            await self.api.close_listen_key(self.key)
        self.key = None


class UserDataStream:
    """Klien User Data Stream dengan keepalive & reconnect otomatis."""

    def __init__(
        self,
        keys: ListenKeyManager,
        *,
        base_url: str | None = None,
        testnet: bool = True,
        metrics: Metrics | None = None,
        on_order: OrderHandler | None = None,
        on_account: AccountHandler | None = None,
        on_raw: RawHandler | None = None,
        rotate_after_sec: float = 23 * 3600.0,
        max_backoff_sec: float = 60.0,
        connect_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.keys = keys
        self.base_url = (base_url or Endpoint.ws(testnet=testnet)).rstrip("/")
        self.metrics = metrics or Metrics()
        self.on_order = on_order
        self.on_account = on_account
        self.on_raw = on_raw
        self.rotate_after_sec = rotate_after_sec
        self.max_backoff_sec = max_backoff_sec
        self._connect = connect_factory or ws_connect
        self._clock = clock
        self._jitter = jitter
        self._stop = asyncio.Event()
        self._ws: ClientConnection | None = None
        self._reconnects = 0
        self._expired_events = 0

    # ------------------------------------------------------------------ info
    def url(self) -> str:
        if not self.keys.key:
            raise ValueError("listenKey belum dibuat")
        return f"{self.base_url}/ws/{self.keys.key}"

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def reconnects(self) -> int:
        return self._reconnects

    @property
    def expired_events(self) -> int:
        return self._expired_events

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ inti
    async def run(self) -> None:
        """Jalankan sampai :meth:`stop` dipanggil (reconnect otomatis)."""
        attempt = 0
        while not self._stop.is_set():
            try:
                await self.keys.ensure()
                await self._run_connection()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._stop.is_set():
                    break
                attempt += 1
                self._reconnects += 1
                delay = min(self.max_backoff_sec, 0.5 * 2**attempt) * (0.75 + 0.5 * self._jitter())
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=max(0.1, delay))

    async def _run_connection(self) -> None:
        await self.keys.ensure()
        started = self._clock()
        last_message = started
        keepalive_task: asyncio.Task[None] | None = None
        try:
            async with self._connect(self.url(), max_size=2**20, open_timeout=10) as ws:
                self._ws = ws
                keepalive_task = asyncio.create_task(self._keepalive_loop())
                while not self._stop.is_set():
                    timeout = max(1.0, self.rotate_after_sec - (self._clock() - started))
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(timeout, 120.0))
                    except TimeoutError:
                        if self._clock() - started >= self.rotate_after_sec:
                            return  # rotasi 24 jam: biarkan run() menyambung lagi
                        if self._clock() - last_message > 600:
                            return  # tidak ada kabar 10 menit: anggap mati
                        continue
                    last_message = self._clock()
                    self.metrics.record_ws_message(len(raw) if isinstance(raw, bytes | str) else 0)
                    await self._handle_raw(raw)
        finally:
            self._ws = None
            if keepalive_task is not None:
                keepalive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await keepalive_task

    async def _keepalive_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.keys.keepalive_sec)
            if self._stop.is_set():
                return
            baru = await self.keys.extend()
            if not baru:
                # Key mati: tutup koneksi supaya run() menyambung dengan key baru.
                if self._ws is not None:
                    with contextlib.suppress(Exception):
                        await self._ws.close()
                return

    async def _handle_raw(self, raw: str | bytes) -> None:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        if self.on_raw is not None:
            await _maybe_await(self.on_raw(payload))
        event = str(payload.get("e", ""))
        if event == "listenKeyExpired":
            self._expired_events += 1
            await self.keys.create()  # key baru; koneksi akan disambung ulang
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
            return
        if event == "ORDER_TRADE_UPDATE" and self.on_order is not None:
            try:
                update = parse_order_update(payload)
            except ValueError:
                return
            await _maybe_await(self.on_order(update))
            return
        if event == "ACCOUNT_UPDATE" and self.on_account is not None:
            try:
                akun = parse_account_update(payload)
            except ValueError:
                return
            await _maybe_await(self.on_account(akun))


async def _maybe_await(value: Any) -> None:
    if hasattr(value, "__await__"):
        await value
