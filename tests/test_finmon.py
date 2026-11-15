from datetime import date, timedelta

import pytest

from finmon import splits, stats, sync
from finmon.categorize import normalise_merchant, rule_category
from finmon.config import load_settings
from finmon.csv_import import import_csv
from finmon.db import connect

TODAY = date.today()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FINMON_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("MONTHLY_BUDGET", raising=False)
    settings = load_settings()
    conn = connect(settings.db_path)
    return settings, conn


def add(conn, id_, amount, desc, days_ago=0, counterparty=None, status="BOOK"):
    t = {"id": id_, "account_uid": "a1", "booking_date": (TODAY - timedelta(days=days_ago)).isoformat(),
         "amount": amount, "currency": "EUR", "status": status, "counterparty": counterparty,
         "description": desc, "merchant": normalise_merchant(counterparty, desc), "mcc": None, "raw": None}
    sync.store_transactions(conn, [t], "enablebanking", mark_processed=False)
    return conn.execute("SELECT rowid AS ref, * FROM transactions WHERE id = ?", (id_,)).fetchone()


def spent(conn, settings):
    start = TODAY.replace(day=1)
    rows = stats._spending_rows(conn, settings, start, TODAY + timedelta(days=1))
    return round(stats._total(rows), 2), stats._income(conn, settings, start, TODAY + timedelta(days=1))


def test_rules_and_normalisation():
    m = normalise_merchant(None, "COMPRA TARJ. 5402XXXXXXXX1234 MERCADONA MADRID 12/11/2026")
    assert "mercadona" in m
    assert rule_category(m, None, -30) == "groceries"
    assert rule_category("uber eats", None, -20) == "eating_out"
    assert rule_category("uber trip", None, -20) == "transport"
    assert rule_category("parent co", None, -20) is None  # ' rent ' must not match 'parent'
    assert rule_category(normalise_merchant(None, "Bizum de ANA LOPEZ concepto cena"), None, 20) == "bizum"


def test_paid_for_group_then_reimbursed(env):
    settings, conn = env
    dinner = add(conn, "d1", -80.0, "COMPRA TARJ RESTAURANTE LA PEPA", days_ago=0)
    assert spent(conn, settings) == (80.0, 0)

    # Declared up front: only my share counts, the rest is owed.
    splits.set_split(conn, dinner, people=4)
    assert spent(conn, settings)[0] == 20.0
    assert splits.open_splits(conn)[0]["owed"] == 60.0

    # Two friends pay back with Bizum: not income, owed goes down.
    for i, who in enumerate(["ANA", "LUIS"]):
        b = add(conn, f"b{i}", 20.0, f"Bizum de {who} concepto cena")
        splits.link_transfer(conn, b, "reimbursement", dinner["id"], source="claude")
    assert spent(conn, settings) == (20.0, 0)
    assert splits.open_splits(conn)[0]["owed"] == 20.0

    # Third friend never pays: close it, the unpaid part becomes my cost.
    splits.close_split(conn, dinner["id"])
    assert spent(conn, settings)[0] == 40.0
    assert splits.open_splits(conn) == []


def test_reimbursed_without_declared_split(env):
    settings, conn = env
    tickets = add(conn, "t1", -90.0, "TICKETMASTER", days_ago=0)
    b = add(conn, "b1", 30.0, "Bizum de MARTA entradas")
    splits.link_transfer(conn, b, "reimbursement", tickets["id"])
    assert spent(conn, settings) == (60.0, 0)


def test_outgoing_share_gets_real_category_and_loans_excluded(env):
    settings, conn = env
    share = add(conn, "o1", -25.0, "Bizum a PEDRO cena cumple")
    splits.link_transfer(conn, share, "share", category="eating_out", source="claude")
    loan = add(conn, "o2", -200.0, "Bizum a PEDRO prestamo")
    splits.link_transfer(conn, loan, "loan_out")
    repaid = add(conn, "i1", 200.0, "Bizum de PEDRO devuelvo")
    splits.link_transfer(conn, repaid, "loan_repaid")
    rows = stats._spending_rows(conn, settings, TODAY.replace(day=1), TODAY + timedelta(days=1))
    assert [(r["category"], r["cost"]) for r in rows] == [("eating_out", 25.0)]
    assert spent(conn, settings) == (25.0, 0)


def test_unmatched_reimbursement_reduces_spending(env):
    settings, conn = env
    add(conn, "g1", -50.0, "MERCADONA")
    b = add(conn, "b1", 10.0, "Bizum de ANA la compra")
    splits.link_transfer(conn, b, "reimbursement", None)
    assert spent(conn, settings) == (40.0, 0)


def test_classify_transfers_with_claude_stub(env, monkeypatch):
    settings, conn = env
    dinner = add(conn, "d1", -60.0, "RESTAURANTE EL FARO", days_ago=2)
    b1 = add(conn, "b1", 20.0, "Bizum de ANA cena faro")
    b2 = add(conn, "b2", 20.0, "Bizum de LUIS cena")
    out = add(conn, "o1", -15.0, "Bizum a SARA regalo cumple Juan")

    def fake_ask_json(_settings, prompt, _system):
        assert "EL FARO" in prompt
        return {"transfers": [
            {"ref": b1["ref"], "kind": "reimbursement", "expense_ref": dinner["ref"], "split_people": 3,
             "confidence": "high"},
            {"ref": b2["ref"], "kind": "reimbursement", "expense_ref": dinner["ref"], "confidence": "low"},
            {"ref": out["ref"], "kind": "share", "category": "gifts_donations", "confidence": "high"},
            {"ref": 99999, "kind": "income"},  # unknown ref: ignored
        ]}

    monkeypatch.setattr(splits, "ask_json", fake_ask_json)
    unsure = splits.classify_transfers(conn, settings)
    assert len(unsure) == 1 and f"#{b2['ref']}" in unsure[0]
    assert splits.unreviewed_transfers(conn) == []
    # 60 split 3 ways (inferred), both friends paid -> my cost 20 + gift share 15
    assert spent(conn, settings) == (35.0, 0)
    assert splits.open_splits(conn) == []


def test_pending_then_booked_is_not_double_alerted(env):
    settings, conn = env
    pending = sync.normalise_eb_transaction(
        {"transaction_amount": {"amount": "45.10", "currency": "EUR"}, "credit_debit_indicator": "DBIT",
         "status": "PDNG", "transaction_date": TODAY.isoformat(), "remittance_information": ["GLOVO"]}, "a1")
    sync.store_transactions(conn, [pending], "enablebanking", mark_processed=False)
    conn.execute("UPDATE transactions SET processed = 1")  # alert already handled
    # Next sync: pending disappeared, booked version arrives with a real id.
    conn.execute("DELETE FROM transactions WHERE id = ?", (pending["id"],))
    sync._removed_pending.append({**pending, "processed": 1})
    booked = sync.normalise_eb_transaction(
        {"transaction_amount": {"amount": "45.10", "currency": "EUR"}, "credit_debit_indicator": "DBIT",
         "status": "BOOK", "booking_date": TODAY.isoformat(), "entry_reference": "XYZ",
         "remittance_information": ["GLOVO"]}, "a1")
    sync.store_transactions(conn, [booked], "enablebanking", mark_processed=False)
    row = conn.execute("SELECT processed, amount FROM transactions").fetchone()
    assert row["processed"] == 1 and row["amount"] == -45.10


def test_csv_import_spanish_export(env, tmp_path):
    settings, conn = env
    f = tmp_path / "export.csv"
    f.write_text(
        "Número de cuenta;ES00 0000 0000 0000 0000 0000\n\n"
        "F. VALOR;CATEGORÍA;SUBCATEGORÍA;DESCRIPCIÓN;COMENTARIO;IMPORTE (€);SALDO (€)\n"
        "03/11/2026;Alimentación;Supermercados;Pago en MERCADONA;;-45,30;1.200,00\n"
        "03/11/2026;Alimentación;Supermercados;Pago en MERCADONA;;-45,30;1.154,70\n"
        "01/11/2026;Nómina;Nómina;Nómina ACME SL;;2.100,00;1.245,30\n",
        encoding="utf-8",
    )
    assert import_csv(conn, f) == 3
    assert import_csv(conn, f) == 0  # idempotent
    amounts = sorted(r[0] for r in conn.execute("SELECT amount FROM transactions"))
    assert amounts == [-45.30, -45.30, 2100.0]


def test_month_stats_projection(env):
    settings, conn = env
    # Three previous months of ~1000 € each, spread across the month.
    first = TODAY.replace(day=1)
    for m in range(1, 4):
        mstart = stats._add_months(first, -m)
        for d in range(0, 28, 7):
            add(conn, f"h{m}{d}", -250.0, "AMAZON", days_ago=(TODAY - (mstart + timedelta(days=d))).days)
    add(conn, "now", -600.0, "AMAZON", days_ago=0)
    s = stats.month_stats(conn, settings, TODAY)
    assert s["usual_month_total"] == 1000.0
    assert s["spent_this_month"] == 600.0
    assert s["projected_month_total"] >= 600.0
    assert s["status"] in {"good", "on_track", "watch", "overspending"}


# --- learning / adaptation ----------------------------------------------------------------

from finmon import learning  # noqa: E402
from finmon.db import connect as _connect  # noqa: E402


def _notify_row(conn, kind, tx_ids):
    import json
    cur = conn.execute("INSERT INTO notifications(kind, sent_at, text, tx_ids) VALUES(?, 'now', 'x', ?)",
                       (kind, json.dumps(tx_ids)))
    return cur.lastrowid


def test_es_normal_button_stops_alerts_for_that_merchant(env):
    settings, conn = env
    t1 = add(conn, "c1", -150.0, "DECATHLON MADRID", days_ago=1)
    assert stats.is_unusual(conn, settings, t1)  # new merchant, large
    nid = _notify_row(conn, "unusual_expense", [t1["id"]])
    assert "no te avisaré" in learning.record_feedback(conn, nid, "normal")
    t2 = add(conn, "c2", -170.0, "DECATHLON MADRID", days_ago=0)
    assert stats.is_unusual(conn, settings, t2) is None       # within 1.25x of what was approved
    t3 = add(conn, "c3", -400.0, "DECATHLON MADRID", days_ago=0)
    assert stats.is_unusual(conn, settings, t3) is not None   # way above: still alert


def test_one_off_excluded_from_usual_baseline(env):
    settings, conn = env
    prev = stats._add_months(TODAY.replace(day=1), -1)
    days_ago = (TODAY - prev).days
    add(conn, "g", -300.0, "MERCADONA", days_ago=days_ago)
    tv = add(conn, "tv", -900.0, "MEDIAMARKT", days_ago=days_ago - 1)
    assert stats.month_stats(conn, settings, TODAY)["usual_month_total"] == 1200.0
    nid = _notify_row(conn, "unusual_expense", [tv["id"]])
    learning.record_feedback(conn, nid, "one_off")
    assert stats.month_stats(conn, settings, TODAY)["usual_month_total"] == 300.0


def test_large_threshold_adapts_to_category_history(env):
    settings, conn = env
    # Someone who regularly spends ~180-200 € per shopping trip.
    for i in range(10):
        add(conn, f"s{i}", -(180.0 + i * 2), f"ZARA TIENDA {i}", days_ago=100 - i * 7)
    thr = stats.large_expense_threshold(conn, settings, "shopping", TODAY.isoformat())
    assert thr > 250  # not the fixed 100 € anymore
    t = add(conn, "new", -210.0, "ZARA ONLINE")
    assert stats.is_unusual(conn, settings, t) is None


def test_review_habits_replaces_learned_but_keeps_user_notes(env, monkeypatch):
    settings, conn = env
    add(conn, "m0", -55.0, "MERCADONA", days_ago=70)  # history covers at least one full month
    add(conn, "m1", -60.0, "MERCADONA", days_ago=35)
    learning.add_user_note(conn, "En diciembre gasto más en regalos")
    conn.execute("INSERT INTO habit_notes(text, source, created_at) VALUES('vieja', 'learned', 'x')")

    def fake(_s, prompt, _sys):
        assert "En diciembre gasto más en regalos" in prompt and "vieja" in prompt
        return {"notes": ["Compra semanal en Mercadona de ~60 €"], "new_insights": ["Nuevo patrón"]}

    monkeypatch.setattr(learning, "ask_json", fake)
    assert learning.review_habits(conn, settings) == ["Nuevo patrón"]
    texts = {(r["source"], r["text"]) for r in learning.notes(conn)}
    assert texts == {("user", "En diciembre gasto más en regalos"), ("learned", "Compra semanal en Mercadona de ~60 €")}
    ctx = learning.habits_context(conn)
    assert "Mercadona" in ctx and "told you" in ctx


def test_feedback_summary_in_context(env):
    settings, conn = env
    nid = _notify_row(conn, "daily", [])
    learning.record_feedback(conn, nid, "not_useful")
    assert '"not_useful": 1' in learning.habits_context(conn)


def test_migration_adds_columns_to_old_db(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE transactions (id TEXT PRIMARY KEY, account_uid TEXT NOT NULL, booking_date TEXT NOT NULL,"
                " amount REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'EUR', status TEXT NOT NULL DEFAULT 'BOOK',"
                " counterparty TEXT, description TEXT, merchant TEXT, mcc TEXT, category TEXT, source TEXT NOT NULL,"
                " first_seen TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0, raw TEXT)")
    old.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,"
                " sent_at TEXT NOT NULL, text TEXT NOT NULL)")
    old.commit()
    old.close()
    conn = _connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    assert {"one_off", "category_source"} <= cols


# --- LLM provider layer -------------------------------------------------------------------------

import sys  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

from finmon import llm  # noqa: E402


def test_parse_json_object_variants():
    assert llm.parse_json_object('{"a": 1}') == {"a": 1}
    assert llm.parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json_object('Here you go:\n{"a": {"b": 2}}\nThanks') == {"a": {"b": 2}}
    with pytest.raises(llm.LLMError):
        llm.parse_json_object("no json here")


def _settings_with(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("FINMON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FINMON_ENV_FILE", str(tmp_path / "none.env"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_settings()


def test_command_provider(monkeypatch, tmp_path):
    script = tmp_path / "fake_llm.py"
    script.write_text("import sys, json; data = sys.stdin.read(); print(json.dumps({'len': len(data) > 0}))")
    s = _settings_with(monkeypatch, tmp_path, LLM_PROVIDER="command",
                       LLM_COMMAND=f"{sys.executable} {script}")
    assert llm.ask_json(s, "hello", "system") == {"len": True}


def test_openai_compatible_provider(monkeypatch, tmp_path):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            import json as _j
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = _j.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = _j.dumps({"choices": [{"message": {"content": '{"notify": false}'}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    s = _settings_with(monkeypatch, tmp_path, LLM_PROVIDER="openai-compatible", LLM_MODEL="local-model",
                       LLM_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1", LLM_API_KEY="k")
    assert llm.ask_json(s, "prompt", "sys") == {"notify": False}
    assert seen["path"] == "/v1/chat/completions" and seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "local-model"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["response_format"] == {"type": "json_object"}
    server.server_close()


def test_unknown_provider(monkeypatch, tmp_path):
    s = _settings_with(monkeypatch, tmp_path, LLM_PROVIDER="nope")
    with pytest.raises(llm.LLMError):
        llm.ask(s, "x", "y")


def test_debug_mode_collects_and_expires(env, capsys):
    from finmon import debug
    settings, conn = env
    debug.log(conn, "ignored while off")
    debug.enable(conn, 1)
    add(conn, "dbg1", -12.5, "MERCADONA")          # store_transactions logs new transactions
    debug.flush(conn, settings, "test", dry_run=True)
    out = capsys.readouterr().out
    assert "Debug" in out and "MERCADONA" in out and "ignored" not in out
    from finmon.db import kv_set
    kv_set(conn, "debug_until", "2000-01-01T00:00:00+00:00")  # expired
    debug.log(conn, "after expiry")
    debug.flush(conn, settings, "test", dry_run=True)
    assert "after expiry" not in capsys.readouterr().out
    assert not debug.is_on(conn)


def test_spanish_bank_formats():
    from finmon.categorize import is_person_transfer
    assert normalise_merchant(None, "Pago en NOODLE BAR SEVILLA ES") == "noodle bar"
    assert normalise_merchant(None, "Pago en MEDIA MARKT - MADRID MADRID ES") == "media markt"
    assert normalise_merchant(None, "Pago en PE¥A SOL BILBAO ES") == "pena sol"
    assert normalise_merchant("0000000000", "Pago en") == "unknown"
    assert normalise_merchant("ANA GARCIA", "Bizum recibido de ANA GARCIA") == "ana garcia"
    cine = "Pago Bizum en CINESABIZUM"
    assert rule_category(normalise_merchant(None, cine), cine, -20) == "leisure"
    assert not is_person_transfer("leisure", "Pago Bizum en CINESABIZUM")
    assert is_person_transfer("bizum", "Bizum enviado a Luis")
    assert rule_category("cargo cuota de prestamo", "Cargo cuota de préstamo", -250.0) == "loans"
    assert rule_category(normalise_merchant(None, "Pago en VET SOL MADRID ES"), None, -120) == "pets"
    assert rule_category("traspaso interno emitido", "Traspaso interno emitido", -120) == "internal_transfer"


# --- message formatting ---------------------------------------------------------------------------

def test_spanish_formats():
    from datetime import date as _d

    from finmon.fmt import bar, day, eur
    assert eur(1413.16) == "1.413,16 €"
    assert eur(-7.5) == "−7,50 €"
    assert eur(24, sign=True) == "+24,00 €"
    assert eur(1779.46, 0) == "1.779 €"
    assert day("2026-11-15") == "dom 15 nov" and day(_d(2026, 8, 1)) == "sáb 1 ago"
    assert bar(0.5, 10) == "▓▓▓▓▓░░░░░" and bar(1.4, 4) == "▓▓▓▓"


def test_llm_html_is_sanitised():
    from finmon.fmt import llm_html, strip_html
    out = llm_html("**Ojo** con <script>x</script> & <b>Glovo</b>\n- uno\n## Título")
    assert out.startswith("<b>Ojo</b>") and "&lt;script&gt;" in out and "&amp;" in out
    assert "<b>Glovo</b>" in out and "• uno" in out and "<b>Título</b>" in out
    assert strip_html("<b>A &amp; B</b>") == "A & B"


def test_telegram_chunks_keep_paragraphs():
    from finmon.telegram import _chunks
    text = "\n\n".join(f"<b>p{i}</b> " + "x" * 900 for i in range(10))
    chunks = _chunks(text, limit=3800)
    assert len(chunks) > 1 and all(len(c) <= 3800 for c in chunks)
    assert all(c.count("<b>") == c.count("</b>") for c in chunks)
