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

# 6. Paper trading engine (validate strategy before live execution)
python3 scripts/oie_engine.py init          # Once: seed paper portfolio
python3 scripts/oie_engine.py once          # Run one cycle, see what it does
python3 scripts/oie_engine.py status        # Check paper portfolio anytime
python3 scripts/oie_engine.py run           # Continuous mode (30-min cycles)
```

**Prerequisites**: OpenD running on `127.0.0.1:11111`. Install: `pip3 install moomoo-api yfinance pandas`.

**Two databases, zero confusion:**
- `db/options.db` — your **real** portfolio mirror (synced from moomoo)
- `db/oie_paper.db` — your **paper** trading portfolio (simulated by the engine)

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
│   ├── options.db                     # REAL portfolio mirror (9 tables)
│   └── oie_paper.db                   # Paper trading portfolio (4 tables, separate)
├── config/
│   └── rules.yaml                     # Master config: all parameters, thresholds, limits
├── src/
│   ├── data/
│   │   ├── models.py                  # Dataclasses: StockSnapshot, OptionSnapshot
│   │   ├── moomoo_client.py           # Moomoo OpenD data client (quotes + chains)
│   │   ├── yfinance_client.py         # Yahoo Finance: analyst, earnings, news, macro
│   │   ├── compute.py                 # Deterministic indicators (RSI, MACD, Greeks, GEX)
│   │   ├── iv_history.py              # IV rank persistence
│   │   ├── trade_log.py               # TradeLog + DailyRunDB
│   │   ├── portfolio_db.py            # PortfolioDB (funds, positions, local trades)
│   │   ├── portfolio_sync.py          # PortfolioSync (poll REAL account, sync orders)
│   │   ├── oie_db.py                  # OIE paper portfolio DB (separate from real)
│   │   └── guardrails.py              # Portfolio size/risk limits (shared)
│   ├── analysis/
│   │   ├── sentiment.py               # Shared: macro context + ticker sentiment
│   │   ├── trend.py                   # Trend/momentum indicators
│   │   ├── options_chain.py           # Options chain analysis
│   │   └── correlation.py             # Correlation vs V
│   ├── scoring/                       # WHEEL_SCORE engine (deterministic)
│   ├── signals/                       # Signal generator + sentiment scoring
│   ├── risk/                          # Collar check, position monitor
│   └── config.py                      # Typed config loader from rules.yaml
├── scripts/
│   ├── portfolio.py                   # Portfolio: sync / status / summary / history
│   ├── daily_run.py                   # Daily pipeline: sync + screen + check + log
│   ├── screener.py                    # Watchlist screener: CC + CSP candidates
│   ├── portfolio_check.py             # Position health: score all holdings
│   ├── oie_engine.py                  # OIE: paper trading engine (init/run/once/status)
│   ├── market_data.py                 # Adhoc: single ticker deep dive
│   └── market_sentiment.py            # Adhoc: macro + analyst + earnings + news
├── tests/
│   ├── test_oie_db.py                 # OIE paper DB tests (27 tests)
│   ├── test_screener_scoring.py       # Screener scoring tests (28 tests)
│   ├── test_scoring.py                # WHEEL_SCORE tests
│   ├── test_constraints.py            # Constraint gate tests
│   ├── test_trend.py                  # Trend/momentum formula tests
│   └── ...                            # Full test suite (328 tests)
├── specs/                             # Research + architecture docs
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

### `oie_engine.py` — Options Income Engine (Paper Trading)

Autonomous paper trading engine. Runs the screener, applies decisions to a local simulated portfolio, and tracks P&L over time. **Completely separate from your REAL moomoo account** — it never touches real money. Designed to validate the strategy before live execution.

```bash
python3 scripts/oie_engine.py init         # Seed paper portfolio from REAL account
python3 scripts/oie_engine.py run          # Continuous loop (default: every 30 min)
python3 scripts/oie_engine.py once         # Run a single cycle
python3 scripts/oie_engine.py status       # Show paper portfolio + open positions
python3 scripts/oie_engine.py history      # Show P&L snapshots over time
python3 scripts/oie_engine.py reset        # Wipe paper portfolio
```

**Options:**

| Command | Needs OpenD? | Description |
|---------|:---:|-------------|
| `init` | ✅ | Copies REAL stock holdings + cash into paper portfolio. Does NOT copy existing options — paper starts fresh with only stocks + cash |
| `run` | ✅ | Continuous loop. Runs a full cycle every N minutes. Ctrl+C to stop gracefully |
| `run --interval N` | ✅ | Set cycle interval in minutes (default: 30) |
| `run --skip-closed` | ✅ | Skip cycles when US market is closed (default: ON for `run`) |
| `run --force` | ✅ | Run even if market is closed |
| `once` | ✅ | Single cycle: marks positions to market, checks exits, screens new trades, executes paper trades, takes snapshot |
| `once --dry-run` | ✅ | Screen + guardrails WITHOUT modifying DB. Shows what WOULD happen |
| `once --skip-closed` | ✅ | Skip if market is closed |
| `once --force` | ✅ | Run even if market is closed |
| `test` | ❌ | Self-check: validates DB schema, config, scoring imports, guardrails (no OpenD) |
| `status` | ❌ | Reads paper DB. Shows stocks, options, P&L, cash buffer, recent events |
| `history` | ❌ | Shows portfolio value snapshots over time |
| `reset --force` | ❌ | Wipes all paper data for a fresh start |

#### How It Works

Each cycle runs **8 phases** in order:

```
┌─────────────────────────────────────────────────────────┐
│  PHASE 1: Load State                                     │
│  Reads paper cash, positions, cycle count from DB        │
├─────────────────────────────────────────────────────────┤
│  PHASE 2: Mark-to-Market (moomoo live prices)            │
│  Batch stock snapshots + option snapshots → update DB    │
├─────────────────────────────────────────────────────────┤
│  PHASE 3: Check Exits                                    │
│  For each ACTIVE option:                                 │
│    profit_captured ≥ 70% → CLOSE immediately             │
│    profit_captured ≥ 50% → CLOSE (TastyTrade exit)       │
│    DTE ≤ 0 → EXPIRE or ASSIGN (based on ITM/OTM)        │
│    DTE ≤ 3 → flag warning                                │
├─────────────────────────────────────────────────────────┤
│  PHASE 4: Screen New Opportunities                       │
│  Reuses screener.py scoring engine:                      │
│    → Batch snapshots for all watchlist tickers            │
│    → Fetch option chains (optimal DTE first, then expand)│
│    → _compute_ticker_score + _contract_penalty            │
│    → Filter: delta, OI, volume, RoC, IV, VRP gate        │
│    → Rank by score, dedup to 1 per ticker per strategy    │
├─────────────────────────────────────────────────────────┤
│  PHASE 5: Apply Guardrails                               │
│  Same GuardrailChecker as live portfolio:                 │
│    → Max 15% per ticker (concentration)                   │
│    → Cash buffer ≥ 25%                                   │
│    → Max 8 open positions                                 │
│    → Max 2 new trades per day                             │
│    → CSP capital ≤ 80% of cash                            │
├─────────────────────────────────────────────────────────┤
│  PHASE 6: Execute Paper Trades                           │
│  Top 1-2 candidates that pass all gates:                  │
│    → Open position in paper DB                            │
│    → Update cash balance (add premium)                    │
│    → Log OPEN_CSP or OPEN_CC event                        │
├─────────────────────────────────────────────────────────┤
│  PHASE 7: Snapshot                                       │
│  Records net liquidation value:                           │
│    cash + stock_MV + option_premium − option_liability    │
├─────────────────────────────────────────────────────────┤
│  PHASE 8: Log + Sleep                                    │
│  Increments cycle count, logs completion, sleeps          │
└─────────────────────────────────────────────────────────┘
```

#### Paper Position Lifecycle

Every option position in the paper portfolio follows this state machine:

```
                    ┌─────────┐
                    │  ACTIVE  │ ← opened by engine (OPEN_CSP / OPEN_CC)
                    └────┬────┘
           ┌─────────────┼─────────────┐
           ▼             ▼             ▼
     ┌─────────┐   ┌──────────┐  ┌──────────┐
     │ CLOSED  │   │ EXPIRED  │  │ ASSIGNED │
     │(bought  │   │  (OTM at │  │(ITM at   │
     │ back at  │   │ expiry)  │  │ expiry)  │
     │ profit)  │   │          │  │          │
     └─────────┘   └──────────┘  └────┬─────┘
           │              │           │
           ▼              ▼           ▼
      P&L = (entry    P&L = full   CSP → add 100 shares
      − exit) ×       premium      at strike price
      contracts       kept          CC → remove 100
      × 100                         shares at strike
```

**Exit triggers (checked every cycle):**

| Trigger | Action | P&L Calculation |
|---------|--------|-----------------|
| Profit ≥ 70% | Close immediately | `(entry_premium − current_bid) × contracts × 100` |
| Profit ≥ 50% | Close | Same — TastyTrade backtested optimal exit |
| DTE ≤ 0 + OTM | Expire | Full premium kept: `entry_premium × contracts × 100` |
| DTE ≤ 0 + ITM (CSP) | Assign | Stock added at `strike − premium` effective cost |
| DTE ≤ 0 + ITM (CC) | Assign | Stock removed at strike, P&L = `(strike − cost_basis + premium) × 100` |

#### Paper Portfolio Database

The OIE uses its own SQLite DB at `db/oie_paper.db` — completely separate from `db/options.db`.

| Table | Purpose |
|-------|---------|
| `paper_state` | Key-value store: cash balance, fund value, cycle count, last run time |
| `paper_positions` | Stock + option positions with full lifecycle tracking (status, entry/exit, P&L, Greeks) |
| `paper_trades` | Audit trail: every action logged with timestamp, event type, ticker, cash impact |
| `paper_snapshots` | Time-series portfolio value: one row per cycle for P&L charting |

**Query paper data directly:**
```bash
# See all paper positions
sqlite3 db/oie_paper.db "SELECT ticker, pos_type, status, entry_premium, realized_pnl FROM paper_positions"

# See recent audit events  
sqlite3 db/oie_paper.db "SELECT ts, event, ticker, detail FROM paper_trades ORDER BY id DESC LIMIT 20"

# See P&L over time
sqlite3 db/oie_paper.db "SELECT ts, total_value, cash, unrealized_pnl, realized_pnl_total FROM paper_snapshots"
```

#### Scheduling

**Cron (every 30 minutes during market hours):**
```bash
*/30 9-16 * * 1-5 cd ~/options && python3 scripts/oie_engine.py once >> logs/oie.log 2>&1
```

**Continuous mode (Hermes agent or tmux session):**
```bash
python3 scripts/oie_engine.py run --interval 30
```

**Manual ad-hoc:**
```bash
python3 scripts/oie_engine.py once   # Run one cycle, see what happened
python3 scripts/oie_engine.py status # Check paper portfolio anytime
```

#### Managing the Paper Engine

**Starting fresh:**
```bash
python3 scripts/oie_engine.py reset --force   # Wipe everything
python3 scripts/oie_engine.py init            # Seed from current REAL portfolio
python3 scripts/oie_engine.py run --interval 30  # Start running
```

**Checking progress:**
```bash
python3 scripts/oie_engine.py status   # Current paper positions + P&L
python3 scripts/oie_engine.py history  # Value over time
```

**Comparing paper vs real:**
```bash
python3 scripts/portfolio_check.py     # Real portfolio health
python3 scripts/oie_engine.py status   # Paper portfolio health
# Compare: which made better decisions? Did paper catch exits that real missed?
```

**Stopping gracefully:**
```bash
# If running in continuous mode: Ctrl+C
# Engine finishes current cycle before stopping
# All state persisted — restart picks up where it left off
```

**Resuming after restart:**
```bash
python3 scripts/oie_engine.py run      # Reads last state from DB, continues
```

#### Performance

With optimizations (batch snapshots, cached price history, cached option chains, tiered DTE screening):
- **~107 seconds per cycle** for 19 watchlist tickers
- **~6% duty cycle** at 30-minute interval
- **~75 API calls** per cycle (down from ~200 originally)
- All caches cleared on engine restart — fresh data each session

#### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Separate DB (`oie_paper.db`) | Never pollutes real portfolio data. Easy to wipe and restart |
| Paper cash = us_cash + fund | Real account has margin (negative cash) offset by money market fund. Paper combines them into one positive pool |
| Options NOT copied at seed | Paper starts fresh — engine makes its own decisions from scratch |
| Same scoring as screener | Imports `_compute_ticker_score`, `_contract_penalty` directly from `scripts/screener.py`. Same math, same thresholds |
| Same guardrails as live | `GuardrailChecker` pointed at paper portfolio. Same 15% concentration, 25% cash buffer limits |
| Max 2 new trades per cycle | Prevents overtrading. Configurable via `config/rules.yaml` |
| No yfinance fallback for options | Option chains come from moomoo only. yfinance is for sentiment/macro only |

#### Validating the Engine (Step-by-Step)

Run these commands in order. Each builds on the previous.

```bash
# STEP 1: Self-test (no OpenD needed — works anytime)
python3 scripts/oie_engine.py test
# Expected: "✅ ALL CHECKS PASSED"

# STEP 2: Seed the paper portfolio
python3 scripts/oie_engine.py reset --force
python3 scripts/oie_engine.py init
# Expected: 14 stocks seeded with actual cost basis + cash pool

# STEP 3: Check status (no OpenD needed)
python3 scripts/oie_engine.py status
# Expected: 14 stocks, 0 options, Net Liq Value ~$232K

# STEP 4: Dry-run — see what the engine WOULD do (doesn't modify DB)
python3 scripts/oie_engine.py once --dry-run --force
# Expected: Shows candidates found, guardrail checks, trades it would open
#            Says "DRY RUN — no changes will be written to DB"
#            Position count stays at 14

# STEP 5: Run a real cycle (requires OpenD + market hours or --force)
python3 scripts/oie_engine.py once --force
# Expected: Opens 1-2 CSP trades, shows premium collected

# STEP 6: Verify trades were recorded
python3 scripts/oie_engine.py status
# Expected: 16 positions (14 stocks + 2 options)

# STEP 7: Check history
python3 scripts/oie_engine.py history
# Expected: Shows snapshot(s) with total value, cash, P&L

# STEP 8: Verify data directly
sqlite3 db/oie_paper.db "SELECT ticker, pos_type, status, entry_premium FROM paper_positions WHERE pos_type != 'STOCK'"
# Expected: Shows your paper option positions

# STEP 9: Run second cycle — verify no duplicates
python3 scripts/oie_engine.py once --force
# Expected: "New trades: 0" (existing options block duplicates)

# STEP 10: Compare paper vs real
python3 scripts/portfolio_check.py --no-external   # Real portfolio
python3 scripts/oie_engine.py status               # Paper portfolio
# Compare: Did paper catch exits real missed? Different candidates?
```

#### Cron Setup (Automatic)

Set up these cron entries to run the engine automatically during US market hours.

```bash
# Edit crontab
crontab -e

# Add these lines:

# OIE paper engine — every 30 min during market hours (Mon-Fri)
# Times are in YOUR LOCAL timezone. Adjust if not US Eastern.
# The --skip-closed flag checks market hours and skips if closed
*/30 9-16 * * 1-5 cd ~/options && python3 scripts/oie_engine.py once --skip-closed >> logs/oie.log 2>&1

# Portfolio sync — once after market close
30 16 * * 1-5 cd ~/options && python3 scripts/portfolio.py sync >> logs/portfolio.log 2>&1

# Full daily run — after sync
0 17 * * 1-5 cd ~/options && python3 scripts/daily_run.py --top 10 >> logs/daily.log 2>&1
```

**How `--skip-closed` works:**
- Checks current day (Mon-Fri) and time against US market hours (9:30 AM - 4:00 PM ET)
- Automatically detects EDT (summer, UTC-4) vs EST (winter, UTC-5)
- On weekends: prints "Market closed — Saturday/Sunday" and exits
- Outside hours: prints "Market closed — pre-open/after hours" and exits
- With `--force`: skips the check and runs anyway (for testing)

**Alternative: Continuous mode (tmux/screen session):**
```bash
# Start a persistent session
tmux new -s oie
python3 scripts/oie_engine.py run --interval 30 --skip-closed

# Detach: Ctrl+B, D
# Reattach: tmux attach -t oie
# Stop: Ctrl+C (graceful — finishes current cycle first)
```

#### GenAI / Hermes — Do You Need It?

**No.** The OIE engine is fully deterministic. All decisions are formula-driven:

| What | How |
|------|-----|
| Ticker scoring | 5-dimension weighted formula (`_compute_ticker_score`) |
| Contract selection | Delta, OI, volume, RoC, IV, VRP thresholds |
| Exit decisions | Profit % captured, DTE countdown |
| Guardrails | Position sizing, concentration, cash buffer (all arithmetic) |
| Market regime | VIX + yield curve + credit + DXY + VVIX voting |

**Where GenAI COULD add value (optional, not required):**

| Use Case | How |
|----------|-----|
| **Daily paper P&L summary** | Feed `oie_engine.py history` output to Claude/Hermes for a natural-language summary of what the paper engine did |
| **Paper vs real diff** | Compare paper and real portfolio outputs, ask AI to explain why they diverged |
| **Earnings/event context** | Before a major earnings or FOMC, ask AI whether the engine should pause CSPs |
| **Weekly strategy review** | Feed a week of paper snapshots to AI for pattern analysis: "Did the 50% exit rule work? Should we adjust DTE range?" |

**When NOT to use GenAI:** Never let AI compute scores, check constraints, or generate trade signals. Those must stay deterministic — the AI boundary is narrative only.

#### Testing Without Affecting Data

```bash
# Dry-run: sees what the engine would do, writes NOTHING to DB
python3 scripts/oie_engine.py once --dry-run --force

# Use this to:
#   - Test new config changes (edit config/rules.yaml, then dry-run)
#   - See if a new ticker would be picked up
#   - Debug scoring: why is/isn't a ticker getting trades?
#   - Check guardrails: what's blocking a candidate?

# After dry-run, your DB is unchanged — verify:
python3 scripts/oie_engine.py status  # Same positions as before
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
┌──────────────────────────────────────────────────────────────┐
│                      DATA LAYER                               │
│                                                                │
│  portfolio.py sync (cron or on-demand)                         │
│    ├── moomoo REAL account ← accinfo_query                     │
│    ├── moomoo REAL account ← position_list_query               │
│    └── moomoo REAL account ← order_list_query                  │
│              │                                                 │
│              ▼                                                 │
│  db/options.db (REAL portfolio mirror, 9 tables)               │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────┴───────────────────────────────────────┐
│                    ANALYSIS LAYER                              │
│                                                                │
│  daily_run.py (orchestrator)                                   │
│    ├── src/analysis/sentiment.py (macro + sentiment)           │
│    ├── screener.py (watchlist → CC/CSP candidates)             │
│    ├── portfolio_check.py (positions → decisions)              │
│    ├── src/scoring/ (WHEEL_SCORE)                              │
│    └── src/risk/ (collar check, position monitor)              │
│              │                                                 │
│              ▼                                                 │
│  Console output + DB tables (run_signals, trade_log)           │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────┴───────────────────────────────────────┐
│                PAPER TRADING LAYER (OIE)                       │
│                                                                │
│  oie_engine.py (autonomous paper engine)                       │
│    ├── Seed: copies REAL stocks + cash → db/oie_paper.db       │
│    ├── Cycle: mark-to-market, check exits, screen → execute    │
│    ├── Reuses: screener scoring + guardrails + moomoo client   │
│    └── Logs: every action to paper_trades audit trail          │
│              │                                                 │
│              ▼                                                 │
│  db/oie_paper.db (paper portfolio, 4 tables, SEPARATE)         │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────┴───────────────────────────────────────┐
│                   EXECUTION LAYER                              │
│                                                                │
│  👤 MANUAL — user reviews suggestions from BOTH:               │
│     • screener.py / portfolio_check.py (real portfolio)        │
│     • oie_engine.py status (paper portfolio decisions)         │
│     Submits orders in moomoo desktop app                        │
│                                                                │
│  portfolio.py sync picks up fills next run                     │
└────────────────────────────────────────────────────────────────┘
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
