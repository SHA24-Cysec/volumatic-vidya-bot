"""Utilitas proses: cek PID hidup dan kirim sinyal.

Kenapa dipisah: dipakai bersama oleh ``engine/live.py`` (menolak dua sesi pada
mode yang sama) dan ``cli.py`` (perintah ``stop``). Satu implementasi, satu
perilaku.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

__all__ = ["kirim_sinyal", "proses_hidup"]


def proses_hidup(pid: int) -> bool:
    """True bila PID benar-benar masih berjalan.

    Proses **zombie** (sudah mati tetapi belum di-*reap* induknya) dianggap mati.
    Tanpa pemeriksaan ini, ``volumatic-bot stop`` bisa melaporkan "masih berjalan"
    padahal prosesnya sudah berhenti — persis kebingungan yang ingin dihindari.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - proses milik user lain
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        keadaan = stat.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return keadaan != "Z"


def kirim_sinyal(pid: int, sig: int = signal.SIGTERM) -> bool:
    """Kirim sinyal ke proses; ``False`` bila gagal (mis. bukan milik kita)."""
    try:
        os.kill(pid, sig)
    except OSError:
        return False
    return True
