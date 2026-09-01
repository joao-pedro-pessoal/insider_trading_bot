#!/usr/bin/env python3
"""
SEC INSIDER TRADING ALERT BOT v2.0
==================================

Telegram alerts for open-market insider purchases (SEC Form 4, code P).

Data comes straight from SEC EDGAR: free, no API key, no quota. v1.x used
sec-api.io, whose free tier is 100 lifetime calls -- the bot exhausted it in
days. EDGAR is the primary source that such services resell.

Designed to run one cycle per invocation on a cron (GitHub Actions), with an
optional --loop mode for a long-running VPS process.

How ingestion works, and why:

  * EDGAR publishes a daily index of every filing. We read the index for each
    day in the window, keep the Form 4 entries, and skip any accession number
    already in `processed_filings`. Only genuinely new filings are downloaded.

  * That accession-level dedup replaces time-window pagination entirely. The
    window now only decides *which days* to look at, so there is no cutoff
    arithmetic to get wrong -- which is where v1.0's worst bug lived.

  * Indexes for past days are immutable. Once a day is fully processed it is
    marked done and never fetched again. Only today's index is re-read.

  * Work is capped per run (MAX_FILINGS_PER_RUN). A large backlog is chipped
    away across runs instead of timing out, because progress is recorded as it
    happens rather than at the end.

  * Every parsed filing is recorded, not just the ones that alert. Cluster
    detection reads from that table, so filtering before writing would blind it.

EDGAR requires a User-Agent identifying you, and asks for no more than 10
requests/second. Both are respected below. See:
https://www.sec.gov/os/accessing-edgar-data

NOT financial advice. The scoring weights are heuristics and have never been
backtested. See README.md.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator, Optional
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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "y"}


def _env_list(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {t.strip().upper() for t in raw.split(",") if t.strip()}


TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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
STATUS_TOPIC_ID = os.environ.get("STATUS_TOPIC_ID", "").strip()

# ── EDGAR ─────────────────────────────────────────────────────────
# The SEC requires a User-Agent that identifies you, with a contact address.
# Requests without one get blocked. Format: "Your Name your@email.com".
EDGAR_USER_AGENT   = os.environ.get("EDGAR_USER_AGENT", "").strip()
EDGAR_BASE         = "https://www.sec.gov"
# The published ceiling is 10 requests/second. 0.15s leaves headroom.
EDGAR_DELAY        = _env_float("EDGAR_DELAY", 0.15)
# Filings downloaded per run. Caps runtime; the backlog carries to the next run.
MAX_FILINGS_PER_RUN = _env_int("MAX_FILINGS_PER_RUN", 400)
# Include amended filings (4/A). Off by default: they mostly restate filings
# already alerted on.
INCLUDE_AMENDMENTS = _env_bool("INCLUDE_AMENDMENTS", False)

DB_PATH = os.environ.get("DB_PATH", "state/alerts.db")

MIN_TRANSACTION_VALUE_USD = _env_int("MIN_TRANSACTION_VALUE_USD", 25_000)
CLUSTER_WINDOW_DAYS       = _env_int("CLUSTER_WINDOW_DAYS", 7)
SCORE_MIN_TO_SEND         = _env_int("SCORE_MIN_TO_SEND", 9)
SCORE_SILENT_BELOW        = _env_int("SCORE_SILENT_BELOW", 10)
SCORE_MAX_ALERT_FROM      = _env_int("SCORE_MAX_ALERT_FROM", 13)
SCORE_PENALTY_10B5        = _env_int("SCORE_PENALTY_10B5", 0)

# ── Hard filters: applied before scoring, they drop a filing entirely ──
MIN_PRICE             = _env_float("MIN_PRICE", 1.0)
EXCLUDE_10B5          = _env_bool("EXCLUDE_10B5", False)
REQUIRE_DIRECT        = _env_bool("REQUIRE_DIRECT", False)
MIN_POSITION_INCREASE = _env_float("MIN_POSITION_INCREASE", 0.0)
MIN_MARKET_CAP        = _env_float("MIN_MARKET_CAP", 0)
MAX_MARKET_CAP        = _env_float("MAX_MARKET_CAP", 0)
ONLY_TICKERS          = _env_list("ONLY_TICKERS")
EXCLUDE_TICKERS       = _env_list("EXCLUDE_TICKERS")

# Market-cap normalisation. Shares outstanding come from the SEC's own XBRL
# data (data.sec.gov), multiplied by the transaction price -- which is itself a
# recent market price. Same infrastructure and same User-Agent as the filings,
# so if EDGAR works this works.
ENABLE_MARKET_DATA = _env_bool("ENABLE_MARKET_DATA", True)
MARKET_CACHE_HOURS = _env_int("MARKET_CACHE_HOURS", 168)  # 7 days

# yfinance adds the 52-week range, which the SEC does not publish. Off by
# default: Yahoo blocks GitHub Actions IP ranges, so it costs time and returns
# nothing there. Worth enabling when running from your own machine or a VPS.
ENABLE_YFINANCE = _env_bool("ENABLE_YFINANCE", False)

# Cold-start lookback, used only when there is no previous run in the database.
LOOKBACK_MINUTES        = _env_int("LOOKBACK_MINUTES", 90)
LOOKBACK_BUFFER_MINUTES = _env_int("LOOKBACK_BUFFER_MINUTES", 25)
# Catch-up ceiling. Three days covers a full weekend with room to spare.
MAX_LOOKBACK_MINUTES    = _env_int("MAX_LOOKBACK_MINUTES", 4320)

HTTP_TIMEOUT = 25
MAX_RETRIES  = 3

logging.basicConfig(
    level=logging.DEBUG if _env_bool("VERBOSE") else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("InsiderBot")


class RateLimitError(Exception):
    """EDGAR returned 429, or blocked us for excessive requests."""


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

-- Log of EVERYTHING parsed as a purchase, including filings that never
-- triggered an alert. Cluster detection reads from here: filtering before
-- writing would blind it.
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

-- Every Form 4 we have downloaded, purchase or not. This is what stops the
-- bot re-downloading the same filings on every run, and it is what makes the
-- daily index approach cheap.
CREATE TABLE IF NOT EXISTS processed_filings (
    accession    TEXT PRIMARY KEY,
    filed_date   TEXT,
    was_purchase INTEGER DEFAULT 0,
    processed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_processed_date
    ON processed_filings (filed_date);

-- The bot's own state: last successful run, and which daily indexes are done.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Shares outstanding per issuer, from the SEC's XBRL data. Changes only when
-- a 10-Q or 10-K is filed, so a week of caching costs nothing in accuracy.
CREATE TABLE IF NOT EXISTS shares_cache (
    cik        TEXT PRIMARY KEY,
    shares     REAL,
    as_of      TEXT,
    fetched_at TEXT NOT NULL
);

-- Optional yfinance data (52-week range), when Yahoo is reachable.
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


def duplicate_event(conn: sqlite3.Connection, txn: dict) -> Optional[str]:
    """
    Has this exact purchase already been alerted under a different accession?

    Co-filers (fund groups, spouses, trustees) each file their own Form 4 for
    one economic event, so the same buy arrives two or three times with
    identical ticker, date, share count and price. Two different insiders
    independently buying the identical number of shares at the identical price
    on the identical day is not a thing that happens.

    Returns the accession of the earlier alert, or None.
    """
    cur = conn.execute(
        """
        SELECT t.accession_number
        FROM transactions t
        JOIN sent_alerts s ON s.accession_number = t.accession_number
        WHERE t.ticker = ? AND t.trade_date = ?
          AND t.quantity = ? AND ABS(t.price - ?) < 0.005
          AND t.accession_number != ?
        LIMIT 1
        """,
        (txn["ticker"], txn.get("trade_date"), txn["quantity"],
         txn["price"], txn["accession_number"]),
    )
    row = cur.fetchone()
    return row[0] if row else None


def is_processed(conn: sqlite3.Connection, accession: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM processed_filings WHERE accession = ?", (accession,)
    )
    return cur.fetchone() is not None


def mark_processed(conn: sqlite3.Connection, accession: str,
                   filed_date: str, was_purchase: bool) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO processed_filings
           (accession, filed_date, was_purchase, processed_at)
           VALUES (?, ?, ?, ?)""",
        (accession, filed_date, int(was_purchase), _utc_now_iso()),
    )


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


def days_in_window(lookback_minutes: int) -> list[date]:
    """
    Filing dates the window touches, oldest first, in New York time (EDGAR
    timestamps filings in ET). Weekends produce no index and are skipped later.
    """
    now_ny = datetime.now(NY_TZ)
    start = now_ny - timedelta(minutes=lookback_minutes)
    days = []
    d = start.date()
    while d <= now_ny.date():
        if d.weekday() < 5:      # EDGAR does not accept filings at weekends
            days.append(d)
        d += timedelta(days=1)
    return days


# ══════════════════════════════════════════════════════════════════
#  EDGAR INGESTION
# ══════════════════════════════════════════════════════════════════

def _describe_user_agent() -> str:
    """
    Describe EDGAR_USER_AGENT without printing it. Actions masks secret values
    in logs, so the value itself would appear as *** and tell us nothing --
    but its shape is enough to spot a wrong paste (a topic ID, an empty
    string, a bare email with no name).
    """
    ua = EDGAR_USER_AGENT
    if not ua:
        return "EMPTY - the secret is missing or not passed by the workflow"
    return (f"length={len(ua)} words={len(ua.split())} "
            f"has_at={'@' in ua} has_dot={'.' in ua} "
            f"digits_only={ua.isdigit()} "
            f"starts_with={'letter' if ua[:1].isalpha() else 'non-letter'}")


def _edgar_get(url: str) -> Optional[str]:
    """
    One EDGAR request, rate-limited and retried. Returns None on 404 (a missing
    daily index for a holiday is normal, not an error).
    """
    # No explicit Host header: requests derives it from the URL, and setting it
    # by hand only creates a way for it to be wrong.
    headers = {
        "User-Agent": EDGAR_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(EDGAR_DELAY)   # stay under the 10 req/s ceiling
            resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)

            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                raise RateLimitError(f"EDGAR rate limit (429) on {url}")
            if resp.status_code == 403:
                # Two very different things arrive as 403.
                #
                # 1. S3 answers "AccessDenied" for objects that do not exist,
                #    because the bucket does not grant ListBucket. A daily
                #    index that has not been published yet looks exactly like
                #    this, and it is not an error -- it is "come back later".
                # 2. EDGAR's own block page is HTML and says "undeclared
                #    automated tool". That one really is a rejected
                #    User-Agent, and retrying will never help.
                text = resp.text or ""
                if "AccessDenied" in text and "<Error>" in text:
                    log.debug("not published yet (S3 AccessDenied): %s", url)
                    return None

                body = " ".join(text.split())[:400]
                log.error("EDGAR 403. Response body: %s", body or "(empty)")
                log.error("User-Agent diagnostics: %s", _describe_user_agent())
                raise RuntimeError(
                    "EDGAR returned 403 and it is not a missing file. "
                    "'undeclared automated tool' in the body above means "
                    "EDGAR_USER_AGENT is wrong; a rate message means the IP "
                    "is being throttled."
                )
            resp.raise_for_status()
            return resp.text

        except (RateLimitError, RuntimeError):
            raise
        except Exception as exc:
            last_error = exc
            wait = 2 ** attempt
            log.warning("EDGAR attempt %d/%d failed (%s) - retrying in %ds",
                        attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)

    raise RuntimeError(f"EDGAR unreachable after {MAX_RETRIES} attempts: {last_error}")


def daily_index_url(day: date) -> str:
    quarter = (day.month - 1) // 3 + 1
    return (f"{EDGAR_BASE}/Archives/edgar/daily-index/"
            f"{day.year}/QTR{quarter}/form.{day:%Y%m%d}.idx")


def parse_daily_index(text: str) -> list[dict]:
    """
    Pull the Form 4 rows out of a daily index.

    The file is fixed-width-ish, but company names contain spaces, so fields
    are read from the right: the last token is the path, then the filing date,
    then the CIK.
    """
    filings = []
    wanted = {"4"} if not INCLUDE_AMENDMENTS else {"4", "4/A"}

    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("-"):
            continue

        head = line.split(None, 1)
        if not head or head[0] not in wanted:
            continue

        parts = line.rsplit(None, 3)
        if len(parts) < 4:
            continue
        _, cik, filed_date, path = parts

        if not path.endswith(".txt") or "/" not in path:
            continue

        # edgar/data/1930183/0001930183-26-000123.txt
        accession = path.rsplit("/", 1)[-1].removesuffix(".txt")
        filings.append({
            "accession": accession,
            "cik": cik.strip(),
            "filed_date": filed_date.strip(),
            "url": f"{EDGAR_BASE}/Archives/{path}",
        })

    return filings


def iter_new_filings(conn: sqlite3.Connection,
                     days: list[date]) -> Iterator[dict]:
    """
    Yield Form 4 filings from the given days that have not been processed yet.

    Indexes for past days are immutable, so once a day is fully processed it is
    marked done in `meta` and never fetched again. Only today's index is re-read
    on every run.
    """
    today = datetime.now(NY_TZ).date()

    for day in days:
        key = f"index_done_{day:%Y-%m-%d}"
        if day < today and get_meta(conn, key) == "1":
            log.debug("index %s already complete - skipping", day)
            continue

        text = _edgar_get(daily_index_url(day))
        if text is None:
            log.debug("no index for %s (holiday?)", day)
            if day < today:
                set_meta(conn, key, "1")
            continue

        entries = parse_daily_index(text)
        new = [e for e in entries if not is_processed(conn, e["accession"])]
        log.info("index %s: %d Form 4 filings, %d new", day, len(entries), len(new))

        for entry in new:
            entry["day"] = day
            entry["day_key"] = key
            entry["day_total"] = len(new)
            yield entry


ACCEPTANCE_RE = re.compile(r"<ACCEPTANCE-DATETIME>\s*(\d{14})")


def fetch_and_parse(entry: dict) -> Optional[dict]:
    """
    Download one Form 4 submission and turn it into a normalised transaction.

    The `.txt` is the complete submission with the ownership XML embedded, so
    this is one request per filing rather than a directory listing plus a
    document fetch.
    """
    raw = _edgar_get(entry["url"])
    if raw is None:
        return None

    match = re.search(r"<ownershipDocument>.*?</ownershipDocument>", raw, re.DOTALL)
    if not match:
        log.debug("no ownershipDocument in %s", entry["accession"])
        return None

    filed_at = entry["filed_date"]
    stamp = ACCEPTANCE_RE.search(raw)
    if stamp:
        # 20260901183045 -> 2026-09-01T18:30, in New York time
        s = stamp.group(1)
        filed_at = f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}"

    return parse_form4_xml(match.group(0), entry["accession"], filed_at)


# ══════════════════════════════════════════════════════════════════
#  FORM 4 XML PARSING
# ══════════════════════════════════════════════════════════════════

def _xt(node: Optional[ET.Element], path: str, default: str = "") -> str:
    """
    Text at `path`, unwrapping the <value> element that Form 4 uses for most
    fields (it exists so footnotes can be attached alongside).
    """
    if node is None:
        return default
    found = node.find(path)
    if found is None:
        return default
    value = found.find("value")
    target = value if value is not None else found
    return (target.text or "").strip() if target.text else default


def _xbool(node: Optional[ET.Element], path: str) -> bool:
    raw = _xt(node, path).strip().lower()
    return raw in {"1", "true"}


def parse_form4_xml(xml_text: str, accession: str,
                    filed_at: str) -> Optional[dict]:
    """
    Normalise a Form 4 ownership document into a single aggregated purchase.

    Aggregates EVERY non-derivative row coded P with an "A" (acquired) flag:
    an insider buying in three tranches would otherwise be undervalued.

    Returns None when the filing contains no open-market purchase, which is the
    common case -- most Form 4s are grants, option exercises and sales.
    """
    try:
        return _parse_xml(xml_text, accession, filed_at)
    except Exception as exc:
        log.debug("parse failed for %s (%s)", accession, exc)
        return None


def _parse_xml(xml_text: str, accession: str, filed_at: str) -> Optional[dict]:
    root = ET.fromstring(xml_text)

    issuer  = root.find("issuer")
    ticker  = _normalise_ticker(_xt(issuer, "issuerTradingSymbol"))
    company = _xt(issuer, "issuerName") or ticker
    cik     = _xt(issuer, "issuerCik").lstrip("0")

    owner = root.find("reportingOwner")
    insider_name = _xt(owner, "reportingOwnerId/rptOwnerName") or "Unknown"
    insider_cik  = _xt(owner, "reportingOwnerId/rptOwnerCik").lstrip("0")
    relationship = owner.find("reportingOwnerRelationship") if owner is not None else None
    title = _extract_title(relationship)

    purchases = []
    for txn in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _xt(txn, "transactionCoding/transactionCode")
        acq  = _xt(txn, "transactionAmounts/transactionAcquiredDisposedCode")
        if code == "P" and acq in ("A", ""):
            purchases.append(txn)

    if not purchases:
        return None

    total_shares = 0.0
    total_cost   = 0.0
    post_qty     = 0
    trade_dates: list[str] = []
    is_direct    = False

    for txn in purchases:
        shares = _safe_float(_xt(txn, "transactionAmounts/transactionShares"))
        price  = _safe_float(_xt(txn, "transactionAmounts/transactionPricePerShare"))
        if shares <= 0:
            continue
        total_shares += shares
        total_cost   += shares * price

        post = _safe_float(_xt(txn, "postTransactionAmounts/sharesOwnedFollowingTransaction"))
        post_qty = max(post_qty, int(post))

        trade_date = _xt(txn, "transactionDate")
        if trade_date:
            trade_dates.append(trade_date)

        # "D" = direct: the insider's own money, not a trust, LLC or spouse.
        if _xt(txn, "ownershipNature/directOrIndirectOwnership").upper() == "D":
            is_direct = True

    if total_shares <= 0:
        return None

    avg_price = total_cost / total_shares

    # Position increase, computed here rather than during scoring because the
    # filters need it too. None means the filing did not report a usable
    # post-transaction total (or it was a first purchase).
    pre = post_qty - int(total_shares)
    pct_increase = round((total_shares / pre) * 100, 1) if pre > 0 else None

    # aff10b5One is the official flag from the SEC's 2023 amendments. Older
    # filings predate it, so the footnotes are still worth checking.
    is_10b5 = _xbool(root, "aff10b5One") or _footnotes_mention_10b5(root)

    return {
        "accession_number": accession,
        "ticker":           ticker,
        "company":          company,
        "issuer_cik":       cik,
        "insider_name":     insider_name,
        "insider_cik":      insider_cik,
        "title":            title,
        "trade_date":       min(trade_dates) if trade_dates else _xt(root, "periodOfReport"),
        "filing_date":      filed_at,
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


# Placeholders filers use when the issuer has no listed symbol. Without this,
# "NONE" reads as a perfectly good ticker and produces alerts for companies
# that cannot be bought.
TICKER_PLACEHOLDERS = {"NONE", "N/A", "NA", "-", "--", "N.A.", "NOTAPPLICABLE"}


def _normalise_ticker(raw: str) -> str:
    ticker = (raw or "").strip().upper()
    return "" if ticker in TICKER_PLACEHOLDERS else ticker


def _footnotes_mention_10b5(root: ET.Element) -> bool:
    texts = [(f.text or "") for f in root.findall("footnotes/footnote")]
    return "10b5-1" in " ".join(texts).lower()


def _filing_url(issuer_cik: str, accession: str) -> str:
    """Human-readable EDGAR page for the filing."""
    if not issuer_cik or not accession:
        return "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4"
    plain = accession.replace("-", "")
    return (f"https://www.sec.gov/Archives/edgar/data/{issuer_cik}/"
            f"{plain}/{accession}-index.htm")


def _extract_title(rel: Optional[ET.Element]) -> str:
    if rel is None:
        return "Insider"
    parts = []
    if _xbool(rel, "isOfficer"):
        parts.append(_xt(rel, "officerTitle") or "Officer")
    if _xbool(rel, "isDirector"):
        parts.append("Director")
    if _xbool(rel, "isTenPercentOwner"):
        parts.append("10% Owner")
    if _xbool(rel, "isOther"):
        other = _xt(rel, "otherText")
        if other:
            parts.append(other)
    return ", ".join(parts) if parts else "Insider"


def _safe_float(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


# ══════════════════════════════════════════════════════════════════
#  MARKET DATA
# ══════════════════════════════════════════════════════════════════

SHARES_CONCEPTS = [
    ("dei", "EntityCommonStockSharesOutstanding"),   # cover page of every 10-Q/10-K
    ("us-gaap", "CommonStockSharesOutstanding"),     # fallback
]


def get_market_data(conn: sqlite3.Connection, txn: dict) -> Optional[dict]:
    """
    Market capitalisation for the issuer, so the score can tell a $100k buy in a
    nano-cap (enormous) from a $100k buy in Apple (a rounding error).

    cap = shares outstanding (SEC XBRL) x transaction price

    Using the insider's own execution price is deliberate: it is a real market
    price from the day of the trade, it needs no third-party quote feed, and it
    is the price the ratio should be measured at anyway.

    Returns None when unavailable; every caller must handle that.
    """
    if not ENABLE_MARKET_DATA:
        return None

    cik = txn.get("issuer_cik")
    price = txn.get("price") or 0
    if not cik or price <= 0:
        return None

    shares, as_of = get_shares_outstanding(conn, cik)
    if not shares:
        return None

    data = {
        "market_cap": shares * price,
        "shares_outstanding": shares,
        "price": price,
        "shares_as_of": as_of,
        "source": "sec-xbrl",
    }

    # Optional extra: the 52-week range, which the SEC does not publish.
    if ENABLE_YFINANCE:
        extra = _fetch_yfinance(txn.get("ticker"))
        if extra:
            data.update({k: v for k, v in extra.items() if v})

    return data


def get_shares_outstanding(conn: sqlite3.Connection,
                           cik: str) -> tuple[Optional[float], str]:
    """Shares outstanding for a CIK, cached. Returns (shares, as_of_date)."""
    cur = conn.execute(
        "SELECT shares, as_of, fetched_at FROM shares_cache WHERE cik = ?", (cik,)
    )
    row = cur.fetchone()
    if row:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(row[2])).total_seconds() / 3600
            if age_h < MARKET_CACHE_HOURS:
                return row[0], row[1] or ""
        except ValueError:
            pass  # unreadable timestamp, re-fetch

    shares, as_of = _fetch_shares_outstanding(cik)
    if shares:
        conn.execute(
            """INSERT OR REPLACE INTO shares_cache (cik, shares, as_of, fetched_at)
               VALUES (?, ?, ?, ?)""",
            (cik, shares, as_of, _utc_now_iso()),
        )
        conn.commit()
    return shares, as_of


def _fetch_shares_outstanding(cik: str) -> tuple[Optional[float], str]:
    """
    Read shares outstanding from the SEC's XBRL company-concept API.

    Companies report this on the cover page of every quarterly and annual
    report, so it is at most a quarter stale -- fine for sizing a purchase
    against a company, and far more dependable than a scraped quote.
    """
    padded = str(cik).zfill(10)

    for taxonomy, tag in SHARES_CONCEPTS:
        url = (f"https://data.sec.gov/api/xbrl/companyconcept/"
               f"CIK{padded}/{taxonomy}/{tag}.json")
        try:
            raw = _edgar_get(url)
        except Exception as exc:
            log.debug("shares lookup failed for CIK %s: %s", cik, exc)
            return None, ""
        if raw is None:
            continue   # company does not report this concept

        try:
            units = json.loads(raw).get("units", {}).get("shares", [])
        except ValueError:
            continue
        if not units:
            continue

        # Most recently filed wins; "end" breaks ties on the same filing date.
        latest = max(units, key=lambda u: (u.get("filed", ""), u.get("end", "")))
        value = _safe_float(latest.get("val"))
        if value > 0:
            return value, latest.get("end", "")

    log.debug("no shares-outstanding concept for CIK %s", cik)
    return None, ""


def _fetch_yfinance(ticker: Optional[str]) -> Optional[dict]:
    if not ticker:
        return None
    try:
        import yfinance as yf
    except ImportError:
        log.debug("yfinance not installed")
        return None

    try:
        info = yf.Ticker(ticker).fast_info
        return {
            "low_52w":  _safe_float(info.get("year_low")),
            "high_52w": _safe_float(info.get("year_high")),
        }
    except Exception as exc:
        log.debug("yfinance lookup failed for %s: %s", ticker, exc)
        return None


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
    exists so you can audit why an alert fired, and so a future backtest can
    test each component in isolation.

    Honest note: these weights are heuristics, not backtest output. A wider
    scale with more components looks more sophisticated but is exactly as
    unvalidated as a narrow one. Do not read the score as a probability.
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
    inside the cluster window. Counts people, not filings, and reads from every
    recorded transaction rather than only the ones that alerted.
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
    """Escape & < > for parse_mode=HTML. Company names containing an ampersand
    would otherwise produce a 400 and a silently lost alert."""
    return html.escape(str(value), quote=False)


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
            line += f"  (buy = {txn['pct_of_market_cap']:.3f}%)"
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
        f"\U0001F551 Filed:      <i>{esc(str(txn.get('filing_date')).replace('T', ' '))}</i>",
        f"\U0001F9EE Score:      <i>{esc(', '.join(why) if why else 'no criteria met')}</i>",
        "",
        "<i>Not financial advice. This signal has not been backtested.</i>",
    ]

    return {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
        "reply_markup": {"inline_keyboard": build_buttons(txn)},
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


def send_status(text: str, dry_run: bool = False) -> None:
    """Status message. Always silent (there are ~2 per run) and optionally in a
    separate topic."""
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
    days = days_in_window(lookback)

    log.info("Window: %dmin (%s) | days: %s", lookback, why_lookback,
             ", ".join(str(d) for d in days) or "none")

    stats = {"downloaded": 0, "purchases": 0, "alerts": 0, "filtered": 0,
             "lookback": lookback, "capped": False, "error": None}

    if STATUS_MESSAGES == "always":
        send_status(
            f"\U0001F504 <b>Cycle started</b>\n"
            f"Window: {lookback}min ({esc(why_lookback)})\n"
            f"Days: <code>{esc(', '.join(str(d) for d in days) or 'none')}</code>",
            dry_run=dry_run,
        )

    try:
        _run_ingestion(conn, days, stats, dry_run)
    except (RateLimitError, RuntimeError) as exc:
        log.error("Cycle aborted: %s", exc)
        stats["error"] = str(exc)
        if STATUS_MESSAGES in ("always", "summary", "errors"):
            send_status(f"❌ <b>Cycle failed</b>\n<code>{esc(exc)}</code>",
                        dry_run=dry_run)
        conn.commit()
        return stats

    elapsed = time.monotonic() - started
    stats["duration"] = elapsed

    log.info("cycle: %d downloaded, %d purchases, %d alerts, %d filtered in %s",
             stats["downloaded"], stats["purchases"], stats["alerts"],
             stats["filtered"], _fmt_duration(elapsed))

    # Only mark the run successful when the backlog was fully drained. If work
    # was capped, the window stays open so the next run continues from here.
    if not stats["capped"]:
        set_meta(conn, LAST_RUN_KEY, _utc_now_iso())
    else:
        log.info("Hit MAX_FILINGS_PER_RUN - the next run will continue the backlog")

    should_report = (
        STATUS_MESSAGES == "always"
        or (STATUS_MESSAGES == "summary" and stats["alerts"] > 0)
    )
    if should_report:
        extra = "\n⚠️ backlog capped, continuing next run" if stats["capped"] else ""
        send_status(
            f"✅ <b>Cycle finished</b> in {_fmt_duration(elapsed)}\n"
            f"\U0001F4E5 {stats['downloaded']} filings downloaded\n"
            f"\U0001F4B0 {stats['purchases']} open-market purchases\n"
            f"\U0001F514 {stats['alerts']} alert(s) sent\n"
            f"\U0001F6AB {stats['filtered']} filtered{extra}",
            dry_run=dry_run,
        )

    return stats


def _run_ingestion(conn: sqlite3.Connection, days: list[date],
                   stats: dict, dry_run: bool) -> None:
    """
    Download and process new Form 4 filings.

    Progress is committed as it goes: an interruption keeps everything done so
    far, so the next run resumes instead of restarting.
    """
    seen_per_day: dict[str, int] = {}
    done_per_day: dict[str, int] = {}

    for entry in iter_new_filings(conn, days):
        day_key = entry["day_key"]
        seen_per_day.setdefault(day_key, entry["day_total"])

        if stats["downloaded"] >= MAX_FILINGS_PER_RUN:
            stats["capped"] = True
            break

        txn = fetch_and_parse(entry)
        stats["downloaded"] += 1
        mark_processed(conn, entry["accession"], entry["filed_date"],
                       was_purchase=txn is not None)
        done_per_day[day_key] = done_per_day.get(day_key, 0) + 1
        conn.commit()

        if txn is None:
            continue   # grant, sale, option exercise -- most filings
        stats["purchases"] += 1

        if already_seen(conn, txn["accession_number"]):
            continue

        # Cheap filters first: no point paying for a market-data lookup on a
        # filing that a free check already rejects.
        passed, reason = passes_filters(txn)
        if passed:
            txn["market"] = get_market_data(conn, txn)
            passed, reason = passes_market_filters(txn)

        score, why = calculate_score(txn, conn)

        if not passed:
            record_transaction(conn, txn, score, alerted=False)   # feeds clusters
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

        twin = duplicate_event(conn, txn)
        if twin:
            log.info("  skipped: same purchase already alerted as %s "
                     "(co-filer)", twin)
            record_transaction(conn, txn, score, alerted=False)
            continue

        sent = send_telegram(build_message(txn, score, why), dry_run=dry_run)
        record_transaction(conn, txn, score, alerted=sent)
        if sent:
            record_alert(conn, txn, score)
            stats["alerts"] += 1
            time.sleep(0.4)   # headroom for the Telegram flood limit

    # A past day is complete once every filing it listed has been processed.
    # Marking it means its index is never downloaded again.
    if not stats["capped"]:
        today = datetime.now(NY_TZ).date()
        for day in days:
            key = f"index_done_{day:%Y-%m-%d}"
            if day < today and get_meta(conn, key) != "1":
                set_meta(conn, key, "1")
    conn.commit()


def sleep_seconds() -> tuple[int, str]:
    """Cadence by NYSE hours (only used in --loop mode)."""
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return 3600, "weekend"
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return 900, "market hours"
    if 16 * 60 <= minutes < 18 * 60 + 30:
        return 600, "post-close (filing peak)"
    return 3600, "off hours"


# ══════════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ══════════════════════════════════════════════════════════════════

def check_config(dry_run: bool) -> None:
    missing = []
    if not EDGAR_USER_AGENT:
        missing.append("EDGAR_USER_AGENT")
    if not dry_run:
        if not TELEGRAM_TOKEN:
            missing.append("TELEGRAM_TOKEN")
        if not TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
    if missing:
        log.error("Missing environment variables: %s", ", ".join(missing))
        if "EDGAR_USER_AGENT" in missing:
            log.error("  EDGAR requires you to identify yourself. Set it to "
                      "something like 'Your Name your@email.com'.")
        sys.exit(2)

    if "@" not in EDGAR_USER_AGENT:
        log.warning("EDGAR_USER_AGENT has no email address - the SEC may block "
                    "your requests. Use 'Your Name your@email.com'.")


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


def test_edgar() -> int:
    """Fetch today's (or the most recent) index and report what EDGAR returns.
    Answers 'is my User-Agent accepted and is the feed alive' in one run."""
    log.info("EDGAR_USER_AGENT shape: %s", _describe_user_agent())
    log.info("(the value itself is masked by Actions; the shape above is "
             "enough to tell a good value from a wrong paste)")

    day = datetime.now(NY_TZ).date()
    for _ in range(5):
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        url = daily_index_url(day)
        log.info("Fetching %s", url)
        try:
            text = _edgar_get(url)
        except Exception as exc:
            log.error("FAILED: %s", exc)
            return 1
        if text is not None:
            entries = parse_daily_index(text)
            log.info("OK - %d Form 4 filings listed for %s", len(entries), day)
            for e in entries[:3]:
                log.info("  %s  %s", e["accession"], e["url"])
            return 0
        log.info("  no index for %s, trying the previous day", day)
        day -= timedelta(days=1)

    log.error("No daily index found in the last 5 business days")
    return 1


def log_destination() -> None:
    """State the delivery target and the active thresholds at startup."""
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
    log.info("Source: SEC EDGAR as '%s'", EDGAR_USER_AGENT or "(EMPTY)")
    log.info("Alert threshold: score >= %d | min value: $%s | min price: $%.2f",
             SCORE_MIN_TO_SEND, f"{MIN_TRANSACTION_VALUE_USD:,}", MIN_PRICE)
    log.info("10b5-1 penalty: -%d%s | cap: %d filings/run",
             SCORE_PENALTY_10B5,
             " (excluded entirely)" if EXCLUDE_10B5 else "",
             MAX_FILINGS_PER_RUN)
    log.info("Market cap: %s | 52-week range: %s",
             "SEC XBRL shares x trade price" if ENABLE_MARKET_DATA else "off",
             "yfinance" if ENABLE_YFINANCE else "off")

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
    parser.add_argument("--test-edgar", action="store_true",
                        help="fetch one daily index and report what EDGAR returns")
    args = parser.parse_args(argv)

    if args.test_telegram:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            log.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required for the test")
            return 2
        return test_telegram()

    if args.test_edgar:
        return test_edgar()

    check_config(args.dry_run)
    log_destination()
    conn = init_db(args.db)

    if not args.loop:
        stats = process_cycle(conn, args.lookback, dry_run=args.dry_run)
        conn.close()
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
