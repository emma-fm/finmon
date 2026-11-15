"""Debug mode: send a report of everything a run processed to Telegram.

Meant for the first weeks, to verify that transactions, categories, Bizum matching and alert
decisions look right. Toggle from Telegram with `/debug on [days]` / `/debug off` (or the CLI).
It switches itself off after the chosen number of days so it can't be forgotten on.

Code anywhere calls `debug.log(conn, "...")`; lines are collected during the run and sent as
one message by `debug.flush()` at the end.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import telegram
from .config import Settings
from .db import kv_get, kv_set

DEFAULT_DAYS = 21
_lines: list[str] = []


def _until(conn: sqlite3.Connection) -> datetime | None:
    value = kv_get(conn, "debug_until")
    return datetime.fromisoformat(value) if value else None


def is_on(conn: sqlite3.Connection) -> bool:
    until = _until(conn)
    return until is not None and until > datetime.now(timezone.utc)


def enable(conn: sqlite3.Connection, days: int = DEFAULT_DAYS) -> datetime:
    until = datetime.now(timezone.utc) + timedelta(days=days)
    kv_set(conn, "debug_until", until.isoformat(timespec="seconds"))
    return until


def disable(conn: sqlite3.Connection) -> None:
    kv_set(conn, "debug_until", "")


def status_text(conn: sqlite3.Connection, settings: Settings) -> str:
    if not is_on(conn):
        return "🐞 Modo debug: desactivado. Actívalo con /debug on [días]"
    until = _until(conn).astimezone(settings.timezone)
    return f"🐞 Modo debug: activado hasta el {until:%d/%m %H:%M}. Desactívalo con /debug off"


def log(conn: sqlite3.Connection, line: str) -> None:
    if is_on(conn):
        _lines.append(line)


def section(conn: sqlite3.Connection, title: str) -> None:
    log(conn, f"\n▸ {title}")


def flush(conn: sqlite3.Connection, settings: Settings, header: str, dry_run: bool = False) -> None:
    """Send the collected report (if debug is on), and announce automatic expiry once."""
    lines = list(_lines)
    _lines.clear()
    until = _until(conn)
    if until is not None and until <= datetime.now(timezone.utc):
        disable(conn)
        if not dry_run:
            telegram.send(settings, "🐞 El modo debug se ha desactivado automáticamente. "
                                    "Vuelve a activarlo con /debug on si lo necesitas.")
        return
    if not lines:
        return
    text = f"🐞 DEBUG — {header}\n" + "\n".join(lines)
    if dry_run:
        print(text)
        return
    try:
        telegram.send(settings, text)
    except Exception as e:
        print(f"(debug report could not be sent to Telegram: {e})\n{text}")
