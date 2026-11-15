"""Test User Data Stream terhadap server ``websockets`` lokal.

Yang diuji: parsing event (ORDER_TRADE_UPDATE/ACCOUNT_UPDATE), keepalive
``listenKey``, penanganan ``listenKeyExpired`` (buat key baru + sambung ulang),
reconnect saat koneksi diputus, dan penolakan ``keepalive_sec`` di luar batas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from websockets.asyncio.server import ServerConnection, serve

from volumatic_bot.binance.user_stream import (
    ListenKeyManager,
    UserDataStream,
    parse_account_update,
    parse_order_update,
)


def order_event(
    *,
    symbol: str = "DOGEUSDT",
    status: str = "FILLED",
    execution: str = "TRADE",
    order_id: int = 1002,
    purpose_cid: str = "vbot-tp1",
    orig_qty: str = "66",
    realized: str = "0.55",
    client_order_id: str | None = None,
) -> dict[str, Any]:
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1_700_000_000_000,
        "T": 1_700_000_000_001,
        "o": {
            "s": symbol,
            "c": client_order_id or purpose_cid,
            "S": "SELL",
            "o": "TAKE_PROFIT_MARKET",
            "f": "GTC",
            "q": orig_qty,
            "p": "0",
            "ap": "0.3584",
            "sp": "0.3584",
            "x": execution,
            "X": status,
            "i": order_id,
            "l": orig_qty,
            "z": orig_qty,
            "L": "0.3584",
            "N": "USDT",
            "n": "0.0201",
            "T": 1_700_000_000_001,
            "t": 55,
            "b": "0",
            "a": "0",
            "m": False,
            "R": True,
            "wt": "CONTRACT_PRICE",
            "ot": "TAKE_PROFIT_MARKET",
            "ps": "LONG",
            "cp": False,
            "pP": False,
            "rp": realized,
        },
    }


def account_event() -> dict[str, Any]:
    return {
        "e": "ACCOUNT_UPDATE",
        "E": 1_700_000_000_500,
        "T": 1_700_000_000_501,
        "a": {
            "m": "ORDER",
            "B": [{"a": "USDT", "wb": "50.12", "cw": "50.12", "bc": "0.12"}],
            "P": [
                {
                    "s": "DOGEUSDT",
                    "pa": "67",
                    "ep": "0.35",
                    "up": "0.85",
                    "mt": "isolated",
                    "iw": "4.69",
                    "ps": "LONG",
                }
            ],
        },
    }


async def _selesaikan(task: asyncio.Task[Any]) -> None:
    """Tunggu task berhenti; boleh selesai normal atau karena dibatalkan."""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    assert task.done()


# --------------------------------------------------------------------- parsing
def test_parse_order_update() -> None:
    update = parse_order_update(order_event())
    assert update.symbol == "DOGEUSDT"
    assert update.client_order_id == "vbot-tp1"
    assert update.status == "FILLED"
    assert update.filled is True
    assert update.is_trade is True
    assert update.orig_qty == Decimal("66")
    assert update.avg_price == Decimal("0.3584")
    assert update.realized_pnl == Decimal("0.55")
    assert update.reduce_only is True
    assert update.is_liquidation is False


def test_parse_order_update_likuidasi_dikenali() -> None:
    update = parse_order_update(order_event(client_order_id="autoclose-123"))
    assert update.is_liquidation is True


def test_parse_order_update_payload_salah() -> None:
    with pytest.raises(ValueError, match="ORDER_TRADE_UPDATE"):
        parse_order_update({"e": "ACCOUNT_UPDATE"})


def test_parse_account_update() -> None:
    akun = parse_account_update(account_event())
    assert akun.reason == "ORDER"
    assert akun.balances["USDT"] == Decimal("50.12")
    assert akun.positions[0]["pa"] == "67"


def test_parse_account_update_payload_salah() -> None:
    with pytest.raises(ValueError, match="ACCOUNT_UPDATE"):
        parse_account_update({"e": "ORDER_TRADE_UPDATE"})


# ------------------------------------------------------------------- keepalive
class FakeAPI:
    def __init__(self) -> None:
        self.creates = 0
        self.extends = 0
        self.deleted: list[str] = []
        self.fail_extend = False

    async def create_listen_key(self) -> str:
        self.creates += 1
        return f"kunci-{self.creates}"

    async def keepalive_listen_key(self, key: str) -> None:
        if self.fail_extend:
            raise RuntimeError("-1125 listenKey tidak ada")
        self.extends += 1

    async def close_listen_key(self, key: str) -> None:
        self.deleted.append(key)


@pytest.mark.asyncio
async def test_manage_key_membuat_dan_memperpanjang() -> None:
    api = FakeAPI()
    mgr = ListenKeyManager(api, keepalive_sec=600)  # type: ignore[arg-type]
    key = await mgr.ensure()
    assert key == "kunci-1"
    assert await mgr.extend() is True
    assert api.extends == 1
    await mgr.close()
    assert api.deleted == ["kunci-1"]
    assert mgr.key is None


@pytest.mark.asyncio
async def test_manage_key_membuat_ulang_saat_kedaluwarsa() -> None:
    api = FakeAPI()
    mgr = ListenKeyManager(api, keepalive_sec=600)  # type: ignore[arg-type]
    await mgr.ensure()
    api.fail_extend = True
    assert await mgr.extend() is False
    assert mgr.key == "kunci-2"
    assert api.creates == 2


@pytest.mark.parametrize("keepalive", [30, 3600])
def test_keepalive_di_luar_batas_ditolak(keepalive: float) -> None:
    with pytest.raises(ValueError, match="keepalive_sec"):
        ListenKeyManager(FakeAPI(), keepalive_sec=keepalive)  # type: ignore[arg-type]


# ---------------------------------------------------------------- stream lokal
@pytest_asyncio.fixture
async def user_ws_server() -> Any:
    state: dict[str, Any] = {"connections": 0, "received": [], "kirim": []}

    async def handler(ws: ServerConnection) -> None:
        state["connections"] += 1
        index = state["connections"]
        state.setdefault("paths", []).append(ws.request.path)
        for payload in state["kirim"]:
            await ws.send(json.dumps(payload))
            await asyncio.sleep(0.01)
        if index == 1 and state.get("putus_setelah_kirim", True):
            await ws.close()
            return
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
async def test_stream_menerima_order_dan_account(user_ws_server: Any) -> None:
    base_url, state = user_ws_server
    state["kirim"] = [order_event(), account_event()]

    peristiwa: list[Any] = []
    akun: list[Any] = []

    async def on_order(update: Any) -> None:
        peristiwa.append(update)

    async def on_account(update: Any) -> None:
        akun.append(update)

    api = FakeAPI()
    mgr = ListenKeyManager(api, keepalive_sec=600)  # type: ignore[arg-type]
    stream = UserDataStream(
        mgr,
        base_url=base_url,
        on_order=on_order,
        on_account=on_account,
        rotate_after_sec=60,
    )
    task = asyncio.create_task(stream.run())
    for _ in range(400):
        if peristiwa and akun and state["connections"] >= 2:
            break
        await asyncio.sleep(0.01)
    stream.stop()
    task.cancel()
    await _selesaikan(task)

    assert peristiwa and peristiwa[0].symbol == "DOGEUSDT"
    assert akun and akun[0].balances["USDT"] == Decimal("50.12")
    assert state["paths"][0] == "/ws/kunci-1"
    assert state["connections"] >= 2, "klien tidak menyambung ulang setelah diputus"
    assert stream.reconnects >= 1


@pytest.mark.asyncio
async def test_stream_membuat_key_baru_saat_expired(user_ws_server: Any) -> None:
    base_url, state = user_ws_server
    state["putus_setelah_kirim"] = False
    state["kirim"] = [{"e": "listenKeyExpired", "E": 1_700_000_000_000}]

    api = FakeAPI()
    mgr = ListenKeyManager(api, keepalive_sec=600)  # type: ignore[arg-type]
    stream = UserDataStream(mgr, base_url=base_url, rotate_after_sec=60)
    task = asyncio.create_task(stream.run())
    for _ in range(300):
        if stream.expired_events >= 1:
            break
        await asyncio.sleep(0.01)
    stream.stop()
    task.cancel()
    await _selesaikan(task)
    assert stream.expired_events == 1
    assert api.creates >= 2  # key awal + key baru setelah expired


@pytest.mark.asyncio
async def test_stream_mengabaikan_pesan_bukan_json(user_ws_server: Any) -> None:
    base_url, state = user_ws_server
    state["putus_setelah_kirim"] = False
    raw_masuk: list[Any] = []

    async def on_raw(payload: dict[str, Any]) -> None:
        raw_masuk.append(payload.get("e"))

    api = FakeAPI()
    mgr = ListenKeyManager(api, keepalive_sec=600)  # type: ignore[arg-type]
    stream = UserDataStream(mgr, base_url=base_url, on_raw=on_raw, rotate_after_sec=60)
    await stream.keys.ensure()
    await stream._handle_raw("bukan json")
    await stream._handle_raw(json.dumps({"e": "MARGIN_CALL"}))
    assert raw_masuk == ["MARGIN_CALL"]


def test_url_butuh_listen_key() -> None:
    mgr = ListenKeyManager(FakeAPI(), keepalive_sec=600)  # type: ignore[arg-type]
    stream = UserDataStream(mgr)
    with pytest.raises(ValueError, match="listenKey"):
        _ = stream.url()
