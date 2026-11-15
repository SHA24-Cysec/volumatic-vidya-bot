"""Primitif hitungan numerik yang meniru semantik fungsi bawaan Pine Script.

Semua fungsi di sini bekerja pada ``numpy.ndarray`` 1 dimensi dan memakai
``NaN`` sebagai pengganti ``na`` di Pine. Aturan penting:

* Nilai ``na`` di Pine muncul saat data belum cukup (bar warm-up).
* ``ta.sma`` mengembalikan ``na`` bila jendela belum penuh berisi nilai valid.
* ``ta.rma`` (dipakai ``ta.atr``) selalu mengembalikan nilai terakhir yang valid
  walau sumbernya ``na``, dan di-seed dengan SMA dari ``length`` nilai valid pertama.

Petunjuk: modul ini sengaja tidak bergantung pada pandas agar hasilnya
deterministik dan cepat untuk ratusan ribu bar.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]

__all__ = [
    "as_float_array",
    "rma",
    "rolling_mean_full",
    "rolling_sum",
    "true_range",
    "wma_none",
]


def as_float_array(values: ArrayLike, *, name: str = "values") -> FloatArray:
    """Ubah input menjadi array float 1 dimensi, dengan pesan error yang jelas."""
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"{name} harus 1 dimensi, diterima {arr.ndim} dimensi")
    return arr


def _check_window(window: int, *, name: str = "window") -> None:
    if not isinstance(window, int) or window < 1:
        raise ValueError(f"{name} harus bilangan bulat >= 1, diterima {window!r}")


def rolling_sum(values: ArrayLike, window: int) -> FloatArray:
    """Jumlah bergulir ``window`` nilai; ``NaN`` bila data belum cukup.

    Dihitung per jendela (bukan akumulasi kurangi-tambah) supaya galat
    pembulatan tidak menumpuk pada data panjang.
    """
    arr = as_float_array(values)
    _check_window(window)
    n = arr.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window:
        return out
    # Jendela yang mengandung NaN dianggap tidak valid -- sama seperti Pine.
    if not np.all(np.isfinite(arr)):
        return out
    windows = np.lib.stride_tricks.sliding_window_view(arr, window)
    out[window - 1 :] = windows.sum(axis=-1)
    return out


def rolling_mean_full(values: ArrayLike, window: int) -> FloatArray:
    """SMA yang mensyaratkan seluruh jendela berisi nilai valid (mirip ``ta.sma``)."""
    arr = as_float_array(values)
    _check_window(window)
    n = arr.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window:
        return out
    finite = np.isfinite(arr)
    safe = np.where(finite, arr, 0.0)
    sums = np.lib.stride_tricks.sliding_window_view(safe, window).sum(axis=-1)
    counts = np.lib.stride_tricks.sliding_window_view(finite, window).sum(axis=-1)
    valid = counts == window
    tail = np.full(n - window + 1, np.nan, dtype=np.float64)
    tail[valid] = sums[valid] / window
    out[window - 1 :] = tail
    return out


def rma(values: ArrayLike, length: int) -> FloatArray:
    """Rata-rata bergerak Wilder (``ta.rma``).

    Rekurens: ``rma[t] = (x[t] + (length - 1) * rma[t-1]) / length``.
    Nilai awal (seed) adalah SMA dari ``length`` nilai valid pertama, sama
    seperti implementasi Pine.
    """
    arr = as_float_array(values)
    _check_window(length, name="length")
    n = arr.size
    out = np.full(n, np.nan, dtype=np.float64)
    seed_buffer: list[float] = []
    prev = np.nan
    alpha = 1.0 / length
    for i in range(n):
        value = arr[i]
        if not np.isfinite(value):
            # Pine mempertahankan nilai rma terakhir saat sumber na.
            out[i] = prev
            continue
        if np.isnan(prev):
            seed_buffer.append(float(value))
            if len(seed_buffer) >= length:
                prev = float(np.mean(seed_buffer[:length]))
                out[i] = prev
            else:
                out[i] = np.nan
            continue
        prev = value * alpha + prev * (1.0 - alpha)
        out[i] = prev
    return out


def true_range(high: ArrayLike, low: ArrayLike, close: ArrayLike) -> FloatArray:
    """True Range, identik dengan ``ta.tr`` (tanpa ``handle_na``).

    Bar pertama tidak punya bar sebelumnya sehingga nilainya ``NaN``.
    """
    h = as_float_array(high, name="high")
    lo = as_float_array(low, name="low")
    c = as_float_array(close, name="close")
    if not (h.size == lo.size == c.size):
        raise ValueError("panjang high, low, close harus sama")
    n = h.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < 2:
        return out
    prev_close = c[:-1]
    cur_max = np.maximum(h[1:], prev_close)
    cur_min = np.minimum(lo[1:], prev_close)
    out[1:] = cur_max - cur_min
    return out


def wma_none(values: ArrayLike, length: int) -> FloatArray:
    """Placeholder eksplisit: indikator ini tidak memakai WMA.

    Fungsi disediakan agar pemanggil tidak "menebak-nebak" bahwa WMA setara RMA.
    """
    raise NotImplementedError("Volumatic VIDYA tidak memakai WMA; gunakan rma().")
