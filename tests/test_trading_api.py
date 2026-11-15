"""Test endpoint order/akun terhadap server HTTP lokal (bukan Binance asli).

Server memvalidasi **signature HMAC**, kehadiran ``timestamp``/``recvWindow``,
nilai parameter order, dan mengembalikan bentuk respons sesuai dokumentasi.
Yang tidak bisa diuji di sini: perilaku API Binance sebenarnya (sandbox 451).
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from decimal import Decimal
from typing import Any

import pytest
from aiohttp import web

from volumatic_bot.binance.rest import BinanceREST
from volumatic_bot.binance.trading import (
    OrderRequest,
    TradingAPI,
    parse_order_result,
    side_for_entry,
    side_for_exit,
)

API_KEY = "kunci-uji-ORDER-123456"
API_SECRET = "rahasia-uji-ORDER-987654"


class Timers:
    def __init__(self) -> None:
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    async def sleep(self, duration: float) -> None:
        self.t += duration


def _server() -> Any:
    state: dict[str, Any] = {"requests": [], "listen_keys": 0, "fail_protection": False}
    loop_holder: dict[str, Any] = {}

    async def handler(request: web.Request) -> web.Response:
        query = request.query_string
        parts = [p for p in query.split("&") if not p.startswith("signature=")]
        expected = hmac.new(
            API_SECRET.encode(), "&".join(parts).encode(), hashlib.sha256
        ).hexdigest()
        signature_ok = request.query.get("signature") == expected
        has_stamp = "timestamp" in request.query and "recvWindow" in request.query
        key_ok = request.headers.get("X-MBX-APIKEY") == API_KEY
        state["requests"].append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "signed_ok": signature_ok and has_stamp and key_ok,
            }
        )
        if not (signature_ok and has_stamp and key_ok):
            return web.json_response({"code": -1022, "msg": "Signature invalid"}, status=400)

        path = request.path
        if path == "/fapi/v1/order":
            if request.method == "DELETE":
                return web.json_response({"orderId": request.query.get("orderId", "1")})
            order_type = request.query.get("type", "MARKET")
            if state["fail_protection"] and order_type in (
                "STOP_MARKET",
                "TAKE_PROFIT_MARKET",
            ):
                return web.json_response(
                    {"code": -2021, "msg": "Order would immediately trigger."}, status=400
                )
            qty = request.query.get("quantity", "0")
            status = "FILLED" if order_type == "MARKET" else "NEW"
            return web.json_response(
                {
                    "symbol": request.query.get("symbol"),
                    "orderId": 555,
                    "clientOrderId": request.query.get("newClientOrderId", "x"),
                    "side": request.query.get("side"),
                    "type": order_type,
                    "status": status,
                    "origQty": qty,
                    "executedQty": qty if status == "FILLED" else "0",
                    "avgPrice": "0.35" if status == "FILLED" else "0",
                    "stopPrice": request.query.get("stopPrice", "0"),
                    "reduceOnly": request.query.get("reduceOnly") == "true",
                    "closePosition": request.query.get("closePosition") == "true",
                }
            )
        if path == "/fapi/v1/openOrders":
            return web.json_response(
                [
                    {
                        "symbol": "DOGEUSDT",
                        "orderId": 900,
                        "clientOrderId": "vbot-sl",
                        "side": "SELL",
                        "type": "STOP_MARKET",
                        "status": "NEW",
                        "origQty": "0",
                        "executedQty": "0",
                        "avgPrice": "0",
                        "stopPrice": "0.3444",
                        "reduceOnly": False,
                        "closePosition": True,
                    }
                ]
            )
        if path == "/fapi/v1/leverage":
            return web.json_response(
                {"leverage": int(request.query["leverage"]), "symbol": "DOGEUSDT"}
            )
        if path == "/fapi/v1/marginType":
            return web.json_response({"code": 200, "msg": "success"})
        if path == "/fapi/v1/positionSide/dual":
            return web.json_response({"code": 200, "msg": "success"})
        if path == "/fapi/v1/listenKey":
            if request.method == "DELETE":
                state["listen_keys"] -= 1
                return web.json_response({"code": 200, "msg": "success"})
            if request.method == "PUT":
                return web.json_response({"listenKey": "kunci-stream-uji"})
            state["listen_keys"] += 1
            return web.json_response({"listenKey": "kunci-stream-uji"})
        if path == "/fapi/v3/account":
            return web.json_response(
                {
                    "totalWalletBalance": "50.0",
                    "assets": [{"asset": "USDT", "walletBalance": "50.0"}],
                }
            )
        if path == "/fapi/v3/positionRisk":
            return web.json_response(
                [
                    {
                        "symbol": "DOGEUSDT",
                        "positionAmt": "133",
                        "entryPrice": "0.35",
                        "unRealizedProfit": "1.2",
                        "leverage": "5",
                        "marginType": "isolated",
                    }
                ]
            )
        return web.json_response({"code": -1, "msg": "tidak dikenal"}, status=404)

    loop = __import__("asyncio").new_event_loop()
    ready = threading.Event()

    def serve() -> None:
        __import__("asyncio").set_event_loop(loop)
        app = web.Application()
        for path in (
            "/fapi/v1/order",
            "/fapi/v1/openOrders",
            "/fapi/v1/leverage",
            "/fapi/v1/marginType",
            "/fapi/v1/positionSide/dual",
            "/fapi/v1/listenKey",
            "/fapi/v3/account",
            "/fapi/v3/positionRisk",
        ):
            app.router.add_route("*", path, handler)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        loop_holder["url"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(timeout=10)
    return loop_holder["url"], state, loop


@pytest.fixture(scope="module")
def order_server() -> Any:
    url, state, loop = _server()
    try:
        yield url, state
    finally:
        loop.call_soon_threadsafe(loop.stop)


async def _api(base_url: str, timers: Timers | None = None) -> tuple[BinanceREST, TradingAPI]:
    timers = timers or Timers()
    rest = BinanceREST(
        base_url=base_url,
        api_key=API_KEY,
        api_secret=API_SECRET,
        sleep=timers.sleep,
        clock=timers.clock,
        jitter=lambda: 0.5,
    )
    await rest.start()
    return rest, TradingAPI(rest)


# ------------------------------------------------------------------- unit murni
def test_sisi_order() -> None:
    assert side_for_entry("long") == "BUY"
    assert side_for_entry("short") == "SELL"
    assert side_for_exit("long") == "SELL"
    assert side_for_exit("short") == "BUY"
    with pytest.raises(ValueError, match="arah"):
        side_for_entry("naik")


def test_request_order_hanya_mengirim_field_relevan() -> None:
    order = OrderRequest(
        symbol="DOGEUSDT",
        side="BUY",
        type="LIMIT",
        quantity=Decimal("133"),
        price=Decimal("0.3500"),
        time_in_force="GTC",
        client_order_id="vbot-entry-1",
        purpose="entry",
    )
    params = order.to_params()
    assert params["quantity"] == "133"
    assert params["price"] == "0.35"  # nol di belakang dibuang
    assert "stopPrice" not in params
    assert params["timeInForce"] == "GTC"
    assert params["workingType"] == "CONTRACT_PRICE"
    assert "reduceOnly" not in params and "closePosition" not in params
    assert "LIMIT BUY DOGEUSDT" in order.describe()


def test_request_order_sl_memakai_closeposition() -> None:
    order = OrderRequest(
        symbol="DOGEUSDT",
        side="SELL",
        type="STOP_MARKET",
        stop_price=Decimal("0.3444"),
        close_position=True,
        purpose="stop_loss",
    )
    params = order.to_params()
    assert params["closePosition"] == "true"
    assert "quantity" not in params  # closePosition tidak boleh dikirim qty
    assert params["stopPrice"] == "0.3444"
    assert "reduceOnly" not in params  # Binance menolak kombinasi ini


def test_parse_order_result() -> None:
    hasil = parse_order_result(
        {
            "symbol": "DOGEUSDT",
            "orderId": 7,
            "clientOrderId": "vbot-x",
            "side": "BUY",
            "type": "MARKET",
            "status": "FILLED",
            "origQty": "133",
            "executedQty": "133",
            "avgPrice": "0.35",
            "stopPrice": "0",
            "reduceOnly": False,
            "closePosition": False,
        },
        purpose="entry",
    )
    assert hasil.filled and hasil.active is False
    assert hasil.remaining_qty == 0
    assert "entry: MARKET BUY" in hasil.describe()


# --------------------------------------------------------------- uji HTTP nyata
@pytest.mark.asyncio
async def test_place_order_bertanda_tangan_valid(order_server: Any) -> None:
    base_url, state = order_server
    rest, api = await _api(base_url)
    try:
        hasil = await api.place_order(
            OrderRequest(
                symbol="DOGEUSDT",
                side="BUY",
                type="MARKET",
                quantity=Decimal("133"),
                client_order_id="vbot-entry-1",
                purpose="entry",
            )
        )
        assert hasil.filled
        assert hasil.order_id == 555
        terakhir = state["requests"][-1]
        assert terakhir["path"] == "/fapi/v1/order"
        assert terakhir["signed_ok"] is True
        assert terakhir["query"]["newClientOrderId"] == "vbot-entry-1"
    finally:
        await rest.close()


@pytest.mark.asyncio
async def test_sl_ditolak_bursa_menghasilkan_error_jelas(order_server: Any) -> None:
    base_url, state = order_server
    rest, api = await _api(base_url)
    state["fail_protection"] = True
    try:
        with pytest.raises(Exception) as info:
            await api.place_order(
                OrderRequest(
                    symbol="DOGEUSDT",
                    side="SELL",
                    type="STOP_MARKET",
                    stop_price=Decimal("0.3444"),
                    close_position=True,
                    purpose="stop_loss",
                )
            )
        assert "would immediately trigger" in str(info.value).lower()
    finally:
        state["fail_protection"] = False
        await rest.close()


@pytest.mark.asyncio
async def test_set_leverage_margin_dan_position_mode(order_server: Any) -> None:
    base_url, state = order_server
    rest, api = await _api(base_url)
    try:
        lev = await api.set_leverage("dogeusdt", 5)
        assert lev["leverage"] == 5
        mt = await api.set_margin_type("DOGEUSDT", "ISOLATED")
        assert mt["code"] == 200
        pm = await api.set_position_mode(dual_side=False)
        assert pm["code"] == 200
        assert state["requests"][-1]["query"]["dualSidePosition"] == "false"
    finally:
        await rest.close()


@pytest.mark.asyncio
async def test_open_orders_dan_position_risk(order_server: Any) -> None:
    base_url, _ = order_server
    rest, api = await _api(base_url)
    try:
        orders = await api.open_orders("DOGEUSDT")
        assert orders[0]["stopPrice"] == "0.3444"
        posisi = await api.position_risk("DOGEUSDT")
        assert posisi[0]["positionAmt"] == "133"
        assert await api.balance_usdt() == pytest.approx(50.0)
    finally:
        await rest.close()


@pytest.mark.asyncio
async def test_cancel_butuh_identitas_order(order_server: Any) -> None:
    base_url, _ = order_server
    rest, api = await _api(base_url)
    try:
        with pytest.raises(ValueError, match="order_id"):
            await api.cancel_order("DOGEUSDT")
        hasil = await api.cancel_order("DOGEUSDT", order_id=555)
        assert hasil["orderId"] == "555"
    finally:
        await rest.close()


@pytest.mark.asyncio
async def test_listen_key_dibuat_diperpanjang_dihapus(order_server: Any) -> None:
    base_url, state = order_server
    rest, api = await _api(base_url)
    try:
        sebelum = state["listen_keys"]
        key = await api.create_listen_key()
        assert key == "kunci-stream-uji"
        await api.keepalive_listen_key(key)
        await api.close_listen_key(key)
        assert state["listen_keys"] == sebelum  # +1 dibuat, -1 dihapus
    finally:
        await rest.close()


@pytest.mark.asyncio
async def test_client_order_id_berprefix_dan_pendek() -> None:
    timers = Timers()
    rest = BinanceREST(
        base_url="http://127.0.0.1:1",
        api_key=API_KEY,
        api_secret=API_SECRET,
        sleep=timers.sleep,
        clock=timers.clock,
    )
    api = TradingAPI(rest)
    cid = api.client_order_id("entry", tag="DOGEUSDT")
    assert cid.startswith("vbot-entry-")
    assert len(cid) <= 36
