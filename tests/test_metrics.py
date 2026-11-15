"""Test pengukur bandwidth/latensi/bobot."""

from __future__ import annotations

import pytest

from volumatic_bot.metrics import Metrics


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_bandwidth_nol_tanpa_trafik() -> None:
    metrics = Metrics()
    assert metrics.bandwidth_mb_per_hour() == 0.0
    assert metrics.snapshot().total_bytes == 0


def test_estimasi_bandwidth_dari_jendela() -> None:
    clock = FakeClock()
    metrics = Metrics(window_sec=60.0, clock=clock)
    # 1 MB dalam 10 detik -> 360 MB/jam (murni ekstrapolasi jendela).
    metrics.record_ws_message(1024 * 1024)
    clock.advance(10.0)
    assert metrics.bandwidth_mb_per_hour() == pytest.approx(360.0, rel=0.01)

    # Data lama keluar dari jendela -> estimasi turun ke 0.
    clock.advance(120.0)
    assert metrics.bandwidth_mb_per_hour() == 0.0


def test_jendela_pendek_tidak_menghasilkan_angka_ngawur() -> None:
    """Jendela < 5 detik: lebih baik lapor 0 (belum ada data) daripada menebak."""
    clock = FakeClock()
    metrics = Metrics(window_sec=300.0, clock=clock)
    metrics.record_http(rx_bytes=10_000, tx_bytes=1_000)
    assert metrics.bandwidth_mb_per_hour() == 0.0
    clock.advance(5.0)
    value = metrics.bandwidth_mb_per_hour()
    assert 0 < value < 1000


def test_penghitung_http_dan_ws() -> None:
    metrics = Metrics()
    metrics.record_http(rx_bytes=100, tx_bytes=50)
    metrics.record_http(rx_bytes=200, retry=True, error=True)
    metrics.record_ws_message(300)
    snap = metrics.snapshot()
    assert snap.http_requests == 2
    assert snap.http_retries == 1
    assert snap.http_errors == 1
    assert snap.http_rx_bytes == 300
    assert snap.http_tx_bytes == 50
    assert snap.ws_messages == 1
    assert snap.total_bytes == 300 + 50 + 300


def test_bobot_rate_limit_dan_persentase() -> None:
    metrics = Metrics(weight_limit=2400)
    metrics.record_used_weight(1200)
    assert metrics.snapshot().weight_pct == pytest.approx(50.0)
    metrics.record_used_weight(5000)
    assert metrics.snapshot().weight_pct == 100.0


def test_latensi_ema() -> None:
    metrics = Metrics(latency_ema_alpha=0.5)
    metrics.record_ws_latency(100.0)
    assert metrics.snapshot().ws_latency_ema_ms == pytest.approx(100.0)
    metrics.record_ws_latency(200.0)
    assert metrics.snapshot().ws_latency_ema_ms == pytest.approx(150.0)
    # Nilai tidak masuk akal diabaikan (mis. jam server ngawur).
    metrics.record_ws_latency(-5.0)
    metrics.record_ws_latency(1e9)
    assert metrics.snapshot().ws_latency_ema_ms == pytest.approx(150.0)


def test_reconnect_dihitung() -> None:
    metrics = Metrics()
    metrics.record_ws_reconnect()
    metrics.record_ws_reconnect()
    assert metrics.snapshot().ws_reconnects == 2
