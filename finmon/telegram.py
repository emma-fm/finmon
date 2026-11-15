"""Telegram Bot API helpers (free; plain HTTPS, no extra library)."""

from __future__ import annotations

import logging

import requests

from .config import Settings

log = logging.getLogger(__name__)


def _api(settings: Settings, method: str, **params) -> dict:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    resp = requests.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}",
        json=params, timeout=params.get("timeout", 0) + 30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data}")
    return data["result"]


def _chunks(text: str, limit: int = 3800) -> list[str]:
    """Split on paragraph boundaries (then lines) so HTML tags are not cut in half."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        pieces = [block] if len(block) <= limit else block.split("\n")
        for piece in pieces:
            sep = "\n\n" if piece is block else "\n"
            if current and len(current) + len(sep) + len(piece) > limit:
                out.append(current)
                current = ""
            current = f"{current}{sep}{piece}" if current else piece[:limit]
    if current:
        out.append(current)
    return out


def send(settings: Settings, text: str, chat_id: str | int | None = None,
         reply_markup: dict | None = None, html: bool = True) -> int | None:
    """Send a message (HTML by default, split if long). Buttons go on the last chunk. Returns its message id.

    If Telegram rejects the HTML (e.g. a tag got broken), the chunk is re-sent as plain text.
    """
    from .fmt import strip_html

    chat = chat_id or settings.telegram_chat_id
    if not chat:
        log.warning("TELEGRAM_CHAT_ID not set; message not sent:\n%s", text)
        return None
    chunks = _chunks(text) or [""]
    message_id = None
    for i, chunk in enumerate(chunks):
        params: dict = {"chat_id": chat, "text": chunk, "link_preview_options": {"is_disabled": True}}
        if html:
            params["parse_mode"] = "HTML"
        if reply_markup and i == len(chunks) - 1:
            params["reply_markup"] = reply_markup
        try:
            message_id = _api(settings, "sendMessage", **params)["message_id"]
        except RuntimeError as e:
            if not html or "parse" not in str(e).lower():
                raise
            log.warning("Telegram rejected HTML, sending as plain text: %s", e)
            params.pop("parse_mode")
            params["text"] = strip_html(chunk)
            message_id = _api(settings, "sendMessage", **params)["message_id"]
    return message_id


def answer_callback(settings: Settings, callback_id: str, text: str) -> None:
    _api(settings, "answerCallbackQuery", callback_query_id=callback_id, text=text)


def remove_buttons(settings: Settings, chat_id: str | int, message_id: int) -> None:
    _api(settings, "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
         reply_markup={"inline_keyboard": []})


def get_updates(settings: Settings, offset: int | None, timeout: int = 50) -> list[dict]:
    params: dict = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    return _api(settings, "getUpdates", **params)
