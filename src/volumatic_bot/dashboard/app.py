"""Aplikasi dashboard: pengambil data → render ``rich`` → layar.

Prinsip keamanan operasional: **dashboard tidak boleh mematikan bot**. Galat saat
mengambil data atau saat render hanya ditampilkan sebagai baris galat; loop
trading yang memanggil dashboard tidak ikut gagal.

Mode pakai:

* ``DashboardApp.run()`` — tampilan dinamis (``rich.live.Live``) selama bot
  berjalan. Bawaan memakai alternate screen (layar penuh): frame tidak pernah
  menumpuk di scrollback walau terminal di-resize atau dipindah, dan saat
  berhenti layar kembali ke isi sebelum bot start. Terminal non-interaktif
  (pipe, CI, berkas log) otomatis jatuh ke mode inline.
* ``DashboardApp.render_once()`` — cetak sekali lalu keluar, untuk terminal yang
  tidak mendukung tampilan dinamis, cron, atau log berkas.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace

from rich.console import Console, RenderableType

from volumatic_bot.dashboard.models import BotStatus, DashboardState
from volumatic_bot.dashboard.render import render_state

__all__ = ["DashboardApp", "StateSource"]

logger = logging.getLogger("volumatic_bot.dashboard")

#: Sumber data dashboard: fungsi tanpa argumen yang mengembalikan state.
StateSource = Callable[[], DashboardState]


class DashboardApp:
    """Menjalankan dashboard pada layar penuh atau sekali cetak."""

    def __init__(
        self,
        source: StateSource,
        *,
        console: Console | None = None,
        refresh_sec: float = 1.5,
        compact: bool = False,
        screen: bool = True,
    ) -> None:
        if refresh_sec <= 0:
            raise ValueError("refresh_sec harus > 0")
        self._source = source
        self._console = console or Console()
        self._refresh = refresh_sec
        self._compact = compact
        self._screen = screen
        self.frames = 0
        self.errors = 0

    # ------------------------------------------------------------------ data
    def take(self) -> DashboardState:
        """Ambil state terbaru; galat apa pun dikembalikan sebagai ``state.error``."""
        try:
            return self._source()
        except Exception as exc:
            self.errors += 1
            logger.debug("galat sumber dashboard: %s", exc)
            return DashboardState(status=_status_galat(), error=str(exc))

    def renderable(self, state: DashboardState) -> RenderableType:
        return render_state(state, compact=self._compact, width=self._console.size.width)

    # ------------------------------------------------------------------ mode
    def render_once(self, *, state: DashboardState | None = None) -> str:
        """Cetak satu tampilan dan kembalikan teksnya (tanpa mode layar penuh)."""
        data = state if state is not None else self.take()
        self._console.print(self.renderable(data))
        self.frames += 1
        return self.text_of(data)

    def text_of(self, state: DashboardState) -> str:
        """Hasil render sebagai teks biasa (untuk test/log)."""
        with self._console.capture() as tangkap:
            self._console.print(self.renderable(state))
        return tangkap.get()

    async def run(
        self,
        *,
        iterations: int | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> int:
        """Tampilan dinamis. Berhenti bila ``iterations`` tercapai atau ``stop_event`` aktif.

        Kembalikan jumlah frame yang tergambar. ``KeyboardInterrupt`` (Ctrl+C)
        diperlakukan sebagai berhenti normal, bukan galat.
        """
        from rich.live import Live

        frames = 0
        try:
            with Live(
                self.renderable(self.take()),
                console=self._console,
                refresh_per_second=max(1, int(1 / self._refresh)),
                screen=self._screen and self._console.is_terminal,
                transient=False,
            ) as live:
                frames = 1
                self.frames += 1
                while True:
                    if iterations is not None and frames >= iterations:
                        break
                    if stop_event is not None:
                        if stop_event.is_set():
                            break
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=self._refresh)
                            break
                        except TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(self._refresh)
                    live.update(self.renderable(self.take()))
                    frames += 1
                    self.frames += 1
        except KeyboardInterrupt:  # pragma: no cover - interaksi terminal
            pass
        return frames


def _status_galat() -> BotStatus:
    """Status minimal saat sumber data gagal (dashboard harus tetap tampil)."""
    return replace(BotStatus(), running=False, note="sumber data gagal")
