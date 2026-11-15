"""Test perakitan sesi live/testnet (tanpa jaringan sungguhan).

Fokus: bug yang membuat percobaan testnet pertama gagal — ``build_session``
memanggil ``rest.request`` sebelum ``await rest.start()``, sehingga aiohttp
melempar ``RuntimeError: session belum dibuat``. Test di sini menjaga urutannya.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from volumatic_bot.binance.rest import BinanceREST
from volumatic_bot.config import Secrets, load_config
from volumatic_bot.engine.live import build_session

ROOT = Path(__file__).parents[1]


@pytest.fixture()
def config_testnet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["ops"]["mode"] = "testnet"
    dump["exchange"]["testnet"] = True
    dump["data"]["db_path"] = str(tmp_path / "uji.db")
    from volumatic_bot.config import AppConfig

    monkeypatch.setenv("BINANCE_API_KEY", "kunci-uji")
    monkeypatch.setenv("BINANCE_API_SECRET", "rahasia-uji")
    return AppConfig(**dump), Secrets(_env_file=None)


@pytest.mark.asyncio
async def test_session_http_dibuat_sebelum_request_pertama(
    config_testnet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start()`` wajib dipanggil sebelum request apa pun (regresi bug testnet)."""
    config, secrets = config_testnet
    urutan: list[str] = []

    async def start_palsu(self: BinanceREST) -> None:
        urutan.append("start")

    async def close_palsu(self: BinanceREST) -> None:
        urutan.append("close")

    async def request_palsu(self: BinanceREST, method: str, path: str, **kw: Any) -> Any:
        urutan.append("request")
        raise RuntimeError("berhenti di sini (uji)")

    monkeypatch.setattr(BinanceREST, "start", start_palsu)
    monkeypatch.setattr(BinanceREST, "close", close_palsu)
    monkeypatch.setattr(BinanceREST, "request", request_palsu)
    monkeypatch.setattr(BinanceREST, "exchange_info", request_palsu.__get__)  # tak dipakai

    with pytest.raises(RuntimeError, match="berhenti di sini"):
        await build_session(config, secrets, symbols=["DOGEUSDT"])

    assert urutan[0] == "start", urutan
    assert "request" in urutan, urutan
    # saat gagal, koneksi & SQLite harus ditutup
    assert urutan[-1] == "close", urutan


@pytest.mark.asyncio
async def test_mode_salah_ditolak_sebelum_koneksi(config_testnet: Any) -> None:
    """``build_session`` menolak mode dry_run tanpa membuka koneksi apa pun."""
    config, secrets = config_testnet
    dump = config.model_dump()
    dump["ops"]["mode"] = "dry_run"

    from volumatic_bot.config import AppConfig

    with pytest.raises(ValueError, match="testnet/live"):
        await build_session(AppConfig(**dump), secrets)


def test_netcheck_tersedia_di_cli() -> None:
    """``netcheck`` harus terdaftar sebagai subperintah."""
    from volumatic_bot.cli import build_parser

    teks = build_parser().format_help()
    assert "netcheck" in teks


def test_cli_menangani_error_jaringan_tanpa_traceback(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run`` yang gagal karena jaringan memberi pesan jelas + kode keluar 3."""
    from volumatic_bot.cli import main
    from volumatic_bot.engine import live as modul_live

    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["ops"]["mode"] = "testnet"
    dump["exchange"]["testnet"] = True
    dump["data"]["db_path"] = str(tmp_path / "uji.db")
    import yaml

    berkas = tmp_path / "cfg.yaml"
    berkas.write_text(yaml.safe_dump(dump, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("BINANCE_API_KEY", "kunci-uji")
    monkeypatch.setenv("BINANCE_API_SECRET", "rahasia-uji")

    async def gagal(*_: Any, **__: Any) -> int:
        from volumatic_bot.binance.errors import BinanceAPIError

        raise BinanceAPIError("diblokir", status=451, endpoint="/fapi/v1/exchangeInfo")

    monkeypatch.setattr(modul_live, "run_live_session", gagal, raising=False)

    kode = main(["--config", str(berkas), "run", "--symbols", "DOGEUSDT", "--no-dashboard"])
    galat = capsys.readouterr().err
    assert kode == 3
    assert "HTTP 451" in galat
    assert "VPS" in galat
    assert "Traceback" not in galat


# --------------------------------------------------------------- netcheck
def test_host_alternatif_disarankan_saat_host_utama_diblokir(tmp_path: Path) -> None:
    """``netcheck`` harus bisa menunjuk host testnet lain yang masih hidup."""
    from volumatic_bot.binance.urls import Endpoint
    from volumatic_bot.cli import _host_alternatif

    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["exchange"]["testnet"] = True

    from volumatic_bot.config import AppConfig

    dump["net"]["legacy_testnet_rest"] = False
    assert _host_alternatif(AppConfig(**dump)) == Endpoint.LEGACY_TESTNET_REST

    dump["net"]["legacy_testnet_rest"] = True
    assert _host_alternatif(AppConfig(**dump)) == Endpoint.TESTNET_REST


def test_host_alternatif_tidak_untuk_live_atau_url_khusus() -> None:
    """Mode live / URL kustom tidak boleh disarankan host testnet."""
    from volumatic_bot.cli import _host_alternatif
    from volumatic_bot.config import AppConfig

    dump = load_config(ROOT / "config.yaml").model_dump()
    dump["exchange"]["testnet"] = False
    assert _host_alternatif(AppConfig(**dump)) is None

    dump["exchange"]["testnet"] = True
    dump["net"]["rest_base_url"] = "http://127.0.0.1:8080"
    assert _host_alternatif(AppConfig(**dump)) is None


def test_config_bawaan_memakai_host_testnet_yang_terbukti_hidup() -> None:
    """Bawaan repo = testnet.binancefuture.com (demo-fapi 451/403 saat diuji)."""
    config = load_config(ROOT / "config.yaml")
    assert config.net.legacy_testnet_rest is True
    url = config.net.resolve_rest_url(testnet=True)
    assert url == "https://testnet.binancefuture.com"


# ------------------------------------------------- validasi daftar simbol
def _info() -> Any:
    import json

    from volumatic_bot.binance.symbols import parse_exchange_info

    payload = json.loads((ROOT / "tests/data/exchange_info_sample.json").read_text())
    return parse_exchange_info(payload)


def test_simbol_huruf_kecil_dinormalkan() -> None:
    """``run --symbols dogeusdt`` harus diterima sebagai DOGEUSDT."""
    from volumatic_bot.cli import _load  # noqa: F401  (pastikan modul importable)
    from volumatic_bot.engine.live import _saring_simbol

    config = load_config(ROOT / "config.yaml")
    assert _saring_simbol([" dogeusdt "], config, _info()) == ["DOGEUSDT"]


def test_simbol_blacklist_ditolak() -> None:
    """BTCUSDT ada di blacklist bawaan -> harus ditolak dengan pesan jelas."""
    from volumatic_bot.engine.live import _saring_simbol

    config = load_config(ROOT / "config.yaml")
    assert "BTCUSDT" in [b.upper() for b in config.scanner.blacklist]
    with pytest.raises(ValueError, match="blacklist"):
        _saring_simbol(["btcusdt"], config, _info())


def test_simbol_salah_ketik_ditolak_dengan_saran() -> None:
    """Salah ketik harus ditolak, bukan "berjalan" lalu gagal di warm-up."""
    from volumatic_bot.engine.live import _saring_simbol

    config = load_config(ROOT / "config.yaml")
    with pytest.raises(ValueError, match="tidak ada di exchangeInfo"):
        _saring_simbol(["DOGEUSDTT"], config, _info())


def test_daftar_simbol_kosong_ditolak() -> None:
    from volumatic_bot.engine.live import _saring_simbol

    config = load_config(ROOT / "config.yaml")
    with pytest.raises(ValueError, match="kosong"):
        _saring_simbol([], config, _info())


# --------------------------------------------------- label riwayat dashboard
def test_baris_riwayat_ditandai_selesai() -> None:
    """Posisi tertutup tidak boleh tampil seperti posisi terbuka."""
    from decimal import Decimal

    from volumatic_bot.dashboard.models import PositionRow

    tutup = PositionRow(
        symbol="BTCUSDT",
        side="short",
        state="closed",
        qty=Decimal("0.004"),
        remaining_qty=Decimal("0"),
        entry=Decimal("115979.6"),
        stop=Decimal("116496"),
        tp1=Decimal("115205"),
        close_reason="stop_loss",
    )
    assert tutup.active is False
    assert tutup.flags.startswith("selesai:")
    assert "selesai" in tutup.flags_short
    assert tutup.flags_short.startswith("selesai:")

    buka = PositionRow(
        symbol="DOGEUSDT",
        side="long",
        state="open",
        qty=Decimal("100"),
        remaining_qty=Decimal("100"),
        entry=Decimal("0.35"),
        stop=Decimal("0.34"),
        tp1=Decimal("0.37"),
    )
    assert buka.active is True
    assert buka.flags == "-"


def test_bandwidth_mengabaikan_burst_warmup() -> None:
    """Warm-up tidak boleh menggelembungkan angka MB/jam."""
    from volumatic_bot.metrics import Metrics

    jam = [1000.0]
    m = Metrics(clock=lambda: jam[0])
    m.record_http(rx_bytes=900_000)  # burst warm-up
    jam[0] += 60
    m.record_http(rx_bytes=10_000)
    sebelum = m.bandwidth_mb_per_hour()
    m.reset_window()
    sesudah = m.bandwidth_mb_per_hour()
    assert sebelum > sesudah
    assert sesudah == 0.0


# ----------------------------------------------------- mode simbol otomatis
class _HasilScan:
    def __init__(self, simbol: list[str]) -> None:
        self.accepted_symbols = simbol
        self.accepted = []
        self.trading_perpetuals = 528
        self.dropped_by_cap = 3


class _PemindaiPalsu:
    """Pemindai tiruan: hasil bisa diubah antar-panggilan (untuk uji refresh)."""

    def __init__(self, urutan: list[list[str]]) -> None:
        self.urutan = urutan
        self.banyak_panggilan = 0

    async def scan(self, **_: Any) -> _HasilScan:
        hasil = self.urutan[min(self.banyak_panggilan, len(self.urutan) - 1)]
        self.banyak_panggilan += 1
        return _HasilScan(list(hasil))


class _KlienPalsu:
    def __init__(self, simbol: list[str]) -> None:
        self.simbol = list(simbol)
        self.dihentikan = False

    def stop(self) -> None:
        self.dihentikan = True


def _sesi_untuk_refresh(simbol: list[str], pemindai: Any, posisi: list[str]) -> Any:
    """Objek minimal yang cukup untuk menguji _refresh_simbol."""
    from types import SimpleNamespace

    from volumatic_bot.config import load_config

    class _Store:
        def active_positions(self) -> list[Any]:
            return [SimpleNamespace(symbol=s) for s in posisi]

    return SimpleNamespace(
        scanner=pemindai,
        config=load_config(ROOT / "config.yaml"),
        store=_Store(),
        rest=None,
        feed=SimpleNamespace(seed=lambda *_: None),
        info={"symbols": list(simbol)},
    )


@pytest.mark.asyncio
async def test_refresh_simbol_mempertahankan_simbol_berposisi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simbol yang masih punya posisi tidak boleh dilepas dari stream."""
    from volumatic_bot.engine import live as modul

    async def warmup_palsu(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(modul, "_warmup", warmup_palsu)

    sesi = _sesi_untuk_refresh(
        ["DOGEUSDT", "XRPUSDT"], _PemindaiPalsu([["ETHUSDT"]]), posisi=["XRPUSDT"]
    )
    klien = _KlienPalsu(["DOGEUSDT", "XRPUSDT"])
    baru = await modul._refresh_simbol(sesi, klien, _KlienPalsu)

    assert klien.dihentikan is True
    assert baru is not None and set(baru.simbol) == {"ETHUSDT", "XRPUSDT"}
    assert sesi.info["symbols"] == ["ETHUSDT", "XRPUSDT"]


@pytest.mark.asyncio
async def test_refresh_simbol_tanpa_perubahan_tidak_menyambung_ulang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from volumatic_bot.engine import live as modul

    async def warmup_palsu(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(modul, "_warmup", warmup_palsu)
    sesi = _sesi_untuk_refresh(["DOGEUSDT"], _PemindaiPalsu([["DOGEUSDT"]]), posisi=[])
    klien = _KlienPalsu(["DOGEUSDT"])
    hasil = await modul._refresh_simbol(sesi, klien, _KlienPalsu)
    assert hasil is None
    assert klien.dihentikan is False


@pytest.mark.asyncio
async def test_refresh_simbol_hasil_kosong_mempertahankan_daftar_lama() -> None:
    from volumatic_bot.engine import live as modul

    sesi = _sesi_untuk_refresh(["DOGEUSDT"], _PemindaiPalsu([[]]), posisi=[])
    assert await modul._refresh_simbol(sesi, _KlienPalsu(["DOGEUSDT"]), _KlienPalsu) is None
    assert sesi.info["symbols"] == ["DOGEUSDT"]


@pytest.mark.asyncio
async def test_refresh_simbol_gagal_tidak_mematikan_sesi() -> None:
    from volumatic_bot.engine import live as modul

    class _Rusak:
        async def scan(self, **_: Any) -> Any:
            raise RuntimeError("jaringan putus")

    sesi = _sesi_untuk_refresh(["DOGEUSDT"], _Rusak(), posisi=[])
    assert await modul._refresh_simbol(sesi, _KlienPalsu(["DOGEUSDT"]), _KlienPalsu) is None
    assert sesi.info["symbols"] == ["DOGEUSDT"]
