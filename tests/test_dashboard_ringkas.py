"""Test peringkas log panel KEJADIAN.

Panel ini paling sering "penuh" karena pesan bot berulang: ipcheck tiap 60
menit dan refresh simbol tiap jam. Test di sini menjaga dua hal yang mudah
rusak: pesan penting tidak boleh hilang, dan baris hasil harus tetap urut jam.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from volumatic_bot.dashboard.models import EventLine
from volumatic_bot.dashboard.render import events_panel
from volumatic_bot.dashboard.ringkas import ringkas_log, ringkas_pesan

IP_OK = "ipcheck berkala: 182.9.200.16 masih diterima Binance"
REFRESH = (
    "refresh simbol: 26 simbol (+BCHUSDT,KAVAUSDT,MAGMAUSDT / -SHELLUSDT,VVVUSDT,ONUSDT); "
    "simbol berposisi tetap dipantau"
)


# ------------------------------------------------------------------- pesan


def test_refresh_simbol_jadi_jumlah_masuk_keluar() -> None:
    assert ringkas_pesan(REFRESH) == "refresh simbol: 26 (masuk 3, keluar 3)"


def test_refresh_simbol_tanpa_daftar_tetap_ringkas() -> None:
    assert ringkas_pesan("refresh simbol: 12 simbol") == "refresh simbol: 12"


def test_ipcheck_ok_disingkat_tapi_ip_dipertahankan() -> None:
    assert ringkas_pesan(IP_OK) == "ipcheck: 182.9.200.16 OK"


def test_ipcheck_ditolak_ditandai_jelas() -> None:
    teks = "ipcheck berkala: 182.9.200.16 tidak lagi diterima Binance (HTTP 451)"
    assert ringkas_pesan(teks) == "ipcheck: 182.9.200.16 DITOLAK"


def test_pesan_lain_tidak_diubah() -> None:
    asli = "kline ditutup BNBUSDT 4h (tidak ada sinyal)"
    assert ringkas_pesan(asli) == asli


def test_pesan_panjang_dipotong_di_batas_kata() -> None:
    hasil = ringkas_pesan("kata " * 60)
    assert hasil.endswith("…")
    assert len(hasil) <= 96


# --------------------------------------------------------------- penggabungan


def _deret(jumlah: int, *, mulai: int = 4) -> list[EventLine]:
    baris: list[EventLine] = []
    jam = mulai
    for i in range(jumlah):
        baris.append(EventLine(f"{jam:02d}:00:{i:02d}", "INFO", IP_OK))
        baris.append(EventLine(f"{jam:02d}:01:{i:02d}", "INFO", REFRESH))
        jam += 1
    return baris


def test_jendela_nol_tidak_menggabung_pesan_berselang() -> None:
    hasil = ringkas_log(_deret(3))
    assert all("(x" not in b.text for b in hasil)
    assert len(hasil) == 6


def test_jendela_menggabung_pesan_sama_yang_disela() -> None:
    hasil = ringkas_log(_deret(5), jendela_menit=90)
    teks = [b.text for b in hasil]
    assert "ipcheck: 182.9.200.16 OK (x5)" in teks
    # Refresh simbol berubah tiap jam? tidak di sini: isinya sama, jadi digabung.
    assert "refresh simbol: 26 (masuk 3, keluar 3) (x5)" in teks


def test_jam_memakai_kemunculan_terakhir() -> None:
    hasil = ringkas_log(_deret(4), jendela_menit=90)
    ip = next(b for b in hasil if "ipcheck" in b.text)
    assert ip.clock == "07:00:03"


def test_hasil_tetap_urut_menurut_jam() -> None:
    """Regresi: baris gabungan dulu tertinggal di posisi lama sehingga tidak urut."""
    hasil = ringkas_log(_deret(6), jendela_menit=90)
    jam = [b.clock for b in hasil]
    assert jam == sorted(jam)


def test_jarak_lebih_dari_jendela_mulai_kelompok_baru() -> None:
    baris = [
        EventLine("10:00:00", "INFO", IP_OK),
        EventLine("13:00:00", "INFO", IP_OK),
    ]
    hasil = ringkas_log(baris, jendela_menit=90)
    assert len(hasil) == 2
    assert all("(x" not in b.text for b in hasil)


def test_jendela_besar_tetap_menggabung_lintas_jam() -> None:
    baris = [EventLine("10:00:00", "INFO", IP_OK), EventLine("13:00:00", "INFO", IP_OK)]
    hasil = ringkas_log(baris, jendela_menit=600)
    assert len(hasil) == 1
    assert hasil[0].text.endswith("(x2)")


def test_warning_tidak_digabung_secara_bawaan() -> None:
    baris = [
        EventLine("10:00:00", "WARNING", "refresh simbol gagal: TimeoutError: timed out"),
        EventLine("11:00:00", "WARNING", "refresh simbol gagal: TimeoutError: timed out"),
    ]
    hasil = ringkas_log(baris, jendela_menit=90)
    assert len(hasil) == 2, "dua kegagalan tetap dua baris"


def test_warning_boleh_digabung_bila_diminta() -> None:
    baris = [
        EventLine("10:00:00", "WARNING", "gagal X"),
        EventLine("10:30:00", "WARNING", "gagal X"),
    ]
    hasil = ringkas_log(baris, jendela_menit=90, ulang_warning=True)
    assert len(hasil) == 1
    assert hasil[0].text == "gagal X (x2)"


def test_error_dan_info_dengan_teks_sama_tidak_tercampur() -> None:
    baris = [
        EventLine("10:00:00", "INFO", "sesuatu"),
        EventLine("10:01:00", "ERROR", "sesuatu"),
    ]
    hasil = ringkas_log(baris, jendela_menit=90)
    assert [b.level for b in hasil] == ["INFO", "ERROR"]


# ------------------------------------------------------------------- panel


def _render(baris: list[EventLine], *, max_logs: int, jendela: int) -> str:
    buf = io.StringIO()
    Console(file=buf, width=118, force_terminal=False, no_color=True).print(
        events_panel([], baris, max_logs=max_logs, jendela_menit=jendela)
    )
    return buf.getvalue()


def test_panel_memuat_pesan_terbaru_walau_log_penuh_pesan_berulang() -> None:
    baris = _deret(10)
    baris.append(EventLine("15:00:00", "ERROR", "sesi dihentikan: IP tidak lagi diterima Binance"))
    teks = _render(baris, max_logs=8, jendela=90)
    assert "sesi dihentikan" in teks
    assert "(x10)" in teks


def test_panel_tanpa_log_menampilkan_keterangan() -> None:
    teks = _render([], max_logs=8, jendela=90)
    assert "belum ada log" in teks


@pytest.mark.parametrize("jendela", [0, 90])
def test_panel_tidak_pernah_memuat_baris_kosong(jendela: int) -> None:
    teks = _render(_deret(3), max_logs=6, jendela=jendela)
    baris_isi = [b for b in teks.splitlines() if "│" in b and ":" in b]
    assert baris_isi
