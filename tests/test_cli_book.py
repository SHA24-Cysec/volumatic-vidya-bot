"""Test ujung-ke-ujung perintah ``book`` lewat HTTP lokal.

Yang diuji: config → REST (exchangeInfo + depth) → analisis → keputusan filter →
keluaran terminal. Server Binance tetap tidak tersentuh (sandbox diblokir 451).
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml
from aiohttp import web

from volumatic_bot.cli import main

ROOT = Path(__file__).parents[1]
DATA = Path(__file__).parent / "data"


def depth_payload(
    *,
    bid_price: float = 90000.0,
    ask_price: float = 90000.5,
    bid_qty: float = 6.0,
    ask_qty: float = 1.0,
    levels: int = 3,
) -> dict[str, Any]:
    return {
        "lastUpdateId": 4242,
        "E": 1_700_000_000_000,
        "T": 1_700_000_000_000,
        "bids": [
            [f"{bid_price - i * 0.5:.2f}", f"{bid_qty if i == 0 else 1.0}"] for i in range(levels)
        ],
        "asks": [
            [f"{ask_price + i * 0.5:.2f}", f"{ask_qty if i == 0 else 1.0}"] for i in range(levels)
        ],
    }


@pytest.fixture
def fake_binance() -> Any:
    """Server HTTP lokal di thread terpisah.

    ``main()`` memanggil ``asyncio.run()`` sehingga event loop-nya berbeda dari
    loop test. Karena itu server dijalankan di thread sendiri dengan loop
    permanent; ini meniru perilaku proses nyata (server eksternal).
    """
    state: dict[str, Any] = {"depth_calls": 0, "payload": depth_payload()}

    async def exchange_info(_: web.Request) -> web.Response:
        return web.json_response(
            json.loads((DATA / "exchange_info_sample.json").read_text(encoding="utf-8"))
        )

    async def depth(request: web.Request) -> web.Response:
        state["depth_calls"] += 1
        if request.query.get("symbol") != "BTCUSDT":
            return web.json_response({"code": -1121, "msg": "Invalid symbol."}, status=400)
        return web.json_response(state["payload"])

    async def server_time(_: web.Request) -> web.Response:
        return web.json_response({"serverTime": 1_700_000_000_000})

    loop = asyncio.new_event_loop()
    holder: dict[str, Any] = {"ready": threading.Event()}

    def serve_forever() -> None:
        asyncio.set_event_loop(loop)
        app = web.Application()
        app.router.add_get("/fapi/v1/exchangeInfo", exchange_info)
        app.router.add_get("/fapi/v1/depth", depth)
        app.router.add_get("/fapi/v1/time", server_time)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr,index]
        holder["url"] = f"http://127.0.0.1:{port}"
        holder["ready"].set()
        loop.run_forever()
        loop.run_until_complete(runner.cleanup())

    thread = threading.Thread(target=serve_forever, daemon=True)
    thread.start()
    assert holder["ready"].wait(timeout=10), "server uji gagal menyala"
    try:
        yield holder["url"], state
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


def _write_config(
    tmp_path: Path, base_url: str, *, overrides: dict[str, Any] | None = None
) -> Path:
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["net"]["rest_base_url"] = base_url
    raw.setdefault("data", {})["db_path"] = str(tmp_path / "uji.db")
    for section, values in (overrides or {}).items():
        raw.setdefault(section, {}).update(values)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_perintah_book_ada_di_parser() -> None:
    from volumatic_bot.cli import build_parser

    args = build_parser().parse_args(["book", "BTCUSDT", "--side", "short", "--atr-pct", "1.5"])
    assert args.command == "book"
    assert args.symbols == ["BTCUSDT"]
    assert args.side == "short"
    assert args.atr_pct == 1.5
    assert args.allow_reject is False


def test_listen_menerima_flag_with_book() -> None:
    from volumatic_bot.cli import build_parser

    args = build_parser().parse_args(["listen", "--with-book", "--duration", "1"])
    assert args.with_book is True


def test_format_gate_line_menampilkan_status() -> None:
    from volumatic_bot.cli import _format_gate_line
    from volumatic_bot.signals.ranking import Candidate
    from volumatic_bot.streams.kline_feed import SignalEvent

    signal = SignalEvent(
        symbol="BTCUSDT",
        direction="long",
        open_time=1,
        close_price=90000.0,
        upper=90100.0,
        lower=89900.0,
        trail=90000.0,
        atr=300.0,
        atr_pct=0.33,
        cmo=40.0,
        trend=1,
        vol_delta_pct=1.0,
        vol_delta_win_pct=50.0,
        zone_count=2.0,
        score=0.5,
        bars=1500,
    )
    kandidat = Candidate(
        signal=signal,
        book=None,
        trend_component=0.5,
        volume_component=0.5,
        book_component=0.5,
        score=0.6,
    )
    baris = _format_gate_line(kandidat)
    assert "BTCUSDT" in baris
    assert "LAYAK" in baris
    assert "order book tidak dievaluasi" in baris


def test_book_menampilkan_keputusan_diterima(
    fake_binance: Any, tmp_path: Path, capsys: Any
) -> None:
    base_url, state = fake_binance
    config_path = _write_config(tmp_path, base_url)
    code = main(
        [
            "--config",
            str(config_path),
            "book",
            "BTCUSDT",
            "--notional",
            "20",
            "--atr-pct",
            "0.5",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert state["depth_calls"] == 1
    assert "BTCUSDT" in out
    assert "DITERIMA" in out
    assert "spread" in out
    assert "Permintaan HTTP" in out


def test_book_keluar_kode_1_saat_ditolak(fake_binance: Any, tmp_path: Path, capsys: Any) -> None:
    base_url, state = fake_binance
    # spread 2% -> jelas melewati batas config
    state["payload"] = depth_payload(bid_price=88000.0, ask_price=90000.0)
    config_path = _write_config(tmp_path, base_url)
    code = main(["--config", str(config_path), "book", "BTCUSDT"])
    out = capsys.readouterr().out
    assert code == 1
    assert "DITOLAK" in out
    assert "spread" in out


def test_book_allow_reject_tetap_kode_0(fake_binance: Any, tmp_path: Path, capsys: Any) -> None:
    base_url, state = fake_binance
    state["payload"] = depth_payload(bid_price=88000.0, ask_price=90000.0)
    config_path = _write_config(tmp_path, base_url)
    code = main(["--config", str(config_path), "book", "BTCUSDT", "--allow-reject"])
    assert code == 0
    assert "DITOLAK" in capsys.readouterr().out


def test_book_simbol_tidak_dikenal(fake_binance: Any, tmp_path: Path, capsys: Any) -> None:
    base_url, state = fake_binance
    config_path = _write_config(tmp_path, base_url)
    code = main(["--config", str(config_path), "book", "TIDAKADAUSDT", "--allow-reject"])
    out = capsys.readouterr().out
    assert code == 1  # tidak ada di exchangeInfo -> tetap gagal
    assert "tidak ada di exchangeInfo" in out
    assert state["depth_calls"] == 0


def test_book_dengan_filter_mati_tidak_memanggil_depth(
    fake_binance: Any, tmp_path: Path, capsys: Any
) -> None:
    base_url, _state = fake_binance
    config_path = _write_config(tmp_path, base_url, overrides={"orderbook": {"enabled": False}})
    code = main(["--config", str(config_path), "book", "BTCUSDT", "--allow-reject"])
    out = capsys.readouterr().out
    assert code == 0
    assert "filter dimatikan" in out
