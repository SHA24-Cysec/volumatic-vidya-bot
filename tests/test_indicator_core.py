"""Test inti indikator Volumatic VIDYA.

Fokus:
1. kesamaan hasil dengan implementasi referensi murni-Python (oracle),
2. tidak ada lookahead/repainting (prefiks data harus memberi hasil identik),
3. semantik tren, trail, volume delta, dan zona likuiditas,
4. kasus tepi (pembagian nol, data pendek, validasi parameter).
"""

from __future__ import annotations

import numpy as np
import pytest

from volumatic_bot.indicator import IndicatorParams, TrendState, compute, entry_signal
from volumatic_bot.indicator.reference import compute_reference
from volumatic_bot.models import CandleSeries


def _as_arrays(ref) -> dict[str, np.ndarray]:  # type: ignore[no-untyped-def]
    def to_arr(values):  # type: ignore[no-untyped-def]
        return np.array([np.nan if v is None else v for v in values], dtype=np.float64)

    return {
        "cmo": to_arr(ref.cmo),
        "vidya_raw": to_arr(ref.vidya_raw),
        "vidya": to_arr(ref.vidya),
        "atr": to_arr(ref.atr),
        "upper": to_arr(ref.upper),
        "lower": to_arr(ref.lower),
        "trail": to_arr(ref.trail),
        "trend": np.array(ref.trend, dtype=np.int8),
        "vol_delta_pct": to_arr(ref.vol_delta_pct),
        "vol_delta_win_pct": to_arr(ref.vol_delta_win_pct),
    }


def test_hasil_identik_dengan_implementasi_referensi(sample_candles: CandleSeries) -> None:
    """Vektor vs loop murni: selisih hanya galat pembulatan floating point."""
    params = IndicatorParams()
    result = compute(sample_candles, params)
    ref = compute_reference(
        sample_candles.open_time.tolist(),
        sample_candles.open.tolist(),
        sample_candles.high.tolist(),
        sample_candles.low.tolist(),
        sample_candles.close.tolist(),
        sample_candles.volume.tolist(),
        params,
    )
    arrays = _as_arrays(ref)

    # Status tren & sinyal harus sama persis, bukan "hampir sama".
    np.testing.assert_array_equal(result.trend, arrays["trend"])
    np.testing.assert_array_equal(result.cross_up, np.array(ref.cross_up, dtype=bool))
    np.testing.assert_array_equal(result.cross_down, np.array(ref.cross_down, dtype=bool))

    # Pola nilai na (nan) harus sama persis (penting: menandakan warm-up identik).
    for name in ("vidya", "atr", "upper", "lower", "trail", "vol_delta_pct", "vol_delta_win_pct"):
        np.testing.assert_array_equal(
            np.isnan(getattr(result, name)), np.isnan(arrays[name]), err_msg=name
        )

    # Nilai numerik: toleransi relatif 1e-9 (Pine memakai akumulasi inkremental).
    for name in ("vidya", "atr", "upper", "lower", "cmo", "vol_delta_pct", "vol_delta_win_pct"):
        ours = getattr(result, name)
        theirs = arrays[name]
        mask = np.isfinite(ours) & np.isfinite(theirs)
        assert mask.any(), f"tidak ada nilai valid untuk {name}"
        scale = np.maximum(np.abs(theirs[mask]), 1e-9)
        rel = np.max(np.abs(ours[mask] - theirs[mask]) / scale)
        assert rel < 1e-9, f"{name}: galat relatif terlalu besar ({rel:.2e})"


def test_tidak_ada_lookahead(real_candles: CandleSeries) -> None:
    """Hasil untuk bar ke-i tidak boleh berubah karena ada data SETELAHNYA.

    Catatan: awalan (prefix) harus sama-sama mulai dari bar pertama, karena
    VIDYA/RMA bersifat rekursif -- memotong awal riwayat memang mengubah nilai
    (itu masalah warm-up, diuji terpisah di test_warmup.py).
    """
    params = IndicatorParams()
    short = compute(real_candles.head(1200), params)
    long = compute(real_candles.head(1800), params)
    length = len(short)

    for name in (
        "cmo",
        "vidya",
        "atr",
        "upper",
        "lower",
        "trail",
        "vol_delta_pct",
        "vol_delta_win_pct",
        "zone_count",
    ):
        np.testing.assert_array_equal(
            getattr(short, name),
            getattr(long, name)[:length],
            err_msg=f"{name} berubah saat data ditambah -> ada lookahead",
        )
    np.testing.assert_array_equal(short.trend, long.trend[:length])
    np.testing.assert_array_equal(short.cross_up, long.cross_up[:length])
    np.testing.assert_array_equal(short.cross_down, long.cross_down[:length])


def test_deterministik(sample_candles: CandleSeries) -> None:
    a = compute(sample_candles)
    b = compute(sample_candles)
    np.testing.assert_array_equal(a.vidya, b.vidya)
    np.testing.assert_array_equal(a.trail, b.trail)


def test_band_dan_trail_konsisten(sample_candles: CandleSeries) -> None:
    result = compute(sample_candles)
    mask = np.isfinite(result.vidya) & np.isfinite(result.atr)
    assert mask.any()
    np.testing.assert_allclose(
        result.upper[mask], result.vidya[mask] + result.atr[mask] * 2.0, rtol=1e-12
    )
    np.testing.assert_allclose(
        result.lower[mask], result.vidya[mask] - result.atr[mask] * 2.0, rtol=1e-12
    )
    # Garis trail = lower band saat tren naik, upper band saat tren turun.
    up = (result.trend == 1) & np.isfinite(result.trail)
    down = (result.trend == -1) & np.isfinite(result.trail)
    np.testing.assert_allclose(result.trail[up], result.lower[up], rtol=1e-12)
    np.testing.assert_allclose(result.trail[down], result.upper[down], rtol=1e-12)
    # Na tepat pada bar pergantian tren, dan juga selama warm-up (tren masih flat).
    flip = result.cross_up | result.cross_down
    assert np.isnan(result.trail[flip]).all()
    after_warmup = np.isfinite(result.vidya) & np.isfinite(result.atr)
    assert not np.isnan(result.trail[after_warmup & ~flip]).any()


def test_hanya_crossover_upper_atau_crossunder_lower_yang_mengubah_tren(
    sample_candles: CandleSeries,
) -> None:
    result = compute(sample_candles)
    close = sample_candles.close
    changes = np.flatnonzero(np.diff(result.trend) != 0)
    assert changes.size > 0, "data uji harus punya perubahan tren"
    for idx in changes:
        i = int(idx) + 1
        if result.trend[i] == TrendState.UP:
            assert close[i] > result.upper[i]
        else:
            assert close[i] < result.lower[i]


def test_pembagian_nol_cmo(synthetic_candles: CandleSeries) -> None:
    """Harga datar total -> sumPos + sumNeg = 0, CMO harus 0 (bukan nan/inf)."""
    params = IndicatorParams(atr_length=2, sma_smooth=1, vidya_momentum=3)
    flat = CandleSeries.from_columns(
        synthetic_candles.open_time,
        [100.0] * 8,
        [100.0] * 8,
        [100.0] * 8,
        [100.0] * 8,
        [5.0] * 8,
    )
    result = compute(flat, params)
    valid = np.isfinite(result.cmo)
    assert valid.any()
    assert np.all(result.cmo[valid] == 0.0)
    assert np.isfinite(result.vidya[valid]).all()
    # Catatan: dengan harga benar-benar datar, ATR = 0 sehingga band = garis VIDYA.
    # Karena VIDYA rekursif naik dari 0 menuju harga, harga bisa "menembus" upper
    # band sekali di awal. Yang penting: TIDAK ada sinyal entry palsu.
    assert not result.cross_up.any()
    assert not result.cross_down.any()
    assert result.trend_state in (TrendState.UP, TrendState.FLAT)


def test_data_terlalu_pendek_tidak_crash() -> None:
    candles = CandleSeries.from_columns([1_000], [1.0], [1.0], [1.0], [1.0], [1.0])
    result = compute(candles)
    assert len(result) == 1
    assert np.isnan(result.vidya[0])
    assert result.trend[0] == 0
    assert result.zone_count[0] == 0


def test_candles_kosong_ditolak() -> None:
    empty = CandleSeries.from_columns([], [], [], [], [], [])
    with pytest.raises(ValueError, match="kosong"):
        compute(empty)


def test_volume_delta_dihitung_ulang_setiap_pergantian_tren(
    sample_candles: CandleSeries,
) -> None:
    result = compute(sample_candles)
    flips = np.flatnonzero(result.cross_up | result.cross_down)
    assert flips.size >= 3
    first = int(flips[1])
    opens = sample_candles.open
    closes = sample_candles.close
    vols = sample_candles.volume
    buy = 0.0
    sell = 0.0
    for i in range(int(flips[0]), int(flips[1])):
        if closes[i] > opens[i]:
            buy += vols[i]
        elif closes[i] < opens[i]:
            sell += vols[i]
    expected = (buy - sell) / ((buy + sell) / 2) * 100 if buy + sell > 0 else 0.0
    assert result.vol_delta_pct[first - 1] == pytest.approx(expected, rel=1e-12)


def test_zona_likuiditas_sesuai_definisi(sample_candles: CandleSeries) -> None:
    """Uji properti: setiap zona harus memenuhi definisi spesifikasi."""
    params = IndicatorParams()
    result = compute(sample_candles, params)
    close = sample_candles.close
    high = sample_candles.high
    assert len(result.zones) > 0, "data uji seharusnya menghasilkan zona"

    for zone in result.zones:
        pivot_src = close if zone.kind == "low" else high
        pivot_i = zone.pivot_index
        neighbours = list(range(pivot_i - params.pivot_left, pivot_i)) + list(
            range(pivot_i + 1, pivot_i + 1 + params.pivot_right)
        )
        for j in neighbours:
            if not 0 <= j < len(sample_candles):
                continue
            if zone.kind == "low":
                assert pivot_src[j] > zone.price
            else:
                assert pivot_src[j] < zone.price
        # Konfirmasi terjadi pivot_right bar setelah pivot.
        assert zone.confirm_index == pivot_i + params.pivot_right
        # Saat konfirmasi: tren sesuai dan pivot berada di sisi yang benar dari trail.
        assert result.trend[zone.confirm_index] == (1 if zone.kind == "low" else -1)
        trail_at_confirm = result.trail[zone.confirm_index]
        assert np.isfinite(trail_at_confirm)
        if zone.kind == "low":
            assert zone.price > trail_at_confirm
        else:
            assert zone.price < trail_at_confirm
        # Jika tersentuh: garis trail benar-benar memotong, dan tidak di masa lalu.
        if zone.touched_index is not None:
            assert zone.touched_index >= zone.confirm_index
            trail_at_touch = result.trail[zone.touched_index]
            if zone.kind == "low":
                assert trail_at_touch >= zone.price
            else:
                assert trail_at_touch <= zone.price


def test_jumlah_zona_aktif_konsisten_dengan_daftar_zona(sample_candles: CandleSeries) -> None:
    params = IndicatorParams()
    result = compute(sample_candles, params)
    expected = np.zeros(len(result), dtype=np.float64)
    for zone in result.zones:
        last = zone.confirm_index + params.zone_lookback_bars
        if zone.touched_index is not None:
            last = min(last, zone.touched_index - 1)
        for i in range(zone.confirm_index, min(last, len(expected) - 1) + 1):
            expected[i] += 1
    np.testing.assert_array_equal(result.zone_count, expected)


def test_sinyal_entry_mengikuti_pergantian_tren(sample_candles: CandleSeries) -> None:
    result = compute(sample_candles, IndicatorParams(volume_filter=False))
    longs = [entry_signal(result, i) for i in range(len(result))]
    assert longs.count("long") == int(result.cross_up.sum())
    assert longs.count("short") == int(result.cross_down.sum())
    assert longs.count(None) == len(result) - int(result.cross_up.sum() + result.cross_down.sum())


def test_filter_volume_delta_bisa_menolak_sinyal(sample_candles: CandleSeries) -> None:
    strict = IndicatorParams(
        volume_filter=True, volume_filter_source="rolling", volume_delta_min_pct=40.0
    )
    result = compute(sample_candles, strict)
    accepted = [entry_signal(result, i) for i in range(len(result))]
    raw_flips = int(result.cross_up.sum() + result.cross_down.sum())
    assert raw_flips > 0
    assert accepted.count(None) > 0
    assert accepted.count("long") + accepted.count("short") < raw_flips
    # Filter harus sadar arah: long butuh delta >= +ambang, short butuh <= -ambang.
    for i, direction in enumerate(accepted):
        if direction == "long":
            assert result.vol_delta_win_pct[i] >= 40.0
        elif direction == "short":
            assert result.vol_delta_win_pct[i] <= -40.0


def test_delta_since_flip_degenerat_pada_bar_entry(sample_candles: CandleSeries) -> None:
    """Temuan: pada bar pergantian tren, delta since-flip selalu +-200 atau 0.

    Ini alasan filter default memakai delta jendela bergulir. Test ini mengunci
    perilaku tersebut agar tidak "diam-diam" berubah.
    """
    result = compute(sample_candles)
    flips = np.flatnonzero(result.cross_up | result.cross_down)
    values = result.vol_delta_pct[flips]
    assert np.all(np.isfinite(values))
    assert np.all(np.isclose(np.abs(values), 200.0) | np.isclose(values, 0.0))


def test_filter_volume_delta_default_tidak_membuang_short(sample_candles: CandleSeries) -> None:
    """Regresi: filter default tidak boleh hanya meloloskan sinyal long."""
    result = compute(sample_candles, IndicatorParams(volume_filter=True, volume_delta_min_pct=0.0))
    directions = [entry_signal(result, i) for i in range(len(result))]
    assert directions.count("long") > 0
    assert directions.count("short") > 0


@pytest.mark.parametrize(
    ("kwargs", "pesan"),
    [
        ({"vidya_length": 0}, "vidya_length"),
        ({"vidya_momentum": -1}, "vidya_momentum"),
        ({"band_distance": 0.0}, "band_distance"),
        ({"atr_length": 5000}, "atr_length"),
        ({"pivot_left": -2}, "pivot_left"),
        ({"source": "vwap"}, "source"),
        ({"pivot_low_source": "open"}, "pivot_low_source"),
        ({"volume_delta_min_pct": 101}, "volume_delta_min_pct"),
    ],
)
def test_validasi_parameter(kwargs: dict[str, object], pesan: str) -> None:
    with pytest.raises(ValueError, match=pesan):
        IndicatorParams(**kwargs)  # type: ignore[arg-type]


def test_warmup_bars_mengikuti_komponen() -> None:
    params = IndicatorParams(atr_length=200, sma_smooth=15, vidya_momentum=20)
    assert params.warmup_bars == 235
