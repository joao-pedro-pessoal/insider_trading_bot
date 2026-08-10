#!/usr/bin/env python3
"""
Testes do pipeline com fixtures. Nao toca na rede nem no Telegram.

    python test_bot.py
"""

import os
import sqlite3
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


# ── Fixtures no formato real da sec-api.io ─────────────────────────

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
    [row(10_000, 52.50, 110_000), row(5_000, 53.00, 115_000)],  # 3 tranches -> agrega
)

DIRECTOR_SMALL = filing(
    "0001234567-26-000002", "ACME", "Acme & Sons, Inc.", "999001",
    "222", "Costa Maria", {"isDirector": True},
    [row(1_000, 52.00, 6_000)],
)

TINY = filing(
    "0001234567-26-000003", "MICRO", "Micro Corp", "999002",
    "333", "Pequeno Pedro", {"isDirector": True},
    [row(10, 5.00, 100)],  # $50 -> abaixo do minimo
)

GRANT_DISGUISED = filing(
    "0001234567-26-000004", "GRNT", "Grant Co", "999003",
    "444", "Zero Preco", {"isOfficer": True, "officerTitle": "CFO"},
    [row(100_000, 0.0, 500_000, code="A")],  # codigo A, nao P
)

SALE_ONLY = filing(
    "0001234567-26-000005", "SELL", "Sell Co", "999004",
    "555", "Vendedor", {"isDirector": True},
    [row(50_000, 20.0, 10_000, code="S", ad="D")],
)

PLAN_10B5 = filing(
    "0001234567-26-000006", "PLAN", "Plan Co", "999005",
    "666", "Automatico", {"isOfficer": True, "officerTitle": "CEO"},
    [row(20_000, 30.0, 200_000)],
    aff10b5=True,
)

NO_TICKER = filing(
    "0001234567-26-000007", "", "Private Co", "999006",
    "777", "Nao Cotado", {"isDirector": True},
    [row(10_000, 10.0, 50_000)],
)


# ── 1. Parsing ────────────────────────────────────────────────────
print("\n[1] Parsing")

p = bot.parse_filing(CEO_BIG)
check("parse devolve resultado", p is not None)
check("agrega as 2 tranches", p["quantity"] == 15_000, f"got {p['quantity']}")
check("preco medio ponderado",
      abs(p["price"] - (10_000 * 52.50 + 5_000 * 53.00) / 15_000) < 0.001,
      f"got {p['price']}")
check("valor total", abs(p["total_value"] - 790_000.0) < 0.01, f"got {p['total_value']}")
check("post_qty = maximo das linhas", p["post_qty"] == 115_000, f"got {p['post_qty']}")
check("titulo extraido", "Chief Executive Officer" in p["title"], p["title"])
check("URL do filing usa o CIK do issuer",
      p["sec_url"] == "https://www.sec.gov/Archives/edgar/data/999001/"
                      "000123456726000001/0001234567-26-000001-index.htm",
      p["sec_url"])
check("n_transactions", p["n_transactions"] == 2)

check("codigo A (grant) rejeitado", bot.parse_filing(GRANT_DISGUISED) is None)
check("venda (codigo S) rejeitada", bot.parse_filing(SALE_ONLY) is None)
check("10b5-1 detectado via aff10b5One", bot.parse_filing(PLAN_10B5)["is_10b5"] is True)
check("compra normal nao marcada como 10b5-1", p["is_10b5"] is False)


# ── 2. Filtros ────────────────────────────────────────────────────
print("\n[2] Filtros")

ok, why = bot.passes_filters(p)
check("compra grande passa", ok, why)

ok, why = bot.passes_filters(bot.parse_filing(TINY))
check("valor abaixo do minimo bloqueado", not ok, why)

ok, why = bot.passes_filters(bot.parse_filing(NO_TICKER))
check("sem ticker bloqueado", not ok, why)


# ── 3. Escaping HTML ──────────────────────────────────────────────
print("\n[3] Escaping HTML (o bug que quebrava alertas na v1.0)")

msg = bot.build_message(p, 6, ["+3 CEO/CFO", "+3 valor >= $500k"])
text = msg["text"]
check("& escapado", "&amp;" in text)
check("< escapado", "&lt;Holdings&gt;" in text)
check("nao ha & solto",
      "& " not in text.replace("&amp;", "").replace("&lt;", "").replace("&gt;", ""))
check("parse_mode HTML", msg["parse_mode"] == "HTML")
check("reply_markup e dict (enviado como json=)", isinstance(msg["reply_markup"], dict))
check("MAX ALERT com notificacao", msg["disable_notification"] is False)

quiet = bot.build_message(p, 1, [])
check("score baixo fica silencioso", quiet["disable_notification"] is True)

# Supergrupo com topicos (forum)
bot.TELEGRAM_TOPIC_ID = "3"
routed = bot.with_topic(msg)
check("topico injectado como int", routed.get("message_thread_id") == 3)
check("payload original intacto", "message_thread_id" not in msg)

bot.TELEGRAM_TOPIC_ID = "abc"
check("topico invalido e ignorado", "message_thread_id" not in bot.with_topic(msg))

bot.TELEGRAM_TOPIC_ID = ""
check("sem topico nao injecta nada", "message_thread_id" not in bot.with_topic(msg))


# ── 3b. Botoes ────────────────────────────────────────────────────
print("\n[3b] Botoes inline")

rows = bot.build_buttons(p)
flat = [b for r in rows for b in r]
urls = " ".join(b["url"] for b in flat)

check("3 linhas de botoes", len(rows) == 3, f"got {len(rows)}")
check("5 botoes no total", len(flat) == 5, f"got {len(flat)}")
check("TradingView presente", "tradingview.com/symbols/ACME/" in urls)
check("SEC filing presente", "sec.gov/Archives/edgar/data/999001" in urls)
check("Investing.com presente", "investing.com/search/?q=ACME" in urls)
check("Finviz do ticker presente", "finviz.com/quote.ashx?t=ACME" in urls)
check("Finviz ultimas compras presente", "finviz.com/insidertrading?tc=7" in urls)
check("todos os botoes tem texto e url",
      all(b.get("text") and b.get("url") for b in flat))
check("nenhum texto excede 64 chars (limite Telegram)",
      all(len(b["text"]) <= 64 for b in flat),
      str([len(b["text"]) for b in flat]))

# Tickers com pontos/hifens (BRK.B, BF-B) tem de ir encoded
dotted = dict(p, ticker="BRK.B")
durls = " ".join(b["url"] for r in bot.build_buttons(dotted) for b in r)
check("ticker com ponto vai URL-encoded", "q=BRK%2EB" in durls or "q=BRK.B" in durls, durls)
check("ponto nao quebra o path do TradingView", "symbols/BRK" in durls)


# ── 3c. Penalizacao 10b5-1 ────────────────────────────────────────
print("\n[3c] Penalizacao opcional de planos 10b5-1")

plan = bot.parse_filing(PLAN_10B5)
tmp_conn = bot.init_db(os.path.join(tempfile.mkdtemp(), "p.db"))

bot.SCORE_PENALTY_10B5 = 0
s_off, w_off = bot.calculate_score(plan, tmp_conn)
check("default nao penaliza", "10b5-1" not in " ".join(w_off), str(w_off))

bot.SCORE_PENALTY_10B5 = 2
s_on, w_on = bot.calculate_score(plan, tmp_conn)
check("penalizacao aplicada", s_on == s_off - 2, f"{s_off} -> {s_on}")
check("penalizacao aparece no breakdown", "-2 plano 10b5-1" in " ".join(w_on))

bot.SCORE_PENALTY_10B5 = 99
s_floor, _ = bot.calculate_score(plan, tmp_conn)
check("score nunca fica negativo", s_floor == 0, f"got {s_floor}")

bot.SCORE_PENALTY_10B5 = 0
tmp_conn.close()


# ── 4. Scoring e cluster ──────────────────────────────────────────
print("\n[4] Scoring e deteccao de cluster")

tmpdir = tempfile.mkdtemp()
db = os.path.join(tmpdir, "t.db")
conn = bot.init_db(db)

# Congela "hoje" para as datas das fixtures caírem dentro da janela
import datetime as _dt
_real = _dt.datetime


class FakeDT(_real):
    @classmethod
    def now(cls, tz=None):
        return _real(2026, 8, 10, 20, 0, tzinfo=_dt.timezone.utc) if tz else _real(2026, 8, 10, 20, 0)


bot.datetime = FakeDT

ceo = bot.parse_filing(CEO_BIG)
score1, why1 = bot.calculate_score(ceo, conn)
check("CEO + $790k sem cluster = 6", score1 == 6, f"got {score1}: {why1}")
check("cluster zero na primeira compra", ceo["cluster"] == 0)
bot.record_transaction(conn, ceo, score1, alerted=True)

director = bot.parse_filing(DIRECTOR_SMALL)
score2, why2 = bot.calculate_score(director, conn)
check("segundo insider detecta cluster", director["cluster"] == 1, f"got {director['cluster']}")
# 1000 acoes sobre 5000 previas = +20% exactos -> +2 (fronteira inclusiva)
check("director(+1) + posicao +20%(+2) + cluster(+3) = 6", score2 == 6, f"got {score2}: {why2}")
check("valor $52k nao ganha pontos", "valor" not in " ".join(why2), str(why2))
bot.record_transaction(conn, director, score2, alerted=True)

# Mesmo insider a repetir NAO deve contar como cluster para si proprio
repeat = bot.parse_filing(filing(
    "0001234567-26-000099", "ACME", "Acme & Sons, Inc.", "999001",
    "111", "Silva Joao", {"isOfficer": True, "officerTitle": "CEO"},
    [row(2_000, 54.0, 117_000)],
))
_, _ = bot.calculate_score(repeat, conn)
check("cluster conta insiders distintos, nao filings",
      repeat["cluster"] == 1, f"got {repeat['cluster']} (esperado 1: Costa Maria)")

# Transacoes filtradas tambem alimentam o cluster
tiny = bot.parse_filing(TINY)
bot.record_transaction(conn, tiny, 0, alerted=False)
other_micro = bot.parse_filing(filing(
    "0001234567-26-000100", "MICRO", "Micro Corp", "999002",
    "888", "Outro Insider", {"isOfficer": True, "officerTitle": "CEO"},
    [row(10_000, 6.0, 60_000)],
))
bot.calculate_score(other_micro, conn)
check("transacao filtrada conta para o cluster (bug da v1.0)",
      other_micro["cluster"] == 1, f"got {other_micro['cluster']}")

# Fora da janela nao deve contar
old = bot.parse_filing(filing(
    "0001234567-26-000101", "OLDC", "Old Co", "999007",
    "901", "Antigo", {"isDirector": True},
    [row(5_000, 10.0, 50_000, date="2026-07-01")],
))
bot.record_transaction(conn, old, 1, alerted=False)
recent = bot.parse_filing(filing(
    "0001234567-26-000102", "OLDC", "Old Co", "999007",
    "902", "Recente", {"isDirector": True},
    [row(5_000, 11.0, 50_000, date="2026-08-09")],
))
bot.calculate_score(recent, conn)
check("compra de ha 40 dias fora da janela de 7d",
      recent["cluster"] == 0, f"got {recent['cluster']}")

check("pct_increase None quando post_qty nao e fiavel",
      bot.parse_filing(TINY).get("post_qty") == 100)

bot.datetime = _real


# ── 5. Dedup e paginacao ──────────────────────────────────────────
print("\n[5] Dedup e filtragem temporal")

bot.record_alert(conn, ceo, score1)
check("already_seen apanha repetido", bot.already_seen(conn, ceo["accession_number"]))
check("already_seen ignora novo", not bot.already_seen(conn, "0000000-00-000000"))

d = bot._parse_dt("2022-08-09T21:23:00-04:00")
check("_parse_dt le offset de NY", d is not None and d.utcoffset().total_seconds() == -14400)
check("_parse_dt tolera lixo", bot._parse_dt("nao-e-data") is None)
check("_parse_dt tolera None", bot._parse_dt(None) is None)

# A paginacao para quando encontra um filing mais antigo que o cutoff
calls = {"n": 0}
pages = [
    {"transactions": [dict(CEO_BIG, filedAt="2026-08-10T19:00:00-04:00", accessionNo=f"a{i}")
                      for i in range(50)]},
    {"transactions": [dict(CEO_BIG, filedAt="2026-08-10T18:55:00-04:00", accessionNo="b0"),
                      dict(CEO_BIG, filedAt="2026-01-01T10:00:00-05:00", accessionNo="antigo")]},
]


def fake_post(payload):
    i = calls["n"]
    calls["n"] += 1
    return pages[i] if i < len(pages) else {"transactions": []}


bot._post_with_retry = fake_post
cutoff = _dt.datetime(2026, 8, 10, 22, 0, tzinfo=_dt.timezone.utc)  # 18:00 ET
result = bot.fetch_recent_purchases(cutoff)
check("paginou e parou no cutoff", len(result) == 51, f"got {len(result)}")
check("nao incluiu o filing antigo",
      all(f["accessionNo"] != "antigo" for f in result))


# ── 6. Lookback adaptativo ────────────────────────────────────────
print("\n[6] Lookback adaptativo")

lb_db = os.path.join(tempfile.mkdtemp(), "lb.db")
lb = bot.init_db(lb_db)

mins, why = bot.compute_lookback(lb)
check("arranque a frio usa o default",
      mins == bot.LOOKBACK_MINUTES and "primeira corrida" in why, f"{mins} / {why}")

mins, why = bot.compute_lookback(lb, override=240)
check("--lookback tem prioridade", mins == 240 and "forcado" in why, f"{mins} / {why}")

# 40 minutos desde a ultima corrida -> 40 + buffer
now = _dt.datetime.now(_dt.timezone.utc)
bot.set_meta(lb, bot.LAST_RUN_KEY, (now - _dt.timedelta(minutes=40)).isoformat())
mins, why = bot.compute_lookback(lb)
check("cobre o intervalo + margem",
      40 + bot.LOOKBACK_BUFFER_MINUTES - 1 <= mins <= 40 + bot.LOOKBACK_BUFFER_MINUTES + 1,
      f"{mins} / {why}")

# Fim de semana: 60h de intervalo
bot.set_meta(lb, bot.LAST_RUN_KEY, (now - _dt.timedelta(hours=60)).isoformat())
mins, why = bot.compute_lookback(lb)
check("intervalo de fim de semana coberto", mins > 3000, f"{mins} / {why}")

# 10 dias parado -> trunca no teto
bot.set_meta(lb, bot.LAST_RUN_KEY, (now - _dt.timedelta(days=10)).isoformat())
mins, why = bot.compute_lookback(lb)
check("trunca no maximo",
      mins == bot.MAX_LOOKBACK_MINUTES and "truncado" in why, f"{mins} / {why}")

# Estado corrompido nao deve rebentar
bot.set_meta(lb, bot.LAST_RUN_KEY, "isto-nao-e-uma-data")
mins, why = bot.compute_lookback(lb)
check("estado ilegivel cai no default",
      mins == bot.LOOKBACK_MINUTES and "ilegivel" in why, f"{mins} / {why}")

# Relogio para tras (last_run no futuro)
bot.set_meta(lb, bot.LAST_RUN_KEY, (now + _dt.timedelta(hours=2)).isoformat())
mins, why = bot.compute_lookback(lb)
check("relogio inconsistente cai no default",
      mins == bot.LOOKBACK_MINUTES, f"{mins} / {why}")

# Paginas escalam com a janela, mas com teto
check("janela curta usa paginas base", bot.pages_for(90) == bot.MAX_PAGES)
check("janela longa pede mais paginas", bot.pages_for(3000) > bot.MAX_PAGES)
check("paginas tem teto", bot.pages_for(100000) == bot.MAX_PAGES_CATCHUP)

check("meta le e escreve", bot.get_meta(lb, "inexistente") is None)
lb.close()


# ── 7. Mensagens de estado ────────────────────────────────────────
print("\n[7] Mensagens de estado")

sent = []
bot.send_telegram = lambda p, dry_run=False: sent.append(p) or True

bot.STATUS_MESSAGES = "always"
bot.STATUS_TOPIC_ID = "7"
bot.send_status("teste")
check("estado enviado", len(sent) == 1)
check("estado e silencioso", sent[0]["disable_notification"] is True)
check("estado vai para o topico proprio", sent[0]["message_thread_id"] == 7)

bot.STATUS_TOPIC_ID = ""
bot.TELEGRAM_TOPIC_ID = "3"
sent.clear()
bot.send_status("teste")
check("sem topico de estado usa o dos alertas",
      "message_thread_id" not in sent[0], str(sent[0].get("message_thread_id")))

check("duracao formatada em segundos", bot._fmt_duration(42) == "42s")
check("duracao formatada em minutos", bot._fmt_duration(150) == "2.5min")

bot.STATUS_MESSAGES = "off"
bot.TELEGRAM_TOPIC_ID = ""

conn.close()


# ── Resultado ─────────────────────────────────────────────────────
print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} teste(s) falharam: {', '.join(FAILURES)}")
    sys.exit(1)
print("Todos os testes passaram.")
