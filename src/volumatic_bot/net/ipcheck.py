"""Deteksi IP egress (IP publik yang dipakai bot keluar ke internet).

Kenapa ini ada: Binance memutuskan akses berdasarkan IP sumber.

* IP dari negara yang dilarang -> HTTP 451, tanpa kemungkinan perbaikan dari sisi bot.
* IP yang tidak terdaftar di whitelist API key -> HTTP 403 / kode ``-2015``.
* IP yang terus melanggar rate limit -> HTTP 418 (auto-ban sementara).

Semua masalah itu baru ketahuan setelah bot mencoba mengirim order. Modul ini
memeriksanya lebih dulu, lalu ``run`` berhenti sebelum order pertama bila IP
tidak layak (lihat :func:`wajib_layak`).

Dua sumber bukti dipakai, sengaja dipisah:

1. **Layanan IP publik** (ipify, Cloudflare trace) — memberi tahu alamat IP dan
   negara menurut klasifikasi mereka. Ini berguna untuk diagnosis, tetapi
   klasifikasi geo pihak ketiga tidak selalu sama dengan klasifikasi Binance.
2. **Binance sendiri** — ``GET /fapi/v1/ping`` dan ``/fapi/v1/time``. Kalau dua
   endpoint publik ini menjawab, IP ini memang diizinkan Binance untuk mengakses
   API. Inilah bukti yang menentukan; lihat :attr:`IpReport.binance`.

IP privat (10/8, 192.168/16, ...) diabaikan: yang dilihat Binance selalu IP
publik, jadi ``curl ifconfig.me`` dari dalam LAN tidak relevan.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

import aiohttp

__all__ = [
    "SUMBER_BAWAAN",
    "IpReport",
    "SumberIp",
    "ambil_ip_publik",
    "periksa_binance",
    "ringkas_laporan",
    "sumber_dari_url",
    "wajib_layak",
]


@dataclass(frozen=True, slots=True)
class SumberIp:
    """Satu layanan pengintip IP beserta cara membaca jawabannya."""

    nama: str
    url: str
    #: ``"text"`` = isi respons adalah IP mentah; ``"trace"`` = format
    #: ``key=value`` per baris milik Cloudflare; ``"json"`` = ambil ``kunci``.
    bentuk: str = "text"
    kunci: str = ""


#: Urutan percobaan. Layanan teks dipakai lebih dulu karena responsnya paling
#: kecil; Cloudflare trace dipakai sebagai cadangan sekaligus memberi info colo.
SUMBER_BAWAAN: tuple[SumberIp, ...] = (
    SumberIp("ipify", "https://api.ipify.org", "text"),
    SumberIp("cloudflare", "https://www.cloudflare.com/cdn-cgi/trace", "trace"),
    SumberIp("ifconfig.co", "https://ifconfig.co/json", "json", "ip"),
)


@dataclass(slots=True)
class IpReport:
    """Hasil satu pemeriksaan IP."""

    #: IP publik menurut layanan luar (kosong bila semua gagal).
    ip: str = ""
    #: Nama layanan yang berhasil menjawab.
    sumber: str = ""
    #: Negara menurut layanan (kode ISO bila ada).
    negara: str = ""
    #: Kode colo Cloudflare (mis. ``SIN``), hanya bila sumbernya trace.
    colo: str = ""
    #: True bila IP termasuk rentang privat/loopback (tidak akan terlihat bursa).
    privat: bool = False
    #: Catatan kegagalan per layanan, untuk diagnosis.
    gagal: list[str] = field(default_factory=list)
    #: ``True``/``False`` bila Binance sudah diuji, ``None`` bila belum.
    binance: bool | None = None
    #: HTTP status dari Binance saat gagal (451/403/418), bila ada.
    binance_status: int | None = None
    #: Kode error Binance (mis. -2015) bila ada.
    binance_code: str = ""
    #: Pesan ringkas dari Binance.
    binance_pesan: str = ""
    #: Panduan singkat yang bisa langsung dijalankan pengguna.
    saran: list[str] = field(default_factory=list)

    @property
    def terbaca(self) -> bool:
        """Apakah IP publik berhasil diketahui dari minimal satu layanan."""
        return bool(self.ip) or self.privat

    @property
    def layak_jalan(self) -> bool:
        """Keputusan akhir: boleh mengirim order ke bursa atau tidak.

        Fail-closed: tanpa bukti bahwa Binance menerima IP ini, jawabannya
        ``False``. Pemanggil yang ingin lebih longgar bisa memakai
        :func:`wajib_layak` dengan ``config.ipcheck.wajib_terbaca: false``.
        """
        if not self.binance:
            return False
        return self.binance_status not in (403, 418, 451)


def sumber_dari_url(url: str) -> SumberIp:
    """Bentuk :class:`SumberIp` dari URL config.

    Bentuk jawaban ditebak dari URL yang dikenal, lalu dari akhiran ``.json``.
    URL tak dikenal dianggap teks biasa (isi respons = IP mentah).
    """
    url = url.strip()
    if "cdn-cgi/trace" in url:
        return SumberIp("cloudflare", url, "trace")
    if url.endswith(".json") or "ifconfig.co" in url:
        nama = url.split("//", 1)[-1].split("/", 1)[0]
        return SumberIp(nama, url, "json", "ip")
    nama = url.split("//", 1)[-1].split("/", 1)[0]
    return SumberIp(nama, url, "text")


def _parse_balasan(bentuk: str, kunci: str, teks: str) -> tuple[str, str, str]:
    """Ambil ``(ip, negara, colo)`` dari isi respons; kosong bila tidak valid."""
    teks = teks.strip()
    if not teks:
        return "", "", ""
    if bentuk == "json":
        import json

        try:
            data: Any = json.loads(teks)
        except json.JSONDecodeError:
            return "", "", ""
        if not isinstance(data, dict):
            return "", "", ""
        ip = str(data.get(kunci) or data.get("ip") or "").strip()
        negara = str(data.get("country") or data.get("country_iso") or "").strip()
        return ip, negara, ""
    if bentuk == "trace":
        nilai: dict[str, str] = {}
        for baris in teks.splitlines():
            if "=" in baris:
                k, _, v = baris.partition("=")
                nilai[k.strip()] = v.strip()
        return nilai.get("ip", ""), nilai.get("loc", ""), nilai.get("colo", "")
    return teks.split()[0] if teks.split() else "", "", ""


def _valid_ip(teks: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(teks)
    except ValueError:
        return None


async def ambil_ip_publik(
    sumber: tuple[SumberIp, ...] = SUMBER_BAWAAN,
    *,
    timeout_sec: float = 5.0,
) -> IpReport:
    """Tanya beberapa layanan, ``curl ifconfig.me`` versi bot.

    Layanan dicoba berurutan sampai satu berhasil memberi IP yang valid. Semua
    kegagalan dicatat di ``gagal`` supaya pengguna tahu *kenapa* kosong, bukan
    hanya melihat layar tanpa penjelasan.
    """
    laporan = IpReport()
    if not sumber:
        return laporan
    batas = aiohttp.ClientTimeout(total=max(1.0, timeout_sec))
    async with aiohttp.ClientSession(timeout=batas) as sesi:
        for satu in sumber:
            try:
                async with sesi.get(satu.url, allow_redirects=False) as resp:
                    if resp.status != 200:
                        laporan.gagal.append(f"{satu.nama}: HTTP {resp.status}")
                        continue
                    teks = await resp.text()
            except (TimeoutError, aiohttp.ClientError, OSError) as exc:
                laporan.gagal.append(f"{satu.nama}: {type(exc).__name__}")
                continue
            ip, negara, colo = _parse_balasan(satu.bentuk, satu.kunci, teks)
            alamat = _valid_ip(ip)
            if alamat is None:
                laporan.gagal.append(f"{satu.nama}: jawaban bukan IP valid")
                continue
            laporan.sumber = satu.nama
            laporan.negara = negara
            laporan.colo = colo
            laporan.ip = str(alamat)
            laporan.privat = bool(alamat.is_private or alamat.is_loopback)
            break
    return laporan


async def periksa_binance(
    laporan: IpReport,
    *,
    rest: Any,
) -> IpReport:
    """Uji apakah Binance menerima IP ini, memakai klien REST yang sudah ada.

    ``rest`` adalah objek dengan ``await ping()`` dan ``await server_time()``
    (lihat :class:`volumatic_bot.binance.rest.BinanceREST`). Endpoint ini publik
    dan berbobot 1, jadi biayanya kecil, tetapi jawabannya menentukan: 451/403
    berarti Binance menolak IP ini, apa pun kata layanan geo.
    """
    from volumatic_bot.binance.errors import BinanceAPIError

    try:
        await rest.ping()
        await rest.server_time()
    except BinanceAPIError as exc:
        laporan.binance = False
        laporan.binance_status = exc.status
        laporan.binance_code = "" if exc.code is None else str(exc.code)
        laporan.binance_pesan = str(exc)[:300]
    except Exception as exc:
        laporan.binance = False
        laporan.binance_pesan = f"{type(exc).__name__}: {exc}"[:300]
    else:
        laporan.binance = True
        laporan.binance_status = None
        laporan.binance_code = ""
        laporan.binance_pesan = "ping & server time OK"
    _isi_saran(laporan)
    return laporan


def _isi_saran(laporan: IpReport) -> None:
    """Terjemahkan hasil pemeriksaan menjadi langkah yang bisa dijalankan."""
    saran: list[str] = []
    if laporan.binance_status == 451:
        saran.append(
            "Binance menolak lokasi IP ini (HTTP 451). Pindahkan bot ke VPS di "
            "negara yang dilayani Binance; ganti DNS/proxy tidak menolong selama "
            "IP keluarnya sama."
        )
    elif laporan.binance_status == 418:
        saran.append(
            "IP sedang di-ban sementara (HTTP 418) karena rate limit. Hentikan bot, "
            "tunggu (biasanya puluhan menit), lalu turunkan exchange.weight_budget_per_min."
        )
    elif laporan.binance_status == 403 or laporan.binance_code == "-2015":
        saran.append(
            f"IP {laporan.ip or '(tidak terbaca)'} kemungkinan belum masuk whitelist API key. "
            "Tambahkan IP itu di Binance > API Management, atau pakai key tanpa batas IP "
            "hanya untuk membaca."
        )
    elif laporan.binance is None:
        saran.append(
            "Status IP terhadap Binance belum diuji. Jalankan `volumatic-bot ip` "
            "atau `volumatic-bot netcheck` saat jaringan tersedia."
        )
    if laporan.privat:
        saran.append(
            "IP terbaca sebagai alamat privat/loopback. Itu wajar di dalam LAN; yang "
            "dilihat Binance adalah IP publik router/VPS Anda."
        )
    if not laporan.ip and not laporan.privat and laporan.gagal:
        saran.append(
            "Semua layanan pengintip IP gagal dihubungi. Cek DNS/firewall keluar; "
            "deteksi IP tidak bisa disimpulkan dari mesin ini."
        )
    laporan.saran = saran


def wajib_layak(laporan: IpReport, *, wajib_terbaca: bool = True) -> tuple[bool, str]:
    """Keputusan boleh-tidaknya ``run`` pada mode bursa, beserta alasannya.

    ``wajib_terbaca=True`` (bawaan) menuntut dua hal: IP publik terbaca **dan**
    Binance menerimanya. Itu sengaja ketat: bot ini mengirim order dengan uang,
    jadi bukti yang tidak lengkap diperlakukan sebagai gagal.
    """
    if laporan.binance is None:
        return False, "status IP terhadap Binance belum diuji"
    if not laporan.binance:
        alasan = laporan.binance_pesan or "Binance menolak permintaan dari IP ini"
        return False, alasan
    if wajib_terbaca and not laporan.terbaca:
        return False, "IP publik tidak bisa dibaca dari mesin ini"
    return True, "IP diterima Binance"


def ringkas_laporan(laporan: IpReport) -> list[str]:
    """Baris-baris siap cetak untuk CLI (tanpa warna, mudah masuk log)."""
    baris: list[str] = []
    if laporan.privat:
        baris.append(f"IP publik    : (privat) {laporan.ip}")
    elif laporan.ip:
        asal = laporan.sumber or "?"
        ekstra = []
        if laporan.negara:
            ekstra.append(f"negara {laporan.negara}")
        if laporan.colo:
            ekstra.append(f"colo {laporan.colo}")
        suffix = f" [{asal}" + (f", {', '.join(ekstra)}" if ekstra else "") + "]"
        baris.append(f"IP publik    : {laporan.ip}{suffix}")
    else:
        baris.append("IP publik    : TIDAK TERBACA")
    if laporan.binance is True:
        baris.append("Binance      : OK (ping + server time)")
    elif laporan.binance is False:
        status = "" if laporan.binance_status is None else f" HTTP {laporan.binance_status}"
        baris.append(f"Binance      : DITOLAK{status} — {laporan.binance_pesan}")
    else:
        baris.append("Binance      : belum diuji")
    for satu in laporan.gagal:
        baris.append(f"gagal        : {satu}")
    for satu in laporan.saran:
        baris.append(f"saran        : {satu}")
    return baris
