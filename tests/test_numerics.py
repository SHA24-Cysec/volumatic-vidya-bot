"""Test untuk primitif numerik (padanan fungsi Pine)."""

from __future__ import annotations

import numpy as np
import pytest

from volumatic_bot.indicator.numerics import (
    as_float_array,
    rma,
    rolling_mean_full,
    rolling_sum,
    true_range,
)


def test_as_float_array_menolak_dimensi_salah() -> None:
    with pytest.raises(ValueError, match="1 dimensi"):
        as_float_array([[1.0, 2.0]])


def test_rolling_sum_na_saat_data_kurang() -> None:
    out = rolling_sum([1.0, 2.0, 3.0, 4.0], 3)
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [6.0, 9.0])


def test_rolling_sum_na_bila_jendela_mengandung_nan() -> None:
    out = rolling_sum([1.0, 2.0, np.nan, 4.0], 2)
    assert np.isnan(out).all()


def test_rolling_mean_full_butuh_jendela_penuh() -> None:
    values = [np.nan, 1.0, 2.0, 3.0, 4.0]
    out = rolling_mean_full(values, 3)
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [np.nan, 2.0, 3.0], equal_nan=True)


def test_rma_seed_memakai_sma() -> None:
    values = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
    out = rma(values, 3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == pytest.approx(4.0)  # (2+4+6)/3
    assert out[3] == pytest.approx((8.0 + 2 * 4.0) / 3)  # alpha = 1/3
    assert out[4] == pytest.approx((10.0 + 2 * out[3]) / 3)


def test_rma_mempertahankan_nilai_terakhir_saat_na() -> None:
    out = rma(np.array([1.0, 2.0, np.nan, 4.0]), 2)
    assert out[2] == pytest.approx(out[1])


def test_true_range_bar_pertama_na() -> None:
    out = true_range([10.0, 12.0], [8.0, 9.0], [9.0, 11.0])
    assert np.isnan(out[0])
    # max(12, 9) - min(9, 9) = 3
    assert out[1] == pytest.approx(3.0)


def test_true_range_panjang_tidak_sama() -> None:
    with pytest.raises(ValueError, match="panjang"):
        true_range([1.0, 2.0], [1.0], [1.0, 2.0])
