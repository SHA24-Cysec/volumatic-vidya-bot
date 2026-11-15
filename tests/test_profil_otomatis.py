"""Test deteksi profil otomatis berdasarkan saldo (sekali di awal sesi).

Yang dibuktikan:

1. Pemetaan tiga tingkat beserta batas ambangnya, dan syarat aktif/saldo valid.
2. Pilihan manual (``--profil`` / ``profil_aktif``) selalu menang atas deteksi.
3. Overlay profil TIDAK boleh mengubah setelan ``profil_otomatis`` itu sendiri.
4. ``build_session`` menyusun ulang sesi memakai profil hasil deteksi.
5. Ambang yang terbalik ditolak saat config dimuat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from volumatic_bot.binance.rest import BinanceREST
from volumatic_bot.config import (
    ConfigError,
    ProfilOtomatisConfig,
    Secrets,
    baca_yaml,
    deteksi_profil_otomatis,
    load_config,
    pilih_profil_otomatis,
)
from volumatic_bot.engine.live import build_session

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config.yaml"


# ------------------------------------------------------------- pemetaan saldo
def test_pilih_profil_tiga_tingkat() -> None:
    atur = ProfilOtomatisConfig(aktif=True)
    assert pilih_profil_otomatis(atur, saldo=5.0) == "modal-5usdt"
    assert pilih_profil_otomatis(atur, saldo=9.99) == "modal-5usdt"
    assert pilih_profil_otomatis(atur, saldo=10.0) == "modal-kecil"
    assert pilih_profil_otomatis(atur, saldo=49.99) == "modal-kecil"
    assert pilih_profil_otomatis(atur, saldo=50.0) is None
    assert pilih_profil_otomatis(atur, saldo=120.0) is None


def test_pilih_profil_nonaktif_atau_saldo_tidak_valid() -> None:
    assert pilih_profil_otomatis(ProfilOtomatisConfig(aktif=False), saldo=5.0) is None
    assert pilih_profil_otomatis(ProfilOtomatisConfig(aktif=True), saldo=0.0) is None
    assert pilih_profil_otomatis(ProfilOtomatisConfig(aktif=True), saldo=-1.0) is None


def test_pilih_profil_default_bernama() -> None:
    atur = ProfilOtomatisConfig(aktif=True, profil_default="konservatif")
    assert pilih_profil_otomatis(atur, saldo=500.0) == "konservatif"


# --------------------------------------------------------- deteksi + manual
def test_deteksi_pada_config_proyek() -> None:
    config = load_config(CONFIG)
    assert config.profil_otomatis.aktif is True
    assert deteksi_profil_otomatis(config, CONFIG, profil_cli=None, saldo=5.0) == "modal-5usdt"
    assert deteksi_profil_otomatis(config, CONFIG, profil_cli=None, saldo=30.0) == "modal-kecil"
    assert deteksi_profil_otomatis(config, CONFIG, profil_cli=None, saldo=80.0) is None


def test_deteksi_profil_cli_menang(tmp_path: Path) -> None:
    berkas = _tulis_config(tmp_path)
    config = load_config(berkas)
    hasil = deteksi_profil_otomatis(config, berkas, profil_cli="pilihan-saya", saldo=5.0)
    assert hasil is None


def test_deteksi_profil_aktif_di_file_menang(tmp_path: Path) -> None:
    berkas = _tulis_config(tmp_path, profil_aktif="besar")
    config = load_config(berkas)
    assert deteksi_profil_otomatis(config, berkas, profil_cli=None, saldo=5.0) is None


def test_deteksi_config_hilang_tidak_melempar(tmp_path: Path) -> None:
    config = load_config(CONFIG)
    jalur_hilang = tmp_path / "tidak ada.yaml"
    assert deteksi_profil_otomatis(config, jalur_hilang, profil_cli=None, saldo=5) is None


# ------------------------------------------------------------- pagar overlay
def test_overlay_profil_tidak_bisa_mengubah_profil_otomatis(tmp_path: Path) -> None:
    berkas = tmp_path / "config.yaml"
    berkas.write_text(
        yaml.safe_dump(
            {
                "profil_aktif": "jahat",
                "profil": {
                    "jahat": {
                        "profil_otomatis": {"aktif": False},
                        "risk": {"max_concurrent_positions": 3},
                    }
                },
                "profil_otomatis": {
                    "aktif": True,
                    "ambang_sangat_kecil_usdt": 10,
                    "ambang_kecil_usdt": 50,
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(berkas)
    assert config.profil_otomatis.aktif is True
    assert config.risk.max_concurrent_positions == 3


def test_ambang_terbalik_ditolak(tmp_path: Path) -> None:
    berkas = tmp_path / "config.yaml"
    berkas.write_text(
        yaml.safe_dump(
            {
                "profil_otomatis": {
                    "aktif": True,
                    "ambang_sangat_kecil_usdt": 60,
                    "ambang_kecil_usdt": 50,
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="ambang"):
        load_config(berkas)


# ------------------------------------------------------------ integrasi sesi
EXCHANGE_INFO: dict[str, Any] = {
    "symbols": [
        {
            "symbol": "DOGEUSDT",
            "pair": "DOGEUSDT",
            "contractType": "PERPETUAL",
            "status": "TRADING",
            "baseAsset": "DOGE",
            "quoteAsset": "USDT",
            "pricePrecision": 4,
            "quantityPrecision": 0,
            "orderTypes": ["MARKET", "STOP_MARKET"],
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "tickSize": "0.000010",
                    "minPrice": "0.0001",
                    "maxPrice": "100",
                },
                {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "100000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }
    ],
    "rateLimits": [],
}

KLINES: list[list[Any]] = [
    [
        1_700_000_000_000 + i * 900_000,
        "0.35",
        "0.36",
        "0.34",
        "0.355",
        "123.0",
        "43.0",
        1,
        "0.35",
        "20.0",
        "0.0",
    ]
    for i in range(300)
]


def _tulis_config(tmp_path: Path, *, profil_aktif: str = "") -> Path:
    dump = load_config(CONFIG).model_dump()
    dump["ops"]["mode"] = "testnet"
    dump["exchange"]["testnet"] = True
    dump["data"]["db_path"] = str(tmp_path / "uji.db")
    dump["profil_aktif"] = profil_aktif
    dump["profil"] = {
        "uji-kecil": {
            "exchange": {"default_leverage": 10, "max_leverage": 10},
            "risk": {
                "risk_per_trade_pct": 5.0,
                "allow_aggressive_risk": True,
                "max_concurrent_positions": 1,
                "min_equity_usdt": 5.0,
            },
        },
        "besar": {"risk": {"max_concurrent_positions": 3}},
    }
    dump["profil_otomatis"] = {
        "aktif": True,
        "ambang_sangat_kecil_usdt": 10,
        "ambang_kecil_usdt": 50,
        "profil_sangat_kecil": "uji-kecil",
        "profil_kecil": "uji-kecil",
        "profil_default": "",
    }
    berkas = tmp_path / "config.yaml"
    berkas.write_text(yaml.safe_dump(dump, allow_unicode=True), encoding="utf-8")
    return berkas


@pytest.fixture
def rest_palsu(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch BinanceREST jadi server lokal: exchangeInfo, akun, klines, setelan."""
    state: dict[str, Any] = {"saldo": "25", "panggilan": []}

    async def start_palsu(self: BinanceREST) -> None:
        return None

    async def close_palsu(self: BinanceREST) -> None:
        return None

    async def request_palsu(
        self: BinanceREST,
        method: str,
        path: str,
        *,
        params: Any = None,
        signed: bool = False,
        weight: int = 1,
        retries: int | None = None,
    ) -> Any:
        state["panggilan"].append((method, path))
        if path == "/fapi/v1/exchangeInfo":
            return EXCHANGE_INFO
        if path == "/fapi/v3/account":
            return {"assets": [{"asset": "USDT", "walletBalance": state["saldo"]}]}
        if path in ("/fapi/v1/leverage", "/fapi/v1/marginType", "/fapi/v1/positionSide/dual"):
            return {}
        raise AssertionError(f"path tidak ditangani REST palsu: {path}")

    async def klines_palsu(
        self: BinanceREST,
        symbol: str,
        interval: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        state["panggilan"].append(("GET", "/fapi/v1/klines"))
        return KLINES

    monkeypatch.setattr(BinanceREST, "start", start_palsu)
    monkeypatch.setattr(BinanceREST, "close", close_palsu)
    monkeypatch.setattr(BinanceREST, "request", request_palsu)
    monkeypatch.setattr(BinanceREST, "klines", klines_palsu)
    return state


@pytest.mark.asyncio
async def test_build_session_menerapkan_profil_otomatis(
    tmp_path: Path, rest_palsu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    berkas = _tulis_config(tmp_path)
    monkeypatch.setenv("BINANCE_API_KEY", "kunci-uji")
    monkeypatch.setenv("BINANCE_API_SECRET", "rahasia-uji")
    config = load_config(berkas)
    secrets = Secrets(_env_file=None)

    sesi = await build_session(config, secrets, symbols=["DOGEUSDT"], config_path=berkas)
    try:
        assert sesi.info["profil"] == "uji-kecil"
        assert sesi.info["saldo_awal"] == 25.0
        assert sesi.config.risk.max_concurrent_positions == 1
        assert sesi.config.exchange.default_leverage == 10
        leverage_dipakai = [p for m, p in rest_palsu["panggilan"] if p == "/fapi/v1/leverage"]
        assert leverage_dipakai, "penyiapan leverage harus terjadi dengan profil baru"
    finally:
        await sesi.close()


@pytest.mark.asyncio
async def test_build_session_profil_manual_menang(
    tmp_path: Path, rest_palsu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    berkas = _tulis_config(tmp_path, profil_aktif="besar")
    monkeypatch.setenv("BINANCE_API_KEY", "kunci-uji")
    monkeypatch.setenv("BINANCE_API_SECRET", "rahasia-uji")
    config = load_config(berkas)
    secrets = Secrets(_env_file=None)

    sesi = await build_session(config, secrets, symbols=["DOGEUSDT"], config_path=berkas)
    try:
        assert sesi.info["profil"] == "besar"
        assert sesi.config.risk.max_concurrent_positions == 3
    finally:
        await sesi.close()


@pytest.mark.asyncio
async def test_build_session_modal_normal_tanpa_profil(
    tmp_path: Path, rest_palsu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    berkas = _tulis_config(tmp_path)
    rest_palsu["saldo"] = "80"
    monkeypatch.setenv("BINANCE_API_KEY", "kunci-uji")
    monkeypatch.setenv("BINANCE_API_SECRET", "rahasia-uji")
    config = load_config(berkas)
    secrets = Secrets(_env_file=None)

    sesi = await build_session(config, secrets, symbols=["DOGEUSDT"], config_path=berkas)
    try:
        assert sesi.info["profil"] == ""
        assert sesi.info["saldo_awal"] == 80.0
        assert sesi.config.risk.max_concurrent_positions == 2
        assert sesi.config.exchange.default_leverage == 5
    finally:
        await sesi.close()


def test_config_yaml_proyek_tetap_tanpa_profil_aktif() -> None:
    raw = baca_yaml(CONFIG)
    assert raw["profil_aktif"] == ""
    assert isinstance(raw["profil_otomatis"], dict)
