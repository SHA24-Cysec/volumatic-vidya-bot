# TAHAP 8 — Backtest ulang & filter entry rezim harian

Tanggal: 15 November 2026. Data: arsip resmi `data.binance.vision`
(USDT-M futures, kline 15m Jan 2021 – Okt 2026, funding rate 8 jam).
Semua angka di dokumen ini dihasilkan dari skrip di `scripts/` dan bisa
dijalankan ulang; tidak ada angka yang diketik manual.

## 1. Ringkasan

| Pertanyaan | Jawaban singkat |
|---|---|
| Hasil backtest bawaan? | **Negatif di semua timeframe**. 15m portofolio 8 simbol 2021-2026: **-99.3%**; satu simbol/periode seperti README: -9.4% (leverage 10x). |
| Bisakah dibuat positif? | **Bisa, di 4h** dengan satu filter entry: sinyal hanya diterima bila harga sudah terentang >= 8% melawan arah trade terhadap EMA200 harian. |
| Seberapa besar? | 19 simbol, 4h, maks 2 posisi, risiko 1.5%/trade: **+102.9%** total, CAGR **+13.3%**, maxDD **31.3%**, PF **1.20**, expectancy **+0.14 R/trade**. Pembanding tanpa filter: **-19.6%**. |
| Apakah 15m (bawaan config) jadi positif? | **Tidak.** Filter memperbaiki, tapi 15m tetap negatif karena biaya 14 bps bolak-balik memakan seluruh edge. |
| Apakah filter ini overfit? | Uji yang dilalui: walk-forward (train 2021-2023, test 2024-2026), cross-validation antar-simbol, sensitivitas ambang -5 s/d -15 semuanya positif, tiga subperiode 2 tahun semuanya positif. Batasannya ada di §7. |

## 2. Cara backtest dijalankan

Dua jalur dipakai, keduanya memakai **kode indikator repo apa adanya**:

1. **Mesin repo** (`volumatic-bot backtest`): sizing dari risiko, TP/SL/BE/trailing
   repo, fee 5 bps + slippage 2 bps + funding historis. Dipakai untuk memastikan
   angka bawaan bisa direproduksi.
2. **Simulator portofolio** (`scripts/backtest_portofolio.py`, baru): banyak simbol,
   satu akun, batas 2 posisi bersamaan, margin total 60%, notional minimum 20 USDT,
   risiko 1.5% equity per trade, entry di *open bar berikutnya* (tanpa look-ahead),
   SL didahulukan bila SL & TP tersentuh di bar yang sama, exit `trend_flip`
   memakai harga close bar (pesimistis). Tujuannya mengukur strategi sebagai
   **satu akun**, bukan jumlah R per simbol.

Konfigurasi simbol: tickSize/stepSize/minNotional Binance per Nov 2026 (BTC 100,
ETH/BNB/SOL/XRP/ADA/LINK/DOGE 20), dibulatkan konservatif.

## 3. Hasil bawaan (tanpa filter)

Portofolio, 8 simbol major, 2021-2026, TP 2R, SL 2xATR, tanpa filter:

| Timeframe | Trade | Return | MaxDD | WR | PF | Expectancy |
|---|---|---|---|---|---|---|
| 15m | 3.158 | **-99.3%** | 99.4% | 33.7% | 0.87 | -0.085 R |
| 1h | 2.622 | **-79.2%** | 91.7% | 34.7% | 0.95 | -0.020 R |
| 4h | 651 | -13.6% | 54.6% | 34.4% | 0.98 | +0.003 R |

Penyebab utama di 15m: rata-rata pergerakan per trade terlalu kecil dibanding
biaya. TP 2R hanya kena ~33% dan biaya bolak-balik (fee 10 bps + slippage 4 bps)
setara ~0.3-0.5 R bila SL sempit. Di 4h, jarak SL jauh lebih lebar sehingga
biaya turun jadi ~0.1 R, dan edge yang tersisa (kalau ada) baru terlihat.

Rata-rata per trade di semua simbol/timeframe: **positif hanya di 4h** (+0.03 R),
tapi belum cukup untuk menutup drawdown.

## 4. Yang diuji sebagai filter entry

Semua kandidat diuji pada tabel trade yang sama (bar entry + fitur yang hanya
memakai data tertutup), lalu disaring dengan: (a) expectancy naik, (b) tidak
hanya di satu tahun, (c) tidak hanya di satu simbol, (d) ambang tidak paku.

| Kandidat filter | Hasil | Status |
|---|---|---|
| Volume expansion (`volume >= 2x SMA20`) | +0.10 R (OOS), tapi lunak: +0.04 R saat jumlah posisi tidak dibatasi | cadangan |
| ADX rendah, RSI ekstrem, body besar, gap sesi, hari Rabu | naik di train, **hilang di test** | ditolak |
| Konfirmasi tren (harga di atas EMA200, EMA50>EMA200, tren daily searah) | **negatif** (-0.04 s/d -0.07 R) | ditolak, dibalik |
| Jarak ke EMA200 harian melawan arah trade (`dist200_side <= -8%`) | **+0.09 s/d +0.17 R**, konsisten | **dipakai** |

Temuan yang paling tidak intuitif: filter yang **melawan** tren justru
menguntungkan. Sinyal VIDYA yang searah tren besar (harga jauh di atas EMA200
saat long) punya expectancy terburuk. VIDYA "terlambat" masuk tren, jadi sinyal
searah tren yang sudah terentang adalah titik *late entry* yang sering dibalik.

## 5. Filter yang dipakai

```
dist200_side = (close - EMA200_harian) / EMA200_harian * 100 * arah
             arah = +1 untuk long, -1 untuk short
entry hanya bila dist200_side <= max_dist_pct   (bawaan -8.0)
```

Properti:

* EMA200 dihitung dari candle harian yang **sudah selesai** sebelum hari sinyal.
  Bar hari ini tidak pernah ikut dihitung (dijaga test
  `test_backtest_filter_rezim_tidak_memakai_data_masa_depan`).
* Hari yang belum lengkap (hari berjalan, hari pertama data) dibuang.
* Batas tambahan: `max_daily_atr_pct` (25%) menolak entry saat pasar harian
  terlalu liar, `min_daily_bars` (200) menolak entry bila data harian kurang.

### Hasil 19 simbol, 4h, 2021-2026 (exit bawaan: SL 2xATR, TP 2R)

Semua angka di bawah dihasilkan `scripts/backtest_portofolio.py` dengan
`--no-flip` (exit tren-berganti **tidak** dipakai, karena tidak menambah hasil):

| Varian | Trade | Return | CAGR | MaxDD | WR | PF | Exp. |
|---|---|---|---|---|---|---|---|
| tanpa filter | 734 | **-19.6%** | -3.7% | 55.4% | 34.3% | 0.97 | -0.002 R |
| fade -5% | 443 | +51.4% | +7.6% | 40.5% | 36.8% | 1.11 | +0.083 R |
| **fade -8%** | 410 | **+102.9%** | **+13.3%** | 31.3% | 38.5% | 1.20 | +0.136 R |
| fade -10% | 389 | +88.3% | +11.8% | 27.8% | 38.3% | 1.19 | +0.129 R |
| fade -15% | 346 | +86.2% | +11.6% | 24.7% | 38.7% | 1.21 | +0.141 R |

Semua ambang -5 s/d -15 positif: filternya tidak bergantung pada satu angka ajaib.
Dengan `--exit_on_flip` aktif hasilnya sedikit berubah (+110.2%, maxDD 31.5%,
416 trade), jadi aturan flip tidak wajib; yang wajib adalah filter entry-nya.

### Sensitivitas risiko dan jumlah posisi (fade -8)

Angka dari simulator kerja (aturan sama, exit flip aktif); yang penting di sini
adalah **arah** perubahan, bukan digit terakhir:

| Setelan | Trade | Return | CAGR | MaxDD |
|---|---|---|---|---|
| maks 1 posisi, risiko 1.5% | 279 | +83.3% | +11.2% | 29.5% |
| maks 2 posisi, risiko 0.5% | 461 | +24.0% | +3.8% | **13.3%** |
| maks 2 posisi, risiko 1.0% | 465 | +52.8% | +7.7% | 25.1% |
| **maks 2 posisi, risiko 1.5%** | 456 | +113.6% | +14.2% | 38.3% |
| maks 3 posisi, risiko 1.5% | 585 | +126.4% | +15.4% | 44.8% |
| maks 4 posisi, risiko 1.5% | 680 | +167.0% | +18.8% | 48.6% |

Risiko 0.5% menghasilkan drawdown 13% dengan return +24%: pilihan paling waras
untuk modal yang tidak boleh rugi besar.

### Stabilitas per subperiode (modal 500 direset, filter fade -8, tanpa flip)

| Periode | Return | Trade | MaxDD | WR |
|---|---|---|---|---|
| 2021-2022 | +13.7% | 111 | 16.7% | 36.9% |
| 2023-2024 | +14.6% | 138 | 31.3% | 37.0% |
| 2025-2026 (s.d. Okt) | +55.7% | 161 | 23.6% | 41.0% |

Ketiga subperiode positif, termasuk 2023-2024 (pasar sideways) yang menjadi
tahun terburuk di varian exit lain. Tetap ada tahun merah di beberapa simbol,
dan drawdown 31% pada risiko 1.5% adalah kejadian nyata, bukan skenario ekstrem.

## 6. Perubahan kode

| Berkas | Perubahan |
|---|---|
| `src/volumatic_bot/regime/__init__.py` (baru) | `daily_candles`, `build_regime_from_daily`, `DailyRegimeFilter` (aturan + alasan penolakan berbahasa Indonesia). |
| `src/volumatic_bot/config.py` | Bagian `regime:` (pydantic, `extra=forbid`, batas nilai). |
| `config.yaml` | Bagian `regime:` + komentar alasan & angka hasil. |
| `src/volumatic_bot/streams/kline_feed.py` | `KlineFeed(regime_filter=...)`, `seed_regime`, `seed_regime_series`, penolakan sinyal + hitungan `regime_rejected`. |
| `src/volumatic_bot/backtest/runner.py` | Filter yang sama dipakai di backtester; alasan penolakan masuk `skipped`, catatan masuk `notes`. |
| `src/volumatic_bot/engine/live.py` | Warm-up candle `1d` + refresh tiap `regime.refresh_minutes` (hemat: 1 permintaan per simbol per jam). |
| `src/volumatic_bot/engine/session.py` | Sesi offline memakai filter yang sama. |
| `src/volumatic_bot/engine/trader.py` | **Bug diperbaiki**: `trade.exit_on_trend_flip` sebelumnya tidak pernah dieksekusi (tidak ada kode yang membaca nilainya). Sekarang posisi ditutup saat tren VIDYA berbalik. |
| `tests/test_regime_filter.py` (baru) | 15 test: pembentukan candle harian, anti-look-ahead, aturan filter, integrasi feed, integrasi backtester. |
| `tests/test_engine_trader.py` | 3 test baru untuk `exit_on_trend_flip`. |
| `scripts/backtest_portofolio.py` (baru) | Simulator portofolio multi-simbol, mandiri (tanpa folder kerja), dipakai untuk semua angka di §3-§5. |

**Perintah verifikasi cepat** (data hasil `scripts/unduh_data_uji.py` dan
`scripts/unduh_funding.py`, 19 simbol, 2021-2026):

```bash
# filter aktif -> +102.9%, 410 trade, maxDD 31.3%
python scripts/backtest_portofolio.py --data-dir data/raw --tf 4h     --max-pos 2 --risk-pct 1.5 --dist200-max -8 --no-flip
# filter mati -> -19.6%, 734 trade
python scripts/backtest_portofolio.py --data-dir data/raw --tf 4h     --max-pos 2 --risk-pct 1.5 --dist200-max 99 --no-flip
```

Bawaan `regime.enabled: true` di `config.yaml` **hanya aman untuk timeframe 4h**.
Kalau Anda tetap memakai 15m, filter ini menurunkan kerugian tapi tidak
membuatnya positif; rincian di §7.

## 7. Keterbatasan dan risiko yang tersisa

1. **15m tetap negatif.** Filter bekerja paling kuat di 4h. Mengaktifkan filter
   pada 15m tidak menyelamatkan strategi (-89% vs -93% di BTCUSDT 2021-2026).
   Uji ulang pada data 5 bulan milik repo (BTCUSDT Jul-Nov 2025): tanpa filter
   -33.6% (171 trade), dengan filter -16.7% (55 trade). Kerugian mengecil, tanda
   tetap negatif, jadi **jangan** menganggap filter ini solusi untuk 15m.
2. **Tahun merah tetap ada.** 2023 negatif di semua varian filter (-14 s/d -20%).
   Drawdown 38% pada setelan risiko 1.5% realistis terjadi, bukan skenario ekstrem.
3. **Pemilihan filter memakai seluruh data historis.** Walk-forward 2021-2023 →
   2024-2026 lulus, tetapi periode uji dan periode pemilihan berbagi rezim pasar
   yang sama (bull 2021, bear 2022, sideways 2023, dst). Tidak ada jaminan edge
   berlanjut setelah 2026.
4. **Backtest tidak memodelkan order book, antrean, partial fill, atau penolakan
   margin bursa.** Harga eksekusi memakai `open` bar berikutnya + slippage 2 bps.
5. **Funding memakai tarif historis 8 jam apa adanya.** Posisi yang ditahan
   berhari-hari bisa menerima/membayar lebih dari hitungan ini.
6. **Filter ini menambah 1 permintaan REST per simbol per jam** (candle `1d`,
   bobot 1-2). Untuk 30 simbol: 30-60 bobot/jam, kecil dibanding budget 1800.
7. **Belum diuji di testnet/live.** Semua eksekusi masih melewati `DryRunBroker`;
   klaim "berjalan di live" belum pernah dibuktikan ke bursa (sandbox diblokir
   HTTP 451, sama seperti TAHAP 7).

## 8. Cara memakai

```bash
# 4h, filter aktif (bawaan config sekarang), 19 simbol
volumatic-bot backtest --candles BTCUSDT-15m.csv --symbol BTCUSDT \
    --equity 500 --leverage 10 --funding-file btcusdt_funding.json
# matikan filter (perilaku lama)
#   regime.enabled: false  di config.yaml
# ganti ambang
#   regime.max_dist_pct: -10.0
```

Timeframe bawaan `config.yaml` sekarang **4h**. Tidak perlu profil atau argumen
tambahan:

```bash
volumatic-bot check-config          # timeframe scanner: 4h
volumatic-bot backtest --candles BTCUSDT-15m.csv --symbol BTCUSDT
volumatic-bot run
```

Berkas CSV 15m otomatis digabung ke 4h oleh `backtest` dan `simulate-session`
(pesan "data 15m di-resample ke 4h"). Kalau berkas berisi bar lebih besar dari
`scanner.timeframe`, perintah berhenti dengan pesan jelas, bukan diam-diam
memakai skala yang salah.

### 8.1 Hasil 4h dibanding 15m (19 simbol, 2021-2026, modal 500, lev 10)

Config 4h apa adanya (SL 1,5 ATR "nearest", TP 1,5R, partial 50%, BE 1R,
trailing 2 ATR, warm-up 400, filter -8%):

| Konfigurasi | Trade | Return rata-rata/simbol | Median | Simbol profit | WR | avgR | DD rata-rata |
|---|---|---|---|---|---|---|---|
| **4h, filter ON (config sekarang)** | 723 | **+5,3%** | +1,3% | 10/19 | 48,8% | +0,073 | 13,2% |
| 4h, filter OFF | 1591 | +5,9% | -0,1% | 11/19 | 45,2% | +0,030 | 20,8% |
| 4h, exit "optimasi" (SL 2ATR, TP 2R, tanpa partial) | 658 | +2,7% | +0,6% | 12/19 | — | +0,070 | 14,5% |
| **15m, filter ON** | 5839 | **-91,3%** | -90,5% | 0/8 | 46,3% | -0,196 | 91,5% |
| 15m, filter OFF | 9523 | -95,7% | -95,6% | 0/8 | — | -0,167 | — |

Catatan penting dari tabel ini:

- 15m kehilangan hampir seluruh equity di semua simbol (median -90,5%), juga
  setelah filter. Itu alasan timeframe bawaan dipindah ke 4h.
- Profit factor 4h sekitar 1,17-1,20. Tipis: satu tahun buruk bisa menghapus
  keuntungan dua tahun, dan drawdown 31% pada uji portofolio adalah kejadian
  nyata, bukan skenario ekstrem.
- Exit bawaan LEBIH BAIK daripada SL 2ATR/TP 2R tanpa partial (+5,3% vs +2,7%).
  Jadi `trade.*` sengaja tidak diubah saat pindah timeframe. Rata-rata return
  sedikit lebih tinggi tanpa filter (+5,9%) tetapi dengan DD 20,8% vs 13,2% dan
  avgR +0,030 vs +0,073: filter yang menjaga kualitas trade, bukan menaikkan
  return mentah.
- Angka portofolio (semua simbol dalam satu akun) ada di bagian 6.

### 8.1a Warm-up

`scanner.warmup_bars` dipindah ke **400** (67 hari di 4h). Diukur pada BTCUSDT
4h 2021-2026: error ATR maksimum turun ke ~3% pada 260 bar dan tidak membaik
secara berarti sampai 1500 bar (uji per bandingan sub-run). 1500 bar 4h setara
250 hari, terlalu mahal untuk data uji yang pendek.

### 8.1b Data uji 5 bulan tidak cukup untuk filter

Berkas bawaan `tests/data/btcusdt_15m_5bulan.csv.gz` hanya mencakup 152 hari.
Filter butuh 200 hari harian, jadi `simulate-session` dengan berkas itu
menghasilkan **0 entry** (semua sinyal ditahan dengan alasan "data harian belum
cukup"). Itu bukan kerusakan, tetapi kalau Anda ingin melihat entry, pakai data
minimal 8 bulan. Diuji ulang pada BTCUSDT 4h 2021-2026 (12.780 bar): 17 entry,
83 sinyal ditahan filter, dan warm-up 400 maupun 1250 memberi jumlah entry sama.

### 8.2 Bug yang ditemukan saat uji 4h

Backtest 4h TRXUSDT dan ARBUSDT melempar `ValueError: SL tidak boleh sama dengan
harga entry`. Penyebab: ATR sangat kecil dibanding tickSize (TRXUSDT tick
0,00001), sehingga hasil pembulatan SL sama dengan harga entry. Pesanan seperti
itu juga akan ditolak bursa. Perbaikan: `SymbolFilters.stop_outside_entry()`
memaksa jarak SL minimal satu tick dari entry; `build_trade_plan` memakai fungsi
itu. Test regresi ada di `tests/test_risk_plan.py` (long dan short).
