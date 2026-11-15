"""Backtester: jalankan strategi pada candle historis tanpa jaringan.

Alur satu candle (indeks ``i``) untuk satu simbol:

```
hitung indikator sekali untuk seluruh seri (tanpa look-ahead: hanya memakai
bar <= i)
  │
  ├─ ada posisi?  → periksa SL/TP/BE/trailing dengan high/low candle ini
  │                 (SL didahulukan bila keduanya tersentuh)
  └─ sinyal entry pada bar i? → entry di OPEN bar i+1 (bukan close bar i)
```

Batasan yang disengaja:

* **satu simbol per putaran** (portofolio multi-simbol bisa dijalankan dengan
  memanggil runner beberapa kali; hasil tiap simbol dilaporkan terpisah, tidak
  digabung diam-diam);
* filter order book **tidak** dipakai (data depth historis tidak disimpan) —
  ini disebutkan di laporan sebagai keterbatasan;
* ukuran posisi memakai :func:`volumatic_bot.risk.sizing.size_position` yang sama
  dengan eksekusi langsung, sehingga batas risiko ikut berlaku.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from volumatic_bot.backtest.costs import CostModel, FundingSeries
from volumatic_bot.backtest.engine import BacktestTrade, TradeManager
from volumatic_bot.binance.symbols import SymbolInfo
from volumatic_bot.config import AppConfig
from volumatic_bot.indicator import compute
from volumatic_bot.indicator.core import entry_signal
from volumatic_bot.models import CandleSeries
from volumatic_bot.risk.plan import Side, build_trade_plan
from volumatic_bot.risk.sizing import size_position

__all__ = ["BacktestConfig", "BacktestResult", "muat_funding", "run_backtest"]


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Pengaturan satu putaran backtest."""

    equity: float = 50.0
    leverage: int | None = None
    partial_fraction: float = 0.5
    trailing_atr_mult: float = 2.0
    max_bars_in_trade: int = 0  # 0 = tanpa batas
    warmup_bars: int = 260
    cooldown_bars: int = 1
    #: Tutup paksa semua posisi di bar terakhir (agar hasil bisa dihitung).
    close_at_end: bool = True


@dataclass(slots=True)
class BacktestResult:
    """Hasil satu simbol: perdagangan, kurva ekuitas, dan statistik."""

    symbol: str
    interval: str
    bars: int
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    start_equity: float = 0.0
    end_equity: float = 0.0
    funding_source: str = "tetap"
    funding_available: bool = False
    notes: list[str] = field(default_factory=list)
    first_ms: int = 0
    last_ms: int = 0

    # ------------------------------------------------------------ statistik
    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net_usdt > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.net_usdt < 0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.count * 100.0 if self.count else 0.0

    @property
    def net_usdt(self) -> float:
        return sum(t.net_usdt for t in self.trades)

    @property
    def gross_usdt(self) -> float:
        return sum(t.gross_usdt for t in self.trades)

    @property
    def fees_usdt(self) -> float:
        return sum(t.costs.fee_usdt for t in self.trades)

    @property
    def slippage_usdt(self) -> float:
        return sum(t.costs.slippage_usdt for t in self.trades)

    @property
    def funding_usdt(self) -> float:
        return sum(t.costs.funding_usdt for t in self.trades)

    @property
    def total_costs(self) -> float:
        return self.fees_usdt + self.slippage_usdt + self.funding_usdt

    @property
    def return_pct(self) -> float:
        if self.start_equity <= 0:
            return 0.0
        return self.net_usdt / self.start_equity * 100.0

    @property
    def avg_r(self) -> float:
        return sum(t.r_multiple for t in self.trades) / self.count if self.count else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        """Penurunan terbesar dari puncak ekuitas (persen)."""
        puncak = self.start_equity
        terburuk = 0.0
        for _, ekuitas in self.equity_curve:
            puncak = max(puncak, ekuitas)
            if puncak > 0:
                terburuk = max(terburuk, (puncak - ekuitas) / puncak * 100.0)
        return terburuk

    @property
    def profit_factor(self) -> float | None:
        """Total untung / total rugi. ``None`` bila tidak ada rugi (tidak terhingga)."""
        untung = sum(t.net_usdt for t in self.trades if t.net_usdt > 0)
        rugi = -sum(t.net_usdt for t in self.trades if t.net_usdt < 0)
        if rugi <= 0:
            return None if untung > 0 else 0.0
        return untung / rugi

    def skipped_by_margin(self) -> int:
        """Jumlah entry yang dilewati karena batas margin (bukan karena biaya)."""
        return sum(1 for alasan in self.skipped if "margin" in alasan)

    def reasons(self) -> dict[str, int]:
        hasil: dict[str, int] = {}
        for t in self.trades:
            hasil[t.reason] = hasil.get(t.reason, 0) + 1
        return dict(sorted(hasil.items()))

    def summary(self) -> str:
        pf = self.profit_factor
        pf_teks = "tak terhingga (tidak ada rugi)" if pf is None else f"{pf:.2f}"
        return (
            f"{self.symbol} {self.interval}: {self.count} trade "
            f"(menang {self.wins} / kalah {self.losses}, WR {self.win_rate:.1f}%) "
            f"bersih {self.net_usdt:+.2f} USDT ({self.return_pct:+.2f}%) "
            f"avgR {self.avg_r:+.2f} PF {pf_teks} maxDD {self.max_drawdown_pct:.2f}% "
            f"| biaya fee {self.fees_usdt:.2f} slip {self.slippage_usdt:.2f} "
            f"funding {self.funding_usdt:+.2f}"
        )


def run_backtest(
    series: CandleSeries,
    info: SymbolInfo,
    config: AppConfig,
    *,
    bt: BacktestConfig | None = None,
    costs: CostModel | None = None,
    funding: FundingSeries | None = None,
    interval: str | None = None,
) -> BacktestResult:
    """Jalankan strategi pada satu simbol tanpa jaringan."""
    bt = bt or BacktestConfig()
    if costs is None:
        costs = CostModel(
            taker_bps=config.commission.taker_bps,
            slippage_bps=config.commission.slippage_bps,
            funding_rate_8h_pct=config.commission.funding_rate_8h_pct,
        )
    interval = interval or config.scanner.timeframe
    equity = bt.equity
    result = BacktestResult(
        symbol=info.symbol,
        interval=interval,
        bars=len(series),
        start_equity=equity,
        end_equity=equity,
        funding_source=funding.source if funding is not None else costs_source(costs),
        funding_available=funding is not None,
        first_ms=int(series.open_time[0]) if len(series) else 0,
        last_ms=int(series.open_time[-1]) if len(series) else 0,
    )
    if len(series) < bt.warmup_bars + 2:
        result.notes.append(
            f"data terlalu pendek: {len(series)} bar < warm-up {bt.warmup_bars} + 2"
        )
        result.equity_curve.append((result.first_ms, equity))
        return result

    hasil_indikator = compute(series, config.indicator.to_params())
    manager = TradeManager(costs, funding=funding, trailing_atr_mult=bt.trailing_atr_mult)

    equity_curve: list[tuple[int, float]] = []
    equity = _run_loop(
        series=series,
        result=result,
        config=config,
        bt=bt,
        manager=manager,
        hasil_indikator=hasil_indikator,
        info=info,
        costs=costs,
        equity=equity,
        equity_curve=equity_curve,
    )
    result.equity_curve = equity_curve
    result.end_equity = equity
    if not result.funding_available:
        result.notes.append(
            "funding tidak tersedia dari data: memakai tarif tetap "
            f"{costs.funding_rate_8h_pct:.4f}% / 8 jam (asumsi, bukan data bursa)"
        )
    return result


def muat_funding(path: Path) -> FundingSeries:
    """Muat data funding historis (JSON list atau JSONL) dari disk.

    Format tiap baris: ``{"fundingTime": <ms>, "fundingRate": <desimal>}`` —
    persis keluaran ``scripts/unduh_funding.py``.
    """
    teks = path.read_text(encoding="utf-8").strip()
    if not teks:
        raise ValueError(f"berkas funding kosong: {path}")
    if teks.startswith("["):
        rows = json.loads(teks)
    else:
        rows = [json.loads(baris) for baris in teks.splitlines() if baris.strip()]
    return FundingSeries.from_rows([r for r in rows if "fundingTime" in r])


def costs_source(costs: CostModel) -> str:
    return f"tetap {costs.funding_rate_8h_pct:.4f}%/{costs.funding_interval_hours:.0f}j"


def _run_loop(
    *,
    series: CandleSeries,
    result: BacktestResult,
    config: AppConfig,
    bt: BacktestConfig,
    manager: TradeManager,
    hasil_indikator: Any,
    info: SymbolInfo,
    costs: CostModel,
    equity: float,
    equity_curve: list[tuple[int, float]],
) -> float:
    """Inti loop bar-per-bar. Dipisah agar mudah dibaca dan diuji."""
    ind = hasil_indikator
    open_time = series.open_time
    highs = series.high
    lows = series.low
    closes = series.close
    opens = series.open
    n = len(series)
    leverage = bt.leverage or config.exchange.default_leverage
    cooldown_sampai = -1
    menunggu_entry: str | None = None
    durasi = 0

    for i in range(bt.warmup_bars, n):
        ts = int(open_time[i])
        high = float(highs[i])
        low = float(lows[i])
        close = float(closes[i])
        trail = float(ind.trail[i])
        atr = float(ind.atr[i])

        # --- posisi berjalan ------------------------------------------
        if manager.position is not None:
            durasi += 1
            pos = manager.position
            alasan = manager.on_candle(
                high=high, low=low, close=close, trail=trail, atr=atr, ts_ms=ts
            )
            if alasan is None and bt.max_bars_in_trade and durasi >= bt.max_bars_in_trade:
                alasan = "waktu_habis"
            if alasan is not None:
                equity = _close_trade(
                    manager=manager,
                    result=result,
                    costs=costs,
                    price=_exit_price(alasan, pos.stop_price, close, pos.side),
                    ts_ms=ts,
                    reason=alasan,
                    equity=equity,
                    equity_curve=equity_curve,
                )
                cooldown_sampai = i + max(0, bt.cooldown_bars)
                durasi = 0
        elif menunggu_entry is not None and i > 0:
            # Sinyal muncul di bar sebelumnya → entry di OPEN bar ini.
            sisi = menunggu_entry
            menunggu_entry = None
            if i > cooldown_sampai and _boleh_entry(config, equity):
                harga = costs.fill_price(float(opens[i]), sisi, entering=True)
                equity = _try_open(
                    manager=manager,
                    result=result,
                    config=config,
                    bt=bt,
                    sisi=sisi,
                    harga=harga,
                    close=float(closes[i - 1]),
                    trail=float(ind.trail[i - 1]),
                    atr=float(ind.atr[i - 1]),
                    ts_ms=ts,
                    equity=equity,
                    leverage=leverage,
                    info=info,
                )
                durasi = 0

        # --- sinyal baru pada bar ini (dipakai bar berikutnya) ---------
        if manager.position is None and menunggu_entry is None:
            arah = entry_signal(ind, i)
            if arah is not None and float(ind.atr[i]) > 0:
                menunggu_entry = arah

        if manager.position is None:
            equity_curve.append((ts, equity))

    # --- posisi yang masih terbuka di akhir data ----------------------
    if manager.position is not None:
        pos = manager.position
        if bt.close_at_end:
            equity = _close_trade(
                manager=manager,
                result=result,
                costs=costs,
                price=costs.fill_price(float(closes[-1]), pos.side, entering=False),
                ts_ms=int(open_time[-1]),
                reason="akhir_data",
                equity=equity,
                equity_curve=equity_curve,
            )
        else:
            result.notes.append(
                f"posisi {pos.symbol} masih terbuka di akhir data; tidak dihitung tertutup"
            )
    if not equity_curve:
        equity_curve.append((int(open_time[0]), equity))
    return equity


def _exit_price(alasan: str, stop_price: float, close: float, side: str) -> float:
    """Harga keluar: SL memakai harga SL (bisa gap → pakai harga terburuk)."""
    if alasan.startswith("stop_loss"):
        return stop_price
    return close


def _boleh_entry(config: AppConfig, equity: float) -> bool:
    return equity >= config.risk.min_equity_usdt


def _try_open(
    *,
    manager: TradeManager,
    result: BacktestResult,
    config: AppConfig,
    bt: BacktestConfig,
    sisi: str,
    harga: float,
    close: float,
    trail: float,
    atr: float,
    ts_ms: int,
    equity: float,
    leverage: int,
    info: SymbolInfo,
) -> float:
    """Susun rencana (sizing + level) lalu buka posisi; lewati bila ditolak."""
    arah: Side = "long" if sisi == "long" else "short"
    # Batas margin **sama** dengan RiskManager: 60% equity total & 30% per simbol.
    batas_simbol = equity * config.risk.max_per_symbol_margin_pct / 100.0
    batas_total = equity * config.risk.max_total_margin_pct / 100.0
    size = size_position(
        equity=equity,
        entry_price=harga,
        stop_price=preliminary_stop(sisi, harga, atr, trail, config),
        filters=info.filters,
        side=arah,
        leverage=leverage,
        risk_pct=config.risk.risk_per_trade_pct,
        commission=config.commission,
        config=config.risk,
        available_margin=min(batas_simbol, batas_total),
    )
    if not size.approved:
        result.skipped.append(f"{_jam(ts_ms)} {sisi}: {'; '.join(size.reasons)}")
        return equity
    plan = build_trade_plan(
        side=arah,
        entry_price=harga,
        atr=atr,
        trail=trail,
        qty=float(size.qty),
        filters=info.filters,
        trade=config.trade,
        commission=config.commission,
    )
    entry = float(info.filters.round_price(harga))
    manager.open(
        symbol=info.symbol,
        side=sisi,
        qty=float(size.qty),
        price=entry,
        stop_price=float(plan.stop_price),
        tp1_price=float(plan.tp1_price),
        be_price=float(plan.be_price),
        be_trigger_price=float(plan.be_trigger_price),
        trailing_activation_price=float(plan.trailing_activation_price),
        trail_line=trail,
        atr=atr,
        open_ms=ts_ms,
        partial_fraction=bt.partial_fraction,
    )
    return equity


def preliminary_stop(sisi: str, harga: float, atr: float, trail: float, config: AppConfig) -> float:
    """Perkiraan SL sebelum ukuran dihitung — **logika sama** dengan
    ``RiskManager._preliminary_stop`` (diekstrak agar backtest dan live tidak
    berbeda tanpa disadari)."""
    trade = config.trade
    jarak = trade.sl_atr_mult * atr
    if trail > 0:
        trail_ok = (sisi == "long" and trail < harga) or (sisi == "short" and trail > harga)
        if trail_ok and trade.sl_mode in ("vidya_trail", "nearest"):
            jarak = min(jarak, abs(harga - trail))
    jarak = max(jarak, trade.sl_min_atr_mult * atr)
    return harga - jarak if sisi == "long" else harga + jarak


def _close_trade(
    *,
    manager: TradeManager,
    result: BacktestResult,
    costs: CostModel,
    price: float,
    ts_ms: int,
    reason: str,
    equity: float,
    equity_curve: list[tuple[int, float]],
) -> float:
    pos = manager.position
    assert pos is not None
    harga_keluar = costs.fill_price(price, pos.side, entering=False)
    trade = manager.close(price=harga_keluar, ts_ms=ts_ms, reason=reason, equity_before=equity)
    result.trades.append(trade)
    equity_curve.append((ts_ms, trade.equity_after))
    return trade.equity_after


def _jam(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


__all__ += ["Any"]
