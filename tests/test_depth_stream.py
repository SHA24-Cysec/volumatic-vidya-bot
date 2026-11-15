"""Test stream depth parsial & sumber snapshot depth (REST cache / stream).

Semua jaringan tetap lokal: server ``websockets`` di 127.0.0.1 dan REST palsu,
karena ``fapi.binance.com`` diblokir 451 dari sandbox pengembangan.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
import pytest_asyncio
from websockets.asyncio.server import ServerConnection, serve

from volumatic_bot.binance.depth_stream import (
    VALID_DEPTH_LEVELS,
    DepthStreamClient,
    depth_stream_name,
)
from volumatic_bot.metrics import Metrics
from volumatic_bot.orderbook.depth import (
    DepthUnavailable,
    RestDepthSource,
    StreamDepthSource,
    fetch_snapshot_safe,
)
from volumatic_bot.orderbook.models import BookLevel, OrderBookSnapshot


def depth_payload(
    symbol: str = "BTCUSDT",
    *,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "stream": f"{symbol.lower()}@depth20@100ms",
        "data": {
            "e": "depthUpdate",
            "E": int(time.time() * 1000),
            "T": int(time.time() * 1000),
            "s": symbol,
            "U": 100,
            "u": 105,
            "pu": 99,
            "b": bids if bids is not None else [["90000.0", "1.5"], ["89999.0", "2.0"]],
            "a": asks if asks is not None else [["90001.0", "1.2"], ["90002.0", "3.0"]],
        },
    }


# ------------------------------------------------------------ nama stream
def test_nama_stream_depth() -> None:
    assert depth_stream_name("BTCUSDT", 20, 100) == "btcusdt@depth20@100ms"
    assert depth_stream_name("btcusdt", 5, 500) == "btcusdt@depth5@500ms"
    assert VALID_DEPTH_LEVELS == (5, 10, 20)


@pytest.mark.parametrize("levels", [1, 15, 50, 0])
def test_levels_tidak_valid_ditolak(levels: int) -> None:
    with pytest.raises(ValueError, match="levels"):
        depth_stream_name("BTCUSDT", levels, 100)


@pytest.mark.parametrize("speed", [10, 200, 1000])
def test_speed_tidak_valid_ditolak(speed: int) -> None:
    with pytest.raises(ValueError, match="speed_ms"):
        depth_stream_name("BTCUSDT", 20, speed)


def test_url_klien_depth() -> None:
    client = DepthStreamClient(["BTCUSDT", "ETHUSDT"], levels=10, speed_ms=250)
    assert client.url == (
        "wss://fstream.binancefuture.com/public/stream?streams="
        "btcusdt@depth10@250ms/ethusdt@depth10@250ms"
    )
    client._legacy_fallback = True
    assert client.url == (
        "wss://fstream.binancefuture.com/stream?streams=btcusdt@depth10@250ms/ethusdt@depth10@250ms"
    )


# ------------------------------------------------------------ server lokal
@pytest_asyncio.fixture
async def depth_ws_server() -> Any:
    state: dict[str, Any] = {"connections": 0, "received": [], "messages": 2}

    async def handler(ws: ServerConnection) -> None:
        state["connections"] += 1
        for i in range(state["messages"]):
            await ws.send(json.dumps(depth_payload(bids=[["90000.0", str(1 + i)]])))
            await asyncio.sleep(0.01)
        try:
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


@pytest.mark.asyncio
async def test_klien_depth_menerima_event(depth_ws_server: Any) -> None:
    base_url, _ = depth_ws_server
    got: list[dict[str, Any]] = []

    async def on_depth(data: dict[str, Any]) -> None:
        got.append(data)

    client = DepthStreamClient(
        ["BTCUSDT"],
        levels=20,
        speed_ms=100,
        on_depth=on_depth,
        base_url=base_url,
        metrics=Metrics(),
        idle_timeout_sec=2.0,
    )
    task = asyncio.create_task(client.run())
    for _ in range(200):
        if len(got) >= 2:
            break
        await asyncio.sleep(0.01)
    client.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(got) >= 2
    assert got[0]["e"] == "depthUpdate"
    assert got[0]["s"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_klien_depth_mengirim_unsubscribe_saat_simbol_dihapus(
    depth_ws_server: Any,
) -> None:
    base_url, state = depth_ws_server
    client = DepthStreamClient(
        ["BTCUSDT", "ETHUSDT"],
        base_url=base_url,
        idle_timeout_sec=2.0,
    )
    task = asyncio.create_task(client.run())
    for _ in range(200):
        if client.connected:
            break
        await asyncio.sleep(0.01)
    await client.set_symbols(["BTCUSDT"])
    client.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    unsub = [m for m in state["received"] if m.get("method") == "UNSUBSCRIBE"]
    assert unsub, "perintah UNSUBSCRIBE tidak terkirim"
    assert "ethusdt@depth20@100ms" in unsub[0]["params"]


@pytest.mark.asyncio
async def test_klien_depth_mengabaikan_pesan_bukan_depth() -> None:
    client = DepthStreamClient(["BTCUSDT"])
    dipanggil: list[str] = []

    async def on_depth(data: dict[str, Any]) -> None:
        dipanggil.append(str(data.get("e")))

    client._on_depth = on_depth
    await client.handle_data({"e": "kline", "s": "BTCUSDT"})
    await client.handle_data({"e": "depthUpdate", "s": "BTCUSDT"})
    assert dipanggil == ["depthUpdate"]


# --------------------------------------------------------- sumber REST cache
class FakeREST:
    """REST palsu yang mencatat permintaan depth."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.payload = payload or {
            "lastUpdateId": 7,
            "E": 0,
            "T": 0,
            "bids": [["100.0", "2.0"]],
            "asks": [["100.1", "3.0"]],
        }

    async def depth(self, symbol: str, *, limit: int = 20) -> dict[str, Any]:
        self.calls.append((symbol, limit))
        return self.payload


@pytest.mark.asyncio
async def test_rest_depth_memakai_cache_tanpa_dobel_permintaan() -> None:
    jam = [100.0]
    rest = FakeREST()
    source = RestDepthSource(rest=rest, limit=5, ttl_sec=3.0, clock=lambda: jam[0])  # type: ignore[arg-type]
    first = await source.snapshot("btcusdt")
    second = await source.snapshot("BTCUSDT")
    assert first is second
    assert rest.calls == [("BTCUSDT", 5)]
    assert source.requests == 1
    assert source.cache_hits == 1
    assert source.cached_symbols == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_rest_depth_mengambil_ulang_setelah_ttl() -> None:
    jam = [100.0]
    rest = FakeREST()
    source = RestDepthSource(rest=rest, clock=lambda: jam[0])  # type: ignore[arg-type]
    await source.snapshot("BTCUSDT")
    jam[0] += 4.0  # > ttl 3 detik
    await source.snapshot("BTCUSDT")
    assert len(rest.calls) == 2


@pytest.mark.asyncio
async def test_rest_depth_umur_snapshot_memakai_jam_dinding() -> None:
    """Regresi: ``fetched_at`` harus sebanding dengan ``now`` milik ``gate.evaluate``.

    Sebelum diperbaiki, sumber depth memakai ``time.monotonic`` (mis. 1794660538)
    sedangkan gate memakai ``time.time``, sehingga setiap sinyal ditolak dengan
    alasan "snapshot basi" yang tidak masuk akal.
    """
    rest = FakeREST()
    source = RestDepthSource(rest=rest)  # type: ignore[arg-type]  # clock default
    snap = await source.snapshot("BTCUSDT")
    umur = snap.age_sec(time.time())
    assert 0.0 <= umur < 5.0


@pytest.mark.asyncio
async def test_stream_depth_umur_snapshot_memakai_jam_dinding() -> None:
    client = FakeDepthClient()
    source = StreamDepthSource(client=client)  # type: ignore[arg-type]
    await client._on_depth(depth_payload())
    snap = await source.snapshot("BTCUSDT")
    assert 0.0 <= snap.age_sec(time.time()) < 5.0


@pytest.mark.asyncio
async def test_rest_depth_invalidate() -> None:
    jam = [100.0]
    rest = FakeREST()
    source = RestDepthSource(rest=rest, clock=lambda: jam[0])  # type: ignore[arg-type]
    await source.snapshot("BTCUSDT")
    source.invalidate("BTCUSDT")
    assert source.cached_symbols == []
    await source.snapshot("BTCUSDT")
    assert len(rest.calls) == 2


def test_rest_depth_ttl_negatif_ditolak() -> None:
    with pytest.raises(ValueError, match="ttl_sec"):
        RestDepthSource(rest=FakeREST(), ttl_sec=-1.0)  # type: ignore[arg-type]


# ------------------------------------------------------ sumber stream depth
class FakeDepthClient:
    """Klien depth palsu (tanpa jaringan) untuk StreamDepthSource."""

    def __init__(self) -> None:
        self.symbols: list[str] = []
        self._on_depth: Any = None
        self.set_calls: list[list[str]] = []

    async def set_symbols(self, symbols: list[str]) -> None:
        self.symbols = sorted({s.upper() for s in symbols})
        self.set_calls.append(list(self.symbols))

    async def run(self) -> None:
        await asyncio.sleep(3600)

    def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_stream_source_menyimpan_event_dan_menolak_basi() -> None:
    jam = [500.0]
    client = FakeDepthClient()
    source = StreamDepthSource(client=client, max_age_sec=3.0, clock=lambda: jam[0])  # type: ignore[arg-type]
    await client._on_depth(depth_payload())
    snap = await source.snapshot("btcusdt")
    assert snap.symbol == "BTCUSDT"
    assert snap.source == "stream"
    assert snap.best_bid == 90000.0

    jam[0] += 10.0
    with pytest.raises(DepthUnavailable, match="basi"):
        await source.snapshot("BTCUSDT")


@pytest.mark.asyncio
async def test_stream_source_tanpa_snapshot() -> None:
    source = StreamDepthSource(client=FakeDepthClient(), clock=lambda: 0.0)  # type: ignore[arg-type]
    with pytest.raises(DepthUnavailable, match="belum ada snapshot"):
        await source.snapshot("ETHUSDT")


@pytest.mark.asyncio
async def test_stream_source_mengabaikan_payload_rusak() -> None:
    client = FakeDepthClient()
    source = StreamDepthSource(client=client, clock=lambda: 0.0)  # type: ignore[arg-type]
    await client._on_depth({"e": "kline"})
    assert client._on_depth is not None
    with pytest.raises(DepthUnavailable):
        await source.snapshot("BTCUSDT")


@pytest.mark.asyncio
async def test_stream_source_subscribe_dan_unsubscribe() -> None:
    client = FakeDepthClient()
    source = StreamDepthSource(client=client, clock=lambda: 0.0)  # type: ignore[arg-type]
    await source.subscribe("btcusdt")
    assert client.symbols == ["BTCUSDT"]
    await source.subscribe("ETHUSDT")
    assert client.symbols == ["BTCUSDT", "ETHUSDT"]
    await client._on_depth(depth_payload("ETHUSDT"))
    await source.unsubscribe("ethusdt")
    assert client.symbols == ["BTCUSDT"]
    with pytest.raises(DepthUnavailable):
        await source.snapshot("ETHUSDT")


@pytest.mark.asyncio
async def test_wait_first_event_sukses() -> None:
    client = FakeDepthClient()
    source = StreamDepthSource(client=client, clock=time.monotonic)  # type: ignore[arg-type]

    async def kirim() -> None:
        await asyncio.sleep(0.05)
        await client._on_depth(depth_payload())

    task = asyncio.create_task(kirim())
    await source.wait_first_event("BTCUSDT", max_wait_sec=1.0, poll=0.01)
    await task
    assert (await source.snapshot("BTCUSDT")).symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_wait_first_event_timeout() -> None:
    source = StreamDepthSource(client=FakeDepthClient(), clock=time.monotonic)  # type: ignore[arg-type]
    with pytest.raises(DepthUnavailable, match="belum ada event"):
        await source.wait_first_event("BTCUSDT", max_wait_sec=0.15, poll=0.01)


def test_stream_source_build_membuat_klien() -> None:
    source = StreamDepthSource.build(levels=10, speed_ms=250, base_url="wss://contoh")
    assert source.client.levels == 10
    assert source.client.speed_ms == 250
    # Tanpa simbol, URL belum bisa dijahit; nama stream tetap bisa diuji.
    assert source.client.stream_name("BTCUSDT") == "btcusdt@depth10@250ms"
    with pytest.raises(ValueError, match="tidak ada stream"):
        _ = source.client.url


@pytest.mark.asyncio
async def test_stream_source_start_stop() -> None:
    source = StreamDepthSource(client=FakeDepthClient(), clock=lambda: 0.0)  # type: ignore[arg-type]
    await source.start()
    assert source._task is not None
    await source.stop()
    assert source._task is None


# ------------------------------------------------------------ helper aman
@pytest.mark.asyncio
async def test_fetch_snapshot_safe_meneruskan_alasan() -> None:
    class Gagal:
        async def snapshot(self, symbol: str) -> OrderBookSnapshot:
            raise DepthUnavailable("tidak ada data")

    snap, reason = await fetch_snapshot_safe(Gagal(), "BTCUSDT")
    assert snap is None
    assert reason == "tidak ada data"


@pytest.mark.asyncio
async def test_fetch_snapshot_safe_menangani_galat_jaringan() -> None:
    import aiohttp

    class Jaringan:
        async def snapshot(self, symbol: str) -> OrderBookSnapshot:
            raise aiohttp.ClientConnectionError("putus")

    snap, reason = await fetch_snapshot_safe(Jaringan(), "BTCUSDT")
    assert snap is None
    assert "kegagalan jaringan" in (reason or "")


@pytest.mark.asyncio
async def test_fetch_snapshot_safe_sukses() -> None:
    class Sukses:
        async def snapshot(self, symbol: str) -> OrderBookSnapshot:
            return OrderBookSnapshot(
                symbol=symbol.upper(),
                bids=(BookLevel(100.0, 1.0),),
                asks=(BookLevel(100.1, 1.0),),
                last_update_id=1,
                event_time_ms=0,
                source="rest",
                fetched_at=0.0,
            )

    snap, reason = await fetch_snapshot_safe(Sukses(), "btcusdt")
    assert reason is None
    assert snap is not None and snap.symbol == "BTCUSDT"
