"""Formatting helpers for Telegram messages (HTML parse mode, Spanish conventions)."""

from __future__ import annotations

import html
import re
from datetime import date

CATEGORY_EMOJI = {
    "groceries": "🛒", "eating_out": "🍽️", "transport": "🚌", "fuel": "⛽", "shopping": "🛍️",
    "subscriptions": "🔁", "utilities": "💡", "housing": "🏠", "insurance": "🛡️", "health": "💊",
    "leisure": "🎬", "travel": "✈️", "education": "📚", "personal_care": "💇", "pets": "🐾", "loans": "🏦",
    "gifts_donations": "🎁", "fees": "🧾", "cash": "💶", "taxes": "🏛️", "bizum": "📲",
    "internal_transfer": "🔄", "savings": "🐷", "income": "💰", "other": "•", "uncategorized": "❔",
}

_WEEKDAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
_MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def esc(text: object) -> str:
    """Escape any dynamic text (merchant names, notes...) before putting it in an HTML message."""
    return html.escape(str(text), quote=False)


def eur(amount: float | None, decimals: int = 2, sign: bool = False) -> str:
    """Spanish money format: 1.234,56 €  (sign=True adds + for positive amounts)."""
    if amount is None:
        return "—"
    text = f"{abs(amount):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    prefix = "−" if amount < 0 else ("+" if sign and amount > 0 else "")
    return f"{prefix}{text} €"


def day(d: str | date) -> str:
    """'2026-11-15' -> 'dom 15 nov'."""
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def bar(ratio: float, width: int = 10) -> str:
    """Progress bar for 0..1+ (values above 1 are shown full)."""
    filled = max(0, min(width, round(ratio * width)))
    return "▓" * filled + "░" * (width - filled)


def emoji(category: str | None) -> str:
    return CATEGORY_EMOJI.get(category or "uncategorized", "•")


# --- LLM output -------------------------------------------------------------------------------

_ALLOWED = ("b", "i", "u", "s", "code")


def llm_html(text: str) -> str:
    """Make LLM-written text safe for Telegram HTML.

    Everything is escaped, then only simple formatting tags are re-enabled. Markdown bold/italic that a
    model may still produce (**x**, __x__) is converted, so a different LLM provider can't break messages.
    """
    text = text.strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<i>\1</i>", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.MULTILINE)
    out = html.escape(text, quote=False)
    for tag in _ALLOWED:
        out = out.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return out


def strip_html(text: str) -> str:
    """Plain-text fallback if Telegram rejects the HTML."""
    return html.unescape(re.sub(r"</?[a-z][^>]*>", "", text))


FORMAT_RULES = """Formatting (Telegram HTML): put a short headline on the first line in <b>bold</b>; use <b>…</b> \
for the 1-3 most important figures and <i>…</i> sparingly; separate ideas with a blank line; use "• " for \
lists. No Markdown, no other tags, no tables. Write amounts the Spanish way: 1.234,56 € (or 1.234 € for round \
figures). Dates: every date in the data comes with a ready-made Spanish label (fields "day" / "today_label", \
e.g. "lun 9 nov"); copy those labels exactly and never work out a weekday yourself. If a date has no label, \
write it without the weekday ("9 nov")."""
