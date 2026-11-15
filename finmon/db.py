"""SQLite storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    uid TEXT PRIMARY KEY,
    iban TEXT,
    name TEXT,
    currency TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    account_uid TEXT NOT NULL,
    booking_date TEXT NOT NULL,          -- YYYY-MM-DD
    amount REAL NOT NULL,                -- signed: negative = money out
    currency TEXT NOT NULL DEFAULT 'EUR',
    status TEXT NOT NULL DEFAULT 'BOOK', -- BOOK | PDNG
    counterparty TEXT,
    description TEXT,
    merchant TEXT,                       -- normalised key used for categorisation
    mcc TEXT,
    category TEXT,
    category_source TEXT,                -- auto | transfer | user (transfer/user are never overwritten)
    source TEXT NOT NULL,                -- enablebanking | csv
    first_seen TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0, -- 1 once the alert check has looked at it
    one_off INTEGER NOT NULL DEFAULT 0,  -- 1 = exceptional, excluded from 'usual' baselines
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(booking_date);
CREATE INDEX IF NOT EXISTS idx_tx_merchant ON transactions(merchant);

CREATE TABLE IF NOT EXISTS balances (
    account_uid TEXT NOT NULL,
    as_of TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    balance_type TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account_uid, as_of, balance_type)
);

CREATE TABLE IF NOT EXISTS merchant_categories (
    merchant TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    source TEXT NOT NULL                 -- rule | llm | user
);

-- How each Bizum / person-to-person transfer was interpreted.
--   incoming: reimbursement (of expense_id, or of something unknown), loan_repaid, income, gift, other
--   outgoing: share (my part of something someone else paid; category = what it was), loan_out, gift, other
CREATE TABLE IF NOT EXISTS transfer_links (
    tx_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    expense_id TEXT,
    note TEXT,
    confidence TEXT,
    source TEXT NOT NULL,                -- llm | user
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_expense ON transfer_links(expense_id);

-- Expenses you paid for a group and expect to be partly paid back.
CREATE TABLE IF NOT EXISTS splits (
    expense_id TEXT PRIMARY KEY,
    people INTEGER,
    my_share REAL NOT NULL,
    note TEXT,
    closed INTEGER NOT NULL DEFAULT 0,   -- 1 = stop expecting more money back
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    text TEXT NOT NULL,
    message_id INTEGER,                  -- Telegram message id (for feedback buttons)
    tx_ids TEXT                          -- JSON list of transactions the message is about
);

-- What the monitor has learned about the person. 'learned' notes are rewritten by the LLM
-- every week from the data and feedback; 'user' notes are things the person told it.
CREATE TABLE IF NOT EXISTS habit_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    source TEXT NOT NULL,                -- learned | user
    created_at TEXT NOT NULL
);

-- Reactions to messages (buttons): useful | not_useful | normal | one_off
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER,
    kind TEXT,
    verdict TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Merchants the person said are normal to spend at, up to roughly max_amount.
CREATE TABLE IF NOT EXISTS expected_merchants (
    merchant TEXT PRIMARY KEY,
    max_amount REAL NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("transactions", "category_source", "TEXT"),
    ("transactions", "one_off", "INTEGER NOT NULL DEFAULT 0"),  # excluded from 'usual' baselines
    ("notifications", "message_id", "INTEGER"),
    ("notifications", "tx_ids", "TEXT"),
]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for table, column, definition in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()
    return conn


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
