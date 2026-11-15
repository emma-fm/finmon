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
from .fmt import esc

DEFAULT_DAYS = 21
SUMMARY = "Resumen de la ejecución"
_sections: list[tuple[str, list[str]]] = []  # (title, lines)


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
        return "🐞 <b>Modo debug desactivado</b>\n<i>Actívalo con /debug on (o /debug on 14 para 14 días)</i>"
    until = _until(conn).astimezone(settings.timezone)
    return f"🐞 <b>Modo debug activado</b> hasta el {until:%d/%m %H:%M}\n<i>Desactívalo con /debug off</i>"


def log(conn: sqlite3.Connection, line: str) -> None:
    if is_on(conn):
        if not _sections:
            _sections.append(("General", []))
        _sections[-1][1].append(line)


def section(conn: sqlite3.Connection, title: str) -> None:
    if is_on(conn):
        _sections.append((title, []))


def _render(header: str, sections: list[tuple[str, list[str]]]) -> str:
    """Bold section titles; each section's details in a collapsible quote (tap to expand)."""
    parts = [f"🐞 <b>Debug</b> · {esc(header)}"]
    summary = [sec for sec in sections if sec[0] == SUMMARY]
    for _, lines in summary:  # the run summary stays visible at the top
        parts.append("\n".join(esc(line.strip()) for line in lines))
    for title, lines in (sec for sec in sections if sec[0] != SUMMARY):
        if not lines:
            continue
        shown = lines[:60] + ([f"… y {len(lines) - 60} líneas más"] if len(lines) > 60 else [])
        body = "\n".join(esc(line.strip()) for line in shown)
        parts.append(f"<b>▸ {esc(title)}</b> ({len(lines)})\n<blockquote expandable>{body}</blockquote>")
    return "\n\n".join(parts)


def flush(conn: sqlite3.Connection, settings: Settings, header: str, dry_run: bool = False) -> None:
    """Send the collected report (if debug is on), and announce automatic expiry once."""
    sections = [(t, list(lines)) for t, lines in _sections]
    _sections.clear()
    until = _until(conn)
    if until is not None and until <= datetime.now(timezone.utc):
        disable(conn)
        if not dry_run:
            telegram.send(settings, "🐞 El modo debug se ha desactivado automáticamente. "
                                    "Vuelve a activarlo con /debug on si lo necesitas.")
        return
    if not any(lines for _, lines in sections):
        return
    text = _render(header, sections)
    if dry_run:
        from .fmt import strip_html
        print(strip_html(text))
        return
    try:
        telegram.send(settings, text)
    except Exception as e:
        from .fmt import strip_html
        print(f"(debug report could not be sent to Telegram: {e})\n{strip_html(text)}")
