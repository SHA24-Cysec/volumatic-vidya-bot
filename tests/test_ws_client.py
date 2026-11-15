"""Test klien WebSocket terhadap server lokal ``websockets`` (tanpa internet).

Yang diuji: parsing event kline, hanya memakai candle tertutup (di level konsumen),
reconnect otomatis, watchdog idle, rotasi koneksi sebelum 24 jam, dan pengiriman
perintah SUBSCRIBE saat daftar simbol berubah.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
import pytest_asyncio
from websockets.asyncio.server import ServerConnection, serve

from volumatic_bot.binance.ws import KlineStreamClient, kline_stream_name
from volumatic_bot.metrics import Metrics

INTERVAL_MS = 900_000


def kline_payload(
    symbol: str = "BTCUSDT",
    *,
    open_time: int = 1_764_500_000_000,
    closed: bool = True,
    close: float = 90_000.0,
    event_time: int | None = None,
) -> dict[str, Any]:
    return {
        "stream": f"{symbol.lower()}@kline_15m",
        "data": {
            "e": "kline",
            "E": event_time if event_time is not None else int(time.time() * 1000),
            "s": symbol,
            "k": {
                "t": open_time,
                "T": open_time + INTERVAL_MS - 1,
                "s": symbol,
                "i": "15m",
                "f": 100,
                "L": 200,
                "o": "89000.0",
                "c": str(close),
                "h": "90500.0",
                "l": "88500.0",
                "v": "123.45",
                "n": 999,
                "x": closed,
                "q": "11111111.0",
                "V": "60.0",
                "Q": "5400000.0",
                "B": "0",
            },
        },
    }


@pytest_asyncio.fixture
async def ws_server() -> Any:
    """Server yang perilakunya bisa diatur lewat ``state``."""
    state: dict[str, Any] = {
        "connections": 0,
        "received": [],
        "messages_per_connection": 2,
        "send_nothing": False,
        "close_first": True,
    }

    async def handler(ws: ServerConnection) -> None:
        state["connections"] += 1
        index = state["connections"]
        state.setdefault("paths", []).append(ws.request.path)
        if state["send_nothing"]:
            await asyncio.sleep(5)
            return
        for i in range(state["messages_per_connection"]):
            await ws.send(json.dumps(kline_payload(close=90_000 + index * 10 + i)))
            await asyncio.sleep(0.01)
        try:
            if index == 1 and state.get("close_first", True):
                # Koneksi pertama ditutup paksa -> klien wajib menyambung ulang.
                await ws.close()
                return
            async for raw in ws:
                state["received"].append(json.loads(raw))
        except Exception:
            pass

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", state
    finally:
        server.close()
        await server.wait_closed()


def _client(base_url: str, **kwargs: Any) -> tuple[KlineStreamClient, list[Any], Metrics]:
    events: list[Any] = []

    async def on_kline(event: Any) -> None:
        events.append(event)

    metrics = Metrics()
    client = KlineStreamClient(
        ["BTCUSDT", "ETHUSDT"],
        "15m",
        on_kline=on_kline,
        base_url=base_url,
        metrics=metrics,
        **kwargs,
    )
    return client, events, metrics


def test_url_stream_gabungan() -> None:
    client, _, _ = _client("wss://fstream.binance.com")
    assert client.url == (
        "wss://fstream.binance.com/stream?streams=btcusdt@kline_15m/ethusdt@kline_15m"
    )
    assert kline_stream_name("BTCUSDT", "15m") == "btcusdt@kline_15m"


def test_tanpa_simbol_ditolak() -> None:
    client = KlineStreamClient([], "15m", on_kline=lambda e: None)
    with pytest.raises(ValueError, match="tidak ada stream"):
        _ = client.url


@pytest.mark.asyncio
async def test_menerima_event_kline(ws_server: Any) -> None:
    base_url, state = ws_server
    client, events, metrics = _client(base_url, idle_timeout_sec=2)
    task = asyncio.create_task(client.run())
    for _ in range(100):
        if len(events) >= 2:
            break
        await asyncio.sleep(0.02)
    client.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(events) >= 2
    first = events[0]
    assert first.symbol == "BTCUSDT"
    assert first.interval == "15m"
    assert first.is_closed is True
    assert first.close == 90_010.0
    assert first.volume == 123.45
    snap = metrics.snapshot()
    assert snap.ws_messages >= 2
    assert snap.ws_rx_bytes > 0
    assert snap.ws_latency_ms >= 0
    assert state["connections"] >= 1


@pytest.mark.asyncio
async def test_reconnect_setelah_koneksi_ditutup(ws_server: Any) -> None:
    base_url, state = ws_server
    client, events, metrics = _client(base_url, idle_timeout_sec=2)
    task = asyncio.create_task(client.run())
    for _ in range(150):
        if state["connections"] >= 2 and len(events) >= 4:
            break
        await asyncio.sleep(0.02)
    client.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["connections"] >= 2, "klien harus menyambung ulang"
    assert metrics.snapshot().ws_reconnects >= 1


@pytest.mark.asyncio
async def test_idle_watchdog_memaksa_reconnect(ws_server: Any) -> None:
    base_url, state = ws_server
    state["send_nothing"] = True
    client, _, metrics = _client(base_url, idle_timeout_sec=0.15, max_backoff_sec=0.1)
    task = asyncio.create_task(client.run())
    for _ in range(150):
        if state["connections"] >= 2:
            break
        await asyncio.sleep(0.02)
    client.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["connections"] >= 2
    assert metrics.snapshot().ws_reconnects >= 1


@pytest.mark.asyncio
async def test_rotasi_koneksi_sebelum_24_jam(ws_server: Any) -> None:
    base_url, state = ws_server
    client, _, _ = _client(base_url, idle_timeout_sec=2, rotate_after_sec=0.1)
    task = asyncio.create_task(client.run())
    for _ in range(150):
        if state["connections"] >= 2:
            break
        await asyncio.sleep(0.02)
    client.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["connections"] >= 2


@pytest.mark.asyncio
async def test_set_symbols_mengirim_subscribe(ws_server: Any) -> None:
    base_url, state = ws_server
    state["close_first"] = False  # koneksi harus tetap terbuka agar SUBSCRIBE terkirim
    client, _, _ = _client(base_url, idle_timeout_sec=2)
    task = asyncio.create_task(client.run())
    for _ in range(100):
        if state["connections"] >= 1:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    await client.set_symbols(["BTCUSDT", "SOLUSDT"])
    for _ in range(100):
        if state["received"]:
            break
        await asyncio.sleep(0.02)
    client.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.symbols == ["BTCUSDT", "SOLUSDT"]
    commands = {msg.get("method") for msg in state["received"]}
    assert "SUBSCRIBE" in commands
    assert "UNSUBSCRIBE" in commands
    subscribe = next(m for m in state["received"] if m.get("method") == "SUBSCRIBE")
    assert subscribe["params"] == ["solusdt@kline_15m"]


@pytest.mark.asyncio
async def test_pesan_bukan_kline_diabaikan(ws_server: Any) -> None:
    """Stream campuran: event non-kline harus diabaikan tanpa error."""
    client, events, _ = _client("ws://127.0.0.1:1", idle_timeout_sec=1)
    await client._handle_raw(json.dumps({"result": None, "id": 1}))
    await client._handle_raw(json.dumps({"stream": "x", "data": {"e": "aggTrade", "s": "BTCUSDT"}}))
    await client._handle_raw("bukan json")
    await client._handle_raw(
        json.dumps({"data": {"e": "kline", "k": {"t": 1}}})  # field wajib tidak lengkap
    )
    assert events == []
