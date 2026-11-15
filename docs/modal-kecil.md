# Menjalankan Bot dengan Modal Kecil (contoh: 7 USDT)

Dokumen ini menjawab satu pertanyaan: **dengan modal 7 USDT, bisakah bot ini
live, dan bisakah risikonya dibuat "all in / 100%" atau lebih fleksibel?**

Jawaban singkatnya:

1. **Secara mekanis bisa** — minimum order Binance USDT-M Futures di simbol
   likuid adalah `minNotional = 5 USDT`, dan dengan leverage 10x margin yang
   dibutuhkan hanya **0,50 USDT**. Jadi 7 USDT cukup untuk membuka posisi.
2. **Dengan setelan bawaan, hasilnya 0 trade**, bukan karena kurang uang,
   tetapi karena pagar `risk.min_equity_usdt: 20.0` dan `max_per_symbol_margin_pct: 30`.
3. **"All in 100%" tidak tersedia dan tidak akan ditambahkan.** Plafon keras di
   kode adalah **25% risiko per trade**. Alasannya murni aritmetika (lihat §2).

> Semua angka di bawah diambil dari `testnet.binancefuture.com/fapi/v1/exchangeInfo`
> pada **15 November 2026** (744 simbol) dan dari pemanggilan
> `RiskManager.evaluate` sungguhan di repo ini. **Tidak ada janji profit**:
> hasil backtest repo ini masih negatif setelah biaya
> (`docs/tahap-7-mesin-backtest.md`). Modal 7 USDT memperbesar porsi biaya dan
> risiko likuidasi, bukan memperbesar peluang.

---

## 1. Kenapa setelan bawaan memberi 0 trade

| Pemeriksaan | Nilai bawaan | Efek pada equity 7 USDT |
|---|---|---|
| `min_equity_usdt` | 20,0 | **langsung ditolak** — bot menolak membuka posisi apa pun |
| `max_per_symbol_margin_pct` | 30% | margin per trade hanya **2,10 USDT** |
| `risk_per_trade_pct` | 1,5% | risiko **0,105 USDT** per trade |
| `max_concurrent_positions` | 2 | slot kedua praktis tidak terpakai |

Perintah `sizing` mengonfirmasi hal ini:

```
$ volumatic-bot sizing BCHUSDT --equity 7
DITOLAK: equity 7.00 USDT < minimum 20.00 USDT
```

Jadi bila Anda hanya mengganti API key tanpa menyentuh `config.yaml`, bot akan
**diam** — dan itu perilaku yang disengaja (gagal aman).

---

## 2. Kenapa 100% ("all in") tidak ada

Ukuran posisi tidak dihitung dari "berapa persen modal yang mau dipakai",
tetapi dari **jarak stop loss**:

```
qty = (equity × risk_pct) / jarak_SL
notional = qty × harga_entry
margin = notional / leverage
```

Akibatnya:

| `risk_pct` | jarak SL | notional | margin (lev 10x) | margin dari equity 7 |
|---|---|---|---|---|
| 1,5% | 2% | 5,25 USDT | 0,53 | 7,5% |
| 5% | 2% | 17,5 USDT | 1,75 | 25% |
| 25% (plafon) | 2% | 87,5 USDT | 8,75 | **125% — ditolak** |
| 100% | 2% | 350 USDT | 35 | **500% — ditolak** |

Artinya risiko 100% bukan "memakai seluruh modal", melainkan **menuntut
notional 50x equity**. Order itu ditolak pagar margin, sehingga hasilnya **bukan
all-in, tetapi tidak ada trade sama sekali**. Kalaupun bisa, satu stop loss
kena = modal habis dalam satu transaksi.

Karena itu:

- `risk.risk_per_trade_pct` dibatasi `<= 25`, dan di atas 2% wajib melewati
  saklar `allow_aggressive_risk: true`;
- angka 100 akan ditolak pydantic (`less than or equal to 25`) sebelum bot
  menyentuh jaringan.

**Untuk "memakai modal lebih penuh", yang perlu dinaikkan adalah batas margin,
bukan risiko.** Itulah gunanya profil fleksibel di §3.

---

## 3. Profil "fleksibel" untuk modal 7–50 USDT

### 3a. Cara cepat: profil `modal-kecil` di dalam `config.yaml`

**Tidak ada file config kedua.** Semua setelan tetap di `config.yaml`; bagian
`profil.modal-kecil` hanya *menimpa* nilai biasa bila dipilih:

```yaml
profil_aktif: ""          # "" = pakai nilai biasa; bisa diganti "modal-kecil"

profil:
  modal-kecil:
    exchange:
      default_leverage: 10
      max_leverage: 10
    risk:
      risk_per_trade_pct: 5.0
      allow_aggressive_risk: true
      max_concurrent_positions: 1
      max_total_margin_pct: 80.0
      max_per_symbol_margin_pct: 100.0
      min_equity_usdt: 5.0
      daily_loss_limit_pct: 10.0
      max_trades_per_day: 8
    scanner:
      max_min_notional_usdt: 5.0
```

Tiga cara memakainya:

```bash
volumatic-bot profil                            # lihat profil & nilai yang ditimpa
volumatic-bot --profil modal-kecil check-config  # sekali jalan, file tidak diubah
volumatic-bot --profil modal-kecil scan --equity 7
volumatic-bot --profil modal-kecil run
```

Atau permanen: ubah satu baris `profil_aktif: "modal-kecil"` di config, lalu
semua perintah memakainya tanpa flag.

Beda nilai terhadap setelan biasa:

| kunci | biasa | profil `modal-kecil` | alasan |
|---|---|---|---|
| `exchange.default_leverage` / `max_leverage` | 5 / 8 | 10 / 10 | batas keras yang Anda izinkan |
| `risk.risk_per_trade_pct` | 1,5 | 5,0 | 1,5% di $7 hanya risiko 10 sen |
| `risk.allow_aggressive_risk` | false | **true** | wajib, kalau tidak config ditolak |
| `risk.max_per_symbol_margin_pct` | 30 | 100 | satu posisi boleh memakai margin yang ada |
| `risk.max_total_margin_pct` | 60 | 80 | sisakan 20% untuk fee & funding |
| `risk.max_concurrent_positions` | 2 | 1 | modal kecil: satu posisi |
| `risk.min_equity_usdt` | 20 | 5 | di bawah 5 USDT tidak bisa menahan SL |
| `risk.daily_loss_limit_pct` | 3 | 10 | 10% dari $7 = 0,70 USDT |
| `risk.max_trades_per_day` | 20 | 8 | fee taker jadi dominan |
| `scanner.max_min_notional_usdt` | 0 (nonaktif) | 5,0 | BTC/ETH otomatis tersaring |

Pagar profil (diuji di `tests/test_config_profil.py`):

- salah ketik nama profil → pesan error menyebutkan pilihan yang ada;
- salah ketik nama kunci **di dalam** profil tetap ditolak (`extra="forbid"`);
- profil **tidak bisa** membuka mode live — pagar `allow_live` +
  `live_confirmation` tetap berlaku;
- profil tetap tunduk pada plafon 25%: `risk_per_trade_pct: 100` di dalam profil
  tetap ditolak;
- profil agresif tanpa `allow_aggressive_risk: true` ditolak, jadi saklarnya
  benar-benar bisa dicabut.

### 3b. Penyaring simbol: `max_min_notional_usdt`

Ini bagian paling berguna untuk modal kecil. Simbol dengan minimum order mahal
(BTC 50 USDT, ETH 20 USDT) membuat satu SL 2% langsung memakan 14% / 5,7%
equity — jauh di atas target. Alih-alih menghafal daftar simbol, batasi saja:

```yaml
scanner:
  max_min_notional_usdt: 5.0     # 0 = nonaktif
```

Atau untuk sekali jalan, tanpa mengubah config:

```bash
volumatic-bot scan --equity 7 --max-min-notional 5
```

Hasil nyata `scan --equity 7` dengan preset (testnet, 15 November 2026): **30
simbol diterima**, semuanya `minNotional 5 USDT` dan `minMargin 0,50 USDT`;
ETHUSDT ditolak dengan alasan `minNotional 20.00 USDT > batas 5.00 USDT (modal
kecil)`. Rekap penolakan: `atr=1, blacklist=1, harga=1, kuota=54,
min_notional=1, spread=374, volume=67`.

> Perhatikan `spread=374`: di testnet spread memang lebar, jadi sebagian besar
> simbol gugur di situ — bukan karena modal Anda. Di live angkanya jauh lebih
> longgar, tetapi **verifikasi sendiri** sebelum menyimpulkan apa pun.

### 3c. Kalau ingin menyetel manual

Tempel di `config.yaml` (ganti bagian `risk:` dan `exchange:` yang sudah ada):

```yaml
risk:
  risk_per_trade_pct: 5.0            # 4–5 realistis; >5 cepat menghabiskan 7 USDT
  allow_aggressive_risk: true        # WAJIB: tanpa ini config ditolak saat dimuat
  max_per_symbol_margin_pct: 100.0   # satu posisi boleh memakai semua margin yang ada
  max_total_margin_pct: 80.0         # sisakan 20% untuk fee & funding
  max_concurrent_positions: 1        # modal kecil: satu posisi saja
  min_equity_usdt: 5.0               # di bawah 5 USDT benar-benar tidak bisa menahan SL
  daily_loss_limit_pct: 10.0         # sadar: 10% dari 7 USDT = 0,70 USDT
  daily_loss_reset_hour_local: 0
  consecutive_loss_limit: 3
  cooldown_minutes: 120
  max_trades_per_day: 8              # biaya taker jadi dominan bila terlalu sering
  min_liq_distance_pct: 1.0
  close_all_on_daily_limit: true

exchange:
  default_leverage: 10               # pagar Anda: maksimum 10x
  max_leverage: 10                   # wajib ikut naik, kalau tidak config ditolak
```

Setelah disimpan, periksa dulu tanpa menyentuh bursa:

```bash
volumatic-bot check-config          # harus lolos tanpa "KONFIGURASI GAGAL"
volumatic-bot sizing DOGEUSDT --equity 7
```

### Hasil nyata profil tersebut (equity 7 USDT, leverage 10x, SL ~2%)

`risk_per_trade_pct: 5`, `max_per_symbol_margin_pct: 100`, `max_total_margin_pct: 80`,
diukur lewat `RiskManager.evaluate` dengan `exchangeInfo` asli:

| simbol | disetujui | notional | margin | risiko USDT | risiko nyata | R |
|---|---|---|---|---|---|---|
| BCHUSDT | ya | 38,18 | 3,82 | 0,350 | 5,00% | 0,50 |
| XRPUSDT | ya | 38,13 | 3,81 | 0,349 | 4,99% | 0,50 |
| DOGEUSDT | ya | 35,54 | 3,55 | 0,350 | 5,00% | 0,50 |
| ZROUSDT | ya | 20,12 | 2,01 | 0,348 | 4,97% | 0,50 |
| SHELLUSDT | ya | 9,46 | 0,95 | 0,349 | 4,99% | 0,50 |

Dengan `risk_per_trade_pct: 15` (lebih agresif), sebagian besar **ditolak**
karena margin tidak cukup — bukan karena pagar risiko:

| simbol | notional | margin | catatan |
|---|---|---|---|
| BCHUSDT | 114,53 | 11,45 | ditolak: margin 11,45 > tersedia 5,60 USDT |
| XRPUSDT | 114,59 | 11,46 | ditolak: margin 11,46 > tersedia 5,60 USDT |
| DOGEUSDT | 106,63 | 10,66 | ditolak: margin 10,66 > tersedia 5,60 USDT |
| SHELLUSDT | 28,45 | 2,85 | **diterima** (harga kecil → notional kecil) |

Pelajaran: di modal kecil, yang membatasi hampir selalu **margin**, bukan angka
persen risiko. Menaikkan risiko ke 15% justru membuat bot menolak lebih banyak
simbol.

---

## 4. Pilih simbol: hindari yang `minNotional`-nya besar

Ini kendala yang paling sering menjatuhkan modal kecil. Minimum order sebuah
simbol = `max(minNotional, minQty × harga)`. Bila minimum itu sudah membuat
risiko melebihi target, bot **melewati** trade (tidak membesarkannya diam-diam).

Data testnet 15 November 2026, asumsi SL 2%:

| simbol | harga | minQty | minNotional | notional minimum | risiko bila SL 2% | dari equity 7 |
|---|---|---|---|---|---|---|
| XRPUSDT | 0,9625 | 0,1 | 5,00 | 5,00 | 0,10 USDT | 1,4% |
| DOGEUSDT | 0,05799 | 1 | 5,00 | 5,00 | 0,10 USDT | 1,4% |
| SHELLUSDT | 0,02639 | 1 | 5,00 | 5,00 | 0,10 USDT | 1,4% |
| BCHUSDT | 204,69 | 0,001 | 5,00 | 5,00 | 0,10 USDT | 1,4% |
| BNBUSDT | 592,29 | 0,01 | 5,00 | 5,92 | 0,12 USDT | 1,7% |
| **ETHUSDT** | 1.793,9 | 0,001 | 20,00 | 20,00 | 0,40 USDT | **5,7%** |
| **BTCUSDT** | 64.484 | 0,0001 | **50,00** | 50,00 | 1,00 USDT | **14,3%** |

Uji `simulate-session` dengan data BTCUSDT 15m 5 bulan dan equity 7
membuktikannya: **153 sinyal, 0 entry**, semuanya ditolak dengan alasan
pembulatan `stepSize`/`minNotional` yang memaksa risiko ~250% dari target.
BTC/ETH baru masuk akal di equity ratusan USDT.

Saran praktis: pakai `market.whitelist` berisi simbol ber-`minNotional` kecil
(mis. `XRPUSDT`, `DOGEUSDT`, `SHELLUSDT`, `BCHUSDT`) selama modal masih
di bawah ~50 USDT, dan biarkan `scan` yang menyaring sisanya.

---

## 5. Biaya dan likuidasi — bagian yang paling tidak ramah modal kecil

Angka di bawah memakai `commission.taker_bps: 5` (0,05%) bawaan config.

| notional | fee taker masuk+keluar | dibanding risiko 0,35 USDT |
|---|---|---|
| 5 USDT | 0,005 USDT | 1,4% dari risiko |
| 38 USDT | 0,038 USDT | 11% dari risiko |
| 100 USDT | 0,100 USDT | 29% dari risiko (risiko 0,35) |

- Pada posisi minimum (5 USDT) biaya memang kecil, tetapi **TP harus lebih
  lebar dari spread + 2× fee** supaya trade tidak "menang di harga, kalah di
  biaya". Filter `max_fee_r` sudah menolak trade yang biayanya terlalu besar
  relatif terhadap R.
- Funding (tiap 8 jam) tetap dipotong dari margin; posisi yang ditahan berhari-hari
  bisa habis dimakan funding.
- Leverage 10x: jarak likuidasi kira-kira `1/10 − MMR (0,4%)` ≈ **9,6%** dari
  harga masuk. SL 2–3% masih lolos `min_liq_distance_pct: 1.0`, tetapi bila
  pasar melompat (gap/slippage) SL bisa terlewati dan sisa margin 7 USDT habis.
  Ini risiko nyata, bukan teoretis.

---

## 6. Prosedur aman sebelum uang sungguhan

1. `mode: dry_run` dulu seminggu (preset sudah `dry_run`); lihat log
   penolakan, bukan hanya entry.
2. `volumatic-bot simulate-session ...` pada simbol pilihan Anda sendiri
   (data BTC di repo hanya contoh).
3. `mode: testnet` dengan kunci testnet; pastikan `net.legacy_testnet_rest`
   cocok dengan host yang dipakai (`testnet.binancefuture.com` legacy).
4. Baru `mode: live`, dengan modal yang **siap hilang**. `daily_loss_limit_pct: 10`
   berarti hari terburuk yang diizinkan = −0,70 USDT pada modal 7.
5. Jangan menaikkan leverage di atas 10x dan jangan menaikkan risiko sebagai
   kompensasi drawdown — dua hal itu mempercepat likuidasi, bukan memperbaikinya.

---

## 7. Yang sudah diuji dan yang belum

**Sudah diuji di mesin (Python 3.13.18):**

- validasi config: 100% ditolak, 5% tanpa saklar ditolak, 5% dengan saklar
  diterima, margin total 95% tanpa saklar ditolak, equity minimum < 20 tanpa
  saklar ditolak → `tests/test_risk_agresif.py`;
- `size_position` menerima 4–25% **hanya** bila `allow_aggressive_risk: true`,
  dan menolak di atas 25%;
- `RiskManager.evaluate` pada equity 7 **tidak lagi crash** saat ukuran
  menjadi 0 (sebelumnya `ValueError("qty harus > 0")` di `build_trade_plan`);
- tabel §3 dan §4 diukur dengan `exchangeInfo` asli dan harga testnet;
- `simulate-session` BTCUSDT 15m 5 bulan pada equity 7 → 0 entry, 153 ditolak
  risiko (bukti bahwa penolakan bekerja, bukan bukti strategi bagus);
- profil `modal-kecil` di dalam `config.yaml` dimuat tanpa error, `profil_aktif`
  bawaan kosong (mode tetap `dry_run`), nilai yang tidak disebut profil tidak
  berubah, salah ketik nama/kunci ditolak, profil tidak bisa membuka mode live,
  dan plafon 25% tetap berlaku → `tests/test_config_profil.py` (11 test);
- penyaring `max_min_notional_usdt` membuang BTC/ETH/BIG dan hanya menyisakan
  simbol 5 USDT, termasuk perilaku `0 = nonaktif` →
  `tests/test_scanner.py::test_filter_min_notional_untuk_modal_kecil`;
- `scan --equity 7` dengan preset ke testnet: 30 simbol diterima, semua
  `minMargin 0,50 USDT`.

**Belum diuji / tidak bisa diuji dari sini:**

- `run` end-to-end testnet dengan API key asli (sandbox tidak punya kunci dan
  `fapi.binance.com` menolak dengan 451);
- perilaku lapangan pada spread tipis dini hari, funding nyata, dan partial fill
  saat notional hanya 5 USDT;
- klaim profit apa pun — **tidak ada** dan tidak akan dibuat.

---

## 8. Ringkasan satu tabel

| Pertanyaan | Jawaban |
|---|---|
| $7 bisa live? | Bisa secara mekanis (min 0,50 USDT margin), **tetapi** wajib ubah `min_equity_usdt`, margin %, dan `default_leverage` dulu |
| Risk 100% / all-in? | **Tidak didukung.** Plafon 25%; 100% → notional 50x equity → semua entry ditolak |
| Cara memakai modal lebih penuh? | Jalankan dengan `--profil modal-kecil` (satu file config, tanpa file kedua), atau set permanen `profil_aktif: "modal-kecil"`. Isinya: `max_per_symbol_margin_pct` 100, `max_total_margin_pct` 80, `max_concurrent_positions` 1, `risk_per_trade_pct` 5, `allow_aggressive_risk: true` |
| Simbol apa? | Yang `minNotional` 5 USDT dan harga satuan kecil; **hindari BTC/ETH** di modal 7 — `scanner.max_min_notional_usdt: 5` menyaringnya otomatis |
| Risiko terbesar? | Slippage/likuidasi pada 10x dan fee/funding yang memakan porsi besar dari risiko 0,35 USDT |

---

## 9. Kalau cuma ada 5 USDT (minimum Binance)

Jawaban jujurnya: **bisa, tetapi 5 USDT adalah lantai, bukan modal yang nyaman.**
Ada satu jebakan yang harus Anda tahu lebih dulu.

### 9a. Jebakan: profil `modal-kecil` berhenti setelah SATU kerugian

Pada equity tepat **5,00 USDT** dengan `--profil modal-kecil` (risiko 5%),
posisi masih disetujui:

| simbol | notional | margin | risiko | % equity | fee bolak-balik | fee/R |
|---|---|---|---|---|---|---|
| BCHUSDT | 27,12 | 2,71 | 0,249 | 4,97% | 0,0271 | 0,11 R |
| XRPUSDT | 27,16 | 2,72 | 0,250 | 5,00% | 0,0272 | 0,11 R |
| DOGEUSDT | 25,36 | 2,54 | 0,250 | 4,99% | 0,0254 | 0,10 R |
| HOMEUSDT | 18,27 | 1,83 | 0,250 | 5,00% | 0,0183 | 0,07 R |

Tetapi `min_equity_usdt` profil itu **5,00**, jadi begitu satu stop loss kena
(−0,25 USDT → equity 4,75):

```
DITOLAK: equity 4.75 USDT < minimum 5.00 USDT — modal tambah, atau turunkan
risk.min_equity_usdt (wajib allow_aggressive_risk: true) bila memang ingin
lanjut; lihat docs/modal-kecil.md §9
```

Bot **tetap berjalan dan tetap mengelola posisi yang masih terbuka** (SL, TP,
trailing), ia hanya berhenti **membuka posisi baru**. Itu pagar yang disengaja,
bukan bug — tetapi pada modal 5 USDT artinya praktis "sekali kalah, selesai".

### 9b. Jalan keluar: profil `modal-5usdt` (sudah ada di `config.yaml`)

```bash
volumatic-bot --profil modal-5usdt check-config
volumatic-bot --profil modal-5usdt run
```

Isi profil (hanya menimpa yang perlu): risiko **3%**, lantai equity **2 USDT**,
rugi harian **6%** (0,30 USDT), **4 trade/hari**, 2 rugi beruntun → cooldown,
1 posisi, margin 100%/80%, leverage 10x, `max_min_notional_usdt 5`.

Terukur dengan `exchangeInfo` testnet (16 Nov 2026):

| equity | risiko target | BCHUSDT | XRPUSDT | DOGEUSDT | 1000BONK | HOMEUSDT |
|---|---|---|---|---|---|---|
| 5,00 | 0,150 | ✔ 16,31 (1,63) | ✔ 16,26 | ✔ 15,23 | ✔ 13,03 | ✔ 10,96 |
| 4,50 | 0,135 | ✔ 14,68 | ✔ 14,63 | ✔ 13,66 | ✔ 11,72 | ✔ 9,87 |
| 3,00 | 0,090 | ✔ 9,79 | ✔ 9,76 | ✔ 9,09 | ✔ 7,82 | ✔ 6,58 |
| 2,10 | 0,063 | ✔ 6,73 | ✔ 6,79 | ✔ 6,37 | ✔ 5,47 | ✘ pembulatan |

(angka dalam kurung = margin yang dipakai, USDT)

Perhatikan baris terakhir: pada equity 2,1 USDT target risiko sudah lebih kecil
dari risiko minimum yang bisa dibentuk bursa (minNotional 5 USDT × SL ~2% ≈
0,10 USDT), jadi pembulatan memaksa risiko naik >33% dan bot **melewati** trade.
Itu bukan kegagalan konfigurasi — memang sudah tidak ada ruang.

### 9c. Lantai mekanis sebenarnya (bukan angka config)

Karena `minNotional` 5 USDT, equity terkecil yang masih menghasilkan tiket
disetujui bergantung pada lebar SL simbol itu. Hasil pencarian biner
(`RiskManager.evaluate`, SL = ATR, leverage 10x):

| simbol | ATR/SL | equity minimum @ risiko 3% | @ risiko 5% |
|---|---|---|---|
| BCHUSDT | 0,61% | ≈ 1,53 USDT | ≈ 1,00 USDT |
| XRPUSDT | 0,61% | ≈ 1,52 USDT | ≈ 1,00 USDT |
| DOGEUSDT | 0,65% | ≈ 1,62 USDT | ≈ 1,00 USDT |
| 1000BONKUSDT | 0,76% | ≈ 1,88 USDT | ≈ 1,13 USDT |
| HOMEUSDT | 0,91% | ≈ 2,24 USDT | ≈ 1,34 USDT |
| SHELLUSDT | 2,44% | ≈ 6,02 USDT | ≈ 3,61 USDT |

Artinya: pada 5 USDT Anda **tidak** dibatasi mekanika bursa, melainkan oleh
biaya, slippage, dan (pada profil `modal-kecil`) lantai equity 5 USDT. Simbol
ber-SL lebar seperti SHELLUSDT justru butuh equity lebih besar daripada yang
Anda punya supaya risiko 3% tetap terpenuhi.

### 9d. Matematika bertahan hidup pada 5 USDT

| parameter | `modal-kecil` (5%) | `modal-5usdt` (3%) |
|---|---|---|
| risiko per trade | 0,25 USDT | 0,15 USDT |
| satu SL penuh | equity 5,00 → 4,75 | 5,00 → 4,85 |
| lantai equity | 5,00 → **langsung berhenti** | 2,00 |
| jumlah SL berturut sampai lantai | **1** | ≈ 30 (tetapi lihat baris berikut) |
| batas rugi harian | 10% = 0,50 USDT (2 SL) | 6% = 0,30 USDT (2 SL) |
| yang benar-benar menghentikan lebih dulu | lantai equity | batas harian + cooldown 2 loss |

Kesimpulan praktis: dengan 5 USDT, yang membatasi bukan lantai equity melainkan
**batas rugi harian** — dua kerugian beruntun dan hari itu selesai. Jangan
menaikkan `daily_loss_limit_pct` untuk "melanjutkan"; itu hanya mempercepat
kehabisan modal.

### 9e. Yang tidak berubah (dan tidak akan)

- **"All in" tetap tidak ada** — plafon 25% per trade, 100% ditolak config.
- Pada 5 USDT, satu SL yang terpeleset (gap/slippage) bisa memakan 10–50% modal:
  likuidasi pada 10x hanya ~9,6% dari harga masuk, sedangkan SL 2% tidak
  menjamin eksekusi tepat di harga.
- Fee taker bolak-balik ≈ **0,07–0,11 R** per trade pada ukuran ini, ditambah
  funding tiap 8 jam. Artinya TP harus cukup lebar; TP tipis = rugi pelan-pelan.
- Saran paling jujur: **kumpulkan minimal 50–100 USDT** sebelum live, atau
  anggap 5 USDT sebagai uang sekolah untuk menguji mekanisme (dry-run/testnet)
  — bukan sebagai modal mencari untung.

### 9f. Yang sudah diuji untuk bagian ini

`tests/test_modal_5usdt.py` (10 test, Python 3.13.18):

- profil `modal-5usdt` dimuat dengan nilai yang benar dan tetap `dry_run`;
- `min_equity_usdt` tidak bisa 0 (lantai 1) dan di bawah 20 tetap butuh
  `allow_aggressive_risk`;
- equity 5,00 disetujui; 4,75 dan 4,99 ditolak dengan pesan yang menyebut
  `docs/modal-kecil.md` dan `allow_aggressive_risk`;
- profil `modal-5usdt` masih trading pada 5,0 / 4,5 / 3,0 USDT, ditolak pada
  1,9 USDT (di bawah lantai) dan 2,1 USDT (pembulatan);
- notional tidak pernah di bawah minNotional 5 USDT.

**Belum diuji:** semua angka di atas adalah perhitungan di dalam bot dengan data
`exchangeInfo`, **bukan** eksekusi order nyata ke Binance dengan saldo 5 USDT.
Order 5 USDT bisa ditolak bursa karena alasan lain (mis. filter `LOT_SIZE`
tertentu, margin tidak tersedia saat fee dihitung, atau `-2010` saldo tidak
cukup). Uji di testnet dulu sebelum menyimpulkan.
