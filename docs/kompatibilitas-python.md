# Kompatibilitas Library dengan Python 3.11–3.14

Dokumen ini mencatat bukti pemeriksaan **pada 14 November 2026** terhadap PyPI
(`https://pypi.org/pypi/<paket>/json`) dan halaman *What's New* resmi Python.
Semua angka versi di bawah adalah yang benar-benar terpasang dan diuji, bukan
perkiraan.

## 1. Ringkasan hasil

| Paket | Versi dipakai | `requires-python` | Wheel cp311 | cp312 | cp313 | cp314 | Catatan |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| numpy | **2.4.6** | ≥3.11 | ✅ | ✅ | ✅ | ✅ | 2.5.x **membuang Python 3.11** (butuh ≥3.12) → dibatasi `<2.5` |
| pandas | **3.1.1** | ≥3.11 | ✅ | ✅ | ✅ | ✅ | resolusi datetime jadi **mikrodetik** (lihat §3) |
| pydantic | **2.14.3** | ≥3.10 | py3 (pure) | ✅ | ✅ | ✅ | pydantic-core 2.50.4 punya wheel cp310–cp315 |
| pydantic-settings | **2.16.3** | ≥3.11 | py3 | ✅ | ✅ | ✅ | |
| python-dotenv | **1.2.4** | ≥3.10 | py3 | ✅ | ✅ | ✅ | |
| PyYAML | **6.0.3** | — | ✅ | ✅ | ✅ | ✅ | rilis ini menambahkan wheel cp314 |
| aiohttp | **3.14.5** | ≥3.10 | ✅ | ✅ | ✅ | ✅ | |
| websockets | **17.4** | ≥3.11 | ✅ | ✅ | ✅ | ✅ | |
| rich | **15.0.0** | ≥3.8 | py3 | ✅ | ✅ | ✅ | |
| pytest | **9.2.0** | — | py3 | ✅ | ✅ | ✅ | |
| pytest-asyncio | **1.5.0** | — | py3 | ✅ | ✅ | ✅ | |
| ruff | **0.17.5** | ≥3.7 | wheel biner (abi3) | ✅ | ✅ | ✅ | |
| pip-audit | **2.10.1** | ≥3.10 | py3 | ✅ | ✅ | ✅ | |

**Kesimpulan: tidak ada library inti yang belum mendukung Python 3.14.**
Semua terpasang sebagai *wheel* (tanpa kompilasi dari source) di Python 3.14.10.

### Library yang sengaja TIDAK dipakai

| Paket | Status 3.14 | Alasan tidak dipakai |
|---|---|---|
| `pyarrow` (parquet) | Tersedia (26.0.1, cp311–cp315) | Cache memakai **SQLite + `.npz`** (sudah ada di stack). Instalasi jadi ±120 MB lebih ringan dan CI 4 versi Python selesai jauh lebih cepat. Bisa ditambahkan kapan saja bila Anda ingin cache parquet. |
| `aiosqlite` | Tersedia | Tidak perlu; `sqlite3` standar dijalankan lewat `asyncio.to_thread` (tanpa dependensi tambahan, tanpa blocking event loop). |
| SDK resmi `binance-connector`/`binance-sdk-derivatives-trading-usds-futures` | Belum dinilai | Klien REST/WS akan ditulis sendiri di TAHAP 2 supaya kontrol atas header berat (`X-MBX-USED-WEIGHT`), backoff, dan kompresi lebih transparan. Akan dievaluasi ulang di TAHAP 2. |

### Catatan versi Python Anda

Anda menyebut **Python 3.14.8**. Sandbox pengembangan memakai **Python 3.14.10**
(rilis patch terbaru di seri 3.14 saat dokumen ini ditulis). Kode tidak memakai
API yang berubah antar patch 3.14.x, jadi 3.14.8 seharusnya identik; tetap
diverifikasi ulang di mesin Anda (lihat §5).

## 2. Perubahan Python 3.13 & 3.14 yang berdampak pada kode ini

Diperiksa dari `docs.python.org/3/whatsnew/3.13.html` dan `3.14.html`
(diakses 14 Nov 2026).

| Perubahan | Versi | Dampak | Tindakan di kode |
|---|---|---|---|
| `asyncio.get_event_loop()` **melempar `RuntimeError`** bila tidak ada loop berjalan (tidak lagi membuat loop implisit) | 3.14 | Kode lama `loop = get_event_loop(); run_until_complete(...)` akan crash | Tidak dipakai sama sekali; titik masuk async memakai `asyncio.run()` |
| Sistem *event loop policy* (`get_event_loop_policy`, `set_event_loop_policy`, `AbstractEventLoopPolicy`, `WindowsProactorEventLoopPolicy`) **deprecated**, dihapus di 3.16 | 3.14 | Pola lama untuk memilih loop di Windows akan berhenti bekerja | Tidak dipakai; bila nanti butuh loop khusus → `asyncio.run(..., loop_factory=...)` |
| `asyncio.iscoroutinefunction()` deprecated → `inspect.iscoroutinefunction()` | 3.14 | Peringatan saat import | Tidak dipakai |
| **PEP 649/749**: anotasi tipe dievaluasi *malas* (deferred) | 3.14 | `__annotations__` berisi string/objek `ForwardRef` sampai diakses; kode yang memeriksa anotasi saat runtime bisa berubah perilaku | Tidak ada kode yang membaca `__annotations__` saat runtime; semua modul memakai `from __future__ import annotations` supaya perilaku **seragam** di 3.11–3.14 |
| `sqlite3.version` / `version_info` **dihapus** | 3.14 | Akses versi modul crash | Akan memakai `sqlite3.sqlite_version` (TAHAP 2) |
| `itertools` copy/deepcopy/pickle dihapus; `urllib.parse.Quoter`, `urllib.request.URLopener` dihapus; `pkgutil.get_loader` dihapus | 3.14 | Library lama bisa gagal | Tidak dipakai |
| `typing.io` / `typing.re` dihapus; `typing.AnyStr` deprecated | 3.13 | Import gagal | Tidak dipakai |
| `get_event_loop()` pada policy default memunculkan `DeprecationWarning` | 3.13 | Peringatan | Tidak dipakai |
| Penghapusan modul `distutils`, `imp`, dll. | 3.12/3.13 | Build dependency lama | Build memakai `hatchling` |

Kode tidak memakai fitur khusus 3.14 (mis. template string `t""`, `compression.zstd`),
sehingga **tidak perlu jalur alternatif** untuk 3.11–3.13. Rentang yang dipakai
hanya fitur 3.10+ (`match` tidak dipakai, `X | Y` union, `zip(strict=)`,
`slots=True` pada dataclass) yang aman di seluruh rentang.

## 3. Temuan nyata saat pengujian: pandas 3 mengubah resolusi waktu

Test `tests/test_loaders.py` sempat gagal karena di **pandas 3.x**, `dtype` waktu
default adalah **mikrodetik** (`datetime64[us, UTC]`), bukan nanodetik seperti
pandas 2.x. Akibatnya `astype("int64")` menghasilkan angka 1000× lebih kecil.
Perbaikannya: konversi eksplisit `Series.dt.as_unit("ns").astype("int64") // 10**6`.

Ini contoh persis jenis regresi "kode jalan di 3.11 tapi salah nilai di 3.14"
yang diminta untuk diperiksa. Sekarang tercakup test di keempat versi.

## 4. Hasil eksekusi test per versi

Tanggal 14 November 2026, `pytest -q`, 75 test:

| Python | Versi interpreter | Hasil | Waktu |
|---|---|:--:|---:|
| 3.11 | 3.11.18 | **75 passed** | 3.14 s |
| 3.12 | 3.12.16 | **75 passed** | 3.04 s |
| 3.13 | 3.13.18 | **75 passed** | 3.21 s |
| 3.14 | **3.14.10** | **75 passed** | 2.84 s |

`ruff check` + `ruff format --check`: bersih. `pip-audit -r requirements.lock`
(56 paket): **tidak ada kerentanan yang diketahui**.

## 5. Yang perlu Anda verifikasi di laptop (Python 3.14.8)

```bash
uv python install 3.14.8          # atau pakai pyenv
uv sync --python 3.14.8
uv run pytest -q
uv run python -V                  # harus 3.14.8
```

Kalau ada test yang gagal **hanya** di 3.14.8, kirimkan outputnya — itu
informasi penting dan akan saya perbaiki di tahap berikutnya.

## 6. Rekomendasi Docker

Untuk deploy ke server, pakai **`python:3.13-slim`** (bukan 3.14) karena:

- ekosistem library paling matang di 3.13 (semua wheel tersedia dan sudah teruji
  lebih lama),
- proses build/CI lebih cepat karena cache layer lebih umum,
- 3.14 sudah didukung, jadi ini murni soal kehati-hatian operasional, bukan
  keterbatasan teknis.

Proyek ini tetap **diuji di 3.14** agar Anda bisa mengembangkan di laptop dengan
versi tersebut. Dockerfile disiapkan pada TAHAP 7.
