"""Jenis error Binance yang aman ditampilkan (tanpa membocorkan rahasia)."""

from __future__ import annotations

from typing import Any

from volumatic_bot.logging_setup import sanitize

__all__ = [
    "BinanceAPIError",
    "BinanceNetworkError",
    "IpBannedError",
    "RateLimitError",
    "describe_http_status",
]


def describe_http_status(status: int) -> str:
    """Arti kode HTTP penting menurut dokumentasi Binance."""
    return {
        400: "parameter salah / request tidak valid",
        401: "API key tidak valid atau signature salah",
        403: "IP tidak di-whitelist atau akses ditolak",
        404: "endpoint tidak ditemukan",
        408: "timeout di server Binance",
        418: "IP diblokir sementara (auto-ban) karena terus melanggar rate limit",
        429: "rate limit terlampaui",
        451: "permintaan diblokir dari lokasi/IP ini",
        500: "kesalahan internal Binance",
        503: "layanan Binance sedang tidak tersedia",
        504: "gateway timeout Binance",
    }.get(status, "status tidak dikenal")


class BinanceAPIError(RuntimeError):
    """Error dari REST API Binance (kode negatif seperti -1021, atau HTTP 4xx/5xx)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        endpoint: str = "",
        payload: Any | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.endpoint = endpoint
        self.payload = payload
        detail = message
        if status is not None:
            detail = f"[HTTP {status} {describe_http_status(status)}] {detail}"
        if code is not None:
            detail = f"[kode {code}] {detail}"
        # Sanitasi: jangan pernah biarkan signature/API key ikut di pesan error.
        super().__init__(sanitize(f"{endpoint}: {detail}" if endpoint else detail))

    def __str__(self) -> str:
        return super().__str__()


class RateLimitError(BinanceAPIError):
    """HTTP 429: rate limit terlampaui. ``retry_after`` dalam detik bila dikirim server."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        self.retry_after = retry_after
        super().__init__(message, **kwargs)


class IpBannedError(BinanceAPIError):
    """HTTP 418: IP diblokir sementara. Bot harus berhenti, bukan mengulang."""


class BinanceNetworkError(BinanceAPIError):
    """Kegagalan jaringan/timeout yang boleh diulang dengan backoff."""
