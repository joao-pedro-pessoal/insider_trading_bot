#!/usr/bin/env python3
"""
SEC INSIDER TRADING ALERT BOT v1.2
==================================

Telegram alerts for open-market insider purchases (SEC Form 4, code P).

Designed to run one cycle per invocation on a cron (GitHub Actions), with an
optional --loop mode for a long-running VPS process.

Key design decisions worth knowing before you change anything:

  * Time filtering happens locally, not in the API query. The API returns
    filedAt in New York time with an explicit offset; comparing that against
    a UTC-derived cutoff string was the single worst bug in v1.0. We paginate
    by filedAt descending and stop at the cutoff, comparing timezone-aware
    datetimes in Python -- the only place that comparison is reliable.

  * The lookback window is derived from the last successful run, stored in the
    database. A fixed window loses filings whenever a run fails, is delayed,
    or when there is a long gap between crons (Friday night to Monday).

  * Every parsed filing is recorded, not just the ones that trigger an alert.
    Cluster detection reads from that table, so filtering before writing would
    blind it.

  * Deduplication is by accession number, which makes generous overlap between
    runs harmless. Overlap is deliberate.

NOT financial advice. The scoring weights are heuristics and have never been
backtested. See README.md.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import requests

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

NY_TZ = ZoneInfo("America/New_York")


# ══════════════════════════════════════════════════════════════════
#  CONFIGURATION  (all via environment variables)
# ══════════════════════════════════════════════════════════════════

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "y"}


TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEC_API_KEY      = os.environ.get("SEC_API_KEY", "")

# Optional: topic ID inside a forum-enabled supergroup. Without it, messages
# land in the "General" topic instead of the one you picked. Find it in the
# message_thread_id of any message posted in that topic.
TELEGRAM_TOPIC_ID = os.environ.get("TELEGRAM_TOPIC_ID", "").strip()

# Cycle start/finish status messages:
#   "always"  - every cycle, even quiet ones
#   "summary" - finish only, and only when there were alerts or an error
#   "errors"  - only when something fails
#   "off"     - never
STATUS_MESSAGES = os.environ.get("STATUS_MESSAGES", "off").strip().lower()

# Separate topic for status messages. Empty = same topic as the alerts.
STATUS_TOPIC_ID = os.environ.get("STATUS_TOPIC_ID", "").strip()

SEC_API_ENDPOINT = os.environ.get("SEC_API_ENDPOINT", "https://api.sec-api.io/insider-trading")

DB_PATH = os.environ.get("DB_PATH", "state/alerts.db")

MIN_TRANSACTION_VALUE_USD = _env_int("MIN_TRANSACTION_VALUE_USD", 25_000)
CLUSTER_WINDOW_DAYS       = _env_int("CLUSTER_WINDOW_DAYS", 7)
SCORE_MIN_TO_SEND         = _env_int("SCORE_MIN_TO_SEND", 9)   # below this, log only
SCORE_SILENT_BELOW        = _env_int("SCORE_SILENT_BELOW", 10) # send without notification
SCORE_MAX_ALERT_FROM      = _env_int("SCORE_MAX_ALERT_FROM", 13)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_list(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {t.strip().upper() for t in raw.split(",") if t.strip()}


# ── Hard filters: applied before scoring, they drop a filing entirely ──
# Penny stocks are the single largest source of Form 4 noise.
MIN_PRICE            = _env_float("MIN_PRICE", 1.0)
# Skip 10b5-1 plan purchases outright (scheduled months ahead, low information).
EXCLUDE_10B5         = _env_bool("EXCLUDE_10B5", False)
# Require direct ownership (personal money, not via trust/LLC/partnership).
REQUIRE_DIRECT       = _env_bool("REQUIRE_DIRECT", False)
# Minimum position increase, in percent. 0 = off.
MIN_POSITION_INCREASE = _env_float("MIN_POSITION_INCREASE", 0.0)
# Market-cap bounds in USD. 0 = off. Needs ENABLE_MARKET_DATA.
MIN_MARKET_CAP       = _env_float("MIN_MARKET_CAP", 0)
MAX_MARKET_CAP       = _env_float("MAX_MARKET_CAP", 0)
# Ticker allow/deny lists, comma separated. ONLY_TICKERS acts as a watchlist.
ONLY_TICKERS         = _env_list("ONLY_TICKERS")
EXCLUDE_TICKERS      = _env_list("EXCLUDE_TICKERS")

# Market data (yfinance) powers market-cap normalisation and 52-week context.
# Degrades gracefully: if the lookup fails, those points simply are not awarded.
ENABLE_MARKET_DATA   = _env_bool("ENABLE_MARKET_DATA", True)
MARKET_CACHE_HOURS   = _env_int("MARKET_CACHE_HOURS", 168)  # 7 days

# Penalty for purchases made under a 10b5-1 plan: they were scheduled months in
# advance, so they say far less about the insider's current view. Default 0 (no
# penalty) so behaviour does not change without you choosing it -- set to 2 or 3
# to push automatic plans down the ranking.
SCORE_PENALTY_10B5        = _env_int("SCORE_PENALTY_10B5", 0)

# Cold-start lookback, used only when there is no previous run in the database.
LOOKBACK_MINUTES        = _env_int("LOOKBACK_MINUTES", 90)
# Margin added to the gap since the last run, to absorb GitHub cron delays
# (typically 5-20 minutes).
LOOKBACK_BUFFER_MINUTES = _env_int("LOOKBACK_BUFFER_MINUTES", 25)
# Catch-up ceiling. Three days covers a full weekend with room to spare; beyond
# that the bot has been down for days and flooding the channel helps nobody.
MAX_LOOKBACK_MINUTES    = _env_int("MAX_LOOKBACK_MINUTES", 4320)

PAGE_SIZE         = 50        # SEC-API maximum
MAX_PAGES         = _env_int("MAX_PAGES", 6)
# Page ceiling in catch-up mode. Each page is one SEC-API request, so this is
# also the brake on your API plan consumption.
MAX_PAGES_CATCHUP = _env_int("MAX_PAGES_CATCHUP", 30)
HTTP_TIMEOUT      = 25
MAX_RETRIES       = 3

logging.basicConfig(
    level=logging.DEBUG if _env_bool("VERBOSE") else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("InsiderBot")


class RateLimitError(Exception):
    """SEC-API returned 429."""


# ══════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_alerts (
    accession_number TEXT PRIMARY KEY,
    ticker           TEXT NOT NULL,
    insider_name     TEXT,
    score            INTEGER,
    sent_at          TEXT NOT NULL
);

-- Log of EVERYTHING parsed, including filings that never triggered an alert.
-- Cluster detection reads from here: filtering before writing would blind it.
CREATE TABLE IF NOT EXISTS transactions (
    accession_number TEXT PRIMARY KEY,
    ticker           TEXT NOT NULL,
    insider_cik      TEXT,
    insider_name     TEXT,
    title            TEXT,
    trade_date       TEXT,
    filing_date      TEXT,
    price            REAL,
    quantity         INTEGER,
    total_value      REAL,
    is_10b5          INTEGER,
    score            INTEGER,
    alerted          INTEGER DEFAULT 0,
    recorded_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_txn_ticker_date
    ON transactions (ticker, trade_date);

-- The bot's own state. Holds the last successful run so the lookback window
-- can be computed instead of hardcoded.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Market data cache. Market caps move slowly, and every lookup is a network
-- round trip that can fail, so cache aggressively.
CREATE TABLE IF NOT EXISTS market_cache (
    ticker      TEXT PRIMARY KEY,
    market_cap  REAL,
    price       REAL,
    low_52w     REAL,
    high_52w    REAL,
    fetched_at  TEXT NOT NULL
);
"""

LAST_RUN_KEY = "last_successful_run_utc"


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    log.info("Database ready: %s", path)
    return conn


def already_seen(conn: sqlite3.Connection, accession: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sent_alerts WHERE accession_number = ?", (accession,)
    )
    return cur.fetchone() is not None


def record_transaction(conn: sqlite3.Connection, txn: dict, score: int, alerted: bool) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO transactions
            (accession_number, ticker, insider_cik, insider_name, title,
             trade_date, filing_date, price, quantity, total_value,
             is_10b5, score, alerted, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            txn["accession_number"], txn["ticker"], txn.get("insider_cik"),
            txn.get("insider_name"), txn.get("title"), txn.get("trade_date"),
            txn.get("filing_date"), txn["price"], txn["quantity"],
            txn["total_value"], int(bool(txn.get("is_10b5"))), score,
            int(alerted), _utc_now_iso(),
        ),
    )
    conn.commit()


def record_alert(conn: sqlite3.Connection, txn: dict, score: int) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO sent_alerts
            (accession_number, ticker, insider_name, score, sent_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (txn["accession_number"], txn["ticker"],
         txn.get("insider_name"), score, _utc_now_iso()),
    )
    conn.commit()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    cur = conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def compute_lookback(conn: sqlite3.Connection,
                     override: Optional[int] = None) -> tuple[int, str]:
    """
    Adaptive lookback: cover everything since the last successful run, plus a
    margin.

    A fixed window loses filings whenever a run fails, is delayed, or when the
    gap between crons is large (Friday night through Monday). Deriving it from
    the last successful run closes that whole class of gaps.
    """
    if override is not None:
        return override, "forced via --lookback"

    last = get_meta(conn, LAST_RUN_KEY)
    if not last:
        return LOOKBACK_MINUTES, "first run (no previous state)"

    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return LOOKBACK_MINUTES, f"unreadable state ({last!r})"

    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)

    gap = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
    if gap < 0:
        return LOOKBACK_MINUTES, "clock inconsistency (last run in the future)"

    minutes = int(gap) + LOOKBACK_BUFFER_MINUTES
    if minutes > MAX_LOOKBACK_MINUTES:
        return (MAX_LOOKBACK_MINUTES,
                f"{gap / 60:.1f}h gap truncated to ceiling")
    return max(minutes, 15), f"since last run ({gap:.0f}min ago)"


def pages_for(lookback_minutes: int) -> int:
    """Pages to fetch. A wider window needs more pages, otherwise catch-up
    truncates silently and we are back to losing filings."""
    needed = max(MAX_PAGES, int(lookback_minutes / 30))
    return min(needed, MAX_PAGES_CATCHUP)


# ══════════════════════════════════════════════════════════════════
#  INGESTION
# ══════════════════════════════════════════════════════════════════

# Lucene: Form 4 filings containing at least one open-market purchase (code P).
# Time filtering is done locally -- see the module docstring.
BASE_QUERY = 'documentType:"4" AND nonDerivativeTable.transactions.coding.code:P'


def _post_with_retry(payload: dict) -> dict:
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                SEC_API_ENDPOINT,
                json=payload,
                headers={
                    "Authorization": SEC_API_KEY,
                    "Content-Type": "application/json",
                },
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code == 429:
                raise RateLimitError("SEC-API rate limit (429)")
            if resp.status_code in (401, 403):
                raise RuntimeError(f"SEC-API auth failed ({resp.status_code}) - check SEC_API_KEY")
            resp.raise_for_status()
            return resp.json()
        except (RateLimitError, RuntimeError):
            raise
        except Exception as exc:  # network, timeout, 5xx
            last_error = exc
            wait = 2 ** attempt
            log.warning("SEC-API attempt %d/%d failed (%s) - retrying in %ds",
                        attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"SEC-API unreachable after {MAX_RETRIES} attempts: {last_error}")


def fetch_recent_purchases(cutoff: datetime, max_pages: int = MAX_PAGES) -> list[dict]:
    """
    Return Form 4 filings containing purchases with filedAt >= cutoff.

    Paginates by filedAt descending and stops as soon as it hits a filing older
    than the cutoff. No timezone arithmetic in the query -- the comparison uses
    timezone-aware datetimes in Python, which is the only place it is reliable.
    """
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")

    collected: list[dict] = []
    for page in range(max_pages):
        payload = {
            "query": BASE_QUERY,
            "from": str(page * PAGE_SIZE),
            "size": str(PAGE_SIZE),
            "sort": [{"filedAt": {"order": "desc"}}],
        }
        data = _post_with_retry(payload)
        batch = data.get("transactions", [])
        if not batch:
            break

        stop = False
        for filing in batch:
            filed_at = _parse_dt(filing.get("filedAt"))
            if filed_at is None:
                collected.append(filing)  # no date: let it through, dedup protects us
                continue
            if filed_at < cutoff:
                stop = True
                break
            collected.append(filing)

        log.debug("page %d: %d filings, %d collected", page, len(batch), len(collected))
        if stop or len(batch) < PAGE_SIZE:
            break
    else:
        log.warning("Hit the page ceiling (%d) - filings may be missing. "
                    "Raise MAX_PAGES_CATCHUP or shorten the window.", max_pages)

    return collected


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse SEC-API ISO 8601 ('2022-08-09T21:23:00-04:00') into an aware datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY_TZ)  # the SEC reports in New York time
    return dt


# ══════════════════════════════════════════════════════════════════
#  PARSING
# ══════════════════════════════════════════════════════════════════

def parse_filing(raw: dict) -> Optional[dict]:
    """
    Normalise a SEC-API filing into a single aggregated transaction.

    Aggregates EVERY row coded P with acquiredDisposedCode A from the same
    filing: an insider buying in three tranches was being undervalued when only
    the first row was read.
    """
    try:
        return _parse(raw)
    except Exception as exc:
        log.debug("parse failed (%s): %s", exc, str(raw)[:160])
        return None


def _parse(raw: dict) -> Optional[dict]:
    accession = raw.get("accessionNo") or raw.get("id")
    if not accession:
        return None

    issuer  = raw.get("issuer") or {}
    ticker  = (issuer.get("tradingSymbol") or "").strip().upper()
    company = issuer.get("name") or ticker
    cik     = str(issuer.get("cik") or "").strip()

    owner        = raw.get("reportingOwner") or {}
    insider_name = owner.get("name") or "Unknown"
    insider_cik  = str(owner.get("cik") or "").strip()
    title        = _extract_title(owner.get("relationship") or {})

    rows = ((raw.get("nonDerivativeTable") or {}).get("transactions")) or []
    purchases = [
        r for r in rows
        if ((r.get("coding") or {}).get("code") == "P")
        and ((r.get("amounts") or {}).get("acquiredDisposedCode", "A") == "A")
    ]
    if not purchases:
        return None

    total_shares = 0.0
    total_cost   = 0.0
    post_qty     = 0
    trade_dates: list[str] = []
    is_direct    = False

    for row in purchases:
        amounts = row.get("amounts") or {}
        shares  = _safe_float(amounts.get("shares"))
        price   = _safe_float(amounts.get("pricePerShare"))
        if shares <= 0:
            continue
        total_shares += shares
        total_cost   += shares * price
        post = _safe_float((row.get("postTransactionAmounts") or {})
                           .get("sharesOwnedFollowingTransaction"))
        post_qty = max(post_qty, int(post))
        if row.get("transactionDate"):
            trade_dates.append(row["transactionDate"])
        # "D" = direct: the insider's own money, not a trust, LLC or spouse.
        if ((row.get("ownershipNature") or {})
                .get("directOrIndirectOwnership", "").upper() == "D"):
            is_direct = True

    if total_shares <= 0:
        return None

    avg_price = total_cost / total_shares

    # Position increase, computed here rather than during scoring because the
    # filters need it too. None means the filing did not report a usable
    # post-transaction total (or it was a first purchase).
    pre = post_qty - int(total_shares)
    pct_increase = round((total_shares / pre) * 100, 1) if pre > 0 else None

    # Official field from the SEC's 2023 amendments to Rule 10b5-1. Far more
    # reliable than string-matching "10b5-1" in the footnote text.
    is_10b5 = bool(raw.get("aff10b5One")) or _footnotes_mention_10b5(raw)

    return {
        "accession_number": accession,
        "ticker":           ticker,
        "company":          company,
        "issuer_cik":       cik,
        "insider_name":     insider_name,
        "insider_cik":      insider_cik,
        "title":            title,
        "trade_date":       min(trade_dates) if trade_dates else (raw.get("periodOfReport") or ""),
        "filing_date":      raw.get("filedAt") or "",
        "price":            round(avg_price, 4),
        "quantity":         int(total_shares),
        "post_qty":         post_qty,
        "pct_increase":     pct_increase,
        "total_value":      round(total_cost, 2),
        "n_transactions":   len(purchases),
        "is_10b5":          is_10b5,
        "is_direct":        is_direct,
        "sec_url":          _filing_url(cik, accession),
    }


def _footnotes_mention_10b5(raw: dict) -> bool:
    notes = raw.get("footnotes") or []
    if isinstance(notes, dict):
        notes = list(notes.values())
    text = " ".join(
        (n.get("text", "") if isinstance(n, dict) else str(n)) for n in notes
    ).lower()
    return "10b5-1" in text


def _filing_url(issuer_cik: str, accession: str) -> str:
    """Real EDGAR filing URL. v1.0 used the ticker as the CIK, so every link 404'd."""
    if not issuer_cik or not accession:
        return "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4"
    plain = accession.replace("-", "")
    return (f"https://www.sec.gov/Archives/edgar/data/{issuer_cik}/"
            f"{plain}/{accession}-index.htm")


def _extract_title(rel: dict | str) -> str:
    if isinstance(rel, str):
        return rel or "Insider"
    parts = []
    if rel.get("isOfficer"):
        parts.append(rel.get("officerTitle") or "Officer")
    if rel.get("isDirector"):
        parts.append("Director")
    if rel.get("isTenPercentOwner"):
        parts.append("10% Owner")
    if rel.get("isOther") and rel.get("otherText"):
        parts.append(str(rel["otherText"]))
    return ", ".join(parts) if parts else "Insider"


def _safe_float(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


# ══════════════════════════════════════════════════════════════════
#  MARKET DATA
# ══════════════════════════════════════════════════════════════════

def get_market_data(conn: sqlite3.Connection, ticker: str) -> Optional[dict]:
    """
    Market cap and 52-week range for a ticker, cached in SQLite.

    This is what makes the score aware of scale: a $100k purchase in a
    nano-cap is enormous, in Apple it is a rounding error. Without it the
    scoring is blind to the single most important piece of context.

    Returns None when unavailable. Every caller must handle that -- yfinance
    is a scraper and will fail sometimes, and a missing market cap must never
    take the bot down.
    """
    if not ENABLE_MARKET_DATA or not ticker:
        return None

    cur = conn.execute(
        "SELECT market_cap, price, low_52w, high_52w, fetched_at "
        "FROM market_cache WHERE ticker = ?", (ticker,)
    )
    row = cur.fetchone()
    if row:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(row[4])).total_seconds() / 3600
            if age_h < MARKET_CACHE_HOURS:
                return {"market_cap": row[0], "price": row[1],
                        "low_52w": row[2], "high_52w": row[3], "cached": True}
        except ValueError:
            pass  # unreadable timestamp, re-fetch

    data = _fetch_market_data(ticker)
    if data is None:
        return None

    conn.execute(
        """INSERT OR REPLACE INTO market_cache
           (ticker, market_cap, price, low_52w, high_52w, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ticker, data["market_cap"], data["price"],
         data["low_52w"], data["high_52w"], _utc_now_iso()),
    )
    conn.commit()
    return data


def _fetch_market_data(ticker: str) -> Optional[dict]:
    try:
        import yfinance as yf
    except ImportError:
        log.debug("yfinance not installed - market data disabled")
        return None

    try:
        info = yf.Ticker(ticker).fast_info
        data = {
            "market_cap": _safe_float(info.get("market_cap")),
            "price":      _safe_float(info.get("last_price")),
            "low_52w":    _safe_float(info.get("year_low")),
            "high_52w":   _safe_float(info.get("year_high")),
            "cached":     False,
        }
    except Exception as exc:
        log.debug("market data lookup failed for %s: %s", ticker, exc)
        return None

    if data["market_cap"] <= 0 and data["price"] <= 0:
        return None
    return data


# ══════════════════════════════════════════════════════════════════
#  FILTERS
# ══════════════════════════════════════════════════════════════════

def passes_filters(txn: dict) -> tuple[bool, str]:
    """Cheap filters: no network, applied before any market-data lookup."""
    ticker = txn.get("ticker")
    if not ticker:
        return False, "no ticker (likely not publicly traded)"
    if txn["price"] <= 0:
        return False, "zero price (a grant dressed up as a purchase)"
    if txn["quantity"] <= 0:
        return False, "zero quantity"
    if txn["total_value"] < MIN_TRANSACTION_VALUE_USD:
        return False, f"value ${txn['total_value']:,.0f} below minimum"

    if MIN_PRICE > 0 and txn["price"] < MIN_PRICE:
        return False, f"price ${txn['price']:.2f} below ${MIN_PRICE:.2f} (penny stock)"
    if EXCLUDE_10B5 and txn.get("is_10b5"):
        return False, "10b5-1 plan purchase (EXCLUDE_10B5)"
    if REQUIRE_DIRECT and not txn.get("is_direct", True):
        return False, "indirect ownership (REQUIRE_DIRECT)"
    if ONLY_TICKERS and ticker not in ONLY_TICKERS:
        return False, f"{ticker} not on the watchlist"
    if EXCLUDE_TICKERS and ticker in EXCLUDE_TICKERS:
        return False, f"{ticker} on the exclusion list"

    if MIN_POSITION_INCREASE > 0:
        pct = txn.get("pct_increase")
        if pct is not None and pct < MIN_POSITION_INCREASE:
            return False, f"position +{pct:.1f}% below {MIN_POSITION_INCREASE:.0f}%"

    return True, "ok"


def passes_market_filters(txn: dict) -> tuple[bool, str]:
    """Filters that need market data. Skipped entirely when it is unavailable —
    a failed lookup must not silently drop a good filing."""
    market = txn.get("market")
    if not market:
        return True, "ok (no market data)"

    cap = market.get("market_cap") or 0
    if cap <= 0:
        return True, "ok (no market cap)"

    if MIN_MARKET_CAP > 0 and cap < MIN_MARKET_CAP:
        return False, f"market cap ${cap / 1e6:,.0f}M below minimum"
    if MAX_MARKET_CAP > 0 and cap > MAX_MARKET_CAP:
        return False, f"market cap ${cap / 1e9:,.1f}B above maximum"
    return True, "ok"


# ══════════════════════════════════════════════════════════════════
#  SCORING
# ══════════════════════════════════════════════════════════════════

def calculate_score(txn: dict, conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """
    Weighted score, maximum ~22. Returns (score, breakdown). The breakdown
    exists so you can audit why an alert fired, and so Phase 3 can test each
    component in isolation.

    Honest note: these weights are heuristics, not backtest output. A wider
    scale with more components looks more sophisticated but is exactly as
    unvalidated as the narrow one was. Do not read the score as a probability.
    """
    score = 0
    why: list[str] = []

    # ── Role: who is buying ───────────────────────────────────────
    title_up = (txn.get("title") or "").upper()
    is_ceo_cfo = any(k in title_up for k in
                     ("CEO", "CHIEF EXECUTIVE", "CFO", "CHIEF FINANCIAL"))
    is_other_chief = "CHIEF" in title_up or "PRESIDENT" in title_up
    is_director = "DIRECTOR" in title_up
    is_officer = "OFFICER" in title_up or is_other_chief or "VP" in title_up
    is_ten_pct = "10%" in title_up

    if is_ceo_cfo:
        score += 4
        why.append("+4 CEO/CFO")
    elif is_other_chief:
        score += 3
        why.append("+3 C-suite/President")
    elif is_officer:
        score += 2
        why.append("+2 officer")

    if is_director and not is_ceo_cfo:
        score += 2
        why.append("+2 director")
    if is_ten_pct:
        score += 2
        why.append("+2 10% owner")
    # Someone who is both an executive and a board member sees more than either.
    if (is_ceo_cfo or is_officer) and is_director:
        score += 1
        why.append("+1 dual role")

    # ── Absolute size ─────────────────────────────────────────────
    value = txn["total_value"]
    if value >= 1_000_000:
        score += 4
        why.append("+4 value >= $1M")
    elif value >= 500_000:
        score += 3
        why.append("+3 value >= $500k")
    elif value >= 250_000:
        score += 2
        why.append("+2 value >= $250k")
    elif value >= 100_000:
        score += 1
        why.append("+1 value >= $100k")

    # ── Conviction: how much they increased their own stake ───────
    pct = txn.get("pct_increase")
    if pct is not None:
        if pct >= 100:
            score += 3
            why.append(f"+3 position +{pct:.0f}%")
        elif pct >= 50:
            score += 2
            why.append(f"+2 position +{pct:.0f}%")
        elif pct >= 20:
            score += 1
            why.append(f"+1 position +{pct:.0f}%")

    # ── Cluster: agreement between independent insiders ───────────
    cluster = count_cluster_insiders(conn, txn)
    txn["cluster"] = cluster
    if cluster >= 3:
        score += 4
        why.append(f"+4 cluster ({cluster} insiders)")
    elif cluster == 2:
        score += 3
        why.append("+3 cluster (2 insiders)")
    elif cluster == 1:
        score += 2
        why.append("+2 cluster (1 insider)")

    # ── Scale: the purchase relative to the whole company ─────────
    market = txn.get("market") or {}
    cap = market.get("market_cap") or 0
    if cap > 0:
        pct_cap = (value / cap) * 100
        txn["pct_of_market_cap"] = round(pct_cap, 4)
        if pct_cap >= 1.0:
            score += 3
            why.append(f"+3 {pct_cap:.2f}% of market cap")
        elif pct_cap >= 0.25:
            score += 2
            why.append(f"+2 {pct_cap:.2f}% of market cap")
        elif pct_cap >= 0.05:
            score += 1
            why.append(f"+1 {pct_cap:.2f}% of market cap")

    # ── Timing: buying near the lows, not chasing strength ────────
    low = market.get("low_52w") or 0
    price = market.get("price") or 0
    if low > 0 and price > 0:
        above_low = ((price - low) / low) * 100
        txn["pct_above_52w_low"] = round(above_low, 1)
        if above_low <= 10:
            score += 2
            why.append(f"+2 {above_low:.0f}% above 52w low")
        elif above_low <= 25:
            score += 1
            why.append(f"+1 {above_low:.0f}% above 52w low")

    # ── Personal money, not a trust or LLC ────────────────────────
    if txn.get("is_direct"):
        score += 1
        why.append("+1 direct ownership")

    if txn.get("is_10b5") and SCORE_PENALTY_10B5:
        score -= SCORE_PENALTY_10B5
        why.append(f"-{SCORE_PENALTY_10B5} 10b5-1 plan")

    return max(score, 0), why


def count_cluster_insiders(conn: sqlite3.Connection, txn: dict) -> int:
    """
    Number of DISTINCT insiders (excluding this one) who bought the same ticker
    inside the cluster window.

    Fixes versus v1.0: compares dates against dates rather than a date against
    an ISO timestamp, counts people instead of filings, and reads from every
    recorded transaction instead of only the ones that alerted.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CLUSTER_WINDOW_DAYS)).date().isoformat()
    cur = conn.execute(
        """
        SELECT COUNT(DISTINCT COALESCE(NULLIF(insider_cik, ''), insider_name))
        FROM transactions
        WHERE ticker = ?
          AND date(trade_date) >= date(?)
          AND accession_number != ?
          AND COALESCE(NULLIF(insider_cik, ''), insider_name) !=
              COALESCE(NULLIF(?, ''), ?)
        """,
        (txn["ticker"], cutoff, txn["accession_number"],
         txn.get("insider_cik") or "", txn.get("insider_name") or ""),
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] else 0


# ══════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════

def esc(value: Any) -> str:
    """Escape & < > for parse_mode=HTML. v1.0 had a MarkdownV2 escaper that was
    never called, so any 'Procter & Gamble' produced a 400 and a lost alert."""
    return html.escape(str(value), quote=False)


# Global feed of the latest insider buys on Finviz (tc=7 = latest buys).
FINVIZ_LATEST_BUYS = os.environ.get(
    "FINVIZ_INSIDER_URL", "https://finviz.com/insidertrading?tc=7"
)


def build_buttons(txn: dict) -> list[list[dict]]:
    """
    Inline keyboard, three rows:
      1. TradingView (chart)      | SEC filing (primary source)
      2. Investing.com (news)     | Finviz (fundamentals + insider history)
      3. Latest insider buys market-wide (global link)

    Tickers are URL-encoded: symbols with dots (BRK.B) or hyphens (BF-B) would
    otherwise break the query string.
    """
    ticker = txn["ticker"]
    t = quote(ticker, safe="")

    return [
        [
            {"text": f"\U0001F4C8 {ticker} TradingView",
             "url": f"https://www.tradingview.com/symbols/{t}/"},
            {"text": "\U0001F4C4 SEC filing", "url": txn["sec_url"]},
        ],
        [
            {"text": "\U0001F4F0 Investing.com",
             "url": f"https://www.investing.com/search/?q={t}"},
            {"text": "\U0001F50D Finviz",
             "url": f"https://finviz.com/quote.ashx?t={t}"},
        ],
        [
            {"text": "\U0001F440 Latest insider buys",
             "url": FINVIZ_LATEST_BUYS},
        ],
    ]


def build_message(txn: dict, score: int, why: list[str]) -> dict:
    ticker = txn["ticker"]

    if score >= SCORE_MAX_ALERT_FROM:
        emoji, label, silent = "\U0001F6A8", "MAX ALERT", False
    elif score >= SCORE_SILENT_BELOW:
        emoji, label, silent = "\U0001F534", "STRONG SIGNAL", False
    else:
        emoji, label, silent = "\U0001F7E1", "WEAK SIGNAL", True

    trade_type = ("⚠️ <b>Automatic plan (10b5-1)</b>"
                  if txn.get("is_10b5")
                  else "✅ <b>Discretionary purchase</b>")

    lines = [
        f"{emoji} <b>{label}</b>  |  Score: <b>{score}</b>",
        f"\U0001F3E2 <b>{esc(txn.get('company', ticker))}</b> (<code>{esc(ticker)}</code>)",
        "",
        f"\U0001F464 <b>{esc(txn.get('insider_name'))}</b>",
        f"\U0001F4BC {esc(txn.get('title'))}",
        "",
        f"\U0001F4B5 Total value: <code>${txn['total_value']:,.0f}</code>",
        f"\U0001F4C8 Avg price:   <code>${txn['price']:,.2f}</code>",
        f"\U0001F4E6 Shares:      <code>{txn['quantity']:,}</code>",
    ]

    if txn.get("pct_increase") is not None:
        lines.append(f"\U0001F4CA Position:    <code>+{txn['pct_increase']:.1f}%</code>")
    if txn.get("n_transactions", 1) > 1:
        lines.append(f"\U0001F9FE Aggregated from {txn['n_transactions']} transactions")

    market = txn.get("market") or {}
    cap = market.get("market_cap") or 0
    if cap > 0:
        cap_str = f"${cap / 1e9:.2f}B" if cap >= 1e9 else f"${cap / 1e6:.0f}M"
        line = f"\U0001F3F7 Market cap:  <code>{cap_str}</code>"
        if txn.get("pct_of_market_cap"):
            line += f"  (buy = {txn['pct_of_market_cap']:.2f}%)"
        lines.append(line)
    if txn.get("pct_above_52w_low") is not None:
        lines.append(f"\U0001F4C9 52w low:     <code>+{txn['pct_above_52w_low']:.0f}%</code> above")

    lines += ["", trade_type]

    if txn.get("is_direct"):
        lines.append("\U0001F464 Direct ownership (personal holding)")

    if txn.get("cluster", 0) > 0:
        lines.append(
            f"\U0001F501 <b>CLUSTER BUY</b> — {txn['cluster']} other insider(s) "
            f"bought in the last {CLUSTER_WINDOW_DAYS}d"
        )

    lines += [
        "",
        f"\U0001F4C5 Trade date: <code>{esc(txn.get('trade_date'))}</code>",
        f"\U0001F551 Filed:      <i>{esc(str(txn.get('filing_date'))[:16].replace('T', ' '))}</i>",
        f"\U0001F9EE Score:      <i>{esc(', '.join(why) if why else 'no criteria met')}</i>",
        "",
        "<i>Not financial advice. This signal has not been backtested.</i>",
    ]

    keyboard = {"inline_keyboard": build_buttons(txn)}

    return {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
        "reply_markup": keyboard,
    }


def with_topic(payload: dict) -> dict:
    """Route to a specific topic when TELEGRAM_TOPIC_ID is set. In forum
    supergroups, without this everything lands in General. A message_thread_id
    already present in the payload takes precedence."""
    if "message_thread_id" in payload or not TELEGRAM_TOPIC_ID:
        return payload
    try:
        return {**payload, "message_thread_id": int(TELEGRAM_TOPIC_ID)}
    except ValueError:
        log.warning("TELEGRAM_TOPIC_ID='%s' is not a number - ignored", TELEGRAM_TOPIC_ID)
        return payload


def send_telegram(payload: dict, dry_run: bool = False) -> bool:
    if dry_run:
        print("─" * 60)
        print(payload["text"])
        print("─" * 60)
        return True

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set")
        return False

    payload = with_topic(payload)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                retry_after = 5
                try:
                    retry_after = int(resp.json()["parameters"]["retry_after"])
                except Exception:
                    pass
                log.warning("Telegram flood limit - waiting %ds", retry_after)
                time.sleep(retry_after + 1)
                continue
            log.error("Telegram %s: %s", resp.status_code, resp.text[:300])
            body = resp.text.lower()
            if "thread not found" in body:
                log.error("  -> TELEGRAM_TOPIC_ID=%s does not exist in that group. "
                          "Check the topic's message_thread_id.", TELEGRAM_TOPIC_ID)
            elif "chat not found" in body:
                log.error("  -> TELEGRAM_CHAT_ID=%s is wrong. Supergroup IDs start "
                          "with -100.", TELEGRAM_CHAT_ID)
            elif "not enough rights" in body or "kicked" in body:
                log.error("  -> The bot lacks permission to post in that group/topic.")
            return False
        except Exception as exc:
            log.warning("Telegram attempt %d failed: %s", attempt, exc)
            time.sleep(2 ** attempt)
    return False


def send_plain(text: str, dry_run: bool = False) -> None:
    send_telegram({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, dry_run=dry_run)


def send_status(text: str, dry_run: bool = False) -> None:
    """Status message. Always silent (there are ~2 per run; with notifications
    this would be unbearable) and optionally in a separate topic."""
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": True,
    }
    if STATUS_TOPIC_ID:
        try:
            payload["message_thread_id"] = int(STATUS_TOPIC_ID)
        except ValueError:
            log.warning("STATUS_TOPIC_ID='%s' is not a number - ignored", STATUS_TOPIC_ID)
    send_telegram(payload, dry_run=dry_run)


def _fmt_duration(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 60 else f"{seconds / 60:.1f}min"


# ══════════════════════════════════════════════════════════════════
#  CYCLE
# ══════════════════════════════════════════════════════════════════

def process_cycle(conn: sqlite3.Connection, lookback_minutes: Optional[int] = None,
                  dry_run: bool = False) -> dict:
    started = time.monotonic()
    lookback, why_lookback = compute_lookback(conn, lookback_minutes)
    max_pages = pages_for(lookback)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback)

    log.info("Window: %dmin (%s) | %d pages max", lookback, why_lookback, max_pages)
    log.info("Fetching Form 4 purchases filed since %s UTC", cutoff.strftime("%Y-%m-%d %H:%M"))

    stats = {"fetched": 0, "alerts": 0, "skipped": 0, "filtered": 0,
             "lookback": lookback, "error": None}

    if STATUS_MESSAGES == "always":
        send_status(
            f"\U0001F504 <b>Cycle started</b>\n"
            f"Window: {lookback}min ({esc(why_lookback)})\n"
            f"Since: <code>{cutoff.strftime('%Y-%m-%d %H:%M')} UTC</code>",
            dry_run=dry_run,
        )

    try:
        raw_filings = fetch_recent_purchases(cutoff, max_pages=max_pages)
    except (RateLimitError, RuntimeError) as exc:
        log.error("Cycle aborted: %s", exc)
        stats["error"] = str(exc)
        if STATUS_MESSAGES in ("always", "summary", "errors"):
            send_status(f"❌ <b>Cycle failed</b>\n<code>{esc(exc)}</code>",
                        dry_run=dry_run)
        return stats

    log.info("%d filings received", len(raw_filings))
    stats["fetched"] = len(raw_filings)

    # Chronological ascending order: guarantees the first buyer in a cluster is
    # recorded before the second one is evaluated.
    raw_filings.sort(key=lambda f: f.get("filedAt") or "")

    for raw in raw_filings:
        txn = parse_filing(raw)
        if txn is None:
            continue

        if already_seen(conn, txn["accession_number"]):
            stats["skipped"] += 1
            continue

        # Cheap filters first: no point paying for a market-data lookup on a
        # filing that a free check already rejects.
        passed, reason = passes_filters(txn)
        if passed:
            txn["market"] = get_market_data(conn, txn["ticker"])
            passed, reason = passes_market_filters(txn)

        score, why = calculate_score(txn, conn)

        if not passed:
            # Record it anyway: this feeds cluster detection.
            record_transaction(conn, txn, score, alerted=False)
            stats["filtered"] += 1
            log.debug("filtered %s: %s", txn["ticker"], reason)
            continue

        log.info(
            f"{txn['ticker']:<6} | {(txn.get('title') or '?')[:24]:<24} | "
            f"${txn['total_value']:>12,.0f} | score {score:>2} | "
            f"10b5={txn['is_10b5']}"
        )

        if score < SCORE_MIN_TO_SEND:
            record_transaction(conn, txn, score, alerted=False)
            continue

        sent = send_telegram(build_message(txn, score, why), dry_run=dry_run)
        record_transaction(conn, txn, score, alerted=sent)
        if sent:
            record_alert(conn, txn, score)
            stats["alerts"] += 1
            time.sleep(0.4)  # headroom for the Telegram flood limit

    elapsed = time.monotonic() - started
    stats["duration"] = elapsed

    log.info("cycle: %d filings, %d alerts, %d duplicates, %d filtered in %s",
             stats["fetched"], stats["alerts"], stats["skipped"],
             stats["filtered"], _fmt_duration(elapsed))

    # Only now is the run marked successful. Had the fetch failed, the next
    # cycle would cover this window again.
    set_meta(conn, LAST_RUN_KEY, _utc_now_iso())

    should_report = (
        STATUS_MESSAGES == "always"
        or (STATUS_MESSAGES == "summary" and stats["alerts"] > 0)
    )
    if should_report:
        send_status(
            f"✅ <b>Cycle finished</b> in {_fmt_duration(elapsed)}\n"
            f"\U0001F4E5 {stats['fetched']} filings analysed\n"
            f"\U0001F514 {stats['alerts']} alert(s) sent\n"
            f"\U0001F501 {stats['skipped']} duplicate(s)\n"
            f"\U0001F6AB {stats['filtered']} filtered",
            dry_run=dry_run,
        )

    return stats


def sleep_seconds() -> tuple[int, str]:
    """Cadence by NYSE hours (only used in --loop mode)."""
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return 3600, "weekend"
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return 300, "market hours"
    if 16 * 60 <= minutes < 18 * 60 + 30:
        return 120, "post-close (filing peak)"
    return 1800, "off hours"


# ══════════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ══════════════════════════════════════════════════════════════════

def check_config(dry_run: bool) -> None:
    missing = []
    if not SEC_API_KEY:
        missing.append("SEC_API_KEY")
    if not dry_run:
        if not TELEGRAM_TOKEN:
            missing.append("TELEGRAM_TOKEN")
        if not TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
    if missing:
        log.error("Missing environment variables: %s", ", ".join(missing))
        sys.exit(2)


def test_telegram() -> int:
    """
    Send ONE test message and show Telegram's raw response, including the chat
    and topic the message actually landed in. Ignores --dry-run: the whole point
    is to really send.
    """
    log_destination()

    payload = with_topic({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": ("\U0001F9EA <b>Connection test</b>\n"
                 "If you are reading this in the right topic, everything works."),
        "parse_mode": "HTML",
    })
    log.info("Payload: %s", json.dumps({k: v for k, v in payload.items() if k != "text"}))

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json=payload, timeout=15)
    except Exception as exc:
        log.error("Could not reach the Telegram API: %s", exc)
        return 1

    log.info("HTTP %s", resp.status_code)
    try:
        data = resp.json()
    except ValueError:
        log.error("Non-JSON response: %s", resp.text[:300])
        return 1

    if not data.get("ok"):
        log.error("Telegram refused: %s - %s",
                  data.get("error_code"), data.get("description"))
        return 1

    result = data.get("result", {})
    chat   = result.get("chat", {})
    thread = result.get("message_thread_id")

    log.info("SENT successfully")
    log.info("  chat:  %s (%s)", chat.get("title") or chat.get("username"), chat.get("id"))
    log.info("  topic: %s", thread if thread is not None else
             "General (the message did NOT go to a topic)")

    if TELEGRAM_TOPIC_ID and str(thread) != str(TELEGRAM_TOPIC_ID):
        log.error("  MISMATCH: you asked for topic %s but it landed in %s",
                  TELEGRAM_TOPIC_ID, thread)
        return 1
    return 0


def log_destination() -> None:
    """State the delivery target at startup. Without this, "it went to the wrong
    channel" means guessing whether the problem is the secret, the workflow, or
    the code."""
    chat = TELEGRAM_CHAT_ID or "(EMPTY)"
    if TELEGRAM_TOPIC_ID:
        topic = TELEGRAM_TOPIC_ID
        if not TELEGRAM_TOPIC_ID.lstrip("-").isdigit():
            topic += "  <-- NOT A NUMBER, will be ignored"
    else:
        topic = "(NONE -> messages go to the General topic)"
    log.info("Telegram target: chat=%s | topic=%s", chat, topic)
    log.info("Status messages: %s%s", STATUS_MESSAGES,
             f" (topic {STATUS_TOPIC_ID})" if STATUS_TOPIC_ID else "")
    log.info("Alert threshold: score >= %d | min value: $%s | min price: $%.2f",
             SCORE_MIN_TO_SEND, f"{MIN_TRANSACTION_VALUE_USD:,}", MIN_PRICE)
    log.info("10b5-1 penalty: -%d%s | market data: %s",
             SCORE_PENALTY_10B5,
             " (excluded entirely)" if EXCLUDE_10B5 else "",
             "on" if ENABLE_MARKET_DATA else "off")

    extra = []
    if REQUIRE_DIRECT:
        extra.append("direct ownership required")
    if MIN_POSITION_INCREASE > 0:
        extra.append(f"position >= +{MIN_POSITION_INCREASE:.0f}%")
    if MIN_MARKET_CAP > 0:
        extra.append(f"market cap >= ${MIN_MARKET_CAP / 1e6:,.0f}M")
    if MAX_MARKET_CAP > 0:
        extra.append(f"market cap <= ${MAX_MARKET_CAP / 1e9:,.1f}B")
    if ONLY_TICKERS:
        extra.append(f"watchlist: {','.join(sorted(ONLY_TICKERS))}")
    if EXCLUDE_TICKERS:
        extra.append(f"excluded: {','.join(sorted(EXCLUDE_TICKERS))}")
    if extra:
        log.info("Extra filters: %s", " | ".join(extra))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SEC Insider Trading Alert Bot")
    parser.add_argument("--loop", action="store_true",
                        help="run continuously (for a VPS). Default: one cycle, then exit.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print alerts instead of sending them")
    parser.add_argument("--lookback", type=int, default=None,
                        help="force the window in minutes. By default it is derived "
                             "from the last successful run.")
    parser.add_argument("--db", default=DB_PATH, help="SQLite path")
    parser.add_argument("--test-telegram", action="store_true",
                        help="send one test message, report where it landed, then exit")
    args = parser.parse_args(argv)

    if args.test_telegram:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            log.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required for the test")
            return 2
        return test_telegram()

    check_config(args.dry_run)
    log_destination()
    conn = init_db(args.db)

    if not args.loop:
        stats = process_cycle(conn, args.lookback, dry_run=args.dry_run)
        conn.close()
        # Exit 1 when the cycle failed, so Actions marks the run as failed
        # instead of green-with-a-hidden-error.
        return 1 if stats.get("error") else 0

    log.info("loop mode")
    cycle = 0
    while True:
        cycle += 1
        try:
            secs, slot = sleep_seconds()
            log.info("── cycle %d │ %s ──", cycle, slot)
            process_cycle(conn, args.lookback, dry_run=args.dry_run)
            time.sleep(secs)
        except KeyboardInterrupt:
            log.info("stopped by user")
            return 0
        except Exception as exc:
            log.error("unexpected error in cycle %d: %s", cycle, exc, exc_info=True)
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
