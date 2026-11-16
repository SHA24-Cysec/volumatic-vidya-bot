"""Peringkas log untuk panel KEJADIAN dashboard.

Masalah yang diselesaikan: banyak pesan bot bersifat berulang dan panjang.

* ``ipcheck berkala: 182.9.200.16 masih diterima Binance`` dicatat tiap 60 menit;
  kalau IP-nya tidak berubah, isinya sama terus dan mendesak log lain keluar.
* ``refresh simbol: 26 simbol (+A,B,C,... / -D,E,F...)`` bisa ratusan karakter
  padahal yang penting hanya berapa simbol dan berapa yang masuk/keluar.

Dua langkah, sengaja dipisah supaya mudah diuji:

1. :func:`ringkas_pesan` — memendekkan satu pesan (daftar simbol jadi jumlah,
   alamat IP tetap utuh).
2. :func:`ringkas_log` — menggabungkan pesan identik yang berurutan menjadi satu
   baris berpenanda ``(xN)``.

Yang **tidak** dilakukan: menyembunyikan level WARNING/ERROR. Kegagalan yang
sama dua kali tetap dua baris, karena menyembunyikannya justru berbahaya saat
mencari masalah.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from volumatic_bot.dashboard.models import EventLine

__all__ = ["ringkas_log", "ringkas_pesan"]

#: Panjang maksimum teks per baris sebelum dipotong.
PANJANG_MAKS = 96

_LEVEL_PENTING = frozenset({"WARNING", "ERROR", "CRITICAL"})
_PENANDA_ULANG = re.compile(r" \(x(?P<n>\d+)\)$")
_DAFTAR_SIMBOL = re.compile(r"\+(?P<tambah>[A-Z0-9,]+)\s*/\s*-(?P<lepas>[A-Z0-9,]*)")
_REFRESH = re.compile(r"^refresh simbol:\s*(?P<jumlah>\d+)\s*simbol")


def _pendek(teks: str, batas: int = PANJANG_MAKS) -> str:
    """Potong teks di batas kata terdekat, beri elipsis bila terpotong."""
    teks = " ".join(teks.split())
    if len(teks) <= batas:
        return teks
    potong = teks[: batas - 1]
    if " " in potong:
        potong = potong.rsplit(" ", 1)[0]
    return potong + "…"


def _jumlah(teks: str) -> int:
    if not teks or teks == "-":
        return 0
    return len([x for x in teks.split(",") if x and x != "-"])


def ringkas_pesan(teks: str, *, batas: int = PANJANG_MAKS) -> str:
    """Pendekkan satu baris log tanpa kehilangan informasi penting.

    Aturan khusus, dicocokkan dari bentuk pesan yang benar-benar dipakai bot:

    * ``refresh simbol: N simbol (+A,B / -C,D); ...`` menjadi
      ``refresh simbol: N (masuk a, keluar b)``. Daftar namanya dibuang karena
      yang dibaca operator adalah jumlahnya, bukan 26 nama simbol.
    * ``ipcheck berkala: <ip> masih diterima Binance`` menjadi ``ipcheck: <ip> OK``.
      IP tetap ditampilkan karena itu inti pesannya.
    """
    asli = " ".join(teks.split())
    cocok = _REFRESH.match(asli)
    if cocok:
        daftar = _DAFTAR_SIMBOL.search(asli)
        if daftar:
            masuk = _jumlah(daftar.group("tambah"))
            keluar = _jumlah(daftar.group("lepas"))
            return f"refresh simbol: {cocok.group('jumlah')} (masuk {masuk}, keluar {keluar})"
        return f"refresh simbol: {cocok.group('jumlah')}"
    if asli.startswith("ipcheck berkala:"):
        sisa = asli[len("ipcheck berkala:") :].strip()
        ip = sisa.split()[0] if sisa else ""
        if "masih diterima" in asli:
            return f"ipcheck: {ip} OK" if ip else "ipcheck: OK"
        if "tidak lagi diterima" in asli:
            return f"ipcheck: {ip} DITOLAK" if ip else "ipcheck: DITOLAK"
        return _pendek(asli, batas)
    return _pendek(asli, batas)


def _menit(jam: str) -> int | None:
    """Ubah ``HH:MM:SS`` menjadi menit sejak tengah malam (``None`` bila gagal)."""
    bagian = jam.split(":")
    if len(bagian) < 2:
        return None
    try:
        return int(bagian[0]) * 60 + int(bagian[1])
    except ValueError:
        return None


def ringkas_log(
    baris: Iterable[EventLine],
    *,
    batas: int = PANJANG_MAKS,
    ulang_warning: bool = False,
    jendela_menit: int = 0,
) -> list[EventLine]:
    """Ringkas tiap pesan lalu gabungkan pesan yang sama.

    Urutan hasil mengikuti **kemunculan terakhir** tiap baris. Itu penting:
    kalau baris gabungan dibiarkan di posisi lamanya, daftar jadi tidak urut
    menurut jam, dan pada panel yang hanya menampilkan N baris terakhir pesan
    yang justru masih berulang bisa hilang dari layar.

    ``jendela_menit=0`` hanya menggabung pesan yang benar-benar berurutan;
    ``jendela_menit>0`` juga menggabung pesan sama yang disela pesan lain,
    asalkan jaraknya tidak melebihi jendela. Mode kedua yang dibutuhkan log bot,
    karena ``ipcheck`` (tiap 60 menit) dan ``refresh simbol`` (tiap jam) saling
    berselang sehingga penggabungan berurutan tidak pernah terjadi.

    ``ulang_warning=False`` (bawaan) membiarkan baris WARNING/ERROR tetap
    terpisah, masing-masing dengan jamnya sendiri.
    """
    kelompok: list[dict[str, Any]] = []
    terbuka: dict[str, int] = {}

    for idx, item in enumerate(baris):
        pesan = ringkas_pesan(item.text, batas=batas)
        boleh_digabung = ulang_warning or item.level.upper() not in _LEVEL_PENTING
        kunci = pesan
        posisi = terbuka.get(kunci) if boleh_digabung else None
        if posisi is not None:
            grup = kelompok[posisi]
            jarak: int | None = None
            a, b = _menit(str(grup["jam"])), _menit(item.clock)
            if a is not None and b is not None:
                jarak = (b - a) % (24 * 60)
            if jarak is not None and jendela_menit > 0 and jarak > jendela_menit:
                posisi = None  # terlalu jauh: mulai kelompok baru
            elif jarak is not None and jendela_menit == 0 and jarak > 0 and grup["idx"] != idx - 1:
                posisi = None  # mode ketat: hanya yang benar-benar berurutan
        if posisi is not None:
            grup = kelompok[posisi]
            grup["n"] = int(grup["n"]) + 1
            grup["jam"] = item.clock
            grup["idx"] = idx
            continue
        kelompok.append(
            {"teks": pesan, "jam": item.clock, "level": item.level, "n": 1, "idx": idx}
        )
        if boleh_digabung:
            terbuka[kunci] = len(kelompok) - 1

    hasil: list[EventLine] = []
    for grup in sorted(kelompok, key=lambda g: int(g["idx"])):
        teks = str(grup["teks"])
        if int(grup["n"]) > 1:
            teks = f"{teks} (x{grup['n']})"
        hasil.append(EventLine(clock=str(grup["jam"]), level=str(grup["level"]), text=teks))
    return hasil
