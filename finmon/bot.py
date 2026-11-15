"""Telegram bot: ask questions and manage split costs from your phone.

Runs as a long-polling loop (no public URL / webhook needed). Only messages
from TELEGRAM_CHAT_ID are answered.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import timedelta

from . import telegram
from .analysis import STATUS_ES, _compact_stats, _local_now, _system
from .categorize import CATEGORIES, CATEGORY_ES, set_merchant_category
from .config import Settings
from .db import kv_get, kv_set
from .fmt import bar, day, emoji, esc, eur, llm_html, strip_html
from .learning import add_user_note, forget_note, habits_context, mark_expected, notes, record_feedback, review_habits
from .llm import LLMError, ask_json
from .splits import IN_KINDS, KIND_ES, OUT_KINDS, close_split, link_transfer, open_splits, set_split, tx_by_ref
from .stats import month_stats, recent_transactions

log = logging.getLogger(__name__)

HELP = """🤖 <b>Qué puedo hacer</b>

<b>Ver</b>
/status — cómo va el mes
/recent — últimos movimientos con su #ref
/owed — lo que te deben de gastos compartidos
/habits — lo que he aprendido de tus hábitos

<b>Gastos compartidos</b>
/split #ref 4 — pagaste para 4 (contándote)
/split #ref share=12,50 — o indicando tu parte
/settled #ref — no esperar más devoluciones
/link #bizum #gasto — ese Bizum devuelve ese gasto
/kind #bizum share eating_out — reinterpretar un Bizum

<b>Ajustes</b>
/cat #ref groceries — cambiar la categoría de un comercio
/forget n — olvidar la nota n de /habits
/learn — revisar tus hábitos ahora
/debug on · /debug off — informe tras cada ejecución

<b>Botones</b>: el teclado de abajo tiene lo más usado, y las alertas de gastos que se suelen \
compartir traen ✂️ ÷2 ÷3… para dividirlos de un toque. En cada mensaje: <i>Es normal</i> deja de \
avisarte por ese comercio, <i>Puntual</i> lo excluye de tus cifras habituales y 👍/👎 ajustan cuánto te escribo.

<b>O escríbeme normal</b>, p. ej.:
• «la cena del viernes éramos 4»
• «el #45 es Marta devolviéndome lo del concierto»
• «¿cuánto he gastado en Glovo este mes?»"""


# Persistent keyboard under the text box. Each button just sends its label, mapped to a command here.
MENU = {
    "📊 Estado": "/status", "🧾 Movimientos": "/recent", "🤝 Me deben": "/owed",
    "🧠 Hábitos": "/habits", "❓ Ayuda": "/help",
}
MAIN_KEYBOARD = {
    "keyboard": [[{"text": "📊 Estado"}, {"text": "🧾 Movimientos"}],
                 [{"text": "🤝 Me deben"}, {"text": "🧠 Hábitos"}, {"text": "❓ Ayuda"}]],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Pregúntame o cuéntame algo…",
}

# The "/" menu (commands that need no arguments; the rest are explained in /help).
COMMANDS = [
    ("status", "Cómo va el mes"),
    ("recent", "Últimos movimientos"),
    ("owed", "Lo que te deben"),
    ("habits", "Lo que he aprendido de ti"),
    ("learn", "Revisar tus hábitos ahora"),
    ("debug", "Ver o cambiar el modo debug"),
    ("help", "Todos los comandos"),
]


def _fmt(r: dict) -> str:
    """One transaction line for /recent (the day is shown as a heading)."""
    who = (r.get("counterparty") or r.get("description") or "").strip()
    extra = ""
    if r.get("bizum_kind"):
        extra = f" · <i>{esc(KIND_ES.get(r['bizum_kind'], r['bizum_kind']))}"
        extra += f" de #{r['reimburses_ref']}</i>" if r.get("reimburses_ref") else "</i>"
    if r.get("split_my_share") is not None:
        extra += f" · <i>tu parte {eur(r['split_my_share'])}</i>"
    amount = f"<b>{eur(r['amount'], sign=True)}</b>" if r["amount"] > 0 else eur(r["amount"])
    return f"{emoji(r.get('category'))} {amount}  {esc(who[:32])} <code>#{r['ref']}</code>{extra}"


def _recent(conn: sqlite3.Connection, settings: Settings) -> str:
    since = _local_now(settings).date() - timedelta(days=14)
    rows = recent_transactions(conn, since, 30)
    if not rows:
        return "No hay movimientos en las últimas dos semanas."
    out, current = ["🧾 <b>Últimos movimientos</b>"], None
    for r in rows:
        if r["booking_date"] != current:
            current = r["booking_date"]
            out.append(f"\n<b>{day(current)}</b>")
        out.append(_fmt(r))
    return "\n".join(out)


def _status(conn: sqlite3.Connection, settings: Settings) -> str:
    s = month_stats(conn, settings, _local_now(settings).date())
    ref = s["monthly_budget"] or s["usual_month_total"]
    icon = {"good": "🟢", "on_track": "🟢", "watch": "🟡", "overspending": "🔴"}.get(s["status"], "⚪")
    out = [f"{icon} <b>Este mes: {eur(s['spent_this_month'], 0)}</b>  ·  día {s['day_of_month']}/{s['days_in_month']}"]
    if ref:
        out += ["", f"<code>{bar(s['projected_month_total'] / ref)}</code> {s['projection_vs_reference']:.0%}",
                f"Previsión {eur(s['projected_month_total'], 0)} · "
                f"{'presupuesto' if s['monthly_budget'] else 'habitual'} {eur(ref, 0)}",
                f"<i>Estado: {STATUS_ES[s['status']]}</i>"]
    if s["usual_spent_by_this_day"] is not None:
        diff = s["spent_this_month"] - s["usual_spent_by_this_day"]
        out.append(f"<i>{eur(abs(diff), 0)} {'más' if diff > 0 else 'menos'} que lo habitual a estas alturas</i>")
    out += ["", "<b>Por categoría</b> <i>(este mes · habitual al mes)</i>"]
    for k, v in list(s["category_vs_usual"].items())[:8]:
        hot = " ⚠️" if v["usual_full_month"] and v["this_month"] > v["usual_full_month"] else ""
        out.append(f"{emoji(k)} {esc(CATEGORY_ES.get(k, k))}: <b>{eur(v['this_month'], 0)}</b> · "
                   f"{eur(v['usual_full_month'], 0)}{hot}")
    extras = []
    if s["balance"] is not None:
        extras.append(f"🏦 Saldo: {eur(s['balance'])}")
    owed = sum(o["still_owed"] for o in s["owed_to_you"])
    if owed:
        extras.append(f"🤝 Te deben: {eur(owed)} (/owed)")
    return "\n".join(out + ([""] + extras if extras else []))


def _owed(conn: sqlite3.Connection) -> str:
    rows = open_splits(conn)
    if not rows:
        return "🤝 Nadie te debe nada 🎉"
    total = sum(o["owed"] for o in rows)
    out = [f"🤝 <b>Te deben {eur(total)}</b>"]
    for o in rows:
        who = esc(o["counterparty"] or o["merchant"])
        late = f" <i>(hace {o['days_ago']} días)</i>" if o["days_ago"] >= 7 else ""
        out += ["", f"<b>{who}</b> <code>#{o['ref']}</code> · {day(o['booking_date'])}",
                f"Pagaste {eur(o['gross'])} · tu parte {eur(o['my_share'])} · recibido {eur(o['reimbursed'])}",
                f"➡️ <b>faltan {eur(o['owed'])}</b>{late}"]
    return "\n".join(out)


def _habits(conn: sqlite3.Connection) -> str:
    rows = notes(conn)
    if not rows:
        return "🧠 Aún no he aprendido nada: necesito algo más de historial (lo reviso cada domingo)."
    learned = [r for r in rows if r["source"] == "learned"]
    told = [r for r in rows if r["source"] == "user"]
    out = ["🧠 <b>Lo que sé de tus hábitos</b>"]
    if told:
        out += ["", "<b>Me lo dijiste tú</b>"] + [f"<code>{r['id']}</code> {esc(r['text'])}" for r in told]
    if learned:
        out += ["", "<b>Lo he aprendido</b>"] + [f"<code>{r['id']}</code> {esc(r['text'])}" for r in learned]
    out += ["", "<i>Olvida una nota con /forget n</i>"]
    return "\n".join(out)


# --- actions shared by commands and natural language ------------------------------------

def _need(conn: sqlite3.Connection, ref) -> sqlite3.Row:
    tx = tx_by_ref(conn, ref)
    if tx is None:
        raise ValueError(f"No encuentro el movimiento {ref}")
    return tx


def do_action(conn: sqlite3.Connection, a: dict) -> str:
    kind = a.get("action")
    if kind == "split":
        tx = _need(conn, a.get("ref"))
        share = set_split(conn, tx, people=a.get("people"), my_share=a.get("my_share"), note=a.get("note"))
        return f"✂️ <b>#{tx['ref']} compartido</b>\nTu parte: <b>{eur(share)}</b> de {eur(-tx['amount'])}"
    if kind == "settled":
        tx = _need(conn, a.get("ref"))
        close_split(conn, tx["id"])
        return f"✅ <b>#{tx['ref']} cerrado</b>: ya no espero más devoluciones"
    if kind == "link":
        transfer, expense = _need(conn, a.get("transfer_ref")), _need(conn, a.get("expense_ref"))
        link_transfer(conn, transfer, "reimbursement", expense["id"], note=a.get("note"))
        return (f"🔗 <b>#{transfer['ref']}</b> ({eur(transfer['amount'], sign=True)}) ahora cuenta como "
                f"devolución de <b>#{expense['ref']}</b>")
    if kind == "kind":
        transfer = _need(conn, a.get("ref"))
        link_transfer(conn, transfer, a.get("kind"), category=a.get("category"), note=a.get("note"))
        label = KIND_ES.get(a.get("kind"), a.get("kind"))
        cat = f" ({esc(CATEGORY_ES.get(a['category'], a['category']))})" if a.get("category") else ""
        return f"🏷️ <b>#{transfer['ref']}</b> marcado como <b>{esc(label)}</b>{cat}"
    if kind == "category":
        tx = _need(conn, a.get("ref"))
        if a.get("category") not in CATEGORIES:
            raise ValueError(f"categoría desconocida; usa una de: {', '.join(CATEGORIES)}")
        set_merchant_category(conn, tx["merchant"], a["category"], "user")
        conn.commit()
        label = esc(CATEGORY_ES.get(a["category"], a["category"]))
        return (f"🏷️ <b>{esc(tx['merchant'])}</b> → {emoji(a['category'])} {label}"
                "\n<i>Se aplica a todos sus movimientos.</i>")
    if kind == "remember":
        note_id = add_user_note(conn, str(a.get("text", "")))
        return f"🧠 <b>Lo recordaré</b> (nota {note_id})."
    if kind == "forget":
        if not forget_note(conn, int(a.get("note_id", 0))):
            raise ValueError("no existe esa nota")
        return "🧠 Nota olvidada."
    if kind == "one_off":
        tx = _need(conn, a.get("ref"))
        conn.execute("UPDATE transactions SET one_off = 1 WHERE id = ?", (tx["id"],))
        conn.commit()
        return f"1️⃣ <b>#{tx['ref']} es puntual</b>: no contará para tus cifras habituales."
    if kind == "normal":
        tx = _need(conn, a.get("ref"))
        mark_expected(conn, tx["merchant"], -tx["amount"])
        conn.commit()
        return f"🙂 <b>{esc(tx['merchant'])}</b> es normal para ti: no te avisaré por gastos así."
    raise ValueError(f"acción desconocida {kind!r}")


# --- slash commands --------------------------------------------------------------------------

def handle_command(conn: sqlite3.Connection, settings: Settings, text: str) -> str:
    parts = text.split()
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return _status(conn, settings)
    if cmd == "/recent":
        return _recent(conn, settings)
    if cmd == "/owed":
        return _owed(conn)
    if cmd == "/split" and len(args) >= 2:
        a = {"action": "split", "ref": args[0]}
        if args[1].startswith("share="):
            a["my_share"] = float(args[1].split("=", 1)[1].replace(",", "."))
        else:
            a["people"] = int(args[1])
        return do_action(conn, a)
    if cmd == "/settled" and args:
        return do_action(conn, {"action": "settled", "ref": args[0]})
    if cmd == "/link" and len(args) >= 2:
        return do_action(conn, {"action": "link", "transfer_ref": args[0], "expense_ref": args[1]})
    if cmd == "/kind" and len(args) >= 2:
        return do_action(conn, {"action": "kind", "ref": args[0], "kind": args[1],
                                "category": args[2] if len(args) > 2 else None})
    if cmd == "/debug":
        from . import debug
        if args and args[0].lower() in ("on", "si", "sí"):
            days = int(args[1]) if len(args) > 1 and args[1].isdigit() else debug.DEFAULT_DAYS
            debug.enable(conn, days)
            return (debug.status_text(conn, settings)
                    + "\nRecibirás un informe tras cada ejecución (4 al día).")
        if args and args[0].lower() in ("off", "no"):
            debug.disable(conn)
        return debug.status_text(conn, settings)
    if cmd == "/habits":
        return _habits(conn)
    if cmd == "/forget" and args:
        return do_action(conn, {"action": "forget", "note_id": args[0]})
    if cmd == "/learn":
        insights = review_habits(conn, settings)
        found = "\n\n" + "\n".join(f"• {esc(i)}" for i in insights) if insights else ""
        return f"🧠 <b>Perfil actualizado</b>{found}\n\n<i>Míralo con /habits</i>"
    if cmd == "/cat" and len(args) >= 2:
        return do_action(conn, {"action": "category", "ref": args[0], "category": args[1]})
    usage = {
        "/split": "/split #ref 4  o  /split #ref share=12,50", "/settled": "/settled #ref",
        "/link": "/link #bizum #gasto", "/kind": "/kind #ref share eating_out", "/cat": "/cat #ref groceries",
        "/forget": "/forget n  (el número de la nota en /habits)",
    }
    if cmd in usage:
        return f"✍️ Uso: <code>{esc(usage[cmd])}</code>"
    return "🤔 No he entendido ese comando.\n\n" + HELP


# --- free text -----------------------------------------------------------------------------------

def handle_text(conn: sqlite3.Connection, settings: Settings, text: str) -> str:
    today = _local_now(settings).date()
    stats = month_stats(conn, settings, today)
    prompt = f"""The person wrote to you on Telegram:
<message>{text}</message>

Answer their question and/or carry out what they ask using actions. Refer to transactions by #ref.

Available actions (only use refs that exist in the data below):
- {{"action": "split", "ref": N, "people": N}} or {{"action": "split", "ref": N, "my_share": EUR}} — they paid \
a group expense and expect Bizums back (people includes them)
- {{"action": "settled", "ref": N}} — stop expecting more money back for that expense
- {{"action": "link", "transfer_ref": N, "expense_ref": N}} — an incoming Bizum pays back that expense
- {{"action": "kind", "ref": N, "kind": K, "category": C}} — reinterpret a Bizum. Incoming kinds: \
{sorted(IN_KINDS)}; outgoing kinds: {sorted(OUT_KINDS)}; category only for outgoing share/gift/other
- {{"action": "category", "ref": N, "category": C}} — recategorise that transaction's merchant
- {{"action": "remember", "text": "..."}} — store a fact about their habits/plans they want you to keep in mind \
(write it in {settings.language}, short and self-contained)
- {{"action": "forget", "note_id": N}} — delete a habit note (ids are listed with the notes below)
- {{"action": "one_off", "ref": N}} — an exceptional expense that shouldn't count towards their usual spending
- {{"action": "normal", "ref": N}} — spending like that at that merchant is normal for them; stop alerting
Categories: {', '.join(CATEGORIES)}

If the request is ambiguous (e.g. two possible dinners), ask instead of acting.

{habits_context(conn)}
Habit note ids: {json.dumps({r["id"]: r["text"][:60] for r in notes(conn)}, ensure_ascii=False)}

This month's statistics (JSON):
{json.dumps(_compact_stats(stats), ensure_ascii=False)}

Transactions of the last 60 days (JSON):
{json.dumps(recent_transactions(conn, today - timedelta(days=60), 400), ensure_ascii=False)}

Reply with ONLY a JSON object: {{"reply": "<your answer, formatted as described in the system prompt>", \
"actions": [...]}}"""
    reply = ask_json(settings, prompt, _system(settings))
    results = []
    for a in reply.get("actions") or []:
        try:
            results.append(do_action(conn, a))
        except (ValueError, TypeError) as e:
            results.append(f"⚠️ No he podido hacer <i>{esc(a.get('action'))}</i>: {esc(e)}")
    return "\n\n".join(filter(None, [llm_html(reply.get("reply", "")), "\n".join(results)])) or "👍"


# --- feedback buttons -------------------------------------------------------------------------------

def _handle_button(conn: sqlite3.Connection, settings: Settings, cq: dict) -> None:
    message = cq.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id"))
    if chat_id != str(settings.telegram_chat_id):
        return
    data = cq.get("data", "")
    prefix = data.split(":", 1)[0]
    confirmation = None
    try:
        if prefix == "fb":  # feedback: fb:<notification id>:<verdict>
            _, notification_id, verdict = data.split(":", 2)
            reply = record_feedback(conn, int(notification_id), verdict)
        elif prefix == "sp":  # split: sp:<ref>:<people>
            _, ref, people = data.split(":", 2)
            confirmation = do_action(conn, {"action": "split", "ref": ref, "people": int(people)})
            reply = strip_html(confirmation).replace("\n", " · ")
        else:
            raise ValueError(data)
    except ValueError as e:
        reply = "No he entendido ese botón." if prefix not in ("fb", "sp") else f"⚠️ {e}"
    try:
        telegram.answer_callback(settings, cq["id"], reply[:190])
        # Remove only the row group that was used (feedback or split); keep the others.
        rows = (message.get("reply_markup") or {}).get("inline_keyboard", [])
        remaining = [row for row in rows if not any(b.get("callback_data", "").startswith(prefix + ":") for b in row)]
        telegram.set_buttons(settings, chat_id, message["message_id"], remaining)
        if confirmation:
            telegram.send(settings, confirmation, chat_id, reply_markup=MAIN_KEYBOARD)
    except Exception as e:  # cosmetic; the action is already stored
        log.warning("could not acknowledge button: %s", e)


# --- loop -------------------------------------------------------------------------------------------

def run_bot(conn: sqlite3.Connection, settings: Settings) -> None:
    if not settings.telegram_chat_id:
        raise SystemExit("Set TELEGRAM_CHAT_ID first (see `finmon telegram-setup`).")
    offset = int(kv_get(conn, "tg_offset", "0") or 0) or None
    try:
        telegram.set_commands(settings, COMMANDS)
    except Exception as e:  # cosmetic
        log.warning("could not set the command menu: %s", e)
    log.info("bot started")
    while True:
        try:
            updates = telegram.get_updates(settings, offset)
        except Exception as e:  # network hiccups: back off and retry
            log.warning("getUpdates failed: %s", e)
            time.sleep(10)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            kv_set(conn, "tg_offset", str(offset))
            if "callback_query" in u:
                _handle_button(conn, settings, u["callback_query"])
                continue
            msg = u.get("message") or {}
            chat_id, text = str(msg.get("chat", {}).get("id")), (msg.get("text") or "").strip()
            if chat_id != str(settings.telegram_chat_id):
                who = (msg.get("from") or {}).get("first_name", "?")
                log.warning("Ignoring message from unknown chat %s (%s). If that's you, set TELEGRAM_CHAT_ID=%s "
                            "in .env and restart the bot.", chat_id, who, chat_id)
                print(f"\n>>> Message from chat id {chat_id} ({who}). If that's you: TELEGRAM_CHAT_ID={chat_id}\n",
                      flush=True)
                continue
            if not text:
                continue
            text = MENU.get(text, text)  # keyboard buttons send their label
            try:
                answer = handle_command(conn, settings, text) if text.startswith("/") else \
                    handle_text(conn, settings, text)
            except (ValueError, LLMError) as e:
                answer = f"⚠️ {esc(e)}"
            except Exception:
                log.exception("error handling message")
                answer = "⚠️ Algo ha fallado, revisa los logs del servidor."
            telegram.send(settings, answer, chat_id, reply_markup=MAIN_KEYBOARD)
