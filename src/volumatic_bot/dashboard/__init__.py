"""Dashboard terminal (TAHAP 6).

Empat panel sesuai permintaan pengguna:

1. **Status bot** — mode, ekuitas, laba/rugi harian, batas rugi, jumlah trade,
   posisi, margin, cooldown, bandwidth MB/jam, bobot REST.
2. **Posisi** — posisi aktif (dan riwayat) dengan P/L berjalan, jarak SL,
   serta tanda BE/partial.
3. **Kandidat** — peringkat sinyal terbaru + alasan tidak dipakai.
4. **Kejadian** — insiden terbaru dan ekor log.

Tampilan ringkas satu panel tersedia lewat ``compact=True``.
"""

from __future__ import annotations

from volumatic_bot.dashboard.app import DashboardApp, StateSource
from volumatic_bot.dashboard.logbuffer import RingBufferHandler
from volumatic_bot.dashboard.models import (
    BotStatus,
    CandidateRow,
    DashboardState,
    EventLine,
    PositionRow,
    demo_state,
)
from volumatic_bot.dashboard.render import render_state
from volumatic_bot.dashboard.sources import LocalStateSource

__all__ = [
    "BotStatus",
    "CandidateRow",
    "DashboardApp",
    "DashboardState",
    "EventLine",
    "LocalStateSource",
    "PositionRow",
    "RingBufferHandler",
    "StateSource",
    "demo_state",
    "render_state",
]
