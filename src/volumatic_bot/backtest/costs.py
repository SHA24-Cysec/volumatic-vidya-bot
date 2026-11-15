"""Model biaya backtest: fee, slippage, dan funding rate.

Semua biaya di sini **eksplisit dan bisa diaudit**. Tidak ada mode "tanpa biaya"
secara diam-diam: bila funding tidak tersedia, hasilnya dilaporkan dengan kolom
``funding_tersedia: false`` dan angka performa diberi tanda bahwa funding **tidak**
diperhitungkan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["CostModel", "FundingSeries", "TradeCosts"]


@dataclass(frozen=True, slots=True)
class TradeCosts:
    """Biaya satu trade (USDT)."""

    fee_usdt: float = 0.0
    slippage_usdt: float = 0.0
    funding_usdt: float = 0.0

    @property
    def total_usdt(self) -> float:
        return self.fee_usdt + self.slippage_usdt + self.funding_usdt

    def summary(self) -> str:
        return (
            f"fee={self.fee_usdt:.4f} slippage={self.slippage_usdt:.4f} "
            f"funding={self.funding_usdt:+.4f} total={self.total_usdt:.4f} USDT"
        )


@dataclass(frozen=True, slots=True)
class CostModel:
    """Parameter biaya (bawaan diambil dari ``config.yaml``)."""

    taker_bps: float = 5.0
    slippage_bps: float = 2.0
    #: Funding dibayar/diterima per 8 jam sebagai persen dari notional.
    #: Nilai positif = long membayar short (pasar premium).
    funding_rate_8h_pct: float = 0.01
    funding_interval_hours: float = 8.0

    def __post_init__(self) -> None:
        if self.taker_bps < 0 or self.slippage_bps < 0:
            raise ValueError("fee/slippage tidak boleh negatif")
        if self.funding_interval_hours <= 0:
            raise ValueError("funding_interval_hours harus > 0")

    # ------------------------------------------------------------------ fee
    def fee(self, notional: float) -> float:
        """Fee taker satu sisi."""
        return abs(notional) * self.taker_bps / 10_000.0

    def slip(self, notional: float) -> float:
        """Kerugian slippage satu sisi (selalu merugikan, arah apa pun)."""
        return abs(notional) * self.slippage_bps / 10_000.0

    def entry_cost(self, notional: float) -> float:
        """Fee + slippage saat masuk."""
        return self.fee(notional) + self.slip(notional)

    def exit_cost(self, notional: float) -> float:
        """Fee + slippage saat keluar."""
        return self.fee(notional) + self.slip(notional)

    def fill_price(self, price: float, side: str, *, entering: bool) -> float:
        """Harga eksekusi setelah slippage (beli selalu lebih mahal).

        ``side`` adalah arah **posisi** (long/short). Saat masuk long kita beli,
        saat keluar long kita jual; untuk short kebalikannya.
        """
        beli = (side == "long") == entering
        geser = price * self.slippage_bps / 10_000.0
        return price + geser if beli else price - geser

    # -------------------------------------------------------------- funding
    def funding_payment(self, notional: float, side: str, hours: float) -> float:
        """Total funding untuk posisi yang ditahan ``hours`` jam.

        Positif = biaya (mengurangi PnL), negatif = penerimaan.
        """
        if hours <= 0:
            return 0.0
        periode = hours / self.funding_interval_hours
        tarif = self.funding_rate_8h_pct / 100.0 * periode
        # long membayar bila tarif positif; short menerima.
        tanda = 1.0 if side == "long" else -1.0
        return abs(notional) * tarif * tanda


@dataclass(slots=True)
class FundingSeries:
    """Funding rate per 8 jam dari data historis (opsional).

    Binance menerbitkan ``fundingRate`` per 8 jam lewat
    ``GET /fapi/v1/fundingRate``. Bila file itu tidak ada, backtest memakai
    tarif tetap dari :class:`CostModel` dan melaporkannya sebagai asumsi.
    """

    timestamps_ms: list[int] = field(default_factory=list)
    rates_pct: list[float] = field(default_factory=list)
    source: str = "tetap"

    def __len__(self) -> int:
        return len(self.timestamps_ms)

    @classmethod
    def from_rows(cls, rows: list[dict[str, object]], *, source: str = "binance") -> FundingSeries:
        """Bentuk dari daftar ``{fundingTime, fundingRate}`` (rate desimal)."""
        ts: list[int] = []
        rates: list[float] = []

        def _ms(row: dict[str, object]) -> int:
            return int(str(row["fundingTime"]))

        for row in sorted(rows, key=_ms):
            ts.append(_ms(row))
            rates.append(float(str(row["fundingRate"])) * 100.0)
        return cls(timestamps_ms=ts, rates_pct=rates, source=source)

    def cumulative_pct(self, start_ms: int, end_ms: int) -> float | None:
        """Jumlah tarif (persen) yang jatuh di antara dua waktu; ``None`` bila kosong."""
        if not self.timestamps_ms or end_ms <= start_ms:
            return None
        total = 0.0
        kena = 0
        for ts, rate in zip(self.timestamps_ms, self.rates_pct, strict=True):
            if start_ms < ts <= end_ms:
                total += rate
                kena += 1
        return total if kena else 0.0

    def cost_usdt(self, *, notional: float, side: str, start_ms: int, end_ms: int) -> float | None:
        """Biaya funding (USDT) untuk posisi ``side`` selama rentang waktu."""
        total_pct = self.cumulative_pct(start_ms, end_ms)
        if total_pct is None:
            return None
        tanda = 1.0 if side == "long" else -1.0
        return abs(notional) * total_pct / 100.0 * tanda

    @classmethod
    def from_constant(
        cls, *, rate_8h_pct: float, start_ms: int, end_ms: int, interval_hours: float = 8.0
    ) -> FundingSeries:
        """Bangun deret funding tetap untuk rentang waktu tertentu."""
        langkah = int(interval_hours * 3600_000)
        ts = list(range(start_ms + langkah, end_ms + 1, langkah))
        return cls(
            timestamps_ms=ts,
            rates_pct=[rate_8h_pct] * len(ts),
            source="tetap",
        )
