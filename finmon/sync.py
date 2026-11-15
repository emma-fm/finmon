"""Pull accounts, balances and transactions from Enable Banking into SQLite."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import debug
from .categorize import CATEGORY_ES, categorise, normalise_merchant
from .config import Settings
from .db import kv_get, kv_set
from .enablebanking import EnableBanking
from .fmt import day, emoji, eur

log = logging.getLogger(__name__)


def client(settings: Settings) -> EnableBanking:
    return EnableBanking(settings.eb_app_id, settings.eb_private_key_path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- consent ----------------------------------------------------------------

def start_link(settings: Settings) -> str:
    if not settings.eb_aspsp_name:
        raise SystemExit("Set EB_ASPSP_NAME to your bank's exact name (see `finmon banks --search <name>`).")
    eb = client(settings)
    aspsp = next((a for a in eb.aspsps(settings.eb_country) if a["name"] == settings.eb_aspsp_name), None)
    if aspsp is None:
        raise SystemExit(
            f"Bank '{settings.eb_aspsp_name}' not found for {settings.eb_country}. "
            "Run `finmon banks` to see the exact name and set EB_ASPSP_NAME."
        )
    # Stay a day under the bank's maximum: some banks count calendar days and reject the exact limit.
    max_days = int(aspsp.get("maximum_consent_validity") or 90 * 86400) // 86400 - 1
    validity = timedelta(days=max(1, min(settings.eb_consent_days, max_days)))
    resp = eb.start_auth(settings.eb_aspsp_name, settings.eb_country, settings.eb_redirect_url, validity)
    return resp["url"]


def complete_link(conn: sqlite3.Connection, settings: Settings, code: str) -> list[dict]:
    eb = client(settings)
    session = eb.create_session(code)
    kv_set(conn, "eb_session_id", session["session_id"])
    kv_set(conn, "eb_valid_until", session.get("access", {}).get("valid_until", ""))
    kv_set(conn, "consent_warned_on", "")
    accounts = session.get("accounts", [])
    # Account uids are only valid for the current session: replace the list.
    conn.execute("DELETE FROM accounts")
    for acc in accounts:
        conn.execute(
            "INSERT INTO accounts(uid, iban, name, currency, updated_at) VALUES(?, ?, ?, ?, ?)",
            (acc["uid"], (acc.get("account_id") or {}).get("iban"), acc.get("name") or acc.get("product"),
             acc.get("currency"), _now()),
        )
    conn.commit()
    return accounts


def consent_days_left(conn: sqlite3.Connection) -> float | None:
    valid_until = kv_get(conn, "eb_valid_until")
    if not valid_until:
        return None
    vu = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    return (vu - datetime.now(timezone.utc)).total_seconds() / 86400


# --- transactions -------------------------------------------------------------

def normalise_eb_transaction(tx: dict, account_uid: str) -> dict:
    amt = abs(float(tx["transaction_amount"]["amount"]))
    is_debit = tx.get("credit_debit_indicator") == "DBIT"
    amount = -amt if is_debit else amt
    booking_date = tx.get("booking_date") or tx.get("transaction_date") or tx.get("value_date")
    party = (tx.get("creditor") if is_debit else tx.get("debtor")) or {}
    counterparty = party.get("name")
    remittance = " ".join(tx.get("remittance_information") or []).strip()
    description = remittance or (tx.get("bank_transaction_code") or {}).get("description") or tx.get("note") or ""
    status = tx.get("status") or "BOOK"

    ref = tx.get("transaction_id") or tx.get("entry_reference")
    if ref and status == "BOOK":
        tx_id = f"eb:{account_uid}:{ref}"
    else:
        fingerprint = f"{account_uid}|{booking_date}|{amount:.2f}|{description}|{counterparty}|{status}"
        tx_id = f"eb:{status.lower()}:" + hashlib.sha1(fingerprint.encode()).hexdigest()[:20]

    return {
        "id": tx_id,
        "account_uid": account_uid,
        "booking_date": booking_date,
        "amount": amount,
        "currency": tx["transaction_amount"].get("currency", "EUR"),
        "status": status,
        "counterparty": counterparty,
        "description": description,
        "merchant": normalise_merchant(counterparty, description),
        "mcc": tx.get("merchant_category_code"),
        "raw": json.dumps(tx, ensure_ascii=False),
    }


def store_transactions(conn: sqlite3.Connection, txs: list[dict], source: str, mark_processed: bool) -> int:
    """Insert new transactions; returns how many were new."""
    new = 0
    for t in txs:
        exists = conn.execute("SELECT 1 FROM transactions WHERE id = ?", (t["id"],)).fetchone()
        if exists:
            continue
        processed = 1 if mark_processed else 0
        # A booked transaction that replaces a pending one we already alerted on
        # should not trigger a second alert.
        if not processed and t["status"] == "BOOK":
            processed = 1 if _consume_pending_match(conn, t) else 0
        category = categorise(conn, t["merchant"], t["description"], t["amount"], t.get("category"))
        cur = conn.execute(
            """INSERT INTO transactions(id, account_uid, booking_date, amount, currency, status, counterparty,
                   description, merchant, mcc, category, category_source, source, first_seen, processed, raw)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?, ?, ?, ?)""",
            (t["id"], t["account_uid"], t["booking_date"], t["amount"], t.get("currency", "EUR"),
             t["status"], t.get("counterparty"), t.get("description"), t["merchant"], t.get("mcc"),
             category, source, _now(), processed, t.get("raw")),
        )
        new += 1
        if source != "csv":
            who = t.get("counterparty") or t.get("description") or t["merchant"]
            label = f"{emoji(category)} {CATEGORY_ES.get(category, category)}" if category else "❔ sin categoría"
            debug.log(conn, f"#{cur.lastrowid} {day(t['booking_date'])} · {eur(t['amount'], sign=True)} "
                            f"{who[:40]} → {label}"
                            + (" (pendiente)" if t["status"] == "PDNG" else "")
                            + (" (ya avisado como pendiente)" if processed and not mark_processed else ""))
    conn.commit()
    return new


_removed_pending: list[dict] = []


def _consume_pending_match(conn: sqlite3.Connection, t: dict) -> bool:
    d = date.fromisoformat(t["booking_date"])
    for i, p in enumerate(_removed_pending):
        if (p["account_uid"] == t["account_uid"] and abs(p["amount"] - t["amount"]) < 0.005
                and abs((date.fromisoformat(p["booking_date"]) - d).days) <= 7):
            _removed_pending.pop(i)
            return bool(p["processed"])
    return False


def sync(conn: sqlite3.Connection, settings: Settings) -> int:
    """Fetch recent balances + transactions for every linked account. Returns new tx count."""
    eb = client(settings)
    accounts = conn.execute("SELECT uid FROM accounts").fetchall()
    if not accounts:
        raise SystemExit("No linked accounts. Run `finmon link` first.")

    first_sync = conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'enablebanking'").fetchone()[0] == 0
    lookback = settings.initial_history_days if first_sync else settings.sync_lookback_days
    date_from = date.today() - timedelta(days=lookback)
    total_new = 0

    debug.section(conn, f"Sincronización ({'primera, ' if first_sync else ''}desde {date_from})")
    for (uid,) in accounts:
        for b in eb.balances(uid):
            amt = b["balance_amount"]
            conn.execute(
                "INSERT OR REPLACE INTO balances(account_uid, as_of, amount, currency, balance_type) "
                "VALUES(?, ?, ?, ?, ?)",
                (uid, date.today().isoformat(), float(amt["amount"]), amt.get("currency", "EUR"),
                 b.get("balance_type") or ""),
            )
            debug.log(conn, f"Saldo ({b.get('balance_type') or '?'}): {eur(float(amt['amount']))}")

        fetched = [normalise_eb_transaction(tx, uid) for tx in eb.transactions(uid, date_from)]
        fetched = [t for t in fetched if t["booking_date"]]

        if first_sync and fetched:
            # CSV backfill overlapping the API window would double count.
            earliest = min(t["booking_date"] for t in fetched)
            conn.execute("DELETE FROM transactions WHERE source = 'csv' AND booking_date >= ?", (earliest,))

        # Pending transactions get re-issued with new ids once booked, so drop the
        # pending rows in the window and re-insert whatever the bank still reports.
        removed = conn.execute(
            "SELECT * FROM transactions WHERE account_uid = ? AND status = 'PDNG' AND booking_date >= ?",
            (uid, date_from.isoformat()),
        ).fetchall()
        still_pending = {t["id"] for t in fetched if t["status"] == "PDNG"}
        for r in removed:
            if r["id"] not in still_pending:
                _removed_pending.append(dict(r))
                conn.execute("DELETE FROM transactions WHERE id = ?", (r["id"],))

        new_here = store_transactions(conn, fetched, "enablebanking", mark_processed=first_sync)
        total_new += new_here
        replaced = sum(1 for r in removed if r["id"] not in still_pending)
        debug.log(conn, f"Cuenta …{uid[-6:]}: {len(fetched)} movimientos descargados, {new_here} nuevos"
                        + (f", {replaced} pendientes ya no listados (confirmados o anulados)" if replaced else "")
                        + (" (primera sincronización: sin avisos)" if first_sync else ""))

    kv_set(conn, "last_sync", _now())
    log.info("sync done: %d new transactions", total_new)
    return total_new
