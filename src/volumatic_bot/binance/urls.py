"""URL & konstanta Binance USDⓈ-M Futures yang terverifikasi dari dokumentasi resmi.

Sumber (diakses 14 November 2026 melalui arsip Wayback karena situs dokumentasi
Binance kini berupa SPA yang tidak merender teks untuk klien non-browser):

* `developers.binance.com/docs/derivatives/usds-margined-futures/general-info`
  (snapshot 2026-02-04) — base URL produksi & testnet, timing security, batas IP.
* `.../market-data/rest-api/Kline-Candlestick-Data` (snapshot 2026-01-12) —
  `GET /fapi/v1/klines`, limit maks 1500, bobot per rentang limit.
* `.../market-data/rest-api/Exchange-Information` (snapshot 2026-01-12) —
  bobot 1, `rateLimits` (REQUEST_WEIGHT 2400/menit, ORDERS 1200/menit).
* `.../market-data/rest-api/24hr-Ticker-Price-Change-Statistics` (snapshot
  2026-01-12) — bobot 1 (satu simbol) / 40 (semua simbol).
* `.../market-data/rest-api/Symbol-Order-Book-Ticker` (snapshot 2026-02-13) —
  bobot 2 / 5.
* `.../websocket-market-streams/Kline-Candlestick-Streams` (snapshot 2026-03-05) —
  `<symbol>@kline_<interval>`, update 250 ms, field `x` = kline tertutup.
* `.../websocket-market-streams/Connect` — `wss://fstream.binance.com`,
  combined stream `/stream?streams=`, koneksi maksimal 24 jam, ping server tiap
  3 menit (putus bila tidak ada pong dalam 10 menit), 10 pesan masuk/detik,
  maksimal 1024 stream per koneksi.

**Temuan penting:** dokumentasi 2026 menyebut REST testnet di
``https://demo-fapi.binance.com`` — berbeda dari ``testnet.binancefuture.com``
yang dipakai dokumentasi lama. Keduanya disediakan di sini; pilih lewat config
``net.legacy_testnet_rest`` bila yang baru tidak dapat diakses dari jaringan Anda.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "INTERVAL_MS",
    "WS_STREAM_PREFIX",
    "Endpoint",
    "Interval",
    "interval_to_ms",
]


class Interval(StrEnum):
    """Interval kline yang didukung Binance USDⓈ-M Futures."""

    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"
    MO1 = "1M"


INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_419_200_000,  # perkiraan (30 hari); dipakai hanya untuk cache
}

WS_STREAM_PREFIX = {
    "live": "wss://fstream.binance.com",
    "testnet": "wss://fstream.binancefuture.com",
}

#: Channel WebSocket hasil pemisahan 2026-04-23 (diverifikasi probe langsung
#: 15 Nov 2026): ``depth``/``bookTicker`` hanya hidup di ``/public``, data
#: pasar reguler (``kline``/``markPrice``/``aggTrade``/``ticker``) di
#: ``/market``, dan user data stream di ``/private``. Jalur lama
#: ``/stream`` dan ``/ws`` sudah tidak menerima data di produksi.
WS_CHANNELS = {
    "public": "/public",
    "market": "/market",
    "private": "/private",
}


class Endpoint:
    """Base URL per lingkungan."""

    LIVE_REST = "https://fapi.binance.com"
    LIVE_WS = "wss://fstream.binance.com"
    TESTNET_REST = "https://demo-fapi.binance.com"
    LEGACY_TESTNET_REST = "https://testnet.binancefuture.com"
    TESTNET_WS = "wss://fstream.binancefuture.com"

    @classmethod
    def rest(cls, *, testnet: bool, legacy_testnet: bool = False) -> str:
        if not testnet:
            return cls.LIVE_REST
        return cls.LEGACY_TESTNET_REST if legacy_testnet else cls.TESTNET_REST

    @classmethod
    def ws(cls, *, testnet: bool) -> str:
        return cls.TESTNET_WS if testnet else cls.LIVE_WS


def interval_to_ms(interval: str) -> int:
    """Konversi nama interval ke milidetik. Melempar ``ValueError`` bila tidak dikenal."""
    try:
        return INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(
            f"interval tidak dikenal: {interval!r}. Pilihan: {sorted(INTERVAL_MS)}"
        ) from exc
