"""Mesin trading (TAHAP 7): satu loop yang menyatukan semua modul.

Mesin ini **tidak** membuka koneksi sendiri. Sumber data (kline feed, depth,
user stream) dan broker disuntikkan, sehingga:

* perilaku keputusan bisa diuji penuh tanpa jaringan;
* jalur yang diuji = jalur yang dipakai produksi (``volumatic-bot run``).
"""

from __future__ import annotations

from volumatic_bot.engine.session import (
    SessionReport,
    print_session_report,
    run_offline_session,
    run_offline_session_async,
    session_report,
)
from volumatic_bot.engine.trader import EngineResult, TradingEngine

__all__ = [
    "EngineResult",
    "SessionReport",
    "TradingEngine",
    "print_session_report",
    "run_offline_session",
    "run_offline_session_async",
    "session_report",
]
