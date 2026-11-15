"""Test klien REST terhadap server HTTP lokal (aiohttp) — tanpa jaringan luar.

Ini bukan mock: server sungguhan dijalankan di 127.0.0.1, sehingga yang diuji
mencakup koneksi TCP, header HTTP, gzip, status code, dan retry sungguhan.
Yang tidak bisa diuji di sini: perilaku API Binance yang sebenarnya (sandbox
pengembangan diblokir HTTP 451).
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import time
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web

from volumatic_bot.binance.errors import BinanceAPIError, IpBannedError, RateLimitError
from volumatic_bot.binance.rest import BinanceREST, depth_request_weight, kline_request_weight

API_KEY = "kunci-uji-ABCDEFGH12345678"
API_SECRET = "rahasia-uji-zyxwvu9876543210"


class Timers:
    """Jam dan sleep palsu: sleep menaikkan jam, jadi rate limiter bisa diuji."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.t += duration


@pytest_asyncio.fixture
async def binance_server() -> Any:
    state: dict[str, Any] = {"hits": 0, "flaky": 0, "paths": []}

    async def ping(_: web.Request) -> web.Response:
        state["hits"] += 1
        return web.json_response({})

    async def server_time(_: web.Request) -> web.Response:
        return web.json_response({"serverTime": int(time.time() * 1000) + 5000})

    async def klines(request: web.Request) -> web.Response:
        state["hits"] += 1
        state["paths"].append(request.path_qs)
        limit = int(request.query.get("limit", "500"))
        start = int(request.query.get("startTime", "0"))
        rows = [
            [
                start + i * 900_000,
                "100.0",
                "101.0",
                "99.0",
                "100.5",
                "10.0",
                start + i * 900_000 + 899_999,
                "1000.0",
                5,
                "4.0",
                "400.0",
                "0",
            ]
            for i in range(min(limit, 3))
        ]
        return web.json_response(rows, headers={"X-MBX-USED-WEIGHT-1M": "137"})

    async def depth(request: web.Request) -> web.Response:
        state["hits"] += 1
        state["paths"].append(request.path_qs)
        limit = int(request.query.get("limit", "500"))
        levels = 3 if limit >= 10 else 1
        return web.json_response(
            {
                "lastUpdateId": 123456,
                "E": 1_700_000_000_000,
                "T": 1_700_000_000_000,
                "bids": [[str(100.0 - i * 0.1), "2.0"] for i in range(levels)],
                "asks": [[str(100.1 + i * 0.1), "3.0"] for i in range(levels)],
            }
        )

    async def exchange_info(_: web.Request) -> web.Response:
        payload = json.loads((_DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
        return web.json_response(payload)

    async def bad_param(_: web.Request) -> web.Response:
        state["hits"] += 1
        return web.json_response({"code": -1121, "msg": "Invalid symbol."}, status=400)

    async def rate_limited(request: web.Request) -> web.Response:
        state["hits"] += 1
        if state["hits"] < 3:
            return web.json_response(
                {"code": -1003, "msg": "Too many requests."},
                status=429,
                headers={"Retry-After": "0.2"},
            )
        return web.json_response({"ok": True})

    async def banned(_: web.Request) -> web.Response:
        state["hits"] += 1
        return web.json_response({"code": -1003, "msg": "Way too many requests"}, status=418)

    async def flaky(_: web.Request) -> web.Response:
        state["hits"] += 1
        state["flaky"] += 1
        if state["flaky"] < 3:
            return web.json_response({"msg": "internal"}, status=500)
        return web.json_response({"ok": True})

    async def gzip_payload(_: web.Request) -> web.Response:
        raw = json.dumps({"data": "x" * 2000}).encode()
        return web.Response(
            body=gzip.compress(raw),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )

    async def signed(request: web.Request) -> web.Response:
        query = request.query_string
        parts = [p for p in query.split("&") if not p.startswith("signature=")]
        payload = "&".join(parts)
        expected = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        provided = request.query.get("signature")
        ok = (
            provided == expected
            and request.headers.get("X-MBX-APIKEY") == API_KEY
            and "timestamp" in request.query
            and "recvWindow" in request.query
        )
        return web.json_response({"signature_ok": ok}, status=200 if ok else 400)

    app = web.Application()
    app.router.add_get("/fapi/v1/ping", ping)
    app.router.add_get("/fapi/v1/time", server_time)
    app.router.add_get("/fapi/v1/klines", klines)
    app.router.add_get("/fapi/v1/exchangeInfo", exchange_info)
    app.router.add_get("/fapi/v1/depth", depth)
    app.router.add_get("/uji/bad", bad_param)
    app.router.add_get("/uji/429", rate_limited)
    app.router.add_get("/uji/418", banned)
    app.router.add_get("/uji/flaky", flaky)
    app.router.add_get("/uji/gzip", gzip_payload)
    app.router.add_get("/uji/signed", signed)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr,index]
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        await runner.cleanup()


_DATA = __import__("pathlib").Path(__file__).parent / "data"


def _client(base_url: str, timers: Timers | None = None, **kwargs: Any) -> BinanceREST:
    timers = timers or Timers()
    return BinanceREST(
        base_url=base_url,
        sleep=timers.sleep,
        clock=timers.clock,
        jitter=lambda: 0.5,
        **kwargs,
    )


# ---------------------------------------------------------------- bobot & dasar
@pytest.mark.parametrize(
    ("limit", "weight"),
    [(1, 1), (99, 1), (100, 2), (499, 2), (500, 5), (1000, 5), (1001, 10), (1500, 10)],
)
def test_bobot_klines_sesuai_dokumentasi(limit: int, weight: int) -> None:
    assert kline_request_weight(limit) == weight


@pytest.mark.asyncio
async def test_request_sukses_dan_metrik(binance_server: Any) -> None:
    base_url, _ = binance_server
    async with _client(base_url) as rest:
        assert await rest.ping() is True
        rows = await rest.klines("BTCUSDT", "15m", start_time=1_700_000_000_000, limit=3)
        assert len(rows) == 3
        assert rows[0][1] == "100.0"
        snap = rest.metrics.snapshot()
        assert snap.http_requests == 2
        # Bobot terbaca dari header server (137) dan dari perhitungan lokal.
        assert snap.used_weight == 137
        assert snap.http_rx_bytes > 0


@pytest.mark.asyncio
async def test_klines_memvalidasi_limit(binance_server: Any) -> None:
    base_url, _ = binance_server
    async with _client(base_url) as rest:
        with pytest.raises(ValueError, match=r"1\.\.1500"):
            await rest.klines("BTCUSDT", "15m", limit=2000)


@pytest.mark.asyncio
async def test_kompresi_gzip_didekompresi(binance_server: Any) -> None:
    base_url, _ = binance_server
    async with _client(base_url) as rest:
        payload = await rest._request("GET", "/uji/gzip", weight=1)
        assert payload["data"] == "x" * 2000
        # Metrik memakai ukuran di kabel (terkompresi), bukan ukuran hasil dekompresi.
        assert rest.metrics.snapshot().http_rx_bytes < 2000


@pytest.mark.parametrize(
    ("limit", "weight"),
    [(5, 2), (10, 2), (20, 2), (50, 2), (100, 5), (500, 10), (1000, 20)],
)
def test_bobot_depth_sesuai_dokumentasi(limit: int, weight: int) -> None:
    """Tabel bobot ``GET /fapi/v1/depth`` (dokumentasi snapshot 2026-01-18)."""
    assert depth_request_weight(limit) == weight


@pytest.mark.parametrize("limit", [1, 15, 200, 2000])
def test_bobot_depth_limit_tidak_valid(limit: int) -> None:
    with pytest.raises(ValueError, match="limit depth tidak valid"):
        depth_request_weight(limit)


@pytest.mark.asyncio
async def test_depth_dipanggil_dengan_limit_dan_simbol_benar(binance_server: Any) -> None:
    base_url, state = binance_server
    async with _client(base_url) as rest:
        payload = await rest.depth("btcusdt", limit=10)
        assert payload["lastUpdateId"] == 123456
        assert len(payload["bids"]) == 3
    assert state["paths"][-1] == "/fapi/v1/depth?symbol=BTCUSDT&limit=10"


@pytest.mark.asyncio
async def test_depth_menolak_limit_di_luar_tabel(binance_server: Any) -> None:
    base_url, _ = binance_server
    async with _client(base_url) as rest:
        with pytest.raises(ValueError, match="limit depth tidak valid"):
            await rest.depth("BTCUSDT", limit=200)


@pytest.mark.asyncio
async def test_depth_menolak_simbol_tidak_wajar(binance_server: Any) -> None:
    base_url, _ = binance_server
    async with _client(base_url) as rest:
        for buruk in ["BTC USDT", "BTC/USDT", "A", "BTCUSDT&limit=1000"]:
            with pytest.raises(ValueError, match="nama simbol tidak valid"):
                await rest.depth(buruk)


@pytest.mark.asyncio
async def test_exchange_info_terparse(binance_server: Any) -> None:
    base_url, _ = binance_server
    from volumatic_bot.binance.symbols import parse_exchange_info

    async with _client(base_url) as rest:
        info = parse_exchange_info(await rest.exchange_info())
        assert "BTCUSDT" in info.symbols


# ------------------------------------------------------------------- error
@pytest.mark.asyncio
async def test_error_4xx_tidak_diulang(binance_server: Any) -> None:
    base_url, state = binance_server
    async with _client(base_url) as rest:
        with pytest.raises(BinanceAPIError) as exc_info:
            await rest._request("GET", "/uji/bad", weight=1)
        assert exc_info.value.code == -1121
        assert exc_info.value.status == 400
        assert "Invalid symbol" in str(exc_info.value)
        assert state["hits"] == 1  # tidak ada retry


@pytest.mark.asyncio
async def test_429_diulang_menghormati_retry_after(binance_server: Any) -> None:
    base_url, _ = binance_server
    timers = Timers()
    async with _client(base_url, timers) as rest:
        payload = await rest._request("GET", "/uji/429", weight=1)
        assert payload == {"ok": True}
        assert timers.sleeps[:2] == [0.2, 0.2]
        assert rest.metrics.snapshot().http_retries >= 2


@pytest.mark.asyncio
async def test_429_habis_retry_melempar_rate_limit(binance_server: Any) -> None:
    base_url, _ = binance_server
    async with _client(base_url, max_retries=1) as rest:
        # Server baru berhasil pada hit ke-3, sedangkan max_retries=1 -> gagal.
        with pytest.raises(RateLimitError):
            await rest._request("GET", "/uji/429", weight=1)


@pytest.mark.asyncio
async def test_418_tidak_diulang(binance_server: Any) -> None:
    base_url, state = binance_server
    async with _client(base_url) as rest:
        with pytest.raises(IpBannedError):
            await rest._request("GET", "/uji/418", weight=1)
        assert state["hits"] == 1


@pytest.mark.asyncio
async def test_500_diulang_dengan_backoff(binance_server: Any) -> None:
    base_url, _ = binance_server
    timers = Timers()
    async with _client(base_url, timers) as rest:
        payload = await rest._request("GET", "/uji/flaky", weight=1)
        assert payload == {"ok": True}
        assert len(timers.sleeps) >= 2
        # Backoff naik (0.25 lalu 0.5 dengan jitter tetap 0.5 -> 0.375, 0.75).
        assert timers.sleeps[1] > timers.sleeps[0]


# ------------------------------------------------------------------- signature
@pytest.mark.asyncio
async def test_request_bertanda_tangan_valid(binance_server: Any) -> None:
    base_url, _ = binance_server
    timers = Timers()
    async with _client(base_url, timers, api_key=API_KEY, api_secret=API_SECRET) as rest:
        payload = await rest._request(
            "GET", "/uji/signed", params={"symbol": "BTCUSDT"}, signed=True
        )
        assert payload["signature_ok"] is True


@pytest.mark.asyncio
async def test_request_bertanda_tangan_tanpa_secret_ditolak(binance_server: Any) -> None:
    base_url, _ = binance_server
    async with _client(base_url) as rest:
        with pytest.raises(BinanceAPIError, match="API secret"):
            await rest._request("GET", "/uji/signed", signed=True)


@pytest.mark.asyncio
async def test_api_key_tidak_bocor_di_error(binance_server: Any) -> None:
    base_url, _ = binance_server

    base_url, _ = binance_server
    async with _client(base_url, api_key=API_KEY, api_secret=API_SECRET) as rest:
        with pytest.raises(BinanceAPIError) as exc_info:
            await rest._request(
                "GET", "/uji/bad", params={"signature": "deadbeefcafe"}, signed=True
            )
        teks = str(exc_info.value)
        assert "deadbeefcafe" not in teks
        assert API_SECRET not in teks


# ------------------------------------------------------------------- lain-lain
@pytest.mark.asyncio
async def test_sync_time_offset(binance_server: Any) -> None:
    base_url, _ = binance_server
    async with _client(base_url) as rest:
        offset = await rest.sync_time()
        assert 4000 < offset < 6000  # server +5 detik
        assert rest.need_time_sync(interval_sec=1e9) is False


@pytest.mark.asyncio
async def test_rate_limiter_menahan_request_melebihi_anggaran(binance_server: Any) -> None:
    base_url, _ = binance_server
    timers = Timers()
    async with _client(base_url, timers, weight_budget_per_min=10) as rest:
        # 3 x klines limit 500 = 3 x bobot 5 = 15 > anggaran 10 per menit.
        for _ in range(3):
            await rest.klines("BTCUSDT", "15m", limit=500)
        assert timers.sleeps, "seharusnya menunggu jendela bobot"
        assert all(0.05 <= d <= 5.0 for d in timers.sleeps)


@pytest.mark.asyncio
async def test_exception_jaringan_dibungkus(binance_server: Any) -> None:
    """Port tertutup harus jadi BinanceNetworkError setelah retry, bukan crash."""
    timers = Timers()
    async with _client("http://127.0.0.1:9", timers, max_retries=1, timeout=1) as rest:
        from volumatic_bot.binance.errors import BinanceNetworkError

        with pytest.raises(BinanceNetworkError):
            await rest._request("GET", "/fapi/v1/ping", weight=1)
        assert rest.metrics.snapshot().http_errors >= 2


@pytest.mark.asyncio
async def test_close_idempoten(binance_server: Any) -> None:
    base_url, _ = binance_server
    rest = _client(base_url)
    await rest.start()
    await rest.close()
    await rest.close()  # tidak boleh error
