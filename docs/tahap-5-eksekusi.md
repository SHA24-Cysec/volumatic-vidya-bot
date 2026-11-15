# TAHAP 5 — Eksekusi Order: Entry, Proteksi, Partial TP, BE, Trailing, Rekonsiliasi

Laporan ini menjelaskan apa yang **sudah** dikerjakan, apa yang **sudah diuji**,
dan apa yang **belum** bisa diuji. Bagian terakhir itu penting: **tidak ada satu
pun order yang pernah dikirim ke Binance** selama pengembangan, karena
`fapi.binance.com` mengembalikan **HTTP 451** dari sandbox.

- Versi paket saat pengujian: Python **3.14.10** (juga 3.11.18, 3.12.16,
  3.13.18), aiohttp 3.14.5, websockets 17.4, pydantic 2.14.3, pytest 9.2.0,
  ruff 0.17.5, mypy 2.4.1.
- Rujukan dokumentasi Binance (snapshot arsip Wayback, diambil 14 Nov 2026):
  - *Query Order* — `GET /fapi/v1/order`, bobot **1**, skema respons lengkap
    (memuat `TRAILING_STOP_MARKET`, `closePosition`, `reduceOnly`, `workingType`).
  - *Event: Order Update* — `ORDER_TRADE_UPDATE`, field pendek `o.s/o.c/S/o/f/q/p/ap/sp/x/X/i/l/z/L/n/N/R/m/rp/cp/ot/ps` (snapshot 2025-01-23).
  - *Event: Balance and Position Update* — `ACCOUNT_UPDATE`, field `a.m/B/P`.
  - *User Data Streams / Connect* — listenKey **60 menit**, `PUT` memperpanjang,
    `POST` mengembalikan key aktif, `DELETE` menutup, `/ws/<listenKey>`,
    koneksi tunggal maks **24 jam**.
  - *Account Information V3* — `GET /fapi/v3/account` (bobot 5),
    *Position Information V3* — `GET /fapi/v3/positionRisk` (bobot 5, |
    memuat `positionAmt`, `entryPrice`, `unRealizedProfit`, `leverage`, `marginType`).
- **Tidak diverifikasi dari dokumentasi asli** (halaman *New Order* tidak
  terarsip pada Wayback saat diambil): bobot `POST /fapi/v1/order`. Kode
  mengirim `weight=0` dengan asumsi order masuk ke limit **ORDERS 1200/menit**
  (bukan REQUEST_WEIGHT). Bila ternyata salah, dampaknya hanya pada akurasi
  penghitungan bobot internal — bukan pada keamanan order.

---

## 1. Ruang lingkup

| Dikerjakan | Tidak dikerjakan (tahap lain) |
|---|---|
| Entry `limit_fallback` + fallback market | Dashboard `rich` (TAHAP 6) |
| SL `STOP_MARKET` closePosition/`reduceOnly` **segera** setelah entry | Notifikasi Telegram (TAHAP 6) |
| TP `TAKE_PROFIT_MARKET` reduce-only + partial TP | Backtester (TAHAP 7) |
| BE sadar biaya (dari TAHAP 4) + trailing buatan bot | Funding rate nyata (TAHAP 7) |
| User Data Stream (listenKey, keepalive, expired) | Multi-akun / hedge mode |
| Rekonsiliasi 5 kasus berbahaya + insiden ke SQLite | Order algos (TWAP dsb.) |
| Persistensi posisi & **state harian** (menutup lubang TAHAP 4) | |

**Penting:** kode TAHAP 5 bisa mengirim order nyata **hanya** bila dijalankan
dengan mode `testnet`/`live` dan API key tersedia. Default proyek tetap
`dry_run`, dan `LiveBroker` tidak pernah dipakai pada perintah `simulate-trade`.

---

## 2. Modul baru

```
src/volumatic_bot/
  binance/trading.py     # TradingAPI: order, cancel, set leverage/margin/mode,
                         #   akun, positionRisk, listenKey (signature HMAC)
  binance/user_stream.py # ListenKeyManager + UserDataStream + parser event
  positions/models.py    # PositionState, TrackedPosition
  positions/store.py     # SQLite: positions, orders, incidents, daily_state
  execution/broker.py    # Broker Protocol, LiveBroker, DryRunBroker
  execution/protect.py   # build_protection_orders() → SL/TP/partial
  execution/executor.py  # TradeExecutor: entry → proteksi → BE → trailing
  execution/reconcile.py # reconcile(): 5 kasus berbahaya
  risk/persistence.py    # simpan/pulihkan state harian
  storage/db.py          # + tabel positions/orders/incidents/daily_state
  cli.py                 # + simulate-trade, reconcile, positions
```

---

## 3. Urutan eksekusi (tidak boleh diubah)

```
tiket risiko (approved)
   │
   ├─ cek posisi bursa: sudah ada?        → batalkan + insiden duplicate_position
   ├─ entry (limit IOC/GTC → tunggu → market)
   ├─ simpan posisi (state=open) ke SQLite
   ├─ pasang SL  (STOP_MARKET closePosition, workingType=CONTRACT_PRICE)
   │     └─ gagal?  → TUTUP MARKET + insiden protection_failed (critical)
   ├─ pasang TP  (TAKE_PROFIT_MARKET reduceOnly)
   │     └─ gagal?  → insiden tp_failed (warning), posisi tetap punya SL
   ├─ TP parsial terisi → SL digeser ke BE (BE sudah termasuk fee+slippage)
   ├─ harga melewati pemicu trailing → SL digeser (hanya searah)
   └─ SL/TP terisi → state=closed + alasan (stop_loss | stop_loss_be | take_profit)
```

Alasan detail:

- **SL dulu, TP kemudian.** Posisi tanpa SL adalah risiko yang tidak boleh
  berlangsung lama; tanpa TP hanya kehilangan potensi.
- **`closePosition=true` untuk SL** berarti seluruh sisa posisi ditutup dengan
  satu order, dan qty tidak dikirim (Binance menolak `closePosition` +
  `reduceOnly` bersamaan — ini pernah menjadi bug klasik di banyak bot).
- **Hanya sebagian posisi yang diberi TP** saat partial TP aktif. Dua order TP
  pada harga yang sama akan terpicu **bersamaan**, sehingga "TP parsial +
  trailing" berubah menjadi keluar semua di TP1. Sisa posisi dibiarkan berjalan
  dengan SL break-even + trailing.

---

## 4. Broker nyata vs simulasi

| | `LiveBroker` | `DryRunBroker` |
|---|---|---|
| Kirim order | `TradingAPI` (HMAC + timestamp + recvWindow) | di memori |
| Fill market | bursa | harga acuan `set_price` |
| Fill limit | bursa | saat harga menyentuh limit |
| SL/TP | bursa | terpicu `on_price()` |
| Fee | dicatat dari event | `taker_bps`/`maker_bps` dihitung |
| Risiko | **uang sungguhan** | nol |

Eksekutor hanya bergantung pada `Broker` Protocol, jadi alur yang diuji dengan
`DryRunBroker` adalah alur yang sama dengan live.

---

## 5. Rekonsiliasi (5 kasus berbahaya)

| Kasus | Tindakan | Insiden |
|---|---|---|
| Posisi ada di bursa, tidak di catatan | tutup market (posisi tak dikenal tidak dibiarkan) | `orphan_position` (critical) |
| Posisi di catatan, tapi hilang di bursa | tandai `closed` alasan `hilang_di_bursa`; batalkan order proteksi menggantung | `position_missing` (warning) |
| Posisi ada, **SL tidak ada** | pasang ulang SL; gagal → tutup posisi | `missing_stop_loss` / `rearm_failed` (critical) |
| Arah catatan ≠ arah bursa | tutup posisi | `side_mismatch` (critical) |
| Order proteksi tanpa posisi | batalkan | — |

Rekonsiliasi dijalankan saat start dan berkala (`ops.reconcile_interval_sec`,
default 300 detik) — **jarang**, sesuai aturan hemat bandwidth.

---

## 6. Perintah CLI baru

```bash
# tanpa jaringan: siklus penuh di broker simulasi
volumatic-bot simulate-trade DOGEUSDT --equity 50 --entry 0.35 --atr-pct 2 \
    --trail 0.345 --path 0.3585,0.3455

# tanpa jaringan: lihat posisi, order, dan insiden yang tersimpan
volumatic-bot positions

# dengan jaringan (testnet/live): samakan catatan dengan bursa
volumatic-bot reconcile --symbols DOGEUSDT
```

Keluaran nyata `simulate-trade` (data uji DOGEUSDT, tick 0,00001, stepSize 1):

```
Risiko: DISETUJUI DOGEUSDT: qty=133 notional=46.55 USDT margin=9.31 @5x
risiko=0.745 USDT (1.49% equity) SL=1.60% biaya=0.065 USDT (0.09R) liq~19.60%
| BELI entry=0.35 SL=0.344400 (vidya_trail+min_atr, 1.60%) TP1=0.358400
  BE=0.350600 trail_aktif=0.355600

Eksekusi: OK DOGEUSDT LONG open qty=133/133 entry=0.35 SL=0.344400 TP1=0.358400

Harga bergerak → 0.3585
  order terpicu: partial_tp: TAKE_PROFIT_MARKET SELL DOGEUSDT id=1002 FILLED 66/66 @ 0.3584
Harga bergerak → 0.3455
  order terpicu: stop_loss: STOP_MARKET SELL DOGEUSDT id=1003 FILLED 67/67 @ 0.3506

Status akhir: DOGEUSDT LONG closed qty=0/133 entry=0.35 SL=0.350600 ... partial BE
Broker simulasi: fee=0.0329 USDT, realisasi=0.5946 USDT
rekonsiliasi: tidak ada perbedaan antara bot dan bursa
State harian disimpan: hari=2026-11-15 kerugian=0.00 trade=1
```

Angka di atas **bukan** hasil backtest dan bukan prediksi; itu hanya bukti
mekanisme (TP1 → BE → SL) berjalan sesuai rancangan.

---

## 7. Hasil pengujian

```
pytest -q                    -> 420 passed (~35 detik)
3.11.18 / 3.12.16 / 3.13.18  -> 420 passed (masing-masing)
3.14.10                      -> 420 passed
ruff check + ruff format     -> bersih (58 file sumber + 21 file test)
mypy src (strict)            -> bersih
pip-audit -r requirements.lock -> tidak ada kerentanan dikenal
```

65 test baru (355 → 420):

| File | Isi |
|---|---|
| `tests/test_trading_api.py` (11) | HMAC signature & `timestamp`/`recvWindow` divalidasi server; field order hanya yang relevan; SL `closePosition` tanpa qty; error `-2021` muncul jelas; `set_leverage`/`marginType`/`positionMode`; `openOrders`, `positionRisk`, saldo; `listenKey` create/extend/delete; `clientOrderId` ≤ 36 karakter |
| `tests/test_user_stream.py` (13) | parsing `ORDER_TRADE_UPDATE` (termasuk likuidasi `autoclose-`) & `ACCOUNT_UPDATE`; payload salah ditolak; keepalive membuat/memperpanjang/membuat ulang key; batas `keepalive_sec`; reconnect setelah diputus; `listenKeyExpired` → key baru + sambung ulang; pesan non-JSON diabaikan |
| `tests/test_positions_store.py` (10) | simpan/baca/overwrite posisi, riwayat & posisi aktif, `be_price`/order id `None`, log order, insiden berurutan, `daily_state` roundtrip, **state harian dipulihkan setelah "restart"**, batas rugi harian tetap terkunci setelah restart |
| `tests/test_execution_cycle.py` (11) | entry+SL+TP segera; tiket belum disetujui tidak dieksekusi; duplikat posisi ditolak; **SL gagal → tutup market + insiden**; TP parsial → BE (di atas entry, termasuk biaya); SL setelah BE menutup sisa; trailing hanya searah & tidak mundur; ATR 0 diabaikan; tutup manual; event likuidasi → insiden critical; `limit_fallback` → market |
| `tests/test_reconcile.py` (8) | bersih; SL hilang → dipasang ulang; posisi hantu → ditutup; posisi hilang → ditutup di catatan + order menggantung dibatalkan; arah tidak cocok → ditutup; SL gagal dipasang ulang → posisi ditutup |
| `tests/test_execution_protect.py` (12) | SL selalu `closePosition` tanpa qty, TP reduce-only, sisi BUY untuk short, partial TP hanya sebagian, `partial_qty` tidak valid diabaikan, `clientOrderId` factory, CLI `simulate-trade` (sukses, simbol tak dikenal, ditolak risiko), `positions`, `reconcile` menolak mode dry_run |

### Dua bug desain yang ditemukan & diperbaiki di tahap ini

1. **TP parsial + TP sisa di harga yang sama** (ditemukan saat menjalankan
   `simulate-trade`, bukan oleh unit test): kedua order terpicu bersamaan,
   sehingga posisi keluar semua di TP1 dan trailing tidak pernah berjalan.
   Sekarang hanya sebagian posisi yang punya TP; sisanya mengikuti BE+trailing.
2. **Deteksi "TP parsial" yang salah setelah TP kedua terisi** — posisi
   dikira parsial lagi (sisa qty dihitung ulang dari qty awal), sehingga status
   akhir menjadi `partial` padahal sudah tutup. Diperbaiki dengan penanda
   `partial_done`.

---

## 8. Yang **belum** diuji / keterbatasan (baca ini)

1. **Tidak ada order nyata ke Binance.** Testnet `demo-fapi.binance.com` maupun
   `testnet.binancefuture.com` tidak pernah dihubungi (sandbox HTTP 451). Yang
   belum terbukti: bentuk respons order asli, kode error asli (−2010, −2021,
   −4164, −4131), perilaku trigger `STOP_MARKET`/`TAKE_PROFIT_MARKET`, antrean
   `closePosition`, dan latensi nyata.
2. **`DryRunBroker` bukan emulator pasar.** Ia tidak memodelkan slippage
   berjalan, likuiditas, gap, spread, funding, atau antrean order. Hasil
   simulasi **tidak** bisa dianggap perkiraan performa.
3. **User Data Stream belum pernah tersambung ke Binance.** Semua uji memakai
   server `websockets` lokal. Yang belum terbukti: aturan 60 menit listenKey,
   perilaku `listenKeyExpired` dari server asli, dan disconnection paksa 24 jam.
4. **Belum ada uji end-to-end `testnet`** (entry → SL/TP → rekonsiliasi dengan
   saldo nyata). Ini pekerjaan pertama yang harus Anda lakukan di mesin sendiri
   setelah review (`volumatic-bot reconcile --symbols ...` dengan mode testnet).
5. **Trailing masih buatan bot** (`trailing_mode: bot`): SL diganti
   (cancel + place baru) setiap kali harga bergerak melewati ATR. Ada celah
   singkat **tanpa SL** antara cancel dan place — bila harga melonjak pada saat
   itu, posisi tanpa proteksi. Mitigasi yang belum dikerjakan: pakai
   `TRAILING_STOP_MARKET` native atau pasang SL baru **sebelum** membatalkan
   yang lama (harus diuji; Binance menolak SL ganda dengan `closePosition`).
6. **Partial TP hanya satu kali** (di TP1). Tidak ada TP2/TP3 bertingkat.
7. **Funding rate belum dipakai** di eksekusi; posisi yang ditahan lama bisa
   kehilangan lebih banyak daripada perkiraan R.
8. **Rate limit order (ORDERS 1200/menit) belum dipantau**; bot hanya memantau
   bobot REQUEST_WEIGHT. Untuk 2–3 posisi ini tidak masalah, tetapi perlu
   ditambahkan sebelum dipakai multi-simbol agresif.
9. **Rekonsiliasi memakai 1 permintaan `positionRisk` + 1 `openOrders` per
   simbol**; untuk 30 simbol itu 60 permintaan per siklus. Konfigurasi default
   (300 detik) membatasi dampaknya, tetapi simbol yang dicek sebaiknya hanya
   simbol yang punya catatan posisi aktif.
10. **Mode `live` belum dikunci ganda.** `preflight` memeriksa, tetapi
    `simulate-trade`/`reconcile` tidak memaksa flag konfirmasi tambahan untuk
    live; ini akan dirapikan bersama dashboard (TAHAP 6).
11. **Belum ada pengecekan izin API key (withdraw) secara nyata.** Endpoint
    yang dipakai proyek ini tidak menyediakan info izin; rencana: cek
    `/sapi/v1/account/apiRestrictions` (butuh izin Spot) dan **menolak live**
    bila tidak bisa diverifikasi. Belum diimplementasikan — jangan pakai API key
    yang punya izin withdraw.
12. **Belum ada uji beban**: 30 simbol × user stream + 2 posisi berjalan
    bersamaan belum pernah dijalankan.

---

## 9. Yang harus Anda lakukan sebelum uang sungguhan

1. Jalankan `preflight --equity <saldo>` dan baca semua barisnya.
2. Pakai API key **Futures saja, tanpa withdraw, IP whitelisted**.
3. Uji di testnet: `ops.mode: testnet`, lalu `simulate-trade` → `reconcile`
   berulang; periksa `positions` dan `incidents` setiap kali.
4. Periksa manual di web testnet: SL benar-benar terpasang **setelah** entry,
   TP parsial terisi, SL bergeser ke BE, dan posisi tertutup saat SL kena.
5. Baru setelah itu pertimbangkan `live` dengan modal yang siap Anda kehilangan.

---

## 10. Insiden nyata 16 November 2026: SL ditolak −4120 (Algo Order API)

Ini satu-satunya bagian dokumen ini yang berasal dari **eksekusi sungguhan ke
testnet**, bukan simulasi. Isinya kegagalan yang harus Anda pahami sebelum
menjalankan ulang.

### 10a. Yang terjadi

`volumatic-bot run` pada mode testnet membuka posisi `CTSIUSDT` (SHORT
339314 @ 0,02122). Beberapa detik kemudian:

```
[SL GAGAL] CTSIUSDT BinanceAPIError kode=-4120 HTTP=400 | /fapi/v1/order:
  [kode -4120] [HTTP 400] parameter salah / request tidak valid] Order type not
  supported for this endpoint. Please use the Algo Order API endpoints instead.
[SL GAGAL] CTSIUSDT ... (varian 2, pesan sama)
[DARURAT] CTSIUSDT: SL gagal dipasang ... → menutup posisi market
[KRITIS] CTSIUSDT: posisi TANPA SL dan gagal ditutup otomatis
```

Panel dashboard menampilkan `posisi TANPA SL dan gagal ditutup otomatis` dan
status bot **BERHENTI**. Dua-duanya benar menurut kode saat itu, dan keduanya
adalah **dua bug berbeda**:

1. **SL tidak pernah terkirim ke endpoint yang benar.** Sejak **2025-12-09**
   Binance memindahkan order kondisional (`STOP_MARKET`, `TAKE_PROFIT_MARKET`,
   `STOP`, `TAKE_PROFIT`, `TRAILING_STOP_MARKET`) ke **Algo Order API**:
   `POST /fapi/v1/algoOrder`. Mengirimnya ke `POST /fapi/v1/order` dijawab
   **−4120**. Kedua "varian" SL bot sama-sama order kondisional, jadi keduanya
   gagal — percobaan cadangan tidak menolong.
2. **Penutupan darurat dilaporkan gagal padahal berhasil.** Order MARKET
   penutup dikirim tanpa `newOrderRespType`, sehingga bursa menjawab **ACK**
   (`executedQty=0`). Kode lama menilai "tidak ada qty terisi" = gagal, menandai
   posisi telanjang, lalu menghentikan sesi.

### 10b. Perubahan parameter yang wajib (dari dokumentasi resmi)

| Order lama (`/fapi/v1/order`) | Algo Order API (`/fapi/v1/algoOrder`) |
|---|---|
| `stopPrice` | **`triggerPrice`** |
| `newClientOrderId` | **`clientAlgoId`** |
| — | **`algoType=CONDITIONAL`** (wajib) |
| `orderId` di respons | **`algoId`** di respons |
| `status` di respons | **`algoStatus`** di respons |
| `closePosition=true` + `quantity` dilarang | sama: `closePosition` tidak boleh bersama `quantity`/`reduceOnly` |

Endpoint pendukung: `DELETE /fapi/v1/algoOrder` (cukup `algoid`/`clientalgoid`,
**tanpa** `symbol`), `GET /fapi/v1/openAlgoOrders` (bobot 1 dengan `symbol`,
40 tanpa), `DELETE /fapi/v1/algoOpenOrders` (bobot 1), `POST /fapi/v1/algoOrder`
bobot 0. Sumber: dokumentasi resmi *New Algo Order*, *Cancel Algo Order*,
*Current All Algo Open Orders*, *Cancel All Algo Open Orders* — snapshot arsip
**2025-12-09**; pengumuman perubahan di changelog Binance **2025-11-06**;
berlaku efektif **2025-12-09**.

### 10c. Yang diperbaiki di kode

1. `binance/trading.py`
   - `OrderRequest.is_conditional` + `CONDITIONAL_TYPES`;
   - `OrderRequest.to_algo_params()` (pemetaan parameter di tabel atas);
   - `TradingAPI.place_order()` merutekan order kondisional ke
     `/fapi/v1/algoOrder`, dengan mode `exchange.conditional_order_api`:
     `auto` (bawaan, algo dulu lalu endpoint lama), `algo`, `legacy`;
   - `parse_order_result()` mengenali respons algo (`algoId`/`algoStatus`/
     `triggerPrice`/`orderType`) → `OrderResult.is_algo=True`;
   - endpoint baru: `cancel_algo_order`, `open_algo_orders`,
     `cancel_all_algo_open_orders`.
2. `execution/broker.py`
   - `LiveBroker.cancel()` mencoba endpoint biasa lalu algo (algoId tidak
     dikenal endpoint biasa → −2013, lalu endpoint algo berhasil);
   - `LiveBroker.open_orders()` menggabungkan `openOrders` + `openAlgoOrders`,
     karena kalau tidak, rekonsiliasi menyimpulkan "SL hilang" padahal aktif.
3. `execution/reconcile.py` — kesimpulan "punya SL" diambil dari **tipe order**
   (`STOP_MARKET`), bukan dari label `purpose` internal bot yang tidak pernah
   dikirim ke bursa. Tanpa ini, rekonsiliasi memasang SL **kedua**.
4. `execution/executor.py` — order MARKET (entry & penutupan darurat) memakai
   `newOrderRespType=RESULT`; bila bursa tetap menjawab tanpa qty terisi,
   posisi dikonfirmasi ulang lewat `positionRisk` sebelum menyimpulkan gagal.

### 10d. Kalau Anda mengalami ini lagi

1. **Tutup posisi secara manual** di web testnet/live (Posisi → Close). Jangan
   menunggu bot: posisi tanpa SL bisa berjalan sampai likuidasi.
2. Periksa panel INSIDEN: `naked_position` + `close_failed` = kondisi paling
   berbahaya. Bot sudah berhenti sendiri, jadi tidak ada entry baru.
3. Jalankan versi terbaru, lalu `volumatic-bot check-config` — pastikan
   `conditional_order_api` terbaca (`auto` bawaan).
4. `volumatic-bot positions --all-modes` untuk memastikan tidak ada catatan
   posisi menggantung, lalu `kill-switch --clear` sebelum start.

### 10e. Yang belum diuji setelah perbaikan ini

- Pemanggilan `/fapi/v1/algoOrder` yang **berhasil** ke Binance asli/testnet
  belum pernah terjadi dari mesin ini (sandbox 451). Semua uji memakai server
  tiruan dan REST palsu yang mengikuti dokumentasi di §10b.
- Perilaku `closePosition=true` pada endpoint algo untuk simbol dengan qty
  besar (seperti kasus CTSIUSDT) belum terbukti — dokumentasi menyatakan
  didukung untuk `STOP_MARKET`/`TAKE_PROFIT_MARKET`.
- Apakah `ORDER_TRADE_UPDATE` untuk algo order menyertakan `algoId` atau
  `orderId` hasil trigger: **belum diketahui**. Karena itu pembatalan mencoba
  dua endpoint, bukan menebak.
