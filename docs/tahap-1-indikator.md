# TAHAP 1 — Struktur proyek, konfigurasi, port indikator, dan pengujian

**Status: selesai (kecuali verifikasi terhadap ekspor TradingView milik Anda).**
Tanggal: 14 November 2026. Python pengujian: 3.11.18 / 3.12.16 / 3.13.18 /
**3.14.10** (minta 3.14.8 — lihat [`kompatibilitas-python.md`](kompatibilitas-python.md) §5).

---

## 1. Apa yang sudah jadi

| Bagian | File | Keterangan |
|---|---|---|
| Struktur proyek | `pyproject.toml`, `uv.lock`, `requirements.lock` | `requires-python = ">=3.11"`, 56 paket terkunci |
| Konfigurasi | `config.yaml`, `.env.example`, `src/volumatic_bot/config.py` | pydantic v2, `extra="forbid"`, batas aman |
| Port indikator | `src/volumatic_bot/indicator/{core,params,numerics,reference}.py` | CMO, VIDYA, ATR/RMA, band, tren, trail, volume delta, zona |
| Studi warm-up | `src/volumatic_bot/indicator/warmup.py`, `docs/studi-warmup.md` | empiris, bisa diulang sendiri |
| Pembanding TradingView | `src/volumatic_bot/indicator/tv_compare.py`, CLI `tv-compare` | laporan selisih per kolom |
| Pembaca data | `src/volumatic_bot/data/loaders.py`, `models.py` | CSV/CSV.GZ Binance & TradingView |
| Keamanan log | `src/volumatic_bot/logging_setup.py` | redaksi API key/secret/listenKey/signature |
| CLI | `src/volumatic_bot/cli.py` | `version`, `check-config`, `indicator`, `warmup-study`, `tv-compare` |
| Test | `tests/` (75 test) + data nyata `tests/data/btcusdt_15m_5bulan.csv.gz` | 14.688 candle BTCUSDT 15m (data.binance.vision) |
| CI | `.github/workflows/ci.yml` | matrix pytest 3.11–3.14, ruff, pip-audit, cek secret |

Data uji adalah **kline asli**, bukan data sintetis: BTCUSDT perpetual 15m,
1 Juli – 30 November 2025, 14.688 bar (286 KB terkompresi).
Skrip pengunduh ulang: `scripts/unduh_data_uji.py`.

---

## 2. Pemetaan spesifikasi indikator → implementasi

| Spesifikasi | Implementasi | Catatan |
|---|---|---|
| (a) Momentum & jendela | `numerics.rolling_sum` + `_chande_momentum_oscillator` | `NaN` = `na` Pine |
| (b) CMO absolut, tangani bagi nol | `abs(100*(sumPos-sumNeg)/(sumPos+sumNeg))`, `0.0` bila penyebut 0 | `sumNeg` = **magnitudo** momentum negatif (rumus CMO standar); kalau dipakai nilai bertanda, rumusnya rusak |
| (c) alpha & VIDYA rekursif + SMA(15) | `_vidya_recursive` (nilai awal 0) → `rolling_mean_full(…, 15)` | Band memakai **VIDYA yang sudah dihaluskan** |
| (d) ATR(200) dengan RMA | `numerics.rma(true_range(...), 200)`, seed = SMA 200 nilai valid pertama | Setara `ta.atr` |
| (e) upper/lower band | `vidya ± atr*band_distance` | diuji `rtol=1e-12` |
| (f) status tren | `_trend_and_crosses` (state machine) | naik saat close > upper, turun saat close < lower, selain itu tetap |
| (g) garis trail + `na` saat ganti tren | `_trail_line` | diuji: `na` **tepat** pada bar flip |
| (h) sinyal entry | `cross_up` / `cross_down` + `entry_signal()` | lihat §3.2 |
| (i) volume delta tren | `_volume_delta` | lihat §3.3 (ada temuan) |
| (j) zona likuiditas | `_build_zones` + `LiquidityZone` | lihat §3.4 |
| (k) lisensi | README + header modul | CC BY-NC-SA 4.0, karya turunan non-komersial & SA |
| (l) unit test vs TradingView | `tv_compare.py` + CLI | **belum dijalankan dengan data TV asli** (§8) |
| (m) hanya candle closed, tanpa repainting | `CandleSeries` + uji anti-lookahead | data dari WebSocket hanya dipakai bila `x=true` (TAHAP 2) |
| (n) warm-up & konvergensinya | `warmup.py`, `docs/studi-warmup.md` | hasil di §4 |

---

## 3. Keputusan yang harus Anda ketahui (dan konfirmasi)

### 3.1 Sumber harga pivot (sesuai permintaan: port apa adanya + flag)
Default: **pivot low memakai `close`**, **pivot high memakai `high`** — meniru
skrip asli. Dua flag di `config.yaml`:
`indicator.pivot_low_source` (`close`/`low`) dan `indicator.pivot_high_source`
(`high`/`close`) untuk menyeragamkan. Perbedaan ini berpengaruh pada **deteksi
zona** saja, bukan pada sinyal entry.

### 3.2 Perubahan tren pertama **tidak** menghasilkan sinyal
Pine: `ta.change(trend)` bernilai `na` ketika `trend[1]` masih `na` (baru mulai),
sehingga sinyal tidak muncul. Implementasi meniru itu: sinyal hanya keluar bila
tren sebelumnya sudah pasti (±1). Konsekuensi praktis: setelah warm-up selesai,
**tidak ada** sinyal sampai tren benar-benar berpindah sisi band. Menurut saya
ini perilaku yang benar (menghindari entry "mengejar" harga yang sudah jauh di
luar band), tapi kalau Anda ingin sinyal di bar pertama saat tren pertama
terbentuk, cukup ubah satu kondisi — beri tahu saya.

### 3.3 **Temuan penting**: delta volume "sejak tren berganti" tidak bisa dipakai
sebagai filter pada bar entry

Sesuai spesifikasi, delta% diakumulasi **sejak tren berganti**, dan sinyal entry
terjadi **tepat pada bar pergantian tren**. Pada bar itu baru ada **satu** candle
yang terakumulasi, sehingga:

```
delta% = (buy - sell) / ((buy + sell)/2) * 100  →  +200, −200, atau 0 (doji)
```

Tidak ada nilai lain. Artinya filter dengan ambang apa pun di antara −200..200
praktis hanya memeriksa "arah candle terakhir", bukan konfirmasi volume.

**Yang saya lakukan (bisa Anda ubah):**
- `vol_delta_pct` (since-flip) **tetap dihitung** dan ditampilkan persis sesuai
  spesifikasi — berguna untuk konteks/dashboard.
- Untuk filter entry, ditambahkan `vol_delta_win_pct` = delta% **jendela
  bergulir** `volume_delta_window` (default 20 bar), dan config
  `indicator.volume_filter_source: "rolling"` (default) atau `"since_flip"`.
- Filter **sadar arah**: long butuh delta ≥ +ambang, short butuh delta ≤ −ambang.
  Tanpa ini, filter default 0 akan membuang **semua** sinyal short.

Bukti: `tests/test_indicator_core.py::test_delta_since_flip_degenerat_pada_bar_entry`.

### 3.4 Interpretasi zona likuiditas
Spesifikasi: "pivot low yang berada **di atas** garis trail saat tren naik, dan
pivot high **di bawah** garis trail saat tren turun"; zona "tersentuh bila garis
trail memotongnya dalam 50 bar". Saya implementasikan apa adanya:

- tren naik: zona low dibuat saat `pivot_low > trail`; tersentuh saat `trail >= pivot_low`
  (trail naik lalu memotong dari bawah),
- tren turun: zona high dibuat saat `pivot_high < trail`; tersentuh saat `trail <= pivot_high`,
- zona konfirmasi = `pivot_index + pivot_right` (pivot butuh `right` bar untuk pasti),
- zona dibuang setelah `zone_lookback_bars` (50) bar tanpa sentuhan.

Diuji sebagai properti atas data nyata (bukan angka hardcode) di
`test_zona_likuiditas_sesuai_definisi` dan `test_jumlah_zona_aktif_konsisten…`.

### 3.5 Zona tidak dipakai sebagai filter entry (belum)
Spesifikasi menyebut zona sebagai "konteks opsional". Saat ini zona dihitung,
dihitung per bar (`zone_count`), dan dicatat, tetapi **belum** memblokir entry.
Kalau Anda ingin, TAHAP 3 bisa memberi skor tambahan saat entry dekat zona
likuiditas.

---

## 4. Studi warm-up: berapa candle minimum? (jawaban empiris)

Perintah: `volumatic-bot warmup-study --candles tests/data/btcusdt_15m_5bulan.csv.gz`
Data: 14.688 bar. Metode: hitung indikator dengan seluruh data (dianggap
"kebenaran"), lalu hitung ulang hanya dengan `W` bar terakhir dan bandingkan
200 bar terakhir di **6 titik akhir berbeda** (supaya tidak bergantung pada satu
segmen pasar). Tabel lengkap: [`studi-warmup.md`](studi-warmup.md).

| Warm-up | ATR (galat rel.) | VIDYA (galat rel.) | Band (galat rel.) | Beda tren | Beda zona | Konvergen |
|---:|---:|---:|---:|---:|---:|:--:|
| 250 | 23.1% | 58.1% | 1.09% | 192 bar | 190 bar | ✗ |
| 300 | 24.3% | 16.7% | 1.08% | 152 bar | 166 bar | ✗ |
| 400 | 11.7% | 0.45% | 0.48% | 38 bar | 83 bar | ✗ |
| 500 | 9.0% | 0.0019% | 0.033% | **0** | 17 bar | ✗ |
| 750 | 2.8% | 6e-7% | 0.020% | 0 | 6 bar | ✗ |
| 1000 | 1.3% | 8e-11% | 0.0089% | 0 | 1 bar | ✗ |
| 1500 | 0.08% | ~0 | 0.00055% | 0 | 0 | ✗ (ATR belum <0.01%) |
| 2000 | 0.0064% | ~0 | 0.000026% | 0 | 0 | **✓** |

**Kesimpulan jujur:**

1. Angka "300–500 candle" di spesifikasi **cukup untuk VIDYA** (VIDYA konvergen
   sangat cepat karena `alpha*CMO/100` membuat bobot riwayat lama meluruh), tetapi
   **tidak cukup untuk ATR(200)**: RMA Wilder punya memori panjang
   (bobot bar ke-`n` yang lalu ≈ `(1−1/200)^n`, jadi butuh ribuan bar untuk
   benar-benar "lupa" kondisi awal).
2. Pada 400 bar, **tren bisa berbeda 38 bar** dari hasil data panjang. Itu bukan
   galat kecil — itu bisa berarti entry yang tidak ada di TradingView.
3. **Default config sekarang `scanner.warmup_bars: 1500`** (ATR <0.1%, tren
   identik di semua titik sampel, biaya komputasi 23 ms/simbol → ±0.7 s untuk 30
   simbol). `2000` bila Anda ingin benar-benar konvergen (<0.01%).
4. **Syarat mencocokkan TradingView secara persis:** jumlah bar yang dimuat di
   chart TradingView harus **sama** dengan `warmup_bars` kita. Kalau TV memuat
   5.000 bar dan bot memuat 1.500, ATR akan berbeda ~0.1% dan sesekali menggeser
   entry dekat band. Ini batasan matematis, bukan bug.

Biaya komputasi terukur (Python 3.14, 1 core):

| Warm-up | per simbol | 30 simbol |
|---:|---:|---:|
| 400 | 6.2 ms | 0.18 s |
| 500 | 7.2 ms | 0.22 s |
| 1500 | 23 ms | 0.70 s |
| 2000 | 31 ms | 0.92 s |

---

## 5. Hasil pengujian

```
$ pytest -q            # Python 3.11.18 / 3.12.16 / 3.13.18 / 3.14.10
75 passed in 2.7–3.6 s
$ ruff check src tests && ruff format --check src tests
All checks passed!  (29 file)
$ mypy src            # mode strict
Success: no issues found in 22 source files
$ pip-audit -r requirements.lock
No known vulnerabilities found
```

Test penting:

| Test | Yang dijamin |
|---|---|
| `test_hasil_identik_dengan_implementasi_referensi` | implementasi vektor == implementasi referensi murni-Python (loop, akumulasi inkremental seperti Pine); tren & pola `na` sama persis, nilai relatif < 1e-9 |
| `test_tidak_ada_lookahead` | menambah data SETELAH bar tidak mengubah nilai bar sebelumnya (anti-repainting) |
| `test_band_dan_trail_konsisten` | trail = lower/upper band, `na` tepat di bar flip |
| `test_hanya_crossover_upper_atau_crossunder_lower_yang_mengubah_tren` | tren hanya berubah lewat penembusan band |
| `test_zona_likuiditas_sesuai_definisi` | setiap zona memenuhi definisi + waktu sentuhan |
| `test_delta_since_flip_degenerat_pada_bar_entry` | mengunci temuan §3.3 |
| `test_mode_live_butuh_dua_pengaman` | mode live butuh 2 flag eksplisit |
| `test_log_tidak_membocorkan_secret`, `test_sanitize_pola_binance` | API key/secret/signature/listenKey tidak bocor ke log |
| `test_nilai_berbahaya_ditolak` (12 kasus) | leverage >10, risiko >2%, margin >80%, dll ditolak |
| `test_konvergensi_warmup_pada_data_nyata` (slow) | mengunci angka studi warm-up |

Jalankan cepat tanpa test slow: `pytest -q -m "not slow"`.

---

## 6. Bug & celah yang ditemukan selama tahap ini (dan statusnya)

| # | Temuan | Dampak | Status |
|---|---|---|---|
| 1 | **Delta volume since-flip selalu ±200/0 pada bar entry** → filter volume tidak bermakna, dan filter default 0 membuang semua sinyal short | Tinggi (sinyal hilang) | Diperbaiki: filter sadar arah + opsi delta jendela bergulir (§3.3) |
| 2 | **Arah zona likuiditas** — implementasi pertama saya memakai pivot low *di bawah* trail; spesifikasi menyatakan *di atas* | Sedang (zona salah total) | Diperbaiki + diuji properti |
| 3 | **pandas 3 memakai resolusi waktu mikrodetik** → konversi epoch salah 1000× | Tinggi (data waktu kacau) | Diperbaiki, ada test |
| 4 | `_build_zones` memakai `list.index()` (O(n²) + risiko salah objek) | Sedang (performa/kebenaran) | Diganti indeks eksplisit |
| 5 | `ops.live_confirmation` punya **default** berisi string pengaman → mode live bisa aktif dengan satu flag saja | **Tinggi (uang!)** | Diperbaiki: default kosong, wajib diisi eksplisit |
| 6 | Nilai `nan` pada warm-up dianggap "tidak ada sinyal" tetapi tidak pernah dilaporkan ke pengguna | Rendah | Dicatat; TAHAP 2 akan menandai simbol "belum warm-up" di dashboard |
| 7 | Belum ada penjaga "data candle tidak boleh mengandung bar yang belum tertutup" di level loader | Sedang | Akan ditangani di TAHAP 2 saat data datang dari WebSocket (`x=true`) |

Tidak ditemukan: secret ter-hardcode, secret di log, dependensi rentan
(`pip-audit` bersih), atau akses jaringan di dalam test.

---

## 7. Risiko yang tersisa & yang **belum** diuji (jujur)

**Belum diuji sama sekali:**
- Modul apa pun yang memanggil API Binance (belum ada di tahap ini).
- **Perbandingan dengan ekspor TradingView asli** — perintah `tv-compare` sudah
  ada dan diuji dengan ekspor tiruan, tapi belum dengan data dari chart Anda.
- Perilaku di data simbol selain BTCUSDT (data uji hanya 1 simbol; TAHAP 2 akan
  memakai puluhan simbol dari data.binance.vision).
- Timeframe selain 15m (kode tidak bergantung timeframe, tetapi belum diuji).

**Risiko yang tetap ada (bukan bug, tapi perlu Anda tahu):**
1. Indikator ini **terlambat secara desain** (konfirmasi tren butuh penembusan
   band ± 2×ATR). Banyak whipsaw di pasar sideways.
2. Konvergensi warm-up bergantung jumlah bar yang dimuat di TradingView
   (§4 poin 4). Selisih kecil di ATR bisa menggeser entry di sekitar band.
3. Zona likuiditas memakai pivot yang dikonfirmasi 3 bar kemudian — itu
   informatif, bukan prediktif.
4. Semua angka performa **belum ada**. Tidak ada backtest di tahap ini, dan saya
   tidak akan mengarangnya. Backtest baru di TAHAP 7.

---

## 8. Cara memverifikasi terhadap TradingView (butuh Anda)

Kalau Anda ingin bukti angka, saya butuh **ekspor CSV** dari chart:

1. Buka chart USDT-M Futures, simbol **BTCUSDT**, timeframe **15m**.
2. Pastikan jumlah bar yang dimuat = `scanner.warmup_bars` (default **1500**).
   Cara paling mudah: aktifkan "Bar replay" ke titik tertentu, atau gunakan
   pilihan *"Load more bars"* sampai jumlahnya sesuai.
3. Pasang indikator Volumatic VIDYA, lalu **plot** nilai-nilai ini agar bisa
   diekspor (lewat "Add to chart" pada *Data Window*, atau salin manual):
   `VIDYA`, `Upper`, `Lower`, `Trail`, `ATR`, `Trend`.
4. Ekspor: klik kanan chart → *Export chart data* → CSV.
5. Simpan juga OHLC-nya (ekspor yang sama sudah memuat OHLC).
6. Jalankan:

```bash
volumatic-bot tv-compare --candles tests/data/btcusdt_15m_5bulan.csv.gz \
                         --tv ~/unduhan/BTCUSDT_15m_TV.csv \
                         --out docs/hasil-tv-compare.md
```

Nama kolom dikenali fleksibel (`vidya`/`VIDYA`, `upper`/`Upper Band`,
`trail`/`Trail`, …). Output: jumlah bar yang cocok, selisih maksimum & rata-rata
per kolom, serta status LULUS/GAGAL dengan toleransi 1e-6 relatif. Kalau ada
kolom yang berbeda, kirim laporannya ke saya — saya akan telusuri.

---

## 9. Pertanyaan konfirmasi sebelum TAHAP 2

**Pertanyaan utama:** Apakah TAHAP 1 sudah sesuai dan saya boleh lanjut ke
**TAHAP 2 (koneksi data: REST warm-up, WebSocket kline, cache SQLite, scanner
multi-simbol)**?

Sub-pertanyaan yang memengaruhi tahap berikutnya (jawab seadanya saja):

1. **Warm-up**: pakai `1500` (default, disarankan) atau `2000` (konvergen penuh,
   REST awal sedikit lebih berat)?
2. **Sinyal pada tren pertama**: biarkan tanpa sinyal (default, meniru Pine)
   atau munculkan sinyal?
3. **Filter volume**: pakai delta jendela bergulir 20 bar (default) atau
   since-flip apa adanya (sesuai spesifikasi, tapi praktis hanya cek arah candle)?
4. **Zona likuiditas**: cukup sebagai konteks (default) atau dipakai sebagai
   filter/skor entry di TAHAP 3?
