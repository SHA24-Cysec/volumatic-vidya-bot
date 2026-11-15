"""Penyalur notifikasi: konsol/log (bawaan) dengan penggabungan & pembatasan.

Kenapa ada pembatasan (throttle)?
Kegagalan jaringan yang berulang bisa menghasilkan ratusan pesan identik per
menit. Di terminal itu membuat dashboard tidak terbaca, dan bila kelak kanal
lain ditambahkan, itu juga memboroskan kuota. Jadi:

* pesan dengan :attr:`NotifyEvent.key` sama digabung (dicetak sekali per
  ``dedupe_sec``), dengan hitungan ``(xN)``;
* ada batas ``max_per_minute`` pesan keseluruhan;
* pesan di bawah ``min_level`` dibuang.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import ClassVar, Protocol, runtime_checkable

from volumatic_bot.notify.models import NotifyEvent, NotifyLevel

__all__ = ["ConsoleNotifier", "Notifier", "NotifierDispatcher"]

logger = logging.getLogger("volumatic_bot.notify")


@runtime_checkable
class Notifier(Protocol):
    """Kanal pengiriman notifikasi."""

    name: str

    def send(self, event: NotifyEvent) -> bool:
        """Kirim satu peristiwa. Kembalikan ``False`` bila dibatalkan/dibuang."""
        ...


class ConsoleNotifier:
    """Kanal bawaan: tulis ke logger (dan karena itu juga ke dashboard).

    Level log mengikuti level notifikasi supaya ``grep ERROR`` tetap berguna.
    Kanal ini tidak butuh kredensial dan tidak memakai jaringan.
    """

    name = "console"

    _LEVELS: ClassVar[dict[NotifyLevel, int]] = {
        NotifyLevel.INFO: logging.INFO,
        NotifyLevel.SUCCESS: logging.INFO,
        NotifyLevel.WARNING: logging.WARNING,
        NotifyLevel.CRITICAL: logging.ERROR,
    }

    def __init__(self, *, target: logging.Logger | None = None, with_symbol: bool = True) -> None:
        self._log = target or logger
        self._with_symbol = with_symbol

    def send(self, event: NotifyEvent) -> bool:
        teks = event.format_line()
        if not self._with_symbol and event.symbol:
            teks = teks.replace(f" [{event.symbol}]", "")
        self._log.log(self._LEVELS[event.level], "[NOTIF] %s", teks)
        return True


class NotifierDispatcher:
    """Pintu masuk tunggal untuk semua notifikasi bot.

    Kanal yang tersedia saat ini hanya ``console``. Bila ``telegram_enabled``
    dinyatakan di config, dispatcher memberi **satu** peringatan bahwa kanal itu
    belum diimplementasikan lalu mengabaikannya — tidak diam-diam gagal, dan
    tidak berpura-pura mengirim.
    """

    def __init__(
        self,
        channels: Iterable[Notifier] = (),
        *,
        min_level: NotifyLevel = NotifyLevel.INFO,
        dedupe_sec: float = 60.0,
        max_per_minute: int = 30,
        clock: Callable[[], float] = time.monotonic,
        notify_on_trade: bool = True,
        notify_on_error: bool = True,
        skipped_channels: Iterable[str] = (),
    ) -> None:
        self._channels = list(channels)
        self._min_level = min_level
        self._dedupe_sec = max(0.0, dedupe_sec)
        self._max_per_minute = max(1, max_per_minute)
        self._clock = clock
        self._on_trade = notify_on_trade
        self._on_error = notify_on_error
        self._last_sent: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._window: deque[float] = deque()
        self._warned: set[str] = set()
        self.sent = 0
        self.suppressed = 0
        self.dropped = 0
        for nama in skipped_channels:
            self._warn_skipped(nama)

    # --------------------------------------------------------------- kanal
    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(ch.name for ch in self._channels)

    def add(self, channel: Notifier) -> None:
        self._channels.append(channel)

    def _warn_skipped(self, nama: str) -> None:
        if nama in self._warned:
            return
        self._warned.add(nama)
        logger.warning(
            "kanal notifikasi '%s' belum diimplementasikan pada versi ini; "
            "notifikasi hanya dikirim ke konsol/log",
            nama,
        )

    # ---------------------------------------------------------------- kirim
    def emit(self, event: NotifyEvent) -> bool:
        """Kirim peristiwa bila lolos semua saringan. Kembalikan ``True`` bila terkirim."""
        if event.level < self._min_level:
            self.dropped += 1
            return False
        if not self._on_trade and event.kind in _KIND_TRADE:
            self.dropped += 1
            return False
        if not self._on_error and event.level >= NotifyLevel.WARNING:
            self.dropped += 1
            return False
        if not self._channels:
            self.dropped += 1
            return False

        now = self._clock()
        if self._dedupe_sec > 0:
            terakhir = self._last_sent.get(event.key)
            if terakhir is not None and now - terakhir < self._dedupe_sec:
                self._suppressed[event.key] = self._suppressed.get(event.key, 0) + 1
                self.suppressed += 1
                return False

        self._prune(now)
        if len(self._window) >= self._max_per_minute:
            self.dropped += 1
            logger.debug("notifikasi dibuang: batas %d/menit tercapai", self._max_per_minute)
            return False

        ulang = self._suppressed.pop(event.key, 0)
        if ulang:
            event = NotifyEvent(
                kind=event.kind,
                title=event.title,
                detail=f"{event.detail} (pesan serupa sebelumnya: {ulang}x)".strip(),
                level=event.level,
                symbol=event.symbol,
                created_at=event.created_at,
                dedupe_key=event.dedupe_key,
            )

        terkirim = False
        for ch in self._channels:
            try:
                terkirim = ch.send(event) or terkirim
            # Kanal notifikasi tidak boleh mematikan bot; cukup dicatat.
            except Exception:
                logger.exception("kanal notifikasi '%s' gagal mengirim", ch.name)
        if not terkirim:
            self.dropped += 1
            return False

        self._last_sent[event.key] = now
        self._window.append(now)
        self.sent += 1
        return True

    def emit_many(self, events: Iterable[NotifyEvent]) -> int:
        return sum(1 for ev in events if self.emit(ev))

    def _prune(self, now: float) -> None:
        batas = now - 60.0
        while self._window and self._window[0] < batas:
            self._window.popleft()

    # ---------------------------------------------------------- pembantu
    def info(self, kind_title: str, detail: str = "", *, symbol: str | None = None) -> bool:
        from volumatic_bot.notify.models import NotifyKind

        return self.emit(
            NotifyEvent(kind=NotifyKind.SIGNAL, title=kind_title, detail=detail, symbol=symbol)
        )

    def reset_window(self) -> None:
        """Kosongkan pembatas (dipakai test & pergantian hari trading)."""
        self._window.clear()
        self._last_sent.clear()


#: Jenis peristiwa yang dianggap "perdagangan" (ikut saringan notify_on_trade).
_KIND_TRADE: frozenset[str] = frozenset(
    {
        "signal",
        "entry",
        "partial_tp",
        "break_even",
        "trailing",
        "exit",
        "rejected",
    }
)
