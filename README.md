# Options Wheel Strategy — Deterministic Trading Engine

Covered Call & Cash Secured Put screening, scoring, and portfolio management.

**Hard constraints**: no naked options, no margin trading, no spreads. CC requires 100 shares owned. CSP requires full cash coverage.

**No script submits orders to moomoo.** All trades executed manually.

---

## Quick Start

```bash
# 1. Sync REAL portfolio to local DB (funds + positions + orders)
python3 scripts/portfolio.py sync

# 2. View portfolio from local DB (no OpenD needed)
python3 scripts/portfolio.py status

# 3. Full daily analysis: sync + screen + portfolio check + log
python3 scripts/daily_run.py --top 10

# 4. Screen for best trades only
python3 scripts/screener.py --top 5

# 5. Adhoc research
python3 scripts/market_data.py NVDA --options
python3 scripts/market_sentiment.py
```

**Prerequisites**: OpenD running on `127.0.0.1:11111`. Install: `pip3 install moomoo-api yfinance pandas`.

---

## Architecture

```
Moomoo REAL account (read-only poll)
       │
       ├── portfolio.py sync ──→ db/options.db
       │     ├── portfolio_snapshots   (fund history)
       │     ├── positions             (stocks + options)
       │     └── local_trades          (executed orders)
       │
       └── daily_run.py / screener.py ──→ Analysis
             ├── src/analysis/sentiment.py   (macro + ticker data)
             ├── src/scoring/                (WHEEL_SCORE engine)
             ├── src/signals/                (signal generation)
             └── src/risk/                   (collar check)
```

**Key principle**: Scripts poll data and analyze. Never submit orders. Clean separation between data (DB), analysis (src/), and execution (manual).

---

## Project Structure

```
options/
├── db/
│   └── options.db                     # Single SQLite — 9 tables, one source of truth
├── src/
│   ├── data/
│   │   ├── models.py                  # Dataclasses: StockSnapshot, OptionSnapshot
│   │   ├── moomoo_client.py           # Moomoo OpenD data client (quotes + chains)
│   │   ├── yfinance_client.py         # Yahoo Finance: analyst, earnings, news, macro
│   │   ├── compute.py                 # Deterministic indicators (RSI, MACD, Greeks, GEX)
│   │   ├── iv_history.py              # IV rank persistence in options.db
│   │   ├── trade_log.py               # TradeLog + DailyRunDB
│   │   ├── portfolio_db.py            # PortfolioDB (funds, positions, local trades)
│   │   ├── portfolio_sync.py          # PortfolioSync (poll REAL account, sync orders)
│   │   └── guardrails.py              # Portfolio size/risk limits
│   ├── analysis/
│   │   ├── sentiment.py               # Shared: macro context + ticker sentiment
│   │   ├── trend.py                   # Trend/momentum indicators
│   │   ├── options_chain.py           # Options chain analysis
│   │   └── correlation.py             # Correlation vs V
│   ├── scoring/                       # WHEEL_SCORE engine (deterministic)
│   ├── signals/                       # Signal generator + sentiment scoring
│   ├── risk/                          # Collar check, position monitor
│   └── trade/                         # Validator, position sizer
├── scripts/
│   ├── portfolio.py                   # Portfolio: sync / status / summary / history
│   ├── daily_run.py                   # Daily pipeline: sync + screen + check + log
│   ├── screener.py                    # Watchlist screener: CC + CSP candidates
│   ├── portfolio_check.py             # Position health: score all holdings
│   ├── market_data.py                 # Adhoc: single ticker deep dive
│   └── market_sentiment.py            # Adhoc: macro + analyst + earnings + news
├── specs/                             # Research + architecture docs
├── tests/                             # Test suite
├── CLAUDE.md                          # AI coding instructions
└── GOAL.md                            # Investment goals
```

---

## Scripts

### `portfolio.py` — Portfolio Manager

Pulls REAL account data (funds, positions, executed orders) into local DB. Auto-syncs fills from moomoo — no manual trade entry. **Read-only, never submits orders.**

```bash
python3 scripts/portfolio.py sync          # Full sync: funds + positions + orders
python3 scripts/portfolio.py status        # Full portfolio view from DB
python3 scripts/portfolio.py summary       # Quick one-line numbers
python3 scripts/portfolio.py history 30    # Fund history over N days
```

**Options:**

| Command | Needs OpenD? | Description |
|---------|:---:|-------------|
| `sync` | ✅ | Polls REAL account via `accinfo_query`, `position_list_query`, `order_list_query`. Writes to `portfolio_snapshots`, `positions`, `local_trades` tables |
| `status` | ❌ | Reads from DB. Shows funds, 13 stocks, 13 options with P&L |
| `summary` | ❌ | One line: `Cash=$80 Stocks=13($182K) Options=13 Unrealized=$42K OpenTrades=0` |
| `history N` | ❌ | Daily fund snapshots: total assets, cash, stock value over N days |

**How sync works:**

```
portfolio.py sync
  ├── accinfo_query(REAL) → portfolio_snapshots (total_assets, cash, buying_power)
  ├── position_list_query(REAL) → positions (code, qty, cost, price, P&L)
  └── order_list_query(REAL) → local_trades (auto-matched fills)
        ├── FILLED order → recorded as new OPEN trade
        └── BUY_BACK order → matches existing OPEN trade → marks CLOSED
```

**Schedule via cron:**
```bash
7 17 * * 1-5 cd ~/options && python3 scripts/portfolio.py sync
```

Also runs automatically at the start of `daily_run.py`.

---

### `daily_run.py` — Daily Pipeline

Single-command daily workflow. Syncs portfolio, screens watchlist, checks positions, logs everything to DB. Runs in ~10 seconds.

```bash
python3 scripts/daily_run.py                 # Full run
python3 scripts/daily_run.py --top 5         # Top 5 screener picks
python3 scripts/daily_run.py --no-external   # Offline (skip yfinance)
python3 scripts/daily_run.py --archive-chains # Archive option chains for backtesting
```

**Options:**

| Flag | Description |
|------|-------------|
| `--top N` | Number of screener picks to show (default: 10) |
| `--no-external` | Skip Yahoo Finance — faster, offline |
| `--archive-chains` | Store full option chains in `run_chains` table (~60s extra, builds historical DB) |

**What it does (in order):**

1. **Portfolio sync** — auto-runs `portfolio.py sync` (funds + positions + orders)
2. **Macro snapshot** — VIX, VVIX, DXY, yields, credit spreads, Fear & Greed, regime score
3. **Watchlist screening** — scores all tickers, finds best CC/CSP candidates
4. **Portfolio check** — scores all open stocks + options, generates buy/sell/hold/close/roll decisions
5. **Execution summary** — what to open, close, roll
6. **Logs to DB** — `daily_runs`, `run_signals`, `run_positions`, `run_chains`

**Output example:**
```
🌍 VIX 15.8 | CAUTIOUS | Size: 50% | 10Y 4.6%

🔍 SCREENING 11 TICKERS
  🎯 TOP 5 PICKS:
   # Ticker   Strat  Score    Strike       Expiry  DTE      Δ     Bid    RoC     IV     OI
   1 NVDA       CC   2.6 $  220.00   2026-08-14  36 0.316 $  5.00  24.8%  41.1%  1,045
   2 AVGO       CSP  2.8 $  350.00   2026-08-21  43 0.244 $ 10.95  26.6%  52.3%  5,569
   ...

📋 EXECUTION SUMMARY
  🟢 OPEN (5 candidates): CC NVDA $220, CSP AVGO $350, ...
  🔴 CLOSE (3 positions): V C380 ✅, AVGO P350 ✅
  🟡 ROLL (1 position): MU P790 ⚠️
```

---

### `screener.py` — Watchlist Screener

Scores every watchlist ticker across 5 dimensions. Finds the best option contract (strike + expiry + delta + RoC) per ticker.

```bash
python3 scripts/screener.py                   # Full scan
python3 scripts/screener.py --top 5           # Top 5 only
python3 scripts/screener.py --cc-only         # Covered calls only
python3 scripts/screener.py --csp-only        # Cash-secured puts only
python3 scripts/screener.py --no-external     # Offline mode
python3 scripts/screener.py --log             # Log picks to trade_log table
```

**Options:**

| Flag | Description |
|------|-------------|
| `--top N` | Show top N results (default: 10) |
| `--cc-only` | Covered calls only |
| `--csp-only` | Cash-secured puts only |
| `--no-external` | Skip Yahoo Finance (faster, offline) |
| `--log` | Log top picks to `trade_log` table |

**Scoring dimensions (1-10, 1=best):**

| Dimension | Weight | What it measures |
|-----------|--------|-----------------|
| Technical | 25% | RSI, trend alignment (SMA stack), ADX, volume |
| Options Quality | 25% | Bid-ask spread, IV rank, market cap, beta |
| Fundamental | 15% | P/E, dividend yield, earnings consistency |
| External Sentiment | 20% | Analyst consensus, earnings blackout, insider trades, news score |
| Macro/Risk | 15% | VIX regime, yield curve, credit spreads |

**Contract penalty added** for: low OI, wide spread, extreme delta, DTE outside 30-45 sweet spot.

**Output:**
```
🌍 VIX 15.8 | CAUTIOUS | Size: 50% | 10Y 4.6%

  # Ticker   Strat  Score    Strike       Expiry  DTE      Δ     Bid    RoC     IV     OI   Capital   Reason
  1 NVDA       CC   2.6 $  220.00   2026-08-14  36 0.316 $  5.00  24.8%  41.1%  1,045 $  1,200   Strong CC
  2 AVGO       CSP  2.8 $  350.00   2026-08-21  43 0.244 $ 10.95  26.6%  52.3%  5,569 $ 35,000   Excellent

  💡 Best CSP: AVGO $350 2026-08-21 Δ0.24 RoC 26.6% Score 2.8
  💡 Best CC:  NVDA $220 2026-08-14 Δ0.32 RoC 24.8% Score 2.6
```

---

### `portfolio_check.py` — Position Health

Scores every open stock and option position. Gives decisions: sell CC, close at 50% profit, roll underwater, hold.

```bash
python3 scripts/portfolio_check.py                # Full check
python3 scripts/portfolio_check.py --no-external  # Offline mode
```

**Options:**

| Flag | Description |
|------|-------------|
| `--no-external` | Skip Yahoo Finance (offline, faster) |

**Decision logic for stocks:**

| Condition | Decision |
|-----------|----------|
| ≥ 100 shares + CC RoC ≥ 8% | 🎯 SELL CC $STRIKE DATE @ RoC% |
| < 100 shares | 📋 HOLD (<100 shares, can't sell CC) |
| No liquid calls available | 📋 HOLD (no suitable contracts) |

**Decision logic for options:**

| Condition | Decision |
|-----------|----------|
| Profit ≥ 70% | ✅ CLOSE (70%+ profit — risk/reward inverted) |
| Profit ≥ 50% | ✅ CLOSE (50%+ profit — TastyTrade backtested) |
| Profit ≥ 30% | 👍 HOLD (30%+ captured, still earning) |
| DTE ≤ 3 | ⚠️ EXPIRING — close or roll (gamma explosion) |
| ITM (Δ > 0.50) | ⚠️ ITM RISK — assignment likely |
| Underwater + DTE ≤ 21 | 🔄 CONSIDER ROLLING |
| Loss < -$500 | 🔴 UNDERWATER — evaluate exit |

**Output:**
```
💰 Liquid: $48,653 (cash $80 + fund $48,573) | BP: $48,653 | 13 stocks, 13 options

🎯 COVERED CALL CANDIDATES:
  Ticker      Qty      Price       Cost       MktVal     P&L%  Score  Decision
  V           431 $   348.20 $   270.54 $ 150,074.20   +28.7%    3.0  SELL CC $360 2026-08-21 @ 21.0%

📊 OPTION POSITIONS (13)
  Code                       Qty  DTE       Δ     Cost      Bid        P&L    P&L%  Profit%  Score  Decision
  US.AVGO260731P350000        -1   22  -0.139 $  11.20 $   4.00 $   705.00  +62.9%    64.3%    3.5  ✅ CLOSE (50%+ profit)
  US.V260821C380000           -1   43  +0.170 $   5.15 $   2.42 $   255.50  +49.6%    53.0%    3.5  ✅ CLOSE (50%+ profit)
  US.GOOG260717P335000        -1    8  -0.154 $   1.22 $   1.43 $   -33.00  -27.0%   -17.2%    6.0  ⚠️  UNDERWATER
```

---

### `market_data.py` — Ticker Deep Dive (Adhoc)

Single-ticker comprehensive data: price, technicals, fundamentals, option chain with full Greeks.

```bash
python3 scripts/market_data.py AAPL                       # Stock only
python3 scripts/market_data.py V --options                # Stock + option chain (30-45 DTE)
python3 scripts/market_data.py NVDA --options --all       # Full chain + computed indicators
python3 scripts/market_data.py MSFT --options --chain 15 30  # Custom DTE range
```

**Options:**

| Flag | Description |
|------|-------------|
| `ticker` | Required. e.g. `V`, `US.AAPL`, `NVDA` |
| `--options`, `-o` | Include option chain (30-45 DTE, top 10 by OI) |
| `--all`, `-a` | Full chain with max pain, PCR, skew, GEX |
| `--chain MIN MAX`, `-c` | Custom DTE range (default: 0 90 = all) |

**Output:**
```
💰 PRICE
  Last: $348.20  (-1.3%)  Bid: $348.76  Ask: $349.07  Spread: 0.09%

📈 TECHNICALS
  RSI(14): 62  ADX(14): 29  ATR(14): $8.71  HV(30d): +23.0%
  SMA-20: $335.65  SMA-50: $328.37  SMA-200: $328.04
  MACD: 7.902  Signal: 5.949  Histogram: 1.952
  Beta vs SPY: 0.43

🏛️ FUNDAMENTALS
  P/E: 34.1  P/E TTM: 30.3  P/B: 18.46
  Div Yld: +0.7%  Mkt Cap: $658B

📊 OPTIONS CHAIN (30-45 DTE)
  ATM IV: 26.8%  Put/Call OI: 0.86  Max Pain: $330.00
  25Δ Skew: +2.7%  Term Structure: BACKWARDATION
  Call Wall: $345.00  Put Wall: $330.00
  GEX: 3,747,725 (Long γ)
```

---

### `market_sentiment.py` — Macro + Sentiment (Adhoc)

External data from Yahoo Finance and alternative.me. Uses shared `src/analysis/sentiment.py` — same module as the pipeline.

```bash
python3 scripts/market_sentiment.py                 # Macro only
python3 scripts/market_sentiment.py AAPL            # Macro + AAPL
python3 scripts/market_sentiment.py V --news        # Macro + V + news headlines
python3 scripts/market_sentiment.py --watchlist     # All watchlist tickers
```

**Options:**

| Flag | Description |
|------|-------------|
| `ticker` | Optional. Single ticker for ratings + earnings |
| `--news`, `-n` | Include recent news headlines with sentiment |
| `--watchlist`, `-w` | Fetch for all watchlist tickers (11 tickers) |

**Output:**
```
🌍 MACRO — 2026-07-10
  VIX:        15.79
  Regime:      CAUTIOUS  (score: -1, size: 50%)
  10Y Yield:   4.57%    2Y: 3.70%    30Y: 5.08%
  10Y-2Y:     +0.87%
  DXY:        100.93    VVIX: 91.38
  Credit:     -14.84%  (STRESSED) 🔴
  Fear&Greed: 22  (Extreme Fear) 🔴🔴
  💡 POSITION SIZING: 50% of normal size

📋 US.V (V)
🎯 ANALYST RATINGS (38 analysts)
  Consensus: STRONG_BUY  |  Target: $401.16  |  Upside: +15.4%

📅 EARNINGS
  Next: 2026-07-29 (20d)  |  EPS Est: $3.22  |  Growth: +8.2%
```

---

## Scoring Guide — 1-10 Scale

Both `screener.py` and `portfolio_check.py` use the same 1-10 scale. **Lower is better.** 1 = act now, 10 = emergency.

### Score Ranges

| Score | Label | What Gets This Score |
|-------|-------|---------------------|
| **1.0–2.0** | 🔥 Must act | 70%+ profit captured, or expiring today. Close immediately. |
| **2.0–3.0** | ✅ Strong close | 50%+ profit captured — industry standard exit (TastyTrade backtested). |
| **3.0–4.0** | 👍 Good | 30%+ profit captured, healthy position, optimal DTE. Hold or monitor. |
| **4.0–5.0** | 😐 Neutral | Default starting score. No issues, no urgency. Standard hold. |
| **5.0–6.0** | ⚠️ Watch | Underwater, near expiry, elevated delta, or approaching earnings. |
| **6.0–7.0** | 🔴 Problem | Multiple risk factors: underwater + near expiry + ITM. Needs action. |
| **8.0–10.0** | 🚨 Emergency | Stacked risk: ITM + expiring + earnings blackout + wide spread. |

### How `portfolio_check.py` Scores Existing Positions

Starts at **5.0 (neutral)**, then adjusts:

```
Score starts at 5.0
  -2.0  if profit ≥ 70%       → 3.0   CLOSE — risk/reward inverted
  -1.5  if profit ≥ 50%       → 3.5   CLOSE — industry standard exit
  -0.5  if profit ≥ 30%       → 4.5   HOLD — good progress
  +1.0  if DTE ≤ 7            → 6.0   near expiry gamma risk
  +1.0  if underwater         → 6.0+  losing position
  +1.5  if ITM (Δ > 0.50)     → 6.5+  assignment likely
  +1.0  if earnings in DTE    → 6.0+  binary event risk
```

**Existing positions score higher (worse) by design.** A position held for 3 weeks has already captured most of its theta — the remaining risk/reward is worse than a fresh entry. This is the T-21 management principle: close at 50%, redeploy into fresh 45 DTE.

### How `screener.py` Scores New Candidates

Built from scratch across 5 weighted dimensions, then adjusted per contract:

```
ticker_score = (technical × 0.25) + (options_quality × 0.25)
             + (fundamental × 0.15) + (external_sentiment × 0.20)
             + (macro_risk × 0.15)

final_score = ticker_score + contract_penalty

Contract penalties:
  -0.5  DTE 30-45           bonus for sweet spot
  +1.5  DTE 14-21           penalty for short duration
  +99   DTE < 7              hard block — gamma explosion
  +1.5  OI < 100             low liquidity
  +2.0  bid-ask > 5%         wide spread
  -1.5  RoC > 24%            reward high return
```

**New candidates score lower (better) because they start fresh** — full theta curve ahead, optimal DTE, no bagged P&L.

### Why Your Portfolio Shows 4–5 While Screener Shows 1–3

This is **expected and correct.** The screener finds fresh entries at peak conditions (DTE 30-45, good IV, tight spreads). Your existing positions have already decayed — some are winners ready to close, some are neutral holds, one is underwater. The system is telling you: **close the 50%+ winners, free up capital, redeploy into fresh 1–3 score candidates.**

---

## Database — `db/options.db`

Single SQLite file. All tables in one place. No fragmentation.

| Table | Purpose | Populated By |
|-------|---------|-------------|
| `portfolio_snapshots` | Fund history (total_assets, cash, buying_power) | `portfolio.py sync` |
| `positions` | Current stock + option holdings with P&L | `portfolio.py sync` |
| `local_trades` | Auto-synced executed orders from moomoo | `portfolio.py sync` |
| `trade_log` | Trade lifecycle: recommendations → fills → P&L | `daily_run.py` |
| `daily_runs` | Run metadata (VIX, regime, yields, macro) | `daily_run.py` |
| `run_signals` | Screener output per run | `daily_run.py` |
| `run_positions` | Position health per run | `daily_run.py` |
| `run_chains` | Options chain snapshots (backtest-ready) | `daily_run.py --archive-chains` |
| `iv_history` | Daily IV per ticker (IV Rank source) | `iv_history.py` |

Run daily — builds your own historical options dataset for backtesting.

---

## Library Modules

### `src/analysis/sentiment.py` — Shared Sentiment

Used by `screener.py`, `portfolio_check.py`, `daily_run.py`, and `market_sentiment.py`. Single source of truth — no duplicate API calls.

| Function | Returns | Description |
|----------|---------|-------------|
| `get_macro_context(client)` | `MacroData` | VIX, VVIX, DXY, yields, credit spreads, regime |
| `get_ticker_sentiment(client, ticker)` | `TickerSentiment` | Analyst ratings, earnings, news score |
| `get_watchlist_sentiment(client, tickers)` | `WatchlistSentiment` | Macro + all tickers in one call |
| `score_analyst_consensus(consensus)` | `float (0-1)` | STRONG_BUY=1.0, HOLD=0.5, STRONG_SELL=0.0 |
| `score_news_sentiment(score)` | `float (0-1)` | Normalize 1-100 news score to 0-1 |
| `score_earnings_blackout(dte)` | `(bool, float)` | Blackout check + penalty |

### `src/data/portfolio_db.py` — PortfolioDB

CRUD for `portfolio_snapshots`, `positions`, `local_trades` tables. Methods: `save_funds()`, `save_positions()`, `log_trade()`, `close_trade()`, `get_portfolio_summary()`, `get_funds_history()`.

### `src/data/portfolio_sync.py` — PortfolioSync

Polls REAL moomoo account (`accinfo_query`, `position_list_query`, `order_list_query`). Auto-matches executed orders to local trades. Read-only — never submits.

---

## Data Flow

```
┌─────────────────────────────────────────────────────┐
│                    DATA LAYER                         │
│                                                       │
│  portfolio.py sync (cron or on-demand)                │
│    ├── moomoo REAL account ← accinfo_query            │
│    ├── moomoo REAL account ← position_list_query      │
│    └── moomoo REAL account ← order_list_query         │
│              │                                        │
│              ▼                                        │
│  db/options.db (9 tables, SQLite)                     │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────┴──────────────────────────────┐
│                  ANALYSIS LAYER                       │
│                                                       │
│  daily_run.py (orchestrator)                          │
│    ├── src/analysis/sentiment.py (macro + sentiment)  │
│    ├── screener.py (watchlist → CC/CSP candidates)    │
│    ├── portfolio_check.py (positions → decisions)     │
│    ├── src/scoring/ (WHEEL_SCORE)                     │
│    └── src/risk/ (collar check, position monitor)     │
│              │                                        │
│              ▼                                        │
│  Console output + DB tables (run_signals,             │
│  run_positions, trade_log)                            │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────┴──────────────────────────────┐
│                 EXECUTION LAYER                       │
│                                                       │
│  👤 MANUAL — user reviews suggestions,                │
│     submits orders in moomoo desktop app               │
│                                                       │
│  portfolio.py sync picks up fills next run             │
└─────────────────────────────────────────────────────┘
```

---

## Investment Rules

1. **Wheel strategy only**: Covered Calls (must own 100 shares) and Cash Secured Puts (must hold cash)
2. **Capital preservation first** — earn premium, let stocks assign if needed
3. **Never trade purely on premium** — screen strike, expiry, macro, and technicals first
4. **30% margin max**, hard 15-day window to clear
5. **Document every position** — DB tracks everything automatically

Full rules in [GOAL.md](GOAL.md) Actions. **Always reference GOAL.md before making trade recommendations.**

## Decision Framework

### Layer 1: Macro Regime Gate → Position Sizing

5-condition voting (VIX + yield curve + credit + DXY + VVIX) determines regime, position size, and delta.

| Regime | VIX | Size | Cash Reserve | CSP Delta | CC Delta | CSP:CC Ratio |
|--------|-----|:---:|:---:|:---:|:---:|:---:|
| BULLISH | < 15 | 100% | ≥ 15% | 0.20-0.30 | 0.20-0.30 | 60:40 |
| NEUTRAL | 15-20 | 75% | ≥ 20% | 0.20-0.30 | 0.20-0.30 | 50:50 |
| CAUTIOUS | 20-25 | 50% | ≥ 25% | 0.15-0.25 | 0.25-0.35 | 30:70 |
| VOLATILE | 25-30 | 25% | ≥ 30% | 0.10-0.20 | 0.30-0.40 | 10:90 |
| BEARISH | > 30 | 0% | ≥ 35% | NONE | existing only | 0:100 |

### CSP Pause Triggers (stop new CSPs immediately if ANY true)

- VIX > 25 | SPY < 200 SMA | Regime ≤ -2 | Cash reserve < 20% | Stock > 15% below cost basis

### Layer 2: Position Limits

| Rule | Limit | Type |
|------|-------|------|
| Single stock | ≤ 15% of net liq | 🔴 BLOCK |
| Sector concentration | ≤ 25% of portfolio | 🟡 WARN |
| CSP capital deployed | ≤ 25% of net liq (≤ 10% volatile) | 🔴 BLOCK |
| Cash reserve | Per regime table | 🔴 BLOCK |
| Open positions | ≤ 8 total | 🟡 WARN |
| Watchlist diversification | ≥ 3 sectors | 🟡 WARN |

### Layer 3: Ticker Risk Gates + Scoring

14 hard constraints + VRP, GEX, concentration, earnings gates. 5-dimension ticker score + contract penalty. See [GOAL.md](GOAL.md) for pre-trade checklist.

### T-21 Management

At 21 DTE: if position is tested and rolling pays net credit → roll to 45 DTE. Close at 50% profit regardless of DTE. Never sell CC below cost basis.

---

## Data Sources

| Source | What | Key Required? |
|--------|------|:---:|
| Moomoo OpenD | Price, Greeks, OI, volume, portfolio, orders | No (local) |
| Yahoo Finance | Analyst ratings, earnings, institutional, news, VIX, yields | No |
| alternative.me | Fear & Greed Index | No |

## Tests

```bash
pytest tests/ -v --tb=short
pytest tests/ --cov=src --cov-report=term
```
