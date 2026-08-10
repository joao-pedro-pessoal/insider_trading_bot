#!/usr/bin/env python3
"""
SEC INSIDER TRADING ALERT BOT v1.1
==================================

Alerta no Telegram sobre compras de mercado aberto (Form 4, codigo P) reportadas
por insiders na SEC.

Mudancas face a v1.0:
  * Corre UM ciclo por invocacao (--once, default) para cron / GitHub Actions.
    O modo --loop continua disponivel para VPS.
  * Field paths da SEC-API corrigidos (issuer.*, coding.code, amounts.*).
  * Query enviada como string Lucene (a API nao aceita query_string aninhado).
  * Filtragem temporal feita localmente com datetimes timezone-aware, com
    paginacao -- acaba a classe de bugs de UTC vs horario de Nova Iorque.
  * Escaping HTML correcto (& < > deixam de quebrar mensagens).
  * URL do filing construido a partir do accession number (o antigo usava o
    ticker como CIK e nunca funcionava).
  * Agrega TODAS as transacoes P do mesmo filing (antes lia so a primeira).
  * 10b5-1 detectado via campo oficial aff10b5One, nao por string matching.
  * Cluster conta insiders DISTINTOS e le de todas as transacoes vistas, nao
    so das que geraram alerta.
  * Segredos vem do ambiente. Nada hardcoded.

NAO e conselho financeiro. Ver README.md.
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
from typing import Any, Iterable, Optional

import requests

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

NY_TZ = ZoneInfo("America/New_York")


# ══════════════════════════════════════════════════════════════════
#  CONFIG  (tudo via variaveis de ambiente)
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

SEC_API_ENDPOINT = os.environ.get("SEC_API_ENDPOINT", "https://api.sec-api.io/insider-trading")

DB_PATH = os.environ.get("DB_PATH", "state/alerts.db")

MIN_TRANSACTION_VALUE_USD = _env_int("MIN_TRANSACTION_VALUE_USD", 25_000)
CLUSTER_WINDOW_DAYS       = _env_int("CLUSTER_WINDOW_DAYS", 7)
SCORE_MIN_TO_SEND         = _env_int("SCORE_MIN_TO_SEND", 1)   # abaixo disto nem envia
SCORE_SILENT_BELOW        = _env_int("SCORE_SILENT_BELOW", 3)  # envia sem notificacao
SCORE_MAX_ALERT_FROM      = _env_int("SCORE_MAX_ALERT_FROM", 6)

# Janela de lookback. Dedup por accession torna a sobreposicao inofensiva,
# por isso vale a pena ser generoso: uma corrida falhada nao perde filings.
LOOKBACK_MINUTES = _env_int("LOOKBACK_MINUTES", 90)

PAGE_SIZE      = 50           # maximo da SEC-API
MAX_PAGES      = _env_int("MAX_PAGES", 6)
HTTP_TIMEOUT   = 25
MAX_RETRIES    = 3

logging.basicConfig(
    level=logging.DEBUG if _env_bool("VERBOSE") else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("InsiderBot")


class RateLimitError(Exception):
    """SEC-API devolveu 429."""


# ══════════════════════════════════════════════════════════════════
#  BASE DE DADOS
# ══════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_alerts (
    accession_number TEXT PRIMARY KEY,
    ticker           TEXT NOT NULL,
    insider_name     TEXT,
    score            INTEGER,
    sent_at          TEXT NOT NULL
);

-- Log de TUDO o que foi parseado, mesmo o que nao gerou alerta.
-- E daqui que sai a deteccao de cluster: filtrar antes de gravar cegava-a.
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
"""


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    log.info("DB pronta: %s", path)
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


# ══════════════════════════════════════════════════════════════════
#  INGESTAO
# ══════════════════════════════════════════════════════════════════

# Lucene: Form 4 com pelo menos uma compra de mercado aberto (codigo P).
# O filtro temporal e feito localmente -- ver docstring do modulo.
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
                raise RuntimeError(f"SEC-API auth falhou ({resp.status_code}) - verifica SEC_API_KEY")
            resp.raise_for_status()
            return resp.json()
        except (RateLimitError, RuntimeError):
            raise
        except Exception as exc:  # rede, timeout, 5xx
            last_error = exc
            wait = 2 ** attempt
            log.warning("SEC-API tentativa %d/%d falhou (%s) - retry em %ds",
                        attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"SEC-API indisponivel apos {MAX_RETRIES} tentativas: {last_error}")


def fetch_recent_purchases(cutoff: datetime, max_pages: int = MAX_PAGES) -> list[dict]:
    """
    Devolve filings Form 4 com compras, com filedAt >= cutoff.

    Pagina por filedAt descendente e para assim que encontra um filing mais
    antigo que o cutoff. Sem aritmetica de fusos horarios na query -- a
    comparacao e feita com datetimes aware em Python, que e o unico sitio
    onde isso e fiavel.
    """
    if cutoff.tzinfo is None:
        raise ValueError("cutoff tem de ser timezone-aware")

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
                collected.append(filing)  # sem data: deixa passar, dedup protege
                continue
            if filed_at < cutoff:
                stop = True
                break
            collected.append(filing)

        log.debug("pagina %d: %d filings, %d acumulados", page, len(batch), len(collected))
        if stop or len(batch) < PAGE_SIZE:
            break
    else:
        log.warning("MAX_PAGES (%d) atingido - podem faltar filings. "
                    "Reduz LOOKBACK_MINUTES ou aumenta MAX_PAGES.", max_pages)

    return collected


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse ISO 8601 da SEC-API ('2022-08-09T21:23:00-04:00') -> aware datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY_TZ)  # SEC reporta em horario de NY
    return dt


# ══════════════════════════════════════════════════════════════════
#  PARSING
# ══════════════════════════════════════════════════════════════════

def parse_filing(raw: dict) -> Optional[dict]:
    """
    Normaliza um filing da SEC-API numa transacao agregada.

    Agrega TODAS as linhas com codigo P + acquiredDisposedCode A do mesmo
    filing: um insider que compra em 3 tranches aparecia subvalorizado quando
    se lia apenas a primeira linha.
    """
    try:
        return _parse(raw)
    except Exception as exc:
        log.debug("parse falhou (%s): %s", exc, str(raw)[:160])
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
    insider_name = owner.get("name") or "Desconhecido"
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

    if total_shares <= 0:
        return None

    avg_price = total_cost / total_shares

    # Campo oficial das alteracoes de 2023 a Rule 10b5-1. Muito mais fiavel
    # do que procurar "10b5-1" no texto das footnotes.
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
        "total_value":      round(total_cost, 2),
        "n_transactions":   len(purchases),
        "is_10b5":          is_10b5,
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
    """URL real do filing no EDGAR. A v1.0 usava o ticker como CIK -> 404."""
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
#  FILTROS
# ══════════════════════════════════════════════════════════════════

def passes_filters(txn: dict) -> tuple[bool, str]:
    if not txn.get("ticker"):
        return False, "sem ticker (provavelmente nao cotada)"
    if txn["price"] <= 0:
        return False, "preco zero (grant disfarcado de compra)"
    if txn["quantity"] <= 0:
        return False, "quantidade zero"
    if txn["total_value"] < MIN_TRANSACTION_VALUE_USD:
        return False, f"valor ${txn['total_value']:,.0f} < minimo"
    return True, "ok"


# ══════════════════════════════════════════════════════════════════
#  SCORING
# ══════════════════════════════════════════════════════════════════

def calculate_score(txn: dict, conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """
    Score ponderado. Devolve (score, breakdown) -- o breakdown existe para
    poderes auditar porque e que um alerta apareceu, e para a Fase 3 poder
    testar cada componente isoladamente.

    Nota honesta: estes pesos sao heuristicas, nao resultado de backtest.
    Nao trates o score como probabilidade de nada.
    """
    score = 0
    why: list[str] = []

    title_up = (txn.get("title") or "").upper()
    if any(k in title_up for k in ("CEO", "CHIEF EXECUTIVE", "CFO", "CHIEF FINANCIAL")):
        score += 3
        why.append("+3 CEO/CFO")
    elif any(k in title_up for k in ("PRESIDENT", "VP", "VICE PRESIDENT", "DIRECTOR", "OFFICER")):
        score += 1
        why.append("+1 director/officer")

    value = txn["total_value"]
    if value >= 500_000:
        score += 3
        why.append("+3 valor >= $500k")
    elif value >= 100_000:
        score += 1
        why.append("+1 valor >= $100k")

    post = txn.get("post_qty", 0)
    qty  = txn["quantity"]
    pre  = post - qty
    if pre > 0:
        pct = (qty / pre) * 100
        txn["pct_increase"] = round(pct, 1)
        if pct >= 20:
            score += 2
            why.append(f"+2 posicao +{pct:.0f}%")
    else:
        # post_qty <= qty: primeira compra, ou o filing nao reportou o total.
        txn["pct_increase"] = None

    cluster = count_cluster_insiders(conn, txn)
    txn["cluster"] = cluster
    if cluster > 0:
        score += 3
        why.append(f"+3 cluster ({cluster} insider(s))")

    return score, why


def count_cluster_insiders(conn: sqlite3.Connection, txn: dict) -> int:
    """
    Numero de insiders DISTINTOS (excluindo este) que compraram o mesmo ticker
    na janela de cluster.

    Correcoes face a v1.0: compara datas com datas (nao data vs timestamp ISO),
    conta pessoas distintas em vez de filings, e le de todas as transacoes
    registadas em vez de so das que geraram alerta.
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
    """Escapa & < > para parse_mode=HTML. A v1.0 tinha um escaper de
    MarkdownV2 que nunca era chamado -- qualquer 'Procter & Gamble' dava 400."""
    return html.escape(str(value), quote=False)


def build_message(txn: dict, score: int, why: list[str]) -> dict:
    ticker = txn["ticker"]

    if score >= SCORE_MAX_ALERT_FROM:
        emoji, label, silent = "\U0001F6A8", "MAX ALERT", False
    elif score >= SCORE_SILENT_BELOW:
        emoji, label, silent = "\U0001F534", "SINAL FORTE", False
    else:
        emoji, label, silent = "\U0001F7E1", "SINAL FRACO", True

    trade_type = ("⚠️ <b>Plano automatico (10b5-1)</b>"
                  if txn.get("is_10b5")
                  else "✅ <b>Compra discricionaria</b>")

    lines = [
        f"{emoji} <b>{label}</b>  |  Score: <b>{score}</b>",
        f"\U0001F3E2 <b>{esc(txn.get('company', ticker))}</b> (<code>{esc(ticker)}</code>)",
        "",
        f"\U0001F464 <b>{esc(txn.get('insider_name'))}</b>",
        f"\U0001F4BC {esc(txn.get('title'))}",
        "",
        f"\U0001F4B5 Valor total: <code>${txn['total_value']:,.0f}</code>",
        f"\U0001F4C8 Preco medio: <code>${txn['price']:,.2f}</code>",
        f"\U0001F4E6 Acoes: <code>{txn['quantity']:,}</code>",
    ]

    if txn.get("pct_increase") is not None:
        lines.append(f"\U0001F4CA Posicao: <code>+{txn['pct_increase']:.1f}%</code>")
    if txn.get("n_transactions", 1) > 1:
        lines.append(f"\U0001F9FE Agregado de {txn['n_transactions']} transacoes")

    lines += ["", trade_type]

    if txn.get("cluster", 0) > 0:
        lines.append(
            f"\U0001F501 <b>CLUSTER</b> — {txn['cluster']} outro(s) insider(s) "
            f"compraram nos ultimos {CLUSTER_WINDOW_DAYS}d"
        )

    lines += [
        "",
        f"\U0001F4C5 Transacao: <code>{esc(txn.get('trade_date'))}</code>",
        f"\U0001F551 Reportado: <i>{esc(str(txn.get('filing_date'))[:16].replace('T', ' '))}</i>",
        f"\U0001F9EE Score: <i>{esc(', '.join(why) if why else 'nenhum criterio')}</i>",
        "",
        "<i>Nao e conselho financeiro. Sinal nao testado historicamente.</i>",
    ]

    keyboard = {
        "inline_keyboard": [[
            {"text": f"\U0001F4CA {ticker} no TradingView",
             "url": f"https://www.tradingview.com/symbols/{ticker}/"},
            {"text": "\U0001F4C4 Filing SEC", "url": txn["sec_url"]},
        ]]
    }

    return {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
        "reply_markup": keyboard,
    }


def send_telegram(payload: dict, dry_run: bool = False) -> bool:
    if dry_run:
        print("─" * 60)
        print(payload["text"])
        print("─" * 60)
        return True

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID nao definidos")
        return False

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
                log.warning("Telegram flood limit - espera %ds", retry_after)
                time.sleep(retry_after + 1)
                continue
            log.error("Telegram %s: %s", resp.status_code, resp.text[:300])
            return False
        except Exception as exc:
            log.warning("Telegram tentativa %d falhou: %s", attempt, exc)
            time.sleep(2 ** attempt)
    return False


def send_plain(text: str, dry_run: bool = False) -> None:
    send_telegram({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, dry_run=dry_run)


# ══════════════════════════════════════════════════════════════════
#  CICLO
# ══════════════════════════════════════════════════════════════════

def process_cycle(conn: sqlite3.Connection, lookback_minutes: int,
                  dry_run: bool = False) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    log.info("A buscar Form 4 com compras desde %s UTC", cutoff.strftime("%Y-%m-%d %H:%M"))

    try:
        raw_filings = fetch_recent_purchases(cutoff)
    except RateLimitError:
        log.warning("Rate limit - a desistir deste ciclo")
        return {"fetched": 0, "alerts": 0, "skipped": 0}

    log.info("%d filings recebidos", len(raw_filings))
    stats = {"fetched": len(raw_filings), "alerts": 0, "skipped": 0, "filtered": 0}

    # Ordem cronologica ascendente: garante que o primeiro comprador de um
    # cluster e gravado antes de o segundo ser avaliado.
    raw_filings.sort(key=lambda f: f.get("filedAt") or "")

    for raw in raw_filings:
        txn = parse_filing(raw)
        if txn is None:
            continue

        if already_seen(conn, txn["accession_number"]):
            stats["skipped"] += 1
            continue

        passed, reason = passes_filters(txn)
        score, why = calculate_score(txn, conn)

        if not passed:
            # Grava mesmo assim: alimenta a deteccao de cluster.
            record_transaction(conn, txn, score, alerted=False)
            stats["filtered"] += 1
            log.debug("filtrado %s: %s", txn["ticker"], reason)
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
            time.sleep(0.4)  # margem para o flood limit do Telegram

    log.info("ciclo: %d filings, %d alertas, %d repetidos, %d filtrados",
             stats["fetched"], stats["alerts"], stats["skipped"], stats["filtered"])
    return stats


def sleep_seconds() -> tuple[int, str]:
    """Cadencia por horario NYSE (so usada no modo --loop)."""
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return 3600, "fim de semana"
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return 300, "mercado aberto"
    if 16 * 60 <= minutes < 18 * 60 + 30:
        return 120, "pos-fecho (pico de filings)"
    return 1800, "fora de horas"


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
        log.error("Variaveis de ambiente em falta: %s", ", ".join(missing))
        sys.exit(2)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SEC Insider Trading Alert Bot")
    parser.add_argument("--loop", action="store_true",
                        help="corre continuamente (para VPS). Default: um ciclo e sai.")
    parser.add_argument("--dry-run", action="store_true",
                        help="imprime os alertas em vez de os enviar")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_MINUTES,
                        help=f"minutos de lookback (default {LOOKBACK_MINUTES})")
    parser.add_argument("--db", default=DB_PATH, help="caminho da SQLite")
    args = parser.parse_args(argv)

    check_config(args.dry_run)
    conn = init_db(args.db)

    if not args.loop:
        stats = process_cycle(conn, args.lookback, dry_run=args.dry_run)
        conn.close()
        return 0 if stats["fetched"] >= 0 else 1

    log.info("modo loop")
    cycle = 0
    lookback = args.lookback
    while True:
        cycle += 1
        try:
            secs, slot = sleep_seconds()
            log.info("── ciclo %d │ %s ──", cycle, slot)
            process_cycle(conn, lookback, dry_run=args.dry_run)
            lookback = max(secs // 60 + 5, 15)
            time.sleep(secs)
        except KeyboardInterrupt:
            log.info("parado pelo utilizador")
            return 0
        except Exception as exc:
            log.error("erro inesperado no ciclo %d: %s", cycle, exc, exc_info=True)
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
