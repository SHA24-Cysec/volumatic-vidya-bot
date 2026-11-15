"""Parameter indikator Volumatic VIDYA beserta validasinya.

Nilai default mengikuti spesifikasi proyek dan setelan awal indikator asli
(BigBeluga). Semua parameter divalidasi dengan batas aman agar konfigurasi
tidak bisa membuat bot menghitung sesuatu yang tidak masuk akal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["PRICE_SOURCES", "PRICE_SOURCE_LABEL", "IndicatorParams"]

PRICE_SOURCES = ("close", "hl2", "ohlc4")
PRICE_SOURCE_LABEL = {
    "close": "close",
    "hl2": "(high + low) / 2",
    "ohlc4": "(open + high + low + close) / 4",
}


@dataclass(frozen=True, slots=True)
class IndicatorParams:
    """Parameter indikator. Semua angka dalam satuan bar (candle) kecuali disebut lain."""

    vidya_length: int = 10
    vidya_momentum: int = 20
    band_distance: float = 2.0
    atr_length: int = 200
    sma_smooth: int = 15
    pivot_left: int = 3
    pivot_right: int = 3
    source: Literal["close", "hl2", "ohlc4"] = "close"

    # --- Perbedaan dengan skrip asli (lihat README bagian "Atribusi & lisensi") ---
    # Skrip asli memakai *close* untuk pivot low dan *high* untuk pivot high.
    # Default di sini mempertahankan perilaku asli ("port apa adanya"), tetapi
    # bisa diseragamkan lewat dua flag di bawah.
    pivot_low_source: Literal["low", "close"] = "close"
    pivot_high_source: Literal["high", "close"] = "high"

    # --- Filter tambahan (opsional) ---
    # Temuan penting (lihat docs/tahap-1-indikator.md): delta% "sejak tren berganti"
    # bernilai +-200 atau 0 pada bar pergantian tren itu sendiri, karena baru satu
    # candle yang terakumulasi. Filter yang memakai nilai itu jadi tidak bermakna.
    # Karena itu filter default memakai delta% jendela bergulir, sementara nilai
    # since-flip tetap dihitung & ditampilkan sesuai spesifikasi.
    volume_filter: bool = True
    volume_filter_source: Literal["rolling", "since_flip"] = "rolling"
    volume_delta_window: int = 20  # panjang jendela delta% untuk filter
    volume_delta_min_pct: float = 0.0  # ambang magnitudo delta% searah trade

    # --- Zona likuiditas ---
    zone_lookback_bars: int = 50

    def __post_init__(self) -> None:
        def _check_int(name: str, value: int, low: int, high: int) -> None:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} harus bilangan bulat, diterima {value!r}")
            if not low <= value <= high:
                raise ValueError(f"{name} harus di antara {low} dan {high}, diterima {value}")

        _check_int("vidya_length", self.vidya_length, 1, 500)
        _check_int("vidya_momentum", self.vidya_momentum, 1, 500)
        _check_int("atr_length", self.atr_length, 1, 1000)
        _check_int("sma_smooth", self.sma_smooth, 1, 500)
        _check_int("pivot_left", self.pivot_left, 0, 50)
        _check_int("pivot_right", self.pivot_right, 0, 50)
        _check_int("zone_lookback_bars", self.zone_lookback_bars, 1, 500)

        if not isinstance(self.band_distance, (int, float)) or isinstance(self.band_distance, bool):
            raise ValueError(f"band_distance harus angka, diterima {self.band_distance!r}")
        if not 0.0 < float(self.band_distance) <= 20.0:
            raise ValueError(
                f"band_distance harus di antara 0 (eksklusif) dan 20, diterima {self.band_distance}"
            )
        _check_int("volume_delta_window", self.volume_delta_window, 1, 500)
        if self.volume_filter_source not in ("rolling", "since_flip"):
            raise ValueError(f"volume_filter_source tidak valid: {self.volume_filter_source!r}")
        if not isinstance(self.volume_delta_min_pct, (int, float)):
            raise ValueError("volume_delta_min_pct harus angka")
        if not -100.0 <= float(self.volume_delta_min_pct) <= 100.0:
            raise ValueError("volume_delta_min_pct harus di antara -100 dan 100")
        if self.source not in PRICE_SOURCES:
            raise ValueError(
                f"source harus salah satu dari {PRICE_SOURCES}, diterima {self.source!r}"
            )
        for name in ("pivot_low_source", "pivot_high_source"):
            if getattr(self, name) not in ("low", "high", "close"):
                raise ValueError(f"{name} tidak valid: {getattr(self, name)!r}")

    @property
    def alpha(self) -> float:
        """Faktor pemulusan dasar VIDYA: ``2 / (length + 1)``."""
        return 2.0 / (self.vidya_length + 1)

    @property
    def warmup_bars(self) -> int:
        """Jumlah candle minimum yang wajar sebelum nilai indikator dipakai.

        Rinciannya:
        * ``atr_length`` bar agar seed RMA(ATR) terbentuk,
        * ``sma_smooth`` bar agar SMA penghalus VIDYA penuh,
        * ``vidya_momentum`` bar agar CMO punya jendela penuh.

        Angka ini adalah batas bawah teoretis. Studi empiris di
        ``docs/tahap-1-indikator.md`` menunjukkan konvergensi penuh baru terjadi
        lebih lambat karena sifat rekursif VIDYA dan RMA.
        """
        return self.atr_length + self.sma_smooth + self.vidya_momentum
