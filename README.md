# SEC Insider Trading Alert Bot v1.2

Telegram alerts for open-market insider purchases (SEC Form 4, code `P`). Runs on GitHub Actions cron — no server, no hosting cost.

> **Not financial advice.** The score is heuristic and **has never been backtested**. See [Limitations](#limitations-you-should-know-about).

---

## What changed from v1.0

| # | v1.0 bug | Fix |
|---|---|---|
| 1 | Endpoint `/form-4` and a nested `query_string` object — the API accepts neither | `/insider-trading` with a plain Lucene query string |
| 2 | Wrong field paths: `raw["ticker"]`, `transactionCoding.transactionCode`, `transactionAmounts.transactionShares` | `issuer.tradingSymbol`, `coding.code`, `amounts.shares` |
| 3 | `datetime.utcnow()` compared against `filedAt` in New York time → the query returned almost nothing | Local filtering with timezone-aware datetimes plus pagination to the cutoff |
| 4 | `_esc()` written for MarkdownV2 and never called, with `parse_mode: HTML` → "Procter & Gamble" produced a 400 | `html.escape()` on every dynamic field |
| 5 | `sec_url` used the ticker as the CIK → every link 404'd | EDGAR URL built from `issuer.cik` + accession number |
| 6 | Only the first `P` row per filing was read | Aggregates all `P`/`A` rows with a share-weighted average price |
| 7 | 10b5-1 detected via `str(raw).lower()` | Official `aff10b5One` field (with a footnote fallback) |
| 8 | Cluster detection compared `YYYY-MM-DD` against an ISO timestamp, counted filings instead of people, and read only from sent alerts | Dates against dates, distinct insiders, reads **every** recorded transaction |
| 9 | `RateLimitError` used before it was defined | Defined at the top |
| 10 | `while True` on Colab — dies when the runtime disconnects | One cycle per invocation, with an optional `--loop` |
| 11 | Credentials hardcoded in the file | Everything via environment variables |

Added since: adaptive lookback, cycle status messages, forum-topic routing, exponential backoff, Telegram flood-limit handling, a score breakdown on every alert (`+3 CEO/CFO, +3 value >= $500k`), `acquiredDisposedCode` filtering, `--dry-run`, `--test-telegram`, and 106 fixture tests.

---

## Setup

### 1. Repository

```
your-repo/
├── insider_bot.py
├── test_bot.py
├── requirements.txt
├── README.md
└── .github/workflows/insider-bot.yml
```

Private repos work fine (2,000 Actions minutes/month on the Free plan; see [budget](#cadence-and-minute-budget)).

### 2. Telegram

1. `@BotFather` → `/newbot` → save the token
2. Send any message to your bot
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` → copy `message.chat.id`

### 3. Secrets

**Settings → Secrets and variables → Actions → New repository secret:**

| Secret | Value |
|---|---|
| `SEC_API_KEY` | key from [sec-api.io](https://sec-api.io) |
| `TELEGRAM_TOKEN` | token from @BotFather |
| `TELEGRAM_CHAT_ID` | your chat id (supergroups start with `-100`) |
| `TELEGRAM_TOPIC_ID` | **optional** — forum supergroups only |
| `STATUS_TOPIC_ID` | **optional** — separate topic for status messages |

### Posting to a specific topic (forum supergroup)

Without `TELEGRAM_TOPIC_ID`, messages land in the *General* topic. To route them:

1. Add the bot to the group (member is enough; admin also works)
2. Post any message **inside** the topic you want to use
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. On that message, copy `message_thread_id` → that is your `TELEGRAM_TOPIC_ID`
5. The `chat.id` on the same object is your `TELEGRAM_CHAT_ID` (negative, starts with `-100`)

### 4. First run

**Actions → Insider Alert Bot → Run workflow.**

Three ways to run it, in the order worth trying:

| Input | What it does |
|---|---|
| `test_telegram: true` | sends one test message and logs **which chat and topic it landed in**. Start here |
| `dry_run: true` | full cycle against live SEC data, alerts printed to the log only |
| defaults | live |

---

## How persistence works

GitHub Actions has no persistent disk. The SQLite file is stored on an orphan branch called `bot-state` (just the `.db`, force-pushed each run) and restored at the start of the next run.

That is what makes dedup and cluster detection work across runs. Delete the `bot-state` branch and the bot cold-starts, possibly repeating recent alerts.

`concurrency: insider-bot` stops two simultaneous runs from clobbering each other's writes.

---

## Cadence and minute budget

| Window (ET) | Frequency | Runs/day |
|---|---|---|
| 09:00–16:00, Mon-Fri | 30 min | 14 |
| 16:00–19:00, Mon-Fri | 15 min | 12 |
| 22:00 ET (evening) | 1x | 1 |
| Saturday 01:00 ET | 1x (Friday catch-up) | 1 |

**~28 runs/weekday, ~590/month.** At 1–2 billed minutes each, that is 600–1200 of the 2,000 min/month included on the Free plan for private repos. Room left for another bot or two.

The frequency is deliberately low. Form 4 has a two-business-day filing deadline and becomes public to everyone simultaneously — arriving 20 minutes later changes nothing for a signal measured in months. Running every 10 minutes burned the entire quota without improving the signal.

### Adaptive lookback

The bot stores the last **successful** run in the `meta` table and derives the window from it, plus a 25-minute margin:

- normal run (30 min gap) → ~55 min window
- previous run failed → the next window covers both
- weekend (60h) → 60h window, truncated at `MAX_LOOKBACK_MINUTES` (3 days)
- first ever run → `LOOKBACK_MINUTES` (90 min)

The number of pages requested from the SEC-API scales with the window (`pages_for`), capped at `MAX_PAGES_CATCHUP` so a large catch-up cannot blow through your API quota.

This eliminates the "I lost filings because the cron did not fire" class of bug. The timestamp is written **after** a successful cycle — if the API fails, the next window covers the same period again.

## Status messages

`STATUS_MESSAGES` controls the cycle start/finish notices:

| Value | Behaviour |
|---|---|
| `always` | a message at the start and end of every cycle |
| `summary` | finish only, and only when there were alerts or an error |
| `errors` | only when something fails |
| `off` | never (code default) |

They are always **silent** (no phone notification) — with `always` there are ~56/day. To keep them out of the alert feed, create a dedicated topic and set `STATUS_TOPIC_ID`.

---

## Configuration

Everything is optional except the three credentials.

| Variable | Default | What it does |
|---|---|---|
| `MIN_TRANSACTION_VALUE_USD` | `25000` | ignore purchases below this value |
| `MIN_PRICE` | `1.0` | ignore stocks below this share price |
| `EXCLUDE_10B5` | `false` | drop 10b5-1 plan purchases entirely |
| `REQUIRE_DIRECT` | `false` | personal holdings only |
| `MIN_POSITION_INCREASE` | `0` | minimum stake increase, in percent |
| `MIN_MARKET_CAP` / `MAX_MARKET_CAP` | `0` | market-cap bounds in USD (0 = off) |
| `ONLY_TICKERS` / `EXCLUDE_TICKERS` | — | comma-separated ticker lists |
| `ENABLE_MARKET_DATA` | `true` | yfinance lookups for cap and 52-week range |
| `MARKET_CACHE_HOURS` | `168` | how long to trust a cached market cap |
| `CLUSTER_WINDOW_DAYS` | `7` | window for cluster-buy detection |
| `SCORE_MIN_TO_SEND` | `9` | below this, log only (no message) |
| `SCORE_SILENT_BELOW` | `10` | send without a notification |
| `SCORE_MAX_ALERT_FROM` | `13` | 🚨 MAX ALERT threshold |
| `SCORE_PENALTY_10B5` | `0` | points to subtract from 10b5-1 plan purchases |
| `LOOKBACK_MINUTES` | `90` | cold-start window |
| `LOOKBACK_BUFFER_MINUTES` | `25` | margin for cron delays |
| `MAX_LOOKBACK_MINUTES` | `4320` | catch-up ceiling (3 days) |
| `MAX_PAGES` | `6` | pages of 50 filings per run |
| `MAX_PAGES_CATCHUP` | `30` | page ceiling during catch-up |
| `STATUS_MESSAGES` | `off` | `always` / `summary` / `errors` / `off` |
| `TELEGRAM_TOPIC_ID` | — | destination topic in forum supergroups |
| `STATUS_TOPIC_ID` | — | separate topic for status messages |
| `FINVIZ_INSIDER_URL` | `finviz.com/insidertrading?tc=7` | target of the "latest buys" button |
| `VERBOSE` | `false` | DEBUG logging |

## Filters

Two stages. Cheap filters run first; the market-data lookup only happens for filings that survive them.

### Stage 1 — no network

| Filter | Default | Drops |
|---|---|---|
| `MIN_TRANSACTION_VALUE_USD` | `25000` | small purchases |
| `MIN_PRICE` | `1.0` | penny stocks — the single largest source of Form 4 noise |
| `EXCLUDE_10B5` | `false` | scheduled plan purchases, entirely |
| `REQUIRE_DIRECT` | `false` | holdings via trust, LLC or spouse |
| `MIN_POSITION_INCREASE` | `0` | token top-ups (first purchases are never blocked) |
| `ONLY_TICKERS` | — | anything off your watchlist |
| `EXCLUDE_TICKERS` | — | tickers you never want to see |

### Stage 2 — needs market data

| Filter | Default | Drops |
|---|---|---|
| `MIN_MARKET_CAP` | `0` (off) | shells and nano caps |
| `MAX_MARKET_CAP` | `0` (off) | mega caps, where insider buys are noise |

When a market-data lookup fails, these are **skipped** rather than applied. A network hiccup must never silently drop a good filing.

## Scoring

Maximum around 22. Alerts fire at `SCORE_MIN_TO_SEND` (default **9**).

| Criterion | Points |
|---|---|
| CEO / CFO | +4 |
| Other C-suite / President | +3 |
| Officer / VP | +2 |
| Director (when not already CEO/CFO) | +2 |
| 10% owner | +2 |
| Dual role (executive **and** board member) | +1 |
| Value ≥ $1M / $500k / $250k / $100k | +4 / +3 / +2 / +1 |
| Position increase ≥ 100% / 50% / 20% | +3 / +2 / +1 |
| Cluster: 3+ / 2 / 1 other insiders within 7d | +4 / +3 / +2 |
| Purchase ≥ 1% / 0.25% / 0.05% of market cap | +3 / +2 / +1 |
| Price within 10% / 25% of the 52-week low | +2 / +1 |
| Direct ownership (personal money) | +1 |
| 10b5-1 plan purchase | `-SCORE_PENALTY_10B5` (default 0) |

Every alert carries its breakdown so you can audit why it fired.

**The percent-of-market-cap row is the one that matters most.** It is what stops a $100k purchase at Apple from ranking alongside a $100k purchase at a $50M company. Everything else is a variation on "who" and "how much".

**A wider scale is not a validated scale.** Adding components makes the number look more considered while leaving it exactly as untested as the six-point version was. Phase 3 is what would change that.

### Buttons on each alert

| Button | Purpose |
|---|---|
| 📈 TradingView | chart for the ticker |
| 📄 SEC filing | primary source, the Form 4 itself |
| 📰 Investing.com | news and company context |
| 🔍 Finviz | fundamentals + insider history for the ticker |
| 👀 Latest insider buys | market-wide feed of recent buys |

---

## Testing locally

```bash
pip install -r requirements.txt
python test_bot.py                       # 106 tests, no network

export SEC_API_KEY="..."
python insider_bot.py --dry-run          # live data, prints instead of sending

export TELEGRAM_TOKEN="..." TELEGRAM_CHAT_ID="..."
python insider_bot.py --test-telegram    # one message, reports where it landed
```

Inspecting the database:

```bash
sqlite3 state/alerts.db "
  SELECT ticker, insider_name, title, total_value, score, is_10b5, alerted
  FROM transactions ORDER BY recorded_at DESC LIMIT 20;"

sqlite3 state/alerts.db "SELECT * FROM meta;"   -- last successful run
```

For a VPS instead of Actions: `python insider_bot.py --loop`.

---

## Limitations you should know about

These are not bugs — they are properties of the signal. They matter more than the code.

**The signal is slow, not fast.** Form 4 is filed up to two business days after the trade and becomes public to everyone at once. There is no speed advantage to capture. The academic literature (Lakonishok & Lee; Cohen, Malloy & Pomorski on "opportunistic insiders") points to **modest abnormal returns over 6 to 12 months**. Ten-minute alerts help you not miss the filing, not scalp it.

**No market-cap normalisation.** A $100k purchase in a nano-cap is enormous; in Apple it is noise. The current score treats them identically — the biggest weakness that remains. Phase 2.

**There is no sell signal.** Insiders sell for taxes, diversification, divorce, scheduled plans. Purchases inform; sales inform much less. Any exit rule has to come from you (fixed horizon, trailing stop, target), not from insider data.

**The score has not been backtested.** The weights are plausible heuristics, not measurements. They may be scoring noise. Until Phase 3 runs, treat the score as "this deserves a look", not "this will go up".

**Legal note:** trading on public Form 4 filings is legal — it is not insider trading. Insider trading means trading on material **non-public** information.

---

## Roadmap

- **Phase 2 — enrich:** market cap (yfinance), price vs 50/200-day MA, capture sales (`S`), group same-day filings by ticker
- **Phase 3 — backtest:** sec-api bulk archives (`/bulk/form-4/YYYY/YYYY-MM.jsonl.gz`) + historical prices; returns at 1/5/21/63/126 days per score bucket. This answers whether the score is worth anything
- **Phase 4 — signals:** only if Phase 3 shows an edge. Explicit entry and exit, position sizing, and paper trading before real money

## Sources

- [Insider Trading Data from SEC Form 3, 4, 5 Filings — sec-api.io](https://sec-api.io/docs/insider-ownership-trading-api)
- [Analyze SEC Form 4 Insider Trades with Python — sec-api.io](https://sec-api.io/docs/insider-ownership-trading-api/python-example)
- [Form 4 Bulk Dataset — sec-api.io](https://sec-api.io/datasets/form-4)
- [GitHub Actions billing — GitHub Docs](https://docs.github.com/billing/managing-billing-for-github-actions/about-billing-for-github-actions)
