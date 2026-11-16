# Volumatic VIDYA Bot — USDT-M Futures (Binance)

Bot pemindai multi-simbol untuk Binance USDT-M Futures yang memakai indikator
**Volumatic VIDYA** sebagai pemicu perubahan tren, divalidasi dengan order book,
lalu mengelola **maksimal satu posisi per simbol** dengan TP, SL, break-even
(memperhitungkan biaya), dan trailing.

> **Status proyek: TAHAP 7 dari 7 (selesai).** Semua tahap sudah dikerjakan.
> Sudah ada: port indikator + unit test, konfigurasi tervalidasi, klien REST &
> WebSocket hemat bandwidth, cache SQLite, pemindai multi-simbol, umpan
> kline → sinyal, validasi order book + skor kandidat, manajemen risiko (sizing
> dari risiko, rencana SL/TP/BE sadar biaya, batas harian, cooldown, kill switch,
> batas margin & konsentrasi), eksekusi order (entry limit→market, SL
> `closePosition` segera setelah entry, TP reduce-only, partial TP, BE, trailing,
> user data stream, rekonsiliasi, insiden ke SQLite), dashboard `rich` 4 panel +
> notifikasi konsol, **mesin trading satu loop** (`engine/`), **sesi live/testnet**
> yang merakit semua komponen, **backtester dengan model biaya lengkap**
> (fee + slippage + funding historis), serta **audit keamanan** berupa 15 invarian
> yang dijaga test.
> **Timeframe bawaan sekarang 4h.** Di 15m (bawaan repo sebelumnya) backtest
> selalu negatif setelah biaya (-91% s/d -96% per simbol, juga setelah filter).
> TAHAP 8 menambah **filter entry rezim harian** dan memindah default ke **4h**:
> hasil portofolio +102,9% / CAGR +13,3% pada 19 simbol 2021-2026, dengan
> drawdown 31%. Angka per simbol jauh lebih sederhana: rata-rata +5,3%, median
> +1,3%, 10 dari 19 simbol profit. Filter bukan obat untuk semua timeframe —
> lihat [`docs/tahap-8-filter-rezim.md`](docs/tahap-8-filter-rezim.md).
> Perintah `simulate-session`/`backtest` **tidak** mengirim order ke bursa; jalur
> `testnet`/`live` sudah ditulis tetapi **belum pernah dijalankan ke Binance**
> (sandbox pengembangan diblokir HTTP 451 — lihat bagian keterbatasan).

## ⚠️ Baca ini dulu (kejujuran hasil)

- Proyek ini **tidak menjanjikan profit**. Indikator tren apa pun bisa salah,
  dan futures dengan leverage bisa menghabiskan modal Anda.
- **Backtest bawaan (15m) negatif.** BTCUSDT 15m, 14.688 bar (Juli–November
  2025), fee 5 bps + slippage 2 bps: −9,40% pada skenario leverage 10x, dan
  **0 trade** dengan pagar bawaan untuk modal 50 USDT. Diperluas ke 19 simbol
  2021-2026: portofolio 15m **−99,3%**; 1h −79,2%; 4h −13,6%. Rincian TAHAP 7:
  [`docs/tahap-7-mesin-backtest.md`](docs/tahap-7-mesin-backtest.md).
- **TAHAP 8: filter entry rezim harian.** Sinyal hanya diterima bila harga sudah
  terentang >= 8% melawan arah trade terhadap EMA200 harian. Pada **4h**, 19
  simbol 2021-2026: **+102,9%** (CAGR +13,3%, maxDD 31,3%, PF 1,20, 410 trade)
  vs **−19,6%** tanpa filter. Ketiga subperiode 2 tahun positif. Pada **15m
  tetap negatif** (−16,7% vs −33,6% pada data 5 bulan repo). Detail, semua tabel,
  dan daftar keterbatasan: [`docs/tahap-8-filter-rezim.md`](docs/tahap-8-filter-rezim.md).
- Seluruh pengujian berjalan **offline** di sandbox: indikator diuji terhadap
  data kline asli Binance (data.binance.vision) + implementasi referensi
  independen; lapisan REST/WebSocket diuji terhadap **server lokal** (bukan mock).
  Perbandingan terhadap **ekspor TradingView asli belum dilakukan** karena saya
  tidak punya data ekspor Anda — tersedia perintah `volumatic-bot tv-compare`.
- REST **produksi** Binance diblokir dari sandbox pengembangan (HTTP 451), dan
  host testnet baru `demo-fapi.binance.com` juga diblokir (451/403) — **host legacy
  `testnet.binancefuture.com` bekerja**, jadi endpoint publik testnet (ping,
  exchangeInfo, klines, depth, WebSocket) sudah **terverifikasi sungguhan** pada
  14 Nov 2026. Yang **belum**: endpoint bersignature (saldo, order, user stream),
  karena tidak ada API key testnet di sandbox.

## Atribusi & lisensi (penting)

- Konsep dan logika indikator berasal dari **"Volumatic VIDYA" karya BigBeluga**
  di TradingView, berlisensi **CC BY-NC-SA 4.0**.
- Kode di repositori ini adalah **implementasi ulang independen** (tidak menyalin
  kode Pine), tetapi tetap merupakan **karya turunan** dari ide tersebut.
  Konsekuensinya: bagian indikator **non-komersial** dan **share-alike (SA)** —
  jika Anda menyebarkan versi modifikasi, lisensi yang sama harus dipakai.
- Kode infrastruktur (koneksi, eksekusi, risiko, dashboard) ditulis sendiri dan
  tidak berasal dari skrip BigBeluga.
- Perbedaan yang disengaja/diketahui terhadap skrip asli didokumentasikan di
  [`docs/tahap-1-indikator.md`](docs/tahap-1-indikator.md), termasuk perbedaan
  sumber harga pivot (pivot low memakai `close`, pivot high memakai `high`).

## Instalasi

Butuh Python **3.11 sampai 3.14** (build standar, bukan free-threaded).

```bash
# dengan uv (disarankan, dipakai untuk lockfile)
uv sync --python 3.14

# atau venv biasa
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Perintah CLI:

```bash
# tanpa jaringan
volumatic-bot version
volumatic-bot check-config --config config.yaml
volumatic-bot indicator --candles tests/data/btcusdt_15m_5bulan.csv.gz --show 5
volumatic-bot warmup-study --candles tests/data/btcusdt_15m_5bulan.csv.gz
volumatic-bot tv-compare --candles data.csv --tv ekspor_tradingview.csv
volumatic-bot storage                       # info cache SQLite

# butuh jaringan ke Binance
volumatic-bot symbols                       # ringkasan exchangeInfo + filter
volumatic-bot scan --equity 50              # pindai simbol + alasan penolakan
volumatic-bot warmup                        # isi cache candle simbol aktif
volumatic-bot listen                        # dengarkan kline closed & cetak sinyal
volumatic-bot listen --with-book            # + evaluasi order book tiap sinyal
volumatic-bot book BTCUSDT                  # periksa order book & keputusan filter
volumatic-bot book BTCUSDT ETHUSDT --side short --atr-pct 0.8
volumatic-bot netcheck                     # diagnostik koneksi + host testnet alternatif
volumatic-bot kill-switch                  # darurat: tutup semua posisi lalu berhenti
volumatic-bot kill-switch --status         # cek apakah kill switch aktif
volumatic-bot kill-switch --clear          # hapus (izinkan sesi baru)
volumatic-bot stop                         # hentikan bot berdasarkan berkas PID
volumatic-bot stop --close                 # tutup semua posisi market lalu berhenti
volumatic-bot positions --all-modes        # posisi aktif di semua berkas mode

# risiko (tahap 4)
volumatic-bot preflight --equity 40       # tanpa jaringan: kesiapan & keamanan config
volumatic-bot sizing DOGEUSDT --equity 50 --entry 0.35 --atr-pct 2 --trail 0.345
volumatic-bot sizing BTCUSDT --equity 100 --entry 90000 --atr-pct 1 --side short

# mesin + backtester (tahap 7) — tanpa jaringan
volumatic-bot simulate-session --candles tests/data/btcusdt_15m_5bulan.csv.gz \
    --symbol BTCUSDT --equity 500 --risk-pct 0.5
volumatic-bot backtest --candles tests/data/btcusdt_15m_5bulan.csv.gz \
    --symbol BTCUSDT --exchange-info tests/data/exchange_info_sample.json \
    --equity 500 --leverage 10 --funding-file tests/data/btcusdt_funding_2025-07_2025-11.json

# mesin berjalan (butuh jaringan; mode dry_run/backtest ditolak, keluar kode 2)
volumatic-bot run --symbols DOGEUSDT --no-dashboard
```

`book` keluar dengan kode `1` bila keputusannya menolak (berguna untuk skrip);
tambahkan `--allow-reject` bila ingin selalu `0`.

Simulasi pipeline tanpa jaringan sama sekali (memutar ulang data historis):

```bash
python scripts/simulasi_offline.py --seed 1500 --feed 500
```

Cek cepat sesuai versi Python Anda (3.11–3.14): `uv pip install python@...` tidak
perlu; pakai `uv venv --python 3.14 && uv pip install -e ".[dev]"`.

## Konfigurasi

- `config.yaml` — semua parameter non-rahasia (indikator, scanner, risiko, trade,
  biaya, dashboard). Divalidasi dengan pydantic; salah ketik nama field = error.
- `.env` (salin dari `.env.example`) — **hanya** kredensial:
  `BINANCE_API_KEY`, `BINANCE_API_SECRET`, opsional `TELEGRAM_BOT_TOKEN`.

Mode operasi: `backtest` | `dry_run` (**default**) | `testnet` | `live`.
Mode `live` menolak jalan kecuali `ops.allow_live: true` **dan**
`ops.live_confirmation` diset persis `SAYA-MENGERTI-RISIKONYA`.

## Keamanan

- API key **wajib** hanya punya izin Futures, **tanpa** izin withdraw, dan
  sebaiknya IP-whitelisted. **Jujur:** bot **tidak bisa** memverifikasi izin
  withdraw lewat kode — endpoint yang dipakai proyek ini tidak menyediakan
  informasi izin, dan `/sapi/v1/account/apiRestrictions` butuh izin Spot (belum
  diimplementasikan). Jadi izin itu **Anda** yang harus mematikan saat membuat
  kunci. Yang dijamin kode: bot tidak pernah memanggil endpoint penarikan apa pun
  (`TradingAPI` hanya membungkus order, leverage, dan listenKey).
- Semua pesan log disanitasi: API key/secret/listenKey/signature tidak pernah
  ditulis ke log, dashboard, maupun database (lihat `logging_setup.py` + test).
- Batas keras di config: leverage ≤ 10x, risiko per trade ≤ 25% (di atas 2%
  hanya dengan saklar `allow_aggressive_risk: true`), margin total ≤ 95%
  (di atas 80% juga butuh saklar), batas rugi harian ≤ 10%. "All in 100%"
  **tidak didukung** — alasannya aritmetika, lihat
  [`docs/modal-kecil.md`](docs/modal-kecil.md) §2.

## Catatan dokumentasi Binance (penting)

Dokumentasi resmi dibaca dari **snapshot arsip 2026** karena situs
`developers.binance.com` kini SPA dan `binance-docs.github.io` hanya redirect.
Rincian tanggal & temuannya ada di
[`docs/tahap-2-data-scanner.md`](docs/tahap-2-data-scanner.md) §1. Ringkasnya:
bobot `GET /fapi/v1/klines` bergantung `limit` (maks 1500 → bobot 10),
`ticker/24hr` semua simbol bobot 40, `bookTicker` semua simbol bobot 5,
dan **REST testnet sekarang `https://demo-fapi.binance.com`** (bukan
`testnet.binancefuture.com`).

## Order book (TAHAP 3)

Filter order book **bukan** sinyal utama: ia hanya menyaring sinyal indikator.
Urutan pemeriksaan: data tersedia → buku tidak rusak → kesegaran snapshot →
spread → wall di depan arah trade → likuiditas cukup → slippage → skor minimum.
Setiap penolakan menyertakan alasan berbahasa Indonesia di log/terminal.

- Ambang ada di `config.yaml` bagian `orderbook:` (mis. `max_spread_pct: 0.05`,
  `wall_multiplier: 8`, `max_slippage_pct: 0.08`,
  `depth_snapshot_ttl_sec: 3.0`, `reject_if_unavailable: true`).
- Bobot skor gabungan ada di `scoring:` (`weight_trend`, `weight_volume`,
  `weight_book`, `trend_cmo_ref`).
- Hemat bandwidth: depth hanya untuk simbol yang baru memberi sinyal, snapshot
  REST di-cache 3 detik (bobot 2 per permintaan), atau mode stream
  `<symbol>@depth20@100ms` yang berhenti saat simbol tidak lagi kandidat.
- Matikan sama sekali dengan `orderbook.enabled: false`.
- Rincian rumus, uji, dan keterbatasan: [`docs/tahap-3-orderbook.md`](docs/tahap-3-orderbook.md).

## Manajemen risiko (TAHAP 4)

Semua pagar ada di `config.yaml` bagian `risk:` dan `trade:` — tidak ada angka
ajaib di kode. Prinsipnya: **melewati trade selalu lebih baik daripada
membesarkan risiko diam-diam**.

- Ukuran posisi: `qty = (equity x risiko%) / |entry - SL|`, dibulatkan **ke
  bawah** ke `stepSize`. Bila `minQty`/`minNotional` memaksa ukuran naik lebih
  dari `max_qty_round_up_pct` (default 2%), trade **dilewati**.
- Biaya dihitung: fee taker 2 sisi + slippage, dinyatakan dalam USDT dan dalam
  R; trade ditolak bila biaya bolak-balik ≥ `trade.max_fee_r` (default 0,5R).
- Break-even benar-benar impas: digeser `2 x (taker + slippage) + be_buffer_pct`
  dari entry, bukan sekadar harga entry.
- Batas harian: rugi 3%/hari (sekali kena → berhenti sampai hari trading
  berikutnya), maks 20 trade, cooldown 120 menit setelah 3 rugi beruntun,
  maks 2 posisi, maks 1 posisi per simbol, margin total 60%, konsentrasi 30%
  per simbol, kill switch file `KILL_SWITCH`.
- Rincian rumus, contoh keluaran, dan keterbatasan:
  [`docs/tahap-4-risiko.md`](docs/tahap-4-risiko.md).
- **Modal kecil (mis. 7 USDT):** setelan bawaan memberi 0 trade, dan "all in
  100%" mustahil. Profil "fleksibel" yang sudah diukur, tabel `minNotional` per
  simbol, serta peringatan fee/likuidasi ada di
  [`docs/modal-kecil.md`](docs/modal-kecil.md).
- **Profil di dalam config (tanpa file kedua):** `config.yaml` memuat bagian
  `profil.modal-kecil` (leverage 10x, risiko 5% + saklar agresif, 1 posisi,
  `min_equity_usdt` 5, `max_min_notional_usdt` 5). Pakai sekali jalan dengan
  `volumatic-bot --profil modal-kecil check-config`, atau permanen dengan
  mengubah `profil_aktif: "modal-kecil"`. `volumatic-bot profil` menampilkan
  daftar profil beserta nilai yang ditimpanya. Bawaan `profil_aktif: ""` =
  mode aman `dry_run`.
- **Modal 5 USDT (minimum Binance):** pakai `--profil modal-5usdt` (risiko 3%,
  lantai equity 2 USDT). Profil `modal-kecil` berhenti setelah **satu** kerugian
  karena lantainya 5 USDT — penjelasan, tabel terukur, dan matematika bertahan
  hidupnya di [`docs/modal-kecil.md`](docs/modal-kecil.md) §9.

## Eksekusi order (TAHAP 5)

Aturan yang tidak bisa diubah lewat config:

1. Entry → **SL dipasang segera**; jika gagal, posisi ditutup market dan
   insiden `protection_failed` dicatat (critical).
2. SL selalu `STOP_MARKET` dengan `closePosition=true` (tanpa `quantity`,
   tanpa `reduceOnly` — Binance menolak kombinasi itu).
3. **SL/TP dikirim ke Algo Order API** (`POST /fapi/v1/algoOrder`), bukan
   `/fapi/v1/order`. Sejak **2025-12-09** Binance menolak `STOP_MARKET`/
   `TAKE_PROFIT_MARKET` di endpoint lama dengan **−4120** — ini terjadi sungguhan
   di testnet pada 16 November 2026. Parameter berubah: `stopPrice` →
   `triggerPrice`, `newClientOrderId` → `clientAlgoId`, wajib `algoType=CONDITIONAL`.
   Mode `exchange.conditional_order_api` (`auto`/`algo`/`legacy`) mengatur
   routing; `auto` mencoba algo lalu jatuh ke endpoint lama. Rincian, termasuk
   bug "TUTUP GAGAL" palsu karena respons ACK:
   [`docs/tahap-5-eksekusi.md`](docs/tahap-5-eksekusi.md) §10.
4. TP pada harga partial **hanya untuk sebagian posisi**; sisa posisi berjalan
   dengan BE + trailing. Dua order TP di harga sama akan keluar bersamaan.
5. Break-even sudah memperhitungkan fee + slippage, bukan `entry` mentah.
6. Status order dibaca dari **User Data Stream** (`listenKey`, keepalive,
   `listenKeyExpired` → key baru), bukan polling REST.
7. Rekonsiliasi (start + tiap `ops.reconcile_interval_sec`): posisi hantu
   ditutup, posisi hilang ditandai, SL yang lepas dipasang ulang.

```bash
volumatic-bot simulate-trade DOGEUSDT --equity 50 --entry 0.35 --atr-pct 2 \
    --trail 0.345 --path 0.3585,0.3455   # tanpa jaringan, broker simulasi
volumatic-bot positions                    # posisi, order, insiden dari SQLite
volumatic-bot reconcile --symbols DOGEUSDT # samakan catatan dengan bursa (butuh jaringan)
```

Rincian, hasil uji, dan daftar lengkap yang **belum** diuji:
[`docs/tahap-5-eksekusi.md`](docs/tahap-5-eksekusi.md).
## Dashboard & notifikasi (TAHAP 6)

```bash
volumatic-bot dashboard --once            # baca posisi/insiden dari SQLite
volumatic-bot dashboard --demo --once     # data CONTOH (angka karangan, hanya tampilan)
volumatic-bot dashboard                   # layar penuh (terminal interaktif)
```

Empat panel: **status** (mode, ekuitas, jarak ke batas rugi harian, trade, margin,
bandwidth MB/jam, bobot REST), **posisi** (P/L berjalan, jarak SL, tanda BE/partial),
**kandidat** (skor + alasan tidak dipakai), **kejadian** (insiden + ekor log).
`--compact` memberi satu panel ringkas; terminal < 100 kolom otomatis mengurangi
kolom supaya nama simbol tidak terpotong.

Notifikasi bawaan hanya **konsol/log** — tanpa layanan luar dan tanpa kredensial —
dengan penggabungan pesan berulang (`dedupe_sec`) dan batas `max_per_minute`.
Isi `notify:` di `config.yaml` untuk mengatur level minimum dan kelompok pesan.

Rincian, hasil uji, dan keterbatasan: [`docs/tahap-6-dashboard.md`](docs/tahap-6-dashboard.md).

## Mesin trading & backtester (TAHAP 7)

### Mesin trading

`engine/trader.py` menyatukan semuanya dalam satu jalur tanpa pintas:
`kline tertutup → sinyal → gate order book → RiskManager → TradeExecutor →
SQLite → notifier/dashboard`. Pagar diperiksa **sebelum** order dikirim (kill
switch, batas rugi harian, cooldown, maks posisi, margin, konsentrasi, satu posisi
per simbol, sinyal ganda). Setelah entry, SL dipasang segera; gagal → posisi
ditutup market + insiden `protection_failed`.

```bash
# mesin produksi dijalankan pada candle historis (menulis ke SQLite, memakai
# DryRunBroker — jalur yang sama dengan testnet/live)
volumatic-bot simulate-session --candles tests/data/btcusdt_15m_5bulan.csv.gz \
    --symbol BTCUSDT --equity 500 --risk-pct 0.5 --limit-bars 1500

# loop sesungguhnya (butuh jaringan + mode testnet/live; dry_run/backtest ditolak)
volumatic-bot run                      # OTOMATIS: simbol dipilih pemindai + refresh 4 jam
volumatic-bot run --symbols DOGEUSDT   # MANUAL: hanya simbol ini
```

Contoh hasil sesi offline (data nyata, 14.688 bar): 15 sinyal, **3 entry, 3
ditutup**, ekuitas 500 → **487,51** (−12,49), posisi terakhir ditutup `stop_loss`
dengan realisasi −2,07, cooldown 120 menit aktif setelah 3 rugi beruntun. Sesi ini
yang menemukan empat bug akuntansi (qty `closePosition` bertanda, `realized_pnl`
selalu 0, deteksi penutupan memakai state basi, urutan SL/TP tidak deterministik);
semuanya sudah diperbaiki dan dijaga test.

### Deteksi IP egress

Sebelum `run` mengirim order, bot memeriksa IP publiknya sendiri dan menguji
apakah Binance menerimanya (ping + server time). Bila IP ditolak (HTTP 451
lokasi terlarang, 403 whitelist, atau 418 auto-ban), sesi **tidak dimulai**
(exit code 3) alih-alih gagal di tengah jalan setelah posisi dibuka.

```bash
volumatic-bot ip                 # IP publik + status di Binance
volumatic-bot ip --tanpa-binance # hanya baca IP
```

Selama sesi berjalan, IP diperiksa ulang tiap `ipcheck.ulang_setiap_menit`
menit; bila berubah jadi ditolak, bot berhenti membuka posisi baru tanpa
menutup posisi yang sudah ada (SL di bursa tetap berlaku).

Panel KEJADIAN merangkum log: pesan berulang digabung menjadi satu baris
berpenanda `(xN)` (dengan jam kemunculan terakhir), dan daftar simbol pada
`refresh simbol` diringkas menjadi jumlah masuk/keluar. Atur lewat
`dashboard.log_ulang_jendela_menit` (bawaan 90 menit, `0` = hanya pesan yang
benar-benar berurutan).

Hasilnya tampil di dashboard pada panel STATUS BOT (`IP egress  34.19.24.63 (SG)
· Binance OK`), hanya bila ada datanya. Dashboard membaca catatan dari SQLite,
jadi tidak membuka koneksi tambahan. Rincian dan batasnya:
[`docs/ip-egress.md`](docs/ip-egress.md).

### Filter entry rezim harian (TAHAP 8)

`regime:` di `config.yaml` (bawaan **aktif**) menambah satu syarat entry:

```
dist200_side = (close - EMA200_harian) / EMA200_harian * 100 * arah
entry hanya bila dist200_side <= regime.max_dist_pct      (bawaan -8.0)
```

EMA200 dihitung dari candle **harian yang sudah selesai** sebelum hari sinyal
(tidak ada look-ahead, dijaga test). Ini filter *mean-reversion*: sinyal VIDYA
yang searah tren besar justru punya expectancy terburuk (−0,07 R), sedangkan
sinyal saat harga terentang melawan tren menghasilkan +0,14 R.

| 19 simbol, 4h, 2021-2026, risiko 1,5%, maks 2 posisi | Trade | Return | CAGR | MaxDD | PF |
|---|---|---|---|---|---|
| tanpa filter | 734 | −19,6% | −3,7% | 55,4% | 0,97 |
| **filter rezim −8%** | 410 | **+102,9%** | **+13,3%** | 31,3% | 1,20 |
| filter −5% / −10% / −15% | 443 / 389 / 346 | +51,4% / +88,3% / +86,2% | +7,6% / +11,8% / +11,6% | 40,5% / 27,8% / 24,7% | 1,11 / 1,19 / 1,21 |

Per subperiode (modal direset): 2021-2022 **+13,7%**, 2023-2024 **+14,6%**,
2025-Okt 2026 **+55,7%**.

Yang **tidak** berubah: di **15m** strategi tetap rugi (−16,7% vs −33,6% pada
data 5 bulan repo) karena fee+slippage ±14 bps bolak-balik memakan seluruh edge.
Karena itu timeframe 4h yang masuk akal untuk bot ini, dan `trade.*` (SL/TP
berbasis ATR) harus disesuaikan bila Anda pindah timeframe.

Verifikasi ulang (butuh `scripts/unduh_data_uji.py` + `scripts/unduh_funding.py`):

```bash
python scripts/backtest_portofolio.py --data-dir data/raw --tf 4h \
    --max-pos 2 --risk-pct 1.5 --dist200-max -8 --no-flip
```

Bug yang ikut diperbaiki: `trade.exit_on_trend_flip` sebelumnya **tidak pernah
dieksekusi** (tidak ada kode yang membaca nilainya). Sekarang posisi ditutup saat
tren VIDYA berbalik, dan perilakunya dijaga test.

### Backtester

Model biaya **lengkap dan terpisah**: fee taker 5 bps/sisi, slippage 2 bps/sisi
(selalu merugikan), dan **funding historis asli** (`--funding-file`, 459 catatan
8 jam dari arsip Binance). Asumsi pesimistis: entry di **open bar berikutnya**,
**SL didahulukan** bila SL & TP tersentuh di bar yang sama.

Hasil BTCUSDT 15m, 14.688 bar (Juli–November 2025), biaya 5+2 bps:

| Skenario | Ekuitas | Trade | WR | Bersih | Ret | avgR | maxDD | Dilewati |
|---|---|---|---|---|---|---|---|---|
| **Bawaan** (5x, risiko 1,5%, margin 30%) | 50 | **0** | – | 0,00 | 0,00% | – | 0,00% | 153 |
| Bawaan, modal 500 | 500 | **0** | – | 0,00 | 0,00% | – | 0,00% | 153 |
| Leverage 10x | 500 | 30 | 50,0% | **−47,00** | −9,40% | −0,23 | 13,71% | 118 |
| Margin/simbol 60% | 500 | 30 | 50,0% | **−47,00** | −9,40% | −0,23 | 13,71% | 118 |
| Risiko 0,5%/trade | 500 | 102 | 48,0% | **−69,19** | −13,84% | −0,34 | 14,81% | 40 |
| 10x + margin 60% | 500 | 116 | 48,3% | **−225,06** | −45,01% | −0,34 | 46,73% | 24 |
| 10x, biaya dinolkan (pembanding saja) | 500 | 31 | 51,6% | +4,51 | +0,90% | +0,03 | 5,09% | 118 |

Bacaan yang jujur:

1. **Tanpa biaya nyaris nol (+0,90%, PF 1,04); dengan biaya −9,40%.** Edge tipis
   habis oleh fee + slippage (46 USDT biaya untuk 30 trade vs 7,5 USDT risiko per
   trade).
2. **Tidak ada TP penuh yang tercapai** — semua keluar lewat `stop_loss` (15) atau
   `stop_loss_be` setelah TP parsial (15). Target TP terlalu jauh untuk volatilitas
   15m BTCUSDT.
3. **Funding bukan penyebabnya** (+0,41 USDT, justru diterima karena posisi short).
4. **Pagar bawaan menolak SEMUA entry untuk modal 50 USDT** (153 sinyal dilewati,
   karena notional yang dibutuhkan melebihi batas margin 30%). Bot memilih melewati
   trade, bukan membesarkan risiko — benar secara aturan, tetapi artinya dengan 50
   USDT strategi ini praktis tidak trading di BTCUSDT.
5. Melonggarkan pagar (10x + margin 60%) menaikkan jumlah trade **dan** kerugian
   (−45%). Melonggarkan pagar tidak memperbaiki strategi.

Rincian lengkap, audit keamanan (15 invarian), dan daftar yang belum diuji:
[`docs/tahap-7-mesin-backtest.md`](docs/tahap-7-mesin-backtest.md).

## Kompatibilitas Python

Lihat [`docs/kompatibilitas-python.md`](docs/kompatibilitas-python.md) untuk
rincian wheel per versi, catatan Python 3.13/3.14 (event loop, anotasi tipe
tertunda), serta library yang belum mendukung 3.14.

## Daftar risiko yang sudah diketahui

1. Indikator berbasis tren selalu terlambat — entry terjadi setelah tren berubah.
2. Order book bisa dimanipulasi (spoofing); filter order book hanya tambahan.
3. Modal kecil (<50 USD) mudah terkena minimum notional, pembulatan stepSize, dan
   biaya yang relatif besar; bot akan **melewati** trade, tidak membesarkan posisi.
4. Slippage pada kondisi pasar cepat bisa melewati estimasi.
5. Funding rate dapat menggerus posisi yang ditahan lama.
6. Kegagalan jaringan/API bisa membuat SL tidak terpasang; mitigasi: SL dipasang
   segera setelah entry, dan posisi ditutup market bila pemasangan SL gagal.
7. Bug perangkat lunak bisa menyebabkan kerugian — uji dulu di testnet/dry-run.
8. Berlangganan stream kline untuk 30 simbol memakai **±35–139 MB data/jam**
   (bergantung aktivitas pasar). Turunkan `scanner.max_symbols` bila kuota terbatas.
9. Order book bisa dipalsukan (spoofing) sehingga filter depth bukan jaminan.
10. Batas rugi harian dihitung dari catatan bot sendiri (sudah dipersistensi ke
    SQLite sejak TAHAP 5, jadi restart tidak menghapusnya) — tetapi tetap bukan
    data resmi bursa: trade yang dilakukan manual dari aplikasi tidak terhitung.
11. Perkiraan harga likuidasi memakai satu tier MMR tanpa biaya/funding, jadi
    harga likuidasi nyata bisa lebih dekat daripada perkiraan bot.
12. **Order ke bursa nyata/testnet belum pernah diuji** (sandbox kena HTTP 451).
    Semua eksekusi diuji dengan `DryRunBroker` + server lokal tiruan; bentuk
    respons, kode error, dan perilaku trigger asli belum terverifikasi.
13. **Trailing buatan bot** punya celah singkat tanpa SL antara cancel order lama
    dan place order baru. Jangan meninggalkan bot berjalan tanpa pengawasan
    sampai ini diuji di testnet.
14. **Izin API key (withdraw) tidak bisa diverifikasi bot** — bukan fitur yang
    gagal, memang tidak tersedia lewat endpoint yang dipakai. Pakai API key tanpa
    izin withdraw + IP whitelist; itu tanggung jawab Anda.
15. **Hasil backtest negatif** (lihat TAHAP 7). Indikator ini, dengan pagar bawaan,
    tidak menghasilkan uang pada data uji. Jangan menganggap pagar yang lebih longgar
    sebagai solusi — itu hanya memperbesar kerugian.
16. **`DryRunBroker` bukan emulator pasar**: tanpa antrean order book, tanpa
    partial fill sungguhan, tanpa penolakan margin dari bursa. Uji testnet wajib.
17. **Backtest hanya satu simbol, satu periode**, tanpa filter order book dan tanpa
    walk-forward. Angka di README tidak boleh dipakai memilih parameter.
18. **Binance memblokir Indonesia.** Domain Binance diblokir di Indonesia, jadi
    dari jaringan lokal Anda akan selalu muncul HTTP 451 ("restricted location").
    Bot harus dijalankan di VPS luar negeri. `volumatic-bot netcheck` menjelaskan
    hal ini secara eksplisit alih-alih menampilkan traceback.
19. **Spread testnet jauh lebih lebar daripada live** (0,1–0,5% vs 0,02%): `scan`
    di testnet menolak 391 dari 528 simbol. Hasil scan testnet **tidak** mewakili
    live, dan filter `orderbook` bisa menolak sinyal yang di live akan lolos.
20. **Bandwidth terukur 119 MB/jam untuk 28 simbol** (uji testnet nyata). Turunkan
    `scanner.max_symbols` bila kuota terbatas.
21. ~~Semua mode memakai `data.db_path` yang sama.~~ **Sudah diperbaiki
    (14 Nov 2026):** database posisi dipisah per mode
    (`var/bot-dry-run.db`, `var/bot-testnet.db`, `var/bot-live.db`), sedangkan
    cache candle dipakai bersama (`var/cache.db`). Lihat
    [`docs/cara-menjalankan.md`](docs/cara-menjalankan.md) §4c.
23. **Filter rezim (TAHAP 8) hanya terbukti di 4h.** Di 15m/1h strategi tetap
    rugi setelah biaya, dengan atau tanpa filter. Filter dipilih dari data
    historis 2021-2026 yang sama dengan data uji; walk-forward dan uji silang
    simbol lulus, tetapi **tidak ada jaminan edge bertahan** setelah 2026.
    Satu tahun merah tetap mungkin (drawdown 31% pada risiko 1,5% adalah
    kejadian nyata).
24. **Pergantian timeframe mengubah segalanya.** `trade.sl_atr_mult`, `tp1_r`,
    BE, dan trailing memakai ATR timeframe aktif. Pindah dari 15m ke 4h membuat
    jarak SL ~4x lebih lebar, sehingga qty otomatis mengecil (risiko USDT sama)
    dan notional bisa jatuh di bawah `minNotional` untuk modal kecil.
25. **Proteksi diuji ulang setelah insiden testnet 15 Nov 2026**: SL tidak lagi
    dikirim bila harga sudah melewati stop (`-2021`), ada varian SL cadangan
    (`reduceOnly`), `-2022` tidak dianggap gagal, dan posisi yang gagal
    dilindungi **sekaligus** gagal ditutup akan menghentikan sesi
    (`naked_position`). Pesan insiden kini memuat kode bursa + parameter order.
    Tetap: uji testnet sebelum uang sungguhan.
26. **Kill switch kini reaktif (±3 detik)** — sebelumnya hanya diperiksa saat ada
    sinyal, sehingga `touch KILL_SWITCH` pada bot yang berjalan nyaris tidak
    berefek. Ctrl+C / kill proses **tidak** menutup posisi (SL tetap aktif di
    bursa); gunakan `kill-switch` bila ingin keluar semua.
27. **Timeframe bawaan sekarang 4h, bukan 15m.** 15m merugi di semua simbol
    (median -90% per simbol, juga setelah filter), jadi default-nya dipindah.
    Konsekuensinya: CSV 15m yang Anda berikan ke `backtest` akan digabung ke 4h
    lebih dulu (dengan pesan eksplisit), dan berkas 1h/1d ditolak karena tidak
    bisa dipecah. Angka pendukungnya di
    [`docs/tahap-8-filter-rezim.md`](docs/tahap-8-filter-rezim.md) §8.1.
29. **Deteksi IP memakai layanan pihak ketiga** (ipify, ifconfig.co) untuk
    membaca IP publik. Tidak ada data akun yang dikirim, hanya permintaan HTTP
    biasa, tetapi itu tetap koneksi keluar tambahan: setel `ipcheck.enabled:
    false` bila tidak diinginkan. Klasifikasi geo layanan itu bisa berbeda dari
    klasifikasi Binance, jadi yang dipakai sebagai keputusan adalah hasil uji
    langsung ke Binance.
30. **SL bisa jatuh tepat di harga entry** bila ATR lebih kecil dari satu tick
    (TRXUSDT, ARBUSDT di 4h). Dulu melempar `ValueError`; kini
    `SymbolFilters.stop_outside_entry()` memaksa jarak minimal satu tick.
    Test regresi: `tests/test_risk_plan.py`. Pesanan dengan SL = entry tetap
    akan ditolak bursa, jadi jangan memakai pola `round_price(stop_raw)` polos.

## Struktur proyek

```
src/volumatic_bot/
  binance/     klien REST, stream gabungan, kline WS, partial depth WS,
               trading API (order/leverage/listenKey), user data stream
  indicator/   port indikator (core, params, numerics, reference, warmup, tv_compare)
  data/        pemuat candle lokal + cache SQLite & repositori kline
  scanner/     filter multi-simbol (tahap 2)
  orderbook/   analisis depth: spread, imbalance, wall, slippage, gate keputusan
  signals/     skor gabungan tren+volume+buku & peringkat kandidat
  risk/        ukuran posisi, rencana level, batas harian, portofolio, kill switch
  execution/   entry, proteksi SL/TP/partial, BE, trailing, rekonsiliasi (tahap 5)
  engine/      mesin trading satu loop, sesi offline, sesi live/testnet (tahap 7)
  backtest/    model biaya (fee/slippage/funding), simulator, runner (tahap 7)
  positions/   state posisi + penyimpanan SQLite (tahap 5)
  streams/     kline feed (sinyal bar-tertutup); mark price & user data (tahap 5)
  dashboard/   panel rich, sumber data lokal, penangkap log (tahap 6)
  notify/      peristiwa & penyalur notifikasi konsol (tahap 6)
  storage/     SQLite: log trade, cache, state (tahap 2+)
tests/         pytest (unit + integrasi data nyata)
docs/          laporan teknis tiap tahap + docs/modal-kecil.md (modal <50 USDT)
```

## Menjalankan test

```bash
pytest -q                      # semua test (695 test, ~45–85 detik tergantung mesin)
pytest -q -m "not slow"        # lewati studi konvergensi (lebih cepat)
ruff check src tests && ruff format --check src tests
mypy src                       # mode strict
pip-audit -r requirements.lock
```

Versi Python yang dipakai saat pengembangan: lihat `docs/tahap-1-indikator.md`
bagian "Hasil pengujian".
