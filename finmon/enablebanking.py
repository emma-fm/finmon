"""Thin client for the Enable Banking API (PSD2 account information).

Docs: https://enablebanking.com/docs/api/reference/
"""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import jwt
import requests

API = "https://api.enablebanking.com"


class EnableBankingError(RuntimeError):
    pass


class EnableBanking:
    def __init__(self, app_id: str, private_key_path: Path):
        if not app_id:
            raise EnableBankingError("EB_APP_ID is not set")
        if not private_key_path.exists():
            raise EnableBankingError(f"Private key not found at {private_key_path}")
        self.app_id = app_id
        self._key = private_key_path.read_bytes()
        self._session = requests.Session()

    def _token(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600},
            self._key,
            algorithm="RS256",
            headers={"kid": self.app_id},
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        resp = self._session.request(method, API + path, headers=headers, timeout=60, **kwargs)
        if resp.status_code >= 400:
            raise EnableBankingError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    # --- discovery -------------------------------------------------------
    def aspsps(self, country: str) -> list[dict]:
        return self._request("GET", "/aspsps", params={"country": country, "psu_type": "personal"})["aspsps"]

    # --- consent flow ----------------------------------------------------
    def start_auth(self, aspsp_name: str, country: str, redirect_url: str, valid_for: timedelta) -> dict:
        valid_until = datetime.now(timezone.utc) + valid_for
        body = {
            "access": {"valid_until": valid_until.isoformat(timespec="seconds")},
            "aspsp": {"name": aspsp_name, "country": country},
            "state": str(uuid.uuid4()),
            "redirect_url": redirect_url,
            "psu_type": "personal",
        }
        return self._request("POST", "/auth", json=body)

    def create_session(self, code: str) -> dict:
        return self._request("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/sessions/{session_id}")

    # --- data --------------------------------------------------------------
    def balances(self, account_uid: str) -> list[dict]:
        return self._request("GET", f"/accounts/{account_uid}/balances")["balances"]

    def transactions(self, account_uid: str, date_from: date, date_to: date | None = None) -> Iterator[dict]:
        params: dict[str, str] = {"date_from": date_from.isoformat()}
        if date_to:
            params["date_to"] = date_to.isoformat()
        while True:
            page = self._request("GET", f"/accounts/{account_uid}/transactions", params=params)
            yield from page.get("transactions", [])
            key = page.get("continuation_key")
            if not key:
                break
            params["continuation_key"] = key
