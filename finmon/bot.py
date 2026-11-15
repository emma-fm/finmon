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
from .learning import add_user_note, forget_note, habits_context, mark_expected, notes, record_feedback, review_habits
from .llm import LLMError, ask_json
from .splits import IN_KINDS, KIND_ES, OUT_KINDS, close_split, link_transfer, open_splits, set_split, tx_by_ref
from .stats import month_stats, recent_transactions

log = logging.getLogger(__name__)

HELP = """Comandos:
/status — cómo va el mes
/recent — últimos movimientos con su #ref
/owed — gastos compartidos que aún no te han devuelto del todo
/split #ref <personas> — pagué por <personas> (incluyéndome); espero Bizums de vuelta
/split #ref share=<€> — lo mismo, indicando mi parte en euros
/settled #ref — dejar de esperar más dinero de vuelta por #ref
/link #bizum #gasto — este Bizum recibido devuelve ese gasto
/kind #bizum <tipo> [categoría] — p. ej. /kind #31 share eating_out, /kind #32 loan_out
/cat #ref <categoría> — cambiar la categoría de ese comercio
/habits — lo que he aprendido de tus hábitos
/forget <n> — olvidar la nota n de /habits
/learn — revisar ahora tus hábitos con todo el historial
/debug on [días] — enviarte un informe de todo lo que proceso en cada ejecución (por defecto 21 días)
/debug off — desactivarlo · /debug — ver su estado

Los botones de cada mensaje me enseñan: «Es normal» deja de avisarte por ese comercio, \
«Puntual» excluye el gasto de tus cifras habituales y 👍/👎 ajustan cuánto te escribo.

O simplemente escríbeme, p. ej. “la cena del viernes éramos 4”, \
“el #45 es Marta devolviéndome lo del concierto”, “recuerda que en diciembre gasto más en regalos”, \
“¿cuánto he gastado en Glovo este mes?”"""


def _fmt(r: dict) -> str:
    who = r.get("counterparty") or r.get("description") or ""
    extra = ""
    if r.get("bizum_kind"):
        extra = f" · bizum: {KIND_ES.get(r['bizum_kind'], r['bizum_kind'])}"
        if r.get("reimburses_ref"):
            extra += f" de #{r['reimburses_ref']}"
    if r.get("split_my_share") is not None:
        extra += f" · compartido, mi parte {r['split_my_share']:.2f} €"
    return f"#{r['ref']} {r['booking_date']} {r['amount']:+.2f} € {who[:40]} [{r.get('category', '?')}]{extra}"


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
        return f"✂️ #{tx['ref']} compartido: tu parte es {share:.2f} € de {-tx['amount']:.2f} €"
    if kind == "settled":
        tx = _need(conn, a.get("ref"))
        close_split(conn, tx["id"])
        return f"✅ #{tx['ref']} cerrado: ya no espero más devoluciones"
    if kind == "link":
        transfer, expense = _need(conn, a.get("transfer_ref")), _need(conn, a.get("expense_ref"))
        link_transfer(conn, transfer, "reimbursement", expense["id"], note=a.get("note"))
        return f"🔗 #{transfer['ref']} ({transfer['amount']:+.2f} €) ahora cuenta como devolución de #{expense['ref']}"
    if kind == "kind":
        transfer = _need(conn, a.get("ref"))
        link_transfer(conn, transfer, a.get("kind"), category=a.get("category"), note=a.get("note"))
        label = KIND_ES.get(a.get("kind"), a.get("kind"))
        return f"🏷️ #{transfer['ref']} marcado como {label}" + (f" ({a['category']})" if a.get("category") else "")
    if kind == "category":
        tx = _need(conn, a.get("ref"))
        if a.get("category") not in CATEGORIES:
            raise ValueError(f"categoría desconocida; usa una de: {', '.join(CATEGORIES)}")
        set_merchant_category(conn, tx["merchant"], a["category"], "user")
        conn.commit()
        return f"🏷️ '{tx['merchant']}' → {a['category']} (se aplica a todos sus movimientos)"
    if kind == "remember":
        note_id = add_user_note(conn, str(a.get("text", "")))
        return f"🧠 Lo recordaré (nota {note_id})."
    if kind == "forget":
        if not forget_note(conn, int(a.get("note_id", 0))):
            raise ValueError("no existe esa nota")
        return "🧠 Nota olvidada."
    if kind == "one_off":
        tx = _need(conn, a.get("ref"))
        conn.execute("UPDATE transactions SET one_off = 1 WHERE id = ?", (tx["id"],))
        conn.commit()
        return f"1️⃣ #{tx['ref']} marcado como puntual: no contará para lo habitual."
    if kind == "normal":
        tx = _need(conn, a.get("ref"))
        mark_expected(conn, tx["merchant"], -tx["amount"])
        conn.commit()
        return f"🙂 '{tx['merchant']}' es normal para ti: no te avisaré por gastos así."
    raise ValueError(f"acción desconocida {kind!r}")


# --- slash commands --------------------------------------------------------------------------

def handle_command(conn: sqlite3.Connection, settings: Settings, text: str) -> str:
    parts = text.split()
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        s = month_stats(conn, settings, _local_now(settings).date())
        ref = s["monthly_budget"] or s["usual_month_total"]
        lines = [
            f"📊 Llevas {s['spent_this_month']:.2f} € gastados este mes (día {s['day_of_month']}/{s['days_in_month']})",
            f"Previsión: {s['projected_month_total']:.0f} €" + (f" frente a {ref:.0f} € de referencia" if ref else ""),
            f"Estado: {STATUS_ES[s['status']]}",
        ]
        if s["balance"] is not None:
            lines.append(f"Saldo: {s['balance']:.2f} €")
        lines += [f"  {CATEGORY_ES.get(k, k)}: {v['this_month']:.0f} € (habitual {v['usual_full_month']:.0f})"
                  for k, v in list(s["category_vs_usual"].items())[:8]]
        owed = sum(o["still_owed"] for o in s["owed_to_you"])
        if owed:
            lines.append(f"Te deben: {owed:.2f} € (/owed)")
        return "\n".join(lines)
    if cmd == "/recent":
        since = _local_now(settings).date() - timedelta(days=14)
        return "\n".join(_fmt(r) for r in recent_transactions(conn, since, 25)) or "No hay movimientos."
    if cmd == "/owed":
        rows = open_splits(conn)
        if not rows:
            return "Nadie te debe nada 🎉"
        return "\n".join(
            f"#{o['ref']} {o['booking_date']} {o['counterparty'] or o['merchant']}: pagaste {o['gross']:.2f}, "
            f"tu parte {o['my_share']:.2f}, recibido {o['reimbursed']:.2f} → te deben {o['owed']:.2f} €"
            for o in rows)
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
                    + "\nRecibirás un informe tras cada ejecución (4 al día) con todo lo procesado.")
        if args and args[0].lower() in ("off", "no"):
            debug.disable(conn)
        return debug.status_text(conn, settings)
    if cmd == "/habits":
        rows = notes(conn)
        if not rows:
            return "Aún no he aprendido nada: necesito algo más de historial (lo reviso cada domingo)."
        return "🧠 Lo que sé de tus hábitos:\n" + "\n".join(
            f"{r['id']}. {r['text']}" + (" (me lo dijiste tú)" if r["source"] == "user" else "") for r in rows)
    if cmd == "/forget" and args:
        return do_action(conn, {"action": "forget", "note_id": args[0]})
    if cmd == "/learn":
        insights = review_habits(conn, settings)
        return ("🧠 Perfil actualizado." + ("\n" + "\n".join(f"• {i}" for i in insights) if insights else "")
                + "\nMíralo con /habits")
    if cmd == "/cat" and len(args) >= 2:
        return do_action(conn, {"action": "category", "ref": args[0], "category": args[1]})
    return "No he entendido ese comando.\n\n" + HELP


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

Reply with ONLY a JSON object: {{"reply": "<your answer, plain text>", "actions": [...]}}"""
    reply = ask_json(settings, prompt, _system(settings))
    results = []
    for a in reply.get("actions") or []:
        try:
            results.append(do_action(conn, a))
        except (ValueError, TypeError) as e:
            results.append(f"⚠️ no he podido hacer {a.get('action')}: {e}")
    return "\n".join(filter(None, [reply.get("reply", "").strip(), *results])) or "👍"


# --- feedback buttons -------------------------------------------------------------------------------

def _handle_button(conn: sqlite3.Connection, settings: Settings, cq: dict) -> None:
    message = cq.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id"))
    if chat_id != str(settings.telegram_chat_id):
        return
    try:
        _, notification_id, verdict = cq.get("data", "").split(":", 2)
        reply = record_feedback(conn, int(notification_id), verdict)
    except ValueError:
        reply = "No he entendido ese botón."
    try:
        telegram.answer_callback(settings, cq["id"], reply)
        telegram.remove_buttons(settings, chat_id, message["message_id"])
    except Exception as e:  # cosmetic; the feedback is already stored
        log.warning("could not acknowledge button: %s", e)


# --- loop -------------------------------------------------------------------------------------------

def run_bot(conn: sqlite3.Connection, settings: Settings) -> None:
    if not settings.telegram_chat_id:
        raise SystemExit("Set TELEGRAM_CHAT_ID first (see `finmon telegram-setup`).")
    offset = int(kv_get(conn, "tg_offset", "0") or 0) or None
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
            try:
                answer = handle_command(conn, settings, text) if text.startswith("/") else \
                    handle_text(conn, settings, text)
            except (ValueError, LLMError) as e:
                answer = f"⚠️ {e}"
            except Exception:
                log.exception("error handling message")
                answer = "⚠️ Algo ha fallado, revisa los logs del servidor."
            telegram.send(settings, answer, chat_id)
