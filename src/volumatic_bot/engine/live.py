"""Sesi **live/testnet**: menyambungkan mesin ke bursa sungguhan.

⚠️ **Jalur ini belum pernah dijalankan ke Binance.** Selama pengembangan,
``fapi.binance.com`` mengembalikan HTTP 451 dari lingkungan sandbox, jadi yang
bisa diuji hanya bagian yang menyusun objek (fungsi :func:`build_session`) dan
penolakan mode berbahaya. Semua kegagalan nyata (bentuk respons, kode error,
perilaku trigger, disconnect) baru akan terlihat saat Anda menjalankannya.

Komponen yang disusun:

```
BinanceREST ─┬─ TradingAPI ─┬─ LiveBroker
             │              └─ ListenKeyManager ─► UserDataStream ─► engine
             ├─ RestDepthSource ─► SignalGate
             └─ exchangeInfo + klines (warm-up) ─► KlineFeed
KlineStreamClient ─► KlineFeed.handle_event ─► engine.handle_signal
ticker berkala ─► harga (premiumIndex) ─► engine.handle_price + rekonsiliasi
```

Hemat bandwidth: satu koneksi kline gabungan, depth hanya per sinyal (cache 3
detik), harga hanya untuk simbol yang **sedang punya posisi**, status order lewat
user data stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from volumatic_bot.binance.rest import BinanceREST
from volumatic_bot.binance.symbols import ExchangeInfo, parse_exchange_info
from volumatic_bot.binance.trading import TradingAPI
from volumatic_bot.binance.user_stream import ListenKeyManager, UserDataStream
from volumatic_bot.binance.ws import KlineStreamClient
from volumatic_bot.config import (
    AppConfig,
    Secrets,
    baca_yaml,
    deteksi_profil_otomatis,
    load_config,
    profil_aktif_dari,
)
from volumatic_bot.dashboard.app import DashboardApp
from volumatic_bot.dashboard.sources import LocalStateSource
from volumatic_bot.data.cache import KlineCache
from volumatic_bot.data.repository import KlineRepository
from volumatic_bot.engine.trader import TradingEngine
from volumatic_bot.execution.broker import LiveBroker
from volumatic_bot.execution.executor import TradeExecutor
from volumatic_bot.execution.reconcile import reconcile
from volumatic_bot.metrics import Metrics
from volumatic_bot.notify.factory import build_dispatcher
from volumatic_bot.orderbook.depth import RestDepthSource
from volumatic_bot.positions.store import PositionStore
from volumatic_bot.risk.limits import DailyLimits, DailyState
from volumatic_bot.risk.manager import RiskManager
from volumatic_bot.risk.persistence import restore_daily_state, save_daily_state
from volumatic_bot.signals.pipeline import SignalGate
from volumatic_bot.storage.db import SqliteDatabase
from volumatic_bot.streams.kline_feed import KlineFeed

__all__ = ["LiveSession", "build_session", "run_live_session"]

logger = logging.getLogger("volumatic_bot.engine.live")


@dataclass(slots=True)
class LiveSession:
    """Kumpulan objek satu sesi; dibuat terpisah supaya bisa diuji tanpa jaringan."""

    config: AppConfig
    db: SqliteDatabase
    store: PositionStore
    metrics: Metrics
    rest: BinanceREST
    api: TradingAPI
    broker: LiveBroker
    feed: KlineFeed
    engine: TradingEngine
    dashboard: LocalStateSource
    exchange_info: ExchangeInfo
    info: dict[str, Any] = field(default_factory=dict)
    #: Mode otomatis: simbol dipilih pemindai dan disegarkan berkala.
    auto: bool = False
    scanner: Any = None
    cache: KlineCache | None = None
    #: Waktu monotonic berikutnya candle harian (filter rezim) disegarkan.
    _rezim_berikut: float = 0.0

    async def close(self) -> None:
        if self.cache is not None:
            with contextlib.suppress(Exception):
                await self.cache.close()
        with contextlib.suppress(Exception):
            await self.rest.close()
        self.db.close()


def _now_zona(config: AppConfig) -> Any:
    from volumatic_bot.cli import _now_sadar_zona

    return _now_sadar_zona(config.app.timezone)


def _proses_hidup(pid: int) -> bool:
    """True bila PID masih berjalan (mengabaikan zombie)."""
    from volumatic_bot.procutil import proses_hidup

    return proses_hidup(pid)


def _tulis_pid(jalur: Path) -> None:
    """Tulis PID berkas sesi ini; tolak bila sudah ada bot lain di mode yang sama."""
    if jalur.exists():
        try:
            lama = int(jalur.read_text(encoding="utf-8").split()[0])
        except (ValueError, IndexError, OSError):
            lama = 0
        if lama and lama != os.getpid() and _proses_hidup(lama):
            raise ValueError(
                f"sesi mode ini sudah berjalan (PID {lama}, berkas {jalur}). "
                "Hentikan dulu: volumatic-bot stop   (atau hapus berkas PID bila "
                "prosesnya sudah mati)"
            )
    jalur.parent.mkdir(parents=True, exist_ok=True)
    jalur.write_text(f"{os.getpid()} {int(time.time())}\n", encoding="utf-8")


def _saring_simbol(simbol: list[str], config: AppConfig, exchange_info: ExchangeInfo) -> list[str]:
    """Rapikan & validasi daftar simbol sebelum sesi dibuka.

    Tanpa ini, ``run --symbols btcusdt`` (huruf kecil, simbol yang ada di
    blacklist, atau salah ketik) tetap "berjalan" lalu gagal jauh di belakang
    dengan pesan yang membingungkan.
    """
    bersih = [s.strip().upper() for s in simbol if s.strip()]
    if not bersih:
        raise ValueError("daftar simbol kosong: isi scanner.whitelist atau pakai --symbols")

    hitam = {b.strip().upper() for b in config.scanner.blacklist}
    kena_blacklist = [s for s in bersih if s in hitam]
    if kena_blacklist:
        raise ValueError(
            f"simbol ada di scanner.blacklist: {', '.join(kena_blacklist)}. "
            "Hapus dari blacklist di config.yaml bila memang ingin dipakai."
        )

    tidak_ada = [s for s in bersih if s not in exchange_info.symbols]
    if tidak_ada:
        contoh = ", ".join(sorted(exchange_info.symbols)[:5])
        raise ValueError(
            f"simbol tidak ada di exchangeInfo: {', '.join(tidak_ada)}. "
            f"Periksa ejaannya (contoh yang tersedia: {contoh}, ...)."
        )
    return bersih


async def build_session(
    config: AppConfig,
    secrets: Secrets,
    *,
    symbols: list[str] | None = None,
    config_path: str | Path | None = None,
    profil: str | None = None,
    saldo_awal: float | None = None,
    laporan_ip: Any = None,
) -> LiveSession:
    """Susun semua komponen sesi dari konfigurasi (tanpa membuka koneksi WS).

    ``config_path`` + ``profil`` dipakai deteksi profil otomatis: bila saldo
    akun berada di bawah ambang dan tidak ada pilihan profil manual, sesi
    disusun ulang dari config dengan profil modal kecil. Keputusan diambil
    sekali di awal; tidak pernah berganti di tengah sesi.
    """
    if config.ops.mode not in ("testnet", "live"):
        raise ValueError("build_session hanya untuk mode testnet/live")
    if config.ops.mode == "live" and not config.ops.allow_live:
        raise ValueError("mode live butuh ops.allow_live: true")

    metrics = Metrics(weight_limit=config.exchange.weight_budget_per_min)
    rest = BinanceREST(
        base_url=config.net.resolve_rest_url(testnet=config.exchange.testnet),
        testnet=config.exchange.testnet,
        api_key=secrets.api_key(),
        api_secret=secrets.api_secret(),
        recv_window=config.exchange.recv_window_ms,
        timeout=config.exchange.request_timeout_sec,
        max_retries=config.exchange.max_retries,
        weight_budget_per_min=config.exchange.weight_budget_per_min,
        metrics=metrics,
    )
    # Session aiohttp WAJIB dibuat sebelum request pertama. Tanpa baris ini,
    # request pertama gagal dengan RuntimeError "session belum dibuat" — bug ini
    # baru ketahuan saat percobaan testnet pertama (14 Nov 2026).
    await rest.start()
    db = SqliteDatabase(config.db_posisi())
    singgahan_awal: KlineCache | None = None

    try:
        api = TradingAPI(rest, conditional_api=config.exchange.conditional_order_api)
        broker = LiveBroker(api)

        db.open()
        store = PositionStore(db)

        # --- metadata simbol -------------------------------------------------
        payload = await rest.request("GET", "/fapi/v1/exchangeInfo", weight=1)
        exchange_info = parse_exchange_info(payload)

        # --- daftar simbol: validasi dulu, sebelum menyentuh akun ------------
        # Salah ketik / simbol blacklist harus ditolak lebih awal, bukan setelah
        # warm-up 1500 candle.
        diminta = list(symbols or config.scanner.whitelist or [])
        auto = not diminta

        # --- state harian (dipulihkan dari SQLite) ---------------------------
        zona = _now_zona(config)
        saldo = saldo_awal if saldo_awal is not None else await api.balance_usdt()

        # --- deteksi profil otomatis (sekali, di awal sesi) ------------------
        # Pilihan manual (--profil / profil_aktif) selalu menang. Bila deteksi
        # memilih profil, seluruh sesi disusun ulang dari config final supaya
        # tidak ada komponen yang memakai setengah profil lama setengah baru.
        if config_path is not None:
            nama_otomatis = deteksi_profil_otomatis(
                config, config_path, profil_cli=profil, saldo=saldo
            )
            if nama_otomatis is not None:
                config_baru = load_config(config_path, profil=nama_otomatis)
                logger.warning(
                    "PROFIL OTOMATIS: saldo %.2f USDT -> profil '%s' diterapkan "
                    "(leverage %sx, risiko %.1f%%/trade, maks %s posisi, lantai "
                    "equity %.2f USDT). Profil dipilih SEKALI di awal sesi; "
                    "pilihan manual (--profil / profil_aktif) selalu menang; "
                    "matikan lewat profil_otomatis.aktif: false.",
                    saldo,
                    nama_otomatis,
                    config_baru.exchange.default_leverage,
                    config_baru.risk.risk_per_trade_pct,
                    config_baru.risk.max_concurrent_positions,
                    config_baru.risk.min_equity_usdt,
                )
                await rest.close()
                db.close()
                return await build_session(
                    config_baru,
                    secrets,
                    symbols=symbols,
                    config_path=config_path,
                    profil=nama_otomatis,
                    saldo_awal=saldo,
                )
        limits = DailyLimits(
            daily_loss_limit_pct=config.risk.daily_loss_limit_pct,
            max_trades_per_day=config.risk.max_trades_per_day,
            consecutive_loss_limit=config.risk.consecutive_loss_limit,
            cooldown_minutes=config.risk.cooldown_minutes,
            reset_hour_local=config.risk.daily_loss_reset_hour_local,
            timezone=config.app.timezone,
        )
        harian = DailyState(limits=limits, start_equity=saldo, current_equity=saldo)
        restore_daily_state(store, harian, zona())
        risk = RiskManager(config, daily=harian, clock=zona)

        # --- daftar simbol ---------------------------------------------------
        # Tiga sumber, berurutan: --symbols (manual), scanner.whitelist, atau
        # pemindai otomatis. Manual tetap didukung karena paling bisa diprediksi
        # (simbol & bandwidth yang dipantau diketahui pasti).
        pemindai: Any = None
        singgahan = singgahan_awal
        if auto:
            from volumatic_bot.scanner.scanner import SymbolScanner

            singgahan = KlineCache(config.db_cache(), enabled=True)
            singgahan_awal = singgahan  # agar penutupan darurat di `except` pasti kena
            await singgahan.open()
            gudang = KlineRepository(
                rest,
                singgahan,
                config.scanner.timeframe,
                warmup_bars=config.scanner.warmup_bars,
                kline_rest_limit=config.scanner.kline_rest_limit,
                pace_sec=config.net.kline_pace_sec,
            )
            pemindai = SymbolScanner(
                rest,
                gudang,
                config.scanner,
                leverage=config.exchange.default_leverage,
                max_concurrency=config.net.rest_concurrency,
            )
            # Batas margin per trade dipakai sebagai filter "modal": simbol yang
            # minNotional-nya menuntut margin lebih besar dibuang sebelum candle
            # diunduh (hemat bandwidth).
            batas_margin = saldo * config.risk.max_per_symbol_margin_pct / 100.0
            hasil = await pemindai.scan(max_margin_per_trade=batas_margin)
            diminta = hasil.accepted_symbols
            logger.info(
                "pemindai otomatis: %s simbol lolos dari %s perpetual "
                "(ditahan kuota: %s, margin/trade <= %.2f USDT)",
                len(diminta),
                hasil.trading_perpetuals,
                hasil.dropped_by_cap,
                batas_margin,
            )
            if not diminta:
                raise ValueError(
                    "pemindai tidak menemukan simbol yang lolos filter. Periksa "
                    "scanner.min_quote_volume_24h_usdt / max_spread_pct, atau "
                    "sebutkan simbol manual: run --symbols DOGEUSDT"
                )
        daftar = _saring_simbol(diminta, config, exchange_info)

        # --- siapkan akun: position mode, leverage, margin -------------------
        # RiskManager menghitung ukuran posisi dengan asumsi leverage
        # ``exchange.default_leverage`` dan margin ISOLATED satu arah (one-way).
        # Tanpa langkah ini, akun yang masih hedge-mode/20x/CROSSED akan trading
        # dengan parameter yang salah atau ditolak bursa (galat positionSide).
        await _siapkan_akun(api, daftar, config)

        # --- order book + gate ----------------------------------------------
        depth = RestDepthSource(rest, limit=config.orderbook.depth_limit, ttl_sec=3.0)
        gate = SignalGate(config, depth, exchange_info.get, clock=time.time)

        # --- eksekutor -------------------------------------------------------
        async def _harga_mark(symbol: str) -> float | None:
            """Harga terakhir untuk validasi SL sebelum dikirim (hemat: 1 simbol)."""
            try:
                peta = await rest.prices([symbol])
            except Exception:
                return None
            nilai = peta.get(symbol)
            return float(nilai) if nilai is not None else None

        def _filters_of(symbol: str) -> Any:
            item = exchange_info.symbols.get(symbol.upper())
            return item.filters if item is not None else None

        executor = TradeExecutor(
            config,
            broker,
            store,
            clock=lambda: int(time.time() * 1000),
            logger=logger.info,
            mode=config.ops.mode,
            mark_price=_harga_mark,
            filters_of=_filters_of,
        )

        # --- feed sinyal -----------------------------------------------------
        from volumatic_bot.regime import DailyRegimeFilter

        saring_rezim = (
            DailyRegimeFilter(
                enabled=True,
                max_dist_pct=config.regime.max_dist_pct,
                min_daily_bars=config.regime.min_daily_bars,
                max_daily_atr_pct=config.regime.max_daily_atr_pct,
            )
            if config.regime.enabled
            else None
        )
        feed = KlineFeed(
            config.indicator.to_params(),
            max_bars=1700,
            regime_filter=saring_rezim,
            regime_ema_length=config.regime.ema_length,
            regime_atr_length=config.regime.atr_length,
        )
        await _warmup(rest, feed, daftar, config)
        # Warm-up (1500 candle + exchangeInfo) adalah biaya sekali. Buang dari
        # jendela bandwidth supaya dashboard tidak melaporkan "72 MB/jam" hanya
        # karena burst awal.
        metrics.reset_window()

        dashboard = LocalStateSource(
            store,
            mode=config.ops.mode,
            max_positions=config.risk.max_concurrent_positions,
            max_trades=config.risk.max_trades_per_day,
            daily_loss_limit_pct=config.risk.daily_loss_limit_pct,
            max_margin_pct=config.risk.max_total_margin_pct,
            metrics=metrics,
            timezone=config.app.timezone,
            reset_hour_local=config.risk.daily_loss_reset_hour_local,
        )
        dashboard.daily_state = harian
        if laporan_ip is not None:
            _catat_ip_ke_dashboard(dashboard, laporan_ip)

        engine = TradingEngine(
            config,
            store=store,
            risk=risk,
            executor=executor,
            broker=broker,
            gate=gate,
            notifier=build_dispatcher(config, target=logger),
            dashboard=dashboard,
            reconcile_fn=reconcile,
        )

        # Setiap sinyal dari feed langsung masuk ke mesin (satu-satunya titik sambung).
        async def _on_signal(signal: Any) -> None:
            await engine.handle_signal(signal)

        feed._on_signal = _on_signal

        nama_profil = profil or ""
        if not nama_profil and config_path is not None:
            with contextlib.suppress(Exception):
                nama_profil = profil_aktif_dari(baca_yaml(config_path))
        return LiveSession(
            config=config,
            db=db,
            store=store,
            metrics=metrics,
            rest=rest,
            api=api,
            broker=broker,
            feed=feed,
            engine=engine,
            dashboard=dashboard,
            exchange_info=exchange_info,
            info={
                "harian": harian,
                "depth": depth,
                "saldo_awal": saldo,
                "symbols": daftar,
                "profil": nama_profil,
            },
            auto=auto,
            scanner=pemindai,
            cache=singgahan,
        )
    except BaseException:
        # Jangan tinggalkan koneksi HTTP atau berkas SQLite terbuka bila
        # penyusunan sesi gagal di tengah jalan.
        if singgahan_awal is not None:
            with contextlib.suppress(Exception):
                await singgahan_awal.close()
        with contextlib.suppress(Exception):
            await rest.close()
        with contextlib.suppress(Exception):
            db.close()
        raise


async def _siapkan_akun(api: TradingAPI, symbols: list[str], config: AppConfig) -> None:
    """Samakan setelan akun bursa dengan asumsi perhitungan risiko bot.

    Tiga hal yang wajib sama: mode posisi satu arah, leverage
    ``exchange.default_leverage``, dan margin ISOLATED. Kode -4046 (margin sudah
    sesuai) dan -4059 (mode posisi sudah sesuai) bukan kegagalan. Kegagalan
    lain tidak menghentikan sesi, tapi dicatat supaya operator tahu ukuran
    margin/likuidasi bisa menyimpang dari hitungan bot.
    """
    try:
        await api.set_position_mode(dual_side=False)
    except Exception as exc:
        if getattr(exc, "code", None) != -4059:
            logger.warning("gagal set position mode one-way: %s", exc)
    leverage = config.exchange.default_leverage
    for symbol in symbols:
        try:
            await api.set_leverage(symbol, leverage)
        except Exception as exc:
            logger.warning("gagal set leverage %sx untuk %s: %s", leverage, symbol, exc)
        try:
            await api.set_margin_type(symbol, config.exchange.margin_mode.upper())
        except Exception as exc:
            if getattr(exc, "code", None) != -4046:
                logger.warning(
                    "gagal set margin %s untuk %s: %s", config.exchange.margin_mode, symbol, exc
                )


async def _warmup(
    rest: BinanceREST, feed: KlineFeed, symbols: list[str], config: AppConfig
) -> None:
    """Isi riwayat candle tiap simbol lewat REST (bobot 1–5 per permintaan)."""
    for symbol in symbols:
        try:
            rows = await rest.klines(
                symbol,
                interval=config.scanner.timeframe,
                limit=config.scanner.kline_rest_limit,
            )
        except Exception as exc:
            logger.warning("warm-up %s gagal: %s", symbol, exc)
            continue
        feed.seed(symbol, _series_dari_klines(rows))
        await asyncio.sleep(config.net.kline_pace_sec)

    if feed.regime_filter is None:
        return
    limit = config.regime.daily_kline_limit
    for symbol in symbols:
        try:
            harian = await rest.klines(symbol, interval="1d", limit=limit)
        except Exception as exc:
            logger.warning(
                "warm-up harian %s gagal (filter rezim menolak simbol ini): %s", symbol, exc
            )
            continue
        feed.seed_regime(symbol, _series_dari_klines(harian))
        await asyncio.sleep(config.net.kline_pace_sec)


def _kline_ke_mesin(sesi: LiveSession) -> Any:
    """Bungkus ``feed.handle_event`` agar sesuai tipe yang diharapkan stream."""

    async def handler(event: Any) -> None:
        await sesi.feed.handle_event(event)

    return handler


def _series_dari_klines(rows: list[list[Any]]) -> Any:
    """Ubah respons ``/fapi/v1/klines`` menjadi ``CandleSeries``."""
    from volumatic_bot.models import CandleSeries

    if not rows:
        return CandleSeries.from_columns([], [], [], [], [], [])
    return CandleSeries.from_columns(
        open_time=[int(row[0]) for row in rows],
        open=[float(row[1]) for row in rows],
        high=[float(row[2]) for row in rows],
        low=[float(row[3]) for row in rows],
        close=[float(row[4]) for row in rows],
        volume=[float(row[5]) for row in rows],
    )


async def run_live_session(
    config: AppConfig,
    secrets: Secrets,
    *,
    symbols: list[str] | None = None,
    iterations: int | None = None,
    dashboard: bool = True,
    config_path: str | Path | None = None,
    profil: str | None = None,
    laporan_ip: Any = None,
) -> int:
    """Jalankan sesi sampai dihentikan (Ctrl+C / kill switch / batas harian).

    Mengembalikan kode keluar: 0 normal, 2 bila sesi diblokir sebelum mulai.

    ``laporan_ip`` (opsional) adalah hasil pemeriksaan IP yang sudah dilakukan
    pemanggil, mis. sebelum sesi dimulai. Dipakai mengisi panel STATUS BOT tanpa
    menembak layanan IP dua kali.
    """
    sesi = await build_session(
        config,
        secrets,
        symbols=symbols,
        config_path=config_path,
        profil=profil,
        laporan_ip=laporan_ip,
    )
    # Catat PID: supaya `volumatic-bot stop` bisa menemukan proses ini, dan supaya
    # tidak ada dua bot yang jalan bersamaan pada database/mode yang sama.
    jalur_pid = Path(config.pid_file())
    _tulis_pid(jalur_pid)
    engine = sesi.engine
    harian: DailyState = sesi.info["harian"]

    diblokir, alasan = engine.is_blocked()
    if diblokir:
        logger.error("sesi tidak dijalankan: %s", alasan)
        await sesi.close()
        return 2

    keys = ListenKeyManager(sesi.api, keepalive_sec=config.ops.user_stream_keepalive_sec)
    user_stream = UserDataStream(
        keys,
        testnet=config.exchange.testnet,
        metrics=sesi.metrics,
        on_order=engine.handle_order_update,
        on_algo=engine.handle_algo_update,
    )

    def _buat_klien_kline(simbol: list[str]) -> KlineStreamClient:
        return KlineStreamClient(
            simbol,
            config.scanner.timeframe,
            on_kline=_kline_ke_mesin(sesi),
            testnet=config.exchange.testnet,
            metrics=sesi.metrics,
            idle_timeout_sec=config.net.ws_idle_timeout_sec,
            rotate_after_sec=config.net.ws_rotate_hours * 3600.0,
            max_streams=config.net.ws_max_streams,
        )

    kline_stream = _buat_klien_kline(sesi.info["symbols"])

    # Panel "log terakhir" hanya terisi bila ada handler penangkap. Tanpa ini
    # dashboard menampilkan "belum ada log" padahal bot menulis log.
    from volumatic_bot.dashboard.logbuffer import RingBufferHandler

    penangkap = RingBufferHandler(maxlen=max(50, config.dashboard.max_log_lines * 4))
    logger.addHandler(penangkap)

    def _state() -> Any:
        # ``sesi.dashboard`` = LocalStateSource; ``dashboard`` di sini adalah bool
        # (saklar tampilan), jangan tertukar.
        sesi.dashboard.set_logs(penangkap.lines(limit=config.dashboard.max_log_lines))
        state = engine.dashboard_state()
        assert state is not None
        return state

    app = DashboardApp(
        _state,
        refresh_sec=config.dashboard.refresh_sec,
        screen=config.dashboard.layar_penuh,
        jendela_menit=config.dashboard.log_ulang_jendela_menit,
    )
    tugas = [
        asyncio.create_task(user_stream.run(), name="user-stream"),
        asyncio.create_task(kline_stream.run(), name="kline-stream"),
    ]
    if dashboard:

        async def _jalankan_dashboard() -> None:
            await app.run(iterations=iterations)

        tugas.append(asyncio.create_task(_jalankan_dashboard(), name="dashboard"))

    jeda_refresh = max(1, int(config.scanner.symbol_refresh_hours * 3600))
    berikut_refresh = time.monotonic() + jeda_refresh
    # Kill switch harus reaktif: loop utama tidur sampai reconcile_interval_sec
    # (bawaan 300 detik) — terlalu lama untuk tombol darurat. Karena itu tidur
    # dipecah menjadi potongan kecil dan file kill switch diperiksa tiap potongan.
    jeda_cek_kill = max(1.0, float(config.ops.kill_switch_poll_sec))

    async def _tidur_sambil_cek(detik: float) -> None:
        sisa = detik
        while sisa > 0:
            potong = min(jeda_cek_kill, sisa)
            await asyncio.sleep(potong)
            sisa -= potong
            if await engine.check_kill_switch():
                return

    berikut_ip = (
        time.monotonic() + config.ipcheck.ulang_setiap_menit * 60.0
        if config.ipcheck.enabled and config.ipcheck.ulang_setiap_menit > 0
        else float("inf")
    )

    try:
        while not engine.stopped:
            await _tidur_sambil_cek(config.ops.reconcile_interval_sec)
            if bool(engine.stopped):
                break
            if time.monotonic() >= berikut_ip:
                berikut_ip = time.monotonic() + config.ipcheck.ulang_setiap_menit * 60.0
                await _periksa_ip_berkala(config, sesi, engine)
                if bool(engine.stopped):
                    break
            await engine.periodic()
            await _tick_harga(sesi)
            await _segarkan_rezim(sesi, config)
            save_daily_state(sesi.store, harian, _now_zona(config)())

            # --- refresh daftar simbol (mode otomatis) -----------------------
            # tanpanya, bot akan memantau simbol yang sama selamanya walaupun
            # volume/spread pasar berubah.
            if sesi.auto and time.monotonic() >= berikut_refresh:
                berikut_refresh = time.monotonic() + jeda_refresh
                baru = await _refresh_simbol(sesi, kline_stream, _buat_klien_kline)
                if baru is not None:
                    kline_stream = baru
                    tugas = [t for t in tugas if t.get_name() != "kline-stream"]
                    tugas.append(asyncio.create_task(kline_stream.run(), name="kline-stream"))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.warning("sesi dihentikan operator")
    finally:
        for t in tugas:
            t.cancel()
        for t in tugas:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        with contextlib.suppress(Exception):
            await keys.close()
        logger.removeHandler(penangkap)
        await sesi.close()
        with contextlib.suppress(OSError):
            await asyncio.to_thread(jalur_pid.unlink)
    return 0


def _catat_ip_ke_dashboard(sumber: Any, laporan: Any) -> None:
    """Teruskan hasil pemeriksaan IP ke panel STATUS BOT (tanpa I/O tambahan).

    ``sumber`` boleh :class:`LiveSession` atau :class:`LocalStateSource`; yang
    dipakai hanya atribut ``dashboard``/``set_ip`` (dan ``store`` untuk
    menyimpan ke SQLite).
    """
    from contextlib import suppress
    from datetime import datetime

    panel = getattr(sumber, "dashboard", sumber)
    panel.set_ip(
        laporan.ip,
        negara=laporan.negara,
        binance=laporan.binance,
        diperiksa=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    # Simpan juga ke SQLite: `volumatic-bot dashboard` adalah proses terpisah
    # yang hanya membaca database, jadi tanpa ini baris IP tidak muncul di sana.
    store = getattr(sumber, "store", None)
    if store is not None:
        with suppress(Exception):
            store.set_state(
                "ip_egress",
                f"{laporan.ip}|{laporan.negara}|"
                f"{'' if laporan.binance is None else (1 if laporan.binance else 0)}|"
                f"{datetime.now().astimezone().isoformat(timespec='seconds')}",
            )


async def _periksa_ip_berkala(config: AppConfig, sesi: LiveSession, engine: Any) -> None:
    """Cek ulang IP di tengah sesi; hentikan bot bila Binance mulai menolaknya.

    IP dinamis (VPS dengan floating IP, koneksi rumahan, container yang
    dijadwalkan ulang) bisa berubah tanpa bot restart. Kalau IP baru tidak
    dilayani Binance, order berikutnya akan gagal dan SL bisa tidak terpasang:
    itu risiko yang terlalu mahal untuk dibiarkan sampai kebetulan terlihat.
    """
    from volumatic_bot.net.ipcheck import ambil_ip_publik, periksa_binance, sumber_dari_url

    sumber = tuple(sumber_dari_url(u) for u in config.ipcheck.sumber)
    laporan = await ambil_ip_publik(sumber, timeout_sec=config.ipcheck.timeout_sec)
    await periksa_binance(laporan, rest=sesi.api.rest)
    _catat_ip_ke_dashboard(sesi, laporan)
    kunci = f"{laporan.ip}|{laporan.binance}"
    sebelumnya = str(sesi.info.get("ip_kunci", ""))
    sesi.info["ip_kunci"] = kunci
    if laporan.binance:
        # Hanya dicatat saat berubah: pesan "masih diterima" tiap 60 menit
        # memenuhi panel KEJADIAN tanpa menambah informasi apa pun. Status
        # terkini sudah terlihat di panel STATUS BOT.
        if kunci != sebelumnya:
            logger.info("ipcheck: %s diterima Binance", laporan.ip or "(tak terbaca)")
        else:
            logger.debug(
                "ipcheck berkala: %s masih diterima Binance", laporan.ip or "(tak terbaca)"
            )
        return
    pesan = (
        f"IP {laporan.ip or '(tak terbaca)'} tidak lagi diterima Binance "
        f"({laporan.binance_pesan or 'tanpa pesan'})"
    )
    logger.error("ipcheck berkala: %s", pesan)
    # Sengaja TIDAK menutup posisi: kalau IP diblokir, order penutup juga akan
    # ditolak dan justru meninggalkan posisi tanpa pelindung. SL yang sudah
    # terpasang di bursa tetap berlaku; yang dihentikan hanya pembukaan baru.
    engine.stop_session(f"{pesan} (posisi dibiarkan dengan SL yang sudah terpasang)")


async def _refresh_simbol(
    sesi: LiveSession,
    klien_lama: KlineStreamClient | None,
    buat_klien: Any,
) -> KlineStreamClient | None:
    """Pindai ulang simbol; bila berubah, sambungkan ulang stream kline.

    Aturan yang dipegang:

    * simbol yang **sedang punya posisi** selalu dipertahankan — melepas stream
      untuk simbol berposisi berarti berhenti menerima candle yang dibutuhkan
      trailing/BE;
    * bila hasil pemindaian kosong, daftar lama dipakai (jangan matikan bot hanya
      karena pasar sedang sepi);
    * kegagalan pemindaian tidak mematikan sesi.
    """
    assert sesi.scanner is not None
    try:
        hasil = await sesi.scanner.scan()
    except Exception as exc:
        logger.warning("refresh simbol gagal: %s: %s", type(exc).__name__, exc)
        return None

    lama = list(sesi.info["symbols"])
    posisi = {p.symbol for p in sesi.store.active_positions()}
    baru = [s for s in hasil.accepted_symbols if s not in sesi.config.scanner.blacklist]
    gabungan = list(dict.fromkeys([*baru, *[s for s in lama if s in posisi]]))
    if not gabungan:
        logger.warning("refresh simbol: hasil kosong; daftar lama dipertahankan")
        return None
    if set(gabungan) == set(lama):
        logger.debug("refresh simbol: tidak ada perubahan (%s simbol)", len(lama))
        return None

    tambah = [s for s in gabungan if s not in lama]
    lepas = [s for s in lama if s not in gabungan]
    logger.info(
        "refresh simbol: %s simbol (+%s / -%s); simbol berposisi tetap dipantau",
        len(gabungan),
        ",".join(tambah) or "-",
        ",".join(lepas) or "-",
    )

    # Isi dulu riwayat simbol baru supaya indikatornya siap sebelum candle live.
    if tambah:
        await _warmup(sesi.rest, sesi.feed, tambah, sesi.config)
    sesi.info["symbols"] = gabungan
    if klien_lama is not None:
        klien_lama.stop()
    return buat_klien(gabungan)  # type: ignore[no-any-return]


async def _segarkan_rezim(sesi: LiveSession, config: AppConfig) -> None:
    """Segarkan candle harian untuk filter rezim (hemat: tiap 6 jam).

    Tanpa ini, EMA200 harian tidak pernah bergerak setelah warm-up dan filter
    perlahan menjadi basi (paling lambat 1 hari saat pasar bergerak).
    """
    if sesi.feed.regime_filter is None:
        return
    sekarang = time.monotonic()
    if sekarang < getattr(sesi, "_rezim_berikut", 0.0):
        return
    sesi._rezim_berikut = sekarang + config.regime.refresh_minutes * 60.0
    for simbol in sesi.feed.symbols:
        try:
            harian = await sesi.rest.klines(
                simbol, interval="1d", limit=config.regime.daily_kline_limit
            )
        except Exception as exc:
            logger.warning("segarkan rezim %s gagal: %s", simbol, exc)
            continue
        sesi.feed.seed_regime(simbol, _series_dari_klines(harian))
        await asyncio.sleep(config.net.kline_pace_sec)


async def _tick_harga(sesi: LiveSession) -> None:
    """Ambil harga **hanya** untuk simbol yang punya posisi aktif, lalu trailing."""
    aktif = sesi.store.active_positions()
    if not aktif:
        return
    try:
        harga = await sesi.rest.prices([p.symbol for p in aktif])
    except Exception as exc:
        logger.warning("gagal mengambil harga: %s", exc)
        return
    for pos in aktif:
        terbaru = harga.get(pos.symbol)
        if terbaru is None:
            continue
        atr = sesi.feed.last_atr.get(pos.symbol)
        if atr is not None and atr > 0:
            sesi.engine.set_atr(pos.symbol, atr)
        await sesi.engine.handle_price(pos.symbol, float(terbaru))
