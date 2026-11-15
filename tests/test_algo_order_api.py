"""Test migrasi ke **Algo Order API** (insiden -4120, testnet 16 Nov 2026).

Latar belakang nyata: pada 2025-12-09 Binance memindahkan order kondisional
(STOP_MARKET, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET) dari
``POST /fapi/v1/order`` ke ``POST /fapi/v1/algoOrder``. Di testnet, SL bot
ditolak **-4120** *"Order type not supported for this endpoint"*, lalu penutupan
darurat juga dilaporkan gagal. Dua hal itu diuji di sini tanpa menyentuh bursa:

1. order kondisional dikirim ke endpoint algo dengan parameter yang benar
   (``algoType``, ``triggerPrice``, ``clientAlgoId``);
2. fallback ke endpoint lama tetap tersedia (mode ``auto``) dan bisa dipaksa;
3. respons algo (``algoId``/``algoStatus``) dinormalkan dengan benar;
4. pembatalan mencoba dua endpoint (order biasa & algo);
5. penutupan market memakai ``newOrderRespType=RESULT`` dan tetap dianggap
   berhasil bila bursa menjawab ACK tetapi posisi sudah nol.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from volumatic_bot.binance.errors import BinanceAPIError
from volumatic_bot.binance.trading import (
    CONDITIONAL_TYPES,
    OrderRequest,
    TradingAPI,
    parse_order_result,
)
from volumatic_bot.config import load_config
from volumatic_bot.execution.broker import ExchangePosition, LiveBroker
from volumatic_bot.positions.models import PositionState, TrackedPosition

ROOT = Path(__file__).parents[1]


# ------------------------------------------------------------------ REST palsu
class FakeREST:
    """Merekam semua panggilan dan menjawab sesuai skenario."""

    def __init__(self, *, algo_error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.algo_error = algo_error

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        weight: float = 1,
    ) -> Any:
        self.calls.append(
            {"method": method, "path": path, "params": dict(params or {}), "weight": weight}
        )
        if path == "/fapi/v1/algoOrder" and self.algo_error is not None:
            raise self.algo_error
        if path == "/fapi/v1/algoOrder" and method == "POST":
            p = params or {}
            return {
                "algoId": 2146760,
                "clientAlgoId": p.get("clientAlgoId", "otomatis"),
                "algoType": "CONDITIONAL",
                "orderType": p.get("type"),
                "symbol": p.get("symbol"),
                "side": p.get("side"),
                "positionSide": "BOTH",
                "quantity": p.get("quantity", "0"),
                "algoStatus": "NEW",
                "triggerPrice": p.get("triggerPrice", "0"),
                "price": "0",
                "closePosition": p.get("closePosition") == "true",
                "reduceOnly": p.get("reduceOnly") == "true",
            }
        if path == "/fapi/v1/algoOrder" and method == "DELETE":
            return {"algoId": 2146760, "code": "200", "msg": "success"}
        if path == "/fapi/v1/algoOpenOrders":
            return {"code": 200, "msg": "done"}
        if path == "/fapi/v1/openAlgoOrders":
            return [
                {
                    "algoId": 777,
                    "clientAlgoId": "vbot-sl-algo",
                    "orderType": "STOP_MARKET",
                    "symbol": params.get("symbol") if params else "DOGEUSDT",
                    "side": "SELL",
                    "quantity": "100",
                    "algoStatus": "NEW",
                    "triggerPrice": "0.3444",
                    "closePosition": True,
                }
            ]
        if path == "/fapi/v1/order":
            p = params or {}
            return {
                "symbol": p.get("symbol"),
                "orderId": 555,
                "clientOrderId": p.get("newClientOrderId", "x"),
                "side": p.get("side"),
                "type": p.get("type"),
                "status": "FILLED",
                "origQty": p.get("quantity", "0"),
                "executedQty": p.get("quantity", "0"),
                "avgPrice": "0.35",
                "stopPrice": p.get("stopPrice", "0"),
                "reduceOnly": p.get("reduceOnly") == "true",
                "closePosition": p.get("closePosition") == "true",
            }
        if path == "/fapi/v1/openOrders":
            return []
        raise AssertionError(f"path tidak ditangani test: {path}")


def sl_order(*, close_position: bool = True) -> OrderRequest:
    return OrderRequest(
        symbol="DOGEUSDT",
        side="SELL",
        type="STOP_MARKET",
        stop_price=Decimal("0.3444"),
        close_position=close_position,
        quantity=None if close_position else Decimal("100"),
        reduce_only=not close_position,
        client_order_id="vbot-sl-1",
        purpose="stop_loss",
    )


# ------------------------------------------------------- 1) parameter algo
def test_order_kondisional_dikenali() -> None:
    assert "STOP_MARKET" in CONDITIONAL_TYPES
    assert "TAKE_PROFIT_MARKET" in CONDITIONAL_TYPES
    assert "TRAILING_STOP_MARKET" in CONDITIONAL_TYPES
    assert sl_order().is_conditional
    assert not OrderRequest(symbol="X", side="BUY", type="MARKET").is_conditional


def test_parameter_algo_memakai_triggerprice_dan_clientalgoid() -> None:
    params = sl_order().to_algo_params()
    assert params["algoType"] == "CONDITIONAL"
    assert params["triggerPrice"] == "0.3444"
    assert "stopPrice" not in params
    assert params["clientAlgoId"] == "vbot-sl-1"
    assert "newClientOrderId" not in params
    assert params["closePosition"] == "true"
    # Dokumentasi: quantity & reduceOnly TIDAK boleh bersama closePosition.
    assert "quantity" not in params
    assert "reduceOnly" not in params


def test_parameter_algo_untuk_reduceonly_membawa_quantity() -> None:
    params = sl_order(close_position=False).to_algo_params()
    assert params["quantity"] == "100"
    assert params["reduceOnly"] == "true"
    assert "closePosition" not in params


# ---------------------------------------------------------------- 2) routing
@pytest.mark.asyncio
async def test_auto_mengirim_sl_ke_endpoint_algo() -> None:
    rest = FakeREST()
    api = TradingAPI(rest, conditional_api="auto")  # type: ignore[arg-type]
    hasil = await api.place_order(sl_order())
    path = rest.calls[0]["path"]
    assert path == "/fapi/v1/algoOrder"
    assert rest.calls[0]["weight"] == 0  # dokumentasi: bobot 0
    assert hasil.is_algo is True
    assert hasil.order_id == 2146760  # algoId
    assert hasil.type == "STOP_MARKET"
    assert hasil.active is True
    assert hasil.stop_price == Decimal("0.3444")


@pytest.mark.asyncio
async def test_order_market_tetap_ke_endpoint_lama() -> None:
    rest = FakeREST()
    api = TradingAPI(rest, conditional_api="auto")  # type: ignore[arg-type]
    order = OrderRequest(symbol="DOGEUSDT", side="BUY", type="MARKET", quantity=Decimal("100"))
    await api.place_order(order)
    assert rest.calls[0]["path"] == "/fapi/v1/order"


@pytest.mark.asyncio
async def test_auto_jatuh_ke_endpoint_lama_bila_algo_ditolak() -> None:
    """Bursa/uji yang belum migrasi tetap bisa dipakai (mode auto)."""
    rest = FakeREST(algo_error=BinanceAPIError("Order type not supported", status=400, code=-4120))
    api = TradingAPI(rest, conditional_api="auto")  # type: ignore[arg-type]
    hasil = await api.place_order(sl_order())
    assert [c["path"] for c in rest.calls] == ["/fapi/v1/algoOrder", "/fapi/v1/order"]
    assert hasil.is_algo is False
    assert hasil.status == "FILLED"
    assert rest.calls[1]["params"]["stopPrice"] == "0.3444"


@pytest.mark.asyncio
async def test_mode_algo_tidak_jatuh_ke_endpoint_lama() -> None:
    rest = FakeREST(algo_error=BinanceAPIError("Order type not supported", status=400, code=-4120))
    api = TradingAPI(rest, conditional_api="algo")  # type: ignore[arg-type]
    with pytest.raises(BinanceAPIError):
        await api.place_order(sl_order())
    assert [c["path"] for c in rest.calls] == ["/fapi/v1/algoOrder"]


@pytest.mark.asyncio
async def test_mode_legacy_tidak_menyentuh_algo() -> None:
    rest = FakeREST()
    api = TradingAPI(rest, conditional_api="legacy")  # type: ignore[arg-type]
    await api.place_order(sl_order())
    assert rest.calls[0]["path"] == "/fapi/v1/order"


def test_mode_tidak_dikenal_ditolak() -> None:
    with pytest.raises(ValueError, match="conditional_api"):
        TradingAPI(FakeREST(), conditional_api="campur")  # type: ignore[arg-type]


# ---------------------------------------------------------------- 3) parsing
def test_parse_respons_algo() -> None:
    hasil = parse_order_result(
        {
            "algoId": 2146760,
            "clientAlgoId": "vbot-sl-1",
            "orderType": "TAKE_PROFIT_MARKET",
            "algoStatus": "NEW",
            "quantity": "100",
            "triggerPrice": "0.375",
            "closePosition": False,
            "reduceOnly": True,
        },
        purpose="take_profit",
    )
    assert hasil.is_algo and hasil.active
    assert hasil.order_id == 2146760
    assert hasil.client_order_id == "vbot-sl-1"
    assert hasil.type == "TAKE_PROFIT_MARKET"
    assert hasil.orig_qty == Decimal("100")
    assert hasil.stop_price == Decimal("0.375")
    assert hasil.reduce_only is True


def test_algo_status_working_dianggap_aktif() -> None:
    aktif = parse_order_result({"algoId": 1, "algoStatus": "WORKING"})
    assert aktif.active is True
    selesai = parse_order_result({"algoId": 1, "algoStatus": "CANCELED"})
    assert selesai.active is False


# ------------------------------------------------------------- 4) pembatalan
@pytest.mark.asyncio
async def test_cancel_algo_memakai_algoid_tanpa_symbol() -> None:
    rest = FakeREST()
    api = TradingAPI(rest, conditional_api="auto")  # type: ignore[arg-type]
    await api.cancel_algo_order(algo_id=2146760)
    call = rest.calls[0]
    assert (call["method"], call["path"]) == ("DELETE", "/fapi/v1/algoOrder")
    assert call["params"] == {"algoid": 2146760}  # dokumentasi: tanpa symbol
    assert call["weight"] == 1


@pytest.mark.asyncio
async def test_cancel_algo_butuh_id() -> None:
    with pytest.raises(ValueError, match="algo_id"):
        await TradingAPI(FakeREST()).cancel_algo_order()  # type: ignore[arg-type]


# --------------------------------------------------- 5) broker: cancel & open
class RESTTanpaAlgo(FakeREST):
    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if "algo" in path.lower():
            raise BinanceAPIError("not found", status=404, code=-1)
        return await super().request(method, path, **kwargs)


class RESTAlgoSaja(FakeREST):
    """Meniru bursa nyata: algoId tidak dikenal oleh endpoint order biasa (-2013)."""

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/fapi/v1/order" and method == "DELETE":
            self.calls.append({"method": method, "path": path, "params": {}, "weight": 1})
            raise BinanceAPIError("Order does not exist.", status=400, code=-2013)
        return await super().request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_broker_cancel_mencoba_endpoint_lama_lalu_algo() -> None:
    rest = RESTAlgoSaja()
    broker = LiveBroker(TradingAPI(rest, conditional_api="auto"))  # type: ignore[arg-type]
    ok = await broker.cancel("DOGEUSDT", order_id=2146760)
    assert ok is True
    assert [c["path"] for c in rest.calls] == ["/fapi/v1/order", "/fapi/v1/algoOrder"]
    assert rest.calls[1]["params"] == {"algoid": 2146760}


@pytest.mark.asyncio
async def test_broker_open_orders_menggabungkan_algo() -> None:
    rest = FakeREST()
    broker = LiveBroker(TradingAPI(rest, conditional_api="auto"))  # type: ignore[arg-type]
    daftar = await broker.open_orders("DOGEUSDT")
    assert any(o.is_algo and o.order_id == 777 for o in daftar)


@pytest.mark.asyncio
async def test_broker_open_orders_tetap_jalan_tanpa_endpoint_algo() -> None:
    broker = LiveBroker(TradingAPI(RESTTanpaAlgo(), conditional_api="auto"))  # type: ignore[arg-type]
    daftar = await broker.open_orders("DOGEUSDT")
    assert daftar == []


# ------------------------------------------------- 6) penutupan market (ACK)
class BrokerACK:
    """Broker yang meniru ACK: order diterima, tapi executedQty=0."""

    name = "uji"

    def __init__(self, *, sisa: Decimal | None) -> None:
        self.sisa = sisa
        self.orders: list[OrderRequest] = []

    async def place(self, order: OrderRequest) -> Any:
        self.orders.append(order)
        from volumatic_bot.binance.trading import OrderResult

        return OrderResult(
            symbol=order.symbol,
            order_id=1,
            client_order_id="x",
            side=order.side,
            type=order.type,
            status="NEW",
            orig_qty=order.quantity or Decimal(0),
            executed_qty=Decimal(0),
            avg_price=Decimal(0),
            stop_price=Decimal(0),
            reduce_only=True,
            close_position=False,
            purpose=order.purpose,
        )

    async def position(self, symbol: str) -> ExchangePosition | None:
        if self.sisa is None:
            return None
        return ExchangePosition(
            symbol=symbol,
            qty=self.sisa,
            entry_price=Decimal("0.35"),
            unrealized=0.0,
            leverage=10,
            margin_type="isolated",
        )

    async def status(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("tidak dipakai")

    async def cancel(self, *args: Any, **kwargs: Any) -> bool:  # pragma: no cover
        return True

    async def open_orders(self, symbol: str) -> list[Any]:  # pragma: no cover
        return []


def _posisi() -> TrackedPosition:
    return TrackedPosition(
        symbol="CTSIUSDT",
        side="short",
        state=PositionState.OPEN,
        qty=Decimal("339314"),
        remaining_qty=Decimal("339314"),
        entry_price=Decimal("0.02122"),
        stop_price=Decimal("0.02144"),
        tp1_price=Decimal("0.02089"),
        risk_per_unit=Decimal("0.00022"),
        leverage=10,
        mode="testnet",
        opened_at=0,
        updated_at=0,
    )


@pytest.mark.asyncio
async def test_penutupan_ack_dikonfirmasi_lewat_posisi_bursa(tmp_path: Path) -> None:
    """ACK + posisi sudah nol = BERHASIL, bukan 'TUTUP GAGAL' seperti 16 Nov 2026."""
    from volumatic_bot.execution.executor import TradeExecutor
    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.storage.db import SqliteDatabase

    db = SqliteDatabase(str(tmp_path / "bot.db"))
    db.open()
    store = PositionStore(db)
    pos = _posisi()
    store.save_position(pos)

    broker = BrokerACK(sisa=None)  # bursa: tidak ada posisi tersisa
    eksekutor = TradeExecutor(
        load_config(ROOT / "config.yaml"),
        broker,
        store,
        clock=lambda: 1000,  # type: ignore[arg-type]
    )
    ok = await eksekutor._close_market(pos, reason="protection_failed")
    assert ok is True
    assert pos.state is PositionState.CLOSED
    assert broker.orders[0].new_order_resp_type == "RESULT"
    db.close()


@pytest.mark.asyncio
async def test_penutupan_ack_tetap_gagal_bila_posisi_masih_ada(tmp_path: Path) -> None:
    from volumatic_bot.execution.executor import TradeExecutor
    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.storage.db import SqliteDatabase

    db = SqliteDatabase(str(tmp_path / "bot.db"))
    db.open()
    store = PositionStore(db)
    pos = _posisi()
    store.save_position(pos)

    broker = BrokerACK(sisa=Decimal("339314"))
    eksekutor = TradeExecutor(
        load_config(ROOT / "config.yaml"),
        broker,
        store,
        clock=lambda: 1000,  # type: ignore[arg-type]
    )
    ok = await eksekutor._close_market(pos, reason="protection_failed")
    assert ok is False
    assert pos.state is PositionState.OPEN
    db.close()


# ------------------------------------------------------------------ 7) config
def test_config_memuat_pilihan_endpoint_kondisional() -> None:
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.exchange.conditional_order_api == "auto"
    data = cfg.model_dump()
    data["exchange"]["conditional_order_api"] = "campur"
    with pytest.raises(Exception, match="conditional_order_api"):
        type(cfg)(**data)


# ------------------------------------------------- 8) rekonsiliasi tidak dobel SL
@pytest.mark.asyncio
async def test_reconcile_mengenali_sl_algo_sebagai_sl(tmp_path: Path) -> None:
    """SL yang hidup di Algo Order API tidak boleh dianggap "SL hilang".

    Bursa tidak menyimpan label ``purpose`` bot, jadi kesimpulan harus diambil
    dari tipe order. Kalau tidak, rekonsiliasi memasang SL kedua.
    """
    from volumatic_bot.execution.reconcile import reconcile
    from volumatic_bot.positions.store import PositionStore
    from volumatic_bot.storage.db import SqliteDatabase

    db = SqliteDatabase(str(tmp_path / "bot.db"))
    db.open()
    store = PositionStore(db)
    pos = _posisi()
    store.save_position(pos)

    class BrokerDenganSL(BrokerACK):
        async def open_orders(self, symbol: str) -> list[Any]:
            return [
                parse_order_result(
                    {
                        "algoId": 777,
                        "clientAlgoId": "vbot-sl-algo",
                        "orderType": "STOP_MARKET",
                        "symbol": symbol,
                        "side": "BUY",
                        "algoStatus": "NEW",
                        "triggerPrice": "0.02144",
                        "closePosition": True,
                    }
                )
            ]

    # Posisi short di bursa: qty bertanda negatif (itulah cara bursa menyatakan sisi).
    broker = BrokerDenganSL(sisa=Decimal("-339314"))
    laporan = await reconcile(broker=broker, store=store, symbols=["CTSIUSDT"], mode="testnet")
    jenis = [a.kind for a in laporan.actions]
    assert "missing_stop_loss" not in jenis
    assert jenis == ["stop_loss_ok"]
    assert broker.orders == []  # tidak ada order baru sama sekali
    db.close()
