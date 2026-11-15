"""Klien REST async untuk Binance USDⓈ-M Futures.

Prinsip yang dipegang:

* **Hemat bandwidth**: satu :class:`aiohttp.ClientSession` dipakai ulang
  (keep-alive), kompresi HTTP aktif, dan endpoint publik tidak pernah dipanggil
  berulang-ulang tanpa perlu (lihat cache di ``data/cache.py``).
* **Hormati rate limit**: bobot tiap request dihitung di muka, nilainya dibaca
  ulang dari header ``X-MBX-USED-WEIGHT-1M``, dan anggaran per menit dijaga di
  bawah batas (``weight_budget_per_min``).
* **Backoff jujur**: 429 diulang dengan ``Retry-After`` (atau backoff
  eksponensial + jitter), 5xx/timeout diulang, sedangkan 418 (IP ban) dan 4xx
  lain **tidak** diulang — mengulang hanya memperburuk.
* **Tanpa kebocoran rahasia**: URL bertanda tangan tidak pernah masuk log atau
  pesan error (lihat ``errors.py`` dan ``logging_setup.py``).

Catatan dokumentasi (14 Nov 2026): ``GET /fapi/v1/klines`` punya bobot 1/2/5/10
menurut nilai ``limit`` (``>1000`` = 10), ``/fapi/v1/ticker/24hr`` bobot 1 (satu
simbol) / 40 (semua simbol), ``/fapi/v1/ticker/bookTicker`` bobot 2/5,
``/fapi/v1/exchangeInfo`` bobot 1. Limiter di sini memakai angka-angka itu.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlencode

import aiohttp

from volumatic_bot.binance.errors import (
    BinanceAPIError,
    BinanceNetworkError,
    IpBannedError,
    RateLimitError,
)
from volumatic_bot.binance.urls import Endpoint
from volumatic_bot.logging_setup import sanitize
from volumatic_bot.metrics import Metrics

__all__ = ["BinanceREST", "depth_request_weight", "kline_request_weight"]

_WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"


def kline_request_weight(limit: int) -> int:
    """Bobot ``GET /fapi/v1/klines`` menurut ``limit`` (dokumentasi 2026)."""
    if limit <= 0:
        return 1
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


#: Bobot ``GET /fapi/v1/depth`` menurut dokumentasi (snapshot 2026-01-18).
DEPTH_WEIGHTS: dict[int, int] = {5: 2, 10: 2, 20: 2, 50: 2, 100: 5, 500: 10, 1000: 20}


def depth_request_weight(limit: int) -> int:
    """Bobot endpoint order book berdasarkan ``limit`` (valid: 5,10,20,50,100,500,1000)."""
    try:
        return DEPTH_WEIGHTS[limit]
    except KeyError as exc:
        raise ValueError(
            f"limit depth tidak valid: {limit}. Pilihan: {sorted(DEPTH_WEIGHTS)}"
        ) from exc


class BinanceREST:
    """Klien REST tipis dengan pengelolaan bobot, retry, dan metrik."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        testnet: bool = True,
        api_key: str | None = None,
        api_secret: str | None = None,
        recv_window: int = 5000,
        timeout: float = 10.0,
        max_retries: int = 4,
        weight_budget_per_min: int = 1800,
        metrics: Metrics | None = None,
        session: aiohttp.ClientSession | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
        max_backoff: float = 30.0,
    ) -> None:
        if recv_window < 1000 or recv_window > 60_000:
            raise ValueError("recv_window harus di antara 1000 dan 60000 ms")
        self.base_url = (base_url or Endpoint.rest(testnet=testnet)).rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self.recv_window = recv_window
        self.timeout = timeout
        self.max_retries = max_retries
        self.weight_budget = weight_budget_per_min
        self.metrics = metrics or Metrics(weight_limit=max(weight_budget_per_min, 1))
        self._session = session
        self._own_session = session is None
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock
        self._jitter = jitter
        self._max_backoff = max_backoff
        self._weight_log: deque[tuple[float, int]] = deque()
        self._time_offset_ms: float = 0.0
        self._last_sync: float | None = None
        self._closed = False

    # ------------------------------------------------------------------ setup
    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None:
            # Catatan: parameter enable_cleanup_closed sudah tidak relevan di
            # Python 3.14 (aiohttp memunculkan DeprecationWarning), jadi tidak dipakai.
            connector = aiohttp.TCPConnector(limit=8, limit_per_host=8, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                # aiohttp otomatis mengirim Accept-Encoding: gzip/deflate dan
                # mendekompresi respons (hemat bandwidth).
                auto_decompress=True,
                headers={"User-Agent": "volumatic-vidya-bot/0.2"},
            )
            self._own_session = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._own_session and self._session is not None and not self._session.closed:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("session belum dibuat; panggil start() lebih dulu")
        return self._session

    # ------------------------------------------------------------ waktu server
    def now_ms(self) -> int:
        """Waktu sekarang menurut estimasi jam server Binance (milidetik)."""
        return int(self._wall_clock() * 1000 + self._time_offset_ms)

    @property
    def time_offset_ms(self) -> float:
        return self._time_offset_ms

    async def sync_time(self) -> float:
        """Selaraskan jam lokal dengan server Binance; kembalikan offset (ms)."""
        t0 = self._wall_clock()
        payload = await self._request("GET", "/fapi/v1/time", weight=1, retries=2)
        t1 = self._wall_clock()
        server_ms = payload.get("serverTime")
        if not isinstance(server_ms, (int, float)):
            raise BinanceAPIError("respons /fapi/v1/time tidak berisi serverTime")
        local_ms = (t0 + t1) / 2 * 1000
        self._time_offset_ms = float(server_ms) - local_ms
        self._last_sync = self._clock()
        return float(self._time_offset_ms)

    def need_time_sync(self, interval_sec: float = 1800.0) -> bool:
        if self._last_sync is None:
            return True
        return (self._clock() - self._last_sync) > interval_sec

    # ------------------------------------------------------------- rate limiter
    def _prune_weight(self, now: float) -> None:
        while self._weight_log and now - self._weight_log[0][0] >= 60.0:
            self._weight_log.popleft()

    def _used_weight_window(self) -> int:
        return sum(w for _, w in self._weight_log)

    async def _reserve_weight(self, weight: int) -> None:
        """Tunggu sampai bobot ``weight`` masih muat dalam anggaran per menit."""
        if weight <= 0:
            return
        while True:
            now = self._clock()
            self._prune_weight(now)
            used = self._used_weight_window()
            if used + weight <= self.weight_budget:
                self._weight_log.append((now, weight))
                return
            oldest = self._weight_log[0][0] if self._weight_log else now
            wait = max(0.05, min(60.0 - (now - oldest), 5.0))
            await self._sleep(wait)

    # ------------------------------------------------------------------ inti
    def _build_query(self, params: Mapping[str, Any] | None, *, signed: bool) -> str:
        items: dict[str, Any] = {k: v for k, v in (params or {}).items() if v is not None}
        secret: str | None = None
        if signed:
            secret = self._api_secret
            if secret is None or secret == "":
                raise BinanceAPIError("request bertanda tangan butuh API secret")
            items["timestamp"] = self.now_ms()
            items["recvWindow"] = self.recv_window
        query = urlencode(items, doseq=True)
        if secret is not None and signed:
            digest = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query = f"{query}&signature={digest}"
        return query

    def _headers(self, *, signed: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if signed or self._api_key:
            if not self._api_key:
                raise BinanceAPIError("request ini butuh API key")
            headers["X-MBX-APIKEY"] = self._api_key
        return headers

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        """Simbol Binance hanya huruf/angka (mis. ``BTCUSDT``); cegah injeksi query."""
        cleaned = symbol.strip().upper()
        if not cleaned.isalnum() or not 4 <= len(cleaned) <= 20:
            raise ValueError(f"nama simbol tidak valid: {symbol!r}")
        return cleaned

    def _backoff(self, attempt: int) -> float:
        base = float(min(self._max_backoff, 0.5 * (2.0**attempt)))
        factor = 0.75 + 0.5 * float(self._jitter())
        return float(base * factor)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        signed: bool = False,
        weight: int = 1,
        retries: int | None = None,
    ) -> Any:
        """Kirim satu request dengan retry & backoff. Mengembalikan JSON."""
        attempts = self.max_retries if retries is None else retries
        last_error: Exception | None = None
        for attempt in range(attempts + 1):
            await self._reserve_weight(weight)
            query = self._build_query(params, signed=signed)
            url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
            headers = self._headers(signed=signed)
            try:
                tx_bytes = len(url) + sum(len(k) + len(v) for k, v in headers.items())
                async with self.session.request(
                    method, url, headers=headers, allow_redirects=False
                ) as resp:
                    raw = await resp.read()
                    wire = resp.headers.get("Content-Length")
                    rx_bytes = int(wire) if wire and wire.isdigit() else len(raw)
                    used = resp.headers.get(_WEIGHT_HEADER)
                    if used and used.isdigit():
                        self.metrics.record_used_weight(int(used), limit=self.weight_budget)
                    self.metrics.record_http(
                        rx_bytes=rx_bytes, tx_bytes=tx_bytes, retry=attempt > 0
                    )
                    status = resp.status
                    if status == 200:
                        try:
                            return json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise BinanceAPIError(
                                f"respons bukan JSON valid ({len(raw)} byte)",
                                status=status,
                                endpoint=path,
                            ) from exc
                    payload: Any
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = {"msg": raw[:200].decode("utf-8", "replace")}
                    code = payload.get("code") if isinstance(payload, dict) else None
                    msg = payload.get("msg") if isinstance(payload, dict) else None
                    message = sanitize(str(msg or "tanpa pesan"))
                    if status == 418:
                        raise IpBannedError(message, status=status, code=code, endpoint=path)
                    if status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        raise RateLimitError(
                            message,
                            status=status,
                            code=code,
                            endpoint=path,
                            retry_after=float(retry_after) if retry_after else None,
                        )
                    if status >= 500 or status == 408:
                        raise BinanceNetworkError(message, status=status, code=code, endpoint=path)
                    raise BinanceAPIError(message, status=status, code=code, endpoint=path)
            except (BinanceNetworkError, RateLimitError, aiohttp.ClientError, TimeoutError) as exc:
                last_error = exc
                self.metrics.http_errors += 1
                if attempt >= attempts:
                    break
                delay = (
                    exc.retry_after
                    if isinstance(exc, RateLimitError) and exc.retry_after is not None
                    else self._backoff(attempt)
                )
                await self._sleep(max(0.05, delay))
                continue
        if isinstance(last_error, BinanceNetworkError | RateLimitError):
            raise last_error
        if last_error is None:  # pragma: no cover - tidak mungkin tercapai
            raise BinanceAPIError("request gagal tanpa error yang terekam", endpoint=path)
        raise BinanceNetworkError(
            f"gagal setelah {attempts + 1} percobaan: {type(last_error).__name__}: {last_error}",
            endpoint=path,
        )

    # ------------------------------------------------------------ publik tipis
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        signed: bool = False,
        weight: int = 1,
        retries: int | None = None,
    ) -> Any:
        """Akses publik ke inti request (dipakai modul order/akun).

        Tetap memakai penandatanganan, rate limit, retry, dan metrik yang sama,
        sehingga tidak ada jalur HTTP kedua yang lolos dari pengawasan bobot.
        """
        return await self._request(
            method, path, params=params, signed=signed, weight=weight, retries=retries
        )

    # ------------------------------------------------------- endpoint publik
    async def ping(self) -> bool:
        await self._request("GET", "/fapi/v1/ping", weight=1)
        return True

    async def server_time(self) -> int:
        payload = await self._request("GET", "/fapi/v1/time", weight=1)
        return int(payload["serverTime"])

    async def exchange_info(self) -> dict[str, Any]:
        """``GET /fapi/v1/exchangeInfo`` (bobot 1)."""
        result = await self._request("GET", "/fapi/v1/exchangeInfo", weight=1)
        if not isinstance(result, dict):  # pragma: no cover - defensif
            raise BinanceAPIError("respons exchangeInfo bukan objek JSON")
        return result

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        """``GET /fapi/v1/klines`` dengan bobot otomatis dari ``limit``."""
        if not 1 <= limit <= 1500:
            raise ValueError("limit klines harus 1..1500 (dokumentasi Binance)")
        params = {
            "symbol": self._validate_symbol(symbol),
            "interval": interval,
            "startTime": start_time,
            "endTime": end_time,
            "limit": limit,
        }
        result = await self._request(
            "GET", "/fapi/v1/klines", params=params, weight=kline_request_weight(limit)
        )
        if not isinstance(result, list):
            raise BinanceAPIError("respons klines bukan array")
        return result

    async def ticker_24hr(self) -> list[dict[str, Any]]:
        """``GET /fapi/v1/ticker/24hr`` semua simbol (bobot 40 — panggil jarang)."""
        result = await self._request("GET", "/fapi/v1/ticker/24hr", weight=40)
        if not isinstance(result, list):
            raise BinanceAPIError("respons ticker/24hr bukan array")
        return result

    async def ticker_24hr_one(self, symbol: str) -> dict[str, Any]:
        """``GET /fapi/v1/ticker/24hr`` satu simbol (bobot 1)."""
        params = {"symbol": self._validate_symbol(symbol)}
        result = await self._request("GET", "/fapi/v1/ticker/24hr", params=params, weight=1)
        if not isinstance(result, dict):
            raise BinanceAPIError("respons ticker/24hr satu simbol bukan objek")
        return result

    async def depth(self, symbol: str, *, limit: int = 20) -> dict[str, Any]:
        """``GET /fapi/v1/depth`` (snapshot order book).

        ``limit`` (default parameter Binance 500) dibatasi ketat ke nilai resmi
        [5, 10, 20, 50, 100, 500, 1000]; bobot mengikuti tabel dokumentasi.
        """
        weight = depth_request_weight(limit)
        result = await self._request(
            "GET",
            "/fapi/v1/depth",
            params={"symbol": self._validate_symbol(symbol), "limit": limit},
            weight=weight,
        )
        if not isinstance(result, dict):  # pragma: no cover - defensif
            raise BinanceAPIError("respons depth bukan objek JSON")
        return result

    async def price(self, symbol: str) -> float:
        """``GET /fapi/v1/ticker/price`` satu simbol (bobot 1) — hemat bandwidth."""
        result = await self._request(
            "GET",
            "/fapi/v1/ticker/price",
            params={"symbol": self._validate_symbol(symbol)},
            weight=1,
        )
        if not isinstance(result, dict):  # pragma: no cover - defensif
            raise BinanceAPIError("respons ticker/price bukan objek JSON")
        return float(result["price"])

    async def prices(self, symbols: list[str]) -> dict[str, float]:
        """Harga terakhir beberapa simbol (satu permintaan per simbol, bobot 1)."""
        hasil: dict[str, float] = {}
        for symbol in symbols:
            try:
                hasil[symbol.upper()] = await self.price(symbol)
            except Exception:  # pragma: no cover - simbol bermasalah tidak fatal
                continue
        return hasil

    async def book_ticker(self) -> list[dict[str, Any]]:
        """``GET /fapi/v1/ticker/bookTicker`` semua simbol (bobot 5)."""
        result = await self._request("GET", "/fapi/v1/ticker/bookTicker", weight=5)
        if not isinstance(result, list):
            raise BinanceAPIError("respons bookTicker bukan array")
        return result
