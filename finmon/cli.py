"""Command-line entry point: `python -m finmon <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analysis, telegram
from .categorize import CATEGORIES, recategorise_all
from .config import load_settings
from .db import connect


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="finmon", description="Personal finance monitor")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("banks", help="list banks available in your country (to find the exact name)")
    s.add_argument("--search", default="", help="filter by part of the name")
    sub.add_parser("link", help="connect / renew access to your bank account")
    s = sub.add_parser("link-complete", help="finish linking with the URL you were redirected to")
    s.add_argument("redirect_url")
    sub.add_parser("sync", help="fetch new transactions only")
    s = sub.add_parser("run", help="sync + alerts + due digests (what the timer runs)")
    s.add_argument("--no-sync", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="print messages instead of sending them")
    s = sub.add_parser("digest", help="force a digest now")
    s.add_argument("kind", choices=["daily", "weekly", "monthly"])
    s.add_argument("--dry-run", action="store_true")
    sub.add_parser("doctor", help="check that the configuration works (bank key, Telegram, LLM)")
    s = sub.add_parser("debug", help="send a Telegram report of everything each run processes")
    s.add_argument("state", choices=["on", "off", "status"], nargs="?", default="status")
    s.add_argument("days", type=int, nargs="?", default=21)
    s = sub.add_parser("import-csv", help="backfill history from a bank CSV export")
    s.add_argument("file", type=Path)
    s = sub.add_parser("learn", help="review spending habits now and update the learned profile")
    s.add_argument("--dry-run", action="store_true")
    sub.add_parser("habits", help="show what has been learned")
    sub.add_parser("recategorize", help="re-apply category rules to all transactions")
    sub.add_parser("categories", help="list valid categories")
    sub.add_parser("telegram-setup", help="show the chat id of whoever messaged your bot")
    sub.add_parser("bot", help="run the Telegram bot (long polling)")
    s = sub.add_parser("ask", help="ask a question from the terminal")
    s.add_argument("question", nargs="+")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    conn = connect(settings.db_path)

    if args.cmd == "banks":
        from .sync import client
        for a in client(settings).aspsps(settings.eb_country):
            if args.search.lower() in a["name"].lower():
                days = int(a.get("maximum_consent_validity") or 0) // 86400
                print(f"{a['name']!r}  (max consent {days} days, psu_types={a.get('psu_types')})")

    elif args.cmd == "link":
        from .sync import start_link
        url = start_link(settings)
        print("1. Open this URL and log in to your bank:\n\n   " + url)
        print(f"\n2. You'll be sent to {settings.eb_redirect_url}?code=... (the page may not load; that's fine).")
        print("3. Copy the full address from the browser bar and run:\n\n   finmon link-complete '<that URL>'")

    elif args.cmd == "link-complete":
        from .sync import complete_link
        q = parse_qs(urlparse(args.redirect_url).query)
        code = q.get("code", [args.redirect_url])[0]
        if "error" in q:
            sys.exit(f"Bank returned an error: {q['error']}")
        accounts = complete_link(conn, settings, code)
        print(f"Linked {len(accounts)} account(s):")
        for a in accounts:
            print(f"  - {(a.get('account_id') or {}).get('iban')} {a.get('name') or a.get('product') or ''}")
        print("Now run: finmon sync")

    elif args.cmd == "sync":
        from .sync import sync
        print(f"{sync(conn, settings)} new transactions")

    elif args.cmd == "run":
        import time

        from . import debug, llm
        from .sync import consent_days_left, sync
        started = time.monotonic()
        new = None
        if not args.no_sync:
            try:
                new = sync(conn, settings)
            except Exception as e:  # keep going: alerts/digests still work on stored data
                logging.exception("sync failed")
                debug.log(conn, f"⚠️ Sincronización fallida: {str(e)[:300]}")
                if not args.dry_run:
                    _warn_once(conn, settings, f"⚠️ Falló la sincronización con el banco: {str(e)[:300]}")
            days_left = consent_days_left(conn)
            if days_left is not None:
                debug.log(conn, f"Consentimiento del banco: quedan {days_left:.0f} días")
            analysis.check_consent(conn, settings, days_left)
        try:
            analysis.run_scheduled(conn, settings, dry_run=args.dry_run)
        finally:
            u = llm.usage
            debug.section(conn, debug.SUMMARY)
            debug.log(conn, "Movimientos nuevos: " + (str(new) if new is not None else "— (sin sincronizar)"))
            debug.log(conn, f"LLM {settings.llm_provider}/{settings.llm_model or 'por defecto'}: "
                            f"{u['calls']} llamadas, {u['errors']} errores, {u['seconds']:.0f} s")
            debug.log(conn, f"Duración total: {time.monotonic() - started:.0f} s")
            now = datetime.now(settings.timezone)
            debug.flush(conn, settings, f"ejecución {now:%d/%m %H:%M}", dry_run=args.dry_run)

    elif args.cmd == "doctor":
        sys.exit(0 if doctor(conn, settings) else 1)

    elif args.cmd == "debug":
        from . import debug
        if args.state == "on":
            until = debug.enable(conn, args.days)
            print(f"Debug on until {until:%Y-%m-%d %H:%M} UTC")
        elif args.state == "off":
            debug.disable(conn)
            print("Debug off")
        else:
            print("on" if debug.is_on(conn) else "off")

    elif args.cmd == "digest":
        msg = analysis.digest(conn, settings, args.kind, dry_run=args.dry_run)
        if msg is None:
            print("(the LLM decided nothing was worth sending)")

    elif args.cmd == "import-csv":
        from .csv_import import import_csv
        print(f"{import_csv(conn, args.file)} transactions imported")

    elif args.cmd == "learn":
        from .learning import review_habits
        for insight in review_habits(conn, settings, dry_run=args.dry_run):
            print("• " + insight)

    elif args.cmd == "habits":
        from .learning import notes
        for r in notes(conn):
            print(f"{r['id']:>3}. [{r['source']}] {r['text']}")

    elif args.cmd == "recategorize":
        print(f"{recategorise_all(conn)} transactions re-categorised")

    elif args.cmd == "categories":
        print("\n".join(CATEGORIES))

    elif args.cmd == "telegram-setup":
        try:
            updates = telegram.get_updates(settings, None, timeout=0)
        except RuntimeError as e:
            if "409" in str(e) or "Conflict" in str(e):
                sys.exit("The bot is running (`fm bot`) and receiving the messages. Stop it with Ctrl+C, send your "
                         "bot a new message, and run this again. Or just look at the bot's output: it prints "
                         "the chat id of messages from unknown chats.")
            raise
        if not updates:
            print("No messages yet: send any message to your bot in Telegram, then run this again.")
        for u in updates:
            chat = (u.get("message") or {}).get("chat", {})
            print(f"chat id {chat.get('id')}  ({chat.get('first_name', '')} {chat.get('username', '')})")
        print("Put your chat id in .env as TELEGRAM_CHAT_ID.")

    elif args.cmd == "bot":
        from .bot import run_bot
        run_bot(conn, settings)

    elif args.cmd == "ask":
        from .bot import handle_text
        print(handle_text(conn, settings, " ".join(args.question)))


def doctor(conn, settings) -> bool:
    """Check each piece of the setup and say what to do next. Returns True if all essentials pass."""
    ok = True

    def check(name: str, passed: bool, detail: str = "", essential: bool = True) -> None:
        nonlocal ok
        mark = "✅" if passed else ("❌" if essential else "⚠️ ")
        print(f"{mark} {name}" + (f": {detail}" if detail else ""))
        if essential and not passed:
            ok = False

    print(f"Config: {settings.data_dir} (database), .env loaded from project dir\n")

    # Bank
    check("EB_APP_ID set", bool(settings.eb_app_id),
          "" if settings.eb_app_id else "add your Enable Banking application id")
    key = settings.eb_private_key_path
    check("Enable Banking private key", key.exists(), str(key) if key.exists() else f"put the .pem at {key}")
    if key.exists() and (key.stat().st_mode & 0o077):
        check("Private key permissions", False, f"run: chmod 600 {key}", essential=False)
    if settings.eb_app_id and key.exists():
        try:
            import difflib

            from .sync import client
            names = [a["name"] for a in client(settings).aspsps(settings.eb_country)]
            found = settings.eb_aspsp_name in names
            similar = difflib.get_close_matches(settings.eb_aspsp_name, names, n=5, cutoff=0.3)
            check(f"Bank '{settings.eb_aspsp_name}' available in {settings.eb_country}", found,
                  "" if found else f"set EB_ASPSP_NAME to the exact name; close matches: {similar or '—'} "
                                   "(full list: finmon banks)")
        except Exception as e:
            check("Enable Banking API reachable with your key", False, str(e)[:200])
    accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    from .sync import consent_days_left
    days = consent_days_left(conn)
    check("Bank account linked", accounts > 0,
          f"{accounts} account(s), consent valid {days:.0f} more days" if accounts and days is not None
          else "run: python -m finmon link", essential=False)

    # Telegram
    if settings.telegram_bot_token:
        try:
            me = telegram._api(settings, "getMe")
            check("Telegram bot token", True, f"@{me.get('username')}")
        except Exception as e:
            check("Telegram bot token", False, str(e)[:200])
    else:
        check("Telegram bot token", False, "create a bot with @BotFather and set TELEGRAM_BOT_TOKEN")
    check("TELEGRAM_CHAT_ID set", bool(settings.telegram_chat_id),
          "" if settings.telegram_chat_id else "message your bot, then run: python -m finmon telegram-setup")
    bot_id = settings.telegram_bot_token.split(":", 1)[0]
    if settings.telegram_chat_id and settings.telegram_chat_id == bot_id:
        check("TELEGRAM_CHAT_ID is your chat", False,
              "it's the bot's own id (the number in the token). Message the bot and run telegram-setup, "
              "or start `fm bot`: it prints the chat id of whoever writes to it")

    # LLM
    from .llm import LLMError, ask_json
    try:
        reply = ask_json(settings, 'Reply with exactly this JSON object: {"ok": true}', "You are a health check.")
        check(f"LLM ({settings.llm_provider}/{settings.llm_model or 'default'})", reply.get("ok") is True,
              "" if reply.get("ok") is True else f"unexpected answer: {reply}")
    except LLMError as e:
        check(f"LLM ({settings.llm_provider})", False, str(e)[:300])

    print("\nAll essentials OK." if ok else "\nFix the ❌ items above and run doctor again.")
    return ok


def _warn_once(conn, settings, text: str) -> None:
    """Send sync-failure warnings at most once a day."""
    from datetime import date

    from .db import kv_get, kv_set
    if kv_get(conn, "sync_warned_on") != date.today().isoformat():
        telegram.send(settings, text)
        kv_set(conn, "sync_warned_on", date.today().isoformat())


if __name__ == "__main__":
    main()
