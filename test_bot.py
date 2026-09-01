#!/usr/bin/env python3
"""
Pipeline tests using Form 4 XML fixtures. Touches neither the network,
EDGAR nor Telegram.

    python test_bot.py
"""

import datetime as _dt
import os
import sys
import tempfile

os.environ.setdefault("EDGAR_USER_AGENT", "Test Runner test@example.com")
os.environ.setdefault("MIN_TRANSACTION_VALUE_USD", "25000")

import insider_bot as bot  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# ── Fixtures in the real Form 4 XML shape ──────────────────────────

def txn_xml(shares, price, post, code="P", ad="A",
            date="2026-08-08", ownership="D"):
    return f"""
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{date}</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>{code}</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>{post}</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>{ownership}</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>"""


def form4(ticker, company, cik, owner_cik, owner_name, rows,
          officer_title=None, director=False, ten_pct=False,
          aff10b5=False, footnote=None):
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2026-08-08</periodOfReport>
  <notSubjectToSection16>0</notSubjectToSection16>
  <aff10b5One>{'1' if aff10b5 else '0'}</aff10b5One>
  <issuer>
    <issuerCik>{cik.zfill(10)}</issuerCik>
    <issuerName>{company}</issuerName>
    <issuerTradingSymbol>{ticker}</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>{owner_cik.zfill(10)}</rptOwnerCik>
      <rptOwnerName>{owner_name}</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{'1' if director else '0'}</isDirector>
      <isOfficer>{'1' if officer_title else '0'}</isOfficer>
      {f'<officerTitle>{officer_title}</officerTitle>' if officer_title else ''}
      <isTenPercentOwner>{'1' if ten_pct else '0'}</isTenPercentOwner>
      <isOther>0</isOther>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>{''.join(rows)}</nonDerivativeTable>
  <footnotes>{f'<footnote id="F1">{footnote}</footnote>' if footnote else ''}</footnotes>
</ownershipDocument>"""


def parse(xml, accession="0001234567-26-000001", filed="2026-08-10T18:30"):
    return bot.parse_form4_xml(xml, accession, filed)


CEO_BIG = form4("ACME", "Acme &amp; Sons, Inc.", "999001", "111", "Silva Joao",
                [txn_xml(10_000, 52.50, 110_000), txn_xml(5_000, 53.00, 115_000)],
                officer_title="Chief Executive Officer")

DIRECTOR_SMALL = form4("ACME", "Acme &amp; Sons, Inc.", "999001", "222", "Costa Maria",
                       [txn_xml(1_000, 52.00, 6_000)], director=True)

TINY = form4("MICRO", "Micro Corp", "999002", "333", "Small Pete",
             [txn_xml(10, 5.00, 100)], director=True)

GRANT = form4("GRNT", "Grant Co", "999003", "444", "Zero Price",
              [txn_xml(100_000, 0.0, 500_000, code="A")], officer_title="CFO")

SALE = form4("SELL", "Sell Co", "999004", "555", "The Seller",
             [txn_xml(50_000, 20.0, 10_000, code="S", ad="D")], director=True)

PLAN_10B5 = form4("PLAN", "Plan Co", "999005", "666", "Auto Pilot",
                  [txn_xml(20_000, 30.0, 200_000)],
                  officer_title="CEO", aff10b5=True)

OLD_10B5 = form4("OLDP", "Old Plan Co", "999008", "668", "Legacy Plan",
                 [txn_xml(20_000, 30.0, 200_000)], officer_title="CEO",
                 footnote="Shares purchased under a Rule 10b5-1 trading plan "
                          "adopted on March 1, 2026.")

NO_TICKER = form4("", "Private Co", "999006", "777", "Not Listed",
                  [txn_xml(10_000, 10.0, 50_000)], director=True)

PENNY = form4("PENY", "Penny Corp", "999010", "910", "Cheap Charlie",
              [txn_xml(200_000, 0.35, 900_000)], officer_title="CEO")

INDIRECT = form4("TRST", "Trust Co", "999011", "911", "Via Trust",
                 [txn_xml(10_000, 30.0, 100_000, ownership="I")], director=True)

FIRST_BUY = form4("NEWB", "New Buyer Co", "999012", "912", "First Timer",
                  [txn_xml(5_000, 20.0, 5_000)], director=True)


# ── 1. XML parsing ────────────────────────────────────────────────
print("\n[1] Form 4 XML parsing")

p = parse(CEO_BIG)
check("parse returns a result", p is not None)
check("ticker extracted", p["ticker"] == "ACME", p["ticker"])
check("company entity decoded", "&" in p["company"], p["company"])
check("issuer CIK loses leading zeros", p["issuer_cik"] == "999001", p["issuer_cik"])
check("insider CIK loses leading zeros", p["insider_cik"] == "111", p["insider_cik"])
check("aggregates both tranches", p["quantity"] == 15_000, f"got {p['quantity']}")
check("share-weighted average price",
      abs(p["price"] - (10_000 * 52.50 + 5_000 * 53.00) / 15_000) < 0.001,
      f"got {p['price']}")
check("total value", abs(p["total_value"] - 790_000.0) < 0.01, f"got {p['total_value']}")
check("post_qty is the max across rows", p["post_qty"] == 115_000, f"got {p['post_qty']}")
check("title from the relationship block",
      p["title"] == "Chief Executive Officer", p["title"])
check("filing URL uses the issuer CIK",
      p["sec_url"] == "https://www.sec.gov/Archives/edgar/data/999001/"
                      "000123456726000001/0001234567-26-000001-index.htm",
      p["sec_url"])
check("n_transactions", p["n_transactions"] == 2)
check("filed timestamp carried through", p["filing_date"] == "2026-08-10T18:30")

check("grant (code A) rejected", parse(GRANT) is None)
check("sale (code S) rejected", parse(SALE) is None)
check("10b5-1 detected via aff10b5One", parse(PLAN_10B5)["is_10b5"] is True)
check("10b5-1 detected via footnote on older filings",
      parse(OLD_10B5)["is_10b5"] is True)
check("normal purchase not flagged as 10b5-1", p["is_10b5"] is False)
check("malformed XML returns None instead of raising",
      parse("<ownershipDocument><broken") is None)
check("empty document returns None", parse("<ownershipDocument/>") is None)

combo = parse(form4("MIX", "Mixed Co", "9", "9", "Mixed Owner",
                    [txn_xml(1_000, 10.0, 5_000),
                     txn_xml(500, 12.0, 5_000, code="S", ad="D")],
                    officer_title="CFO", director=True, ten_pct=True))
check("only the purchase row counts in a mixed filing", combo["quantity"] == 1_000)
check("all three roles in the title",
      combo["title"] == "CFO, Director, 10% Owner", combo["title"])


# ── 2. Filters ────────────────────────────────────────────────────
print("\n[2] Filters")

check("large purchase passes", bot.passes_filters(p)[0])
ok, why = bot.passes_filters(parse(TINY))
check("value below minimum blocked", not ok, why)
ok, why = bot.passes_filters(parse(NO_TICKER))
check("missing ticker blocked", not ok, why)

check("direct ownership detected", p["is_direct"] is True)
check("indirect ownership detected", parse(INDIRECT)["is_direct"] is False)

bot.MIN_PRICE = 1.0
ok, why = bot.passes_filters(parse(PENNY))
check("penny stock blocked by MIN_PRICE", not ok, why)
bot.MIN_PRICE = 0.0
check("MIN_PRICE=0 disables the check", bot.passes_filters(parse(PENNY))[0])
bot.MIN_PRICE = 1.0

bot.EXCLUDE_10B5 = True
ok, why = bot.passes_filters(parse(PLAN_10B5))
check("EXCLUDE_10B5 drops plan purchases", not ok, why)
bot.EXCLUDE_10B5 = False
check("10b5-1 allowed when the flag is off", bot.passes_filters(parse(PLAN_10B5))[0])

bot.REQUIRE_DIRECT = True
ok, why = bot.passes_filters(parse(INDIRECT))
check("REQUIRE_DIRECT drops trust holdings", not ok, why)
bot.REQUIRE_DIRECT = False

bot.MIN_POSITION_INCREASE = 25.0
ok, why = bot.passes_filters(parse(DIRECTOR_SMALL))   # exactly +20%
check("MIN_POSITION_INCREASE blocks a small top-up", not ok, why)
check("first purchase is never blocked by it",
      bot.passes_filters(parse(FIRST_BUY))[0])
bot.MIN_POSITION_INCREASE = 0.0

bot.ONLY_TICKERS = {"NVDA"}
ok, why = bot.passes_filters(p)
check("watchlist blocks tickers outside it", not ok, why)
bot.ONLY_TICKERS = {"ACME"}
check("watchlist lets its own tickers through", bot.passes_filters(p)[0])
bot.ONLY_TICKERS = set()

bot.EXCLUDE_TICKERS = {"ACME"}
ok, why = bot.passes_filters(p)
check("exclusion list blocks a ticker", not ok, why)
bot.EXCLUDE_TICKERS = set()

check("pct_increase computed during parsing",
      parse(DIRECTOR_SMALL)["pct_increase"] == 20.0)
check("pct_increase is None on a first purchase",
      parse(FIRST_BUY)["pct_increase"] is None)

# Market-cap filters
big = dict(p, market={"market_cap": 3e12, "price": 200, "low_52w": 150, "high_52w": 260})
small = dict(p, market={"market_cap": 80e6, "price": 5, "low_52w": 4.8, "high_52w": 12})

bot.MAX_MARKET_CAP = 2e9
ok, why = bot.passes_market_filters(big)
check("mega cap blocked by MAX_MARKET_CAP", not ok, why)
check("small cap passes", bot.passes_market_filters(small)[0])
bot.MAX_MARKET_CAP = 0

bot.MIN_MARKET_CAP = 300e6
check("micro cap blocked by MIN_MARKET_CAP", not bot.passes_market_filters(small)[0])
bot.MIN_MARKET_CAP = 0

check("missing market data never drops a filing",
      bot.passes_market_filters(dict(p, market=None))[0])


# ── 3. Daily index parsing ────────────────────────────────────────
print("\n[3] EDGAR daily index parsing")

INDEX = """Description:           Daily Index of EDGAR Dissemination Feed
Form Type   Company Name                     CIK        Date Filed  File Name
---------------------------------------------------------------------------------
3           SOME HOLDER INC                  1111       2026-09-01  edgar/data/1111/0001111-26-000001.txt
4           ACME & SONS INC                  999001     2026-09-01  edgar/data/999001/0000999001-26-000042.txt
4           PROCEPT BIOROBOTICS CORP         1930183    2026-09-01  edgar/data/1930183/0001930183-26-000123.txt
4/A         AMENDED CORP                     2222       2026-09-01  edgar/data/2222/0002222-26-000009.txt
8-K         UNRELATED CO                     3333       2026-09-01  edgar/data/3333/0003333-26-000004.txt
"""

entries = bot.parse_daily_index(INDEX)
check("only Form 4 rows kept", len(entries) == 2, f"got {len(entries)}")
check("amendments excluded by default",
      all("/A" not in e["accession"] for e in entries))
check("accession extracted from the path",
      entries[0]["accession"] == "0000999001-26-000042", entries[0]["accession"])
check("absolute URL built",
      entries[0]["url"] == "https://www.sec.gov/Archives/edgar/data/999001/"
                           "0000999001-26-000042.txt", entries[0]["url"])
check("filing date captured", entries[0]["filed_date"] == "2026-09-01")
check("company names with spaces do not break parsing",
      entries[1]["accession"] == "0001930183-26-000123", entries[1]["accession"])

bot.INCLUDE_AMENDMENTS = True
check("amendments included when enabled", len(bot.parse_daily_index(INDEX)) == 3)
bot.INCLUDE_AMENDMENTS = False

check("empty index yields nothing", bot.parse_daily_index("") == [])
check("header-only index yields nothing",
      bot.parse_daily_index("Form Type  Company\n-----\n") == [])

url = bot.daily_index_url(_dt.date(2026, 9, 1))
check("index URL uses the right quarter", "QTR3" in url and "form.20260901.idx" in url, url)
check("Q1 date maps to QTR1", "QTR1" in bot.daily_index_url(_dt.date(2026, 2, 5)))

days = bot.days_in_window(60)
check("short window yields at most one day", len(days) <= 1, str(days))
check("weekends excluded from the window",
      all(d.weekday() < 5 for d in bot.days_in_window(10_000)))


# ── 4. Submission extraction ──────────────────────────────────────
print("\n[4] Submission text extraction")

SUBMISSION = f"""<SEC-DOCUMENT>0001234567-26-000001.txt : 20260810
<SEC-HEADER>0001234567-26-000001.hdr.sgml : 20260810
<ACCEPTANCE-DATETIME>20260810183045
ACCESSION NUMBER:  0001234567-26-000001
</SEC-HEADER>
<DOCUMENT>
<TYPE>4
<XML>
{CEO_BIG}
</XML>
</DOCUMENT>
</SEC-DOCUMENT>"""

captured = {}
bot._edgar_get = lambda u: (captured.__setitem__("url", u) or SUBMISSION)
result = bot.fetch_and_parse({"url": "https://example/x.txt",
                              "accession": "0001234567-26-000001",
                              "filed_date": "2026-08-10"})
check("ownership document found inside the submission", result is not None)
check("acceptance datetime preferred over the index date",
      result["filing_date"] == "2026-08-10T18:30", result["filing_date"])
check("aggregated value survives extraction", result["total_value"] == 790_000.0)

bot._edgar_get = lambda u: "<SEC-DOCUMENT>no xml here</SEC-DOCUMENT>"
check("submission without ownership XML returns None",
      bot.fetch_and_parse({"url": "u", "accession": "a", "filed_date": "d"}) is None)

bot._edgar_get = lambda u: None
check("404 on a submission returns None",
      bot.fetch_and_parse({"url": "u", "accession": "a", "filed_date": "d"}) is None)


# ── 5. Deduplication of downloads ─────────────────────────────────
print("\n[5] Download deduplication")

ddb = bot.init_db(os.path.join(tempfile.mkdtemp(), "d.db"))

check("unknown accession is not processed", not bot.is_processed(ddb, "x-1"))
bot.mark_processed(ddb, "x-1", "2026-09-01", was_purchase=False)
ddb.commit()
check("marked accession is processed", bot.is_processed(ddb, "x-1"))
check("non-purchases are remembered too, so they are never re-downloaded",
      bot.is_processed(ddb, "x-1"))

index_calls = []


def fake_get(url):
    index_calls.append(url)
    return INDEX


bot._edgar_get = fake_get
today = _dt.datetime.now(bot.NY_TZ).date()
yesterday = today - _dt.timedelta(days=1)

new = list(bot.iter_new_filings(ddb, [today]))
check("all filings new on the first pass", len(new) == 2, f"got {len(new)}")

for e in new:
    bot.mark_processed(ddb, e["accession"], e["filed_date"], False)
ddb.commit()
check("nothing new on the second pass", list(bot.iter_new_filings(ddb, [today])) == [])

index_calls.clear()
bot.set_meta(ddb, f"index_done_{yesterday:%Y-%m-%d}", "1")
list(bot.iter_new_filings(ddb, [yesterday]))
check("completed past days are never re-fetched", index_calls == [], str(index_calls))

index_calls.clear()
list(bot.iter_new_filings(ddb, [today]))
check("today's index is always re-fetched", len(index_calls) == 1)

bot._edgar_get = lambda u: None
check("a missing index (holiday) is not an error",
      list(bot.iter_new_filings(ddb, [today])) == [])
ddb.close()


# ── 6. HTML escaping and buttons ──────────────────────────────────
print("\n[6] HTML escaping and buttons")

msg = bot.build_message(p, 14, ["+4 CEO/CFO", "+3 value >= $500k"])
text = msg["text"]
check("& escaped", "&amp;" in text)
check("parse_mode is HTML", msg["parse_mode"] == "HTML")
check("reply_markup is a dict (sent via json=)", isinstance(msg["reply_markup"], dict))
check("MAX ALERT notifies", msg["disable_notification"] is False)
check("low score stays silent",
      bot.build_message(p, 1, [])["disable_notification"] is True)

bot.TELEGRAM_TOPIC_ID = "3"
check("topic injected as int", bot.with_topic(msg).get("message_thread_id") == 3)
check("original payload untouched", "message_thread_id" not in msg)
bot.TELEGRAM_TOPIC_ID = "abc"
check("invalid topic ignored", "message_thread_id" not in bot.with_topic(msg))
bot.TELEGRAM_TOPIC_ID = ""
check("no topic injects nothing", "message_thread_id" not in bot.with_topic(msg))

flat = [b for r in bot.build_buttons(p) for b in r]
urls = " ".join(b["url"] for b in flat)
check("five buttons total", len(flat) == 5, f"got {len(flat)}")
check("TradingView present", "tradingview.com/symbols/ACME/" in urls)
check("SEC filing present", "sec.gov/Archives/edgar/data/999001" in urls)
check("Investing.com present", "investing.com/search/?q=ACME" in urls)
check("Finviz present", "finviz.com/quote.ashx?t=ACME" in urls)
check("latest buys feed present", "finviz.com/insidertrading?tc=7" in urls)
check("no label exceeds 64 chars (Telegram limit)",
      all(len(b["text"]) <= 64 for b in flat))

durls = " ".join(b["url"] for r in bot.build_buttons(dict(p, ticker="BRK.B")) for b in r)
check("dotted ticker URL-encoded", "q=BRK%2EB" in durls or "q=BRK.B" in durls, durls)


# ── 7. Scoring and cluster detection ──────────────────────────────
print("\n[7] Scoring and cluster detection")

conn = bot.init_db(os.path.join(tempfile.mkdtemp(), "t.db"))

_real = _dt.datetime


class FakeDT(_real):
    @classmethod
    def now(cls, tz=None):
        return _real(2026, 8, 10, 20, 0, tzinfo=_dt.timezone.utc) if tz else _real(2026, 8, 10, 20, 0)


bot.datetime = FakeDT

ceo = parse(CEO_BIG)
score1, why1 = bot.calculate_score(ceo, conn)
check("CEO + $790k direct, no cluster = 8", score1 == 8, f"got {score1}: {why1}")
check("no cluster on the first purchase", ceo["cluster"] == 0)
bot.record_transaction(conn, ceo, score1, alerted=True)

director = parse(DIRECTOR_SMALL, accession="0001234567-26-000002")
score2, why2 = bot.calculate_score(director, conn)
check("second insider detects the cluster", director["cluster"] == 1)
check("director + position + cluster + direct = 6", score2 == 6, f"got {score2}: {why2}")
bot.record_transaction(conn, director, score2, alerted=True)

repeat = parse(form4("ACME", "Acme", "999001", "111", "Silva Joao",
                     [txn_xml(2_000, 54.0, 117_000)], officer_title="CEO"),
               accession="0001234567-26-000099")
bot.calculate_score(repeat, conn)
check("cluster counts distinct insiders, not filings", repeat["cluster"] == 1,
      f"got {repeat['cluster']}")

tiny = parse(TINY, accession="0001234567-26-000003")
bot.record_transaction(conn, tiny, 0, alerted=False)
other_micro = parse(form4("MICRO", "Micro Corp", "999002", "888", "Another Insider",
                          [txn_xml(10_000, 6.0, 60_000)], officer_title="CEO"),
                    accession="0001234567-26-000100")
bot.calculate_score(other_micro, conn)
check("filtered transactions still feed the cluster", other_micro["cluster"] == 1)

old = parse(form4("OLDC", "Old Co", "999007", "901", "Long Ago",
                  [txn_xml(5_000, 10.0, 50_000, date="2026-07-01")], director=True),
            accession="0001234567-26-000101")
bot.record_transaction(conn, old, 1, alerted=False)
recent = parse(form4("OLDC", "Old Co", "999007", "902", "Just Now",
                     [txn_xml(5_000, 11.0, 50_000, date="2026-08-09")], director=True),
               accession="0001234567-26-000102")
bot.calculate_score(recent, conn)
check("a purchase 40 days ago is outside the 7d window", recent["cluster"] == 0)

# 10b5-1 penalty
plan = parse(PLAN_10B5, accession="0001234567-26-000006")
bot.SCORE_PENALTY_10B5 = 0
s_off, w_off = bot.calculate_score(plan, conn)
check("default applies no penalty", "10b5-1" not in " ".join(w_off))
bot.SCORE_PENALTY_10B5 = 2
s_on, w_on = bot.calculate_score(plan, conn)
check("penalty applied", s_on == s_off - 2, f"{s_off} -> {s_on}")
bot.SCORE_PENALTY_10B5 = 99
check("score never goes negative", bot.calculate_score(plan, conn)[0] == 0)
bot.SCORE_PENALTY_10B5 = 0

# Market-data scoring
no_market = dict(ceo, market=None)
s_none, _ = bot.calculate_score(no_market, conn)
micro = dict(ceo, market={"market_cap": 50e6, "price": 10, "low_52w": 9.5, "high_52w": 40})
s_micro, w_micro = bot.calculate_score(micro, conn)
check("large stake in a micro cap scores higher", s_micro > s_none, f"{s_none} -> {s_micro}")
check("percent of market cap recorded",
      abs(micro["pct_of_market_cap"] - 1.58) < 0.01, str(micro.get("pct_of_market_cap")))
mega = dict(ceo, market={"market_cap": 3e12, "price": 200, "low_52w": 100, "high_52w": 260})
_, w_mega = bot.calculate_score(mega, conn)
check("same purchase in a mega cap earns no size points",
      not any("market cap" in x for x in w_mega))
at_low = dict(ceo, market={"market_cap": 1e9, "price": 10.4, "low_52w": 10.0, "high_52w": 30})
_, w_low = bot.calculate_score(at_low, conn)
check("buying near the 52w low scores", any("52w low" in x for x in w_low))
at_high = dict(ceo, market={"market_cap": 1e9, "price": 29.0, "low_52w": 10.0, "high_52w": 30})
_, w_high = bot.calculate_score(at_high, conn)
check("buying near the highs scores nothing there",
      not any("52w low" in x for x in w_high))

bot.datetime = _real

# Market-data cache
bot.ENABLE_MARKET_DATA = True
calls = {"n": 0}
bot._fetch_market_data = lambda t: (calls.__setitem__("n", calls["n"] + 1) or
                                    {"market_cap": 1e9, "price": 10.0,
                                     "low_52w": 8.0, "high_52w": 15.0, "cached": False})
first = bot.get_market_data(conn, "TEST")
second = bot.get_market_data(conn, "TEST")
check("market data fetched once", calls["n"] == 1, f"got {calls['n']} calls")
check("second read comes from cache", second.get("cached") is True)
bot._fetch_market_data = lambda t: None
check("failed lookup returns None", bot.get_market_data(conn, "NOPE") is None)
bot.ENABLE_MARKET_DATA = False
check("disabled market data returns None", bot.get_market_data(conn, "TEST2") is None)
bot.ENABLE_MARKET_DATA = True

check("already_seen catches a duplicate",
      bot.record_alert(conn, ceo, score1) or bot.already_seen(conn, ceo["accession_number"]))
check("already_seen ignores a new one", not bot.already_seen(conn, "0000-00-000000"))
conn.close()


# ── 8. Adaptive lookback ──────────────────────────────────────────
print("\n[8] Adaptive lookback")

lb = bot.init_db(os.path.join(tempfile.mkdtemp(), "lb.db"))

mins, why = bot.compute_lookback(lb)
check("cold start uses the default",
      mins == bot.LOOKBACK_MINUTES and "first run" in why, f"{mins} / {why}")
check("--lookback takes precedence", bot.compute_lookback(lb, override=240)[0] == 240)

now = _dt.datetime.now(_dt.timezone.utc)
bot.set_meta(lb, bot.LAST_RUN_KEY, (now - _dt.timedelta(minutes=40)).isoformat())
mins, why = bot.compute_lookback(lb)
check("covers the gap plus margin",
      40 + bot.LOOKBACK_BUFFER_MINUTES - 1 <= mins <= 40 + bot.LOOKBACK_BUFFER_MINUTES + 1,
      f"{mins} / {why}")

bot.set_meta(lb, bot.LAST_RUN_KEY, (now - _dt.timedelta(hours=60)).isoformat())
check("weekend gap covered", bot.compute_lookback(lb)[0] > 3000)

bot.set_meta(lb, bot.LAST_RUN_KEY, (now - _dt.timedelta(days=10)).isoformat())
mins, why = bot.compute_lookback(lb)
check("truncates at the ceiling",
      mins == bot.MAX_LOOKBACK_MINUTES and "truncated" in why, f"{mins} / {why}")

bot.set_meta(lb, bot.LAST_RUN_KEY, "this-is-not-a-date")
check("unreadable state falls back to the default",
      bot.compute_lookback(lb)[0] == bot.LOOKBACK_MINUTES)

bot.set_meta(lb, bot.LAST_RUN_KEY, (now + _dt.timedelta(hours=2)).isoformat())
check("clock inconsistency falls back to the default",
      bot.compute_lookback(lb)[0] == bot.LOOKBACK_MINUTES)
lb.close()


# ── 9. Status messages ────────────────────────────────────────────
print("\n[9] Status messages")

sent = []
bot.send_telegram = lambda p, dry_run=False: sent.append(p) or True

bot.STATUS_TOPIC_ID = "7"
bot.send_status("test")
check("status is silent", sent[0]["disable_notification"] is True)
check("status goes to its own topic", sent[0]["message_thread_id"] == 7)

bot.STATUS_TOPIC_ID = ""
bot.TELEGRAM_TOPIC_ID = "3"
sent.clear()
bot.send_status("test")
check("without a status topic it uses the alert topic",
      "message_thread_id" not in sent[0])
bot.TELEGRAM_TOPIC_ID = ""

check("duration in seconds", bot._fmt_duration(42) == "42s")
check("duration in minutes", bot._fmt_duration(150) == "2.5min")


# ── 10. Full cycle with a capped backlog ──────────────────────────
print("\n[10] Full cycle and backlog capping")

cdb = bot.init_db(os.path.join(tempfile.mkdtemp(), "c.db"))
bot.ENABLE_MARKET_DATA = False
bot.STATUS_MESSAGES = "off"

big_index = ["Form Type   Company   CIK   Date Filed  File Name",
             "-" * 60]
for i in range(10):
    big_index.append(
        f"4           CO {i}      99{i}    {today:%Y-%m-%d}  "
        f"edgar/data/99{i}/000099{i}-26-00000{i}.txt")


def cycle_get(url):
    if "daily-index" in url:
        return "\n".join(big_index)
    return SUBMISSION


bot._edgar_get = cycle_get
bot.MAX_FILINGS_PER_RUN = 4
stats = bot.process_cycle(cdb, lookback_minutes=60, dry_run=True)
check("run stops at MAX_FILINGS_PER_RUN", stats["downloaded"] == 4,
      f"got {stats['downloaded']}")
check("capped run is flagged", stats["capped"] is True)
check("capped run does not mark itself successful",
      bot.get_meta(cdb, bot.LAST_RUN_KEY) is None)

bot.MAX_FILINGS_PER_RUN = 400
stats2 = bot.process_cycle(cdb, lookback_minutes=60, dry_run=True)
check("the next run continues the backlog", stats2["downloaded"] == 6,
      f"got {stats2['downloaded']}")
check("completed run records success", bot.get_meta(cdb, bot.LAST_RUN_KEY) is not None)

stats3 = bot.process_cycle(cdb, lookback_minutes=60, dry_run=True)
check("a third run downloads nothing new", stats3["downloaded"] == 0)


def failing_get(url):
    raise bot.RateLimitError("EDGAR rate limit (429)")


bot._edgar_get = failing_get
stats4 = bot.process_cycle(cdb, lookback_minutes=60, dry_run=True)
check("a failed cycle reports the error", stats4["error"] is not None)
cdb.close()


# ── Result ────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} test(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("All tests passed.")
