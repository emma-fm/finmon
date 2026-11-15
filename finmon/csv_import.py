"""Fallback / backfill: import bank movement exports saved as CSV.

Most Spanish banks export movements from web banking as .xls/.csv. Save it as
CSV (UTF-8) and run `finmon import-csv file.csv`. Header names are matched
loosely (fecha/concepto/importe, date/description/amount...).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .categorize import _strip_accents, normalise_merchant
from .sync import store_transactions

DATE_KEYS = ["f. valor", "fecha valor", "fecha", "f. operacion", "fecha operacion", "date", "booking date"]
DESC_KEYS = ["descripcion", "concepto", "description", "comentario"]
AMOUNT_KEYS = ["importe", "importe (eur)", "importe (€)", "amount", "cantidad"]
CATEGORY_KEYS = ["subcategoria", "categoria", "category"]


def _norm(h: str) -> str:
    return _strip_accents(h.strip().lower())


def _pick(header: list[str], keys: list[str]) -> int | None:
    normed = [_norm(h) for h in header]
    for k in keys:
        for i, h in enumerate(normed):
            if h == k or h.startswith(k):
                return i
    return None


def _parse_amount(s: str) -> float:
    s = s.replace("€", "").replace("EUR", "").strip().replace(" ", "")
    if re.search(r",\d{1,2}$", s):  # Spanish format 1.234,56
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    return float(s)


def _parse_date(s: str) -> str:
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {s!r}")


def import_csv(conn: sqlite3.Connection, path: Path, account_uid: str = "csv") -> int:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    # Exports often have a few preamble lines; find a delimiter + header row combination that works.
    # (csv.Sniffer gets confused by Spanish decimal commas.)
    rows, header_idx = [], None
    for delimiter in (";", "\t", ","):
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        header_idx = next(
            (i for i, r in enumerate(rows)
             if _pick(r, DATE_KEYS) is not None and _pick(r, AMOUNT_KEYS) is not None),
            None,
        )
        if header_idx is not None:
            break
    if header_idx is None:
        raise SystemExit("Could not find a header row with date and amount columns.")
    header = rows[header_idx]
    di, ai = _pick(header, DATE_KEYS), _pick(header, AMOUNT_KEYS)
    desc_i, cat_i = _pick(header, DESC_KEYS), _pick(header, CATEGORY_KEYS)

    # Avoid double counting: only keep rows older than the bank-API history.
    earliest_api = conn.execute(
        "SELECT MIN(booking_date) FROM transactions WHERE source = 'enablebanking'").fetchone()[0]

    txs = []
    seen: dict[str, int] = {}
    for r in rows[header_idx + 1:]:
        if len(r) <= max(di, ai) or not r[di].strip() or not r[ai].strip():
            continue
        try:
            booking_date, amount = _parse_date(r[di]), _parse_amount(r[ai])
        except ValueError:
            continue
        if earliest_api and booking_date >= earliest_api:
            continue
        desc = r[desc_i].strip() if desc_i is not None and desc_i < len(r) else ""
        base = f"{booking_date}|{amount:.2f}|{desc}"
        seen[base] = seen.get(base, 0) + 1  # identical rows on the same day are distinct purchases
        tx_id = "csv:" + hashlib.sha1(f"{base}|{seen[base]}".encode()).hexdigest()[:20]
        bank_cat = None
        if cat_i is not None and cat_i < len(r) and r[cat_i].strip():
            bank_cat = _strip_accents(r[cat_i].strip().lower()).replace(" ", "_")
        txs.append({
            "id": tx_id, "account_uid": account_uid, "booking_date": booking_date, "amount": amount,
            "currency": "EUR", "status": "BOOK", "counterparty": None, "description": desc,
            "merchant": normalise_merchant(None, desc), "mcc": None, "category": bank_cat, "raw": None,
        })
    # Imports are historical: never alert on them.
    return store_transactions(conn, txs, "csv", mark_processed=True)
