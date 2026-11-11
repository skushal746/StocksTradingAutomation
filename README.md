# Stocks Trading Automation (MVP prototype)

An automated intraday trading platform for NSE equities, built as a single-process **modular monolith** in Python 3.11+ with `asyncio`.

```
pre-market RSS  ->  LLM analysis  ->  top-5 watchlist (by 08:45 IST)
                                            |
live Kite WebSocket ticks  ->  ORB + VWAP breakout  ->  risk checks  ->  order router
                                                                        (paper | live)
```

> **Read this first.** This is a prototype for learning and paper trading. It is not financial advice. Live trading is disabled unless you deliberately enable it, and even then the strategy has not been backtested or validated. Paper-trade for a meaningful period before risking capital.

---

## 1. Repository layout

```
config.py                 Typed settings from .env (pydantic-settings), safety gates
schemas.py                Pydantic contracts between layers (NewsItem, LLMAnalysis, Tick, TradeSignal, ...)
main.py                   Typer CLI: run-premarket, run-live, backfill-news, init-db, kite-*
core/
  logging_setup.py        loguru JSON logs + trade_audit.jsonl audit trail
  resilience.py           retry_async / exponential backoff with jitter
  kite_client.py          KiteConnect factory, instrument-token lookup, tick rounding
ingestion/
  news_scraper.py         async httpx + feedparser RSS reader, dedupe by headline and URL
  tick_listener.py        KiteTicker (threaded) bridged into asyncio via a bounded queue
analysis/
  llm_analyzer.py         OpenAI / Anthropic structured-JSON analysis, validated output
  watchlist.py            Scoring and ranking of material headlines into a top-N list
execution/
  orb_strategy.py         Opening Range Breakout + VWAP + volume confirmation (pure logic)
  risk_manager.py         Daily loss cap, max positions, sizing, SEBI peak-margin cap
  order_router.py         PaperOrderRouter (default) and KiteOrderRouter (MIS bracket legs)
  engine.py               Glue: ticks -> strategy -> risk -> router -> DB + audit
db/
  models.py               news_events, watchlist_history, trade_logs
  session.py              Async engine / session factory / create_all
  repository.py           Data-access functions
tests/                    42 unit tests (dedupe, LLM parsing, watchlist, risk, ORB, paper exits, config)
```

Execution logic is decoupled from the broker behind the `OrderRouter` protocol, so the same engine runs in paper or live mode.

---

## 2. Prerequisites

- Python **3.11+**
- PostgreSQL 14+ (local Docker is fine)
- A **Zerodha Kite Connect** app (API key and secret) with an active API subscription
- An **OpenAI** or **Anthropic** API key

---

## 3. Setup

### Option A: Docker Compose (Postgres + app, recommended)

Requires Docker Desktop (or Docker Engine with the Compose plugin).

```bash
cd StocksTradingAutomation
cp .env.example .env            # fill in KITE_*, OPENAI_API_KEY / ANTHROPIC_API_KEY
docker compose up -d --build    # starts Postgres on :5432, creates tables, starts the app container
docker compose ps               # both services should be "running"/"healthy"
```

The `app` container stays idle after startup. Run commands inside it:

```bash
docker compose exec app python main.py kite-login-url
docker compose exec app python main.py kite-session --request-token <TOKEN>   # then update KITE_ACCESS_TOKEN in .env
docker compose up -d app        # recreate app so it picks up the new token
docker compose exec app python main.py run-premarket
docker compose exec app python main.py run-live
```

Notes:
- The app reaches Postgres at host `db` automatically. Do not point `DATABASE_URL` at `localhost` for the containerised app.
- Change the database password with `POSTGRES_PASSWORD` in `.env` before the first `up`. The volume keeps the password from its first initialisation.
- If port 5432 is already used on your machine, set `POSTGRES_PORT=5433` in `.env`.
- Logs are written to `./logs` on the host through a bind mount.
- `docker compose down` stops everything and keeps data. `docker compose down -v` also deletes the database.

### Option B: Local Python with Postgres in Docker

```bash
cd StocksTradingAutomation            # project root
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime deps + pytest
cp .env.example .env                  # then edit .env
```

Start Postgres (example with Docker):

```bash
docker run -d --name trading-pg -e POSTGRES_USER=trader -e POSTGRES_PASSWORD=trader \
  -e POSTGRES_DB=trading -p 5432:5432 postgres:16
```

Create tables:

```bash
python main.py init-db
```

Run the unit tests (no network, DB or broker needed):

```bash
pytest -q
```

---

## 4. Environment variables

All variables live in `.env` (never commit it; `.gitignore` already excludes it). The full annotated list is in `.env.example`.

| Group | Variable | Default | Purpose |
|---|---|---|---|
| Mode | `TRADING_MODE` | `paper` | `paper` simulates fills. `live` places real orders. |
| Mode | `LIVE_TRADING_ACK` | empty | Must equal `I-UNDERSTAND-LIVE-TRADING-RISK` when `TRADING_MODE=live`. |
| DB | `DATABASE_URL` | `postgresql+asyncpg://trader:trader@localhost:5432/trading` | SQLAlchemy async URL. |
| Kite | `KITE_API_KEY` | | Kite Connect API key. |
| Kite | `KITE_API_SECRET` | | Used only by `kite-session`. |
| Kite | `KITE_ACCESS_TOKEN` | | Daily token. Generate with `kite-login-url` and `kite-session`. |
| Kite | `KITE_EXCHANGE` | `NSE` | Exchange for instruments and orders. |
| Kite | `WS_RECONNECT_MAX_TRIES` / `_MAX_DELAY` | `50` / `60` | WebSocket reconnect policy (exponential backoff inside the SDK). |
| LLM | `LLM_PROVIDER` | `openai` | `openai` or `anthropic`. |
| LLM | `LLM_MODEL` | `gpt-4o-mini` / `claude-haiku-5-5` | Blank means the provider default. |
| LLM | `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | | Key for the selected provider. |
| LLM | `LLM_CONCURRENCY` / `LLM_TIMEOUT_SECONDS` | `5` / `30` | Parallel requests and per-call timeout. |
| News | `NEWS_FEED_URLS` | Moneycontrol, Economic Times, Mint | Comma-separated RSS/Atom URLs. |
| News | `NEWS_LOOKBACK_HOURS` | `18` | Window used when building the watchlist. |
| Watchlist | `WATCHLIST_SIZE` | `5` | Number of names in the morning list. |
| Watchlist | `PREMARKET_CUTOFF` | `08:45` | Warning is logged if `run-premarket` runs later than this. |
| Watchlist | `WATCHLIST_ALLOW_SHORTS` | `false` | Allow BEARISH names to trigger short entries. |
| Risk | `CAPITAL_INR` | `500000` | Capital base for sizing and loss caps. |
| Risk | `MAX_DAILY_LOSS_PCT` | `0.02` | Entries halt once realised + unrealised PnL reaches -2% of capital. |
| Risk | `STOP_LOSS_PCT` | `0.005` | Stop distance from entry (0.5%). |
| Risk | `TARGET_PCT` | `0.01` | Target distance from entry (1.0%). |
| Risk | `RISK_PER_TRADE_PCT` | `0.005` | Capital risked per trade. This sizes the position. |
| Risk | `MAX_OPEN_POSITIONS` | `3` | Concurrent positions cap. |
| Risk | `SEBI_PEAK_MARGIN_PCT` | `0.20` | Minimum margin as a share of notional (about 5x intraday leverage). |
| Risk | `MAX_MARGIN_UTILISATION` | `0.80` | Never use more than this share of available margin. |
| ORB | `ORB_RANGE_MINUTES` | `15` | Opening range length, 5 or 15. |
| ORB | `ORB_VOLUME_MULTIPLIER` | `1.5` | Last-60s volume must be at least this multiple of the range's per-minute average. |
| ORB | `MARKET_OPEN` / `ENTRY_CUTOFF` / `SQUARE_OFF_TIME` | `09:15` / `14:30` / `15:10` | IST session windows. |
| ORB | `PAPER_SLIPPAGE_PCT` | `0.0005` | Adverse slippage applied to simulated fills. |

Configuration is validated at startup. For example, `ORB_RANGE_MINUTES` must be 5 or 15, `TARGET_PCT` must exceed `STOP_LOSS_PCT`, and `RISK_PER_TRADE_PCT` cannot exceed `MAX_DAILY_LOSS_PCT`.

---

## 5. Daily workflow (paper trading)

### Step 1: Morning, before 08:45 IST. Build the watchlist

```bash
python main.py run-premarket
```

This fetches the RSS feeds, deduplicates them, stores new headlines, sends unanalysed ones to the LLM, and prints the ranked watchlist. Example output (illustrative, not real data):

```
Pre-market run 2026-11-11 08:20 IST
  fetched=143 new=61 analysed=61 material=17

Watchlist for 2026-11-11:
   #  TICKER       BIAS     CATALYST               CONF  HEADS
   1  TCS          BULLISH  CONTRACT_WIN           0.86  2
   2  RELIANCE     BULLISH  REGULATORY_APPROVAL    0.78  1
   ...
```

Useful flags: `--skip-llm` ingests only, and `--lookback-hours 24` widens the window.

### Step 2: Start of session. Refresh the Kite token

Kite access tokens expire daily.

```bash
python main.py kite-login-url                       # open the URL, log in
python main.py kite-session --request-token <TOKEN> # copy the printed KITE_ACCESS_TOKEN into .env
```

### Step 3: Market hours. Run the paper engine

```bash
python main.py run-live
```

With the default `TRADING_MODE=paper` this:

1. loads today's watchlist from Postgres and resolves NSE instrument tokens
2. subscribes to full-mode ticks on those instruments over the Kite WebSocket
3. builds the 15-minute (or 5-minute) opening range from 09:15 IST
4. after the range closes, evaluates breakouts until `ENTRY_CUTOFF` and simulates fills
5. exits simulated positions on stop-loss or take-profit, using live ticks
6. flattens everything at `SQUARE_OFF_TIME` (15:10 IST) or on Ctrl-C

No orders reach the broker in paper mode. Ticks and quotes are real.

### Step 4: Review

- Trade rows: `SELECT ticker, side, entry_price, exit_price, pnl, exit_reason FROM trade_logs WHERE trade_date = CURRENT_DATE;`
- Audit trail: `logs/trade_audit.jsonl` (one JSON object per signal decision, order, and exit)
- Application logs: `logs/app.jsonl` and stderr, both JSON

### Optional: backfill news

```bash
python main.py backfill-news --days 2 --limit 200
```

RSS feeds only expose recent items, so this ingests what the feeds currently publish and scores anything still unanalysed. It does not reach back in time.

---

## 6. Strategy

**Opening Range Breakout with VWAP and volume confirmation**, per watchlist name:

- **Range:** high and low of trades from 09:15 to 09:15 + `ORB_RANGE_MINUTES`.
- **VWAP:** cumulative price times traded-volume delta, from the first tick of the day.
- **Long entry:** a tick after the range closes with `price > range_high`, `price > VWAP`, and `volume_ratio >= ORB_VOLUME_MULTIPLIER`. `volume_ratio` is last-60s volume divided by the range's average per-minute volume.
- **Short entry** (only with `WATCHLIST_ALLOW_SHORTS=true`): the mirror image on the downside.
- **Levels:** stop at entry x (1 - 0.5%), target at entry x (1 + 1.0%).
- **One signal per symbol per day.** Entries stop at `ENTRY_CUTOFF`.

Position size is the smaller of:

- risk budget: `capital x RISK_PER_TRADE_PCT / per-share stop distance`
- margin cap: `available_margin x MAX_MARGIN_UTILISATION / SEBI_PEAK_MARGIN_PCT / entry`

---

## 7. Safety design

| Concern | How it is handled |
|---|---|
| Accidental live trading | Default is paper. Live needs `TRADING_MODE=live` and the exact `LIVE_TRADING_ACK` token. `assert_live_allowed()` runs before anything connects. |
| Runaway losses | Daily loss cap (realised + unrealised), kill switch API, max open positions, and a hard square-off time. |
| Naked positions | Live exit legs are placed only after the entry is COMPLETE. If leg placement fails, the position is flattened at market. |
| Duplicate entries | One signal per symbol per day. Entry tags are unique per order. Trade rows are keyed by order ID. |
| Stale data | The tick queue drops the oldest tick when full. Fresh prices matter more than backlog. |
| WebSocket drops | KiteTicker reconnects with exponential backoff (`WS_RECONNECT_MAX_TRIES`, `WS_RECONNECT_MAX_DELAY`). If it gives up, the engine flattens and exits. |
| External HTTP | Feed fetches and LLM calls retry only transient errors (transport, 408/409/429, 5xx) with exponential backoff and jitter. One dead feed never stops the others. |
| LLM output | Responses are parsed and validated with pydantic. Out-of-range confidence, unknown biases or malformed JSON are logged and skipped. |
| Secrets | Read from `.env`. Kite secret and tokens are `SecretStr` and never logged. |

**Known limitations** (these matter before any live use):

- **Live mode is not battle-tested.** It is written against the Kite Connect API (`place_order`, `orders`, `cancel_order`, `margins`) but has not been run against a live account. Start with a tiny `CAPITAL_INR` and `MAX_OPEN_POSITIONS=1`.
- **No partial-fill handling.** Exit legs are sized to the filled quantity reported at COMPLETE. Partial fills are not split.
- **Paper fills are idealised.** Fills are at signal price plus a fixed slippage percentage. No queue position, no rejections, no brokerage or STT, no impact. Real PnL will be worse.
- **Restart mid-session.** Realised PnL for the day is restored from the DB, but open live positions are not re-adopted after a crash. Check the broker terminal and square off manually.
- **SEBI margin figure.** `SEBI_PEAK_MARGIN_PCT=0.20` is a conservative approximation. Confirm the current rule and your broker's MIS margins before relying on the sizing.
- **Feed URLs** are examples. Publishers change feed locations. Verify each URL and add corporate-disclosure feeds as you need them.
- **Schema management** uses `create_all`. Add Alembic before any production schema change.
- **No backtester yet.** The ORB logic is covered by unit tests, but its edge is unmeasured.

---

## 8. Development

```bash
pytest -q                          # unit tests
python -m compileall -q .          # syntax check
python main.py --help
```

The pure logic (`orb_strategy`, `risk_manager`, `watchlist`, `news_scraper.dedupe`, `llm_analyzer.parse_llm_response`, `order_router.PaperOrderRouter`) is I/O-free and fully unit-tested. The network, database and broker paths are exercised only against real services.

Adding a news source: append its URL to `NEWS_FEED_URLS`. Adding an LLM provider: implement `complete(system, user) -> str` and register it in `build_llm_client`.

---

## 9. Disclaimer

This software is provided as-is, without warranty. Trading involves substantial risk of loss. You are responsible for compliance with SEBI and exchange rules, your broker's terms, and the tax treatment of your trades.
