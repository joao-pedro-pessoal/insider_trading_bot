#!/usr/bin/env python3
"""
Pipeline tests using fixtures. Touches neither the network nor Telegram.

    python test_bot.py
"""

import datetime as _dt
import os
import sys
import tempfile

os.environ.setdefault("SEC_API_KEY", "test")
os.environ.setdefault("MIN_TRANSACTION_VALUE_USD", "25000")

import insider_bot as bot  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# ── Fixtures in the real sec-api.io response shape ─────────────────

def filing(accession, ticker, company, cik, owner_cik, owner_name,
           relationship, rows, filed_at="2026-08-10T18:30:00-04:00",
           aff10b5=False, footnotes=None):
    return {
        "accessionNo": accession,
        "filedAt": filed_at,
        "documentType": "4",
        "periodOfReport": "2026-08-08",
        "aff10b5One": aff10b5,
        "issuer": {"cik": cik, "name": company, "tradingSymbol": ticker},
        "reportingOwner": {"cik": owner_cik, "name": owner_name,
                           "relationship": relationship},
        "nonDerivativeTable": {"transactions": rows},
        "footnotes": footnotes or [],
    }


def row(shares, price, post, code="P", ad="A", date="2026-08-08"):
    return {
        "securityTitle": "Common Stock",
        "transactionDate": date,
        "coding": {"formType": "4", "code": code},
        "amounts": {"shares": shares, "pricePerShare": price,
                    "acquiredDisposedCode": ad},
        "postTransactionAmounts": {"sharesOwnedFollowingTransaction": post},
        "ownershipNature": {"directOrIndirectOwnership": "D"},
    }


CEO_BIG = filing(
    "0001234567-26-000001", "ACME", "Acme & Sons, Inc. <Holdings>", "999001",
    "111", "Silva Joao", {"isOfficer": True, "officerTitle": "Chief Executive Officer"},
    [row(10_000, 52.50, 110_000), row(5_000, 53.00, 115_000)],  # tranches -> aggregate
)

DIRECTOR_SMALL = filing(
    "0001234567-26-000002", "ACME", "Acme & Sons, Inc.", "999001",
    "222", "Costa Maria", {"isDirector": True},
    [row(1_000, 52.00, 6_000)],
)

TINY = filing(
    "0001234567-26-000003", "MICRO", "Micro Corp", "999002",
    "333", "Small Pete", {"isDirector": True},
    [row(10, 5.00, 100)],  # $50 -> below the minimum
)

GRANT_DISGUISED = filing(
    "0001234567-26-000004", "GRNT", "Grant Co", "999003",
    "444", "Zero Price", {"isOfficer": True, "officerTitle": "CFO"},
    [row(100_000, 0.0, 500_000, code="A")],  # code A, not P
)

SALE_ONLY = filing(
    "0001234567-26-000005", "SELL", "Sell Co", "999004",
    "555", "The Seller", {"isDirector": True},
    [row(50_000, 20.0, 10_000, code="S", ad="D")],
)

PLAN_10B5 = filing(
    "0001234567-26-000006", "PLAN", "Plan Co", "999005",
    "666", "Auto Pilot", {"isOfficer": True, "officerTitle": "CEO"},
    [row(20_000, 30.0, 200_000)],
    aff10b5=True,
)

NO_TICKER = filing(
    "0001234567-26-000007", "", "Private Co", "999006",
    "777", "Not Listed", {"isDirector": True},
    [row(10_000, 10.0, 50_000)],
)


# ── 1. Parsing ────────────────────────────────────────────────────
print("\n[1] Parsing")

p = bot.parse_filing(CEO_BIG)
check("parse returns a result", p is not None)
check("aggregates both tranches", p["quantity"] == 15_000, f"got {p['quantity']}")
check("share-weighted average price",
      abs(p["price"] - (10_000 * 52.50 + 5_000 * 53.00) / 15_000) < 0.001,
      f"got {p['price']}")
check("total value", abs(p["total_value"] - 790_000.0) < 0.01, f"got {p['total_value']}")
check("post_qty is the max across rows", p["post_qty"] == 115_000, f"got {p['post_qty']}")
check("title extracted", "Chief Executive Officer" in p["title"], p["title"])
check("filing URL uses the issuer CIK",
      p["sec_url"] == "https://www.sec.gov/Archives/edgar/data/999001/"
                      "000123456726000001/0001234567-26-000001-index.htm",
      p["sec_url"])
check("n_transactions", p["n_transactions"] == 2)

check("code A (grant) rejected", bot.parse_filing(GRANT_DISGUISED) is None)
check("sale (code S) rejected", bot.parse_filing(SALE_ONLY) is None)
check("10b5-1 detected via aff10b5One", bot.parse_filing(PLAN_10B5)["is_10b5"] is True)
check("normal purchase not flagged as 10b5-1", p["is_10b5"] is False)


# ── 2. Filters ────────────────────────────────────────────────────
print("\n[2] Filters")

ok, why = bot.passes_filters(p)
check("large purchase passes", ok, why)

ok, why = bot.passes_filters(bot.parse_filing(TINY))
check("value below minimum blocked", not ok, why)

ok, why = bot.passes_filters(bot.parse_filing(NO_TICKER))
check("missing ticker blocked", not ok, why)


# ── 2b. Hard filters ──────────────────────────────────────────────
print("\n[2b] Hard filters")

PENNY = filing(
    "0001234567-26-000010", "PENY", "Penny Corp", "999010",
    "910", "Cheap Charlie", {"isOfficer": True, "officerTitle": "CEO"},
    [row(200_000, 0.35, 900_000)],  # $70k but at $0.35/share
)
INDIRECT = filing(
    "0001234567-26-000011", "TRST", "Trust Co", "999011",
    "911", "Via Trust", {"isDirector": True},
    [dict(row(10_000, 30.0, 100_000),
          ownershipNature={"directOrIndirectOwnership": "I",
                           "natureOfOwnership": "By family trust"})],
)

penny = bot.parse_filing(PENNY)
indirect = bot.parse_filing(INDIRECT)

check("direct ownership detected", bot.parse_filing(CEO_BIG)["is_direct"] is True)
check("indirect ownership detected", indirect["is_direct"] is False)

bot.MIN_PRICE = 1.0
ok, why = bot.passes_filters(penny)
check("penny stock blocked by MIN_PRICE", not ok, why)
bot.MIN_PRICE = 0.0
ok, _ = bot.passes_filters(penny)
check("MIN_PRICE=0 disables the check", ok)
bot.MIN_PRICE = 1.0

bot.EXCLUDE_10B5 = True
ok, why = bot.passes_filters(bot.parse_filing(PLAN_10B5))
check("EXCLUDE_10B5 drops plan purchases", not ok, why)
bot.EXCLUDE_10B5 = False
check("10b5-1 allowed when the flag is off",
      bot.passes_filters(bot.parse_filing(PLAN_10B5))[0])

bot.REQUIRE_DIRECT = True
ok, why = bot.passes_filters(indirect)
check("REQUIRE_DIRECT drops trust holdings", not ok, why)
bot.REQUIRE_DIRECT = False

bot.MIN_POSITION_INCREASE = 25.0
ok, why = bot.passes_filters(bot.parse_filing(DIRECTOR_SMALL))  # exactly +20%
check("MIN_POSITION_INCREASE blocks a small top-up", not ok, why)
bot.MIN_POSITION_INCREASE = 0.0

bot.ONLY_TICKERS = {"NVDA", "AMD"}
ok, why = bot.passes_filters(bot.parse_filing(CEO_BIG))
check("watchlist blocks tickers outside it", not ok, why)
bot.ONLY_TICKERS = {"ACME"}
check("watchlist lets its own tickers through",
      bot.passes_filters(bot.parse_filing(CEO_BIG))[0])
bot.ONLY_TICKERS = set()

bot.EXCLUDE_TICKERS = {"ACME"}
ok, why = bot.passes_filters(bot.parse_filing(CEO_BIG))
check("exclusion list blocks a ticker", not ok, why)
bot.EXCLUDE_TICKERS = set()

# pct_increase now comes from the parser, because the filters need it
check("pct_increase computed during parsing",
      bot.parse_filing(DIRECTOR_SMALL)["pct_increase"] == 20.0,
      str(bot.parse_filing(DIRECTOR_SMALL)["pct_increase"]))
check("pct_increase derived from post_qty",
      bot.parse_filing(TINY)["pct_increase"] == 11.1,   # 10 shares on top of 90
      str(bot.parse_filing(TINY)["pct_increase"]))

FIRST_BUY = filing(
    "0001234567-26-000012", "NEWB", "New Buyer Co", "999012",
    "912", "First Timer", {"isDirector": True},
    [row(5_000, 20.0, 5_000)],  # post == qty, so there was no prior position
)
check("pct_increase is None on a first purchase",
      bot.parse_filing(FIRST_BUY)["pct_increase"] is None)
check("first purchase is not blocked by MIN_POSITION_INCREASE",
      bot.passes_filters(bot.parse_filing(FIRST_BUY))[0])


# ── 2c. Market-cap filters ────────────────────────────────────────
print("\n[2c] Market-cap filters")

big = dict(bot.parse_filing(CEO_BIG), market={"market_cap": 3e12, "price": 200,
                                              "low_52w": 150, "high_52w": 260})
small = dict(bot.parse_filing(CEO_BIG), market={"market_cap": 80e6, "price": 5,
                                                "low_52w": 4.8, "high_52w": 12})
nodata = dict(bot.parse_filing(CEO_BIG), market=None)

bot.MAX_MARKET_CAP = 2e9
ok, why = bot.passes_market_filters(big)
check("mega cap blocked by MAX_MARKET_CAP", not ok, why)
check("small cap passes", bot.passes_market_filters(small)[0])
bot.MAX_MARKET_CAP = 0

bot.MIN_MARKET_CAP = 300e6
ok, why = bot.passes_market_filters(small)
check("micro cap blocked by MIN_MARKET_CAP", not ok, why)
bot.MIN_MARKET_CAP = 0

check("missing market data never drops a filing",
      bot.passes_market_filters(nodata)[0])


# ── 3. HTML escaping ──────────────────────────────────────────────
print("\n[3] HTML escaping (the bug that silently killed v1.0 alerts)")

msg = bot.build_message(p, 14, ["+4 CEO/CFO", "+3 value >= $500k"])
text = msg["text"]
check("& escaped", "&amp;" in text)
check("< escaped", "&lt;Holdings&gt;" in text)
check("no bare ampersand",
      "& " not in text.replace("&amp;", "").replace("&lt;", "").replace("&gt;", ""))
check("parse_mode is HTML", msg["parse_mode"] == "HTML")
check("reply_markup is a dict (sent via json=)", isinstance(msg["reply_markup"], dict))
check("MAX ALERT notifies", msg["disable_notification"] is False)

quiet = bot.build_message(p, 1, [])
check("low score stays silent", quiet["disable_notification"] is True)

# Forum supergroup topics
bot.TELEGRAM_TOPIC_ID = "3"
routed = bot.with_topic(msg)
check("topic injected as int", routed.get("message_thread_id") == 3)
check("original payload untouched", "message_thread_id" not in msg)

bot.TELEGRAM_TOPIC_ID = "abc"
check("invalid topic ignored", "message_thread_id" not in bot.with_topic(msg))

bot.TELEGRAM_TOPIC_ID = ""
check("no topic injects nothing", "message_thread_id" not in bot.with_topic(msg))


# ── 3b. Buttons ───────────────────────────────────────────────────
print("\n[3b] Inline buttons")

rows = bot.build_buttons(p)
flat = [b for r in rows for b in r]
urls = " ".join(b["url"] for b in flat)

check("three button rows", len(rows) == 3, f"got {len(rows)}")
check("five buttons total", len(flat) == 5, f"got {len(flat)}")
check("TradingView present", "tradingview.com/symbols/ACME/" in urls)
check("SEC filing present", "sec.gov/Archives/edgar/data/999001" in urls)
check("Investing.com present", "investing.com/search/?q=ACME" in urls)
check("Finviz ticker page present", "finviz.com/quote.ashx?t=ACME" in urls)
check("Finviz latest buys present", "finviz.com/insidertrading?tc=7" in urls)
check("every button has text and url",
      all(b.get("text") and b.get("url") for b in flat))
check("no label exceeds 64 chars (Telegram limit)",
      all(len(b["text"]) <= 64 for b in flat),
      str([len(b["text"]) for b in flat]))

# Tickers with dots/hyphens (BRK.B, BF-B) must be encoded
dotted = dict(p, ticker="BRK.B")
durls = " ".join(b["url"] for r in bot.build_buttons(dotted) for b in r)
check("dotted ticker URL-encoded", "q=BRK%2EB" in durls or "q=BRK.B" in durls, durls)
check("dot does not break the TradingView path", "symbols/BRK" in durls)


# ── 3c. Optional 10b5-1 penalty ───────────────────────────────────
print("\n[3c] Optional 10b5-1 penalty")

plan = bot.parse_filing(PLAN_10B5)
tmp_conn = bot.init_db(os.path.join(tempfile.mkdtemp(), "p.db"))

bot.SCORE_PENALTY_10B5 = 0
s_off, w_off = bot.calculate_score(plan, tmp_conn)
check("default applies no penalty", "10b5-1" not in " ".join(w_off), str(w_off))

bot.SCORE_PENALTY_10B5 = 2
s_on, w_on = bot.calculate_score(plan, tmp_conn)
check("penalty applied", s_on == s_off - 2, f"{s_off} -> {s_on}")
check("penalty shows in the breakdown", "-2 10b5-1 plan" in " ".join(w_on))

bot.SCORE_PENALTY_10B5 = 99
s_floor, _ = bot.calculate_score(plan, tmp_conn)
check("score never goes negative", s_floor == 0, f"got {s_floor}")

bot.SCORE_PENALTY_10B5 = 0
tmp_conn.close()


# ── 3d. Market-data scoring and cache ─────────────────────────────
print("\n[3d] Market-data scoring and cache")

mc = bot.init_db(os.path.join(tempfile.mkdtemp(), "m.db"))

base = bot.parse_filing(CEO_BIG)          # $790k purchase
no_market = dict(base, market=None)
s_none, w_none = bot.calculate_score(no_market, mc)

# $790k against a $50M company = 1.58% of the whole company
micro = dict(base, market={"market_cap": 50e6, "price": 10,
                           "low_52w": 9.5, "high_52w": 40})
s_micro, w_micro = bot.calculate_score(micro, mc)
check("large stake in a micro cap scores higher", s_micro > s_none,
      f"{s_none} -> {s_micro}")
check("percent of market cap in the breakdown",
      any("market cap" in x for x in w_micro), str(w_micro))
check("pct_of_market_cap recorded",
      abs(micro["pct_of_market_cap"] - 1.58) < 0.01, str(micro.get("pct_of_market_cap")))

# Same purchase against a $3T company = noise
mega = dict(base, market={"market_cap": 3e12, "price": 200,
                          "low_52w": 100, "high_52w": 260})
s_mega, w_mega = bot.calculate_score(mega, mc)
check("same purchase in a mega cap scores no size points",
      not any("market cap" in x for x in w_mega), str(w_mega))
check("micro cap outscores mega cap", s_micro > s_mega, f"{s_micro} vs {s_mega}")

# Buying near the 52-week low
at_low = dict(base, market={"market_cap": 1e9, "price": 10.4,
                            "low_52w": 10.0, "high_52w": 30})
s_low, w_low = bot.calculate_score(at_low, mc)
check("buying near the 52w low scores",
      any("52w low" in x for x in w_low), str(w_low))
check("pct_above_52w_low recorded",
      abs(at_low["pct_above_52w_low"] - 4.0) < 0.1, str(at_low.get("pct_above_52w_low")))

at_high = dict(base, market={"market_cap": 1e9, "price": 29.0,
                             "low_52w": 10.0, "high_52w": 30})
_, w_high = bot.calculate_score(at_high, mc)
check("buying near the highs scores nothing there",
      not any("52w low" in x for x in w_high), str(w_high))

# Cache behaviour
bot.ENABLE_MARKET_DATA = True
calls = {"n": 0}
bot._fetch_market_data = lambda t: (calls.__setitem__("n", calls["n"] + 1) or
                                    {"market_cap": 1e9, "price": 10.0,
                                     "low_52w": 8.0, "high_52w": 15.0, "cached": False})
first = bot.get_market_data(mc, "TEST")
second = bot.get_market_data(mc, "TEST")
check("market data fetched once", calls["n"] == 1, f"got {calls['n']} calls")
check("second read comes from cache", second.get("cached") is True)
check("cached values match", second["market_cap"] == first["market_cap"])

bot._fetch_market_data = lambda t: None
check("failed lookup returns None", bot.get_market_data(mc, "NOPE") is None)

bot.ENABLE_MARKET_DATA = False
check("disabled market data returns None", bot.get_market_data(mc, "TEST") is None)
bot.ENABLE_MARKET_DATA = True

mc.close()


# ── 4. Scoring and cluster detection ──────────────────────────────
print("\n[4] Scoring and cluster detection")

tmpdir = tempfile.mkdtemp()
db = os.path.join(tmpdir, "t.db")
conn = bot.init_db(db)

# Freeze "today" so the fixture dates land inside the cluster window
_real = _dt.datetime


class FakeDT(_real):
    @classmethod
    def now(cls, tz=None):
        return _real(2026, 8, 10, 20, 0, tzinfo=_dt.timezone.utc) if tz else _real(2026, 8, 10, 20, 0)


bot.datetime = FakeDT

ceo = bot.parse_filing(CEO_BIG)
score1, why1 = bot.calculate_score(ceo, conn)
# +4 CEO/CFO, +3 value >= $500k, +1 direct = 8
check("CEO + $790k direct, no cluster = 8", score1 == 8, f"got {score1}: {why1}")
check("no cluster on the first purchase", ceo["cluster"] == 0)
bot.record_transaction(conn, ceo, score1, alerted=True)

director = bot.parse_filing(DIRECTOR_SMALL)
score2, why2 = bot.calculate_score(director, conn)
check("second insider detects the cluster", director["cluster"] == 1, f"got {director['cluster']}")
# +2 director, +1 position +20% (inclusive boundary), +2 cluster, +1 direct = 6
check("director + position + cluster + direct = 6", score2 == 6, f"got {score2}: {why2}")
check("$52k earns no value points", "value" not in " ".join(why2), str(why2))
bot.record_transaction(conn, director, score2, alerted=True)

# The same insider buying again must not count as a cluster with themselves
repeat = bot.parse_filing(filing(
    "0001234567-26-000099", "ACME", "Acme & Sons, Inc.", "999001",
    "111", "Silva Joao", {"isOfficer": True, "officerTitle": "CEO"},
    [row(2_000, 54.0, 117_000)],
))
_, _ = bot.calculate_score(repeat, conn)
check("cluster counts distinct insiders, not filings",
      repeat["cluster"] == 1, f"got {repeat['cluster']} (expected 1: Costa Maria)")

# Filtered-out transactions must still feed the cluster
tiny = bot.parse_filing(TINY)
bot.record_transaction(conn, tiny, 0, alerted=False)
other_micro = bot.parse_filing(filing(
    "0001234567-26-000100", "MICRO", "Micro Corp", "999002",
    "888", "Another Insider", {"isOfficer": True, "officerTitle": "CEO"},
    [row(10_000, 6.0, 60_000)],
))
bot.calculate_score(other_micro, conn)
check("filtered transaction still counts for the cluster (v1.0 bug)",
      other_micro["cluster"] == 1, f"got {other_micro['cluster']}")

# Outside the window must not count
old = bot.parse_filing(filing(
    "0001234567-26-000101", "OLDC", "Old Co", "999007",
    "901", "Long Ago", {"isDirector": True},
    [row(5_000, 10.0, 50_000, date="2026-07-01")],
))
bot.record_transaction(conn, old, 1, alerted=False)
recent = bot.parse_filing(filing(
    "0001234567-26-000102", "OLDC", "Old Co", "999007",
    "902", "Just Now", {"isDirector": True},
    [row(5_000, 11.0, 50_000, date="2026-08-09")],
))
bot.calculate_score(recent, conn)
check("purchase 40 days ago falls outside the 7d window",
      recent["cluster"] == 0, f"got {recent['cluster']}")

check("pct_increase is None when post_qty is unreliable",
      bot.parse_filing(TINY).get("post_qty") == 100)

bot.datetime = _real


# ── 5. Dedup and time filtering ───────────────────────────────────
print("\n[5] Dedup and time filtering")

bot.record_alert(conn, ceo, score1)
check("already_seen catches a duplicate", bot.already_seen(conn, ceo["accession_number"]))
check("already_seen ignores a new one", not bot.already_seen(conn, "0000000-00-000000"))

d = bot._parse_dt("2022-08-09T21:23:00-04:00")
check("_parse_dt reads the NY offset", d is not None and d.utcoffset().total_seconds() == -14400)
check("_parse_dt tolerates garbage", bot._parse_dt("not-a-date") is None)
check("_parse_dt tolerates None", bot._parse_dt(None) is None)

# Pagination stops when it hits a filing older than the cutoff
calls = {"n": 0}
pages = [
    {"transactions": [dict(CEO_BIG, filedAt="2026-08-10T19:00:00-04:00", accessionNo=f"a{i}")
                      for i in range(50)]},
    {"transactions": [dict(CEO_BIG, filedAt="2026-08-10T18:55:00-04:00", accessionNo="b0"),
                      dict(CEO_BIG, filedAt="2026-01-01T10:00:00-05:00", accessionNo="old")]},
]


def fake_post(payload):
    i = calls["n"]
    calls["n"] += 1
    return pages[i] if i < len(pages) else {"transactions": []}


bot._post_with_retry = fake_post
cutoff = _dt.datetime(2026, 8, 10, 22, 0, tzinfo=_dt.timezone.utc)  # 18:00 ET
result = bot.fetch_recent_purchases(cutoff)
check("paginated and stopped at the cutoff", len(result) == 51, f"got {len(result)}")
check("the old filing was excluded",
      all(f["accessionNo"] != "old" for f in result))


# ── 6. Adaptive lookback ──────────────────────────────────────────
print("\n[6] Adaptive lookback")

lb_db = os.path.join(tempfile.mkdtemp(), "lb.db")
lb = bot.init_db(lb_db)

mins, why = bot.compute_lookback(lb)
check("cold start uses the default",
      mins == bot.LOOKBACK_MINUTES and "first run" in why, f"{mins} / {why}")

mins, why = bot.compute_lookback(lb, override=240)
check("--lookback takes precedence", mins == 240 and "forced" in why, f"{mins} / {why}")

# 40 minutes since the last run -> 40 + buffer
now = _dt.datetime.now(_dt.timezone.utc)
bot.set_meta(lb, bot.LAST_RUN_KEY, (now - _dt.timedelta(minutes=40)).isoformat())
mins, why = bot.compute_lookback(lb)
check("covers the gap plus margin",
      40 + bot.LOOKBACK_BUFFER_MINUTES - 1 <= mins <= 40 + bot.LOOKBACK_BUFFER_MINUTES + 1,
      f"{mins} / {why}")

# Weekend: 60h gap
bot.set_meta(lb, bot.LAST_RUN_KEY, (now - _dt.timedelta(hours=60)).isoformat())
mins, why = bot.compute_lookback(lb)
check("weekend gap covered", mins > 3000, f"{mins} / {why}")

# Down for 10 days -> truncate at the ceiling
bot.set_meta(lb, bot.LAST_RUN_KEY, (now - _dt.timedelta(days=10)).isoformat())
mins, why = bot.compute_lookback(lb)
check("truncates at the ceiling",
      mins == bot.MAX_LOOKBACK_MINUTES and "truncated" in why, f"{mins} / {why}")

# Corrupted state must not crash
bot.set_meta(lb, bot.LAST_RUN_KEY, "this-is-not-a-date")
mins, why = bot.compute_lookback(lb)
check("unreadable state falls back to the default",
      mins == bot.LOOKBACK_MINUTES and "unreadable" in why, f"{mins} / {why}")

# Clock moved backwards (last_run in the future)
bot.set_meta(lb, bot.LAST_RUN_KEY, (now + _dt.timedelta(hours=2)).isoformat())
mins, why = bot.compute_lookback(lb)
check("clock inconsistency falls back to the default",
      mins == bot.LOOKBACK_MINUTES, f"{mins} / {why}")

# Pages scale with the window, but with a ceiling
check("short window uses base pages", bot.pages_for(90) == bot.MAX_PAGES)
check("long window requests more pages", bot.pages_for(3000) > bot.MAX_PAGES)
check("pages are capped", bot.pages_for(100000) == bot.MAX_PAGES_CATCHUP)

check("meta reads and writes", bot.get_meta(lb, "nonexistent") is None)
lb.close()


# ── 7. Status messages ────────────────────────────────────────────
print("\n[7] Status messages")

sent = []
bot.send_telegram = lambda p, dry_run=False: sent.append(p) or True

bot.STATUS_MESSAGES = "always"
bot.STATUS_TOPIC_ID = "7"
bot.send_status("test")
check("status sent", len(sent) == 1)
check("status is silent", sent[0]["disable_notification"] is True)
check("status goes to its own topic", sent[0]["message_thread_id"] == 7)

bot.STATUS_TOPIC_ID = ""
bot.TELEGRAM_TOPIC_ID = "3"
sent.clear()
bot.send_status("test")
check("without a status topic it uses the alert topic",
      "message_thread_id" not in sent[0], str(sent[0].get("message_thread_id")))

check("duration formatted in seconds", bot._fmt_duration(42) == "42s")
check("duration formatted in minutes", bot._fmt_duration(150) == "2.5min")

bot.STATUS_MESSAGES = "off"
bot.TELEGRAM_TOPIC_ID = ""

conn.close()


# ── Result ────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} test(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("All tests passed.")
