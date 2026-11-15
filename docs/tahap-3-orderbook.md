# TAHAP 3 — Validasi Order Book & Skor Kandidat

Laporan ini menjelaskan apa yang **sudah** dikerjakan, apa yang **sudah diuji**,
dan apa yang **belum** bisa diuji. Tidak ada angka performa profit di sini:
TAHAP 3 hanya menyaring sinyal, bukan menghasilkan uang.

- Versi paket saat pengujian: Python **3.14.10** (juga diuji 3.11.18, 3.12.16,
  3.13.18), aiohttp 3.14.5, websockets 17.4, pydantic 2.14.3, ruff 0.17.5,
  mypy 2.4.1, pytest 9.2.0.
- Rujukan dokumentasi Binance dipakai **snapshot arsip** (SPA-nya tidak bisa
  dibuka langsung dari sandbox):
  - *Order Book* (`GET /fapi/v1/depth`) — snapshot **2026-01-18**
  - *Partial Book Depth Streams* — snapshot **2026-02-13**
  - *Connect* (aturan stream gabungan) — snapshot 2026

---

## 1. Ruang lingkup TAHAP 3

| Dikerjakan | Tidak dikerjakan (tahap lain) |
|---|---|
| Baca snapshot/book stream, hitung spread, imbalance, wall, slippage | Ukuran posisi berbasis risiko (TAHAP 4) |
| Aturan terima/tolak sinyal dengan **alasan berbahasa Indonesia** | Kirim order / SL / TP (TAHAP 5) |
| Skor gabungan tren + volume + order book & peringkat kandidat | Dashboard `rich` (TAHAP 6) |
| Perintah CLI `book`, opsi `listen --with-book` | Backtester (TAHAP 7) |

**Tidak ada order yang dikirim** oleh kode TAHAP 3. Modul ini hanya membaca
data publik dan memberi keputusan.

---

## 2. Modul baru

```
src/volumatic_bot/
  binance/
    stream_base.py    # basis klien combined stream: reconnect, watchdog, rotasi, SUBSCRIBE
    depth_stream.py   # DepthStreamClient: <symbol>@depth<5|10|20>@<100|250|500>ms
    ws.py             # KlineStreamClient (ditulis ulang di atas stream_base)
    rest.py           # + depth() dan depth_request_weight()
  orderbook/
    models.py         # OrderBookSnapshot, BookLevel, Wall, parse_rest_depth, parse_depth_event
    analyzer.py       # band_volume, imbalance_pct, detect_wall, estimate_fill, analyze
    depth.py          # RestDepthSource (cache TTL), StreamDepthSource, fetch_snapshot_safe
    gate.py           # evaluate() -> BookDecision
  signals/
    ranking.py        # trend_strength, volume_score, book_score, combine, rank_candidates
    pipeline.py       # SignalGate: SignalEvent -> depth -> keputusan -> Candidate
  cli.py              # + perintah `book`, opsi `listen --with-book`
```

---

## 3. Aturan keputusan (urutan pemeriksaan)

Semua ambang ada di `config.yaml` (`orderbook:` dan `scoring:`), **tidak ada
angka ajaib di kode**. Urutannya dari yang paling fatal:

| # | Pemeriksaan | Field config (default) | Alasan penolakan yang muncul |
|---|---|---|---|
| 1 | Data depth tersedia | `reject_if_unavailable: true` | `depth tidak tersedia: ...` |
| 2 | Buku tidak rusak (bid & ask ada, bid < ask) | — | `buku rusak: sisi bid/ask kosong atau harga bersilangan` |
| 3 | Kesegaran snapshot | `depth_snapshot_ttl_sec: 3.0` | `snapshot depth basi Xs > 3.0s` |
| 4 | Spread wajar | `max_spread_pct: 0.05` | `spread 0.1234% > batas 0.0500% (12.3 tick)` |
| 5 | Tidak ada wall besar di depan arah trade | `wall_multiplier: 8`, `wall_proximity_atr: 1.0` | `wall ask 12.4x median di harga 90012.5 (0.42 ATR <= 1.00 ATR) menghalangi arah long` |
| 6 | Likuiditas cukup untuk ukuran order | `target_notional_usdt: 20` | `likuiditas tidak cukup untuk 0.0022 unit ...` |
| 7 | Slippage estimasi dalam batas | `max_slippage_pct: 0.08` | `estimasi slippage 0.1043% > batas 0.0800%` |
| 8 | Skor minimum | `min_score_to_enter: 0.0` (nonaktif) | `skor order book 0.31 < ambang minimum 0.60` |

Rumus yang dipakai (semua murni, tanpa I/O):

```
spread_pct      = (best_ask - best_bid) / mid * 100
imbalance_pct   = (V_bid - V_ask) / (V_bid + V_ask) * 100     # hanya level dalam pita ±0.3% dari mid
wall            = level dengan qty >= 8 x median(qty) pada sisinya, dipilih yang terdekat ke mid
slippage_pct    = |avg_fill - mid| / mid * 100                # avg_fill dari "memakan" level berurutan
skor_buku       = 0.5 + 0.5 * clamp(±imbalance/100)           # + = mendukung arah entry
```

**Wall** adalah sumber order yang *melawan* arah entry: untuk **long** yang
menghalangi adalah sisi **ask**, untuk **short** sisi **bid**. Jarak wall
dinyatakan dalam ATR supaya sebanding antar simbol: `|harga_wall - mid| / ATR`.

Skor gabungan saat beberapa simbol memberi sinyal bersamaan:

```
skor = w_tren * min(|CMO| / trend_cmo_ref, 1)
     + w_volume * (arah * delta_volume_jendela + 1) / 2
     + w_buku * skor_buku
```

Default `w_tren=0.4`, `w_volume=0.3`, `w_buku=0.3` (dinormalisasi otomatis).
Skor **hanya** menentukan urutan saat slot posisi terbatas — tidak menjamin
sinyal bagus.

---

## 4. Kebijakan bandwidth (tetap dijaga)

| Aturan | Penerapan |
|---|---|
| Depth hanya untuk simbol kandidat | `SignalGate.evaluate` selalu dipanggil per sinyal, tidak ada polling massal |
| Cache REST | `RestDepthSource` menyimpan snapshot per simbol selama `depth_snapshot_ttl_sec` (3 s); sinyal beruntun tidak menambah permintaan |
| Bobot REST | `depth?limit=20` = **bobot 2** (tabel resmi: 5/10/20/50→2, 100→5, 500→10, 1000→20) |
| Mode stream | `<symbol>@depth20@100ms` **hanya** selama simbol jadi kandidat, lalu `UNSUBSCRIBE` |
| Batas stream | `@depth` parsial adalah stream 20 level, bukan stream massal `!depth@arr` |
| Klien stream | Satu koneksi gabungan, maks 10 pesan/detik (SUBSCRIBE/UNSUBSCRIBE diberi jeda 0,12 s), rotasi 23 jam |

Perkiraan tambahan bila mode stream aktif: satu simbol depth20@100ms ≈ puluhan
KB/detik saat pasar ramai. Untuk itu default tetap `source: rest` + TTL 3 s.

---

## 5. Perintah baru

```bash
# periksa order book satu simbol (tanpa mengirim order)
volumatic-bot book BTCUSDT
volumatic-bot book BTCUSDT ETHUSDT --side short --atr-pct 0.8 --notional 25

# dengarkan sinyal kline DAN evaluasi order book tiap sinyal
volumatic-bot listen --with-book
```

Contoh keluaran nyata `book` (dijalankan terhadap **server HTTP lokal** yang
meniru respons Binance, karena API asli diblokir 451 dari sandbox):

```
Sumber depth: rest | limit=20 | TTL=3.0s | arah uji: long

BTCUSDT (tick=0.10, uji qty=0.001):
  DITERIMA skor=0.73 | spread=0.0006% | imbalance=+45.5% | slippage=0.0003%
  - spread 0.0006% (5.0 tick)
  - estimasi slippage 0.0003% (2.5 tick, 1 level)
  - imbalance +45.5% mendukung long
Permintaan HTTP: 3 (retry 0, error 0) | unduhan 7.9 KB | bobot terpakai 0/1800 |
estimasi 0.00 MB/jam | hitung internal 0
```

Kode keluar `book`: `0` bila semua simbol diterima, `1` bila ada yang ditolak
(pakai `--allow-reject` bila ingin selalu `0`). Ini memudahkan pemakaian di
skrip/CI.

---

## 6. Hasil pengujian

```
pytest -q                    -> 259 passed (32 detik)
3.11.18 / 3.12.16 / 3.13.18  -> 259 passed (masing-masing)
3.14.10                      -> 259 passed
ruff check + ruff format     -> bersih (44 file sumber + 18 file test)
mypy src (strict)            -> bersih
```

Test baru di TAHAP 3 (99 test tambahan dari 160 → 259):

| File | Isi |
|---|---|
| `tests/test_orderbook_analyzer.py` (21) | parsing REST/stream, qty 0 dibuang, urutan harga, spread tick, buku bersilangan, imbalance pita, deteksi wall (termasuk tidak ada wall), estimasi fill sebagian/penuh, harga limit slippage |
| `tests/test_orderbook_gate.py` (27) | 8 aturan penolakan satu per satu, jalur "filter dimatikan", `reject_if_unavailable=false`, skor naik/turun karena imbalance, komponen skor (tren/volume/buku), normalisasi bobot, peringkat kandidat, `SignalGate` (slot, metadata hilang, depth gagal, target qty dari config) |
| `tests/test_depth_stream.py` (29) | nama stream & validasi levels/speed, klien depth di server `websockets` lokal (terima event, kirim UNSUBSCRIBE, abaikan pesan non-depth), cache REST + TTL + invalidate, `StreamDepthSource` (basi, payload rusak, subscribe/unsubscribe, `wait_first_event`), `fetch_snapshot_safe` |
| `tests/test_cli_book.py` (8) | perintah `book` ujung-ke-ujung lewat HTTP lokal: exchangeInfo → depth → keputusan → kode keluar, `--allow-reject`, simbol tak dikenal, filter dimatikan, flag `--with-book` |
| `tests/test_rest_client.py` (+14) | tabel bobot depth resmi, limit di luar tabel ditolak, validasi nama simbol (mencegah injeksi query) |

### Dua bug yang ditemukan & diperbaiki pada tahap ini

1. **Domain jam tidak konsisten (serius).** Sumber depth memakai
   `time.monotonic` sebagai `fetched_at`, sedangkan `gate.evaluate(now=...)`
   memakai `time.time`. Akibatnya **setiap sinyal ditolak** dengan alasan
   `snapshot depth basi 1794660538.5s > 3.0s`. Ditemukan saat menjalankan
   perintah `book` ujung-ke-ujung, bukan oleh unit test — sekarang ada dua test
   regresi (`test_rest_depth_umur_snapshot_memakai_jam_dinding`,
   `test_stream_depth_umur_snapshot_memakai_jam_dinding`).
2. **Temuan #8 laporan TAHAP 2**: parameter `symbol` dulu masuk ke query string
   tanpa validasi. Sekarang `_validate_symbol()` menolak nama non-alfanumerik
   atau panjang di luar 4–20 karakter, untuk `klines`, `ticker/24hr`, dan
   `depth`.

---

## 7. Yang **belum** diuji / keterbatasan (baca ini)

1. **Tidak ada koneksi nyata ke Binance.** `fapi.binance.com` mengembalikan
   **HTTP 451** dari sandbox. Semua uji depth memakai server lokal. Yang belum
   pernah terbukti: bentuk respons asli, blokir/limit nyata, perilaku
   `depthUpdate` di jaringan, dan testnet `demo-fapi.binance.com`.
2. **Order book bisa dipalsukan (spoofing).** Wall bisa muncul lalu hilang
   dalam milidetik; `detect_wall` tidak bisa membedakan order nyata dan umpan.
   Karena itu skor buku hanya 30% dari total, dan filter bisa dimatikan
   (`orderbook.enabled: false`).
3. **Snapshot REST hanya 1 momen.** Limit default 20 level per sisi pada
   BTCUSDT hanyalah sebagian kecil buku; likuiditas di luar 20 level tidak
   terlihat, sehingga estimasi slippage untuk order besar (bukan kasus bot ini)
   akan terlalu optimistis.
4. **Estimasi slippage mengabaikan latensi, antrean, dan hidden/iceberg
   order**, serta mengasumsikan level tidak berubah selama order dieksekusi.
   Angka ini untuk penyaringan, **bukan** janji harga eksekusi.
5. **Mode stream belum diuji ke Binance**: `DepthStreamClient` hanya diuji ke
   server `websockets` lokal. Ordering `U/u/pu` tidak direkonsiliasi (kita hanya
   memakai snapshot parsial terakhir, bukan buku yang dibangun dari diff).
6. **Skor belum dikalibrasi.** Bobot 0,4/0,3/0,3 adalah titik awal yang wajar,
   bukan hasil optimasi. Belum ada bukti statistik bahwa peringkat ini
   menghasilkan trade lebih baik — butuh backtest (TAHAP 7).
7. **Ambang default belum diuji lintas simbol.** `max_spread_pct: 0.05` masuk
   akal untuk BTCUSDT/ETHUSDT; koin kecil dengan spread 0,1–0,5% akan selalu
   ditolak. Sesuaikan di config atau per-simbol pada tahap berikutnya.
8. Hanya timeframe `15m` dan data uji BTCUSDT yang dipakai; belum ada uji
   multi-timeframe.

---

## 8. Risiko yang tersisa untuk tahap berikutnya

- TAHAP 4 (risiko & ukuran posisi) harus memakai **angka notional nyata** dari
  perhitungan risiko, menggantikan `orderbook.target_notional_usdt` yang kini
  hanya nilai fallback.
- Slot posisi masih dari `risk.max_concurrent_positions`; belum ada
  pemesanan slot/kunci per simbol (baru perlu saat eksekusi TAHAP 5).
- `listen --with-book` mengevaluasi **satu sinyal per waktu**; bila beberapa
  simbol memberi sinyal pada candle yang sama, peringkat lintas simbol belum
  dijalankan dalam satu putaran (fungsi `evaluate_many` sudah siap dipakai).
