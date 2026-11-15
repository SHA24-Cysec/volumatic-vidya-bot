# TAHAP 2 — Koneksi data, cache, dan pemindai multi-simbol

**Status: selesai dan teruji offline. Bagian yang butuh API Binance belum bisa
diuji dari sandbox (lihat §7).**
Tanggal: 14 November 2026. Python pengujian: 3.11.18 / 3.12.16 / 3.13.18 / 3.14.10.

---

## 1. Dokumentasi Binance yang dipakai (diverifikasi, bukan dari ingatan)

Situs `developers.binance.com` sekarang berupa SPA (Zudoku) yang tidak
mengirim teks ke klien non-browser, dan `binance-docs.github.io/apidocs/futures`
hanya me-redirect. Karena itu dokumentasi dibaca dari **snapshot arsip Wayback
2026** dengan tanggal yang bisa Anda periksa sendiri:

| Halaman | Snapshot | Yang diambil |
|---|---|---|
| `general-info` | **2026-02-04** | base URL produksi `https://fapi.binance.com`; **testnet REST `https://demo-fapi.binance.com`**; testnet WS `wss://fstream.binancefuture.com`; header `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)`; `recvWindow` default 5000; wajib backoff saat 429 |
| `market-data/rest-api/Kline-Candlestick-Data` | **2026-01-12** | `GET /fapi/v1/klines`; `limit` default 500 **maks 1500**; bobot 1 / 2 / 5 / 10 menurut `limit` |
| `market-data/rest-api/Exchange-Information` | **2026-01-12** | bobot 1; `rateLimits`: REQUEST_WEIGHT **2400/menit**, ORDERS 1200/menit |
| `market-data/rest-api/24hr-Ticker-Price-Change-Statistics` | **2026-01-12** | bobot **1** (satu simbol) / **40** (semua simbol) |
| `market-data/rest-api/Symbol-Order-Book-Ticker` | **2026-02-13** | bobot **2** (satu simbol) / **5** (semua simbol); catatan: header `X-MBX-USED-WEIGHT-1M` dari endpoint ini tidak akurat |
| `websocket-market-streams/Kline-Candlestick-Streams` | **2026-03-05** | `<symbol>@kline_<interval>`, update tiap 250 ms, field `x` = candle tertutup |
| `websocket-market-streams/Connect` | (mirror teks) | `wss://fstream.binance.com`; combined `/stream?streams=`; koneksi **hanya berlaku 24 jam**; ping server tiap 3 menit, putus bila tidak ada pong 10 menit; **10 pesan masuk/detik**; maks **1024 stream**/koneksi |

### Temuan dokumentasi yang penting

1. **URL testnet REST berubah** menjadi `https://demo-fapi.binance.com`
   (dokumen lama: `testnet.binancefuture.com`). Kalau ingatan saya dipakai,
   bot akan menembak host yang salah. Keduanya tersedia di kode; pilih lewat
   `net.legacy_testnet_rest: true` bila perlu.
2. Bobot `klines` bergantung `limit`: meminta 1500 bar = bobot **10**, bukan 1.
   Warm-up 30 simbol = 300 bobot sekali (dari 2400/menit).
3. `bookTicker` **semua simbol** hanya bobot 5 → jauh lebih murah daripada
   30 panggilan per simbol (60). Ini dipakai untuk filter spread.
4. `ticker/24hr` semua simbol = bobot 40 → dipakai sekali per refresh (tiap 4 jam),
   bukan tiap menit.

---

## 2. Yang dibangun

| Modul | Isi |
|---|---|
| `binance/urls.py` | Base URL produksi/testnet + semua interval (dengan umur milidetik) |
| `binance/errors.py` | `BinanceAPIError`, `RateLimitError`, `IpBannedError`, `BinanceNetworkError`; pesan disanitasi |
| `binance/rest.py` | Klien REST async: keep-alive, gzip, bobot dihitung & dibaca dari header, backoff eksponensial + jitter, `Retry-After`, sinkronisasi jam server, metrik |
| `binance/ws.py` | Combined stream: 1 koneksi untuk semua simbol, reconnect otomatis, watchdog idle, rotasi < 24 jam, SUBSCRIBE/UNSUBSCRIBE tanpa memutus koneksi |
| `binance/kline_messages.py` | Parser event kline WS (termasuk `x`) & baris kline REST |
| `binance/symbols.py` | Parsing `exchangeInfo`: LOT_SIZE, MARKET_LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL, MAX_NUM_*; pembulatan `Decimal` ke tick/step; margin minimum per leverage |
| `storage/db.py` | SQLite (WAL) dijalankan lewat `asyncio.to_thread`; memakai `sqlite_version` (bukan `sqlite3.version` yang dihapus di 3.14) |
| `data/cache.py` | Cache kline + metadata simbol; prune; statistik |
| `data/repository.py` | Penyedia riwayat: hitung jendela candle tertutup, back-fill/forward-fill dari REST **hanya untuk bar yang belum ada** |
| `scanner/scanner.py` | Filter berlapis (murah → mahal), kuota top-N volume, ATR%, skor, alasan penolakan |
| `streams/kline_feed.py` | WebSocket → riwayat per simbol → indikator → `SignalEvent` (hanya candle tertutup, dedupe) |
| `metrics.py` | Bandwidth (MB/jam), latensi WS (EMA), bobot rate limit |
| CLI | `symbols`, `scan`, `warmup`, `storage`, `listen` |

---

## 3. Desain hemat bandwidth (dan angka nyatanya)

### REST
- Warm-up **sekali** per simbol; setelah itu cache SQLite yang menjawab.
  Restart berikutnya hanya mengunduh bar baru (terbukti di test
  `test_repository_forward_fill_hanya_bar_baru`: 3 bar baru = 1 permintaan `limit=3`).
- Filter berlapis: `exchangeInfo` (1) + `ticker/24hr` semua simbol (40) +
  `bookTicker` semua simbol (5) = **46 bobot** untuk menyaring ratusan simbol,
  **sebelum** menyentuh candle.
- Estimasi unduhan warm-up (diukur dari bentuk respons asli, gzip):
  1500 baris JSON = 193 KB mentah → **42,7 KB gzip** per simbol.
  30 simbol ≈ **1,25 MB sekali**, lalu 0 byte saat restart.
- Jeda kecil antar halaman (`net.kline_pace_sec`) supaya tidak membanjiri API.

### WebSocket
- Satu koneksi gabungan untuk semua simbol (batas 1024 stream, kita pakai ≤ 200).
- Hanya stream `@kline_15m`; tidak ada `bookTicker`/`aggTrade`/`depth` untuk semua simbol.
- Ukuran satu pesan kline (diukur dari struktur resmi): **≈ 329 byte**.
- Perkiraan pemakaian (harus dikonfirmasi dengan angka `listen` Anda):

| Simbol | Update/detik/simbol | Perkiraan |
|---:|---:|---:|
| 30 | 1 | ± 35 MB/jam |
| 30 | 4 (paling sibuk) | ± 139 MB/jam |
| 10 | 1 | ± 12 MB/jam |

**Jujur:** 30 simbol × stream kline memang tidak gratis. Binance tidak
menyediakan stream "hanya candle tertutup". Kalau kuota data Anda terbatas,
turunkan `scanner.max_symbols` (10 simbol ≈ 12–46 MB/jam). Angka sebenarnya
akan ditampilkan dashboard (TAHAP 6) dan sudah bisa dilihat sekarang lewat
`volumatic-bot listen` (baris "WS pesan … latensi EMA").

### Latency & reconnect
- Latensi dihitung dari `E` (event time) vs jam lokal, ditampilkan sebagai EMA.
- Reconnect: backoff `1·2^n` (maks 60 s) × jitter 0,75–1,25.
- Rotasi koneksi dipaksa sebelum 24 jam (default 23 jam) **tanpa menunggu pesan**.
- Watchdog idle 120 s (kline stream normalnya ramai tiap 250 ms; sepi 2 menit = mati).

---

## 4. Risiko & aturan yang ditanam di kode

1. **Hanya candle tertutup.** Data berjalan (`x=false`) tidak pernah masuk riwayat
   dan tidak pernah memicu sinyal — diuji eksplisit.
2. **Dedupe.** Event yang sama terkirim ulang (mis. setelah reconnect) tidak
   memicu sinyal ganda.
3. **Rate limit dijaga di muka** (`weight_budget_per_min`, default 1800 dari 2400)
   dan disesuaikan dari header. 429 dihormati (`Retry-After`), **418 = berhenti**
   (bukan diulang, karena mengulang hanya memperpanjang ban).
4. **Kalau REST gagal terus**, bot melempar error jelas — tidak pernah
   "diam-diam" memakai data basi tanpa memberi tahu.
5. **Modal kecil**: simbol dibuang lebih awal bila `minNotional` menuntut margin
   di atas batas per-trade. Tidak ada pembesaran posisi diam-diam.

---

## 5. Hasil pengujian (offline, tanpa koneksi Binance)

```
$ pytest -q
Python 3.11.18 : 160 passed in 31.1 s
Python 3.12.16 : 160 passed in 32.8 s
Python 3.13.18 : 160 passed in 36.9 s
Python 3.14.10 : 160 passed in 31.6 s     # 75 test TAHAP 1 + 85 test TAHAP 2
$ ruff check src tests && ruff format --check src tests   -> bersih (29 file)
$ mypy src (strict)                                       -> Success: no issues found in 35 source files
$ pip-audit -r requirements.lock                          -> No known vulnerabilities found
```

| Test | Yang dibuktikan |
|---|---|
| `test_rest_client.py` (lokal `aiohttp`) | request nyata via TCP: JSON, gzip, header bobot, 400 tanpa retry, 429 dengan `Retry-After`, 418 tanpa retry, 500 dengan backoff naik, signature HMAC valid (diverifikasi server), API key/secret tidak bocor di pesan error, offset jam server, limiter menahan request berlebih |
| `test_ws_client.py` (server `websockets` lokal) | event kline terparse, reconnect setelah koneksi diputus, watchdog idle memaksa reconnect, rotasi < 24 jam, SUBSCRIBE/UNSUBSCRIBE saat daftar simbol berubah, pesan non-kline diabaikan |
| `test_cache_repository.py` | cache dipakai ulang (**0 permintaan** saat restart), forward-fill hanya bar baru, back-fill saat cache pendek, paginasi ≤ 1000/permintaan, candle belum tertutup dibuang, prune |
| `test_scanner.py` | setiap alasan penolakan (volume, spread, blacklist/whitelist, modal, kuota, ATR, data kurang), skor, `series` diteruskan untuk dipakai ulang |
| `test_kline_feed.py` (data nyata) | sinyal bar-demi-bar **identik** dengan hitungan penuh; `x=false` diabaikan; duplikat tidak dobel; event usang diabaikan; offload thread tidak mengubah hasil; buffer dipotong sesuai `max_bars` |
| `test_symbols.py` / `test_metrics.py` | filter & pembulatan `Decimal`, margin minimum, estimasi bandwidth, latensi EMA |

Simulasi offline yang bisa Anda jalankan sendiri:

```
$ python scripts/simulasi_offline.py
Simulasi BTCUSDT 15m: seed 1500 bar, mengalirkan 500 candle tertutup
Selesai dalam 13956 ms | event diproses: 500 | sinyal: 3
  2025-11-26 16:45 UTC  LONG  close=87,778.90 ATR=0.40% ...
  2025-11-30 09:00 UTC  LONG  close=91,432.20 ATR=0.26% ...
  2025-11-30 23:15 UTC  SHORT close=90,615.60 ATR=0.25% ...
Verifikasi silang dengan hitungan penuh: COCOK
```

Catatan performa: 1 event = 28 ms (indikator 2000 bar, dijalankan di thread).
Saat 30 simbol tutup bersamaan pada batas 15 menit: ≈ 0,85 s CPU total, jadi
masih aman, tetapi inilah alasan offload thread diaktifkan.

---

## 6. Bug & celah yang ditemukan pada tahap ini

| # | Temuan | Dampak | Status |
|---|---|---|---|
| 1 | `history()` mengunduh data **dua kali** saat cache kosong/dimatikan (satu kali untuk di-cache, sekali lagi karena hasilnya tidak dipakai) | Bandwidth & bobot terbuang 2× | Diperbaiki (hasil unduhan dipakai ulang) + test |
| 2 | Sinyal bisa **dobel** bila event candle tertutup yang sama terkirim ulang setelah reconnect | Order ganda di TAHAP 5 | Diperbaiki: `_last_processed` per simbol + test |
| 3 | Rotasi 24 jam hanya dicek **setelah** ada pesan masuk → stream sepi bisa tetap tersambung melewati batas | Koneksi diputus paksa oleh Binance tanpa kendali | Diperbaiki: timeout `recv` dipotong ke sisa waktu rotasi |
| 4 | `need_time_sync()` memakai `0.0` sebagai penanda "belum pernah sync" → dengan jam uji yang mulai dari 0 selalu dianggap belum sync | Sinkronisasi jam berulang sia-sia | Diperbaiki (`None` sebagai penanda) |
| 5 | `aiohttp.TCPConnector(enable_cleanup_closed=True)` memunculkan `DeprecationWarning` di Python 3.14 | Noise + API usang | Dihapus |
| 6 | Estimasi bandwidth awal saya "melantai" span ke 30 detik → angka menyesatkan (120 MB/jam padahal 360) | Dashboard melaporkan angka salah | Diperbaiki: jendela < 5 detik lapor 0 (belum cukup data) |
| 7 | Endpoint error 4xx mengembalikan `msg` yang bisa memuat query string bertanda tangan | Potensi kebocoran | Disanitasi + test khusus |
| 8 | Belum ada penjagaan bahwa `symbol` dari luar masuk ke query tanpa validasi bentuk | Risiko injeksi parameter | **Belum diperbaiki** — akan ditambahkan bersama modul order di TAHAP 5 |

---

## 7. Bagian yang **belum** diuji (jujur)

1. **API Binance yang sebenarnya.** Sandbox diblokir (`HTTP 451`). Semua tes
   REST/WS memakai server lokal. Artinya: bentuk respons asli, kode error asli,
   dan perilaku rate limit Binance **belum** pernah saya lihat langsung.
2. **Testnet `demo-fapi.binance.com`.** Nama host diambil dari dokumentasi; belum
   ada bukti koneksi. Kalau gagal, coba `net.legacy_testnet_rest: true`.
3. **Perilaku WebSocket 24 jam** hanya diuji dengan rotasi yang dipercepat (0,1–23 jam),
   bukan menunggu pemutusan sungguhan.
4. **Jumlah simbol > 30** dan dampak nyata kuota 1024 stream/10 pesan per detik
   belum diuji dengan akun sungguhan.
5. Data uji hanya **BTCUSDT**; simbol murah dengan `tickSize`/`stepSize` ekstrem
   (mis. harga 0.0000xx) diuji lewat fixture, bukan data pasar nyata.

---

## 8. Pertanyaan konfirmasi sebelum TAHAP 3

**Pertanyaan utama:** boleh lanjut ke **TAHAP 3 (modul order book + skor sinyal)**?

Sub-pertanyaan yang menentukan desain tahap berikutnya:

1. **Konfirmasi host testnet**: setelah Anda coba `volumatic-bot symbols`
   (atau `scan`) dari jaringan Anda, host mana yang berhasil —
   `demo-fapi.binance.com` atau `testnet.binancefuture.com`? Kalau keduanya
   tidak bisa diakses, kita pakai data publik `fapi.binance.com` untuk uji baca
   saja.
2. **Jumlah simbol aktif**: 30 (default, ±35–139 MB/jam) atau 10–15 (lebih hemat)?
3. **Sumber depth order book** di TAHAP 3: snapshot REST sekali per sinyal
   (paling hemat, default saya) atau langganan `depth20@100ms` sementara untuk
   kandidat (lebih segar, lebih boros)?
4. Apakah `scanner.max_spread_pct` 0.05% sudah wajar untuk daftar simbol Anda,
   atau perlu dilonggarkan (mis. 0.08%)?
