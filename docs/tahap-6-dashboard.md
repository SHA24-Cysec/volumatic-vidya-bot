# TAHAP 6 — Dashboard Terminal & Notifikasi

Laporan ini menjelaskan dashboard 4 panel dan jalur notifikasi, apa yang **sudah**
diuji, dan apa yang **belum**. Sama seperti tahap sebelumnya: tidak ada order
maupun koneksi Binance yang dijalankan dari sandbox (HTTP 451), jadi semua uji di
sini memakai data lokal (SQLite), data contoh, dan terminal `rich` sungguhan.

- Versi saat pengujian: Python **3.14.10** (juga 3.11.18, 3.12.16, 3.13.18),
  rich **15.0.0**, pytest 9.2.0, ruff 0.17.5, mypy 2.4.1.
- Pilihan pengguna pada tahap ini: **notifikasi cukup konsol/log** (tanpa Telegram
  atau webhook) dan **dashboard lengkap 4 panel** (dengan mode ringkas opsional).

---

## 1. Isi dashboard

| Panel | Isi | Sumber data |
|---|---|---|
| **Status bot** | mode (dry_run/testnet/live), status berjalan/kill switch/batas harian, ekuitas, laba-rugi hari ini (USDT & %), bar jarak ke batas rugi harian, jumlah trade, jumlah posisi, margin terpakai, cooldown, bandwidth MB/jam, bobot REST, pesan/err HTTP, pesan WS + latensi | `DailySnapshot`/`DailyState`, `Metrics` |
| **Posisi** | simbol, arah, qty sisa/awal, entry, harga terakhir, SL, TP1, P/L USDT, P/L %, tanda `BE`/`partial`/alasan tutup | `PositionStore` + harga terakhir yang disuntikkan bot |
| **Kandidat** | simbol, arah, skor gabungan + komponen (tren/volume/buku), status `DIPILIH` atau alasan tidak dipakai | `signals.ranking.Candidate` |
| **Kejadian** | insiden terbaru (paling atas, berwarna menurut severity) + ekor log | `PositionStore.incidents` + `RingBufferHandler` |

Prinsip yang dipegang:

1. **Dashboard tidak menyentuh bursa.** Ia hanya menampilkan `DashboardState`
   yang sudah disiapkan. Dashboard yang macet tidak bisa memblokir jalur order,
   dan render bisa diuji tanpa jaringan.
2. **Galat tidak mematikan bot.** Bila sumber data gagal, panel tetap tampil
   dengan baris `galat dashboard: ...` dan status "BERHENTI" — bukan traceback.
3. **Tidak mengarang angka.** Belum ada posisi → "tidak ada posisi terbuka";
   belum ada snapshot harian → ekuitas 0 dan trade 0, bukan tebakan.
4. **Rahasia tetap disaring.** Semua log yang masuk panel lewat
   `SecretRedactingFilter` yang sama dengan handler konsol.

Contoh tampilan nyata (data contoh, lebar 112 kolom):

```text
Volumatic VIDYA Bot — 2026-11-14 17:35:42
╭───────────────────────────────────────────────── STATUS BOT ─────────────────────────────────────────────────╮
│ Mode       DRY_RUN              Status      BERJALAN                                                         │
│ Ekuitas    48.90 USDT           Uptime      3j 12m                                                           │
│ Hari ini   -1.10 USDT (-2.20%)  Batas rugi  ███████░░░ 73% dari 3%                                           │
│ Trade      4/20                 Posisi      2/2                                                              │
│ Margin     41.5% / 60%          Cooldown    -                                                                │
│ Bandwidth  11.40 MB/jam         Bobot       210/2400 (9%)                                                    │
│ HTTP       142 req, 1 err       WS          18420 pesan, 1 reconnect, latensi 165 ms                         │
│ CONTOH TAMPILAN (--demo), bukan data pasar nyata                                                             │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭────────────────────────────────────────────── POSISI (2 aktif) ──────────────────────────────────────────────╮
│                                                                                                              │
│  Simbol      Arah        Qty sisa     Entry     Harga        SL       TP1    P/L USDT    P/L %   Tanda       │
│  ──────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│  DOGEUSDT    LONG         133/133      0.35    0.3542    0.3506    0.3584       +0.56   +1.20%   BE          │
│  ETHUSDT     SHORT      0.12/0.25    3120.5    3095.2    3120.5      3080       +3.04   +0.81%   BE partial  │
│  XRPUSDT     LONG           0/800      2.15         -      2.12      2.21       -1.83        -   stop_loss   │
│                                                                                                              │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### Responsif terhadap lebar terminal

| Lebar | Perilaku |
|---|---|
| ≥ 100 kolom | semua kolom (TP1, P/L %, komponen tren/volume/buku, statistik HTTP/WS detail) |
| < 100 kolom | kolom dikurangi, alasan tutup disingkat (`stop_loss_be` → `SL-BE`), supaya **nama simbol tidak terpotong** |
| `--compact` | satu panel: satu baris status + ringkasan P/L tiap posisi aktif |

---

## 2. Notifikasi

Bawaan: **konsol/log saja** — tanpa layanan luar, tanpa token, tanpa tambahan
pemakaian jaringan. Semua pesan berbentuk `NotifyEvent` (jenis, judul, detail,
level, simbol) yang **menyanitasi teksnya saat objek dibuat**, sehingga tidak ada
jalur yang lupa menyaring.

Jenis peristiwa yang dikirim: `signal`, `entry`, `partial_tp`, `break_even`,
`trailing`, `exit`, `rejected`, `error`, `incident`, `daily_stop`, `kill_switch`.

Pagarnya (semua bisa diatur di `config.yaml`):

- `min_level` — buang pesan di bawah level ini;
- `dedupe_sec` (default 60) — pesan dengan kunci sama (mis. `error:rest`) digabung,
  lalu dilaporkan sebagai `(pesan serupa sebelumnya: Nx)` saat muncul lagi;
- `max_per_minute` (default 30) — pagar agar terminal tidak dibanjiri saat
  jaringan bermasalah;
- `notify_on_trade` / `notify_on_error` — mematikan kelompok pesan tertentu.

Kanal Telegram **ada di config tetapi belum diimplementasikan**. Bila
`notify.telegram_enabled: true`, dispatcher mencetak satu peringatan
("belum diimplementasikan … hanya konsol/log") lalu tetap bekerja — bukan gagal
diam-diam, bukan pura-pura mengirim.

---

## 3. Perintah baru

```bash
volumatic-bot dashboard --once                 # cetak sekali, pakai state SQLite
volumatic-bot dashboard --demo --once          # data CONTOH untuk melihat tampilan
volumatic-bot dashboard --compact --once       # satu panel ringkas
volumatic-bot dashboard                        # layar penuh (butuh terminal interaktif)
volumatic-bot dashboard --refresh 3 --iterations 20
```

`simulate-trade` kini juga mengirim notifikasi (`--no-notify` untuk mematikan):
entry, keluar, dan insiden. Contoh keluaran nyata:

```
[NOTIF] 17:29:20 SUCCESS  entry LONG DOGEUSDT - qty=133 @ 0.35 | SL=0.344400 TP1=0.358400 | risiko≈0.74 USDT | mode=dry_run
[NOTIF] 17:29:20 INFO     keluar DOGEUSDT (stop_loss_be) - impas setelah biaya | sisa=0 dari 133 | SL akhir=0.350600
```

---

## 4. Hasil pengujian

```
pytest -q                        -> 482 passed (~41 detik)
3.11.18 / 3.12.16 / 3.13.18      -> 482 passed (masing-masing)
3.14.10                          -> 482 passed
ruff check + ruff format --check -> bersih (68 file sumber, 33 file test)
mypy src (strict)                -> bersih
pip-audit -r requirements.lock   -> tidak ada kerentanan dikenal
```

62 test baru (420 → 482):

| File | Isi |
|---|---|
| `tests/test_notify.py` (17) | urutan level, format baris/blok, **penyaringan rahasia** (`signature`, `X-MBX-APIKEY`, `listenKey`), kirim ke semua kanal, `min_level`, `notify_on_trade`/`on_error`, penggabungan pesan + hitungan `(xN)`, batas per menit, kanal rusak tidak mematikan bot, tanpa kanal, konsol menulis ke log, event entry/exit/rejected/incident/candidate, factory menghormati config + peringatan Telegram |
| `tests/test_dashboard_render.py` (20) | isi panel status (ekuitas, uptime, bar batas rugi, kill switch, batas harian, cooldown, metrik), P/L long & short, posisi tanpa harga tidak mengarang angka, tanda BE/partial, kandidat dipotong ke 10 baris, alasan penolakan, panel insiden+log, pemotongan log dari belakang, galat sumber, mode ringkas, data contoh, lebar 60 kolom tidak error |
| `tests/test_dashboard_app.py` (15) | ring buffer (batas, `limit=0`, level, sanitasi), sumber lokal kosong/berisi, hari trading mengikuti zona waktu + jam reset, `render_once`, galat sumber ditangkap, `refresh_sec` divalidasi, `run(iterations=3)`, berhenti lewat `stop_event`, mode ringkas |
| `tests/test_dashboard_cli.py` (10) | `dashboard --demo`, `--compact`, membaca state dari SQLite setelah `simulate-trade`, `dashboard.enabled: false` → exit 1, handler log tidak tertinggal, notifikasi muncul di `simulate-trade`, `--no-notify`, `notify_on_trade: false`, `min_level` menyaring |

Dua masalah yang ditemukan saat menjalankan (bukan oleh unit test):

1. **Pesan "trade dilewati" terkirim untuk tiket yang disetujui** — notifikasi
   `rejected` dikirim sebelum memeriksa `tiket.rejected`. Sudah diperbaiki.
2. **Ekuitas dashboard selalu 0** meski snapshot harian ada: sumber data memakai
   tanggal lokal sandbox (UTC), sedangkan snapshot disimpan dengan tanggal
   Asia/Jakarta (WIB = UTC+7, jadi sudah beda hari). Sekarang sumber data
   menghitung "hari trading" dengan zona waktu + jam reset yang sama dengan
   modul risiko.

---

## 5. Yang **belum** diuji / keterbatasan

1. **Dashboard belum tersambung ke mesin trading yang berjalan.** Sampai TAHAP 6
   belum ada satu proses "engine" yang menyatukan pemindai → sinyal → risiko →
   eksekusi → dashboard dalam satu loop. `dashboard` membaca **state yang sudah
   ada di SQLite** (hasil `simulate-trade` atau proses lain) dan data contoh.
   Penyatuan itu direncanakan bersama backtester di TAHAP 7 supaya loop sinyal
   yang sama dipakai untuk backtest dan live.
2. **Harga terakhir tidak tersedia dari DB.** Kolom "Harga" dan P/L berjalan hanya
   terisi bila bot menyuntikkan harga (`LocalStateSource.set_mark`). Pada
   `dashboard` mandiri, kolom itu tampil `-` — bukan angka karangan.
3. **Kolom P/L posisi tertutup bergantung pada bursa.** Nilainya berasal dari
   `realizedPnl` yang dikirim Binance; pada simulasi CLI nilainya 0 karena broker
   simulasi tidak mengirim realizedPnl per order.
4. **Tampilan layar penuh (`rich.live`) tidak diuji di terminal nyata** — di
   sandbox tidak ada TTY, jadi seluruh uji memakai mode sekali-cetak dan
   `Console(record=True)`. Yang belum terbukti: perilaku `Live` saat resize
   terminal, saat output dialihkan ke berkas, dan saat Ctrl+C.
5. **Notifikasi belum punya kanal eksternal.** Bila nanti ingin Telegram/webhook,
   tinggal implementasi protokol `Notifier`; belum ada uji pengiriman nyata.
6. **Belum ada notifikasi untuk `daily_stop`/`kill_switch` di kode** — jenis
   pesannya sudah ada, tetapi pemicunya baru dipasang saat engine dibuat (TAHAP 7).
7. **Tangkapan log hanya dalam memori** (`RingBufferHandler`, default 200 baris);
   setelah restart, panel log kosong walaupun insiden tetap ada di SQLite.
8. **Belum ada uji beban dashboard** dengan 30 simbol × 3 posisi × refresh 1,5
   detik. Beban sebenarnya kecil (render teks), tetapi belum diukur.
