"""Pemuatan dan validasi konfigurasi bot.

Prinsip:
* File ``config.yaml`` memuat parameter non-rahasia.
* Kredensial **hanya** dari environment variable (``.env`` lokal atau env proses).
* Semua model memakai ``extra="forbid"`` supaya salah ketik nama field langsung
  gagal, bukan diabaikan diam-diam.
* Batas aman (leverage, risiko, ukuran posisi) divalidasi di sini agar nilai
  berbahaya tidak pernah sampai ke modul eksekusi.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from volumatic_bot.indicator.params import IndicatorParams

__all__ = [
    "AppConfig",
    "CommissionConfig",
    "ConfigError",
    "ExchangeConfig",
    "IndicatorConfig",
    "NetConfig",
    "OpsConfig",
    "ProfilOtomatisConfig",
    "RiskConfig",
    "ScoringConfig",
    "Secrets",
    "TradeConfig",
    "baca_yaml",
    "daftar_profil",
    "deteksi_profil_otomatis",
    "load_config",
    "mask_secret",
    "pilih_profil_otomatis",
    "profil_aktif_dari",
    "terapkan_profil",
]


class ConfigError(RuntimeError):
    """Kesalahan konfigurasi yang harus menghentikan bot sebelum ada order."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class AppInfo(_Strict):
    name: str = "volumatic-vidya-bot"
    timezone: str = "Asia/Jakarta"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("timezone")
    @classmethod
    def _tz_must_exist(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - env dependent
            raise ValueError(f"timezone tidak dikenal: {value}") from exc
        return value


class OpsConfig(_Strict):
    mode: Literal["backtest", "dry_run", "testnet", "live"] = "dry_run"
    allow_live: bool = False
    live_confirmation: str = ""
    kill_switch_file: str = "KILL_SWITCH"
    #: Seberapa sering file kill switch diperiksa saat bot berjalan (detik).
    #: Kecil = tombol darurat lebih reaktif; 1 detik = satu stat file per detik.
    kill_switch_poll_sec: float = Field(default=3.0, ge=1.0, le=60.0)
    reconcile_interval_sec: int = Field(default=300, ge=60, le=3600)
    user_stream_keepalive_sec: int = Field(default=1800, ge=60, le=3000)

    @model_validator(mode="after")
    def _live_guard(self) -> OpsConfig:
        if self.mode == "live":
            if not self.allow_live:
                raise ValueError("mode live butuh ops.allow_live: true -- ini sengaja dibuat sulit")
            if self.live_confirmation != "SAYA-MENGERTI-RISIKONYA":
                raise ValueError(
                    "mode live butuh ops.live_confirmation persis 'SAYA-MENGERTI-RISIKONYA'"
                )
        return self


class ExchangeConfig(_Strict):
    market: Literal["usdm"] = "usdm"
    testnet: bool = True
    position_mode: Literal["one-way"] = "one-way"
    margin_mode: Literal["isolated"] = "isolated"
    default_leverage: int = Field(default=5, ge=1, le=10)
    max_leverage: int = Field(default=8, ge=1, le=10)
    recv_window_ms: int = Field(default=5000, ge=1000, le=60000)
    time_sync_interval_min: int = Field(default=30, ge=1, le=240)
    request_timeout_sec: float = Field(default=10.0, gt=0, le=60)
    max_retries: int = Field(default=4, ge=1, le=10)
    weight_budget_per_min: int = Field(default=1800, ge=100, le=2400)
    #: Endpoint untuk order kondisional (SL/TP). Sejak 2025-12-09 Binance
    #: menolak STOP_MARKET/TAKE_PROFIT_MARKET di /fapi/v1/order dengan -4120;
    #: ``auto`` = coba Algo Order API lebih dulu, jatuh ke endpoint lama bila
    #: ditolak. ``legacy`` hanya untuk bursa/uji yang belum migrasi.
    conditional_order_api: Literal["auto", "algo", "legacy"] = "auto"

    @model_validator(mode="after")
    def _leverage_order(self) -> ExchangeConfig:
        if self.default_leverage > self.max_leverage:
            raise ValueError(
                f"default_leverage ({self.default_leverage}) tidak boleh melebihi "
                f"max_leverage ({self.max_leverage})"
            )
        return self


class NetConfig(_Strict):
    """Pengaturan jaringan (TAHAP 2)."""

    rest_base_url: str = ""
    ws_base_url: str = ""
    legacy_testnet_rest: bool = False
    ws_idle_timeout_sec: float = Field(default=120.0, ge=15, le=600)
    ws_rotate_hours: float = Field(default=23.0, ge=1, le=23.5)
    ws_max_streams: int = Field(default=200, ge=1, le=1024)
    rest_concurrency: int = Field(default=4, ge=1, le=10)
    kline_pace_sec: float = Field(default=0.05, ge=0, le=5)

    @field_validator("rest_base_url", "ws_base_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        if value and not value.startswith(("http://", "https://", "ws://", "wss://")):
            raise ValueError(f"URL harus diawali http(s):// atau ws(s)://: {value!r}")
        return value.rstrip("/")

    def resolve_rest_url(self, *, testnet: bool) -> str:
        if self.rest_base_url:
            return self.rest_base_url
        from volumatic_bot.binance.urls import Endpoint

        return Endpoint.rest(testnet=testnet, legacy_testnet=self.legacy_testnet_rest)

    def resolve_ws_url(self, *, testnet: bool) -> str:
        if self.ws_base_url:
            return self.ws_base_url
        from volumatic_bot.binance.urls import Endpoint

        return Endpoint.ws(testnet=testnet)


class IndicatorConfig(_Strict):
    vidya_length: int = Field(default=10, ge=1, le=500)
    vidya_momentum: int = Field(default=20, ge=1, le=500)
    band_distance: float = Field(default=2.0, gt=0, le=20)
    atr_length: int = Field(default=200, ge=1, le=1000)
    sma_smooth: int = Field(default=15, ge=1, le=500)
    pivot_left: int = Field(default=3, ge=0, le=50)
    pivot_right: int = Field(default=3, ge=0, le=50)
    source: Literal["close", "hl2", "ohlc4"] = "close"
    pivot_low_source: Literal["low", "close"] = "close"
    pivot_high_source: Literal["high", "close"] = "high"
    zone_lookback_bars: int = Field(default=50, ge=1, le=500)
    volume_filter: bool = True
    volume_filter_source: Literal["rolling", "since_flip"] = "rolling"
    volume_delta_window: int = Field(default=20, ge=1, le=500)
    volume_delta_min_pct: float = Field(default=0.0, ge=-100, le=100)

    def to_params(self) -> IndicatorParams:
        """Konversi ke dataclass yang dipakai mesin indikator."""
        return IndicatorParams(
            vidya_length=self.vidya_length,
            vidya_momentum=self.vidya_momentum,
            band_distance=self.band_distance,
            atr_length=self.atr_length,
            sma_smooth=self.sma_smooth,
            pivot_left=self.pivot_left,
            pivot_right=self.pivot_right,
            source=self.source,
            pivot_low_source=self.pivot_low_source,
            pivot_high_source=self.pivot_high_source,
            zone_lookback_bars=self.zone_lookback_bars,
            volume_filter=self.volume_filter,
            volume_filter_source=self.volume_filter_source,
            volume_delta_window=self.volume_delta_window,
            volume_delta_min_pct=self.volume_delta_min_pct,
        )


class ScannerConfig(_Strict):
    timeframe: Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"] = "15m"
    max_symbols: int = Field(default=30, ge=1, le=200)
    symbol_refresh_hours: float = Field(default=4.0, ge=0.25, le=24)
    min_quote_volume_24h_usdt: float = Field(default=50_000_000, ge=0)
    max_spread_pct: float = Field(default=0.05, gt=0, le=5)
    min_atr_pct: float = Field(default=0.25, ge=0, le=50)
    max_atr_pct: float = Field(default=6.0, gt=0, le=100)
    min_price: float = Field(default=0.00001, gt=0)
    quote_asset: Literal["USDT"] = "USDT"
    contract_type: Literal["PERPETUAL"] = "PERPETUAL"
    whitelist: list[str] = Field(default_factory=list)
    blacklist: list[str] = Field(default_factory=list)
    exclude_if_min_notional_exceeds_margin: bool = True
    # 0 = nonaktif. Bila diisi, simbol yang minNotional-nya di atas angka ini
    # dibuang saat scan — penyaring paling efektif untuk modal kecil karena
    # minNotional-lah yang menentukan risiko minimum yang bisa dipasang.
    max_min_notional_usdt: float = Field(default=0.0, ge=0, le=1000)
    warmup_bars: int = Field(default=1500, ge=250, le=1500)
    kline_rest_limit: int = Field(default=1500, ge=100, le=1500)

    @model_validator(mode="after")
    def _ranges(self) -> ScannerConfig:
        if self.min_atr_pct >= self.max_atr_pct:
            raise ValueError("scanner.min_atr_pct harus lebih kecil dari max_atr_pct")
        overlap = set(self.whitelist) & set(self.blacklist)
        if overlap:
            raise ValueError(f"simbol ada di whitelist dan blacklist sekaligus: {sorted(overlap)}")
        return self

    @field_validator("whitelist", "blacklist")
    @classmethod
    def _normalize_symbols(cls, values: list[str]) -> list[str]:
        out = []
        for raw in values:
            symbol = raw.strip().upper()
            if not symbol:
                continue
            if not symbol.isalnum():
                raise ValueError(f"nama simbol tidak valid: {raw!r}")
            out.append(symbol)
        return out


class OrderBookConfig(_Strict):
    """Filter order book (TAHAP 3).

    Catatan jujur: order book bisa dimanipulasi (spoofing). Filter ini adalah
    lapisan tambahan, bukan sinyal utama.
    """

    enabled: bool = True
    source: Literal["rest", "stream"] = "rest"
    depth_limit: Literal[5, 10, 20, 50, 100, 500, 1000] = 20
    stream_levels: Literal[5, 10, 20] = 20
    stream_speed_ms: Literal[100, 250, 500] = 100
    imbalance_range_pct: float = Field(default=0.3, gt=0, le=5)
    wall_multiplier: float = Field(default=8.0, ge=1, le=100)
    wall_proximity_atr: float = Field(default=1.0, gt=0, le=10)
    max_spread_pct: float = Field(default=0.05, gt=0, le=5)
    max_slippage_pct: float = Field(default=0.08, gt=0, le=5)
    min_score_to_enter: float = Field(default=0.0, ge=0, le=1)
    depth_snapshot_ttl_sec: float = Field(default=3.0, ge=0.5, le=60)
    reject_if_unavailable: bool = True
    # Ukuran order (notional USDT) untuk estimasi slippage. TAHAP 4 akan memberi
    # angka sebenarnya dari perhitungan risiko; nilai ini hanya fallback.
    target_notional_usdt: float = Field(default=20.0, gt=0, le=100_000)


class ScoringConfig(_Strict):
    """Bobot penggabungan skor saat beberapa simbol memberi sinyal bersamaan."""

    weight_trend: float = Field(default=0.4, ge=0, le=1)
    weight_volume: float = Field(default=0.3, ge=0, le=1)
    weight_book: float = Field(default=0.3, ge=0, le=1)
    # Nilai |CMO| yang dianggap "tren kuat" (untuk normalisasi skor 0..1).
    trend_cmo_ref: float = Field(default=50.0, gt=1, le=100)

    @model_validator(mode="after")
    def _weights_sum(self) -> ScoringConfig:
        total = self.weight_trend + self.weight_volume + self.weight_book
        if total <= 0:
            raise ValueError("jumlah bobot skor harus > 0")
        return self

    @property
    def normalized(self) -> tuple[float, float, float]:
        """Bobot dinormalisasi agar berjumlah tepat 1.0."""
        total = self.weight_trend + self.weight_volume + self.weight_book
        return (
            self.weight_trend / total,
            self.weight_volume / total,
            self.weight_book / total,
        )


class RiskConfig(_Strict):
    """Batas risiko (TAHAP 4). Semua angka di sini adalah pagar keras."""

    #: Risiko per trade (% equity). Di atas 2% hanya boleh bila
    #: ``allow_aggressive_risk: true`` — lihat komentar di validator.
    risk_per_trade_pct: float = Field(default=1.5, ge=0.1, le=25.0)
    #: Saklar sadar-risiko. Membuka: risiko >2%, margin per simbol >60%,
    #: margin total >80%, dan equity minimum <20 USDT. Tanpa ini, bot menolak
    #: konfigurasi tersebut (bukan sekadar memperingatkan).
    allow_aggressive_risk: bool = False
    max_concurrent_positions: int = Field(default=2, ge=1, le=5)
    max_total_margin_pct: float = Field(default=60.0, ge=5, le=95)
    #: Batas konsentrasi: margin satu simbol tidak boleh melebihi ini.
    max_per_symbol_margin_pct: float = Field(default=30.0, ge=5, le=100)
    daily_loss_limit_pct: float = Field(default=3.0, gt=0, le=10)
    daily_loss_reset_hour_local: int = Field(default=0, ge=0, le=23)
    consecutive_loss_limit: int = Field(default=3, ge=1, le=10)
    cooldown_minutes: int = Field(default=120, ge=5, le=1440)
    max_trades_per_day: int = Field(default=20, ge=1, le=200)
    min_liq_distance_pct: float = Field(default=1.0, ge=0.1, le=20)
    max_qty_round_up_pct: float = Field(default=2.0, ge=0, le=20)
    max_risk_overshoot_factor: float = Field(default=2.0, ge=1.0, le=3.0)
    #: Equity minimum agar bot mau jalan (di bawah ini biaya terlalu dominan).
    # Lantai keras 1 USDT. Binance sendiri sudah membatasi lewat minNotional
    # (5 USDT) dan leverage; angka ini hanya mencegah equity 0/negatif ikut
    # dihitung. Turun di bawah 20 USDT wajib `allow_aggressive_risk: true`.
    min_equity_usdt: float = Field(default=20.0, ge=1, le=1000)
    #: Di bawah nilai ini bot menandai "modal kecil" (peringatan biaya).
    small_capital_usdt: float = Field(default=50.0, ge=10, le=10_000)
    #: Maintenance margin rate yang dipakai memperkirakan harga likuidasi.
    maintenance_margin_rate: float = Field(default=0.004, ge=0, le=0.2)
    #: True = saat batas rugi harian tercapai, tutup semua posisi (TAHAP 5).
    close_all_on_daily_limit: bool = True

    @model_validator(mode="after")
    def _aggressive_guard(self) -> RiskConfig:
        """Pagar berlapis untuk setelan agresif (permintaan "all in").

        Kenapa tidak ada 100%: ukuran posisi dihitung dari **jarak SL**, jadi
        ``risk_pct = 100`` berarti notional = equity / jarak_SL. Untuk SL 2% itu
        notional 50x equity — mustahil dengan leverage maksimum 10x. Bot akan
        menolak SEMUA entry (bukan "all in"), dan kalau pun bisa, satu SL kena =
        modal habis. Karena itu batas atasnya 25%, dan itu sudah ekstrem.
        """
        if self.risk_per_trade_pct > 25.0:
            raise ValueError(
                "risk_per_trade_pct maksimum 25%. 'All in 100%' tidak didukung: "
                "ukuran posisi = risiko / jarak SL, sehingga risiko 100% menuntut "
                "notional puluhan kali equity (ditolak batas margin) — hasilnya "
                "bukan all-in, melainkan TIDAK ADA trade. Untuk memakai modal "
                "sebesar mungkin, naikkan max_per_symbol_margin_pct, bukan risiko."
            )
        agresif = (
            self.risk_per_trade_pct > 2.0
            or self.max_per_symbol_margin_pct > 60.0
            or self.max_total_margin_pct > 80.0
            or self.min_equity_usdt < 20.0
        )
        if agresif and not self.allow_aggressive_risk:
            bagian = []
            if self.risk_per_trade_pct > 2.0:
                bagian.append(f"risiko {self.risk_per_trade_pct}% > 2%")
            if self.max_per_symbol_margin_pct > 60.0:
                bagian.append(f"margin/simbol {self.max_per_symbol_margin_pct}% > 60%")
            if self.max_total_margin_pct > 80.0:
                bagian.append(f"margin total {self.max_total_margin_pct}% > 80%")
            if self.min_equity_usdt < 20.0:
                bagian.append(f"equity minimum {self.min_equity_usdt} < 20 USDT")
            raise ValueError(
                "setelan risiko melewati batas aman ("
                + "; ".join(bagian)
                + "). Bila ini memang disengaja, set risk.allow_aggressive_risk: true "
                "di config.yaml — dan baca docs/modal-kecil.md dulu."
            )
        return self


class TradeConfig(_Strict):
    entry_mode: Literal["market", "limit_fallback"] = "limit_fallback"
    entry_limit_timeout_sec: float = Field(default=5.0, gt=0, le=60)
    entry_limit_offset_ticks: int = Field(default=0, ge=0, le=50)
    sl_mode: Literal["atr", "vidya_trail", "nearest"] = "nearest"
    sl_atr_mult: float = Field(default=1.5, gt=0, le=10)
    sl_min_atr_mult: float = Field(default=0.8, gt=0, le=10)
    tp1_r: float = Field(default=1.5, gt=0, le=20)
    partial_tp_enabled: bool = True
    partial_tp_pct: float = Field(default=50.0, gt=0, le=100)
    be_trigger_r: float = Field(default=1.0, ge=0, le=20)
    be_buffer_pct: float = Field(default=0.03, ge=0, le=2)
    trailing_mode: Literal["native", "bot"] = "bot"
    trailing_activation_r: float = Field(default=1.0, ge=0, le=20)
    trailing_callback_rate_pct: float = Field(default=0.5, gt=0.1, le=10)
    trailing_atr_mult: float = Field(default=2.0, gt=0, le=10)
    exit_on_trend_flip: bool = True
    funding_aware_bars: int = Field(default=32, ge=0, le=1000)
    #: Biaya bolak-balik maksimum yang masih diterima, dalam satuan R.
    max_fee_r: float = Field(default=0.5, gt=0, le=2.0)

    @model_validator(mode="after")
    def _sanities(self) -> TradeConfig:
        if self.sl_min_atr_mult > self.sl_atr_mult:
            raise ValueError(
                "trade.sl_min_atr_mult tidak boleh lebih besar dari sl_atr_mult "
                "(batas minimum SL akan selalu menang)"
            )
        if self.be_trigger_r > self.tp1_r:
            # Bukan mustahil, tapi hampir selalu salah konfigurasi.
            raise ValueError("be_trigger_r sebaiknya <= tp1_r agar BE aktif sebelum TP1")
        if not self.partial_tp_enabled and self.partial_tp_pct >= 100:
            raise ValueError("partial_tp_pct 100 berarti tidak ada sisa posisi untuk trailing")
        return self


class CommissionConfig(_Strict):
    use_account_commission: bool = True
    maker_bps: float = Field(default=2.0, ge=0, le=100)
    taker_bps: float = Field(default=5.0, ge=0, le=100)
    slippage_bps: float = Field(default=2.0, ge=0, le=100)
    #: Dipakai backtest bila data funding historis tidak tersedia (persen / 8 jam).
    funding_rate_8h_pct: float = Field(default=0.01, ge=-1, le=1)


class DataConfig(_Strict):
    """Bagian ``data:`` — lokasi database.

    Ada **dua** database dengan tujuan berbeda:

    * ``db_path`` — posisi, order, insiden, dan state harian. Ini data **modal
      sungguhan**, jadi dipisah per mode (``separate_by_mode``): posisi dari
      ``simulate-session`` tidak boleh dianggap posisi nyata oleh rekonsiliasi
      saat mode ``testnet``/``live``.
    * ``cache_db_path`` — cache candle. Ini data **pasar** yang sama untuk semua
      mode, jadi sengaja dipakai bersama supaya tidak mengunduh 1500 candle
      berkali-kali.
    """

    cache_dir: str = "var/cache"
    db_path: str = "var/bot.db"
    #: Pisahkan database posisi per mode: var/bot-dry_run.db, var/bot-testnet.db, dll.
    separate_by_mode: bool = True
    #: Cache candle dipakai bersama semua mode (kosongkan = ikut berkas posisi).
    cache_db_path: str = "var/cache.db"
    parquet_cache: bool = True
    max_cache_symbols: int = Field(default=60, ge=1, le=500)


class DashboardConfig(_Strict):
    """Bagian ``dashboard:`` — hanya tampilan, tidak memengaruhi order."""

    enabled: bool = True
    refresh_sec: float = Field(default=1.5, ge=0.5, le=10)
    max_log_lines: int = Field(default=12, ge=0, le=100)
    max_signals_shown: int = Field(default=10, ge=1, le=100)
    #: Tampilkan panel ringkas satu blok (cocok untuk layar sempit / log berkas).
    compact: bool = False
    #: Mode layar penuh (alternate screen) untuk tampilan dinamis: kebal
    #: duplikasi frame saat terminal di-resize; ``false`` = mode inline lama.
    layar_penuh: bool = True
    #: Ikut menampilkan posisi yang sudah tertutup pada panel posisi.
    show_history: bool = True
    history_rows: int = Field(default=10, ge=0, le=50)


class ProfilOtomatisConfig(_Strict):
    """Deteksi profil berdasarkan saldo saat sesi dimulai.

    Keputusan diambil SEKALI di awal sesi (bukan saat berjalan) supaya parameter
    risiko tidak berganti di tengah posisi terbuka. Pilihan manual (``--profil``
    di CLI atau ``profil_aktif`` di file) selalu menang atas deteksi ini.
    """

    aktif: bool = False
    ambang_sangat_kecil_usdt: float = Field(default=10.0, gt=0)
    ambang_kecil_usdt: float = Field(default=50.0, gt=0)
    profil_sangat_kecil: str = "modal-5usdt"
    profil_kecil: str = "modal-kecil"
    #: Profil untuk modal normal; kosong berarti nilai dasar config tanpa profil.
    profil_default: str = ""

    @model_validator(mode="after")
    def _urutan_ambang(self) -> ProfilOtomatisConfig:
        if self.ambang_sangat_kecil_usdt > self.ambang_kecil_usdt:
            raise ValueError(
                "profil_otomatis.ambang_sangat_kecil_usdt tidak boleh lebih besar "
                "dari ambang_kecil_usdt"
            )
        return self


class NotifyConfig(_Strict):
    """Bagian ``notify:`` — bawaan: konsol/log saja, tanpa layanan luar."""

    console_enabled: bool = True
    #: Kanal Telegram **belum diimplementasikan**; bila true bot memberi
    #: peringatan dan tetap memakai konsol.
    telegram_enabled: bool = False
    telegram_chat_id: str = ""
    notify_on_trade: bool = True
    notify_on_error: bool = True
    min_level: Literal["info", "success", "warning", "critical"] = "info"
    #: Jeda minimum sebelum pesan sejenis dicetak lagi (detik).
    dedupe_sec: float = Field(default=60.0, ge=0, le=3600)
    max_per_minute: int = Field(default=30, ge=1, le=600)

    @model_validator(mode="after")
    def _chat_id_required(self) -> NotifyConfig:
        if self.telegram_enabled and not self.telegram_chat_id.strip():
            raise ValueError("telegram_enabled: true butuh notify.telegram_chat_id")
        return self


class AppConfig(_Strict):
    """Root konfigurasi (satu file YAML)."""

    app: AppInfo = Field(default_factory=AppInfo)
    ops: OpsConfig = Field(default_factory=OpsConfig)
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    net: NetConfig = Field(default_factory=NetConfig)
    indicator: IndicatorConfig = Field(default_factory=IndicatorConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    orderbook: OrderBookConfig = Field(default_factory=OrderBookConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    trade: TradeConfig = Field(default_factory=TradeConfig)
    commission: CommissionConfig = Field(default_factory=CommissionConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    profil_otomatis: ProfilOtomatisConfig = Field(default_factory=ProfilOtomatisConfig)

    # ------------------------------------------------------------- database
    def mode_slug(self) -> str:
        """Nama mode untuk nama berkas (``dry_run`` -> ``dry-run``)."""
        return str(self.ops.mode).replace("_", "-")

    def db_posisi(self) -> str:
        """Berkas database **posisi/state** untuk mode yang sedang aktif.

        ``data.separate_by_mode: true`` (bawaan) menambahkan akhiran mode:
        ``var/bot.db`` -> ``var/bot-testnet.db``. Tujuannya bukan sekadar
        kerapian: rekonsiliasi membaca berkas ini dan membandingkannya dengan
        posisi di bursa, jadi posisi hasil simulasi tidak boleh tercampur.
        """
        dasar = self.data.db_path
        if not self.data.separate_by_mode:
            return dasar
        jalur = Path(dasar)
        akhiran = self.mode_slug()
        if jalur.suffix:
            return str(jalur.with_name(f"{jalur.stem}-{akhiran}{jalur.suffix}"))
        return f"{dasar}-{akhiran}"

    def pid_file(self) -> str:
        """Berkas PID proses ``run`` untuk mode aktif (mis. ``var/bot-testnet.pid``).

        Dipakai perintah ``stop`` untuk menemukan proses yang harus dihentikan —
        tanpa perlu ``ps -ef | grep`` yang mudah salah sasaran.
        """
        return str(Path(self.db_posisi()).with_suffix(".pid"))

    def db_cache(self) -> str:
        """Berkas database **cache candle** (dipakai bersama semua mode)."""
        return self.data.cache_db_path or self.db_posisi()

    def fingerprint(self) -> str:
        """Hash pendek konfigurasi (tanpa secret) untuk log/audit."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()[:12]


class Secrets(BaseSettings):
    """Kredensial: hanya dari environment / file .env. Tidak pernah di-log."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None

    @property
    def has_api_credentials(self) -> bool:
        return self.binance_api_key is not None and self.binance_api_secret is not None

    def api_key(self) -> str:
        if self.binance_api_key is None:
            raise ConfigError("BINANCE_API_KEY belum diset di environment")
        return self.binance_api_key.get_secret_value()

    def api_secret(self) -> str:
        if self.binance_api_secret is None:
            raise ConfigError("BINANCE_API_SECRET belum diset di environment")
        return self.binance_api_secret.get_secret_value()

    def telegram_token(self) -> str | None:
        return self.telegram_bot_token.get_secret_value() if self.telegram_bot_token else None

    def secret_values(self) -> list[str]:
        """Daftar nilai rahasia (untuk filter log). Jangan disimpan/di-log."""
        values = []
        for field in ("binance_api_key", "binance_api_secret", "telegram_bot_token"):
            secret: SecretStr | None = getattr(self, field)
            if secret is not None:
                raw = secret.get_secret_value()
                if raw:
                    values.append(raw)
        return values


def mask_secret(value: str | None, *, keep: int = 4) -> str:
    """Tampilkan sebagian kecil secret untuk keperluan debug yang aman."""
    if not value:
        return "<kosong>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def baca_yaml(path: str | Path = "config.yaml") -> dict[str, object]:
    """Baca ``config.yaml`` mentah (tanpa validasi), untuk inspeksi profil."""
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"file konfigurasi tidak ditemukan: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML tidak valid di {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("isi config harus mapping (key: value) di level atas")
    return raw


def daftar_profil(raw: dict[str, object]) -> dict[str, dict[str, object]]:
    """Ambil bagian ``profil:`` dari config mentah beserta validasi bentuknya."""
    isi = raw.get("profil", {})
    if isi in (None, ""):
        return {}
    if not isinstance(isi, dict):
        raise ConfigError("bagian 'profil' harus mapping: profil: {nama: {bagian: {...}}}")
    hasil: dict[str, dict[str, object]] = {}
    for nama, nilai in isi.items():
        if not isinstance(nilai, dict):
            raise ConfigError(f"profil {nama!r} harus mapping berisi bagian config")
        hasil[str(nama)] = {str(k): v for k, v in nilai.items()}
    return hasil


def profil_aktif_dari(raw: dict[str, object], profil: str | None = None) -> str:
    """Nama profil yang akan dipakai: argumen CLI menang atas ``profil_aktif``."""
    if profil:
        return profil
    nilai = raw.get("profil_aktif", "")
    return str(nilai).strip() if nilai else ""


def _gabung_dalam(dasar: dict[str, object], timpa: dict[str, object]) -> dict[str, object]:
    """Gabung dua mapping secara rekursif: nilai di ``timpa`` menang."""
    hasil = dict(dasar)
    for kunci, nilai in timpa.items():
        lama = hasil.get(kunci)
        if isinstance(lama, dict) and isinstance(nilai, dict):
            hasil[kunci] = _gabung_dalam(dasar=lama, timpa=nilai)
        else:
            hasil[kunci] = nilai
    return hasil


def terapkan_profil(
    raw: dict[str, object], profil: str | None = None
) -> tuple[dict[str, object], str]:
    """Terapkan profil terpilih ke config mentah.

    Kunci ``profil`` dan ``profil_aktif`` selalu dibuang sebelum validasi —
    keduanya milik lapisan pemilihan, bukan ``AppConfig`` (yang ``extra=forbid``).
    Bila nama profil tidak dikenal, pesan errornya menyebutkan pilihan yang ada
    supaya salah ketik tidak berujung teka-teki.
    """
    nama = profil_aktif_dari(raw, profil)
    tersedia = daftar_profil(raw)
    bersih = {k: v for k, v in raw.items() if k not in ("profil", "profil_aktif")}
    if not nama:
        return bersih, ""
    if nama not in tersedia:
        pilihan = ", ".join(sorted(tersedia)) or "(tidak ada)"
        raise ConfigError(f"profil {nama!r} tidak ada di config. Pilihan: {pilihan}")
    lapisan = {k: v for k, v in tersedia[nama].items() if k != "profil_otomatis"}
    return _gabung_dalam(bersih, lapisan), nama


def load_config(path: str | Path = "config.yaml", profil: str | None = None) -> AppConfig:
    """Baca YAML dan validasi menjadi :class:`AppConfig`.

    Bila ``config.yaml`` memuat ``profil_aktif: "<nama>"`` (atau argumen
    ``profil`` diisi), nilai pada ``profil.<nama>`` menimpa nilai biasa lebih
    dulu. Semuanya tetap **satu file** — tidak ada config kedua yang bisa
    menyimpang.
    """
    raw, _nama = terapkan_profil(baca_yaml(path), profil)
    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError -> ConfigError yang ramah
        raise ConfigError(f"konfigurasi tidak valid: {exc}") from exc


def load_secrets(env_file: str | Path | None = None) -> Secrets:
    """Baca secret dari environment (atau ``.env`` bila ada)."""
    if env_file is not None:
        os.environ.setdefault("_ENV_FILE_HINT", str(env_file))
    return Secrets()


def pilih_profil_otomatis(pengaturan: ProfilOtomatisConfig, *, saldo: float) -> str | None:
    """Nama profil hasil deteksi saldo; ``None`` bila modal normal / deteksi mati.

    Pemetaan: ``saldo < ambang_sangat_kecil`` -> profil sangat kecil;
    ``< ambang_kecil`` -> profil kecil; selain itu ``profil_default``
    (kosong berarti tanpa profil).
    """
    if not pengaturan.aktif or saldo <= 0:
        return None
    if saldo < pengaturan.ambang_sangat_kecil_usdt:
        nama = pengaturan.profil_sangat_kecil
    elif saldo < pengaturan.ambang_kecil_usdt:
        nama = pengaturan.profil_kecil
    else:
        nama = pengaturan.profil_default
    return nama or None


def deteksi_profil_otomatis(
    config: AppConfig,
    path: str | Path,
    *,
    profil_cli: str | None,
    saldo: float,
) -> str | None:
    """Profil otomatis untuk ``saldo`` pada config di ``path``.

    Mengembalikan ``None`` (tidak ada deteksi) bila: deteksi nonaktif, saldo
    tidak valid, atau ada pilihan manual (``--profil`` / ``profil_aktif``).
    Pilihan manual selalu lebih kuat supaya perilaku bot tetap prediktabel.
    """
    if profil_cli:
        return None
    try:
        raw = baca_yaml(path)
    except ConfigError:
        return None
    if profil_aktif_dari(raw):
        return None
    return pilih_profil_otomatis(config.profil_otomatis, saldo=saldo)
