"""Test deteksi IP egress: parsing, keputusan fail-closed, dan gerbang `run`.

Semua test di sini offline: ``aiohttp`` tidak boleh dipanggil ke internet dalam
test, jadi pengambilan IP diuji lewat fungsi parsing dan objek REST palsu.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from volumatic_bot.binance.errors import BinanceAPIError
from volumatic_bot.config import IpCheckConfig
from volumatic_bot.net.ipcheck import (
    IpReport,
    _parse_balasan,
    ambil_ip_publik,
    periksa_binance,
    ringkas_laporan,
    sumber_dari_url,
    wajib_layak,
)

# ------------------------------------------------------------------ parsing


def test_parse_teks_mentah_mengambil_ip_pertama() -> None:
    assert _parse_balasan("text", "", "  203.0.113.9\n") == ("203.0.113.9", "", "")


def test_parse_trace_cloudflare_mengambil_ip_negara_colo() -> None:
    teks = "fl=745f9\nh=www.cloudflare.com\nip=198.51.100.7\nts=1\nloc=SG\ncolo=SIN\n"
    ip, negara, colo = _parse_balasan("trace", "", teks)
    assert ip == "198.51.100.7"
    assert negara == "SG"
    assert colo == "SIN"


def test_parse_json_ifconfig() -> None:
    teks = '{"ip": "192.0.2.44", "country": "Indonesia", "country_iso": "ID"}'
    ip, negara, _ = _parse_balasan("json", "ip", teks)
    assert ip == "192.0.2.44"
    assert negara == "Indonesia"


def test_parse_json_rusak_mengembalikan_kosong() -> None:
    assert _parse_balasan("json", "ip", "{bukan json") == ("", "", "")


def test_parse_teks_kosong() -> None:
    assert _parse_balasan("text", "", "   ") == ("", "", "")


# ------------------------------------------------------ tebak bentuk sumber


def test_sumber_dari_url_mengenali_bentuk_jawaban() -> None:
    assert sumber_dari_url("https://api.ipify.org").bentuk == "text"
    assert sumber_dari_url("https://ifconfig.co/json").bentuk == "json"
    assert sumber_dari_url("https://www.cloudflare.com/cdn-cgi/trace").bentuk == "trace"
    assert sumber_dari_url("https://contoh.test/ip").bentuk == "text"


# ---------------------------------------------------------------- pengambilan


def test_ambil_ip_publik_tanpa_sumber_langsung_kosong() -> None:
    laporan = asyncio.run(ambil_ip_publik(()))
    assert laporan.ip == ""
    assert laporan.binance is None
    assert laporan.terbaca is False


# --------------------------------------------------------------- keputusan


def _laporan(**ubah: Any) -> IpReport:
    dasar: dict[str, Any] = {"ip": "203.0.113.5", "sumber": "ipify", "binance": True}
    dasar.update(ubah)
    return IpReport(**dasar)


def test_ip_diterima_binance_dianggap_layak() -> None:
    layak, alasan = wajib_layak(_laporan())
    assert layak is True
    assert "diterima" in alasan


def test_belum_diuji_binance_tidak_layak() -> None:
    layak, alasan = wajib_layak(_laporan(binance=None))
    assert layak is False
    assert "belum diuji" in alasan


@pytest.mark.parametrize("status", [403, 418, 451])
def test_status_penolakan_binance_tidak_layak(status: int) -> None:
    laporan = _laporan(binance=False, binance_status=status, binance_pesan=f"HTTP {status}")
    assert laporan.layak_jalan is False
    layak, _ = wajib_layak(laporan)
    assert layak is False


def test_status_penolakan_menang_atas_binance_true() -> None:
    """Jangan percaya ``binance=True`` bila statusnya justru 451.

    Kombinasi ini bisa terjadi kalau hasil lama tidak dibersihkan; keputusan
    harus tetap menolak.
    """
    laporan = _laporan(binance=True, binance_status=451)
    assert laporan.layak_jalan is False


def test_ip_tidak_terbaca_tapi_binance_menerima_bisa_dilonggarkan() -> None:
    laporan = _laporan(ip="", sumber="")
    assert wajib_layak(laporan, wajib_terbaca=True)[0] is False
    assert wajib_layak(laporan, wajib_terbaca=False)[0] is True


def test_ip_privat_dianggap_terbaca() -> None:
    laporan = _laporan(ip="192.168.1.10", privat=True)
    assert laporan.terbaca is True
    assert wajib_layak(laporan)[0] is True


# --------------------------------------------------------------- uji binance


class _RestOK:
    async def ping(self) -> bool:
        return True

    async def server_time(self) -> int:
        return 1_700_000_000_000


class _Rest451:
    async def ping(self) -> bool:
        raise BinanceAPIError("restricted location", status=451, code=0, endpoint="/fapi/v1/ping")

    async def server_time(self) -> int:  # pragma: no cover - tidak dipanggil
        return 0


class _Rest403Whitelist:
    async def ping(self) -> bool:
        raise BinanceAPIError(
            "Invalid API-key, IP, or permissions for action.",
            status=403,
            code=-2015,
            endpoint="/fapi/v1/order",
        )

    async def server_time(self) -> int:  # pragma: no cover
        return 0


class _RestRusak:
    async def ping(self) -> bool:
        raise OSError("network unreachable")

    async def server_time(self) -> int:  # pragma: no cover
        return 0


def test_periksa_binance_ok() -> None:
    laporan = asyncio.run(periksa_binance(IpReport(ip="203.0.113.5"), rest=_RestOK()))
    assert laporan.binance is True
    assert laporan.binance_status is None


def test_periksa_binance_451_memberi_saran_lokasi() -> None:
    laporan = asyncio.run(periksa_binance(IpReport(ip="203.0.113.5"), rest=_Rest451()))
    assert laporan.binance is False
    assert laporan.binance_status == 451
    assert any("VPS" in s for s in laporan.saran)
    assert wajib_layak(laporan)[0] is False


def test_periksa_binance_403_menyebut_whitelist_dan_ip() -> None:
    laporan = asyncio.run(
        periksa_binance(IpReport(ip="203.0.113.5"), rest=_Rest403Whitelist())
    )
    assert laporan.binance_status == 403
    assert laporan.binance_code == "-2015"
    saran = " ".join(laporan.saran)
    assert "whitelist" in saran
    assert "203.0.113.5" in saran


def test_periksa_binance_error_jaringan_tidak_crash() -> None:
    laporan = asyncio.run(periksa_binance(IpReport(ip="203.0.113.5"), rest=_RestRusak()))
    assert laporan.binance is False
    assert "OSError" in laporan.binance_pesan


# ----------------------------------------------------------------- laporan


def test_ringkas_laporan_memuat_ip_dan_status() -> None:
    laporan = asyncio.run(periksa_binance(_laporan(negara="ID"), rest=_RestOK()))
    teks = "\n".join(ringkas_laporan(laporan))
    assert "203.0.113.5" in teks
    assert "ipify" in teks
    assert "OK" in teks


def test_ringkas_laporan_saat_ip_tidak_terbaca() -> None:
    laporan = IpReport(gagal=["ipify: HTTP 503"])
    teks = "\n".join(ringkas_laporan(laporan))
    assert "TIDAK TERBACA" in teks
    assert "ipify: HTTP 503" in teks


# ------------------------------------------------------------------- config


def test_config_ipcheck_menolak_sumber_http() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        IpCheckConfig(sumber=["http://api.ipify.org"])


def test_config_ipcheck_bawaan_aman() -> None:
    cfg = IpCheckConfig()
    assert cfg.enabled is True
    assert cfg.wajib_layak is True
    assert all(url.startswith("https://") for url in cfg.sumber)


# ------------------------------------------------- gerbang run (CLI, offline)


def test_gerbang_run_menolak_saat_binance_memblokir(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` tidak boleh lanjut bila IP ditolak; ini inti fail-closed."""
    from volumatic_bot import cli as cli_mod

    config = cli_mod.load_config("config.yaml")
    laporan = IpReport(ip="203.0.113.5", binance=False, binance_status=451, binance_pesan="451")

    async def palsu(config: Any, secrets: Any, *, uji_binance: bool) -> IpReport:
        return laporan

    monkeypatch.setattr(cli_mod, "_periksa_ip", palsu)
    ok, laporan_keluar = cli_mod._lolos_pemeriksaan_ip(config, object(), mode="testnet")
    assert ok is False
    assert laporan_keluar is laporan


def test_gerbang_run_meneruskan_saat_ip_layak(monkeypatch: pytest.MonkeyPatch) -> None:
    from volumatic_bot import cli as cli_mod

    config = cli_mod.load_config("config.yaml")

    async def palsu(config: Any, secrets: Any, *, uji_binance: bool) -> IpReport:
        return IpReport(ip="203.0.113.5", binance=True)

    monkeypatch.setattr(cli_mod, "_periksa_ip", palsu)
    ok, laporan_keluar = cli_mod._lolos_pemeriksaan_ip(config, object(), mode="testnet")
    assert ok is True
    assert laporan_keluar is not None


def test_gerbang_run_bisa_dimatikan_lewat_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from volumatic_bot import cli as cli_mod

    config = cli_mod.load_config("config.yaml")
    longgar = config.model_copy(
        update={"ipcheck": config.ipcheck.model_copy(update={"wajib_layak": False})}
    )

    async def palsu(config: Any, secrets: Any, *, uji_binance: bool) -> IpReport:
        return IpReport(ip="203.0.113.5", binance=False, binance_status=451)

    monkeypatch.setattr(cli_mod, "_periksa_ip", palsu)
    ok, _ = cli_mod._lolos_pemeriksaan_ip(longgar, object(), mode="testnet")
    assert ok is True


def test_cli_ip_tanpa_binance_cetak_ip(capsys: pytest.CaptureFixture[str]) -> None:
    """Perintah ``ip --tanpa-binance`` harus jalan tanpa jaringan sama sekali."""
    from volumatic_bot import cli as cli_mod

    async def palsu(config: Any, secrets: Any, *, uji_binance: bool) -> IpReport:
        assert uji_binance is False
        return IpReport(ip="203.0.113.77", sumber="ipify")

    asli = cli_mod._periksa_ip
    cli_mod._periksa_ip = palsu
    try:
        kode = cli_mod.main(["ip", "--tanpa-binance"])
    finally:
        cli_mod._periksa_ip = asli
    out = capsys.readouterr().out
    assert kode == 0
    assert "203.0.113.77" in out
    assert "LAYAK" not in out


def test_sumber_ip_precedence_tetap_urutan_config() -> None:
    """Sumber dicoba sesuai urutan config; yang pertama valid dipakai."""
    from volumatic_bot.net.ipcheck import SumberIp as S

    urutan = (S("a", "https://a.test", "text"), S("b", "https://b.test", "text"))
    assert [s.nama for s in urutan] == ["a", "b"]


# ------------------------------------------------- tampilan di dashboard


def test_label_ip_belum_diperiksa() -> None:
    from volumatic_bot.dashboard.models import BotStatus

    assert BotStatus().ip_label == "belum diperiksa"


def test_label_ip_ok_dengan_negara() -> None:
    from volumatic_bot.dashboard.models import BotStatus

    st = BotStatus(ip="34.19.24.63", ip_negara="US", ip_binance=True)
    assert st.ip_label == "34.19.24.63 (US) · Binance OK"


def test_label_ip_ditolak() -> None:
    from volumatic_bot.dashboard.models import BotStatus

    st = BotStatus(ip="34.19.24.63", ip_binance=False)
    assert st.ip_label == "34.19.24.63 · Binance MENOLAK"


def test_label_ip_tak_terbaca_tapi_sudah_diuji() -> None:
    from volumatic_bot.dashboard.models import BotStatus

    st = BotStatus(ip="", ip_binance=True)
    assert st.ip_label.startswith("tak terbaca")


def _panel_teks(status: Any, *, wide: bool = True) -> str:
    import io

    from rich.console import Console

    from volumatic_bot.dashboard.render import status_panel

    buf = io.StringIO()
    Console(file=buf, width=110, force_terminal=False, no_color=True).print(
        status_panel(status, wide=wide)
    )
    return buf.getvalue()


def test_panel_status_menampilkan_baris_ip() -> None:
    from volumatic_bot.dashboard.models import BotStatus

    teks = _panel_teks(
        BotStatus(
            mode="testnet",
            ip="34.19.24.63",
            ip_negara="US",
            ip_binance=True,
            ip_diperiksa="2026-11-15T17:55:03",
        )
    )
    assert "IP egress" in teks
    assert "34.19.24.63 (US)" in teks
    assert "17:55" in teks


def test_panel_status_menyembunyikan_baris_ip_tanpa_data() -> None:
    from volumatic_bot.dashboard.models import BotStatus

    teks = _panel_teks(BotStatus(mode="dry_run"))
    assert "IP egress" not in teks


def test_panel_status_menandai_ip_ditolak() -> None:
    from volumatic_bot.dashboard.models import BotStatus

    teks = _panel_teks(BotStatus(mode="testnet", ip="34.19.24.63", ip_binance=False))
    assert "MENOLAK" in teks


def test_jam_ip_mengubah_iso_ke_jam_lokal() -> None:
    from volumatic_bot.dashboard.render import _jam_ip

    # Tanpa zona waktu: dianggap sudah jam lokal pengguna.
    assert _jam_ip("2026-11-15T17:55:03") == "17:55"
    # Dengan zona waktu: dikonversi ke zona mesin yang menampilkan.
    assert len(_jam_ip("2026-11-15T17:55:03+07:00")) == 5
    assert _jam_ip("") == "-"
    assert _jam_ip("bukan-tanggal") == "bukan-tanggal"


def test_sumber_dashboard_membaca_ip_dari_sqlite(tmp_path: Any) -> None:
    """Dashboard proses terpisah hanya punya SQLite, bukan memori sesi."""
    from volumatic_bot.dashboard.sources import LocalStateSource
    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.storage.db import SqliteDatabase

    db = SqliteDatabase(tmp_path / "dash.db")
    db.open()
    try:
        store = PositionStore(db)
        store.set_state("ip_egress", "203.0.113.9|SG|1|2026-11-15T18:01:00+07:00")
        status = LocalStateSource(store, mode="testnet").snapshot().status
        assert status.ip == "203.0.113.9"
        assert status.ip_negara == "SG"
        assert status.ip_binance is True
        assert status.ip_diperiksa == "2026-11-15T18:01:00+07:00"
    finally:
        db.close()


def test_sumber_dashboard_tahan_data_rusak(tmp_path: Any) -> None:
    from volumatic_bot.dashboard.sources import LocalStateSource
    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.storage.db import SqliteDatabase

    db = SqliteDatabase(tmp_path / "dash2.db")
    db.open()
    try:
        store = PositionStore(db)
        store.set_state("ip_egress", "rusak")
        status = LocalStateSource(store, mode="testnet").snapshot().status
        assert status.ip == ""
        assert status.ip_binance is None
    finally:
        db.close()


def test_setstate_kv_upsert(tmp_path: Any) -> None:
    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.storage.db import SqliteDatabase

    db = SqliteDatabase(tmp_path / "kv.db")
    db.open()
    try:
        store = PositionStore(db)
        assert store.get_state("x") is None
        store.set_state("x", "1")
        store.set_state("x", "2")
        assert store.get_state("x") == "2"
    finally:
        db.close()


def test_catat_ip_menulis_ke_dashboard_dan_sqlite(tmp_path: Any) -> None:
    """Fungsi penyambung menulis ke panel dan ke database dalam satu panggilan."""
    from volumatic_bot.dashboard.sources import LocalStateSource
    from volumatic_bot.engine.live import _catat_ip_ke_dashboard
    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.storage.db import SqliteDatabase

    db = SqliteDatabase(tmp_path / "catat.db")
    db.open()
    try:
        store = PositionStore(db)
        src = LocalStateSource(store, mode="testnet")

        class Sesi:
            def __init__(self) -> None:
                self.dashboard = src
                self.store = store

        _catat_ip_ke_dashboard(
            Sesi(), IpReport(ip="198.51.100.4", negara="ID", binance=True)
        )
        status = src.snapshot().status
        assert status.ip == "198.51.100.4"
        assert status.ip_binance is True
        assert store.get_state("ip_egress").startswith("198.51.100.4|ID|1|")
    finally:
        db.close()
