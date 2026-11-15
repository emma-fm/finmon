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


def send(settings: Settings, text: str, chat_id: str | int | None = None,
         reply_markup: dict | None = None) -> int | None:
    """Send a message (split if long). Buttons go on the last chunk. Returns its message id."""
    chat = chat_id or settings.telegram_chat_id
    if not chat:
        log.warning("TELEGRAM_CHAT_ID not set; message not sent:\n%s", text)
        return None
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    message_id = None
    for i, chunk in enumerate(chunks):
        params: dict = {"chat_id": chat, "text": chunk, "disable_web_page_preview": True}
        if reply_markup and i == len(chunks) - 1:
            params["reply_markup"] = reply_markup
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
