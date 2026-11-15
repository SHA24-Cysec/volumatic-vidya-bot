"""Backtester (TAHAP 7): strategi yang sama, data historis, tanpa jaringan.

Yang **dihitung**: fee taker, slippage, dan funding (data historis bila tersedia,
selain itu tarif tetap yang dinyatakan sebagai asumsi).
Yang **tidak** dihitung: kedalaman order book historis, antrean order, dan
perubahan margin/likuidasi intra-candle.
"""

from __future__ import annotations

from volumatic_bot.backtest.costs import CostModel, FundingSeries, TradeCosts
from volumatic_bot.backtest.engine import BacktestTrade, OpenPosition, TradeManager
from volumatic_bot.backtest.runner import BacktestConfig, BacktestResult, run_backtest

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "BacktestTrade",
    "CostModel",
    "FundingSeries",
    "OpenPosition",
    "TradeCosts",
    "TradeManager",
    "run_backtest",
]
