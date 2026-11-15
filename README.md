# finmon — personal finance monitor

finmon watches a Spanish bank account and messages you on Telegram when something deserves attention:
unexpected expenses, a month heading over your usual spending or budget, weekly summaries and monthly recaps.
It understands shared costs settled with **Bizum**, learns your spending habits over time, and lets you ask
questions or correct it in plain language.

- **Bank data:** read-only PSD2 access through [Enable Banking](https://enablebanking.com). Any bank they
  support works; it's free when you only connect your own accounts.
- **Analysis:** numbers are computed in Python, and an LLM of your choice interprets and writes the messages
  (Claude, OpenAI, a local Ollama model…).
- **Interface:** a Telegram bot. Messages are in Spanish; the LLM's language is configurable.
- **Hosting:** any always-on Linux machine (home server, Raspberry Pi, small cloud VM) or your desktop.

It can run with no recurring cost if you use free tiers, your own machine and a local or existing LLM.

```
Bank ──PSD2──▶ Enable Banking ──▶ finmon (4×/day) ──▶ SQLite
                                     │  numbers computed in Python
                                     ▼
                                 LLM provider ──▶ Telegram bot ◀──▶ you
```

## What it does

| When | What |
|---|---|
| Every run (08:00, 13:00, 18:00, 21:30) | New transactions are categorised, new Bizums interpreted, and unusual expenses alerted: large for you in that category, much more than usual at that merchant, or a big first purchase somewhere new. |
| Evening, daily | The LLM decides whether something is worth telling you: over pace, a category running hot, a new recurring charge, encouragement when earned, or money still owed to you. Otherwise it stays quiet. |
| Sunday evening | Weekly summary, plus what it learned about your habits. |
| 1st of the month | Recap of the previous month. |
| Anytime | Ask the bot anything, or tell it about shared expenses. |

**Pace and projection.** Spending so far is compared with what you usually had spent *by the same day of the
month* over the last 3 months. The projection assumes the rest of the month goes as usual, so fixed charges
early in the month don't look like overspending. Set `MONTHLY_BUDGET` to compare against a budget instead.

### Shared costs with Bizum

- **You paid, others pay you back.** Incoming Bizums are matched to the expense by person, amount
  (≈ total / N), timing and, when the bank provides it, the Bizum concept. Only *your share* counts as
  spending, and repayments are not income. Declare a split up front (`/split #108 4`, or "la cena del viernes
  éramos 4") and the rest shows as **owed to you** until paid, with a reminder after a week.
- **Someone else paid, you send your share.** The outgoing Bizum gets the real category (restaurants,
  gifts…) instead of "bizum".
- **Loans, repayments, gifts and income** sent by Bizum are classified so they don't distort your numbers.
  `Pago Bizum en <shop>` (paying a shop with Bizum) is treated as a normal purchase.
- When unsure about a recent Bizum, it tells you its guess and you can correct it in plain language.

### Learning your habits

- **Habits profile.** Every week the LLM reviews up to 12 months of history and keeps short notes:
  fixed charges, routines, typical spend per category, seasonal months, weekday patterns, how shared
  expenses usually go, and trends. Every alert and summary reads them, and an alert is skipped if your
  habits explain the expense. `/habits` shows them, `/learn` refreshes them, and you can add your own
  ("recuerda que en diciembre gasto más en regalos").
- **Feedback buttons** on every message. *Es normal* stops alerts for that merchant up to that amount,
  *Puntual* keeps a one-off out of your "usual" baselines, and 👍/👎 tune how often it messages you.
- **Adaptive thresholds.** "Large" is relative to your own spending in each category. The same month last
  year is used to recognise seasonal spending.

### Bot commands

`/status`, `/recent` (shows `#ref` numbers), `/owed`, `/split #ref 4`, `/split #ref share=25`,
`/settled #ref`, `/link #bizum #expense`, `/kind #ref share eating_out`, `/cat #ref groceries`, `/habits`,
`/learn`, `/forget n`, `/debug on|off`. Anything else is answered by the LLM, which can also take these
actions for you.

## Setup

Requires Python ≥ 3.11 and Linux with systemd for scheduling (the code itself runs anywhere).
In the commands below, `fm` stands for `.venv/bin/python -m finmon`.

### 1. Install
```bash
git clone <repo-url> finmon && cd finmon
./deploy/install.sh                 # venv, dependencies, .env from template, systemd user units
                                    # on a headless server: FINMON_SERVER=1 ./deploy/install.sh
```

### 2. Bank access (Enable Banking)
1. Sign in at enablebanking.com → Control Panel → **API applications** → register an application in
   **Production**. Let the browser generate the key, and add `https://localhost/finmon-callback` as a
   redirect URL. Move the downloaded `<application-id>.pem` to `~/.config/finmon/enablebanking.pem`
   (`chmod 600`), and put the application ID in `.env` as `EB_APP_ID`.
2. Click **Activate by linking accounts** and authorise your bank. This turns on the free restricted mode,
   which only works for accounts you link yourself.
3. Find your bank's exact name with `fm banks --search <part of the name>` and set `EB_ASPSP_NAME`.
4. `fm link`: open the URL, authorise, then copy the address you land on (the page won't load, which is
   fine) and run `fm link-complete '<url>'`.
5. `fm sync` downloads the available history (typically 90 days). The first sync never sends alerts.

Consent lasts up to about 6 months (`EB_CONSENT_DAYS`). The bot warns you a week before it expires, and
`fm link` renews it. Banks allow only about 4 unattended reads per day, which is why the timer runs 4×/day.

### 3. LLM
The default `claude-code` provider needs the Claude Code CLI logged in on the machine. On a headless server,
run `claude setup-token` and set `CLAUDE_CODE_OAUTH_TOKEN`. For other providers, see
[Switching the LLM](#switching-the-llm).

### 4. Telegram
1. Talk to @BotFather → `/newbot` → set `TELEGRAM_BOT_TOKEN`.
2. Send your bot a message, then run `fm telegram-setup` (or start `fm bot` and read the chat id it prints),
   and set `TELEGRAM_CHAT_ID`. The bot ignores everyone else.

### 5. Check and start
```bash
fm doctor                               # checks the bank key, bank name, Telegram and the LLM
fm run --dry-run                        # shows what it would send; nothing is sent or saved
systemctl --user enable --now finmon-run.timer finmon-bot.service
journalctl --user -u finmon-run -f      # logs
```

### Debug mode (recommended for the first weeks)
Send `/debug on` to the bot (auto-off after 21 days, or `/debug on 14`). After every run you get a report with
everything processed:
- new transactions and their categories, and balances
- how each Bizum was interpreted, with confidence and reasoning
- why each expense was or wasn't flagged
- the figures behind the daily check
- what the habit review changed
- LLM calls, errors and timing

`/debug off` turns it off.

## Switching the LLM

All model calls go through `finmon/llm/`, a small provider interface (`complete(prompt, system, json_mode)
-> str`). Prompts are plain text with no vendor-specific features. Choose the provider in `.env`:

| `LLM_PROVIDER` | What |
|---|---|
| `claude-code` (default) | Claude Code CLI, `claude -p` with all tools disabled; uses a Claude subscription |
| `anthropic` | Anthropic API via the official SDK (`pip install anthropic`), pay per use |
| `openai-compatible` | Any OpenAI-compatible API: OpenAI, **Ollama (local, free)**, Mistral, Groq, OpenRouter, Gemini, LM Studio… |
| `command` | Any CLI that reads the prompt on stdin (`ollama run …`, `llm -m …`) |

To add a provider, write a class with `complete()` in `finmon/llm/` and register it in `_build()`. If the LLM is
unavailable, alerts still go out as plain messages. Smaller local models may be weaker at Bizum matching and
habit notes, so check with `fm run --dry-run` first.

## More
- **Older history:** export movements from your bank's website as CSV and run `fm import-csv file.csv`. Only
  rows older than the API history are imported, so nothing is double counted.
- **Mobile wallets** (Google Pay, Apple Pay) need no setup: those payments are card charges and show up in
  the bank feed, at first possibly as *pending* (handled without duplicate alerts).
- **Categories:** Spanish merchant rules live in `finmon/categorize.py`. The LLM categorises unknown
  merchants once and the result is cached. Override with `/cat #ref <category>`.
- **Configuration:** all options are documented in `.env.example`.

## Privacy & security
- Transactions are stored locally in SQLite (`~/.local/share/finmon`) and sent to the configured LLM for
  analysis. A local model keeps them on your machine, apart from Telegram messages.
- Bank access is read-only account information; it can't move money.
- Merchant and Bizum texts are treated as data, never as instructions. The `claude-code` provider runs with
  all tools disabled in an empty directory.
- Keep `.env` and the `.pem` private (`chmod 600`). Neither is tracked by git.

## Development
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest && .venv/bin/ruff check .
```

## License

[GPL-3.0-or-later](LICENSE). Not affiliated with any bank, Enable Banking, Bizum, Telegram or LLM vendor.
Use at your own risk.
