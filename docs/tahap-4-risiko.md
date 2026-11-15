# TAHAP 4 — Manajemen Risiko, Ukuran Posisi & Batas Harian

Laporan ini menjelaskan apa yang **sudah** dikerjakan, apa yang **sudah diuji**,
dan apa yang **belum** bisa diuji. Tidak ada angka performa di sini: modul ini
hanya menentukan **boleh/tidak** dan **sebesar apa** satu posisi dibuka.

- Versi paket saat pengujian: Python **3.14.10** (juga 3.11.18, 3.12.16,
  3.13.18), pydantic 2.14.3, pytest 9.2.0, ruff 0.17.5, mypy 2.4.1.
- TAHAP 4 **belum mengirim order apa pun**. Semua perhitungan murni di memori.

---

## 1. Ruang lingkup

| Dikerjakan | Tidak dikerjakan (tahap lain) |
|---|---|
| Ukuran posisi dari risiko + jarak SL, pembulatan aman | Kirim order, pasang SL/TP (TAHAP 5) |
| Rencana level SL/TP1/**BE sadar biaya**/trailing/partial TP | Manajemen posisi berjalan (TAHAP 5) |
| Batas harian: rugi, jumlah trade, rugi beruntun, cooldown, kill switch | Persistensi state harian ke SQLite (TAHAP 5) |
| Portofolio: margin terpakai, margin bebas, konsentrasi per simbol | Rekonsiliasi saldo/posisi dengan exchange (TAHAP 5) |
| CLI `preflight` (tanpa jaringan) & `sizing` (dengan data simbol asli) | Dashboard (TAHAP 6), backtest (TAHAP 7) |

---

## 2. Modul baru

```
src/volumatic_bot/risk/
  sizing.py     # size_position(), estimate_liquidation() → PositionSize
  plan.py       # build_trade_plan() → TradePlan (SL/TP1/BE/trailing/partial)
  limits.py     # DailyLimits, DailyState, TradeOutcome → LimitDecision
  portfolio.py  # Portfolio, PositionExposure (margin & konsentrasi)
  manager.py    # RiskManager.evaluate() → TradeTicket (pintu tunggal)
src/volumatic_bot/cli.py   # + `preflight`, `sizing`
```

---

## 3. Cara ukuran posisi dihitung

```
risiko_target = equity x risk_per_trade_pct            (default 1,5% equity)
qty_ideal     = risiko_target / |entry - SL|
qty           = bulatkan KE BAWAH ke kelipatan stepSize
```

Lalu **empat pagar** diperiksa. Trade ditolak bila salah satu dilanggar:

| Pagar | Aturan | Alasan penolakan |
|---|---|---|
| Ukuran minimum sah | bila `qty < max(minQty, minNotional/harga)`, ukuran boleh naik **hanya** jika kenaikannya ≤ `max_qty_round_up_pct` (default **2%**) **dan** risiko akhir ≤ `max_risk_overshoot_factor` (default **2x**) target | `pembulatan stepSize/minNotional memaksa ukuran naik 80.0% (batas 2.0%) — trade dilewati, bukan dibesarkan` |
| Min notional | `qty x entry >= minNotional` | `notional 4.1212 USDT < minNotional 5 USDT` |
| Margin | `notional/leverage` ≤ margin bebas **dan** ≤ batas konsentrasi per simbol (`max_per_symbol_margin_pct`, default 30%) | `margin 36.00 USDT > margin tersedia 30.00 USDT` |
| Likuidasi | jarak SL < jarak likuidasi perkiraan; risiko | `SL 10.00% lebih jauh dari perkiraan likuidasi 5.20%` |
| Biaya | `fee_bolak_balik / risiko` < `trade.max_fee_r` (default 0,5R) | `biaya bolak-balik 0.62R terlalu besar dibanding risiko` |

Catatan penting soal pagar pertama: config membatasi `max_qty_round_up_pct`
maksimal **20%**, sehingga dalam praktik pagar inilah yang lebih dulu aktif —
artinya bot **lebih konservatif** daripada aturan minimum "risiko ≤ 2x" yang
Anda tetapkan. Pagar 2x tetap ada sebagai jaring pengaman terakhir. Konsekuensi
jujur: pada modal sangat kecil, banyak trade akan **dilewati** karena ukuran
minimum bursa memaksa risiko membengkak. Itu memang perilaku yang diminta
("SKIP, jangan besarkan diam-diam").

Perkiraan likuidasi memakai pendekatan isolated margin satu tier:

```
long : liq ≈ entry x (1 - 1/leverage + MMR)
short: liq ≈ entry x (1 + 1/leverage - MMR)      MMR default 0,4%
```

Ini **bukan** angka resmi Binance (mengabaikan biaya, funding, tier MMR
berjenjang), jadi dipakai sebagai pagar konservatif, bukan patokan.

---

## 4. Rencana level (SL/TP/BE/trailing)

| Level | Rumus | Pembulatan |
|---|---|---|
| SL | `sl_mode`: `atr` (`sl_atr_mult` x ATR), `vidya_trail` (garis VIDYA), atau `nearest` (terdekat dari keduanya); dilebarkan ke `sl_min_atr_mult` x ATR bila terlalu sempit | **menjauhi** entry (long ke bawah, short ke atas) → jarak SL tidak pernah menyusut |
| TP1 | `entry ± tp1_r x R` | ke tick terdekat |
| **BE** | `entry ± (2 x (taker_bps + slippage_bps) + be_buffer_pct)%` | **menjauhi** entry → BE dijamin menutup biaya |
| Trigger BE | `entry ± be_trigger_r x R` | tick terdekat |
| Aktivasi trailing | `entry ± trailing_activation_r x R` | tick terdekat |
| Partial TP | `partial_tp_pct%` x qty, dibulatkan ke bawah; dilewati bila hasil 0 | stepSize |

Contoh angka nyata (dari perintah `sizing`, biaya default taker 5 bps +
slippage 2 bps): biaya bolak-balik **0,14%** harga; BE digeser **0,17%** dari
entry (0,14% + buffer 0,03%). Artinya posisi yang ditutup di BE benar-benar
impas setelah fee dan slippage, bukan sekadar "harga sama dengan entry".

---

## 5. Batas harian & kill switch

Diperiksa **sebelum** ukuran posisi dihitung:

| Batas | Default | Perilaku |
|---|---|---|
| Rugi harian | 3% equity | `daily_stopped = true` → tolak posisi baru **sampai hari trading berganti** |
| Jumlah trade/hari | 20 | tolak posisi baru |
| Rugi beruntun | 3 | cooldown 120 menit |
| Slot posisi | 2 | tolak posisi baru |
| Duplikat simbol | — | tolak (maks 1 posisi per simbol) |
| Kill switch file | `KILL_SWITCH` | tolak posisi baru selama file ada |
| Kill switch manual | `daily.kill(reason)` | **tidak** hilang saat hari berganti; hanya `reset_kill()` |

Jam "hari trading" mengikuti `app.timezone` + `risk.daily_loss_reset_hour_local`
(mis. reset pukul 07:00 lokal). Perhitungan memakai `datetime` sadar zona.

**Penutupan posisi tidak pernah diblokir** oleh modul ini — memblokir exit
justru berbahaya. `close_all_on_daily_limit: true` disiapkan untuk TAHAP 5
(menutup semua posisi saat batas rugi harian kena).

---

## 6. Perintah CLI baru

```bash
# tanpa jaringan: periksa kesiapan mesin & keamanan config
volumatic-bot preflight --equity 40

# dengan data simbol asli: hitung ukuran + level untuk satu simbol
volumatic-bot sizing DOGEUSDT --equity 50 --entry 0.35 --atr-pct 2 --trail 0.345
volumatic-bot sizing BTCUSDT --equity 100 --entry 90000 --atr-pct 1 --side short
```

Keluaran nyata `preflight --equity 40` (offline, config default):

```
Pemeriksaan awal (tanpa jaringan)
--------------------------------------------------------------
[OK ] mode operasi       mode=dry_run
[OK ] konfirmasi live    string konfirmasi live sesuai/`mode` bukan live
[OK ] kill switch        file KILL_SWITCH tidak ada
[OK ] leverage           default 5x, maks 8x
[OK ] risiko per trade   1.5% equity
[OK ] batas harian       rugi harian 3.0% | maks 20 trade | cooldown 120 menit
[OK ] margin             maks 60.0% equity, konsentrasi 30.0%/simbol
[OK ] equity             40.00 USDT (minimum 20.00) — kategori modal kecil
[OK ] kredensial API     belum diset (tidak wajib untuk mode dry_run)
[OK ] folder data        var bisa ditulis
[OK ] order book         aktif (sumber rest, tolak bila depth hilang=True)
[OK ] fingerprint config a16ad55067f2
--------------------------------------------------------------
Semua pemeriksaan lolos. Catatan: ini BUKAN jaminan profit.
```

Keluaran nyata `sizing` terhadap **server HTTP lokal** (bukan Binance asli):

```
== DOGEUSDT, equity 50 USDT ==
DISETUJUI DOGEUSDT: qty=133 notional=46.55 USDT margin=9.31 @5x
risiko=0.745 USDT (1.49% equity) SL=1.60% biaya=0.065 USDT (0.09R) liq~19.60%
| BELI entry=0.35 SL=0.344400 (vidya_trail+min_atr, 1.60%) TP1=0.358400
  BE=0.350600 trail_aktif=0.355600
  - SL vidya_trail hanya 0.005 (< 0.8x ATR = 0.0056); dilebarkan agar tidak kena noise
  - biaya bolak-balik 0.1400% harga; BE digeser 0.1700% supaya benar-benar impas
Level      : SL=0.344400 (vidya_trail+min_atr) TP1=0.358400 BE=0.350600
Partial TP : 66 / sisa 67 | R = 0.005600 per unit

== BTCUSDT, equity 100 USDT ==
DITOLAK BTCUSDT: pembulatan stepSize/minNotional memaksa ukuran naik 80.0%
(batas 2.0%) — trade dilewati, bukan dibesarkan; margin 36.00 USDT > margin
tersedia 30.00 USDT
```

Contoh kedua adalah kenyataan modal kecil: dengan minNotional 100 USDT dan
stepSize 0,001 pada harga ~90.000, ukuran minimum yang sah (0,002 BTC ≈ 180 USDT
notional) sudah 2x lipat risiko target. Bot **melewatinya**, tidak membesarkan
posisi. Simbol seperti DOGEUSDT/ETHUSDT jauh lebih cocok untuk modal <50 USD.

---

## 7. Hasil pengujian

```
pytest -q                    -> 355 passed (~44 detik)
3.11.18 / 3.12.16 / 3.13.18  -> 355 passed (masing-masing)
3.14.10                      -> 355 passed
ruff check + ruff format     -> bersih (49 file sumber + 18 file test)
mypy src (strict)            -> bersih
```

96 test baru (259 → 355):

| File | Isi |
|---|---|
| `tests/test_risk_sizing.py` (23) | ukuran dasar = risiko target, pembulatan ke bawah, pembulatan naik dengan/di-out pagar, minNotional, SL di sisi salah, leverage/equity/risk tidak valid, perkiraan likuidasi (long/short), SL lebih jauh dari likuidasi, biaya dalam R, peringatan modal kecil, margin tersedia, maxQty |
| `tests/test_risk_plan.py` (22) | SL mode atr/vidya/nearest, pembulatan SL menjauhi entry (tick 0,3), pelebaran ke `sl_min_atr_mult`, trail di sisi salah diabaikan, TP1 & `target_price`, BE di atas/bawah entry + naik saat fee naik, trigger BE < TP1, trailing, partial TP (normal, dimatikan, dilewati bila qty kecil), validasi ATR/qty |
| `tests/test_risk_limits.py` (21) | hari trading dengan jam reset 0 & 7, roll harian, rugi harian hanya menghitung loss, batas trade/hari, cooldown 3 rugi beruntun, profit mereset beruntun, kill manual **tidak** hilang saat hari berganti, `daily_stopped` hilang saat hari berganti, portofolio (margin terpakai/bebas/konsentrasi, equity 0, equity negatif) |
| `tests/test_risk_manager.py` (19) | tiket lengkap (qty, level, alasan, `as_row`), kill switch file & internal, duplikat simbol, slot penuh, batas harian, cooldown, leverage 20x, equity minimum, simbol bukan perpetual USDT, margin portofolio habis, side invalid, ATR nol, penolakan tidak menyisakan tiket setengah jadi, BTCUSDT ditolak untuk 100 USDT, ETHUSDT menyentuh batas konsentrasi |
| `tests/test_cli_risk.py` (11) | `preflight` (lolos, kill switch ada, mode live tanpa flag, equity di bawah minimum), `sizing` (tampil lengkap, BTCUSDT ditolak, simbol tidak ada, short), rantai **sinyal → order book → risiko** berhasil & berhenti saat order book menolak |

### Tiga bug yang ditemukan & diperbaiki di tahap ini

1. **`minNotional` tidak pernah benar-benar ditegakkan.** Kode membulatkan
   `minNotional/harga` dengan `ROUND_UP` ke **bilangan bulat** (bukan kelipatan
   `stepSize`), sehingga untuk harga 100 dihasilkan 0,001 unit — jauh di bawah
   minimum. Akibatnya trade bisa lolos lalu ditolak bursa di TAHAP 5. Sekarang
   memakai `round_qty(..., mode="up")` dan ada test khusus (`min_notional` +
   `BTCUSDT ditolak`).
2. **Kill switch manual hilang sendiri.** `roll()` (awal hari baru) mereset
   `killed`. Sekarang ada dua konsep terpisah: `daily_stopped` (otomatis, hilang
   saat hari berganti) dan `killed` (manual, hanya `reset_kill()`).
3. **`roll()` tidak mereset `daily_stopped`** sehingga bot bisa berhenti
   selamanya setelah kena batas harian. Ditemukan oleh test, bukan saat
   menjalankan bot.

---

## 8. Yang **belum** diuji / keterbatasan (baca ini)

1. **Tidak ada koneksi nyata ke Binance** (`451` dari sandbox). Filter simbol
   yang dipakai di test berasal dari `tests/data/exchange_info_sample.json`
   (salinan bentuk respons), bukan exchangeInfo live pada hari Anda menjalankan.
   `minNotional`, `stepSize`, dan `tickSize` bisa berbeda di kemudian hari.
2. **Perkiraan likuidasi kasar.** Rumus satu tier MMR, tanpa biaya/funding/tier
   berjenjang. Binance memakai perhitungan yang lebih rumit; harga likuidasi
   nyata bisa lebih dekat. Selalu anggap ini batas **optimistis**.
3. **Batas harian dihitung dari catatan bot sendiri** (PnL trade yang ia tutup),
   belum dari saldo exchange. Rugi akibat trade manual/posisi di luar bot
   **tidak** terhitung sampai rekonsiliasi TAHAP 5.
4. **State harian masih di memori.** Restart bot (atau crash) menghapus catatan
   rugi harian, cooldown, dan hitungan trade. Ini **lubang keamanan** yang harus
   ditutup di TAHAP 5 dengan menyimpannya ke SQLite.
5. **Funding rate belum masuk perhitungan ukuran maupun BE.** `funding_aware_bars`
   sudah ada di config tetapi belum dipakai; posisi yang ditahan lama bisa
   kehilangan lebih dari yang diperkirakan. Akan dipakai di TAHAP 5/7.
6. **Biaya diasumsikan taker dua sisi** (0,05% + slippage 0,02% per sisi).
   `commission.use_account_commission: true` belum benar-benar membaca tarif
   akun (butuh endpoint akun, TAHAP 5). Bila akun Anda punya diskon maker/BNB,
   angkanya akan lebih kecil.
7. **Korelasi antar simbol tidak dihitung.** Bot boleh membuka 2 posisi di
   BTCUSDT dan ETHUSDT yang bergerak bersama → risiko gabungan > 2 x 1,5%.
   Batas margin dan slot mengurangi, bukan menghilangkan, masalah ini.
8. **Tidak ada model likuiditas saat SL kena.** SL market bisa slip lebih jauh
   dari jarak SL (gap, stop hunt). Estimasi fill order book TAHAP 3 belum
   disambungkan ke sini.
9. **Partial TP & stepSize kasar**: 50% dari 133 unit = 66 (dibulatkan ke bawah),
   sisa 67. Pada simbol dengan `stepSize` besar (mis. 1), partial TP bisa
   dilewati sepenuhnya (sudah ditangani dengan catatan, bukan error).
10. **Belum ada uji dengan data pasar nyata** untuk memvalidasi bahwa jarak SL
    (`sl_min_atr_mult`) tidak terlalu sempit/lebar di timeframe 15m.

---

## 9. Risiko untuk TAHAP 5 (eksekusi)

- Rekonsiliasi wajib: saldo, posisi, dan order proteksi harus dibaca dari
  exchange saat start; bila SL tidak ada → tutup posisi market dan catat insiden.
- Kill switch & batas harian harus **dipersistensi** ke SQLite agar restart
  tidak mereset rugi harian.
- Order proteksi: SL/TP reduce-only atau `closePosition`, dipasang **segera**
  setelah entry terisi; bila gagal → tutup market + insiden.
- `TradeTicket` adalah kontrak: TAHAP 5 **hanya** boleh mengirim order dari
  tiket ber-`approved=True`, dengan `qty`/`stop`/`tp1` persis dari tiket.
- Angka likuidasi resmi (dari respons order) harus menggantikan estimasi di sini
  begitu tersedia.
