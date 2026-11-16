"""CLI baris perintah bot Volumatic VIDYA.

Perintah yang tersedia pada TAHAP 1:

* ``version``        -- tampilkan versi & info Python
* ``check-config``   -- validasi config.yaml + cek keberadaan kredensial
* ``indicator``      -- hitung indikator dari file candle lokal dan tampilkan ringkasan
* ``warmup-study``   -- studi konvergensi warm-up, tulis laporan Markdown
* ``tv-compare``     -- bandingkan hasil Python dengan ekspor CSV TradingView

Perintah jaringan (mulai TAHAP 2) dan order book (TAHAP 3):

* ``symbols``  -- ringkasan exchangeInfo & filter simbol
* ``scan``     -- pindai simbol, tampilkan alasan lolos/tidak
* ``warmup``   -- isi cache candle
* ``listen``   -- dengarkan kline tertutup (opsional: ``--with-book``)
* ``book``     -- periksa order book satu/beberapa simbol (TAHAP 3)

Perintah risiko (TAHAP 4):

* ``preflight`` -- pemeriksaan awal **tanpa jaringan** (config, mode, kill switch)
* ``sizing``    -- hitung ukuran posisi + level SL/TP/BE untuk satu simbol

Perintah eksekusi (TAHAP 5):

* ``simulate-trade`` -- siklus penuh entry→SL/TP→BE→trailing di broker simulasi
* ``reconcile``      -- (jaringan) samakan catatan bot dengan posisi di bursa
* ``positions``      -- tampilkan posisi/insiden/order yang tercatat di SQLite

Perintah tampilan (TAHAP 6):

* ``dashboard``      -- dashboard ``rich`` 4 panel (``--demo`` untuk data contoh)

Perintah backtest (TAHAP 7):

* ``backtest``         -- strategi pada candle historis, dengan fee+slippage+funding
* ``simulate-session`` -- jalankan **mesin** pada data historis (tanpa jaringan)
* ``run``              -- jalankan mesin dengan data bursa (butuh jaringan)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import platform
import sys
import time as _time
from pathlib import Path
from typing import Any

from volumatic_bot import __version__
from volumatic_bot.config import (
    AppConfig,
    ConfigError,
    Secrets,
    baca_yaml,
    daftar_profil,
    deteksi_profil_otomatis,
    load_config,
    mask_secret,
    profil_aktif_dari,
)
from volumatic_bot.data.loaders import load_candles_csv
from volumatic_bot.indicator import compute
from volumatic_bot.indicator.warmup import convergence_report, recommended_warmup, to_markdown_table
from volumatic_bot.logging_setup import setup_logging
from volumatic_bot.notify.events import (
    event_for_entry,
    event_for_exit,
    event_for_incident,
    event_for_rejected,
)


def _cmd_version(_: argparse.Namespace) -> int:
    print(f"volumatic-vidya-bot {__version__}")
    print(f"Python {platform.python_version()} ({sys.implementation.name})")
    print(f"Executable: {sys.executable}")
    return 0


def _load(args: argparse.Namespace) -> tuple[AppConfig, Secrets]:
    config = load_config(args.config, profil=getattr(args, "profil", None))
    secrets = Secrets()
    return config, secrets


def _cmd_profil(args: argparse.Namespace) -> int:
    """Tampilkan profil yang tersedia dan nilai yang ditimpanya (tanpa jaringan)."""
    raw = baca_yaml(args.config)
    tersedia = daftar_profil(raw)
    aktif = profil_aktif_dari(raw, getattr(args, "profil", None))
    print(f"config   : {args.config}")
    print(f"profil aktif: {aktif or '(tidak ada — memakai nilai biasa)'}")
    otomatis = raw.get("profil_otomatis")
    if isinstance(otomatis, dict) and otomatis.get("aktif"):
        print(
            f"otomatis : AKTIF (saldo < {otomatis.get('ambang_sangat_kecil_usdt')} -> "
            f"{otomatis.get('profil_sangat_kecil')}, < {otomatis.get('ambang_kecil_usdt')} -> "
            f"{otomatis.get('profil_kecil')}; dipilih sekali saat sesi mulai)"
        )
    else:
        print("otomatis : nonaktif")
    if not tersedia:
        print("tidak ada bagian `profil:` di config ini.")
        return 0

    def datar(d: dict[str, object], awalan: str = "") -> dict[str, object]:
        hasil: dict[str, object] = {}
        for kunci, nilai in d.items():
            jalur = f"{awalan}{kunci}"
            if isinstance(nilai, dict):
                hasil.update(datar(nilai, f"{jalur}."))
            else:
                hasil[jalur] = nilai
        return hasil

    dasar = datar({k: v for k, v in raw.items() if k not in ("profil", "profil_aktif")})
    for nama in sorted(tersedia):
        tanda = " (aktif)" if nama == aktif else ""
        print()
        print(f"profil {nama}{tanda}:")
        for jalur, nilai in datar(tersedia[nama]).items():
            lama_nilai = dasar.get(jalur, "(baru)")
            print(f"  {jalur:<38} {lama_nilai}  ->  {nilai}")
    print()
    print("pakai sekali jalan : volumatic-bot --profil <nama> check-config")
    print("pakai permanen     : ubah `profil_aktif:` di config")
    return 0


def _cmd_check_config(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    logger = setup_logging(config.app.log_level, secrets=secrets.secret_values())
    logger.info("Konfigurasi valid. Fingerprint: %s", config.fingerprint())
    raw = baca_yaml(args.config)
    aktif = profil_aktif_dari(raw, getattr(args, "profil", None))
    print(f"profil            : {aktif or '(tidak ada — nilai biasa config)'}")
    print(f"mode operasi      : {config.ops.mode}")
    print(f"testnet           : {config.exchange.testnet}")
    print(f"timeframe scanner : {config.scanner.timeframe}")
    print(f"maks simbol       : {config.scanner.max_symbols}")
    lev = f"{config.exchange.max_leverage}x (default {config.exchange.default_leverage}x)"
    print(f"leverage maks     : {lev}")
    print(f"risiko per trade  : {config.risk.risk_per_trade_pct}% equity")
    print(f"slot posisi       : {config.risk.max_concurrent_positions}")
    api_key = secrets.binance_api_key.get_secret_value() if secrets.binance_api_key else None
    print(f"API key           : {mask_secret(api_key)}")
    if not secrets.has_api_credentials:
        print(
            "PERINGATAN        : BINANCE_API_KEY / BINANCE_API_SECRET belum diset "
            "(wajib untuk testnet/live)."
        )
    if config.ops.mode in ("testnet", "live") and not secrets.has_api_credentials:
        print("GAGAL             : mode testnet/live tanpa kredensial API.")
        return 2
    if config.ops.mode == "live":
        print("PERINGATAN KERAS  : mode LIVE aktif. Uang sungguhan akan dipakai.")
    return 0


def _cmd_indicator(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())
    candles = load_candles_csv(args.candles, max_rows=args.rows)
    candles = _samakan_interval(candles, config.scanner.timeframe)
    params = config.indicator.to_params()
    result = compute(candles, params)
    tail = args.show
    print(
        f"Data: {args.candles} | {len(candles)} candle | warm-up teoretis: "
        f"{params.warmup_bars} bar | warm-up dari config: {config.scanner.warmup_bars} bar "
        f"(lihat docs/studi-warmup.md)"
    )
    print(
        f"{'open_time':>14} {'close':>12} {'vidya':>12} {'atr':>10} {'upper':>12} "
        f"{'lower':>12} {'trail':>12} {'trend':>6} {'delta%':>8}"
    )
    start = max(0, len(result) - tail)
    for i in range(start, len(result)):
        print(
            f"{int(result.open_time[i]):>14} {float(candles.close[i]):>12.4f} "
            f"{result.vidya[i]:>12.4f} {result.atr[i]:>10.4f} {result.upper[i]:>12.4f} "
            f"{result.lower[i]:>12.4f} {result.trail[i]:>12.4f} {int(result.trend[i]):>6} "
            f"{result.vol_delta_pct[i]:>8.2f}"
        )
    print(f"Jumlah zona likuiditas terdeteksi: {len(result.zones)}")
    return 0


def _cmd_warmup_study(args: argparse.Namespace) -> int:
    config, _ = _load(args)
    candles = load_candles_csv(args.candles)
    params = config.indicator.to_params()
    rows = convergence_report(candles, params)
    table = to_markdown_table(rows)
    print(table)
    print(f"Warm-up minimal yang konvergen: {recommended_warmup(rows)} candle")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            f"# Studi warm-up indikator\n\nSumber data: `{args.candles}` "
            f"({len(candles)} candle)\n\n{table}\n",
            encoding="utf-8",
        )
        print(f"Laporan ditulis ke {out}")
    return 0


def _cmd_tv_compare(args: argparse.Namespace) -> int:
    config, _ = _load(args)
    candles = load_candles_csv(args.candles)
    result = compute(candles, config.indicator.to_params())
    from volumatic_bot.indicator.tv_compare import compare_with_tradingview

    report = compare_with_tradingview(result, args.tv)
    markdown = report.to_markdown()
    print(markdown)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
    return 0 if report.passed else 1


# =============================================================================
# Perintah yang membutuhkan jaringan (TAHAP 2)
# =============================================================================


def _make_rest(config: AppConfig, secrets: Secrets) -> Any:
    """Bangun klien REST sesuai config (tanpa kredensial bila belum ada)."""
    from volumatic_bot.binance.rest import BinanceREST

    api_key = secrets.api_key() if secrets.has_api_credentials else None
    api_secret = secrets.api_secret() if secrets.has_api_credentials else None
    return BinanceREST(
        base_url=config.net.resolve_rest_url(testnet=config.exchange.testnet),
        api_key=api_key,
        api_secret=api_secret,
        recv_window=config.exchange.recv_window_ms,
        timeout=config.exchange.request_timeout_sec,
        max_retries=config.exchange.max_retries,
        weight_budget_per_min=config.exchange.weight_budget_per_min,
    )


def _make_cache(config: AppConfig) -> Any:
    from volumatic_bot.data.cache import KlineCache

    return KlineCache(config.db_cache(), enabled=True)


def _make_repository(config: AppConfig, rest: Any, cache: Any) -> Any:
    from volumatic_bot.data.repository import KlineRepository

    return KlineRepository(
        rest,
        cache,
        config.scanner.timeframe,
        warmup_bars=config.scanner.warmup_bars,
        kline_rest_limit=config.scanner.kline_rest_limit,
        pace_sec=config.net.kline_pace_sec,
    )


def _print_metric_summary(metrics: Any, requests: int) -> None:
    snap = metrics.snapshot()
    print(
        f"Permintaan HTTP: {snap.http_requests} (retry {snap.http_retries}, error "
        f"{snap.http_errors}) | unduhan {snap.http_rx_bytes / 1024:.1f} KB | "
        f"bobot terpakai {snap.used_weight}/{snap.weight_limit} | "
        f"estimasi {snap.bandwidth_mb_per_hour:.2f} MB/jam | hitung internal {requests}"
    )


def _make_depth_source(config: AppConfig, rest: Any) -> Any:
    """Bangun sumber snapshot depth sesuai ``config.orderbook.source``."""
    from volumatic_bot.orderbook.depth import RestDepthSource, StreamDepthSource

    if config.orderbook.source == "stream":
        return StreamDepthSource.build(
            levels=config.orderbook.stream_levels,
            speed_ms=config.orderbook.stream_speed_ms,
            base_url=config.net.resolve_ws_url(testnet=config.exchange.testnet),
            max_age_sec=config.orderbook.depth_snapshot_ttl_sec,
        )
    return RestDepthSource(
        rest=rest,
        limit=config.orderbook.depth_limit,
        ttl_sec=config.orderbook.depth_snapshot_ttl_sec,
    )


def _cmd_positions(args: argparse.Namespace) -> int:
    """Tampilkan state lokal (tanpa jaringan): posisi, order terakhir, insiden."""
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())

    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.storage.db import SqliteDatabase

    db = SqliteDatabase(config.db_posisi())
    db.open()
    try:
        store = PositionStore(db)
        aktif = store.active_positions()
        print(f"Database: {config.db_posisi()} (mode {config.ops.mode})")
        print(f"Posisi aktif: {len(aktif)}")
        if getattr(args, "all_modes", False):
            # Penting: bot yang sedang berjalan mungkin memakai mode/berkas lain
            # (mis. `positions` dijalankan dengan config mode dry_run padahal bot
            # jalan testnet). Tanpa ini, "Posisi aktif: 0" bisa menyesatkan.
            folder = Path(config.db_posisi()).parent
            lain = [
                f for f in sorted(folder.glob("bot*.db")) if f.name != Path(config.db_posisi()).name
            ]
            if lain:
                print("\nBerkas database mode lain:")
                for f in lain:
                    try:
                        db_lain = SqliteDatabase(str(f))
                        db_lain.open()
                        try:
                            jml = len(PositionStore(db_lain).active_positions())
                        finally:
                            db_lain.close()
                    except Exception as exc:
                        print(f"  {f.name:<24} tidak terbaca ({type(exc).__name__})")
                        continue
                    tanda = "  <-- ada posisi!" if jml else ""
                    print(f"  {f.name:<24} posisi aktif: {jml}{tanda}")
        for pos in aktif:
            print(f"  - {pos.summary()}")
        riwayat = store.all_positions(limit=args.limit)
        if riwayat:
            print(f"\n{len(riwayat)} posisi terakhir:")
            for pos in riwayat:
                print(
                    f"  {pos.symbol:<12} {pos.state.value:<13} qty={pos.qty} "
                    f"entry={pos.entry_price} alasan_tutup={pos.close_reason or '-'}"
                )
        insiden = store.incidents(limit=10)
        if insiden:
            print(f"\n{len(insiden)} insiden terakhir:")
            for item in insiden:
                print(f"  [{item.severity}] {item.kind} {item.symbol or '-'}: {item.detail}")
        return 0
    finally:
        db.close()


def _load_sample_exchange_info(path: str) -> Any:
    """Muat contoh exchangeInfo dari file (dipakai ``simulate-trade``)."""
    import json

    from volumatic_bot.binance.symbols import parse_exchange_info

    berkas = Path(path)
    if not berkas.exists():
        raise FileNotFoundError(
            f"{path} tidak ada. Jalankan dari akar repo atau berikan --exchange-info."
        )
    return parse_exchange_info(json.loads(berkas.read_text(encoding="utf-8")))


def _cmd_simulate_trade(args: argparse.Namespace) -> int:
    """Siklus penuh di broker simulasi: entry → proteksi → TP1 → BE → trailing."""
    config, secrets = _load(args)
    logger = setup_logging(config.app.log_level, secrets=secrets.secret_values())
    info = _load_sample_exchange_info(args.exchange_info)
    item = info.symbols.get(args.symbol.upper())
    if item is None:
        print(f"{args.symbol} tidak ada di contoh exchangeInfo", file=sys.stderr)
        return 1

    async def run() -> int:
        import time as _time

        from volumatic_bot.execution.broker import DryRunBroker
        from volumatic_bot.execution.executor import TradeExecutor
        from volumatic_bot.execution.reconcile import reconcile
        from volumatic_bot.positions.store import PositionStore
        from volumatic_bot.risk import (
            DailyState,
            Portfolio,
            RiskManager,
            restore_daily_state,
            save_daily_state,
        )
        from volumatic_bot.risk.limits import DailyLimits, TradeOutcome
        from volumatic_bot.storage.db import SqliteDatabase

        db = SqliteDatabase(config.db_posisi())
        db.open()
        try:
            store = PositionStore(db)
            broker = DryRunBroker(taker_bps=config.commission.taker_bps)
            broker.set_price(item.symbol, args.entry)
            zona = _now_sadar_zona(config.app.timezone)
            limits = DailyLimits(
                daily_loss_limit_pct=config.risk.daily_loss_limit_pct,
                max_trades_per_day=config.risk.max_trades_per_day,
                consecutive_loss_limit=config.risk.consecutive_loss_limit,
                cooldown_minutes=config.risk.cooldown_minutes,
                reset_hour_local=config.risk.daily_loss_reset_hour_local,
                timezone=config.app.timezone,
            )
            harian = DailyState(limits=limits, start_equity=args.equity, current_equity=args.equity)
            pulih = restore_daily_state(store, harian, zona())
            if pulih is not None:
                print(
                    f"(state harian dipulihkan: kerugian {pulih.lost_usdt:.2f} USDT, "
                    f"{pulih.trades} trade)"
                )
            mgr = RiskManager(config, daily=harian, clock=zona)
            atr = args.entry * args.atr_pct / 100.0
            tiket = mgr.evaluate(
                symbol=item.symbol,
                side=args.side,
                entry_price=args.entry,
                atr=atr,
                trail=args.trail,
                equity=args.equity,
                portfolio=Portfolio(
                    equity=args.equity,
                    positions=(),
                    max_total_margin_pct=config.risk.max_total_margin_pct,
                    max_per_symbol_margin_pct=config.risk.max_per_symbol_margin_pct,
                ),
                info=item,
                open_positions=0,
            )
            print(f"Risiko: {tiket.summary()}")
            notif = None if args.no_notify else _build_notifier(config, logger)
            if notif is not None and tiket.rejected:
                notif.emit(event_for_rejected(tiket))
            if not tiket.approved:
                print("(tidak ada order dikirim: tiket risiko ditolak)")
                return 1
            executor = TradeExecutor(
                config,
                broker,
                store,
                clock=lambda: int(_time.time() * 1000),
                logger=logger.info,
                mode="dry_run",
                filters_of=lambda simbol: item.filters,
            )
            laporan = await executor.open_trade(tiket, atr=atr)
            print(f"\nEksekusi: {laporan.summary()}")
            if notif is not None:
                pos_baru = store.get_position(item.symbol)
                if laporan.ok and pos_baru is not None:
                    notif.emit(event_for_entry(pos_baru))
                for insiden in laporan.incidents:
                    notif.emit(
                        event_for_incident(
                            kind="protection_failed",
                            severity="critical",
                            detail=insiden,
                            symbol=item.symbol,
                        )
                    )
            for insiden in laporan.incidents:
                print(f"  - insiden: {insiden}")

            jalur = [float(x) for x in args.path.split(",") if x.strip()] if args.path else []
            for harga in jalur:
                print(f"\nHarga bergerak → {harga:g}")
                terpicu = await broker.on_price(item.symbol, harga)
                for order in terpicu:
                    print(f"  order terpicu: {order.describe()}")
                    await executor.on_order_update(_order_to_update(order))
                pos = store.get_position(item.symbol)
                if pos is not None and pos.active and tiket.plan is not None:
                    geser = await executor.update_trailing(
                        item.symbol,
                        price=harga,
                        atr=atr,
                        activation_price=tiket.plan.trailing_activation_price,
                    )
                    if geser:
                        terbaru = store.get_position(item.symbol)
                        print(f"  trailing: SL → {terbaru.stop_price if terbaru else '?'}")

            akhir = store.get_position(item.symbol)
            if notif is not None and akhir is not None and not akhir.active:
                notif.emit(event_for_exit(akhir))
            print(f"\nStatus akhir: {akhir.summary() if akhir else 'tidak ada posisi'}")
            print(
                f"Broker simulasi: fee={broker.fees_usdt:.4f} USDT, "
                f"realisasi={broker.realized_usdt:.4f} USDT"
            )
            laporan_rekon = await reconcile(
                broker=broker, store=store, symbols=[item.symbol], mode="dry_run"
            )
            print(laporan_rekon.summary())

            if akhir is not None and not akhir.active:
                # Satu simbol per simulasi → PnL bersih = realisasi broker − fee.
                pnl_bersih = broker.realized_usdt - broker.fees_usdt
                harian.current_equity = args.equity + pnl_bersih
                harian.record(
                    TradeOutcome(symbol=akhir.symbol, pnl_usdt=pnl_bersih, closed_at=zona()),
                    zona(),
                )
            simpan = save_daily_state(store, harian, zona())
            print(
                f"State harian disimpan: hari={simpan.day} kerugian={simpan.lost_usdt:.2f} "
                f"trade={simpan.trades}"
            )
            return 0
        finally:
            db.close()

    return asyncio.run(run())


def _build_notifier(config: Any, logger: Any) -> Any:
    """Bangun dispatcher notifikasi (kanal konsol) dari konfigurasi."""
    from volumatic_bot.notify.factory import build_dispatcher

    return build_dispatcher(config, target=logger)


def _order_to_update(order: Any) -> Any:
    """Ubah ``OrderResult`` menjadi event order (lihat ``execution.updates``)."""
    from volumatic_bot.execution.updates import order_result_to_update

    return order_result_to_update(order)


def _terapkan_override_risiko(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    """Terapkan ``--risk-pct`` bila diberikan (tanpa mengubah berkas konfigurasi)."""
    nilai = getattr(args, "risk_pct", None)
    if nilai is None:
        return config
    print(f"(override risiko per trade: {nilai}% — hanya untuk proses ini)")
    return config.model_copy(
        update={"risk": config.risk.model_copy(update={"risk_per_trade_pct": nilai})}
    )


def _symbol_dari_nama(path: str) -> str:
    """Tebak simbol dari nama berkas, mis. ``btcusdt_15m_5bulan.csv.gz`` → BTCUSDT."""
    nama = Path(path).name.split(".")[0]
    return nama.split("_")[0].upper()


def _muat_funding(path: Path) -> Any:
    """Muat data funding historis dari berkas JSON/JSONL."""
    from volumatic_bot.backtest.runner import muat_funding

    return muat_funding(path)


def _samakan_interval(candles: Any, target: str) -> Any:
    """Resample candle bila interval data lebih kecil dari ``scanner.timeframe``.

    Tanpa ini, berkas 15m bisa dilaporkan sebagai "4h" hanya karena config
    berubah, padahal mesin tetap memakai bar 15m: label salah dan ATR/SL ikut
    salah skala. Data yang lebih besar dari target ditolak dengan pesan jelas.
    """
    from volumatic_bot.binance.urls import INTERVAL_MS
    from volumatic_bot.data.loaders import resample_candles, tebak_interval

    asal = tebak_interval(candles)
    if asal == target:
        return candles
    if asal is None:
        print(
            "PERINGATAN: interval data tidak bisa ditebak; dianggap sama dengan "
            f"scanner.timeframe ({target}). Periksa apakah berkas berisi bar {target}.",
            file=sys.stderr,
        )
        return candles
    if INTERVAL_MS[asal] > INTERVAL_MS[target]:
        print(
            f"data {asal} lebih besar dari scanner.timeframe {target}: "
            f"tidak bisa dipecah ke interval lebih kecil. Sediakan data {target}.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    hasil = resample_candles(candles, target)
    print(
        f"(data {asal} di-resample ke {target}: {len(candles)} bar -> {len(hasil)} bar lengkap)"
    )
    return hasil


def _cmd_backtest(args: argparse.Namespace) -> int:
    """Backtest tanpa jaringan: candle historis + model biaya eksplisit."""
    import json

    from volumatic_bot.backtest import BacktestConfig, CostModel, run_backtest
    from volumatic_bot.binance.symbols import parse_exchange_info

    config, secrets = _load(args)
    config = _terapkan_override_risiko(config, args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())

    candles = load_candles_csv(args.candles, max_rows=args.rows)
    candles = _samakan_interval(candles, config.scanner.timeframe)
    info_all = parse_exchange_info(json.loads(Path(args.exchange_info).read_text(encoding="utf-8")))
    simbol = (args.symbol or _symbol_dari_nama(args.candles)).upper()
    try:
        info = info_all.get(simbol)
    except KeyError:
        print(
            f"simbol {simbol} tidak ada di {args.exchange_info}; "
            f"tersedia: {', '.join(sorted(info_all.symbols))}",
            file=sys.stderr,
        )
        return 1

    biaya = CostModel(
        taker_bps=args.taker_bps if args.taker_bps is not None else config.commission.taker_bps,
        slippage_bps=(
            args.slippage_bps if args.slippage_bps is not None else config.commission.slippage_bps
        ),
        funding_rate_8h_pct=(
            args.funding_pct
            if args.funding_pct is not None
            else config.commission.funding_rate_8h_pct
        ),
    )
    funding = _muat_funding(Path(args.funding_file)) if args.funding_file else None

    hasil = run_backtest(
        candles,
        info,
        config,
        bt=BacktestConfig(
            equity=args.equity,
            leverage=args.leverage,
            partial_fraction=max(0.0, min(1.0, args.partial_pct / 100.0)),
            warmup_bars=args.warmup,
            max_bars_in_trade=args.max_bars,
        ),
        costs=biaya,
        funding=funding,
        interval=config.scanner.timeframe,
    )
    _cetak_backtest(hasil, args.trades, sumber_funding=funding is not None)
    if args.json:
        _tulis_backtest_json(Path(args.json), hasil, biaya)
    return 0


def _cetak_backtest(hasil: Any, jumlah_trade: int, *, sumber_funding: bool) -> None:
    print(hasil.summary())
    if hasil.skipped:
        print(f"\n{len(hasil.skipped)} entry dilewati oleh aturan risiko (contoh):")
        for baris in hasil.skipped[:5]:
            print(f"  - {baris}")
    if hasil.notes:
        print("\nCatatan:")
        for baris in hasil.notes:
            print(f"  - {baris}")
    print(f"\nAlasan keluar: {hasil.reasons() or '-'}")
    asal = "data" if sumber_funding else "asumsi"
    print(f"Sumber funding: {hasil.funding_source} ({asal})")
    if hasil.trades:
        print(f"\n{min(jumlah_trade, hasil.count)} trade terakhir:")
        print(
            "  simbol     arah   masuk      keluar     qty        alasan        "
            "bruto     biaya     bersih    R"
        )
        for t in hasil.trades[-jumlah_trade:]:
            print(
                f"  {t.symbol:<10} {t.side:<6} {t.entry_price:<10.6g} {t.exit_price:<10.6g} "
                f"{t.qty:<10.4g} {t.reason:<13} {t.gross_usdt:>+8.3f} "
                f"{t.costs.total_usdt:>8.3f} {t.net_usdt:>+8.3f} {t.r_multiple:>+6.2f}"
            )
    print("\nIngat: hasil ini hanya untuk data yang diberikan; bukan jaminan kinerja masa depan.")


def _tulis_backtest_json(path: Path, hasil: Any, biaya: Any) -> None:
    import json

    payload = {
        "symbol": hasil.symbol,
        "interval": hasil.interval,
        "bars": hasil.bars,
        "periode": {"dari_ms": hasil.first_ms, "sampai_ms": hasil.last_ms},
        "ekuitas": {"awal": hasil.start_equity, "akhir": hasil.end_equity},
        "statistik": {
            "trade": hasil.count,
            "menang": hasil.wins,
            "kalah": hasil.losses,
            "win_rate_pct": round(hasil.win_rate, 2),
            "bersih_usdt": round(hasil.net_usdt, 4),
            "bruto_usdt": round(hasil.gross_usdt, 4),
            "fee_usdt": round(hasil.fees_usdt, 4),
            "slippage_usdt": round(hasil.slippage_usdt, 4),
            "funding_usdt": round(hasil.funding_usdt, 4),
            "return_pct": round(hasil.return_pct, 3),
            "avg_r": round(hasil.avg_r, 3),
            "profit_factor": hasil.profit_factor,
            "max_drawdown_pct": round(hasil.max_drawdown_pct, 3),
            "alasan_keluar": hasil.reasons(),
        },
        "biaya": {
            "taker_bps": biaya.taker_bps,
            "slippage_bps": biaya.slippage_bps,
            "funding_rate_8h_pct": biaya.funding_rate_8h_pct,
            "funding_dari_data": hasil.funding_available,
        },
        "catatan": hasil.notes,
        "trade": [t.as_row() for t in hasil.trades],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Hasil lengkap disimpan: {path}")


def logger_info() -> Any:
    """Fungsi logger sederhana (dipakai eksekutor saat CLI)."""
    import logging

    return logging.getLogger("volumatic_bot").info


def _cmd_simulate_session(args: argparse.Namespace) -> int:
    """Jalankan mesin penuh (risiko + eksekusi + notifikasi) pada data historis."""
    import json
    import logging

    from volumatic_bot.binance.symbols import parse_exchange_info
    from volumatic_bot.engine import print_session_report, run_offline_session
    from volumatic_bot.engine.trader import TradingEngine
    from volumatic_bot.execution.broker import DryRunBroker
    from volumatic_bot.execution.executor import TradeExecutor
    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.risk.limits import DailyLimits, DailyState
    from volumatic_bot.risk.manager import RiskManager
    from volumatic_bot.storage.db import SqliteDatabase

    config, secrets = _load(args)
    config = _terapkan_override_risiko(config, args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())

    candles = load_candles_csv(args.candles)
    candles = _samakan_interval(candles, config.scanner.timeframe)
    info_all = parse_exchange_info(json.loads(Path(args.exchange_info).read_text(encoding="utf-8")))
    simbol = (args.symbol or _symbol_dari_nama(args.candles)).upper()
    try:
        info = info_all.get(simbol)
    except KeyError:
        print(f"simbol {simbol} tidak ada di {args.exchange_info}", file=sys.stderr)
        return 1

    db = SqliteDatabase(config.db_posisi())
    db.open()
    try:
        store = PositionStore(db)
        broker = DryRunBroker(taker_bps=config.commission.taker_bps)
        zona = _now_sadar_zona(config.app.timezone)
        limits = DailyLimits(
            daily_loss_limit_pct=config.risk.daily_loss_limit_pct,
            max_trades_per_day=config.risk.max_trades_per_day,
            consecutive_loss_limit=config.risk.consecutive_loss_limit,
            cooldown_minutes=config.risk.cooldown_minutes,
            reset_hour_local=config.risk.daily_loss_reset_hour_local,
            timezone=config.app.timezone,
        )
        harian = DailyState(limits=limits, start_equity=args.equity, current_equity=args.equity)
        mgr = RiskManager(config, daily=harian, clock=zona)
        executor = TradeExecutor(
            config,
            broker,
            store,
            clock=lambda: int(_time.time() * 1000),
            logger=logger_info(),
            mode="dry_run",
            filters_of=lambda simbol: info.filters,
        )
        engine = TradingEngine(
            config,
            store=store,
            risk=mgr,
            executor=executor,
            broker=broker,
            notifier=_build_notifier(config, logging.getLogger("volumatic_bot")),
        )
        engine.set_symbol_info(info)

        hasil = run_offline_session(
            series=candles,
            config=config,
            engine=engine,
            store=store,
            broker=broker,
            info=info,
            limit_bars=args.limit_bars,
            step_pct=args.step,
        )
        print_session_report(hasil)
        if args.json:
            Path(args.json).write_text(
                json.dumps(hasil, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"Laporan sesi disimpan: {args.json}")
        return 0
    finally:
        db.close()


def _petunjuk_error(exc: BaseException) -> str:
    """Terjemahkan error jaringan/API menjadi saran tindakan yang bisa dilakukan."""
    from volumatic_bot.binance.errors import BinanceAPIError, describe_http_status

    status = getattr(exc, "status", None)
    kode = getattr(exc, "code", None)
    if status == 451:
        return (
            "Binance menolak permintaan dari lokasi/IP ini (HTTP 451). Ini blokir "
            "geografis dari pihak Binance, bukan salah konfigurasi.\n"
            "  Solusi: jalankan bot di VPS di negara yang dilayani Binance "
            "(mis. Singapura, Tokyo, Frankfurt). Mengganti API key tidak membantu."
        )
    if status == 403 or kode == -2015:
        return (
            "Kunci/izin ditolak (HTTP 403 atau kode -2015).\n"
            "  Periksa: (1) API key benar, (2) IP server ada di whitelist Binance, "
            "(3) izin 'Enable Futures' aktif, (4) kunci belum kedaluwarsa."
        )
    if status == 401 or kode in (-1022,):
        return "Signature/API key salah (HTTP 401). Periksa BINANCE_API_SECRET di .env."
    if kode == -1021:
        return (
            "Timestamp ditolak server (-1021). Jam komputer melenceng.\n"
            "  Linux: sudo timedatectl set-ntp true"
        )
    if status == 418:
        return (
            "IP diblokir sementara oleh Binance (HTTP 418). Hentikan bot, tunggu, jangan mengulang."
        )
    if status == 429:
        return "Rate limit (HTTP 429). Kurangi jumlah simbol atau frekuensi permintaan."
    if status is not None:
        return f"Binance membalas HTTP {status}: {describe_http_status(status)}."
    if isinstance(exc, BinanceAPIError):
        return "Permintaan ke Binance gagal; lihat pesan di atas."
    return (
        "Tidak bisa menghubungi Binance sama sekali (DNS/jaringan/proxy).\n"
        "  Periksa: koneksi internet, DNS, firewall keluar port 443, dan jam sistem."
    )


def _cetak_gagal_binance(exc: BaseException, *, url: str = "", saran_netcheck: bool = True) -> None:
    """Cetak kegagalan jaringan/API tanpa traceback yang menakutkan."""
    from volumatic_bot.logging_setup import sanitize

    if url:
        print(f"endpoint : {url}", file=sys.stderr)
    print(f"galat    : {type(exc).__name__}", file=sys.stderr)
    print(f"pesan    : {sanitize(str(exc))}", file=sys.stderr)
    print("", file=sys.stderr)
    print("kemungkinan penyebab:", file=sys.stderr)
    for baris in _petunjuk_error(exc).splitlines():
        print(f"  {baris}" if not baris.startswith("  ") else baris, file=sys.stderr)
    if saran_netcheck:
        print("", file=sys.stderr)
        print("Jalankan `volumatic-bot netcheck` untuk diagnosis cepat.", file=sys.stderr)


def _host_alternatif(config: AppConfig) -> str | None:
    """Host testnet REST pengganti (demo-fapi <-> testnet.binancefuture).

    Binance menyediakan dua host testnet. Pada 14 Nov 2026, ``demo-fapi`` diblokir
    dari beberapa jaringan (HTTP 451/403) sementara ``testnet.binancefuture.com``
    menjawab normal — jadi bot perlu memberi tahu pengguna host mana yang hidup,
    bukan sekadar melaporkan gagal.
    """
    from volumatic_bot.binance.urls import Endpoint

    if not config.exchange.testnet or config.net.rest_base_url:
        return None
    sekarang = config.net.resolve_rest_url(testnet=True)
    kandidat = [Endpoint.TESTNET_REST, Endpoint.LEGACY_TESTNET_REST]
    for url in kandidat:
        if url != sekarang:
            return url
    return None


async def _uji_host(url: str, secrets: Any, config: AppConfig) -> tuple[bool, str]:
    """Coba ``/fapi/v1/ping`` pada satu host; kembalikan (berhasil, keterangan)."""
    from volumatic_bot.binance.rest import BinanceREST

    klien = BinanceREST(
        base_url=url,
        testnet=True,
        api_key=None,
        api_secret=None,
        timeout=min(8.0, config.exchange.request_timeout_sec),
        max_retries=0,
        weight_budget_per_min=config.exchange.weight_budget_per_min,
    )
    await klien.start()
    try:
        await klien.ping()
        return True, "ping OK"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {getattr(exc, 'status', '') or exc}"
    finally:
        with contextlib.suppress(Exception):
            await klien.close()


async def _periksa_ip(config: AppConfig, secrets: Any, *, uji_binance: bool) -> Any:
    """Baca IP publik lalu (opsional) uji penerimaan Binance. Tanpa mencetak apa pun."""
    from volumatic_bot.net.ipcheck import ambil_ip_publik, periksa_binance, sumber_dari_url

    sumber = tuple(sumber_dari_url(u) for u in config.ipcheck.sumber)
    laporan = await ambil_ip_publik(sumber, timeout_sec=config.ipcheck.timeout_sec)
    if uji_binance:
        rest = _make_rest(config, secrets)
        await rest.start()
        try:
            await periksa_binance(laporan, rest=rest)
        finally:
            with contextlib.suppress(Exception):
                await rest.close()
    return laporan


def _cmd_ip(args: argparse.Namespace) -> int:
    """Tampilkan IP egress dan apakah Binance menerimanya.

    Perintah ini tidak mengirim order dan tidak butuh API key: endpoint yang
    dipakai (ping, server time) publik.
    """
    from volumatic_bot.net.ipcheck import ringkas_laporan, wajib_layak

    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())
    if not config.ipcheck.enabled:
        print(
            "ipcheck.enabled: false di config, jadi deteksi IP dimatikan. "
            "Perintah ini tetap jalan karena Anda memintanya secara eksplisit.",
            file=sys.stderr,
        )
    url = config.net.resolve_rest_url(testnet=config.exchange.testnet)
    print(f"mode         : {config.ops.mode} (testnet={config.exchange.testnet})")
    print(f"target REST  : {url}")
    uji = not args.tanpa_binance
    if uji:
        print("menguji Binance: ping + server time (bobot 1, tanpa API key)")
    laporan = asyncio.run(_periksa_ip(config, secrets, uji_binance=uji))
    print("")
    for baris in ringkas_laporan(laporan):
        print(baris)
    print("")
    if uji:
        layak, alasan = wajib_layak(laporan, wajib_terbaca=config.ipcheck.wajib_terbaca)
        status = "LAYAK" if layak else "TIDAK LAYAK"
        print(f"kesimpulan   : {status} — {alasan}")
        return 0 if layak else 1
    print("kesimpulan   : IP terbaca; status terhadap Binance tidak diuji (--tanpa-binance)")
    return 0 if laporan.terbaca else 1


def _cmd_netcheck(args: argparse.Namespace) -> int:
    """Cek koneksi ke Binance langkah demi langkah (tanpa mengirim order).

    Berguna ketika ``run`` gagal: perintah ini memisahkan masalah jaringan,
    masalah lokasi/IP, masalah kunci, dan masalah izin.
    """
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())
    url = config.net.resolve_rest_url(testnet=config.exchange.testnet)
    ws = config.net.resolve_ws_url(testnet=config.exchange.testnet)

    print(f"konfigurasi : mode={config.ops.mode} testnet={config.exchange.testnet}")
    print(f"REST        : {url}")
    print(f"WebSocket   : {ws}")
    print(f"kredensial  : {'ada' if secrets.has_api_credentials else 'BELUM DISET'}")
    print("")
    if config.ipcheck.enabled:
        from volumatic_bot.net.ipcheck import ringkas_laporan

        laporan = asyncio.run(_periksa_ip(config, secrets, uji_binance=False))
        for baris in ringkas_laporan(laporan):
            print(baris)
        print("")

    gagal = 0

    async def jalan() -> int:
        nonlocal gagal
        rest = _make_rest(config, secrets)
        nol = 0
        await rest.start()
        try:
            # 1. ping (publik, bobot 1)
            try:
                await rest.ping()
                print("[OK ] ping REST")
            except Exception as exc:
                nol += 1
                print(f"[GAGAL] ping REST -> {type(exc).__name__}")
                _cetak_gagal_binance(exc, url=url, saran_netcheck=False)
                lain = _host_alternatif(config)
                if lain:
                    hidup, ket = await _uji_host(lain, secrets, config)
                    print("", file=sys.stderr)
                    if hidup:
                        nilai = "true" if "binancefuture" in lain else "false"
                        print(
                            f"[PETUNJUK] host testnet ALTERNATIF hidup: {lain}\n"
                            f"  Ubah config.yaml -> net.legacy_testnet_rest: {nilai}",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"[PETUNJUK] host alternatif {lain} juga gagal ({ket}).\n"
                            "  Kemungkinan jaringan/lokasi Anda memblokir Binance.",
                            file=sys.stderr,
                        )
                return nol

            # 2. exchangeInfo (publik, bobot 1)
            try:
                payload = await rest.exchange_info()
                jumlah = len(payload.get("symbols", []))
                print(f"[OK ] exchangeInfo ({jumlah} simbol)")
            except Exception as exc:
                nol += 1
                print(f"[GAGAL] exchangeInfo -> {type(exc).__name__}")
                _cetak_gagal_binance(exc, url=url, saran_netcheck=False)
                return nol

            # 3. jam server vs jam lokal (publik) -> penting untuk signature
            try:
                await rest.sync_time()
                print("[OK ] sinkronisasi waktu server")
            except Exception as exc:
                print(f"[LEWAT] sinkronisasi waktu: {type(exc).__name__}")

            if not secrets.has_api_credentials:
                print("[LEWAT] saldo & izin: kredensial belum diset (isi .env)")
                return nol

            # 4. saldo (butuh signature)
            try:
                saldo = await rest.saldo_usdt() if hasattr(rest, "saldo_usdt") else None
                if saldo is None:
                    from volumatic_bot.binance.trading import TradingAPI

                    saldo = await TradingAPI(
                        rest, conditional_api=config.exchange.conditional_order_api
                    ).balance_usdt()
                print(f"[OK ] tanda tangan diterima; saldo futures {saldo:.2f} USDT")
            except Exception as exc:
                nol += 1
                print(f"[GAGAL] saldo akun -> {type(exc).__name__}")
                _cetak_gagal_binance(exc, url=url, saran_netcheck=False)
            return nol
        finally:
            with contextlib.suppress(Exception):
                await rest.close()

    gagal = asyncio.run(jalan())
    print("")
    if gagal:
        print("HASIL: ada pemeriksaan yang gagal — perbaiki dulu sebelum `run`.")
        return 1
    print("HASIL: koneksi ke Binance siap. Ingat: ini BUKAN jaminan profit.")
    return 0


def _proses_hidup(pid: int) -> bool:
    """True bila PID masih berjalan (mengabaikan zombie)."""
    from volumatic_bot.procutil import proses_hidup

    return proses_hidup(pid)


def _kirim_sinyal(pid: int, sig: int) -> bool:
    """Kirim sinyal ke proses; False bila gagal (mis. bukan milik kita)."""
    from volumatic_bot.procutil import kirim_sinyal

    return kirim_sinyal(pid, sig)


def _berkas_pid(config: AppConfig, *, semua: bool) -> list[Path]:
    """Kumpulkan berkas PID yang relevan (mode aktif atau semua mode)."""
    aktif = Path(config.pid_file())
    if not semua:
        return [aktif] if aktif.exists() else []
    folder = Path(config.db_posisi()).parent
    return sorted(folder.glob("bot*.pid")) if folder.exists() else []


def _cmd_stop(args: argparse.Namespace) -> int:
    """Hentikan bot yang sedang berjalan (tanpa perlu ``ps | grep``).

    Dua cara, dipilih lewat flag:

    * bawaan — kirim SIGTERM. Bot menutup koneksi rapi, **posisi tetap terbuka**
      dan SL/TP tetap aktif di bursa;
    * ``--close`` — pasang file kill switch lebih dulu, sehingga bot menutup
      semua posisi market lalu berhenti sendiri (paling aman bila pasar liar).
    """
    import signal

    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())

    berkas = _berkas_pid(config, semua=args.all_modes)
    if not berkas:
        print(
            "tidak ada berkas PID — kemungkinan bot tidak berjalan, atau dijalankan "
            "dari versi lama. Periksa manual: ps -ef | grep volumatic-bot"
        )
        return 1

    if args.close:
        jalur = Path(config.ops.kill_switch_file)
        jalur.write_text("kill switch (stop --close)\n", encoding="utf-8")
        print(f"kill switch DIPASANG: {jalur.resolve()} (bot akan menutup posisi market)")

    gagal = 0
    for jalur_pid in berkas:
        try:
            isi = jalur_pid.read_text(encoding="utf-8").split()
            pid = int(isi[0])
        except (OSError, ValueError, IndexError):
            print(f"[LEWAT] {jalur_pid} tidak terbaca / rusak")
            continue
        if not _proses_hidup(pid):
            print(f"[BERSIH] {jalur_pid.name}: PID {pid} sudah tidak ada (berkas usang)")
            with contextlib.suppress(OSError):
                jalur_pid.unlink()
            continue

        print(f"[HENTIKAN] {jalur_pid.name}: PID {pid}")
        if not _kirim_sinyal(pid, signal.SIGTERM):
            print("  gagal mengirim sinyal (proses milik user lain?)")
            gagal += 1
            continue

        batas = args.timeout
        while batas > 0 and _proses_hidup(pid):
            _time.sleep(0.5)
            batas -= 0.5
        if not _proses_hidup(pid):
            print("  berhenti.")
            with contextlib.suppress(OSError):
                jalur_pid.unlink()
            continue
        if args.force:
            print("  masih hidup setelah batas waktu -> SIGKILL")
            _kirim_sinyal(pid, signal.SIGKILL)
            _time.sleep(1.0)
            if not _proses_hidup(pid):
                with contextlib.suppress(OSError):
                    jalur_pid.unlink()
                continue
        print(f"  MASIH BERJALAN. Gunakan: kill -9 {pid}")
        gagal += 1

    if gagal:
        print(f"\n{gagal} proses belum berhenti. Periksa: ps -ef | grep volumatic-bot")
        return 1
    print("\nSelesai. Periksa posisi & state: volumatic-bot positions")
    return 0


def _cmd_kill_switch(args: argparse.Namespace) -> int:
    """Pasang/hapus file kill switch (tombol darurat, tanpa jaringan).

    Bot yang sedang berjalan memeriksa file ini tiap ``ops.kill_switch_poll_sec``
    detik: begitu ada, semua posisi ditutup market lalu sesi berhenti.
    """
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())
    jalur = Path(config.ops.kill_switch_file)

    if args.status:
        if jalur.exists():
            print(f"kill switch AKTIF: {jalur.resolve()}")
            print("Hapus dengan: volumatic-bot kill-switch --clear")
        else:
            print(f"kill switch tidak aktif ({jalur} tidak ada)")
        return 1 if jalur.exists() else 0

    if args.clear:
        if jalur.exists():
            jalur.unlink()
            print(f"kill switch dihapus ({jalur}). Sesi baru boleh berjalan.")
            return 0
        print(f"tidak ada yang dihapus: {jalur} memang tidak ada")
        return 0

    jalur.parent.mkdir(parents=True, exist_ok=True)
    jalur.write_text(
        f"kill switch dipasang {_time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8"
    )
    print(f"kill switch DIPASANG: {jalur.resolve()}")
    print(
        "Bot yang berjalan akan menutup semua posisi market lalu berhenti "
        f"(diperiksa tiap {config.ops.kill_switch_poll_sec:g} detik)."
    )
    print("Periksa hasilnya: volumatic-bot positions")
    return 0


def _lolos_pemeriksaan_ip(
    config: AppConfig, secrets: Any, *, mode: str
) -> tuple[bool, Any]:
    """Gerbang IP sebelum ``run`` mengirim order.

    Fail-closed untuk mode testnet/live: bila IP tidak terbukti diterima
    Binance, sesi tidak dimulai. Alasannya praktis, bukan teoretis: dengan IP
    yang ditolak, order pertama akan gagal setelah bot menghangatkan puluhan
    candle, dan pada mode ``live`` kegagalan bisa terjadi tepat saat posisi
    butuh SL. Lebih murah berhenti di sini.
    """
    from volumatic_bot.net.ipcheck import ringkas_laporan, wajib_layak

    if not config.ipcheck.enabled or not config.ipcheck.cek_saat_menjalankan:
        return True, None
    print("memeriksa IP egress sebelum mode " + mode + "...", flush=True)
    laporan = asyncio.run(_periksa_ip(config, secrets, uji_binance=True))
    for baris in ringkas_laporan(laporan):
        print("  " + baris, flush=True)
    if not config.ipcheck.wajib_layak:
        print("  catatan: ipcheck.wajib_layak=false, sesi diteruskan apa pun hasilnya", flush=True)
        return True, laporan
    layak, alasan = wajib_layak(laporan, wajib_terbaca=config.ipcheck.wajib_terbaca)
    if layak:
        print("  IP layak: sesi dilanjutkan.", flush=True)
        return True, laporan
    print("", file=sys.stderr)
    print(f"sesi DIBATALKAN: {alasan}", file=sys.stderr)
    print(
        "Perbaiki IP/whitelist lebih dulu, atau jalankan `volumatic-bot ip` untuk "
        "melihat rinciannya. Bila Anda memang ingin memaksa jalan, setel "
        "ipcheck.wajib_layak: false (tidak disarankan).",
        file=sys.stderr,
    )
    return False, laporan


def _cmd_run(args: argparse.Namespace) -> int:
    """Jalankan mesin trading dengan data bursa (butuh kredensial & jaringan).

    **Belum pernah dijalankan ke Binance** dari lingkungan pengembangan (HTTP 451).
    Perintah ini menolak jalan pada mode ``dry_run``/``backtest`` supaya tidak ada
    kesan keliru bahwa bot sudah mengirim order nyata.
    """
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())

    if config.ops.mode in ("dry_run", "backtest"):
        print(
            "mode " + config.ops.mode + ": jalur data bursa tidak dipakai. "
            "Gunakan `simulate-session` untuk uji mesin tanpa jaringan, atau "
            "setel ops.mode ke testnet (dengan config.exchange.testnet: true).",
            file=sys.stderr,
        )
        return 2

    lolos_ip, laporan_ip = _lolos_pemeriksaan_ip(config, secrets, mode=config.ops.mode)
    if not lolos_ip:
        return 3

    from volumatic_bot.binance.errors import BinanceAPIError
    from volumatic_bot.engine.live import run_live_session

    try:
        return asyncio.run(
            run_live_session(
                config,
                secrets,
                symbols=args.symbols,
                iterations=args.iterations,
                dashboard=not args.no_dashboard,
                config_path=args.config,
                profil=getattr(args, "profil", None),
                laporan_ip=laporan_ip,
            )
        )
    except ValueError as exc:
        # Kesalahan pemakaian (simbol salah/blacklist, mode tidak cocok): pesan
        # singkat jauh lebih berguna daripada traceback.
        print(f"sesi tidak bisa dijalankan: {exc}", file=sys.stderr)
        return 2
    except (BinanceAPIError, OSError, TimeoutError) as exc:
        # Traceback panjang tidak membantu pengguna; beri tahu apa yang harus
        # diperiksa dan kode keluar yang bisa dicek skrip.
        _cetak_gagal_binance(exc, url=config.net.resolve_rest_url(testnet=config.exchange.testnet))
        return 3


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Tampilkan dashboard ``rich`` (tanpa jaringan; data dari SQLite atau contoh)."""

    from rich.console import Console

    from volumatic_bot.dashboard import (
        DashboardApp,
        LocalStateSource,
        RingBufferHandler,
        demo_state,
    )

    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())

    if not config.dashboard.enabled:
        print("dashboard dimatikan di config (dashboard.enabled: false)", file=sys.stderr)
        return 1

    console = Console()
    compact = args.compact or config.dashboard.compact
    refresh = args.refresh if args.refresh is not None else config.dashboard.refresh_sec
    screen = config.dashboard.layar_penuh and not args.inline

    if args.demo:
        contoh = demo_state()

        def sumber_demo() -> Any:
            return contoh

        app = DashboardApp(
            sumber_demo,
            console=console,
            refresh_sec=refresh,
            compact=compact,
            screen=screen,
            jendela_menit=config.dashboard.log_ulang_jendela_menit,
        )
        if args.iterations is not None and args.iterations > 1 and console.is_terminal:
            return 0 if asyncio.run(app.run(iterations=args.iterations)) else 1
        app.render_once()
        return 0

    # --- data lokal: posisi/insiden dari SQLite + ekor log dari handler khusus
    import logging as _logging

    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.storage.db import SqliteDatabase

    logger = _logging.getLogger("volumatic_bot")
    buffer = RingBufferHandler(maxlen=max(50, config.dashboard.max_log_lines * 4))
    logger.addHandler(buffer)

    db = SqliteDatabase(config.db_posisi())
    db.open()
    try:
        store = PositionStore(db)
        source = LocalStateSource(
            store,
            mode=config.ops.mode,
            max_positions=config.risk.max_concurrent_positions,
            max_trades=config.risk.max_trades_per_day,
            daily_loss_limit_pct=config.risk.daily_loss_limit_pct,
            max_margin_pct=config.risk.max_total_margin_pct,
            history_limit=config.dashboard.history_rows,
            incident_limit=4,
            timezone=config.app.timezone,
            reset_hour_local=config.risk.daily_loss_reset_hour_local,
        )
        source.note = f"database: {config.db_posisi()}"

        def ambil() -> Any:
            source.set_logs(buffer.lines(limit=config.dashboard.max_log_lines))
            return source.snapshot()

        app = DashboardApp(
            ambil,
            console=console,
            refresh_sec=refresh,
            compact=compact,
            screen=screen,
            jendela_menit=config.dashboard.log_ulang_jendela_menit,
        )
        if not args.once and console.is_terminal:
            frames = asyncio.run(app.run(iterations=args.iterations))
            return 0 if frames else 1
        app.render_once()
        return 0
    finally:
        logger.removeHandler(buffer)
        db.close()


def _cmd_reconcile(args: argparse.Namespace) -> int:
    """Samakan catatan bot dengan bursa (butuh API key; untuk testnet/live)."""
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())
    if config.ops.mode == "dry_run" and not args.force:
        print(
            "Mode dry_run: tidak ada posisi nyata untuk direkonsiliasi. "
            "Pakai --force untuk tetap memeriksa database lokal.",
            file=sys.stderr,
        )
        return 2
    if not secrets.has_api_credentials and config.ops.mode != "dry_run":
        print("Butuh BINANCE_API_KEY dan BINANCE_API_SECRET.", file=sys.stderr)
        return 2

    async def run() -> int:
        from volumatic_bot.binance.trading import TradingAPI
        from volumatic_bot.execution.broker import LiveBroker
        from volumatic_bot.execution.reconcile import reconcile
        from volumatic_bot.positions.store import PositionStore
        from volumatic_bot.storage.db import SqliteDatabase

        rest = _make_rest(config, secrets)
        await rest.start()
        db = SqliteDatabase(config.db_posisi())
        db.open()
        try:
            await rest.sync_time()
            store = PositionStore(db)
            broker = LiveBroker(
                TradingAPI(rest, conditional_api=config.exchange.conditional_order_api)
            )
            laporan = await reconcile(
                broker=broker,
                store=store,
                symbols=args.symbols,
                mode=config.ops.mode,
            )
            print(laporan.summary())
            _print_metric_summary(rest.metrics, 0)
            return 0 if laporan.ok else 1
        finally:
            db.close()
            await rest.close()

    return asyncio.run(run())


def _cmd_preflight(args: argparse.Namespace) -> int:
    """Pemeriksaan awal tanpa jaringan: apakah aman menjalankan bot di mesin ini."""
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())
    baris: list[tuple[str, bool, str]] = []

    # Deteksi profil otomatis memakai equity yang diberikan eksplisit, supaya
    # hasil preflight mencerminkan profil yang akan dipakai `run` nantinya.
    profil_manual = profil_aktif_dari(baca_yaml(args.config), getattr(args, "profil", None))
    profil_otomatis = ""
    if args.equity is not None and not profil_manual:
        profil_otomatis = (
            deteksi_profil_otomatis(
                config, args.config, profil_cli=getattr(args, "profil", None), saldo=args.equity
            )
            or ""
        )
        if profil_otomatis:
            config = load_config(args.config, profil=profil_otomatis)
    baris.append(
        (
            "profil",
            True,
            (profil_manual or f"{profil_otomatis} (otomatis dari equity)")
            if (profil_manual or profil_otomatis)
            else "(tanpa profil — nilai biasa config)",
        )
    )

    mode = config.ops.mode
    baris.append(
        (
            "mode operasi",
            mode in ("dry_run", "backtest") or (config.ops.allow_live and args.ie_understand_risk),
            f"mode={mode}"
            + ("" if mode in ("dry_run", "backtest") else " (pastikan Anda paham risikonya)"),
        )
    )
    baris.append(
        (
            "konfirmasi live",
            mode != "live" or config.ops.live_confirmation == "SAYA-MENGERTI-RISIKONYA",
            "string konfirmasi live sesuai/`mode` bukan live",
        )
    )
    kill_path = Path(config.ops.kill_switch_file)
    baris.append(
        (
            "kill switch",
            not kill_path.exists(),
            f"file {kill_path} "
            + ("ADA (bot akan menolak membuka posisi)" if kill_path.exists() else "tidak ada"),
        )
    )
    baris.append(
        (
            "leverage",
            config.exchange.max_leverage <= 10,
            f"default {config.exchange.default_leverage}x, maks {config.exchange.max_leverage}x",
        )
    )
    baris.append(
        (
            "risiko per trade",
            config.risk.risk_per_trade_pct <= 2.0,
            f"{config.risk.risk_per_trade_pct}% equity",
        )
    )
    baris.append(
        (
            "batas harian",
            config.risk.daily_loss_limit_pct <= 5,
            f"rugi harian {config.risk.daily_loss_limit_pct}% | "
            f"maks {config.risk.max_trades_per_day} trade | cooldown "
            f"{config.risk.cooldown_minutes} menit",
        )
    )
    baris.append(
        (
            "margin",
            config.risk.max_total_margin_pct <= 70,
            f"maks {config.risk.max_total_margin_pct}% equity, konsentrasi "
            f"{config.risk.max_per_symbol_margin_pct}%/simbol",
        )
    )
    if args.equity is not None:
        cukup = args.equity >= config.risk.min_equity_usdt
        baris.append(
            (
                "equity",
                cukup,
                f"{args.equity:.2f} USDT (minimum {config.risk.min_equity_usdt:.2f})"
                + (
                    ""
                    if args.equity >= config.risk.small_capital_usdt
                    else " — kategori modal kecil"
                ),
            )
        )
    perlu_kredensial = mode in ("testnet", "live")
    baris.append(
        (
            "kredensial API",
            secrets.has_api_credentials or not perlu_kredensial,
            "tersedia"
            if secrets.has_api_credentials
            else (
                f"belum diset (wajib untuk {mode})"
                if perlu_kredensial
                else f"belum diset (tidak wajib untuk mode {mode})"
            ),
        )
    )
    db_dir = Path(config.db_posisi()).parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
        tulis_ok = True
    except OSError:
        tulis_ok = False
    baris.append(
        (
            "folder data",
            tulis_ok,
            f"{db_dir} {'bisa ditulis' if tulis_ok else 'TIDAK bisa ditulis'}",
        )
    )
    baris.append(
        (
            "order book",
            True,
            f"{'aktif' if config.orderbook.enabled else 'dimatikan'} "
            f"(sumber {config.orderbook.source}, tolak bila depth hilang="
            f"{config.orderbook.reject_if_unavailable})",
        )
    )
    baris.append(
        (
            "deteksi IP",
            True,
            "aktif saat run testnet/live"
            + (" (WAJIB layak: sesi dibatalkan bila tidak)" if config.ipcheck.wajib_layak else "")
            + ("" if config.ipcheck.enabled else " — DIMATIKAN di config"),
        )
    )
    cache_dir = Path(config.db_cache()).parent
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_ok = True
    except OSError:
        cache_ok = False
    baris.append(
        (
            "database",
            cache_ok,
            f"posisi: {config.db_posisi()} | cache: {config.db_cache()}"
            + ("" if config.data.separate_by_mode else " (per-mode DIMATIKAN)"),
        )
    )
    lama_db = Path(config.data.db_path)
    if config.db_posisi() != str(lama_db) and lama_db.exists():
        baris.append(
            (
                "db versi lama",
                True,
                f"{lama_db} ada. Sejak database dipisah per mode, berkas yang dipakai "
                f"{config.db_posisi()}. Kalau isi {lama_db.name} berasal dari testnet, "
                f"pindahkan: mv {lama_db} {config.db_posisi()}",
            )
        )
    baris.append(("fingerprint config", True, config.fingerprint()))

    print("Pemeriksaan awal (tanpa jaringan)")
    print("-" * 62)
    for nama, ok, detail in baris:
        print(f"[{'OK ' if ok else 'CEK'}] {nama:<18} {detail}")
    gagal = [b for b in baris if not b[1]]
    print("-" * 62)
    if gagal:
        print(f"{len(gagal)} butir perlu diperiksa. Bot menolak jalan sampai beres.")
        return 1
    print("Semua pemeriksaan lolos. Catatan: ini BUKAN jaminan profit.")
    return 0


def _cmd_sizing(args: argparse.Namespace) -> int:
    """Hitung ukuran posisi & level trade dari data simbol asli (butuh jaringan)."""
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())

    async def run() -> int:
        from volumatic_bot.binance.symbols import parse_exchange_info
        from volumatic_bot.risk import DailyState, Portfolio, RiskManager
        from volumatic_bot.risk.limits import DailyLimits

        rest = _make_rest(config, secrets)
        await rest.start()
        try:
            await rest.sync_time()
            info = parse_exchange_info(await rest.exchange_info())
            item = info.symbols.get(args.symbol.upper())
            if item is None:
                print(f"{args.symbol} tidak ada di exchangeInfo", file=sys.stderr)
                return 1
            atr = args.entry * args.atr_pct / 100.0
            trail = args.trail
            limits = DailyLimits(
                daily_loss_limit_pct=config.risk.daily_loss_limit_pct,
                max_trades_per_day=config.risk.max_trades_per_day,
                consecutive_loss_limit=config.risk.consecutive_loss_limit,
                cooldown_minutes=config.risk.cooldown_minutes,
                reset_hour_local=config.risk.daily_loss_reset_hour_local,
                timezone=config.app.timezone,
            )
            mgr = RiskManager(
                config,
                daily=DailyState(
                    limits=limits, start_equity=args.equity, current_equity=args.equity
                ),
                clock=_now_sadar_zona(config.app.timezone),
            )
            hasil = mgr.evaluate(
                symbol=item.symbol,
                side=args.side,
                entry_price=args.entry,
                atr=atr,
                trail=trail,
                equity=args.equity,
                portfolio=Portfolio(
                    equity=args.equity,
                    positions=(),
                    max_total_margin_pct=config.risk.max_total_margin_pct,
                    max_per_symbol_margin_pct=config.risk.max_per_symbol_margin_pct,
                ),
                info=item,
                open_positions=args.open_positions,
            )
            print(f"Simbol     : {item.symbol} ({args.side})")
            print(
                f"Filter     : tick={item.filters.tick_size} step={item.filters.step_size} "
                f"minQty={item.filters.min_qty} minNotional={item.filters.min_notional}"
            )
            print(
                f"Asumsi     : equity={args.equity:.2f} USDT, entry={args.entry:g}, "
                f"ATR={args.atr_pct:g}% ({atr:g})"
            )
            print("-" * 62)
            print(hasil.summary())
            for note in hasil.notes:
                print(f"  - {note}")
            if hasil.plan is not None:
                plan = hasil.plan
                print(
                    f"Level      : SL={plan.stop_price} ({plan.sl_source}) "
                    f"TP1={plan.tp1_price} BE={plan.be_price} "
                    f"trail_aktif={plan.trailing_activation_price}"
                )
                print(
                    f"Partial TP : {plan.partial_qty} / sisa {plan.remaining_qty}"
                    f" | R = {plan.risk_per_unit} per unit"
                )
            _print_metric_summary(rest.metrics, 0)
            return 0 if hasil.approved else 1
        finally:
            await rest.close()

    return asyncio.run(run())


def _now_sadar_zona(nama_zona: str) -> Any:
    """Pengembalikan jam ``datetime.now`` dalam zona waktu aplikasi."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zona = ZoneInfo(nama_zona)
    return lambda: datetime.now(zona)


def _cmd_book(args: argparse.Namespace) -> int:
    """Periksa order book dan tampilkan keputusan filter (tanpa mengirim order)."""
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())

    async def run() -> int:
        from volumatic_bot.binance.symbols import parse_exchange_info
        from volumatic_bot.orderbook.depth import fetch_snapshot_safe
        from volumatic_bot.orderbook.gate import evaluate

        rest = _make_rest(config, secrets)
        await rest.start()
        try:
            await rest.sync_time()
            info = parse_exchange_info(await rest.exchange_info())
            source = _make_depth_source(config, rest)
            print(
                f"Sumber depth: {config.orderbook.source} | limit="
                f"{config.orderbook.depth_limit} | TTL="
                f"{config.orderbook.depth_snapshot_ttl_sec}s | arah uji: {args.side}"
            )
            exit_code = 0
            for raw_symbol in args.symbols:
                symbol = raw_symbol.upper()
                item = info.symbols.get(symbol)
                if item is None:
                    print(f"{symbol}: tidak ada di exchangeInfo, dilewati")
                    exit_code = 1
                    continue
                snapshot, failure = await fetch_snapshot_safe(source, symbol)
                mid = snapshot.mid if snapshot is not None else 0.0
                price = mid if mid > 0 else float(item.filters.min_notional)
                atr = price * args.atr_pct / 100.0 if args.atr_pct > 0 else 0.0
                notional = args.notional or config.orderbook.target_notional_usdt
                qty = max(float(item.filters.min_qty), notional / max(price, 1e-12))
                decision = evaluate(
                    snapshot,
                    direction=args.side,
                    tick_size=float(item.filters.tick_size),
                    target_qty=qty,
                    atr=atr,
                    config=config.orderbook,
                    now=_time.time(),
                    unavailable_reason=failure,
                )
                print(f"\n{symbol} (tick={item.filters.tick_size}, uji qty={qty:g}):")
                print(f"  {decision.summary()}")
                for note in decision.notes:
                    print(f"  - {note}")
                if decision.rejected:
                    exit_code = 0 if args.allow_reject else 1
            _print_metric_summary(rest.metrics, 0)
            return exit_code
        finally:
            await rest.close()

    return asyncio.run(run())


def _cmd_symbols(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())

    async def run() -> int:
        rest = _make_rest(config, secrets)
        await rest.start()
        try:
            await rest.sync_time()
            from volumatic_bot.binance.symbols import parse_exchange_info

            payload = await rest.exchange_info()
            info = parse_exchange_info(payload)
            perps = info.trading_usdt_perpetuals
            print(f"Total simbol: {len(info.symbols)} | perpetual USDT TRADING: {len(perps)}")
            print(f"Offset jam server: {rest.time_offset_ms:+.0f} ms")
            print(f"Batas bobot per menit dari exchange: {info.weight_limit_per_min}")
            for sym in ("BTCUSDT", "ETHUSDT"):
                if sym in info.symbols:
                    item = info.get(sym)
                    f = item.filters
                    print(
                        f"{sym}: tick={f.tick_size} step={f.step_size} "
                        f"minQty={f.min_qty} minNotional={f.min_notional} "
                        f"margin_min@{config.exchange.default_leverage}x="
                        f"{item.min_margin_required(config.exchange.default_leverage):.4f} USDT"
                    )
            _print_metric_summary(rest.metrics, 0)
            return 0
        finally:
            await rest.close()

    return asyncio.run(run())


def _cmd_scan(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    logger = setup_logging(config.app.log_level, secrets=secrets.secret_values())

    async def run() -> int:
        from volumatic_bot.scanner.scanner import SymbolScanner

        rest = _make_rest(config, secrets)
        cache = _make_cache(config)
        repository = _make_repository(config, rest, cache)
        await rest.start()
        try:
            await cache.open()
            await rest.sync_time()
            scanner_cfg = config.scanner
            if args.max_min_notional is not None:
                # Timpa hanya untuk pemanggilan ini; config tidak diubah (frozen).
                scanner_cfg = config.scanner.model_copy(
                    update={"max_min_notional_usdt": args.max_min_notional}
                )
            if scanner_cfg.max_min_notional_usdt > 0:
                print(
                    f"Filter modal kecil: minNotional maksimum "
                    f"{scanner_cfg.max_min_notional_usdt:.2f} USDT per simbol"
                )
            scanner = SymbolScanner(
                rest,
                repository,
                scanner_cfg,
                leverage=config.exchange.default_leverage,
                max_concurrency=config.net.rest_concurrency,
            )
            max_margin = None
            if args.equity is not None and args.equity > 0:
                max_margin = (
                    args.equity
                    * config.risk.max_total_margin_pct
                    / 100.0
                    / config.risk.max_concurrent_positions
                )
                print(
                    f"Equity diasumsikan {args.equity:.2f} USDT -> batas margin per trade "
                    f"{max_margin:.2f} USDT"
                )
            else:
                print("Tanpa --equity: filter kelayakan modal kecil dilewati.")
            result = await scanner.scan(max_margin_per_trade=max_margin)
            print(
                f"Perpetual USDT TRADING: {result.trading_perpetuals} dari "
                f"{result.total_symbols} simbol | diterima {len(result.accepted)} | "
                f"ditolak {len(result.rejected)}"
            )
            print()
            header = (
                f"{'simbol':<12}{'harga':>12}{'vol24h(jt)':>12}{'spread%':>9}"
                f"{'ATR%':>8}{'minNot':>9}{'minMarg':>9}{'skor':>7}{'bar':>6}"
            )
            print(header)
            print("-" * len(header))
            for row in result.accepted:
                print(
                    f"{row.symbol:<12}{row.last_price:>12.6g}{row.quote_volume_24h / 1e6:>12.1f}"
                    f"{row.spread_pct:>9.3f}{row.atr_pct:>8.2f}{row.min_notional:>9.2f}"
                    f"{row.min_margin_required:>9.2f}{row.score:>7.3f}{row.bars:>6}"
                )
            if result.rejected:
                print()
                print("Contoh penolakan (10 pertama):")
                for rej in result.rejected[:10]:
                    print(f"  - {rej.symbol:<12} [{rej.stage}] {rej.reason}")
                stages: dict[str, int] = {}
                for rej in result.rejected:
                    stages[rej.stage] = stages.get(rej.stage, 0) + 1
                ringkas = ", ".join(f"{k}={v}" for k, v in sorted(stages.items()))
                print(f"  rekap tahap penolakan: {ringkas}")
            _print_metric_summary(rest.metrics, result.requests)
            stats = await cache.stats()
            print(
                f"Cache: {stats.symbols} simbol, {stats.rows} baris, {stats.size_mb} MB "
                f"({config.db_cache()})"
            )
            logger.info("Pemindaian selesai: %d simbol diterima", len(result.accepted))
            return 0
        finally:
            await cache.close()
            await rest.close()

    return asyncio.run(run())


def _cmd_warmup(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    setup_logging(config.app.log_level, secrets=secrets.secret_values())

    async def run() -> int:
        from volumatic_bot.scanner.scanner import SymbolScanner

        rest = _make_rest(config, secrets)
        cache = _make_cache(config)
        repository = _make_repository(config, rest, cache)
        await rest.start()
        try:
            await cache.open()
            await rest.sync_time()
            symbols = [s.upper() for s in args.symbols] if args.symbols else []
            if not symbols:
                scanner = SymbolScanner(
                    rest,
                    repository,
                    config.scanner,
                    leverage=config.exchange.default_leverage,
                    max_concurrency=config.net.rest_concurrency,
                )
                result = await scanner.scan(force_refresh_meta=True)
                symbols = result.accepted_symbols
                print(f"Simbol terpilih dari pemindaian: {len(symbols)}")
            total_bars = 0
            for symbol in symbols:
                hist = await repository.history(symbol, config.scanner.warmup_bars)
                total_bars += hist.total
                print(
                    f"  {symbol:<12} {hist.total:>5} bar (cache {hist.from_cache}, "
                    f"REST {hist.from_rest}, {hist.requests} permintaan)"
                )
            stats = await cache.stats()
            print(
                f"Selesai: {len(symbols)} simbol, {total_bars} bar | cache "
                f"{stats.symbols} simbol / {stats.rows} baris / {stats.size_mb} MB"
            )
            _print_metric_summary(rest.metrics, 0)
            return 0
        finally:
            await cache.close()
            await rest.close()

    return asyncio.run(run())


def _cmd_storage(args: argparse.Namespace) -> int:
    config, _ = _load(args)

    async def run() -> int:
        from volumatic_bot.storage.db import sqlite_runtime_version

        cache = _make_cache(config)
        try:
            await cache.open()
            stats = await cache.stats()
            print(f"SQLite runtime: {sqlite_runtime_version()}")
            print(f"Berkas cache : {config.db_cache()}")
            print(f"Simbol cache : {stats.symbols}")
            print(f"Baris candle : {stats.rows}")
            print(f"Ukuran       : {stats.size_mb} MB")
            return 0
        finally:
            await cache.close()

    return asyncio.run(run())


def _as_void_callback(handler: Any) -> Any:
    """Bungkus handler async agar sesuai tipe ``on_kline`` (hasil diabaikan)."""

    async def wrapper(event: Any) -> None:
        await handler(event)

    return wrapper


def _format_gate_line(candidate: Any) -> str:
    """Satu baris ringkasan kandidat (dipakai ``listen --with-book``)."""
    book = candidate.book
    ringkas = book.summary() if book is not None else "order book tidak dievaluasi"
    status = "LAYAK" if candidate.tradable else "DITOLAK"
    return (
        f"[BOOK] {candidate.symbol:<12} {candidate.direction.upper():<5} {status} "
        f"skor={candidate.score:.3f} | {ringkas}"
    )


def _cmd_listen(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    logger = setup_logging(config.app.log_level, secrets=secrets.secret_values())

    async def run() -> int:
        from volumatic_bot.binance.ws import KlineStreamClient
        from volumatic_bot.scanner.scanner import SymbolScanner
        from volumatic_bot.signals.pipeline import SignalGate
        from volumatic_bot.streams.kline_feed import KlineFeed, SignalEvent

        rest = _make_rest(config, secrets)
        cache = _make_cache(config)
        repository = _make_repository(config, rest, cache)
        await rest.start()
        metrics = rest.metrics
        try:
            await cache.open()
            await rest.sync_time()
            scanner = SymbolScanner(
                rest,
                repository,
                config.scanner,
                leverage=config.exchange.default_leverage,
                max_concurrency=config.net.rest_concurrency,
            )
            result = await scanner.scan()
            if not result.accepted:
                print("Tidak ada simbol yang lolos filter; tidak ada stream untuk didengar.")
                return 1
            symbols = result.accepted_symbols

            def on_signal(signal: SignalEvent) -> None:  # dicetak ke terminal
                print(
                    f"[SINYAL] {signal.symbol:<12} {signal.direction.upper():<5} "
                    f"close={signal.close_price:.6g} ATR={signal.atr_pct:.2f}% "
                    f"trail={signal.trail:.6g} deltaWin={signal.vol_delta_win_pct:.1f}% "
                    f"skor={signal.score:.3f} t={signal.open_time}"
                )

            gate: Any = None
            queue: asyncio.Queue[SignalEvent] | None = None
            consumer: asyncio.Task[None] | None = None
            if args.with_book:
                exchange = await scanner.exchange_info()
                depth_source = _make_depth_source(config, rest)
                gate = SignalGate(config, depth_source, exchange.symbols.get, clock=_time.time)
                queue = asyncio.Queue(maxsize=100)
                if hasattr(depth_source, "start"):
                    await depth_source.start()
                print(
                    f"Filter order book aktif (sumber {config.orderbook.source}, "
                    f"maks {config.risk.max_concurrent_positions} slot, "
                    f"tolak bila depth tidak tersedia={config.orderbook.reject_if_unavailable})"
                )

            def on_signal_masuk(signal: SignalEvent) -> None:
                on_signal(signal)
                if queue is not None:
                    try:
                        queue.put_nowait(signal)
                    except asyncio.QueueFull:
                        logger.warning(
                            "Antrean order book penuh; sinyal %s dilewati", signal.symbol
                        )

            async def konsumsi_sinyal() -> None:
                assert gate is not None and queue is not None
                while True:
                    signal = await queue.get()
                    try:
                        gate_result = await gate.evaluate_many(
                            [signal], slots=config.risk.max_concurrent_positions
                        )
                        for candidate in gate_result.candidates:
                            print(_format_gate_line(candidate))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # jangan matikan pemantauan karena 1 galat
                        logger.error("Gagal mengevaluasi order book: %s", type(exc).__name__)

            feed = KlineFeed(
                config.indicator.to_params(),
                max_bars=config.scanner.warmup_bars + 200,
                on_signal=on_signal_masuk,
                offload=True,
                scores={row.symbol: row.score for row in result.accepted},
            )
            feed.seed_many(
                {row.symbol: row.series for row in result.accepted if row.series is not None}
            )
            print(
                f"Mendengarkan {len(symbols)} simbol pada {config.scanner.timeframe} "
                f"({', '.join(symbols[:6])}{'...' if len(symbols) > 6 else ''})"
            )
            print("Tekan Ctrl+C untuk berhenti.")
            client = KlineStreamClient(
                symbols,
                config.scanner.timeframe,
                on_kline=_as_void_callback(feed.handle_event),
                base_url=config.net.resolve_ws_url(testnet=config.exchange.testnet),
                metrics=metrics,
                idle_timeout_sec=config.net.ws_idle_timeout_sec,
                rotate_after_sec=config.net.ws_rotate_hours * 3600,
            )
            task = asyncio.create_task(client.run())
            if queue is not None:
                consumer = asyncio.create_task(konsumsi_sinyal())
            try:
                await asyncio.sleep(args.duration if args.duration > 0 else 10**9)
            except asyncio.CancelledError:
                pass
            finally:
                client.stop()
                task.cancel()
                if consumer is not None:
                    consumer.cancel()
                    # CancelledError turunan BaseException, BUKAN Exception: kalau
                    # hanya suppress(Exception) dipakai, Ctrl+C/akhir durasi
                    # berubah jadi traceback. Ini bug yang muncul di uji testnet
                    # pertama (14 Nov 2026).
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await consumer
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            snap = metrics.snapshot()
            print(
                f"Berhenti. Event diproses: {feed.processed} | sinyal: {feed.signals} | "
                f"WS pesan: {snap.ws_messages} | latensi EMA: {snap.ws_latency_ema_ms:.0f} ms"
            )
            _print_metric_summary(metrics, 0)
            logger.info("Umpan kline dihentikan dengan rapi")
            return 0
        finally:
            await cache.close()
            await rest.close()

    return asyncio.run(run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="volumatic-bot", description="Bot USDT-M Futures berbasis Volumatic VIDYA"
    )
    parser.add_argument("--config", default="config.yaml", help="path config.yaml")
    parser.add_argument(
        "--profil",
        default=None,
        metavar="NAMA",
        help=(
            "pakai profil dari bagian `profil:` di config (mis. modal-kecil), "
            "menimpa profil_aktif tanpa mengedit file. `volumatic-bot profil` "
            "menampilkan daftarnya"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="tampilkan versi").set_defaults(func=_cmd_version)
    sub.add_parser("check-config", help="validasi konfigurasi").set_defaults(func=_cmd_check_config)
    sub.add_parser(
        "profil", help="(tanpa jaringan) daftar profil di config & nilai yang ditimpa"
    ).set_defaults(func=_cmd_profil)

    ind = sub.add_parser("indicator", help="hitung indikator dari CSV candle")
    ind.add_argument("--candles", required=True, help="file CSV/CSV.GZ candle")
    ind.add_argument("--rows", type=int, default=None, help="batasi jumlah baris terakhir")
    ind.add_argument("--show", type=int, default=10, help="tampilkan N bar terakhir")
    ind.set_defaults(func=_cmd_indicator)

    warm = sub.add_parser("warmup-study", help="studi konvergensi warm-up")
    warm.add_argument("--candles", required=True)
    warm.add_argument("--out", default=None, help="tulis laporan Markdown ke file ini")
    warm.set_defaults(func=_cmd_warmup_study)

    tvc = sub.add_parser("tv-compare", help="bandingkan dengan ekspor TradingView")
    tvc.add_argument("--candles", required=True)
    tvc.add_argument("--tv", required=True, help="CSV ekspor TradingView")
    tvc.add_argument("--out", default=None)
    tvc.set_defaults(func=_cmd_tv_compare)

    sym = sub.add_parser("symbols", help="(jaringan) ringkasan exchangeInfo & filter")
    sym.set_defaults(func=_cmd_symbols)

    scan = sub.add_parser("scan", help="(jaringan) pindai simbol dan tampilkan alasannya")
    scan.add_argument(
        "--equity", type=float, default=None, help="equity USDT (aktifkan filter modal)"
    )
    scan.add_argument(
        "--max-min-notional",
        type=float,
        default=None,
        metavar="USDT",
        help=(
            "tolak simbol yang minNotional-nya di atas angka ini "
            "(menimpa scanner.max_min_notional_usdt; 0 = nonaktif). "
            "Untuk modal kecil: --max-min-notional 5"
        ),
    )
    scan.set_defaults(func=_cmd_scan)

    warm = sub.add_parser("warmup", help="(jaringan) isi cache candle untuk simbol aktif")
    warm.add_argument(
        "--symbols", nargs="*", default=None, help="daftar simbol (default: hasil scan)"
    )
    warm.set_defaults(func=_cmd_warmup)

    store = sub.add_parser("storage", help="info database cache (tanpa jaringan)")
    store.set_defaults(func=_cmd_storage)

    listen = sub.add_parser("listen", help="(jaringan) dengarkan kline closed & cetak sinyal")
    listen.add_argument("--duration", type=float, default=0.0, help="detik (0 = tanpa batas)")
    listen.add_argument(
        "--with-book",
        action="store_true",
        help="evaluasi order book tiap sinyal dan tampilkan keputusannya",
    )
    listen.set_defaults(func=_cmd_listen)

    book = sub.add_parser("book", help="(jaringan) periksa order book & keputusan filter")
    book.add_argument("symbols", nargs="+", help="satu atau beberapa simbol, mis. BTCUSDT")
    book.add_argument("--side", choices=["long", "short"], default="long", help="arah uji")
    book.add_argument(
        "--atr-pct",
        type=float,
        default=0.5,
        help="perkiraan ATR dalam persen harga (untuk jarak wall, default 0.5)",
    )
    book.add_argument(
        "--notional", type=float, default=None, help="notional uji USDT (default dari config)"
    )
    book.add_argument(
        "--allow-reject",
        action="store_true",
        help="tetap keluar dengan kode 0 walau sinyal ditolak",
    )
    book.set_defaults(func=_cmd_book)

    pre = sub.add_parser("preflight", help="(tanpa jaringan) pemeriksaan awal sebelum jalan")
    pre.add_argument("--equity", type=float, default=None, help="equity USDT yang akan dipakai")
    pre.add_argument(
        "--ie-understand-risk",
        action="store_true",
        help="syarat tambahan bila mode testnet/live",
    )
    pre.set_defaults(func=_cmd_preflight)

    siz = sub.add_parser("sizing", help="(jaringan) hitung ukuran posisi & level trade")
    siz.add_argument("symbol", help="simbol, mis. DOGEUSDT")
    siz.add_argument("--equity", type=float, required=True, help="equity USDT")
    siz.add_argument("--entry", type=float, required=True, help="perkiraan harga entry")
    siz.add_argument("--atr-pct", type=float, default=1.0, help="ATR dalam persen harga")
    siz.add_argument("--side", choices=["long", "short"], default="long")
    siz.add_argument("--trail", type=float, default=None, help="garis VIDYA (opsional)")
    siz.add_argument("--open-positions", type=int, default=0, help="posisi yang sedang terbuka")
    siz.set_defaults(func=_cmd_sizing)

    sim = sub.add_parser(
        "simulate-trade", help="(tanpa jaringan) siklus penuh trade di broker simulasi"
    )
    sim.add_argument("symbol", help="simbol dari contoh exchangeInfo, mis. DOGEUSDT")
    sim.add_argument(
        "--exchange-info",
        default="tests/data/exchange_info_sample.json",
        help="file contoh exchangeInfo (default: data uji repo)",
    )
    sim.add_argument("--equity", type=float, default=50.0)
    sim.add_argument("--entry", type=float, default=0.35)
    sim.add_argument("--atr-pct", type=float, default=2.0)
    sim.add_argument("--side", choices=["long", "short"], default="long")
    sim.add_argument("--trail", type=float, default=None, help="garis VIDYA (opsional)")
    sim.add_argument(
        "--path",
        default="",
        help="jalur harga dipisah koma, mis. 0.352,0.358,0.361 (memicu TP/BE/trailing)",
    )
    sim.add_argument(
        "--no-notify", action="store_true", help="jangan kirim notifikasi (hanya cetak hasil)"
    )
    sim.set_defaults(func=_cmd_simulate_trade)

    rek = sub.add_parser("reconcile", help="(jaringan) samakan catatan bot dengan bursa")
    rek.add_argument("--symbols", nargs="*", default=None, help="simbol yang diperiksa")
    rek.add_argument("--force", action="store_true", help="jalankan walau mode dry_run")
    rek.set_defaults(func=_cmd_reconcile)

    pos = sub.add_parser("positions", help="(tanpa jaringan) posisi, order, dan insiden lokal")
    pos.add_argument(
        "--all-modes",
        action="store_true",
        help="tampilkan juga jumlah posisi aktif pada berkas database mode lain",
    )
    pos.add_argument("--limit", type=int, default=10)
    pos.set_defaults(func=_cmd_positions)

    bt = sub.add_parser("backtest", help="(tanpa jaringan) uji strategi pada candle historis")
    bt.add_argument("--candles", required=True, help="file CSV/CSV.GZ candle tertutup")
    bt.add_argument("--symbol", default=None, help="simbol (default: dari nama file)")
    bt.add_argument(
        "--exchange-info",
        default="tests/data/exchange_info_sample.json",
        help="contoh exchangeInfo untuk filter simbol",
    )
    bt.add_argument("--equity", type=float, default=50.0)
    bt.add_argument("--leverage", type=int, default=None)
    bt.add_argument("--taker-bps", type=float, default=None)
    bt.add_argument("--slippage-bps", type=float, default=None)
    bt.add_argument("--funding-pct", type=float, default=None, help="funding tetap %% per 8 jam")
    bt.add_argument("--funding-file", default=None, help="JSON/JSONL fundingRate historis")
    bt.add_argument("--warmup", type=int, default=260)
    bt.add_argument("--max-bars", type=int, default=0, help="batas bar dalam satu trade")
    bt.add_argument("--partial-pct", type=float, default=50.0)
    bt.add_argument("--rows", type=int, default=None, help="batas jumlah bar yang dibaca")
    bt.add_argument("--trades", type=int, default=10, help="jumlah trade terakhir yang dicetak")
    bt.add_argument("--risk-pct", type=float, default=None, help="override risiko per trade (%%)")
    bt.add_argument("--json", default=None, help="simpan hasil lengkap ke berkas JSON")
    bt.set_defaults(func=_cmd_backtest)

    ses = sub.add_parser(
        "simulate-session", help="(tanpa jaringan) jalankan mesin penuh pada candle historis"
    )
    ses.add_argument("--candles", required=True, help="file CSV/CSV.GZ candle tertutup")
    ses.add_argument("--symbol", default=None)
    ses.add_argument("--exchange-info", default="tests/data/exchange_info_sample.json")
    ses.add_argument("--equity", type=float, default=50.0)
    ses.add_argument("--limit-bars", type=int, default=None, help="batas bar yang diproses")
    ses.add_argument("--step", type=float, default=0.5, help="persen harga antar-tick simulasi")
    ses.add_argument("--risk-pct", type=float, default=None, help="override risiko per trade (%%)")
    ses.add_argument("--json", default=None, help="simpan laporan sesi ke JSON")
    ses.set_defaults(func=_cmd_simulate_session)

    stop = sub.add_parser("stop", help="(tanpa jaringan) hentikan bot yang sedang berjalan")
    stop.add_argument(
        "--close",
        action="store_true",
        help="pasang kill switch dulu: bot menutup semua posisi market lalu berhenti",
    )
    stop.add_argument(
        "--all-modes", action="store_true", help="hentikan semua mode (dry_run/testnet/live)"
    )
    stop.add_argument(
        "--timeout", type=float, default=20.0, help="detik menunggu proses berhenti (bawaan 20)"
    )
    stop.add_argument("--force", action="store_true", help="SIGKILL bila tidak berhenti")
    stop.set_defaults(func=_cmd_stop)

    kill = sub.add_parser(
        "kill-switch", help="(tanpa jaringan) pasang/hapus tombol darurat: tutup semua & berhenti"
    )
    kill.add_argument("--clear", action="store_true", help="hapus file kill switch")
    kill.add_argument("--status", action="store_true", help="hanya periksa status")
    kill.set_defaults(func=_cmd_kill_switch)

    ipc = sub.add_parser(
        "ip", help="(jaringan) tampilkan IP egress dan apakah Binance menerimanya"
    )
    ipc.add_argument(
        "--tanpa-binance",
        action="store_true",
        help="hanya baca IP publik, jangan menghubungi Binance",
    )
    ipc.set_defaults(func=_cmd_ip)

    net = sub.add_parser("netcheck", help="(jaringan) cek koneksi ke Binance langkah demi langkah")
    net.set_defaults(func=_cmd_netcheck)

    run = sub.add_parser("run", help="(jaringan) jalankan mesin trading")
    run.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="simbol manual (mis. DOGEUSDT ETHUSDT). Bila dikosongkan, bot memindai "
        "sendiri simbol aktif dan menyegarkannya tiap scanner.symbol_refresh_hours",
    )
    run.add_argument("--iterations", type=int, default=None, help="batas siklus dashboard")
    run.add_argument("--no-dashboard", action="store_true")
    run.set_defaults(func=_cmd_run)

    dash = sub.add_parser("dashboard", help="(tanpa jaringan) dashboard rich 4 panel")
    dash.add_argument("--once", action="store_true", help="cetak sekali lalu keluar")
    dash.add_argument(
        "--demo",
        action="store_true",
        help="pakai data contoh (angka karangan) untuk melihat tampilan saja",
    )
    dash.add_argument("--compact", action="store_true", help="tampilan ringkas satu panel")
    dash.add_argument("--inline", action="store_true", help="pakai mode inline (tanpa layar penuh)")
    dash.add_argument("--refresh", type=float, default=None, help="detik antar-refresh")
    dash.add_argument("--iterations", type=int, default=None, help="batas jumlah frame")
    dash.set_defaults(func=_cmd_dashboard)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"KONFIGURASI GAGAL: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"FILE TIDAK DITEMUKAN: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nDihentikan oleh pengguna (Ctrl+C).", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
