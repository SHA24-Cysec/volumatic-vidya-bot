"""Notifikasi bot (TAHAP 6).

Kanal bawaan adalah **konsol/log** — tanpa layanan luar, tanpa kredensial, dan
tanpa tambahan pemakaian jaringan. Kanal lain bisa ditambahkan dengan
mengimplementasikan protokol :class:`~volumatic_bot.notify.dispatcher.Notifier`
lalu mendaftarkannya ke :class:`~volumatic_bot.notify.dispatcher.NotifierDispatcher`.
"""

from __future__ import annotations

from volumatic_bot.notify.dispatcher import ConsoleNotifier, Notifier, NotifierDispatcher
from volumatic_bot.notify.events import (
    event_for_candidate,
    event_for_entry,
    event_for_exit,
    event_for_incident,
    event_for_rejected,
)
from volumatic_bot.notify.models import NotifyEvent, NotifyKind, NotifyLevel

__all__ = [
    "ConsoleNotifier",
    "Notifier",
    "NotifierDispatcher",
    "NotifyEvent",
    "NotifyKind",
    "NotifyLevel",
    "event_for_candidate",
    "event_for_entry",
    "event_for_exit",
    "event_for_incident",
    "event_for_rejected",
]
