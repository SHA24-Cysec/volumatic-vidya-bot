"""Pemindai multi-simbol: dari ``exchangeInfo`` ke daftar simbol aktif.

Urutan filter dirancang supaya **murah lebih dulu** (hemat bandwidth & bobot):

1. Metadata simbol (``exchangeInfo``, bobot 1): hanya perpetual USDT berstatus TRADING.
2. Whitelist/blacklist dari config.
3. Volume 24 jam + harga minimum (dari ``ticker/24hr`` semua simbol, bobot 40).
4. Spread (dari ``ticker/bookTicker`` semua simbol, bobot 5).
5. Kelayakan modal kecil: ``minNotional`` yang menuntut margin lebih besar dari
   batas per-trade langsung dibuang — **tanpa** perlu mengunduh candle.
6. Ambil ``max_symbols`` teratas berdasarkan volume, baru kemudian unduh/ambil
   candle dari cache untuk menghitung ATR%.
7. Filter ATR% minimum/maksimum (simbol terlalu mati atau terlalu liar).

Setiap penolakan dicatat lengkap dengan alasannya (dipakai dashboard & log).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np

from volumatic_bot.binance.rest import BinanceREST
from volumatic_bot.binance.symbols import ExchangeInfo, SymbolInfo, parse_exchange_info
from volumatic_bot.config import ScannerConfig
from volumatic_bot.data.repository import KlineRepository
from volumatic_bot.indicator.numerics import rma, true_range
from volumatic_bot.models import CandleSeries

#: Panjang ATR untuk filter ATR% (murah dihitung, tanpa VIDYA).
ATR_PCT_LENGTH = 200

__all__ = [
    "ATR_PCT_LENGTH",
    "Rejection",
    "ScanResult",
    "ScanRow",
    "SymbolScanner",
    "atr_percent",
    "spread_pct",
]


def spread_pct(bid: float, ask: float) -> float:
    """Spread relatif dalam persen terhadap mid price."""
    if bid <= 0 or ask <= 0 or ask < bid:
        return float("inf")
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return float("inf")
    return (ask - bid) / mid * 100.0


def atr_percent(series: CandleSeries, length: int = 200) -> float:
    """ATR% = ATR(length) / close terakhir * 100 (murah, tanpa VIDYA)."""
    if len(series) < 2:
        return float("nan")
    atr = rma(true_range(series.high, series.low, series.close), length)
    last_atr = atr[-1]
    last_close = float(series.close[-1])
    if not np.isfinite(last_atr) or last_close <= 0:
        return float("nan")
    return float(last_atr) / last_close * 100.0


@dataclass(frozen=True, slots=True)
class ScanRow:
    """Satu simbol yang lolos semua filter."""

    symbol: str
    last_price: float
    quote_volume_24h: float
    change_pct: float
    spread_pct: float
    atr_pct: float
    score: float
    min_notional: float
    min_margin_required: float
    bars: int = 0
    series: CandleSeries | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Rejection:
    """Simbol yang ditolak pada tahap tertentu, beserta alasannya."""

    symbol: str
    stage: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Hasil satu putaran pemindaian."""

    accepted: list[ScanRow]
    rejected: list[Rejection]
    total_symbols: int
    trading_perpetuals: int
    requests: int
    dropped_by_cap: int = 0

    @property
    def accepted_symbols(self) -> list[str]:
        return [row.symbol for row in self.accepted]


class SymbolScanner:
    """Bangun & perbarui daftar simbol aktif."""

    def __init__(
        self,
        rest: BinanceREST,
        repository: KlineRepository,
        config: ScannerConfig,
        *,
        leverage: int,
        max_concurrency: int = 4,
    ) -> None:
        self.rest = rest
        self.repository = repository
        self.config = config
        self.leverage = leverage
        self.max_concurrency = max(1, max_concurrency)
        self._last_exchange_info: ExchangeInfo | None = None

    # ------------------------------------------------------------------ publik
    async def exchange_info(self, *, force: bool = False) -> ExchangeInfo:
        """Metadata simbol (di-cache di memori; panggil ulang saat refresh simbol)."""
        if self._last_exchange_info is None or force:
            payload = await self.rest.exchange_info()
            self._last_exchange_info = parse_exchange_info(payload)
        return self._last_exchange_info

    async def scan(
        self,
        *,
        max_margin_per_trade: float | None = None,
        force_refresh_meta: bool = True,
    ) -> ScanResult:
        """Jalankan satu putaran pemindaian lengkap."""
        cfg = self.config
        exchange = await self.exchange_info(force=force_refresh_meta)
        requests = 2  # exchangeInfo + ticker24hr
        tickers = await self.rest.ticker_24hr()
        books = await self.rest.book_ticker()
        requests += 1
        ticker_map = {str(t.get("symbol")): t for t in tickers}
        book_map = {str(b.get("symbol")): b for b in books}

        rejected: list[Rejection] = []
        prelim: list[tuple[SymbolInfo, float, float, float, float]] = []
        whitelist = set(cfg.whitelist)
        blacklist = set(cfg.blacklist)

        trading = {s.symbol: s for s in exchange.symbols.values() if s.is_perpetual_usdt}
        for symbol, info in trading.items():
            if whitelist and symbol not in whitelist:
                continue
            if symbol in blacklist:
                rejected.append(Rejection(symbol, "blacklist", "ada di blacklist config"))
                continue
            ticker = ticker_map.get(symbol)
            if ticker is None:
                rejected.append(Rejection(symbol, "ticker", "tidak ada data ticker 24 jam"))
                continue
            last_price = float(ticker.get("lastPrice", 0) or 0)
            quote_volume = float(ticker.get("quoteVolume", 0) or 0)
            change_pct = float(ticker.get("priceChangePercent", 0) or 0)
            if quote_volume < cfg.min_quote_volume_24h_usdt:
                rejected.append(
                    Rejection(
                        symbol,
                        "volume",
                        f"volume 24 jam {quote_volume / 1e6:.1f} jt < "
                        f"{cfg.min_quote_volume_24h_usdt / 1e6:.1f} jt USDT",
                    )
                )
                continue
            if last_price < cfg.min_price:
                rejected.append(
                    Rejection(symbol, "harga", f"harga {last_price} < minimum {cfg.min_price}")
                )
                continue
            book = book_map.get(symbol)
            if book is None:
                rejected.append(Rejection(symbol, "orderbook", "tidak ada data bookTicker"))
                continue
            bid = float(book.get("bidPrice", 0) or 0)
            ask = float(book.get("askPrice", 0) or 0)
            sp = spread_pct(bid, ask)
            if sp > cfg.max_spread_pct:
                rejected.append(
                    Rejection(
                        symbol, "spread", f"spread {sp:.3f}% > maks {cfg.max_spread_pct:.3f}%"
                    )
                )
                continue
            # Penyaring modal kecil: minNotional menentukan risiko minimum yang
            # bisa dipasang. Angka ini tidak bergantung pada --equity supaya
            # `scan` tanpa equity pun memberi daftar simbol yang realistis.
            if cfg.max_min_notional_usdt > 0:
                min_notional = float(info.filters.min_notional)
                if min_notional > cfg.max_min_notional_usdt:
                    rejected.append(
                        Rejection(
                            symbol,
                            "min_notional",
                            f"minNotional {min_notional:.2f} USDT > batas "
                            f"{cfg.max_min_notional_usdt:.2f} USDT (modal kecil)",
                        )
                    )
                    continue
            if cfg.exclude_if_min_notional_exceeds_margin and max_margin_per_trade is not None:
                need = float(info.min_margin_required(self.leverage))
                if need > max_margin_per_trade:
                    rejected.append(
                        Rejection(
                            symbol,
                            "modal",
                            f"minNotional {float(info.filters.min_notional):.2f} USDT butuh "
                            f"margin {need:.2f} > batas per-trade {max_margin_per_trade:.2f} USDT",
                        )
                    )
                    continue
            prelim.append((info, last_price, quote_volume, change_pct, sp))

        # Batasi jumlah simbol aktif: volume terbesar dulu.
        prelim.sort(key=lambda item: item[2], reverse=True)
        if whitelist:
            prelim = [item for item in prelim if item[0].symbol in whitelist]
        selected = prelim[: cfg.max_symbols]
        dropped_by_cap = len(prelim) - len(selected)
        for info, *_rest in prelim[cfg.max_symbols :]:
            rejected.append(
                Rejection(
                    info.symbol,
                    "kuota",
                    f"di luar {cfg.max_symbols} simbol teratas berdasarkan volume",
                )
            )

        # Ambil candle + hitung ATR% hanya untuk kandidat.
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def fetch(info: SymbolInfo) -> tuple[SymbolInfo, CandleSeries, int]:
            async with semaphore:
                result = await self.repository.history(info.symbol, cfg.warmup_bars)
                return info, result.series, result.requests

        fetched = await asyncio.gather(*(fetch(item[0]) for item in selected)) if selected else []
        series_map = {info.symbol: series for info, series, _ in fetched}
        requests += sum(req for _, _, req in fetched)

        rows: list[ScanRow] = []
        for info, last_price, quote_volume, change_pct, sp in selected:
            symbol = info.symbol
            series = series_map.get(symbol)
            if series is None or len(series) < self.config.warmup_bars // 2:
                rejected.append(
                    Rejection(
                        symbol,
                        "data",
                        f"candle tidak cukup ({0 if series is None else len(series)} bar)",
                    )
                )
                continue
            atr_pct = atr_percent(series, ATR_PCT_LENGTH)
            if not np.isfinite(atr_pct):
                rejected.append(Rejection(symbol, "atr", "ATR tidak dapat dihitung"))
                continue
            if atr_pct < cfg.min_atr_pct:
                rejected.append(
                    Rejection(
                        symbol,
                        "atr",
                        f"ATR% {atr_pct:.2f}% < minimum {cfg.min_atr_pct:.2f}% (terlalu datar)",
                    )
                )
                continue
            if atr_pct > cfg.max_atr_pct:
                rejected.append(
                    Rejection(
                        symbol,
                        "atr",
                        f"ATR% {atr_pct:.2f}% > maksimum {cfg.max_atr_pct:.2f}% (terlalu liar)",
                    )
                )
                continue
            rows.append(
                ScanRow(
                    symbol=symbol,
                    last_price=last_price,
                    quote_volume_24h=quote_volume,
                    change_pct=change_pct,
                    spread_pct=sp,
                    atr_pct=atr_pct,
                    score=0.0,
                    min_notional=float(info.filters.min_notional),
                    min_margin_required=float(info.min_margin_required(self.leverage)),
                    bars=len(series),
                    series=series,
                )
            )

        scored = _assign_scores(rows)
        return ScanResult(
            accepted=scored,
            rejected=rejected,
            total_symbols=len(exchange.symbols),
            trading_perpetuals=len(trading),
            requests=requests,
            dropped_by_cap=dropped_by_cap,
        )


def _assign_scores(rows: list[ScanRow]) -> list[ScanRow]:
    """Skor likuiditas 0..1: 60% volume, 40% ATR% (makin likuid & bergerak = makin baik)."""
    if not rows:
        return []
    volumes = np.array([r.quote_volume_24h for r in rows], dtype=np.float64)
    atrs = np.array([r.atr_pct for r in rows], dtype=np.float64)

    def normalize(values: np.ndarray) -> np.ndarray:
        lo, hi = float(values.min()), float(values.max())
        if hi - lo <= 1e-12:
            return np.full_like(values, 0.5)
        return (values - lo) / (hi - lo)

    vol_score = normalize(volumes)
    atr_score = normalize(atrs)
    combined = 0.6 * vol_score + 0.4 * atr_score
    return [
        ScanRow(
            symbol=row.symbol,
            last_price=row.last_price,
            quote_volume_24h=row.quote_volume_24h,
            change_pct=row.change_pct,
            spread_pct=row.spread_pct,
            atr_pct=row.atr_pct,
            score=float(combined[i]),
            min_notional=row.min_notional,
            min_margin_required=row.min_margin_required,
            bars=row.bars,
            series=row.series,
        )
        for i, row in enumerate(rows)
    ]


def min_margin_for_symbol(info: SymbolInfo, leverage: int) -> Decimal:
    """Margin minimum (USDT) agar order memenuhi ``minNotional``."""
    return info.min_margin_required(leverage)
