"""Deterministic numbers the analysis is built on.

The LLM only interprets these; it never does the arithmetic itself.
"""

from __future__ import annotations

import calendar
import sqlite3
import statistics
from datetime import date, timedelta

from .config import Settings
from .splits import open_splits


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


_LEDGER_SQL = """
SELECT t.*, l.kind AS link_kind, l.expense_id AS link_expense_id, s.my_share, s.closed AS split_closed,
       COALESCE((SELECT SUM(r.amount) FROM transfer_links l2 JOIN transactions r ON r.id = l2.tx_id
                 WHERE l2.expense_id = t.id AND l2.kind = 'reimbursement'), 0) AS reimbursed
FROM transactions t
LEFT JOIN transfer_links l ON l.tx_id = t.id
LEFT JOIN splits s ON s.expense_id = t.id
WHERE t.booking_date >= ? AND t.booking_date < ?
ORDER BY t.booking_date
"""


def ledger(conn: sqlite3.Connection, settings: Settings, start: date, end: date,
           exclude_one_off: bool = False) -> tuple[list[dict], float]:
    """Return (expenses with their *effective* cost, income total) for [start, end).

    Effective cost = what the expense really cost you after friends paid you back:
      - declared split (open): your share; the rest is "owed to you", not your spending
      - otherwise: amount paid minus Bizum reimbursements linked to it
    Reimbursements, loan repayments and loans you made are neither income nor spending.
    A reimbursement that couldn't be matched to an expense reduces spending in its month.
    exclude_one_off drops expenses marked as exceptional, for computing what's "usual".
    """
    expenses: list[dict] = []
    income = 0.0
    for r in conn.execute(_LEDGER_SQL, (start.isoformat(), end.isoformat())).fetchall():
        d = dict(r)
        if d["category"] in settings.excluded_categories or d["link_kind"] in ("loan_out", "loan_repaid"):
            continue
        if d["amount"] < 0:
            if exclude_one_off and d["one_off"]:
                continue
            gross = -d["amount"]
            if d["my_share"] is not None and not d["split_closed"]:
                cost = d["my_share"]
            else:
                cost = max(0.0, gross - d["reimbursed"])
            d["cost"] = round(cost, 2)
            expenses.append(d)
        elif d["link_kind"] == "reimbursement":
            if d["link_expense_id"] is None:
                d["cost"] = -d["amount"]  # money back for something unknown: negative spending
                expenses.append(d)
        else:
            income += d["amount"]
    return expenses, round(income, 2)


def _spending_rows(conn: sqlite3.Connection, settings: Settings, start: date, end: date,
                   exclude_one_off: bool = False) -> list[dict]:
    return ledger(conn, settings, start, end, exclude_one_off)[0]


def _income(conn: sqlite3.Connection, settings: Settings, start: date, end: date) -> float:
    return ledger(conn, settings, start, end)[1]


def _by_category(rows: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        key = r["category"] or "uncategorized"
        out[key] = out.get(key, 0) + r["cost"]
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _total(rows: list[dict]) -> float:
    return sum(r["cost"] for r in rows)


def month_stats(conn: sqlite3.Connection, settings: Settings, today: date, baseline_months: int = 3) -> dict:
    start = _month_start(today)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    tomorrow = today + timedelta(days=1)

    mtd_rows = _spending_rows(conn, settings, start, tomorrow)
    mtd_total = _total(mtd_rows)
    mtd_fixed = sum(r["cost"] for r in mtd_rows if r["category"] in settings.fixed_categories)

    # Baseline ("usual"): previous full months that actually have data, without one-off expenses.
    first_date = conn.execute("SELECT MIN(booking_date) FROM transactions").fetchone()[0]
    baseline = []
    for i in range(1, baseline_months + 1):
        m_start = _add_months(start, -i)
        m_end = _add_months(m_start, 1)
        if not first_date or first_date > (m_start + timedelta(days=3)).isoformat():
            continue  # month not (fully) covered by history
        m_days = calendar.monthrange(m_start.year, m_start.month)[1]
        same_day = m_start + timedelta(days=min(today.day, m_days))
        rows = _spending_rows(conn, settings, m_start, m_end, exclude_one_off=True)
        baseline.append({
            "month": m_start.strftime("%Y-%m"),
            "total": round(_total(rows), 2),
            "to_same_day": round(_total([r for r in rows if r["booking_date"] < same_day.isoformat()]), 2),
            "by_category": {k: round(v, 2) for k, v in _by_category(rows).items()},
            "income": round(_income(conn, settings, m_start, m_end), 2),
        })

    avg = lambda key: round(statistics.mean(b[key] for b in baseline), 2) if baseline else None  # noqa: E731
    baseline_total, baseline_to_date = avg("total"), avg("to_same_day")

    # Projection: what's spent so far + what usually still gets spent in the rest of the month.
    if baseline_total is not None:
        projected = mtd_total + max(0.0, baseline_total - baseline_to_date)
    else:
        projected = mtd_total / today.day * days_in_month

    reference = settings.monthly_budget or baseline_total
    ratio = projected / reference if reference else None
    if ratio is None:
        status = "no_reference"
    elif ratio <= 0.95:
        status = "good"
    elif ratio <= 1.05:
        status = "on_track"
    elif ratio <= 1.15:
        status = "watch"
    else:
        status = "overspending"

    cat_baseline: dict[str, float] = {}
    for b in baseline:
        for k, v in b["by_category"].items():
            cat_baseline[k] = cat_baseline.get(k, 0) + v / len(baseline)
    mtd_cats = _by_category(mtd_rows)
    category_vs_usual = {
        k: {"this_month": round(v, 2), "usual_full_month": round(cat_baseline.get(k, 0), 2)}
        for k, v in mtd_cats.items()
    }

    return {
        "today": today.isoformat(),
        "day_of_month": today.day,
        "days_in_month": days_in_month,
        "spent_this_month": round(mtd_total, 2),
        "spent_fixed_this_month": round(mtd_fixed, 2),
        "income_this_month": round(_income(conn, settings, start, tomorrow), 2),
        "usual_spent_by_this_day": baseline_to_date,
        "usual_month_total": baseline_total,
        "monthly_budget": settings.monthly_budget,
        "projected_month_total": round(projected, 2),
        "projection_vs_reference": round(ratio, 3) if ratio else None,
        "status": status,
        "category_vs_usual": category_vs_usual,
        "previous_months": baseline,
        "one_off_this_month": round(sum(r["cost"] for r in mtd_rows if r["one_off"]), 2),
        "same_month_last_year": _same_month_last_year(conn, settings, start, first_date),
        "balance": latest_balance(conn),
        "owed_to_you": [
            {"ref": o["ref"], "date": o["booking_date"], "what": o["counterparty"] or o["merchant"],
             "paid": o["gross"], "my_share": o["my_share"], "received": o["reimbursed"], "still_owed": o["owed"],
             "days_ago": o["days_ago"]}
            for o in open_splits(conn)
        ],
    }


def _same_month_last_year(conn: sqlite3.Connection, settings: Settings, start: date,
                          first_date: str | None) -> dict | None:
    """Seasonality hint: how the same calendar month went a year ago (if history reaches that far)."""
    ly_start = _add_months(start, -12)
    if not first_date or first_date > (ly_start + timedelta(days=3)).isoformat():
        return None
    rows = _spending_rows(conn, settings, ly_start, _add_months(ly_start, 1))
    return {"month": ly_start.strftime("%Y-%m"), "total": round(_total(rows), 2),
            "by_category": {k: round(v, 2) for k, v in list(_by_category(rows).items())[:8]}}


def latest_balance(conn: sqlite3.Connection) -> float | None:
    """Sum of the latest balance of each account (preferring available-balance types)."""
    preference = ["CLAV", "ITAV", "XPCD", "CLBD", "ITBD"]
    rows = conn.execute(
        """SELECT account_uid, balance_type, amount FROM balances b
           WHERE as_of = (SELECT MAX(as_of) FROM balances b2 WHERE b2.account_uid = b.account_uid)"""
    ).fetchall()
    best: dict[str, tuple[int, float]] = {}
    for r in rows:
        rank = preference.index(r["balance_type"]) if r["balance_type"] in preference else len(preference)
        if r["account_uid"] not in best or rank < best[r["account_uid"]][0]:
            best[r["account_uid"]] = (rank, r["amount"])
    return round(sum(a for _, a in best.values()), 2) if best else None


def _category_amounts(conn: sqlite3.Connection, category: str | None, before: str) -> list[float]:
    """Your single expenses in this category over the last 6 months (one-offs excluded), sorted."""
    since = (date.fromisoformat(before) - timedelta(days=180)).isoformat()
    return sorted(
        -r[0] for r in conn.execute(
            """SELECT amount FROM transactions WHERE amount < 0 AND COALESCE(category, '') = COALESCE(?, '')
               AND booking_date >= ? AND booking_date < ? AND one_off = 0""",
            (category, since, before),
        ).fetchall()
    )


def large_expense_threshold(conn: sqlite3.Connection, settings: Settings, category: str | None,
                            before: str) -> float:
    """What counts as a 'large' expense in this category *for you*.

    1.5x your 90th-percentile single expense in the category over the last 6 months, never below
    LARGE_EXPENSE_EUR. With little history it falls back to LARGE_EXPENSE_EUR.
    """
    amounts = _category_amounts(conn, category, before)
    if len(amounts) < 8:
        return settings.large_expense_eur
    p90 = amounts[int(0.9 * (len(amounts) - 1))]
    return max(settings.large_expense_eur, round(1.5 * p90, 2))


def is_unusual(conn: sqlite3.Connection, settings: Settings, tx: sqlite3.Row) -> str | None:
    """Return a reason string if a new outgoing transaction deserves an alert."""
    if tx["amount"] >= 0 or (tx["category"] in settings.excluded_categories):
        return None
    link = conn.execute("SELECT kind FROM transfer_links WHERE tx_id = ?", (tx["id"],)).fetchone()
    if link and link["kind"] == "loan_out":
        return None
    # Judge what it really costs you: your share if split, minus Bizums already received.
    split = conn.execute("SELECT my_share FROM splits WHERE expense_id = ? AND closed = 0", (tx["id"],)).fetchone()
    reimbursed = conn.execute(
        """SELECT COALESCE(SUM(r.amount), 0) FROM transfer_links l JOIN transactions r ON r.id = l.tx_id
           WHERE l.expense_id = ? AND l.kind = 'reimbursement'""", (tx["id"],)).fetchone()[0]
    amount = split["my_share"] if split else -tx["amount"] - reimbursed
    if amount <= 0:
        return None

    # The person told us this merchant is normal (feedback button "Es normal").
    expected = conn.execute("SELECT max_amount FROM expected_merchants WHERE merchant = ?",
                            (tx["merchant"],)).fetchone()
    if expected and amount <= expected["max_amount"] * 1.25:
        return None

    history = [
        -r["amount"] for r in conn.execute(
            "SELECT amount FROM transactions WHERE merchant = ? AND id != ? AND amount < 0 AND booking_date < ?",
            (tx["merchant"], tx["id"], tx["booking_date"]),
        ).fetchall()
    ]
    threshold = large_expense_threshold(conn, settings, tx["category"], tx["booking_date"])
    if amount >= threshold:
        if len(history) >= 3 and amount <= statistics.median(history) * 1.25:
            return None  # large but normal for this merchant (e.g. rent)
        return f"grande para ti en esta categoría (≥ {threshold:.0f} €)"
    if len(history) >= 3 and amount > 3 * statistics.median(history) and amount >= 20:
        return f"unas {amount / statistics.median(history):.1f} veces lo que sueles gastar en este comercio"
    if not history:
        # New merchant: only worth mentioning if it's big compared to what you usually spend in the category.
        cat_amounts = _category_amounts(conn, tx["category"], tx["booking_date"])
        typical = statistics.median(cat_amounts) if len(cat_amounts) >= 8 else 0.0
        if amount >= max(threshold / 2, 2 * typical):
            return "primera compra en un comercio nuevo"
    return None


def recent_transactions(conn: sqlite3.Connection, since: date, limit: int = 200) -> list[dict]:
    """Transactions with their chat reference (#ref) and how Bizums/splits were interpreted."""
    rows = conn.execute(
        """SELECT t.rowid AS ref, t.booking_date, t.amount, t.status, t.counterparty, t.description,
                  t.category, l.kind AS bizum_kind, e.rowid AS reimburses_ref, s.my_share AS split_my_share
           FROM transactions t
           LEFT JOIN transfer_links l ON l.tx_id = t.id
           LEFT JOIN transactions e ON e.id = l.expense_id
           LEFT JOIN splits s ON s.expense_id = t.id
           WHERE t.booking_date >= ? ORDER BY t.booking_date DESC LIMIT ?""",
        (since.isoformat(), limit),
    ).fetchall()
    return [{k: v for k, v in dict(r).items() if v is not None} for r in rows]


def recurring_charges(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Merchants charged in at least 3 of the last 4 months with a stable amount."""
    since = _add_months(_month_start(today), -4)
    rows = conn.execute(
        """SELECT merchant, substr(booking_date, 1, 7) AS month, SUM(-amount) AS total
           FROM transactions WHERE amount < 0 AND booking_date >= ?
           GROUP BY merchant, month""",
        (since.isoformat(),),
    ).fetchall()
    per_merchant: dict[str, list[float]] = {}
    for r in rows:
        per_merchant.setdefault(r["merchant"], []).append(r["total"])
    out = []
    for m, totals in per_merchant.items():
        if len(totals) >= 3:
            med = statistics.median(totals)
            if med > 0 and all(abs(t - med) / med < 0.2 for t in totals):
                out.append({"merchant": m, "monthly_amount": round(med, 2)})
    return sorted(out, key=lambda x: -x["monthly_amount"])
