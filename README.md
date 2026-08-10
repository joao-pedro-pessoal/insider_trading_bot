# SEC Insider Trading Alert Bot v1.1

Alertas no Telegram sobre compras de mercado aberto (Form 4, código `P`) reportadas por insiders à SEC. Corre em GitHub Actions por cron — sem servidor, sem custos.

> **Não é conselho financeiro.** O score é heurístico e **não foi testado historicamente**. Ver [Limitações](#limitações-que-deves-conhecer).

---

## O que mudou da v1.0

| # | Bug na v1.0 | Correção |
|---|---|---|
| 1 | Endpoint `/form-4` e query `query_string` aninhada — a API não aceita nenhum dos dois | `/insider-trading` com query Lucene em string |
| 2 | Field paths errados: `raw["ticker"]`, `transactionCoding.transactionCode`, `transactionAmounts.transactionShares` | `issuer.tradingSymbol`, `coding.code`, `amounts.shares` |
| 3 | `datetime.utcnow()` comparado com `filedAt` em horário de NY → query devolvia vazio quase sempre | Filtragem local com datetimes aware + paginação até ao cutoff |
| 4 | `_esc()` escrita para MarkdownV2 e nunca chamada, com `parse_mode: HTML` → "Procter & Gamble" dava erro 400 | `html.escape()` aplicado a todos os campos dinâmicos |
| 5 | `sec_url` usava o ticker como CIK → link sempre 404 | URL do EDGAR construído a partir de `issuer.cik` + accession |
| 6 | Só a primeira linha `P` do filing era lida | Agrega todas as linhas `P`/`A`, com preço médio ponderado |
| 7 | 10b5-1 detectado por `str(raw).lower()` | Campo oficial `aff10b5One` (+ fallback nas footnotes) |
| 8 | Cluster: comparava `YYYY-MM-DD` com timestamp ISO, contava filings em vez de pessoas, e lia só de alertas enviados | Compara datas com datas, conta insiders distintos, grava **todas** as transações vistas |
| 9 | `RateLimitError` usada antes de ser definida | Definida no topo |
| 10 | `while True` no Colab — morre quando a runtime desconecta | Um ciclo por invocação (`--once`), com `--loop` opcional |
| 11 | Chaves hardcoded no ficheiro | Tudo via variáveis de ambiente |

Extras: retry com backoff exponencial, tratamento do flood limit do Telegram, breakdown do score em cada alerta (`+3 CEO/CFO, +3 valor >= $500k`), filtro de `acquiredDisposedCode` para não deixar passar disposições, `--dry-run`, e 38 testes com fixtures.

---

## Setup

### 1. Repositório

```
o-teu-repo/
├── insider_bot.py
├── test_bot.py
├── requirements.txt
├── README.md
└── .github/workflows/insider-bot.yml
```

O repo pode ser privado — GitHub Actions funciona igual (2000 min/mês grátis; este bot usa ~30s por corrida).

### 2. Telegram

1. `@BotFather` → `/newbot` → guarda o token
2. Manda uma mensagem qualquer ao teu bot
3. Vai a `https://api.telegram.org/bot<TOKEN>/getUpdates` → copia `message.chat.id`

### 3. Secrets

**Settings → Secrets and variables → Actions → New repository secret:**

| Secret | Valor |
|---|---|
| `SEC_API_KEY` | chave da [sec-api.io](https://sec-api.io) |
| `TELEGRAM_TOKEN` | token do @BotFather |
| `TELEGRAM_CHAT_ID` | o teu chat id (supergrupos começam por `-100`) |
| `TELEGRAM_TOPIC_ID` | **opcional** — só para supergrupos com tópicos |

### Enviar para um tópico específico (supergrupo com forum)

Se o bot está num grupo com tópicos activados, sem `TELEGRAM_TOPIC_ID` as mensagens caem no tópico *General*. Para as encaminhar:

1. Adiciona o bot ao grupo (basta ser membro; admin também serve)
2. Escreve uma mensagem qualquer **dentro do tópico** que queres usar
3. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Nessa mensagem, copia o `message_thread_id` → é o `TELEGRAM_TOPIC_ID`
5. O `chat.id` do mesmo objecto é o `TELEGRAM_CHAT_ID` (negativo, começa por `-100`)

### 4. Primeira corrida

**Actions → Insider Alert Bot → Run workflow**, com `dry_run: true` e `lookback: 240`. Os alertas aparecem no log em vez de irem para o Telegram. Se estiverem bem, corre outra vez com `dry_run: false`.

---

## Como funciona a persistência

GitHub Actions não tem disco persistente. A SQLite é guardada num branch órfão `bot-state` (só o ficheiro `.db`, force-push a cada corrida) e restaurada no início da corrida seguinte. Um artifact serve de backup com 14 dias de retenção.

Isto é o que torna o dedup e a detecção de cluster possíveis entre corridas. Se apagares o branch `bot-state`, o bot arranca a frio e pode repetir alertas recentes.

O `concurrency: insider-bot` impede duas corridas simultâneas de escreverem na DB e perderem escritas.

---

## Cadência

| Janela (ET) | Frequência | Porquê |
|---|---|---|
| 09:00–16:00, seg-sex | 15 min | mercado aberto |
| 16:00–19:00, seg-sex | 10 min | pico de entrega de Form 4 |
| resto | 1 hora | varredura de segurança |

O `LOOKBACK_MINUTES=90` cria sobreposição deliberada: uma corrida falhada ou atrasada (o cron do GitHub atrasa-se com frequência em horas de pico) não perde filings, porque o dedup por accession absorve as repetições.

---

## Configuração

Todas as variáveis são opcionais menos as três chaves.

| Variável | Default | O que faz |
|---|---|---|
| `MIN_TRANSACTION_VALUE_USD` | `25000` | ignora compras abaixo deste valor |
| `CLUSTER_WINDOW_DAYS` | `7` | janela para detectar cluster buying |
| `SCORE_MIN_TO_SEND` | `1` | abaixo disto nem envia (grava só na DB) |
| `SCORE_SILENT_BELOW` | `3` | envia sem notificação sonora |
| `SCORE_MAX_ALERT_FROM` | `6` | 🚨 MAX ALERT |
| `LOOKBACK_MINUTES` | `90` | janela de busca |
| `MAX_PAGES` | `6` | páginas de 50 filings por corrida |
| `TELEGRAM_TOPIC_ID` | — | tópico de destino em supergrupos com forum |
| `VERBOSE` | `false` | logging DEBUG |

## Scoring

| Critério | Pontos |
|---|---|
| CEO / CFO | +3 |
| Director / VP / outro officer | +1 |
| Valor ≥ $500k | +3 |
| Valor ≥ $100k | +1 |
| Aumento de posição ≥ 20% | +2 |
| Cluster: outro insider comprou nos últimos 7d | +3 |

Cada alerta traz o breakdown para poderes auditar porque apareceu.

---

## Testar localmente

```bash
pip install -r requirements.txt
python test_bot.py                              # 38 testes, sem rede

export SEC_API_KEY="..."
python insider_bot.py --dry-run --lookback 240  # dados reais, imprime em vez de enviar
```

Inspeccionar a base de dados:

```bash
sqlite3 state/alerts.db "
  SELECT ticker, insider_name, title, total_value, score, is_10b5, alerted
  FROM transactions ORDER BY recorded_at DESC LIMIT 20;"
```

Para VPS em vez de Actions: `python insider_bot.py --loop`.

---

## Limitações que deves conhecer

Estas não são bugs — são propriedades do sinal. Valem mais do que o código.

**O sinal não é rápido, é lento.** O Form 4 é entregue até 2 dias úteis depois da transacção e fica público para todos ao mesmo tempo. Não há vantagem de velocidade a ganhar aqui. A literatura académica (Lakonishok & Lee; Cohen, Malloy & Pomorski sobre "insiders oportunistas") aponta para retornos anormais **modestos, em horizontes de 6 a 12 meses**. Alertas a cada 10 minutos são úteis para não perderes o filing, não para fazer scalping.

**Falta normalização por market cap.** Uma compra de $100k numa nano-cap é enorme; na Apple é irrelevante. O score actual trata as duas de forma idêntica — é o maior defeito que resta. Fase 2.

**Não há sinal de venda.** Insiders vendem por impostos, diversificação, divórcio, planos automáticos. Compras informam; vendas informam muito menos. Qualquer regra de saída tem de vir de ti (horizonte fixo, trailing stop, alvo), não dos dados de insider.

**O score não foi backtestado.** Os pesos são heurísticas plausíveis, não resultado de medição. Podem estar a pontuar ruído. Até correr a Fase 3, trata o score como "isto merece que eu olhe", não como "isto vai subir".

**Legal:** negociar com base em Form 4 públicos é legal — não é insider trading. Insider trading é negociar com informação material **não pública**.

---

## Próximos passos

- **Fase 2 — enriquecer:** market cap (yfinance), preço vs MM50/MM200, capturar vendas (`S`), agrupar filings do mesmo dia por ticker
- **Fase 3 — backtestar:** bulk archives da sec-api (`/bulk/form-4/YYYY/YYYY-MM.jsonl.gz`) + preços históricos; retorno a 1/5/21/63/126 dias por bucket de score. Isto responde se o score serve para algo
- **Fase 4 — sinais:** só se a Fase 3 mostrar edge. Entrada + saída explícitas, sizing, e paper trading antes de dinheiro real

## Fontes

- [Insider Trading Data from SEC Form 3, 4, 5 Filings — sec-api.io](https://sec-api.io/docs/insider-ownership-trading-api)
- [Analyze SEC Form 4 Insider Trades with Python — sec-api.io](https://sec-api.io/docs/insider-ownership-trading-api/python-example)
- [Form 4 Bulk Dataset — sec-api.io](https://sec-api.io/datasets/form-4)
