"""Endpoint *trade* & *account* Binance USDT-M Futures (butuh tanda tangan).

Semua panggilan di sini adalah **order sungguhan** atau perubahan setelan akun.
Karena itu:

* setiap parameter yang dikirim dicatat di :class:`OrderRequest` (bisa diaudit),
* hasilnya dinormalkan ke :class:`OrderResult` (bukan dict mentah yang berubah),
* ``newClientOrderId`` selalu diisi prefix bot supaya order bot mudah dikenali
  saat rekonsiliasi dan tidak tertukar dengan order manual.

Rujukan dokumentasi (snapshot arsip Wayback; SPA tidak bisa dibuka langsung):

* *New Order*, *Query Order*, *Cancel Order*, *Query Current All Open Orders* —
  ``/fapi/v1/order``, ``/fapi/v1/openOrders`` (bobot 1 bila memakai ``symbol``).
* **Algo Order API** (WAJIB sejak 2025-12-09 untuk order kondisional):
  ``POST /fapi/v1/algoOrder`` (bobot 0), ``DELETE /fapi/v1/algoOrder``
  (bobot 1, cukup ``algoid``/``clientalgoid``), ``GET /fapi/v1/openAlgoOrders``
  (bobot 1 dengan ``symbol``, 40 tanpa), ``DELETE /fapi/v1/algoOpenOrders``
  (bobot 1). Snapshot dokumentasi resmi: 2025-12-09 (New Algo Order, Cancel Algo
  Order, Current All Algo Open Orders, Cancel All Algo Open Orders). Sejak
  pemisahan itu, ``STOP_MARKET``/``TAKE_PROFIT_MARKET``/``STOP``/
  ``TAKE_PROFIT``/``TRAILING_STOP_MARKET`` yang dikirim ke ``/fapi/v1/order``
  dijawab **-4120** *"Order type not supported for this endpoint"* — perilaku
  yang benar-benar terjadi di testnet pada 16 November 2026.
* *Change Initial Leverage*, *Change Margin Type*, *Change Position Mode*.
* *Account Information V3* — ``GET /fapi/v3/account`` (bobot 5).
* *Position Information V3* — ``GET /fapi/v3/positionRisk`` (bobot 5).
* *User Data Streams / Connect* (2025-01): ``listenKey`` berlaku **60 menit**,
  ``PUT`` memperpanjang 60 menit, ``POST`` mengembalikan key aktif, ``DELETE``
  menutup stream; koneksi tunggal maksimal **24 jam**.

Catatan jujur: endpoint order **belum** diuji ke Binance (sandbox diblokir 451);
semua uji memakai server HTTP lokal yang meniru bentuk respons dokumentasi.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from volumatic_bot.binance.rest import BinanceREST

__all__ = [
    "CLIENT_ID_PREFIX",
    "OrderRequest",
    "OrderResult",
    "TradingAPI",
    "side_for_entry",
    "side_for_exit",
]

CLIENT_ID_PREFIX = "vbot"

OrderType = Literal[
    "MARKET",
    "LIMIT",
    "STOP_MARKET",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
]

#: Tipe yang sejak 2025-12-09 hanya dilayani ``/fapi/v1/algoOrder``.
CONDITIONAL_TYPES = frozenset(
    {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT", "TRAILING_STOP_MARKET"}
)


def side_for_entry(direction: str) -> str:
    """Sisi order untuk membuka posisi: long = BUY, short = SELL."""
    if direction == "long":
        return "BUY"
    if direction == "short":
        return "SELL"
    raise ValueError(f"arah tidak dikenal: {direction!r}")


def side_for_exit(direction: str) -> str:
    """Sisi order untuk menutup posisi (kebalikan entry)."""
    return "SELL" if direction == "long" else "BUY"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Satu order yang akan dikirim (immutable, aman dicatat ke log/DB)."""

    symbol: str
    side: str
    type: OrderType
    quantity: Decimal | None = None
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reduce_only: bool = False
    close_position: bool = False
    time_in_force: str | None = None
    working_type: str = "CONTRACT_PRICE"
    client_order_id: str | None = None
    #: ``RESULT`` membuat bursa mengembalikan qty terisi langsung (bukan ACK).
    #: Tanpa ini, order MARKET bisa dilaporkan ``executedQty=0`` walau sudah
    #: terisi — penyebab insiden "TUTUP GAGAL" pada 16 November 2026.
    new_order_resp_type: str | None = None
    #: Untuk ``TRAILING_STOP_MARKET`` di endpoint algo (0,1..10 = persen).
    callback_rate: Decimal | None = None
    activation_price: Decimal | None = None
    #: Keterangan internal: ``entry``, ``stop_loss``, ``take_profit``, ``partial_tp``.
    purpose: str = "entry"

    @property
    def is_conditional(self) -> bool:
        """True bila order ini wajib lewat Algo Order API."""
        return self.type in CONDITIONAL_TYPES

    def to_params(self) -> dict[str, Any]:
        """Bentuk query string Binance (nilai dikirim sebagai string desimal)."""
        params: dict[str, Any] = {
            "symbol": self.symbol,
            "side": self.side,
            "type": self.type,
        }
        if self.quantity is not None:
            params["quantity"] = _fmt(self.quantity)
        if self.price is not None:
            params["price"] = _fmt(self.price)
        if self.stop_price is not None:
            params["stopPrice"] = _fmt(self.stop_price)
        if self.time_in_force is not None:
            params["timeInForce"] = self.time_in_force
        if self.reduce_only:
            params["reduceOnly"] = "true"
        if self.close_position:
            params["closePosition"] = "true"
        if self.working_type:
            params["workingType"] = self.working_type
        if self.client_order_id:
            params["newClientOrderId"] = self.client_order_id
        if self.new_order_resp_type:
            params["newOrderRespType"] = self.new_order_resp_type
        if self.callback_rate is not None:
            params["callbackRate"] = _fmt(self.callback_rate)
        if self.activation_price is not None:
            params["activationPrice"] = _fmt(self.activation_price)
        return params

    def to_algo_params(self) -> dict[str, Any]:
        """Bentuk parameter ``POST /fapi/v1/algoOrder`` (Algo Order API).

        Perbedaan yang mudah menjatuhkan implementasi:

        * ``stopPrice`` menjadi **``triggerPrice``**;
        * ``newClientOrderId`` menjadi **``clientAlgoId``**;
        * wajib ada ``algoType=CONDITIONAL``;
        * ``quantity`` tidak boleh dikirim bersama ``closePosition=true``, dan
          ``reduceOnly`` juga tidak boleh bersama ``closePosition``.
        """
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": self.symbol,
            "side": self.side,
            "type": self.type,
        }
        if self.quantity is not None and not self.close_position:
            params["quantity"] = _fmt(self.quantity)
        if self.price is not None:
            params["price"] = _fmt(self.price)
        if self.stop_price is not None:
            params["triggerPrice"] = _fmt(self.stop_price)
        if self.time_in_force is not None:
            params["timeInForce"] = self.time_in_force
        if self.working_type:
            params["workingType"] = self.working_type
        if self.close_position:
            params["closePosition"] = "true"
        elif self.reduce_only:
            params["reduceOnly"] = "true"
        if self.callback_rate is not None:
            params["callbackRate"] = _fmt(self.callback_rate)
        if self.activation_price is not None:
            params["activationPrice"] = _fmt(self.activation_price)
        if self.client_order_id:
            params["clientAlgoId"] = self.client_order_id
        return params

    def describe(self) -> str:
        bagian = [f"{self.type} {self.side}", self.symbol]
        if self.quantity is not None:
            bagian.append(f"qty={_fmt(self.quantity)}")
        if self.price is not None:
            bagian.append(f"price={_fmt(self.price)}")
        if self.stop_price is not None:
            bagian.append(f"stop={_fmt(self.stop_price)}")
        if self.reduce_only:
            bagian.append("reduceOnly")
        if self.close_position:
            bagian.append("closePosition")
        return " ".join(bagian)


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Order yang sudah diterima bursa (atau ditolak dengan alasan)."""

    symbol: str
    order_id: int
    client_order_id: str
    side: str
    type: str
    status: str
    orig_qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal
    stop_price: Decimal
    reduce_only: bool
    close_position: bool
    purpose: str = "entry"
    #: True bila order hidup di Algo Order API (``algoId``, bukan ``orderId``).
    is_algo: bool = False
    #: Laba/rugi terealisasi order ini (diisi broker simulasi; bursa mengirimnya
    #: lewat ``ORDER_TRADE_UPDATE``).
    realized_pnl: Decimal = Decimal(0)
    raw: dict[str, Any] | None = None

    @property
    def filled(self) -> bool:
        return self.status == "FILLED"

    @property
    def active(self) -> bool:
        return self.status in ("NEW", "PARTIALLY_FILLED") or (
            # Algo order memakai ``algoStatus``; nilainya juga "NEW" saat aktif,
            # tetapi bursa bisa mengirim "WORKING" pada masa transisi.
            self.is_algo and self.status in ("WORKING", "TRIGGERED")
        )

    @property
    def remaining_qty(self) -> Decimal:
        return max(Decimal(0), self.orig_qty - self.executed_qty)

    def describe(self) -> str:
        return (
            f"{self.purpose}: {self.type} {self.side} {self.symbol} "
            f"id={self.order_id} status={self.status} "
            f"terisi={self.executed_qty}/{self.orig_qty} @ {self.avg_price}"
        )


def _fmt(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if text else "0"


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # pragma: no cover - defensif
        return Decimal(0)


def parse_order_result(payload: dict[str, Any], *, purpose: str = "entry") -> OrderResult:
    """Normalkan respons order Binance menjadi :class:`OrderResult`.

    Mendukung **dua** bentuk: order biasa (``orderId``/``status``/``stopPrice``)
    dan algo order (``algoId``/``algoStatus``/``triggerPrice``). Tanpa cabang
    kedua, SL yang berhasil dipasang lewat Algo Order API akan terbaca sebagai
    ``orderId=0 status=""`` dan dianggap tidak aktif.
    """
    if "algoId" in payload or "clientAlgoId" in payload:
        return OrderResult(
            symbol=str(payload.get("symbol", "")),
            order_id=int(payload.get("algoId", 0) or 0),
            client_order_id=str(payload.get("clientAlgoId", "")),
            side=str(payload.get("side", "")),
            type=str(payload.get("orderType", payload.get("type", ""))),
            status=str(payload.get("algoStatus", payload.get("status", ""))),
            orig_qty=_dec(payload.get("quantity", 0)),
            executed_qty=_dec(payload.get("executedQty", 0)),
            avg_price=_dec(payload.get("price", 0)),
            stop_price=_dec(payload.get("triggerPrice", 0)),
            reduce_only=bool(payload.get("reduceOnly", False)),
            close_position=bool(payload.get("closePosition", False)),
            purpose=purpose,
            is_algo=True,
            raw=payload,
        )
    return OrderResult(
        symbol=str(payload.get("symbol", "")),
        order_id=int(payload.get("orderId", 0) or 0),
        client_order_id=str(payload.get("clientOrderId", "")),
        side=str(payload.get("side", "")),
        type=str(payload.get("type", "")),
        status=str(payload.get("status", "")),
        orig_qty=_dec(payload.get("origQty", payload.get("quantity", 0))),
        executed_qty=_dec(payload.get("executedQty", 0)),
        avg_price=_dec(payload.get("avgPrice", payload.get("price", 0))),
        stop_price=_dec(payload.get("stopPrice", 0)),
        reduce_only=bool(payload.get("reduceOnly", False)),
        close_position=bool(payload.get("closePosition", False)),
        purpose=purpose,
        raw=payload,
    )


class TradingAPI:
    """Pembungkus tipis endpoint order/akun di atas :class:`BinanceREST`."""

    def __init__(
        self,
        rest: BinanceREST,
        *,
        client_prefix: str = CLIENT_ID_PREFIX,
        conditional_api: str = "auto",
    ) -> None:
        self.rest = rest
        self.client_prefix = client_prefix
        if conditional_api not in ("auto", "algo", "legacy"):
            raise ValueError(f"conditional_api tidak dikenal: {conditional_api!r}")
        #: ``auto`` = coba Algo Order API lalu jatuh ke endpoint lama; ``algo`` =
        #: hanya algo; ``legacy`` = hanya endpoint lama (untuk bursa/uji yang
        #: belum menerapkan pemisahan 2025-12-09).
        self.conditional_api = conditional_api

    # ------------------------------------------------------------- client id
    def client_order_id(self, purpose: str, *, tag: str = "") -> str:
        """ID order ber-prefix bot; maksimal 36 karakter sesuai batas Binance."""
        stamp = int(self.rest.now_ms())
        suffix = f"-{tag}" if tag else ""
        raw = f"{self.client_prefix}-{purpose}-{stamp}{suffix}"
        return raw[:36]

    # ---------------------------------------------------------------- setelan
    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        if not 1 <= leverage <= 125:
            raise ValueError("leverage harus 1..125")
        hasil: dict[str, Any] = await self.rest.request(
            "POST",
            "/fapi/v1/leverage",
            params={"symbol": symbol.upper(), "leverage": leverage},
            signed=True,
            weight=1,
        )
        return hasil

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict[str, Any]:
        """Set margin isolated. Error -4046 berarti sudah isolated (bukan kegagalan)."""
        if margin_type not in ("ISOLATED", "CROSSED"):
            raise ValueError("margin_type harus ISOLATED atau CROSSED")
        hasil: dict[str, Any] = await self.rest.request(
            "POST",
            "/fapi/v1/marginType",
            params={"symbol": symbol.upper(), "marginType": margin_type},
            signed=True,
            weight=1,
        )
        return hasil

    async def set_position_mode(self, *, dual_side: bool = False) -> dict[str, Any]:
        """One-way (``dualSidePosition=false``) sesuai keputusan proyek."""
        hasil: dict[str, Any] = await self.rest.request(
            "POST",
            "/fapi/v1/positionSide/dual",
            params={"dualSidePosition": "true" if dual_side else "false"},
            signed=True,
            weight=1,
        )
        return hasil

    # ----------------------------------------------------------------- order
    async def _place_legacy(self, order: OrderRequest) -> dict[str, Any]:
        payload = await self.rest.request(
            "POST",
            "/fapi/v1/order",
            params=order.to_params(),
            signed=True,
            weight=0,  # order dihitung pada limit ORDERS, bukan REQUEST_WEIGHT
        )
        if not isinstance(payload, dict):
            raise ValueError("respons order bukan objek JSON")
        return payload

    async def _place_algo(self, order: OrderRequest) -> dict[str, Any]:
        payload = await self.rest.request(
            "POST",
            "/fapi/v1/algoOrder",
            params=order.to_algo_params(),
            signed=True,
            weight=0,  # dokumentasi: 0 pada limit IP (x-mbx-used-weight-1m)
        )
        if not isinstance(payload, dict):
            raise ValueError("respons algo order bukan objek JSON")
        return payload

    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Kirim order; order kondisional diarahkan ke Algo Order API.

        Sejak 2025-12-09 Binance menolak ``STOP_MARKET``/``TAKE_PROFIT_MARKET``
        di ``/fapi/v1/order`` dengan **-4120**. Karena itu order kondisional
        selalu dicoba ke ``/fapi/v1/algoOrder`` lebih dulu (kecuali config
        memaksa ``legacy``), dan pada mode ``auto`` kegagalannya jatuh ke
        endpoint lama — supaya tetap jalan di bursa/uji yang belum migrasi.
        """
        if order.is_conditional and self.conditional_api in ("auto", "algo"):
            try:
                payload = await self._place_algo(order)
                return parse_order_result(payload, purpose=order.purpose)
            except Exception:
                if self.conditional_api == "algo":
                    raise
                # auto: endpoint lama sebagai cadangan terakhir
        payload = await self._place_legacy(order)
        return parse_order_result(payload, purpose=order.purpose)

    async def cancel_order(
        self, symbol: str, *, order_id: int | None = None, client_order_id: str | None = None
    ) -> dict[str, Any]:
        if order_id is None and client_order_id is None:
            raise ValueError("butuh order_id atau client_order_id")
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        hasil: dict[str, Any] = await self.rest.request(
            "DELETE", "/fapi/v1/order", params=params, signed=True, weight=1
        )
        return hasil

    async def cancel_algo_order(
        self,
        *,
        algo_id: int | None = None,
        client_algo_id: str | None = None,
    ) -> dict[str, Any]:
        """Batalkan satu algo order (``DELETE /fapi/v1/algoOrder``).

        Dokumentasi hanya mewajibkan salah satu dari ``algoid`` atau
        ``clientalgoid`` — ``symbol`` tidak termasuk parameter endpoint ini.
        """
        if algo_id is None and client_algo_id is None:
            raise ValueError("butuh algo_id atau client_algo_id")
        params: dict[str, Any] = {}
        if algo_id is not None:
            params["algoid"] = algo_id
        if client_algo_id is not None:
            params["clientalgoid"] = client_algo_id
        hasil: dict[str, Any] = await self.rest.request(
            "DELETE", "/fapi/v1/algoOrder", params=params, signed=True, weight=1
        )
        return hasil

    async def open_algo_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Daftar algo order aktif (``GET /fapi/v1/openAlgoOrders``).

        Bobot 1 bila ``symbol`` diisi, **40** bila tidak — jadi selalu isi
        ``symbol`` kalau hanya butuh satu simbol.
        """
        params: dict[str, Any] = {"symbol": symbol.upper()} if symbol else {}
        payload = await self.rest.request(
            "GET",
            "/fapi/v1/openAlgoOrders",
            params=params or None,
            signed=True,
            weight=1 if symbol else 40,
        )
        if isinstance(payload, dict):  # sebagian bursa membungkus dalam {"orders": []}
            payload = payload.get("orders", [])
        if not isinstance(payload, list):
            raise ValueError("respons openAlgoOrders bukan array")
        return payload

    async def cancel_all_algo_open_orders(self, symbol: str) -> dict[str, Any]:
        """Batalkan semua algo order satu simbol (``DELETE /fapi/v1/algoOpenOrders``)."""
        hasil: dict[str, Any] = await self.rest.request(
            "DELETE",
            "/fapi/v1/algoOpenOrders",
            params={"symbol": symbol.upper()},
            signed=True,
            weight=1,
        )
        return hasil

    async def cancel_all_open_orders(self, symbol: str) -> dict[str, Any]:
        hasil: dict[str, Any] = await self.rest.request(
            "DELETE",
            "/fapi/v1/allOpenOrders",
            params={"symbol": symbol.upper()},
            signed=True,
            weight=1,
        )
        return hasil

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else None
        payload = await self.rest.request(
            "GET", "/fapi/v1/openOrders", params=params, signed=True, weight=1 if symbol else 40
        )
        if not isinstance(payload, list):
            raise ValueError("respons openOrders bukan array")
        return payload

    async def query_order(
        self, symbol: str, *, order_id: int | None = None, client_order_id: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        payload = await self.rest.request(
            "GET", "/fapi/v1/order", params=params, signed=True, weight=1
        )
        if not isinstance(payload, dict):
            raise ValueError("respons query order bukan objek JSON")
        return payload

    # ------------------------------------------------------------------ akun
    async def account(self) -> dict[str, Any]:
        payload = await self.rest.request("GET", "/fapi/v3/account", signed=True, weight=5)
        if not isinstance(payload, dict):
            raise ValueError("respons account bukan objek JSON")
        return payload

    async def position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Posisi terbuka (V3). Tanpa ``symbol`` bobotnya jauh lebih besar."""
        params = {"symbol": symbol.upper()} if symbol else None
        payload = await self.rest.request(
            "GET", "/fapi/v3/positionRisk", params=params, signed=True, weight=5
        )
        if not isinstance(payload, list):
            raise ValueError("respons positionRisk bukan array")
        return payload

    async def balance_usdt(self) -> float:
        """Saldo USDT (wallet) dari endpoint akun."""
        payload = await self.account()
        for asset in payload.get("assets", []):
            if asset.get("asset") == "USDT":
                return float(asset.get("walletBalance", payload.get("totalWalletBalance", 0)) or 0)
        return float(payload.get("totalWalletBalance", 0) or 0)

    # ------------------------------------------------------- user data stream
    async def create_listen_key(self) -> str:
        payload = await self.rest.request("POST", "/fapi/v1/listenKey", signed=True, weight=1)
        if not isinstance(payload, dict) or "listenKey" not in payload:
            raise ValueError("respons listenKey tidak berisi 'listenKey'")
        return str(payload["listenKey"])

    async def keepalive_listen_key(self, listen_key: str) -> None:
        await self.rest.request(
            "PUT",
            "/fapi/v1/listenKey",
            params={"listenKey": listen_key},
            signed=True,
            weight=1,
        )

    async def close_listen_key(self, listen_key: str) -> None:
        await self.rest.request(
            "DELETE",
            "/fapi/v1/listenKey",
            params={"listenKey": listen_key},
            signed=True,
            weight=1,
        )
