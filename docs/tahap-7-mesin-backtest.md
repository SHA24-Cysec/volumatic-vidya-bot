# TAHAP 7 — Mesin Trading, Backtester, dan Audit Keamanan

Laporan ini menutup tujuh tahap pengembangan. Isinya: apa yang dibangun pada
TAHAP 7 (mesin trading yang menyatukan semua modul + backtester dengan model biaya
lengkap), **hasil backtest apa adanya — termasuk hasil yang negatif**, bug akuntansi
yang ditemukan saat menguji mesin, dan audit keamanan berupa daftar invarian yang
dijaga test.

Sama seperti tahap sebelumnya: **tidak ada order nyata maupun koneksi ke API
Binance** yang dijalankan dari sandbox ini (`fapi.binance.com` mengembalikan
HTTP 451). Semua angka di bawah dihasilkan dari data historis lokal dan broker
simulasi.

- Versi saat pengujian: **Python 3.14.10** (utama), juga diuji ulang di
  **3.11.18, 3.12.16, 3.13.18**. pytest 9.2.0, ruff 0.17.5, mypy 2.4.1,
  pip-audit 2.10.1.
- Tanggal pengujian: **14 November 2026**.
- Pilihan pengguna pada tahap ini: mesin trading berjalan (loop run/live) **dan**
  backtester dengan model biaya **fee + slippage + funding**.

---

## 1. Apa yang dibangun

| Berkas | Isi |
|---|---|
| `engine/trader.py` (428 baris) | `TradingEngine`: satu jalur keputusan `kline → sinyal → gate order book → RiskManager → executor → SQLite → notifier/dashboard`. Tanpa jalur pintas. |
| `engine/session.py` (228 baris) | Sesi **offline**: memutar candle historis ke **mesin produksi** (bukan simulator terpisah), dengan pemecahan candle menjadi beberapa tick agar SL/TP bisa terpicu di dalam bar. |
| `engine/live.py` (341 baris) | Perakitan sesi **testnet/live**: REST + user data stream + kline gabungan + depth per kandidat + dashboard. Belum pernah dijalankan ke Binance (lihat §7). |
| `backtest/costs.py` (155 baris) | `CostModel` (fee, slippage, funding) + `FundingSeries` (funding historis asli dari data.binance.vision). |
| `backtest/engine.py` (301 baris) | `TradeManager` untuk backtest: SL/TP/BE/trailing per candle, **SL didahulukan** bila SL & TP tersentuh pada bar yang sama. |
| `backtest/runner.py` (482 baris) | `run_backtest`: indikator dihitung sekali, entry di **open bar berikutnya** (bukan close bar sinyal), ukuran posisi memakai `risk.sizing` yang sama dengan produksi. |
| `execution/updates.py` (46 baris) | Jembatan `OrderResult` → `OrderUpdate` (termasuk `realized_pnl`). |

Perintah baru:

```bash
volumatic-bot backtest --candles tests/data/btcusdt_15m_5bulan.csv.gz \
    --symbol BTCUSDT --exchange-info tests/data/exchange_info_sample.json \
    --equity 500 --leverage 10 --risk-pct 1.5 \
    --funding-file tests/data/btcusdt_funding_2025-07_2025-11.json

volumatic-bot simulate-session --candles tests/data/btcusdt_15m_5bulan.csv.gz \
    --symbol BTCUSDT --equity 500 --risk-pct 0.5 --limit-bars 1500

volumatic-bot run --symbols DOGEUSDT --no-dashboard   # butuh jaringan
```

Bedanya `backtest` vs `simulate-session`:

| | `backtest` | `simulate-session` |
|---|---|---|
| Mesin | simulator cepat (`backtest/engine.py`) | **mesin produksi** (`engine/trader.py` + `TradeExecutor` + `DryRunBroker`) |
| SQLite | tidak | ya (posisi, order, insiden tercatat) |
| Notifikasi/dashboard | tidak | ya |
| Kecepatan | ±1 detik / 14.688 bar | ±32 detik / 14.688 bar |
| Kegunaan | riset parameter & biaya | membuktikan jalur yang sama dengan testnet/live |

`run` menolak `mode: dry_run`/`backtest` dengan kode keluar 2 — untuk dua mode itu
pakai `simulate-session`, supaya tidak ada kesan "bot berjalan" padahal tidak
tersambung ke apa pun.

---

## 2. Pagar yang dipegang mesin

Diperiksa **sebelum** order apa pun dikirim:

1. kill switch (file) dan status berhenti harian;
2. batas rugi harian + cooldown setelah rugi beruntun;
3. maksimum posisi bersamaan, maksimum margin, konsentrasi per simbol;
4. satu posisi per simbol — sinyal untuk simbol yang sudah punya posisi dibuang
   sebelum menyentuh broker;
5. sinyal ganda (retransmisi WebSocket setelah reconnect) diabaikan;
6. rencana trade (sizing + level) harus `approved`; `build_trade_plan` **melempar**
   `ValueError` bila qty ≤ 0, jadi pemeriksaan `size.approved` wajib lebih dulu;
7. setelah entry: SL dipasang segera; gagal pasang SL → posisi ditutup market +
   insiden `protection_failed`.

---

## 3. Bug nyata yang ditemukan saat menguji mesin (dan perbaikannya)

Sesi offline pertama **tampak** berjalan, tetapi angkanya mustahil: 3 posisi dibuka,
tidak satu pun tercatat ditutup, dan ekuitas tidak bergerak. Audit menemukan empat
cacat yang saling menutupi:

| # | Berkas | Cacat | Perbaikan |
|---|---|---|---|
| 1 | `execution/broker.py` | Order `closePosition` memakai qty **bertanda** untuk reduce-only, sehingga posisi 0,004 yang ditutup menyisakan **−0,008**; sisa negatif ini lalu memicu insiden palsu "posisi sudah ada di bursa" pada entry berikutnya | `close_qty = abs(pos.qty)` |
| 2 | `execution/broker.py` | Saat SL dan TP tersentuh pada tick yang sama, urutan evaluasi tidak deterministik (bisa "beruntung" kena TP) | `terpicu.sort(key=0 if STOP_MARKET else 1)` → **SL didahulukan** (asumsi pesimistis, sama dengan backtester) |
| 3 | `binance/trading.py`, `execution/broker.py` | `OrderResult.realized_pnl` tidak ada / selalu 0 → laba-rugi realisasi tidak pernah masuk ekuitas | field baru `realized_pnl: Decimal = Decimal(0)`, diisi `DryRunBroker._fill`, diteruskan `execution/updates.py` |
| 4 | `engine/trader.py::handle_price` | Penutupan dideteksi dengan membandingkan `pos` yang **basi** (sudah diambil sebelum harga diproses) → `closed` tidak pernah bertambah, daily state tidak pernah tahu ada rugi | snapshot `sebelum = store.get_position(symbol)` di awal, deteksi `sebelum.active and not terbaru.active` |

**Bug kelima (ditemukan 14 Nov 2026 saat percobaan `run` testnet pertama oleh
pengguna):** `engine/live.py::build_session` memakai `rest.request(...)` sebelum
`await rest.start()`, sehingga setiap `volumatic-bot run` gagal dengan
`RuntimeError: session belum dibuat; panggil start() lebih dulu`. Jalur ini tidak
tertangkap test sebelumnya karena tidak ada test yang menyusun sesi live.
Perbaikan: `await rest.start()` sebelum request pertama, blok `try/except` yang
menutup koneksi + SQLite bila penyusunan gagal, perintah `netcheck`, dan pesan
error ramah (kode keluar 3) alih-alih traceback. Dijaga
`tests/test_engine_live.py` (4 test).

Hasil sesudah perbaikan — sesi offline yang sama, direproduksi dua kali dengan angka
identik:

```
Sesi BTCUSDT selesai: 14688 bar, 15 sinyal, 3 entry, 3 ditutup, 8 ditolak risiko
Ekuitas 500.00 → 487.51 USDT (-12.4892)
Durasi 31.68 detik
posisi terakhir: closed qty=0.004 sisa=0 masuk=115979.6 sl=116496.00
                 alasan=stop_loss realisasi=-2.0656
cooldown: setelah 3 rugi beruntun; sisa 120 menit
```

Artinya: **jalur produksi benar-benar menutup posisi, mencatat realisasi, dan
mengaktifkan cooldown** — bukan sekadar mencetak log.

---

## 4. Model biaya backtest

Tidak ada mode "tanpa biaya" yang diam-diam. Biaya dipisah tiga komponen dan
dicetak terpisah di ringkasan (`fee … slip … funding …`):

| Komponen | Bawaan | Rumus |
|---|---|---|
| Fee taker | 5 bps per sisi | `notional × bps / 10.000` |
| Slippage | 2 bps per sisi, **selalu merugikan** | beli digeser naik, jual digeser turun |
| Funding | dari data historis bila ada (`--funding-file`), jika tidak tarif tetap `funding_rate_8h_pct` | `notional × tarif × jam/8`, tanda mengikuti arah posisi |

Asumsi yang dipilih dan alasannya:

- **Entry di open bar berikutnya**, bukan di harga sinyal — supaya tidak ada
  look-ahead.
- **SL didahulukan** bila SL dan TP tersentuh pada bar yang sama — hasil jadi
  lebih buruk, bukan lebih bagus.
- **Filter order book tidak dipakai** di backtest (data depth historis tidak
  tersedia). Ini membuat hasil backtest **optimistis** terhadap filter, bukan
  pesimistis: di produksi sebagian sinyal akan ditolak lebih awal.
- Funding memakai data nyata: `tests/data/btcusdt_funding_2025-07_2025-11.json`
  (**459 catatan**, interval 8 jam, diunduh dari arsip `data.binance.vision`).
- Data candle: `tests/data/btcusdt_15m_5bulan.csv.gz` — **14.688 bar** 15m
  (Juli–November 2025), BTCUSDT, dari arsip resmi Binance.

---

## 5. Hasil backtest (hanya satu simbol, satu periode — jangan digeneralisasi)

BTCUSDT 15m, 14.688 bar, warm-up 1500 bar, biaya 5 bps + 2 bps per sisi.

| Skenario | Ekuitas | Trade | WR | Bersih (USDT) | Ret | avgR | PF | maxDD | Dilewati |
|---|---|---|---|---|---|---|---|---|---|
| **Bawaan** (5x, risiko 1,5%, margin/simbol 30%) | 50 | **0** | – | 0,00 | 0,00% | – | – | 0,00% | 153 |
| Bawaan, modal 500 | 500 | **0** | – | 0,00 | 0,00% | – | – | 0,00% | 153 |
| Leverage 10x | 500 | 30 | 50,0% | **−47,00** | −9,40% | −0,23 | 0,63 | 13,71% | 118 |
| Margin/simbol 60% | 500 | 30 | 50,0% | **−47,00** | −9,40% | −0,23 | 0,63 | 13,71% | 118 |
| Risiko 0,5%/trade (paling konservatif) | 500 | 102 | 48,0% | **−69,19** | −13,84% | −0,34 | 0,53 | 14,81% | 40 |
| 10x + margin 60% | 500 | 116 | 48,3% | **−225,06** | −45,01% | −0,34 | – | 46,73% | 24 |
| 10x, **biaya dinolkan** (hanya pembanding) | 500 | 31 | 51,6% | +4,51 | +0,90% | +0,03 | 1,04 | 5,09% | 118 |

Rincian biaya pada dua baris teratas yang menghasilkan trade:

| Skenario | fee | slippage | funding | total biaya |
|---|---|---|---|---|
| 10x, eq 500 | 33,17 | 13,27 | **+0,41** (diterima, posisi short) | 46,03 USDT untuk 30 trade |
| Risiko 0,5%, eq 500 | 48,93 | 19,57 | +0,49 | 68,01 USDT untuk 102 trade |

**Kesimpulan yang harus dibaca apa adanya:**

1. **Strategi ini tidak menghasilkan uang pada data ini.** Tanpa biaya hasilnya
   nyaris nol (+0,90% dalam 5 bulan, PF 1,04); begitu fee + slippage dimasukkan,
   hasilnya negatif (−9,40%). Ini pola yang sangat umum: strategi dengan edge tipis
   habis oleh biaya.
2. **Tidak ada satu pun TP penuh yang tercapai** pada skenario 10x. Alasan keluar:
   `stop_loss` 15 kali (rugi ±1R) dan `stop_loss_be` 15 kali (menang tipis setelah
   TP parsial + break-even). Artinya target TP1/TP2 terlalu jauh untuk volatilitas
   BTCUSDT 15m, sementara SL kena lebih dulu.
3. **Funding bukan penyebab kerugian** (+0,41 USDT — justru menerima, karena posisi
   short dibayar saat tarif positif). Yang menghabiskan modal adalah fee + slippage:
   46 USDT biaya vs 7,5 USDT risiko per trade (1,5% × 500) ≈ **0,2R per trade**.
4. **Pagar bawaan (modal 50 USDT, 5x, margin 30%) menolak SEMUA entry** — 153
   sinyal dilewati. Penyebabnya: SL berbasis ATR ±0,35% membuat notional yang
   dibutuhkan untuk risiko 1,5% melebihi batas margin 30% × leverage 5. Bot memilih
   **melewati** trade, bukan membesarkan risiko diam-diam — perilaku ini benar
   secara aturan, tetapi artinya **dengan modal 50 USDT strategi ini praktis tidak
   trading** pada BTCUSDT. Itu temuan penting, bukan sesuatu yang disembunyikan.
5. Melebarkan pagar (10x + margin 60%) membuat trade lebih banyak dan kerugian
   jauh lebih besar (−45%). **Melonggarkan pagar tidak memperbaiki strategi**, ia
   hanya memperbesar kerugian.

Angka di atas **bukan** jaminan kinerja dan tidak boleh dipakai sebagai dasar
menaruh uang. Satu simbol, satu periode, satu rezim pasar.

---

## 6. Audit keamanan

### 6.1 Invarian yang dijaga test (`tests/test_security_invariants.py`, 15 test)

Setiap test menjaga satu aturan yang bila dilanggar berarti bot **tidak boleh**
dipakai dengan uang sungguhan.

| # | Invarian | Cara diperiksa |
|---|---|---|
| 1 | API key/secret tidak pernah muncul di log | `setup_logging` dengan secret asli → semua record (termasuk handler) tersaring |
| 2 | Pola sensitif disaring: `signature=`, `listenKey=`, `token=`, header `X-MBX-APIKEY`, token bot Telegram | `sanitize()` pada contoh string nyata |
| 3 | Tidak ada kredensial tertanam di kode | pindai `src/` untuk `api_key=`/`api_secret="…"` + baris > 200 karakter |
| 4 | `Secrets` tidak punya nilai bawaan | `Secrets(_env_file=None)` → `binance_api_key is None`, `has_api_credentials is False`, `api_key()` melempar `ConfigError` |
| 5 | Mode `live` butuh **dua** pagar | `allow_live: true` saja → error; `live_confirmation` salah → error |
| 6 | Mode bawaan `dry_run` | `config.yaml` yang dikirim di repo |
| 7 | Leverage dibatasi | 11x di config → error |
| 8 | Risiko per trade ≤ 2% | 2,5% → error |
| 9 | SL tidak pernah `reduceOnly` + `closePosition` bersamaan | `build_protection_orders` |
| 10 | SL selalu ada dan memakai `STOP_MARKET` + `closePosition` + `workingType=MARK_PRICE` | sama |
| 11 | Order tidak memakai `positionSide=HEDGE` | `OrderRequest.to_params()` tidak memuat `positionSide` (one-way) |
| 12 | `clientOrderId` ≤ 36 karakter (batas Binance) | panjang hasil `build_protection_orders` |
| 13 | `preflight` di mode testnet tanpa kredensial → keluar 1, bukan jalan | CLI |
| 14 | Ekuitas minimum dijaga config | `min_equity_usdt` |
| 15 | Filter simbol benar untuk `exchangeInfo` contoh | `parse_exchange_info` pada sampel resmi |

### 6.2 Temuan audit (bukan hanya test yang lulus)

1. **`config.yaml` dulu berisi `live_confirmation: "SAYA-MENGERTI-RISIKONYA"`**
   sebagai nilai bawaan. Artinya pengguna yang hanya mengubah `allow_live: true`
   langsung masuk mode live tanpa mengetik apa pun — pagar kedua jadi hiasan.
   **Diperbaiki:** nilainya kini kosong dengan sengaja, dan komentar menjelaskan
   bahwa pengguna harus mengetiknya sendiri.
2. **Penyaring log meloloskan token bot Telegram utuh** (pola menangkap seluruh
   token, bukan hanya awalnya). **Diperbaiki** sehingga yang tersisa hanya
   `bot123456789:***REDACTED***`.
3. **Klaim README lama "bot memvalidasi izin API key dan menolak jalan bila
   withdraw aktif" tidak benar.** Endpoint yang dipakai proyek ini tidak
   menyediakan informasi izin; `/sapi/v1/account/apiRestrictions` butuh izin Spot
   dan belum diimplementasikan. Klaim itu dihapus dari README dan diganti
   pernyataan jujur (lihat §7). Perlindungan yang **nyata** ada: bot tidak pernah
   memanggil endpoint penarikan — `TradingAPI` hanya membungkus endpoint order,
   leverage, dan listenKey.
4. **`cli.py` pernah kehilangan delapan fungsi** karena patch teks yang gagal
   senyap. Prosedur sekarang: setiap patch diikuti `grep` + `ruff` + `pytest`.
   Tidak ada sisa kerusakan pada versi ini (semua perintah `--help` berfungsi).

### 6.3 Pagar berlapis di luar test

- Kunci hanya dari environment (`BINANCE_API_KEY`, `BINANCE_API_SECRET`), tidak
  pernah dibaca dari `config.yaml`; `SecretStr` mencegahnya tercetak tak sengaja.
- Mode `live` butuh `ops.mode: live` **dan** `allow_live: true` **dan**
  `live_confirmation` persis `SAYA-MENGERTI-RISIKONYA`, **dan** flag konfirmasi di
  CLI (`preflight` menolak tanpa flag).
- Kill switch berupa file: sekali ada, semua posisi ditutup dan sesi berhenti.
- `.env.example` hanya berisi nama variabel, tanpa nilai.

---

## 6b. Uji nyata ke Binance **testnet** (14 November 2026)

Sampai TAHAP 7 selesai, tidak ada satu pun koneksi Binance yang berhasil. Pada
14 Nov 2026 ditemukan bahwa **host REST testnet `demo-fapi.binance.com` diblokir**
(HTTP 451 dari sandbox, HTTP 403 di mesin pengguna), sementara host legacy
`testnet.binancefuture.com` menjawab normal. Setelah `net.legacy_testnet_rest`
diaktifkan, endpoint **publik** berhasil diverifikasi sungguhan:

| Uji | Hasil nyata |
|---|---|
| `netcheck` | `[OK ] ping REST`, `[OK ] exchangeInfo (744 simbol)`, `[OK ] sinkronisasi waktu server` (+108 ms) |
| `symbols` | 744 simbol, 528 perpetual USDT TRADING; batas bobot bursa 6000/menit; BTCUSDT minNotional 50, ETHUSDT 20 |
| `scan --equity 50` | 30 simbol lolos dari 528; 391 ditolak filter spread (testnet likuiditasnya tipis — spread 0,1–0,5%, jauh lebih lebar dari pasar nyata); 71 volume; 34 kuota; 7,5 MB unduhan; bobot 20/1800 |
| `book DOGEUSDT` | depth REST nyata: spread 0,0166% (1 tick), slippage 0,0083%, imbalance +18,7% → DITERIMA |
| `sizing DOGEUSDT` | qty=71, notional 24,85 USDT, margin 4,97 @5x, risiko 0,746 USDT (1,49%), biaya 0,05R, BE digeser 0,17% |
| `listen --duration 60` | **WebSocket sungguhan**: 1.723 pesan, 28 simbol, latensi EMA 52 ms, 28 event diproses, shutdown bersih |
| Bandwidth terukur | **±119 MB/jam** untuk 28 simbol (estimasi bot 118,99 MB/jam) |

Dua bug lagi ditemukan pada kesempatan ini dan sudah diperbaiki:

1. `engine/live.py` memanggil `rest.request` sebelum `await rest.start()` →
   `run` selalu gagal dengan `RuntimeError: session belum dibuat`.
2. `cli.py::_cmd_listen` memakai `suppress(Exception)` saat menunggu task yang
   dibatalkan → setiap Ctrl+C/akhir durasi mencetak traceback `CancelledError`
   (`CancelledError` turunan `BaseException`).

### 6b-2. Mode simbol otomatis (ditambahkan 14 Nov 2026)

Sebelumnya `run` **mewajibkan** simbol manual (`--symbols` atau
`scanner.whitelist`). Itu keterbatasan, bukan pilihan desain yang baik: pemindai
(`SymbolScanner`) sudah ada dan terbukti jalan, tetapi hanya dipakai perintah
`scan`/`listen`/`warmup`. `scanner.symbol_refresh_hours` pun diabaikan `run`.

Sekarang:

* tanpa `--symbols` dan `whitelist` kosong → **mode otomatis**: bot memindai
  sendiri, memakai `max_margin_per_trade` (dari `saldo × max_per_symbol_margin_pct`)
  sebagai filter modal, lalu menghangatkan candle simbol terpilih;
* `scanner.symbol_refresh_hours` benar-benar dipakai: pemindaian ulang berkala,
  dan bila daftar berubah, stream kline disambung ulang dengan daftar baru;
* simbol yang **punya posisi aktif tidak pernah dilepas** dari stream;
* pemindaian ulang yang gagal/kosong tidak mematikan sesi (daftar lama dipakai);
* urutan start diperbaiki: exchangeInfo → **saldo (validasi kredensial)** →
  pemindaian. Kunci salah kini gagal dalam ~1 detik, bukan setelah mengunduh
  riwayat 29 simbol.

Diuji dengan jaringan nyata (kredensial dipalsukan, 14 Nov 2026):
`auto=True, simbol=29, contoh=['ETHUSDT','XRPUSDT','DOGEUSDT','BNBUSDT',...],
waktu=9.1s` termasuk warm-up; pemindaian ulang memicu penyambungan ulang stream
seperti dirancang. Test otomatis: 4 test `_refresh_simbol` (offline).

### 6b-3. Database dipisah per mode (14 Nov 2026)

Sebelumnya semua mode menulis ke satu berkas (`data.db_path`). Akibatnya posisi
hasil `simulate-session` (dry-run) terbaca oleh `run` testnet — tampil sebagai
riwayat, tetapi lebih berbahaya: rekonsiliasi membandingkan berkas itu dengan
posisi di bursa dan bisa mencoba "menutup" posisi yang tidak ada.

Sekarang:

* `AppConfig.db_posisi()` → `var/bot-<mode>.db` (mode `dry_run` → `dry-run`),
  dipakai `positions`, `dashboard`, `simulate-session`, `simulate-trade`,
  `reconcile`, dan `run`;
* `AppConfig.db_cache()` → `var/cache.db`, **dipakai bersama** semua mode karena
  isinya data pasar yang sama (menghindari unduh 1500 candle berulang);
* `data.separate_by_mode: false` mengembalikan perilaku lama satu berkas;
* `preflight` menampilkan kedua berkas dan memperingatkan bila `var/bot.db` versi
  lama masih ada beserta perintah `mv` untuk memindahkannya.

Diuji: 10 test baru (`tests/test_db_per_mode.py`) — termasuk membuktikan posisi
yang ditulis di mode `dry_run` **tidak terbaca** di mode `testnet`. Diverifikasi
nyata: `simulate-session` membuat `var/bot-dry-run.db` dan membiarkan
`var/bot.db` lama tidak tersentuh; `positions` lalu melaporkan
`Database: var/bot-dry-run.db (mode dry_run)`.

### 6b-4. Kill switch saat berjalan (15 Nov 2026)

Bug: file kill switch hanya diperiksa di `is_blocked()` (dipanggil ketika ada
sinyal) dan saat start. `touch KILL_SWITCH` pada bot yang sedang berjalan tidak
melakukan apa pun sampai sinyal berikutnya datang — bisa berjam-jam untuk
timeframe 15m. Config menjanjikan "jika file ini ada → tutup semua & hentikan",
jadi ini pelanggaran janji, bukan sekadar ketidaknyamanan.

Perbaikan:

* `TradingEngine.check_kill_switch()` — tutup semua posisi market (reduce-only),
  catat insiden `kill_switch` (critical), hentikan sesi; kegagalan penutupan
  dicatat sebagai error tetapi sesi **tetap** berhenti;
* loop sesi tidur dalam potongan `ops.kill_switch_poll_sec` (bawaan 3 detik) dan
  memeriksa kill switch tiap potongan, bukan menunggu `reconcile_interval_sec`
  (300 detik);
* `periodic()` juga memeriksa sebagai jaring pengaman;
* perintah baru `volumatic-bot kill-switch [--status|--clear]`.

Diuji: `tests/test_kill_switch_runtime.py` (6 test) + simulasi nyata — kill switch
dibuat pada detik ke-2, terdeteksi pada detik ke-2, 1 posisi ditutup market,
sesi berhenti, insiden `kill_switch` tercatat.

### 6b-5. Berkas PID + perintah `stop` (15 Nov 2026)

Masalah nyata dari lapangan: setelah `kill-switch` dipasang, pengguna tidak bisa
memastikan bot benar-benar berhenti (harus `ps -ef | grep`), dan `positions`
menampilkan `Posisi aktif: 0` karena membaca database **mode yang salah**
(bot jalan `testnet`, perintahnya dijalankan dengan config `mode: dry_run`).

Perbaikan:

* `run` menulis `var/bot-<mode>.pid` (PID + waktu mulai), menghapusnya saat
  berhenti, dan **menolak** sesi kedua pada mode yang sama;
* perintah `volumatic-bot stop [--close] [--all-modes] [--timeout] [--force]`:
  SIGTERM berdasarkan berkas PID, membersihkan berkas usang, dan `--close`
  memasang kill switch lebih dulu sehingga posisi ditutup market;
* `volumatic-bot positions --all-modes` menampilkan jumlah posisi aktif di
  **semua** berkas database mode, dengan penanda `<-- ada posisi!`;
* `procutil.proses_hidup()` mengabaikan proses zombie — tanpa itu `stop` bisa
  melaporkan "masih berjalan" padahal sudah mati (terbukti di test).

Diuji: `tests/test_stop_command.py` (8 test, termasuk menghentikan proses tiruan
sungguhan dan membersihkan PID usang).

### 6c. Bug tampilan yang ditemukan dari percobaan `run` pengguna

Sesi testnet pertama pengguna berjalan (mode TESTNET, status BERJALAN), tetapi
dashboard menampilkan beberapa hal menyesatkan. Semuanya sudah diperbaiki:

| # | Gejala di layar | Penyebab | Perbaikan |
|---|---|---|---|
| 1 | `POSISI (0 aktif)` tetapi ada baris `BTCUSDT … SL` | baris **riwayat** ditampilkan tanpa penanda | label `selesai: <alasan>`, baris diredupkan, judul menjadi `(0 aktif, 1 riwayat)` |
| 2 | `Bandwidth 72 MB/jam` untuk **satu** simbol | burst warm-up (884 KB exchangeInfo + 1500 candle) dibagi jendela 54 detik lalu diekstrapolasi | `Metrics.reset_window()` setelah warm-up; total byte tetap dilaporkan |
| 3 | `LOG TERAKHIR — belum ada log` | `run` tidak memasang `RingBufferHandler` (hanya perintah `dashboard` yang memasang) | handler dipasang di `run_live_session` dan dilepas saat sesi ditutup |
| 4 | `run --symbols btcusdt` berjalan padahal BTCUSDT ada di `scanner.blacklist` | `run` memakai `--symbols` langsung, melewati filter pemindai | validasi awal: blacklist ditolak, huruf kecil dinormalkan, salah ketik ditolak + contoh simbol |
| 5 | kesalahan simbol/mode muncul sebagai traceback | `_cmd_run` hanya menangkap error jaringan | `ValueError` ditangkap → pesan singkat, kode keluar 2 |

Temuan operasional: **bandwidth nyata 119 MB/jam untuk 28 simbol** (≈4,2 MB per
simbol-jam). Untuk kuota ketat, turunkan `scanner.max_symbols` atau naikkan
timeframe. Ini juga menjelaskan mengapa spread testnet membuat 391 simbol
ditolak: likuiditas testnet jauh lebih tipis daripada pasar nyata, jadi hasil
`scan` di testnet **tidak** mewakili hasil di live.

Yang **tetap** belum teruji: endpoint bersignature (saldo, order, leverage,
listenKey, user data stream) karena tidak ada API key testnet di lingkungan
pengembangan, serta `run` ujung-ke-ujung.

## 7. Yang belum diuji (jujur)

1. **Order nyata ke Binance belum pernah dijalankan.** Endpoint **publik** testnet
   sudah terverifikasi nyata (§6b), tetapi endpoint **bersignature** — saldo,
   pemasangan order, leverage, listenKey, user data stream — belum: tidak ada API
   key testnet di lingkungan pengembangan. Perilaku trigger STOP_MARKET,
   `closePosition`, dan `listenKeyExpired` **belum terverifikasi**.
2. **`DryRunBroker` bukan emulator pasar.** Ia mengisi order pada harga yang
   diberikan, tanpa antrean order book, tanpa partial fill sungguhan, tanpa
   penolakan margin dari bursa. Pengujian uang sungguhan tetap wajib di testnet.
3. **Backtest hanya satu simbol dan satu periode.** Tidak ada uji multi-simbol
   gabungan, tidak ada walk-forward, tidak ada uji out-of-sample. Angka §5 tidak
   boleh dijadikan dasar memilih parameter.
4. **Filter order book tidak ada di backtest** — hasil backtest lebih optimistis
   daripada produksi.
5. **Sensitivitas TP/SL tidak dipetakan.** Yang diuji hanya leverage, batas margin,
   dan risiko per trade. Target TP yang tidak pernah tercapai (§5 poin 2) adalah
   kandidat penyelidikan berikutnya.
6. **Izin withdraw API key tidak bisa diverifikasi lewat kode** (lihat §6.2 poin 3)
   — ini tanggung jawab pengguna saat membuat kunci.
7. **Belum ada uji beban** 30 simbol dengan WebSocket berjalan berjam-jam.
8. **pip-audit bersih pada saat pengujian** (`No known vulnerabilities found`,
   `requirements.lock`), tetapi itu snapshot hari ini, bukan jaminan besok.

---

## 8. Verifikasi teknis

| Pemeriksaan | Hasil |
|---|---|
| `pytest -q` (Python 3.14.10) | **551 passed** (44,5 detik) |
| `pytest -q` (Python 3.11.18) | **551 passed** (43,9 detik) |
| `pytest -q` (Python 3.12.16) | **551 passed** (46,3 detik) |
| `pytest -q` (Python 3.13.18) | **551 passed** (54,0 detik) |
| `ruff check src tests` | All checks passed |
| `ruff format --check src tests` | bersih |
| `mypy src` (strict) | Success: no issues found in **77** source files |
| `pip-audit -r requirements.lock` | No known vulnerabilities found |
| `pytest tests/test_engine_live.py` | 17 passed (regresi `start()`, host alternatif, validasi simbol, label riwayat, bandwidth) |
| `simulate-session` (ulang) | 3 entry, 3 ditutup, ekuitas 500 → 487,51 (identik dua kali) |
| `sensitivitas_backtest.py` (6 skenario) | angka identik dengan tabel §5 |

Test TAHAP 7 yang baru: `test_backtest_costs.py` (14), `test_backtest_engine.py`
(9), `test_backtest_runner.py` (9), `test_engine_trader.py` (13),
`test_engine_session.py` (9), `test_security_invariants.py` (15) — total **69 test**
baru, dari 482 menjadi **551**.

---

## 9. Cara mereproduksi

```bash
# 1. siapkan lingkungan
uv venv --python 3.14 .venv && uv pip install -e ".[dev]"

# 2. semua test
pytest -q

# 3. mesin produksi pada candle historis (menulis ke SQLite)
volumatic-bot simulate-session --candles tests/data/btcusdt_15m_5bulan.csv.gz \
    --symbol BTCUSDT --equity 500 --risk-pct 0.5 --limit-bars 1500

# 4. backtest dengan biaya + funding nyata
volumatic-bot backtest --candles tests/data/btcusdt_15m_5bulan.csv.gz \
    --symbol BTCUSDT --exchange-info tests/data/exchange_info_sample.json \
    --equity 500 --leverage 10 --risk-pct 1.5 \
    --funding-file tests/data/btcusdt_funding_2025-07_2025-11.json

# 5. tabel sensitivitas pagar risiko
python scripts/sensitivitas_backtest.py \
    --candles tests/data/btcusdt_15m_5bulan.csv.gz \
    --funding tests/data/btcusdt_funding_2025-07_2025-11.json
```

---

## 10. Kesimpulan TAHAP 7

- Mesin trading berjalan dan **terbukti menutup posisi** pada sesi offline, dengan
  ekuitas, realisasi, dan cooldown yang bergerak — setelah empat bug akuntansi
  diperbaiki.
- Backtester memakai model biaya yang bisa diaudit (fee + slippage + funding
  historis asli) dan memilih asumsi pesimistis (SL didahulukan, entry di bar
  berikutnya).
- **Hasilnya negatif setelah biaya** pada BTCUSDT 15m Juli–November 2025, dan
  **nol trade** dengan pagar bawaan untuk modal 50 USDT. Ini disampaikan tanpa
  dibulatkan ke arah yang menyenangkan.
- Audit keamanan menemukan tiga masalah nyata (pagar live yang tidak berfungsi,
  penyaring log yang bocor, klaim README yang berlebihan); semuanya sudah
  diperbaiki dan dijaga test.
- **Langkah wajib sebelum uang sungguhan:** testnet dulu (§7 poin 1–2), dengan
  modal yang siap hilang. Proyek ini tidak menjanjikan profit.
