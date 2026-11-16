# Deteksi IP egress

Fitur ini menjawab satu pertanyaan sebelum bot mengirim order: **IP mana yang
dipakai bot untuk keluar ke internet, dan apakah Binance menerimanya?**

## Kenapa perlu

Binance memutuskan akses berdasarkan IP sumber, bukan negara akun. Tiga
kegagalan yang semuanya berakar pada IP:

| Yang terjadi | Tanda | Akibat |
|---|---|---|
| IP dari lokasi terlarang | HTTP 451 `Service unavailable from a restricted location` | Semua endpoint menolak; bot tidak bisa apa-apa dari sisi konfigurasi |
| IP tidak ada di whitelist API key | HTTP 403, kode `-2015` | Order ditolak, pembacaan saldo gagal |
| IP melanggar rate limit | HTTP 418 (auto-ban sementara) | Semua request diblokir sampai ban berakhir |

Tanpa pemeriksaan awal, ketiganya baru terlihat setelah bot menghangatkan
puluhan candle dan mencoba order pertama. Pada mode `live`, kegagalan seperti
itu bisa terjadi tepat ketika posisi baru butuh SL. Lebih murah berhenti di awal.

Catatan penting: 451 adalah keputusan Binance berdasarkan lokasi IP yang mereka
lihat, bukan sesuatu yang bisa diperbaiki dengan mengganti DNS atau mengatur
header. Yang berubah hanya kalau IP keluarnya berpindah (mis. VPS di negara
lain). Beberapa pengguna melaporkan kuota yang sama bisa hidup di
`testnet.binancefuture.com` meski `fapi.binance.com` menolak; itu juga terlihat
di sandbox pengembangan proyek ini (produksi 451, testnet OK).

## Cara pakai

```bash
# cek sekali, tampilkan IP + status di Binance
volumatic-bot ip

# hanya membaca IP publik, tanpa menghubungi Binance
volumatic-bot ip --tanpa-binance
```

Contoh keluaran yang ditolak:

```
mode         : testnet (testnet=False)
target REST  : https://fapi.binance.com

IP publik    : 34.19.24.63 [api.ipify.org]
Binance      : DITOLAK HTTP 451 — ...restricted location...
saran        : Binance menolak lokasi IP ini (HTTP 451). Pindahkan bot ke VPS
               di negara yang dilayani Binance; ...

kesimpulan   : TIDAK LAYAK — ... (exit code 1)
```

Selain perintah khusus itu, hasilnya juga tampil di **dashboard**, pada panel
STATUS BOT:

```text
│ Margin     41.5% / 60%              Cooldown    -                 │
│ IP egress  34.19.24.63 (SG) · Binance OK   Diperiksa   17:55      │
│ Bandwidth  11.40 MB/jam             Bobot       210/2400 (9%)     │
```

Aturan tampilnya:

- Baris ini **hanya muncul bila ada data IP**. Mode `dry_run` dan
  `dashboard --demo` tidak memeriksa IP, jadi panelnya tidak menampilkan baris
  kosong.
- Warna mengikuti hasil: hijau bila Binance menerima, merah bila menolak.
- ``Binance MENOLAK`` berarti IP ini akan gagal saat order; kalau sesi masih
  berjalan, penyebabnya sudah dicatat sebagai insiden di panel KEJADIAN.
- Jam "Diperiksa" adalah waktu pemeriksaan terakhir di zona waktu mesin yang
  menampilkan.

Dashboard **tidak** melakukan koneksi jaringan sendiri. Hasil pemeriksaan
ditulis oleh sesi `run` ke tabel `kv` di SQLite (kunci `ip_egress`), sehingga
`volumatic-bot dashboard` yang berjalan sebagai proses terpisah tetap bisa
menampilkannya tanpa memanggil layanan IP.

Selain perintah khusus dan dashboard, pemeriksaan juga muncul di:

- `netcheck` — baris IP di bagian atas, sebelum uji REST bertahap.
- `preflight` — satu baris "deteksi IP" yang menyebut apakah gerbangnya wajib.
- `run` — diperiksa otomatis di awal, dan diulang tiap
  `ipcheck.ulang_setiap_menit` menit selama sesi berjalan.

## Perilaku saat IP ditolak (fail-closed)

`run` pada mode `testnet`/`live` **berhenti dengan exit code 3** sebelum order
pertama bila IP tidak layak. Bot tidak dipaksa jalan dengan harapan beruntung.

Yang dinilai layak adalah dua hal sekaligus:

1. IP publik berhasil dibaca (`ipcheck.wajib_terbaca`, bawaan `true`), dan
2. Binance menerima ping + server time dari IP itu.

Selama sesi berjalan, IP diperiksa ulang. Bila tiba-tiba ditolak (VPS dengan
floating IP, koneksi rumahan yang IP-nya berganti, container yang dijadwalkan
ulang), bot menghentikan pembukaan posisi baru tetapi **tidak** menutup posisi
yang sedang terbuka. Alasannya: kalau IP diblokir, order penutup juga akan
ditolak dan posisi justru kehilangan pelindung. SL yang sudah terpasang di
bursa tetap berlaku.

## Konfigurasi

```yaml
ipcheck:
  enabled: true
  sumber:
    - "https://api.ipify.org"
    - "https://ifconfig.co/json"
  timeout_sec: 5
  cek_saat_menjalankan: true
  wajib_layak: true
  wajib_terbaca: true
  ulang_setiap_menit: 60
```

| Kunci | Arti |
|---|---|
| `enabled` | Matikan seluruh deteksi (perintah `volumatic-bot ip` tetap bisa dipakai manual) |
| `sumber` | Daftar layanan pengintip IP, hanya HTTPS, dicoba berurutan |
| `wajib_layak` | `false` membuat gerbang hanya memberi peringatan. Tidak disarankan |
| `wajib_terbaca` | `false` cukup menuntut Binance menerima, walau IP tak terbaca |
| `ulang_setiap_menit` | `0` mematikan pemeriksaan ulang selama sesi |

## Batas yang perlu diketahui

- **IP privat tidak relevan.** Kalau mesin membaca `192.168.x.x`, yang dilihat
  Binance adalah IP publik router/VPS. Bot menandainya sebagai privat dan tetap
  menilai dari hasil uji Binance.
- **Klasifikasi geo pihak ketiga bisa berbeda** dari klasifikasi Binance. Karena
  itu status `Binance` (ping + server time) yang menentukan, bukan kolom negara.
- **Proxy dan VPN tidak diperiksa.** Bot tidak mendeteksi apakah koneksi lewat
  proxy; yang diukur hanya IP akhir yang terlihat Binance.
- **Layanan pengintip bisa mati.** Bila semua sumber gagal, IP dilaporkan tidak
  terbaca dan gerbang menolak jalan (selama `wajib_terbaca: true`). Itu sengaja:
  lebih baik berhenti daripada mengirim order dengan asumsi.
- **Bukan pengganti whitelist.** Pemeriksaan ini menunjukkan IP *sekarang*.
  Menambahkannya ke whitelist API key tetap dilakukan manual di Binance.

## Uji yang menjaga

`tests/test_ipcheck.py` (40 test): parsing tiga bentuk jawaban, tebakan bentuk
dari URL, keputusan layak/tidak layak untuk 451, 403, 418, dan gagal jaringan,
penolakan sumber non-HTTPS, gerbang `run` yang menolak dan meneruskan, label IP
di panel status, dan pembacaan dari SQLite oleh dashboard proses terpisah.
Semua offline, tanpa memanggil internet.
