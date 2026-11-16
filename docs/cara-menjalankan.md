# Cara Menjalankan Bot (panduan operasional)

> Modal di bawah 50 USDT (mis. 7 USDT)? Baca dulu
> [`modal-kecil.md`](modal-kecil.md) — setelan bawaan sengaja menolak equity di
> bawah 20 USDT, dan "all in 100%" tidak didukung (plafon 25%). Ringkasnya:
> jalankan semua perintah dengan `--profil modal-kecil` (semua setelan tetap di
> satu `config.yaml`, tidak ada file config kedua).
>
> Sejak 15 Nov 2026 ada juga **deteksi profil otomatis** (bagian
> `profil_otomatis` di config.yaml): saat `run` dimulai, saldo akun dibaca satu
> kali lalu dipetakan — saldo < 10 USDT memakai profil `modal-5usdt`, 10 s.d.
> <50 USDT memakai `modal-kecil`, dan >= 50 USDT memakai nilai dasar config.
> `preflight --equity <saldo>` memakai pemetaan yang sama. Keputusan diambil
> SEKALI di awal sesi (tidak berganti di tengah jalan walau saldo berubah), dan
> pilihan manual (`--profil <nama>` atau `profil_aktif:`) selalu menang. Profil
> hasil deteksi dicatat di log sebagai peringatan menonjol; matikan fiturnya
> dengan `profil_otomatis.aktif: false`.

Panduan ini urut dari yang **paling aman** ke yang **paling berisiko**. Jangan
lompat langkah. Setiap perintah ditulis persis seperti yang sudah diuji di repo
ini (Python 3.11–3.14, 656 test lulus).

> Peringatan yang tidak berubah: hasil backtest repo ini **negatif setelah biaya**
> (lihat `docs/tahap-7-mesin-backtest.md`). Menjalankan bot ini dengan uang
> sungguhan berarti Anda memakai versi yang belum dicoba ke Binance dan belum
> terbukti menghasilkan uang.

---

## 0. Prasyarat

- Python **3.11–3.14** (dites di 3.11.18, 3.12.16, 3.13.18, 3.14.10).
- Untuk mode testnet/live: akun Binance Futures + API key.
- Terminal Linux/macOS (Windows: pakai WSL atau PowerShell, perintah `venv` sama).

---

## 1. Pasang

```bash
unzip volumatic-vidya-bot-tahap7.zip
cd volumatic-vidya-bot

# cara A — uv (dipakai untuk lockfile proyek ini)
uv venv --python 3.14 .venv
uv pip install -e ".[dev]"

# cara B — venv biasa
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Semua perintah di bawah menganggap venv sudah aktif (`source .venv/bin/activate`).

Cek cepat bahwa semuanya sehat:

```bash
volumatic-bot version
pytest -q          # harus 695 passed
```

---

## 2. Langkah 1 — jalankan TANPA jaringan (tidak ada risiko apa pun)

Ini yang harus Anda lakukan pertama. Tidak ada koneksi ke Binance, tidak ada order.

```bash
# 1a. validasi config (salah ketik nama field = langsung error)
volumatic-bot --config config.yaml check-config

# 1b. pemeriksaan keamanan & kesiapan
volumatic-bot --config config.yaml preflight --equity 50

# 1c. lihat sinyal indikator pada data historis
volumatic-bot indicator --candles tests/data/btcusdt_15m_5bulan.csv.gz --show 5

# Catatan: config bawaan sekarang 4h. Berkas CSV 15m di bawah otomatis
# digabung ke 4h (muncul pesan "data 15m di-resample ke 4h") supaya label,
# ATR, dan SL sesuai timeframe yang benar-benar dipakai bot.
# Berkas uji 5 bulan itu hanya berisi 152 hari, sedangkan filter rezim butuh
# 200 hari harian: hasilnya "0 entry, N ditahan filter rezim" dan itu wajar.
# Untuk sesi yang benar-benar entry, pakai data >= 8 bulan (mis. data/raw/).

# 1d. mesin produksi diputar pada candle historis (menulis ke SQLite)
volumatic-bot --config config.yaml simulate-session \
    --candles tests/data/btcusdt_15m_5bulan.csv.gz --symbol BTCUSDT \
    --equity 500 --risk-pct 0.5

# 1e. backtest dengan biaya + funding nyata
volumatic-bot --config config.yaml backtest \
    --candles tests/data/btcusdt_15m_5bulan.csv.gz --symbol BTCUSDT \
    --exchange-info tests/data/exchange_info_sample.json \
    --equity 500 --leverage 10 \
    --funding-file tests/data/btcusdt_funding_2025-07_2025-11.json

# 1f. lihat hasil sesi di database & dashboard (data contoh)
volumatic-bot positions
volumatic-bot dashboard --demo --once
volumatic-bot dashboard --demo      # layar penuh, Ctrl+C untuk keluar
```

Catatan: semua mode tanpa jaringan menulis ke `var/bot.db` (diatur
`data.db_path`). Hapus berkas itu bila ingin mulai dari state bersih.

---

## 3. Langkah 2 — siapkan kredensial (khusus testnet/live)

**Mulai dari kunci TESTNET**, bukan kunci akun asli: <https://testnet.binancefuture.com>
→ daftar/aktifkan → *API Key* → buat kunci.

```bash
cp .env.example .env
nano .env      # isi BINANCE_API_KEY=... dan BINANCE_API_SECRET=...
chmod 600 .env
```

Aturan kunci (penting, dan bot **tidak bisa** memverifikasinya untuk Anda):

- aktifkan **hanya** izin *Enable Futures*;
- **JANGAN** aktifkan *Enable Withdrawals*;
- aktifkan **IP whitelist** untuk IP server bot;
- jangan commit `.env` (sudah masuk `.gitignore`), jangan tempel ke chat.

Cek terbaca atau tidak:

```bash
volumatic-bot --config config.yaml check-config
# baris "API key" harus menampilkan nilai bertopeng, bukan "<kosong>"
```

---

## 4. Langkah 3 — uji koneksi ke Binance (belum ada order)

Mode masih `dry_run`, jadi perintah ini **tidak mengirim order**.

**Mulai dari `netcheck`** — perintah ini memeriksa koneksi langkah demi langkah dan
menerjemahkan error menjadi saran tindakan:

```bash
volumatic-bot --config config.yaml netcheck

# khusus IP: alamat publik yang dipakai bot + apakah Binance menerimanya
volumatic-bot ip
```

Sejak fitur deteksi IP ditambahkan, `netcheck` mencetak IP publik di bagian
atas. Bila yang Anda butuhkan hanya itu, perintah `ip` lebih singkat dan
langsung memberi kesimpulan LAYAK / TIDAK LAYAK dengan exit code. Rinciannya di
[`ip-egress.md`](ip-egress.md).

Contoh keluaran bila Binance memblokir lokasi Anda (kasus paling sering di
Indonesia):

```text
konfigurasi : mode=testnet testnet=True
REST        : https://demo-fapi.binance.com
kredensial  : ada
[GAGAL] ping REST -> BinanceAPIError
endpoint : https://demo-fapi.binance.com
pesan    : /fapi/v1/ping: [kode 0] [HTTP 451 permintaan diblokir dari lokasi/IP ini]
kemungkinan penyebab:
  Binance menolak permintaan dari lokasi/IP ini (HTTP 451). Ini blokir
  geografis dari pihak Binance, bukan salah konfigurasi.
  Solusi: jalankan bot di VPS di negara yang dilayani Binance
  (mis. Singapura, Tokyo, Frankfurt). Mengganti API key tidak membantu.
```

Bila `netcheck` sudah `[OK ]`, lanjut:

### Memilih simbol: otomatis vs manual

```bash
volumatic-bot --config config.yaml run                 # OTOMATIS (pindai sendiri)
volumatic-bot --config config.yaml run --symbols dogeusdt xrpusdt   # MANUAL
```

| | Otomatis (tanpa `--symbols`, `whitelist` kosong) | Manual (`--symbols` / `whitelist`) |
|---|---|---|
| Simbol | dipilih pemindai: perpetual USDT dengan volume 24 jam ≥ 50 juta, spread ≤ 0,05%, ATR% 0,25–6, minNotional yang muat margin | persis yang Anda sebut |
| Jumlah | maks `scanner.max_symbols` (30) | apa pun |
| Refresh | tiap `scanner.symbol_refresh_hours` (4 jam) | tidak |
| Bandwidth | ±120 MB/jam untuk 28 simbol | sebanding jumlah simbol |
| Kapan dipakai | eksplorasi, pasar berubah cepat | hemat kuota, simbol yang Anda kuasai, uji terarah |

Pada mode otomatis, saat refresh: simbol yang **sedang punya posisi selalu
dipertahankan** (stream-nya tidak dilepas), simbol baru diunduh riwayat 1500
candle sebelum candle live-nya diproses, dan bila hasil pemindaian kosong atau
gagal, daftar lama dipakai — bot tidak mati hanya karena pasar sepi atau satu
permintaan gagal.

Catatan penting: **sinyal tidak "dicari" ke seluruh pasar setiap detik.** Bot
hanya memantau simbol yang lolos filter dan sudah dihangatkan; sinyal VIDYA
muncul saat candle timeframe `scanner.timeframe` (bawaan 4h) tertutup. Jadi "deteksi otomatis" = daftar simbol otomatis,
bukan pemindaian ulang indikator untuk 528 simbol tiap menit (itu akan boros
bandwidth dan bobot REST).

```bash
# daftar simbol + filter (bobot REST kecil)
volumatic-bot --config config.yaml symbols

# pindai simbol: mana yang lolos filter, mana yang ditolak dan alasannya
volumatic-bot --config config.yaml scan --equity 50

# dengarkan kline tertutup & cetak sinyal selama 5 menit (0 = tanpa batas)
volumatic-bot --config config.yaml listen --duration 300

# plus evaluasi order book tiap sinyal
volumatic-bot --config config.yaml listen --duration 300 --with-book
```

Kalau langkah ini gagal, **berhenti** dan baca bagian Troubleshooting (§8).
Tidak ada gunanya lanjut ke testnet sebelum `listen` bisa mencetak sinyal.

---

### 4b. Bila `netcheck` gagal: pilih host testnet yang benar

Binance punya **dua** host REST testnet, dan salah satunya bisa mati/diblokir:

| Host | Diuji 14 Nov 2026 | Setelan |
|---|---|---|
| `https://testnet.binancefuture.com` | **200 OK** (744 simbol) | `net.legacy_testnet_rest: true` ← **bawaan sekarang** |
| `https://demo-fapi.binance.com` | 451 dari sandbox, **403** di mesin pengguna | `net.legacy_testnet_rest: false` |

Gejala bila memakai host yang salah: `ping` (endpoint **publik**, tanpa API key)
sudah gagal dengan `HTTP 403 IP tidak di-whitelist atau akses ditolak` atau
`HTTP 451 restricted location`. Kalau endpoint publik saja menolak, penyebabnya
**host/jaringan**, bukan kunci API Anda.

`netcheck` sekarang mencoba host alternatif secara otomatis:

```text
[GAGAL] ping REST -> BinanceAPIError
...
[PETUNJUK] host testnet ALTERNATIF hidup: https://testnet.binancefuture.com
  Ubah config.yaml -> net.legacy_testnet_rest: true
```

Catatan: `wss://fstream.binancefuture.com` (WebSocket testnet) tetap sama untuk
kedua host, dan sudah terbukti menerima data (uji 14 Nov 2026: 1.723 pesan dalam
60 detik untuk 28 simbol, latensi ~52 ms). Sejak 2026-04-23 Binance memisahkan
channel WebSocket: kline lewat `/market/stream`, depth lewat `/public/stream`,
dan user data stream lewat `/private/ws/<listenKey>`; bot memakai jalur baru
ini secara otomatis dan jatuh ke jalur lama hanya bila channel baru ditolak
(probe langsung 15 Nov 2026: produksi hanya menerima channel baru, testnet
menerima keduanya).

## 4c. Apa yang seharusnya Anda lihat di dashboard `run`

Kalau `run` menampilkan panel **STATUS BOT → Mode TESTNET, Status BERJALAN**, itu
artinya sesi **sudah jalan**: kline masuk, mesin menunggu sinyal, tidak ada order
dikirim sampai ada sinyal yang lolos semua pagar. Contoh yang normal:

| Yang tampil | Arti |
|---|---|
| `Ekuitas 5000.00 USDT` | saldo **testnet** Anda (bukan modal nyata) — angka ini hanya bisa muncul kalau API key + signature Anda **valid**, jadi kunci sudah benar |
| `Posisi 0/2`, `Trade 0/20` | belum ada trade; wajar. Di 4h sinyal jauh lebih jarang: perubahan tren VIDYA ±2 kali per bulan per simbol, dan sebagian ditolak filter rezim |
| `KANDIDAT — belum ada kandidat` | diisi hanya saat bot memindai simbol sendiri (`scan`/`listen`); `run --symbols X` tidak memakai pemindai |
| `POSISI (0 aktif, 1 riwayat)` + baris `selesai:SL` | **riwayat** posisi lama dari database yang sama (`var/bot.db`), bukan posisi terbuka. Baris riwayat kini diredupkan dan diberi label `selesai:` |
| `Bandwidth 72 MB/jam` (menit pertama) | dulu melaporkan burst warm-up (1500 candle + exchangeInfo). Sekarang jendela direset setelah warm-up, jadi angkanya mencerminkan kondisi tunak |
| `LOG TERAKHIR — belum ada log` | dulu panel ini tidak pernah terisi di `run`; sekarang bot memasang penangkap log sehingga baris terakhir muncul |
| `Jaringan 4 req / 53 pesan WS, Latensi 53 ms` | WebSocket sungguhan bekerja |

Sejak 15 Nov 2026 tampilan dinamis (`run` maupun `dashboard` tanpa `--once`)
memakai **layar penuh** (alternate screen): frame digambar ulang dari atas layar
pada setiap refresh sehingga tidak pernah menumpuk di scrollback walau terminal
di-resize, dipindah, atau diperbesar, dan saat bot berhenti terminal kembali
persis seperti sebelum bot start (log startup terlihat lagi). Matikan lewat
`dashboard.layar_penuh: false` di config.yaml, atau `--inline` pada perintah
`dashboard`, bila Anda ingin perilaku lama (frame terakhir tertinggal di
scrollback). Output non-terminal (pipe, CI, berkas log) otomatis memakai mode
inline tanpa alternate screen.

### Database sudah dipisah per mode (otomatis)

Sejak 14 Nov 2026 ada **dua** database dengan tugas berbeda, dan berkas posisi
otomatis diberi akhiran mode:

| Mode | Berkas posisi/state | Berkas cache candle |
|---|---|---|
| `dry_run` | `var/bot-dry-run.db` | `var/cache.db` |
| `backtest` | `var/bot-backtest.db` | `var/cache.db` |
| `testnet` | `var/bot-testnet.db` | `var/cache.db` |
| `live` | `var/bot-live.db` | `var/cache.db` |

- **Posisi/state dipisah** karena rekonsiliasi membandingkan berkas ini dengan
  posisi di bursa. Posisi hasil `simulate-session` tidak boleh terbaca oleh
  `run` testnet — di versi sebelumnya itu bisa membuat bot mencoba "menutup"
  posisi yang sebenarnya tidak ada.
- **Cache candle dipakai bersama** karena datanya sama untuk semua mode; kalau
  dipisah, Anda mengunduh 1500 candle yang sama empat kali.

Setelan di `config.yaml`:

```yaml
data:
  db_path: "var/bot.db"            # dasar nama; akhiran mode ditambahkan otomatis
  separate_by_mode: true           # false = kembali ke satu berkas untuk semua mode
  cache_db_path: "var/cache.db"    # cache bersama (kosongkan = ikut berkas posisi)
```

`preflight` selalu menampilkan berkas yang sedang dipakai:

```text
[OK ] database           posisi: var/bot-testnet.db | cache: var/cache.db
```

Bila Anda punya `var/bot.db` dari versi sebelumnya, `preflight` memberi tahu dan
menyarankan perintah pemindahan:

```text
[OK ] db versi lama      var/bot.db ada. ... pindahkan: mv var/bot.db var/bot-testnet.db
```

Perintah `positions` dan `dashboard` hanya membaca berkas mode aktif — jadi
tambahkan `--config` yang sama dengan saat bot dijalankan.

Kalau ragu bot memakai mode/berkas mana, pakai pemindai semuanya:

```bash
volumatic-bot --config config.yaml positions --all-modes
```

```text
Database: var/bot-dry-run.db (mode dry_run)
Posisi aktif: 0

Berkas database mode lain:
  bot-testnet.db           posisi aktif: 1  <-- ada posisi!
```

Jangan tertipu angka `Posisi aktif: 0` pada berkas yang salah — itu bukan berarti
bursa bersih.

**BTCUSDT ada di `scanner.blacklist` bawaan.** `run --symbols btcusdt` sekarang
**ditolak** dengan pesan jelas (dulu tetap jalan diam-diam):

```text
sesi tidak bisa dijalankan: simbol ada di scanner.blacklist: BTCUSDT.
Hapus dari blacklist di config.yaml bila memang ingin dipakai.
```

Kalau memang ingin BTCUSDT, hapus dari `scanner.blacklist`. Salah ketik simbol
juga ditolak lebih awal beserta contoh simbol yang tersedia.

## 5. Langkah 4 — testnet (order sungguhan, uang palsu)

Butuh **dua** perubahan di `config.yaml`:

```yaml
ops:
  mode: "testnet"        # dari "dry_run"
exchange:
  testnet: true          # harus tetap true
```

Lalu:

```bash
# preflight khusus testnet: wajib menyatakan Anda paham risikonya
volumatic-bot --config config.yaml preflight --equity 1000 --ie-understand-risk

# jalankan mesin (butuh daftar simbol: scanner.whitelist masih kosong)
volumatic-bot --config config.yaml run --symbols DOGEUSDT
```

Yang perlu diketahui tentang `run`:

| Hal | Keterangan |
|---|---|
| `--symbols` | wajib bila `scanner.whitelist: []`. Bot **tidak** memilih simbol sendiri saat `run`; daftar simbol datang dari `--symbols` atau `scanner.whitelist`. |
| `--no-dashboard` | matikan panel `rich` (mis. saat jalan di server tanpa TTY). |
| `--iterations N` | berhenti setelah N kali refresh dashboard (berguna untuk uji singkat). |
| Ctrl+C | berhenti dengan rapi (koneksi ditutup, posisi **tidak** otomatis ditutup). |
| Mode `dry_run`/`backtest` | `run` **menolak** dengan kode keluar 2 — pakai `simulate-session`. |

Sambil berjalan, pantau dari terminal lain:

```bash
volumatic-bot positions                # posisi, order, insiden dari SQLite
volumatic-bot dashboard                # 4 panel: status, posisi, kandidat, kejadian
volumatic-bot dashboard --inline       # mode lama: frame di scrollback (bukan layar penuh)
```

Menghentikan paksa semuanya (kill switch):

```bash
touch KILL_SWITCH     # nama file dari ops.kill_switch_file
# bot menutup semua posisi lalu berhenti; hapus filenya untuk mengaktifkan lagi
rm KILL_SWITCH
```

Yang **wajib Anda periksa manual** di web testnet setelah beberapa trade:

1. SL benar-benar terpasang **setelah** entry (bukan sebelumnya);
2. TP parsial terisi dan sisa posisi tetap berjalan;
3. SL bergeser ke break-even setelah TP1;
4. posisi benar-benar tertutup saat SL kena;
5. tabel `positions` dan `incidents` di SQLite sesuai kenyataan.

Kalau ada yang tidak cocok: `volumatic-bot reconcile --symbols DOGEUSDT` untuk
menyamakan catatan bot dengan bursa.

---

## 6. Langkah 5 — live (uang sungguhan) — pikir dua kali

Tiga pagar harus dibuka **bersamaan**; tanpa ketiganya bot menolak jalan.

```yaml
ops:
  mode: "live"
  allow_live: true
  live_confirmation: "SAYA-MENGERTI-RISIKONYA"   # ketik sendiri, huruf per huruf
exchange:
  testnet: false
```

```bash
volumatic-bot --config config.yaml preflight --equity 50 --ie-understand-risk
volumatic-bot --config config.yaml run --symbols DOGEUSDT
```

Sebelum ini, jawab dulu dengan jujur:

- Apakah testnet sudah berjalan minimal beberapa hari tanpa insiden
  `protection_failed`, posisi hantu, atau SL hilang?
- Apakah Anda siap kehilangan seluruh saldo yang dipakai?
- Modal 50 USDT dengan pagar bawaan **akan melewati hampir semua sinyal**
  (backtest: 0 trade). Itu disengaja: bot memilih tidak trading daripada
  membesarkan risiko.

---

## 7. Menjalankan terus-menerus (opsional, systemd)

Contoh unit, supaya mati otomatis saat crash dan log tersimpan:

```ini
# /etc/systemd/system/volumatic-bot.service
[Unit]
Description=Volumatic VIDYA Bot
After=network-online.target

[Service]
User=bot
WorkingDirectory=/opt/volumatic-vidya-bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/volumatic-vidya-bot/.venv/bin/volumatic-bot \
    --config /opt/volumatic-vidya-bot/config.yaml run --symbols DOGEUSDT --no-dashboard
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now volumatic-bot
journalctl -u volumatic-bot -f
```

Catatan: bot ini **belum pernah** dijalankan berjam-jam/berhari-hari, termasuk uji
beberapa simbol sekaligus. Jangan tinggalkan tanpa pengawasan pada percobaan
pertama.

---

## 7b. Menghentikan bot yang sedang berjalan

Ada tiga cara, dari yang paling ringan ke paling tegas.

### 1. Ctrl+C di terminal bot (paling sering dipakai)

```text
^C
sesi dihentikan operator
```

Yang terjadi: koneksi WebSocket/HTTP ditutup rapi, sesi berhenti, **posisi TIDAK
ditutup** dan **SL/TP tetap terpasang di bursa**. Jadi posisi Anda masih terlindungi
walau bot mati. Cocok bila Anda ingin berhenti tanpa menyentuh posisi.

```bash
volumatic-bot --config config.yaml positions   # pastikan apa yang masih terbuka
```

### 2. Kill switch — tutup semua posisi lalu berhenti

```bash
volumatic-bot --config config.yaml kill-switch           # pasang
volumatic-bot --config config.yaml kill-switch --status  # cek
volumatic-bot --config config.yaml kill-switch --clear    # hapus (izinkan sesi baru)
```

Setara dengan `touch KILL_SWITCH` (nama file dari `ops.kill_switch_file`), tetapi
tidak perlu mengingat nama berkas atau lokasinya. Bot yang sedang berjalan
memeriksa file ini tiap `ops.kill_switch_poll_sec` detik (bawaan **3 detik**),
lalu:

1. mencatat insiden `kill_switch` (critical) + notifikasi;
2. **menutup semua posisi dengan order market** (`close_position`, reduce-only);
3. menghentikan sesi dengan alasan `kill switch (KILL_SWITCH)`.

Selama file itu ada, sesi baru **menolak jalan** (`preflight` menandainya `[CEK]`).
Hapus dengan `--clear` kalau memang ingin mulai lagi.

> Bug yang diperbaiki 15 Nov 2026: sebelumnya file kill switch hanya diperiksa
> saat **start** dan saat ada **sinyal** — jadi `touch KILL_SWITCH` pada bot yang
> sedang berjalan tidak melakukan apa pun sampai sinyal berikutnya datang (bisa
> berjam-jam). Sekarang diperiksa tiap beberapa detik.

### 3. `stop` — hentikan berdasarkan berkas PID (paling tidak ambigu)

`run` kini menulis `var/bot-<mode>.pid` saat mulai dan menghapusnya saat berhenti.
Jadi Anda tidak perlu `ps -ef | grep` yang bisa salah sasaran:

```bash
volumatic-bot --config config.yaml stop                 # SIGTERM (posisi tetap terbuka)
volumatic-bot --config config.yaml stop --close         # tutup posisi dulu, lalu berhenti
volumatic-bot --config config.yaml stop --all-modes     # hentikan semua mode
volumatic-bot --config config.yaml stop --force         # SIGKILL bila membandel
```

Contoh keluaran:

```text
[HENTIKAN] bot-testnet.pid: PID 1925691
  berhenti.

Selesai. Periksa posisi & state: volumatic-bot positions
```

Berkas PID juga mencegah **dua bot jalan bersamaan** pada mode yang sama: `run`
kedua akan menolak dengan `sesi mode ini sudah berjalan (PID ...)`.

### 4. Matikan prosesnya manual (terminal sudah hilang / SSH putus)

```bash
# cari prosesnya
ps -ef | grep -i volumatic-bot

# hentikan dengan sopan (SIGTERM -> bot menutup koneksi)
kill <PID>

# kalau tidak berhenti juga
pkill -f "volumatic-bot.*run"
```

**Catatan penting:** mematikan proses **tidak** menutup posisi dan **tidak**
membatalkan SL — SL tetap aktif di bursa. Sesudah proses mati, periksa:

```bash
volumatic-bot --config config.yaml positions
volumatic-bot --config config.yaml reconcile --symbols DOGEUSDT   # samakan dengan bursa
```

### 5. Bila dijalankan lewat systemd

```bash
sudo systemctl stop volumatic-bot      # SIGTERM rapi
sudo systemctl status volumatic-bot
journalctl -u volumatic-bot -n 50
```

### Ringkasan: mana yang harus dipilih

| Situasi | Tindakan | Posisi |
|---|---|---|
| Selesai uji, mau berhenti biasa | Ctrl+C | tetap terbuka + SL aktif |
| Pasar bergerak liar, mau keluar semua | `kill-switch` | **ditutup market** |
| Terminal/SSH putus, proses masih hidup | `kill <PID>` | tetap terbuka + SL aktif |
| Bot menghentikan diri (`stop_reason`) | cek `positions` | tergantung alasan |

Yang **tidak** boleh: menutup posisi manual di web testnet sementara bot masih
berjalan — bot akan menganggapnya posisi hantu lalu memicu insiden rekonsiliasi.
Hentikan bot lebih dulu, baru tutup manual.

## 8. Catatan versi: bug `run` pada rilis pertama

Rilis zip pertama (`volumatic-vidya-bot-tahap7.zip` sebelum 14 Nov 2026, malam)
punya **bug** di `engine/live.py`: `build_session()` memanggil
`rest.request(...)` sebelum `await rest.start()`, sehingga `run` selalu gagal
dengan:

```text
File ".../binance/rest.py", line 163, in session
    raise RuntimeError("session belum dibuat; panggil start() lebih dulu")
RuntimeError: session belum dibuat; panggil start() lebih dulu
```

Artinya kegagalan itu **bukan** masalah kunci API atau jaringan Anda. Sudah
diperbaiki: `rest.start()` kini dipanggil sebelum request pertama (dan koneksi
ditutup rapi bila penyusunan sesi gagal di tengah jalan). Unduh ulang zip terbaru
atau terapkan perubahan berikut pada `src/volumatic_bot/engine/live.py`:

```python
    rest = BinanceREST(...)          # sudah ada
    await rest.start()               # <- baris yang ditambahkan
```

Perbaikan ini juga menambah perintah `netcheck` dan pesan error yang jelas
(bukan traceback) untuk `run`.

**Bug kedua (rilis yang sama):** `listen` mencetak traceback panjang saat
Ctrl+C / akhir `--duration`:

```text
File ".../asyncio/queues.py", line 186, in get
    await getter
asyncio.exceptions.CancelledError
```

Penyebabnya `contextlib.suppress(Exception)` dipakai untuk menunggu task yang
dibatalkan — padahal `CancelledError` turunan `BaseException`, bukan `Exception`.
Sudah diperbaiki (`suppress(asyncio.CancelledError, Exception)`); kini shutdown
mencetak `Umpan kline dihentikan dengan rapi`.

## 8b. Kalau muncul insiden `protection_failed` / `close_failed`

Panel dashboard memotong teks, jadi **baca dari SQLite** untuk melihat pesan
lengkapnya:

```bash
volumatic-bot --config config.yaml positions --limit 5
```

Contoh keluaran yang berguna:

```text
[critical] protection_failed REZUSDT: SL gagal dipasang. varian 1: BinanceAPIError
  kode=-2021 HTTP=400 | Order would immediately trigger | order: STOP_MARKET SELL
  REZUSDT stop=0.0028... closePosition
[critical] stop_already_breached IOTAUSDT: harga ... sudah melewati SL ...
```

Arti dan tindakannya:

| Insiden | Arti | Tindakan |
|---|---|---|
| `stop_already_breached` | harga sudah melewati SL sebelum SL terpasang; bot menutup posisi market | normal (pagar bekerja). Bila sering terjadi, entry terlalu telat |
| `sl_fallback` | varian SL pertama ditolak bursa, cadangan (reduceOnly) dipakai | aman; laporkan bila sering |
| `protection_failed` | SL **tidak** terpasang | bot langsung menutup posisi market |
| `close_failed` | SL gagal **dan** penutupan gagal | **tutup manual di web testnet sekarang**, lalu hentikan bot |
| `naked_position` | sama seperti di atas, dari sisi mesin; sesi **dihentikan** | periksa bursa, tutup manual, baru jalankan lagi |
| `stop_loss` hilang saat rekonsiliasi | SL lepas (mis. dibatalkan manual) | bot memasang ulang; bila gagal → posisi ditutup |

Bila `protection_failed` berulang dengan kode yang sama, kirimkan **satu baris
insiden lengkap** (kode + parameter order) — dari situ penyebabnya bisa dipastikan
(mis. `-1111` presisi harga, `-4120` tipe order tidak didukung, `-4045` batas order
stop, `-2021` langsung terpicu).

## 9. Troubleshooting

| Gejala | Artinya | Tindakan |
|---|---|---|
| `HTTP 451` / "restricted location" | **Blokir geografis dari Binance.** Domain Binance diblokir di Indonesia (Kominfo/Bappebti), jadi dari jaringan lokal Anda koneksi akan selalu 451 — mengganti API key, VPN gratisan, atau mengubah config **tidak** menyelesaikannya | Jalankan bot di **VPS luar negeri** (Singapura/Tokyo/Frankfurt), lalu akses VPS itu lewat SSH. Ini juga syarat praktis untuk bot yang harus hidup 24 jam |
| `HTTP 403` pada `ping` (endpoint publik) | **Host testnet salah**, bukan kunci API | Set `net.legacy_testnet_rest: true` (§4b) |
| `HTTP 403` / `-2015` pada endpoint bersignature | Kunci salah, IP tidak di-whitelist, atau izin Futures belum aktif | Periksa `.env`, IP whitelist, dan izin kunci di web Binance |
| `-1021` timestamp | Jam server melenceng | `sudo timedatectl set-ntp true` |
| `-4164` / minNotional | Notional di bawah minimum bursa | Normal untuk modal kecil — bot akan melewati trade, jangan paksa |
| `run` keluar kode `2` + "mode dry_run" | `ops.mode` masih `dry_run` | Setel `testnet`/`live` (§5/§6) |
| "daftar simbol kosong" | `run` tanpa `--symbols` dan whitelist kosong | Tambahkan `--symbols DOGEUSDT` atau isi `scanner.whitelist` |
| `preflight` gagal "mode operasi" | Mode testnet/live tanpa `--ie-understand-risk` | Tambahkan flag itu setelah Anda paham risikonya |
| Tidak ada trade sama sekali | Pagar risiko menolak (normal untuk modal <50 USDT, BTCUSDT) | Turunkan `risk_per_trade_pct`? Justru makin sedikit trade. Lihat `scripts/sensitivitas_backtest.py` |
| Dashboard "tidak ada posisi" | Belum ada trade / `data.db_path` beda | Cek `volumatic-bot positions` dengan config yang sama |
| Bot berhenti sendiri | Batas rugi harian 3% kena / kill switch ada | Sesi berhenti sampai hari trading berikutnya; periksa `incidents` |
| `-4120 Order type not supported for this endpoint` | Versi lama mengirim SL/TP ke `/fapi/v1/order`; sejak **2025-12-09** Binance mewajibkan **Algo Order API** | **Tutup posisi manual di web bursa** (bot sudah berhenti karena posisi telanjang), pakai versi terbaru, dan biarkan `exchange.conditional_order_api: "auto"`. Lihat `docs/tahap-5-eksekusi.md` §10 |
| Insiden `naked_position` + `close_failed` | SL gagal dipasang **dan** penutupan otomatis gagal | Posisi ini bahaya nyata: tutup manual sekarang, jangan hanya restart bot |
| `TUTUP GAGAL` padahal posisi hilang di web | Respons ACK (`executedQty=0`) dibaca sebagai gagal | Sudah diperbaiki (order MARKET memakai `newOrderRespType=RESULT` + konfirmasi `positionRisk`); pastikan versi terbaru |

Log tersimpan di konsol. Bila perlu berkas, arahkan:

```bash
volumatic-bot --config config.yaml run --symbols DOGEUSDT 2>&1 | tee bot.log
```

Rahasia (API key, secret, listenKey, signature) otomatis disaring menjadi
`***REDACTED***` sebelum masuk log — jadi `bot.log` aman dibagikan, tetapi tetap
periksa sebelum mengunggah ke mana pun.

---

## 10. Checklist sebelum uang sungguhan

- [ ] `pytest -q` → 695 passed di mesin Anda.
- [ ] `check-config` + `preflight --equity <saldo asli>` semua `[OK ]`.
- [ ] `netcheck` semua `[OK ]` dari mesin yang akan dipakai (VPS, bukan laptop
      di jaringan yang diblokir).
- [ ] `listen --duration 300` mencetak sinyal tanpa error.
- [ ] Testnet jalan ≥ 2–3 hari; tidak ada insiden `protection_failed`.
- [ ] SL/TP/BE/trailing diverifikasi **manual** di web testnet.
- [ ] Kunci tanpa izin withdraw + IP whitelist.
- [ ] Kill switch diuji sekali (`touch KILL_SWITCH`) dan bot bereaksi benar.
- [ ] Anda siap kehilangan seluruh saldo yang dipakai.

Kalau ada satu saja yang belum dicentang, tetap di `dry_run`/testnet.
