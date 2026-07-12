# Options Wheel Strategy — Deterministic Trading Engine

Covered Call & Cash Secured Put screening, scoring, and portfolio management.

**Hard constraints**: no naked options, no margin trading, no spreads. CC requires 100 shares owned. CSP requires full cash coverage.

**No script submits orders to moomoo.** All trades executed manually.

---

## Quick Start

```bash
# 1. Check real portfolio health (reads moomoo directly — no DB needed)
python3 scripts/portfolio_check.py

# 2. Screen for best option trades (CC + CSP)
python3 scripts/screener.py --top 10 --force

# 3. Paper trading engine (validate strategy before live execution)
python3 scripts/oie_engine.py init          # Once: seed paper portfolio from REAL
python3 scripts/oie_engine.py once --force  # Run one cycle, see what it does
python3 scripts/oie_engine.py status        # Check paper portfolio anytime
python3 scripts/oie_engine.py run           # Continuous mode (30-min cycles)

# 4. Adhoc research
python3 scripts/market_data.py NVDA --options
python3 scripts/market_sentiment.py
```

**Prerequisites**: OpenD running on `127.0.0.1:11111`. Install: `pip3 install moomoo-api yfinance pandas`.

**One database, one engine:**
- `db/oie_paper.db` — your **paper** trading portfolio (simulated by the OIE engine)
- Real portfolio is read directly from moomoo — no local mirror needed

---

## Architecture

```
Moomoo REAL account (read-only poll)
       │
       ├── portfolio_check.py ──→ Real portfolio health (direct moomoo read)
       ├── screener.py ──→ Watchlist screening (direct moomoo read)
       │
       └── oie_engine.py ──→ db/oie_paper.db
             ├── paper_positions   (option positions + lifecycle)
             ├── paper_trades      (full audit trail)
             ├── paper_snapshots   (P&L over time)
             └── paper_state       (engine resume state)
```

**Key principle**: Scripts poll moomoo directly, analyze, and simulate. Never submit orders. One DB for paper trading only.

---

## Project Structure

```
options/
├── db/
│   └── oie_paper.db                   # Paper trading portfolio (4 tables)
├── config/
│   └── rules.yaml                     # Master config: all parameters, thresholds, limits
├── src/
│   ├── data/
│   │   ├── models.py                  # Dataclasses: StockSnapshot, OptionSnapshot
│   │   ├── moomoo_client.py           # Moomoo OpenD data client (quotes + chains)
│   │   ├── yfinance_client.py         # Yahoo Finance: analyst, earnings, news, macro
│   │   ├── compute.py                 # Deterministic indicators (RSI, MACD, ADX, HV)
│   │   ├── oie_db.py                  # OIE paper portfolio DB
│   │   └── guardrails.py              # Portfolio size/risk limits (shared)
│   ├── analysis/
│   │   └── sentiment.py               # Shared: macro context + ticker sentiment
│   ├── config.py                      # Typed config loader from rules.yaml
│   └── logging_setup.py               # Shared logging → logs/options.log
├── scripts/
│   ├── portfolio_check.py             # Real portfolio health (moomoo direct read)
│   ├── screener.py                    # Watchlist screener: CC + CSP candidates
│   ├── oie_engine.py                  # OIE: paper trading engine (init/run/once/status/sim)
│   ├── market_data.py                 # Adhoc: single ticker deep dive
│   └── market_sentiment.py            # Adhoc: macro + analyst + earnings + news
├── tests/
│   ├── test_oie_db.py                 # OIE paper DB tests (27 tests)
│   ├── test_oie_simulation.py         # OIE simulation + lifecycle tests (19 tests)
│   ├── test_screener_scoring.py       # Screener scoring tests (28 tests)
│   └── ...                            # 316 tests total
├── specs/                             # Research + architecture docs
├── CLAUDE.md                          # AI coding instructions
├── GOAL.md                            # Investment goals
└── README.md                          # This file
```

---

## Scripts

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
```

#### Dimension 1: Technical (25% weight)

Evaluates whether the stock's price action supports premium selling. All indicators computed deterministically from OHLCV data — no AI.

| Indicator | What it measures | Scoring logic |
|-----------|-----------------|---------------|
| **RSI(14)** | Momentum: overbought vs oversold | 45-55 = best (1.0), 40-60 = good (3.0), <30 or >70 = avoid (9.0) |
| **SMA Stack** | Trend alignment: price vs SMA50 vs SMA200 | Price > SMA50 > SMA200 = uptrend (1.0), price < SMA200 = downtrend (9.0) |
| **ADX(14)** | Trend strength (not direction) | >40 = strong trend (1.0), >25 = trending (3.0), <20 = choppy (8.0) |
| **Volume Ratio** | Today's volume vs 30-day average | >1.0× = liquid (1.0), <0.5× = thin (7.0) |

**RSI (Relative Strength Index):** Momentum oscillator on 0-100 scale. Computed as `100 - (100 / (1 + avg_gain_14d / avg_loss_14d))`.
- **70+:** Overbought — stock may pull back. Good for selling CC before a drop.
- **45-55:** Neutral — ideal for premium selling, no directional pressure.
- **30-:** Oversold — stock may bounce. Good for selling CSP before a rise.
- Extreme readings (>80 or <20) get the worst scores — premium selling works best in calm markets.

**HV(30d) — Historical Volatility:** How much the stock actually moved over the last 30 days, annualized. Computed as `std(log_returns_30d) × √252`. Not used directly in the ticker score — instead it powers the **VRP Gate** (Volatility Risk Premium):

```
VRP Gate: IV must > HV(30d) × 0.8
```

This ensures we only sell premium when options are priced above actual volatility. If implied vol is cheaper than historical vol, the contract is skipped — we're not getting paid enough for the risk.

| Stock | HV(30d) | VRP Threshold | Meaning |
|-------|---------|---------------|---------|
| MSFT | 40.1% | IV must > 32.1% | Need IV above 32% to sell |
| META | 54.6% | IV must > 43.7% | Need higher IV — stock is volatile |
| MRVL | 142.0% | IV must > 113.6% | Extreme — most options will fail VRP |

**Volume Ratio:** Today's volume ÷ 30-day average volume. From moomoo snapshot directly — no computation needed.

| Ratio | Score | Meaning |
|-------|-------|---------|
| >1.0× | **1.0** (liquid) | Above-average activity — easy fills |
| 0.7-1.0× | **4.0** (normal) | Typical day, acceptable |
| <0.7× | **7.0** (thin) | Below-average — wider spreads, harder exits |

Volume ratio carries **15% weight** within the Technical dimension. Combined with RSI (35%), Trend (30%), and ADX (20%), it feeds into the technical sub-score.

#### Dimension 2: Options Quality (25% weight)

Measures how tradeable the options chain is.

| Indicator | Good | Bad |
|-----------|------|-----|
| **Bid-Ask Spread** | <0.5% = tight (1.0) | >5% = wide (9.0) |
| **IV Rank** | 30-70 = elevated premium (1.0) | <20 = cheap options (7.0) |
| **Market Cap** | >$500B = mega-cap liquid (1.0) | <$10B = small cap (8.0) |
| **Beta vs SPY** | <1.0 = stable (1.0) | >2.0 = volatile (9.0) |

---

### IV (Implied Volatility) — Complete Reference

IV is the market's expectation of future volatility, derived from option prices. It's the single most important input for premium selling — we get paid for taking volatility risk.

**Source:** Moomoo OpenD provides IV directly per option contract (`option_implied_volatility` field). Shown as a percentage (e.g. IV=38% means the market expects ~38% annualized movement).

IV is used in **4 separate places** across the pipeline:

#### 1. IV Sanity Gate (Stage 4)

```
Rejects: IV ≤ 0  or  IV ≥ 500%
```

Pure data quality check. Moomoo sometimes returns 0 or absurd values for deep OTM / illiquid contracts. These are skipped immediately.

#### 2. VRP Gate — Volatility Risk Premium (Stage 4)

```
Condition: IV > HV(30d) × 0.8

Where HV(30d) = actual volatility over last 30 days (annualized)
```

**What this means:** We only sell options when the market is pricing in MORE volatility than what actually happened. If IV is cheaper than historical vol, we're not getting paid enough for the risk — the contract is skipped.

| Scenario | IV | HV(30d) | VRP Check | Result |
|----------|----|---------|-----------|--------|
| IV rich — good to sell | 38% | 25% | 38% > 20% ✓ | **PASS** |
| IV fairly priced | 30% | 33% | 30% > 26.4% ✓ | **PASS** |
| IV cheap — avoid | 20% | 35% | 20% > 28% ✗ | **SKIP** |

#### 3. Contract Penalty — High IV Bonus (Stage 5)

```
If IV > 35%: penalty += -0.5  (bonus — elevated premium)
```

When IV is elevated (>35%), we get paid more for the same risk. The bonus pulls the final score lower (better), making these contracts rank higher.

| IV Level | Penalty | Meaning |
|----------|---------|---------|
| <35% | 0 | Normal — no adjustment |
| >35% | **-0.5** | Elevated — bonus for selling rich premium |

#### 4. IV Rank — Options Quality Dimension (Stage 3)

```
IV Rank = where current IV sits in its 1-year range (0-100)
  IV at 1Y high → IV Rank = 100
  IV at 1Y low  → IV Rank = 0
```

Computed by `IVHistoryTracker` which persists daily IV data in `db/options.db`.

| IV Rank | Score | What it means |
|---------|-------|---------------|
| **30-70** | 1.0 (best) | IV is in the middle of its range — elevated enough to sell, not panic-level |
| **20-80** | 3.0 (good) | Slightly outside ideal but acceptable |
| **>80** | 5.0 | IV is near 1Y highs — good premium but higher risk |
| **<20** | 7.0 (worst) | IV near 1Y lows — premiums are cheap, not worth selling |

**Why 30-70 is ideal:** At IV Rank 30, IV is above 30% of its historical readings — elevated enough for decent premium. At IV Rank 70, IV is starting to get expensive but not panic-level. Above 80, you're selling into fear (high premium but high risk of getting steamrolled).

#### IV Flow Summary

```
moomoo returns IV per contract (e.g. 38%)
        │
        ├──→ IV Sanity: 0 < 38% < 500% ✓
        ├──→ VRP Gate:   38% > HV(30d)×0.8 ? → pass/skip
        ├──→ IV Rank:    where is 38% in 1Y range? → 1.0-7.0 score
        └──→ IV Bonus:   38% > 35% → -0.5 to contract penalty
```

#### Dimension 3: Fundamental (15% weight)

| Indicator | Good | Bad |
|-----------|------|-----|
| **P/E Ratio (TTM)** | 10-25 = reasonable (1.0) | >60 or negative (8.0) |
| **Dividend Yield** | >2% = income stock (1.0) | 0% = growth only (6.0) |
| **EPS (TTM)** | Positive = profitable (1.0) | Negative = unprofitable (7.0) |

#### Dimension 4: External Sentiment (20% weight)

| Input | Source | Effect |
|-------|--------|--------|
| **Analyst Consensus** | Yahoo Finance | STRONG_BUY → -1.5 to score, SELL → +3.0 |
| **Price Target Upside** | Yahoo Finance | >15% upside → -1.0 bonus |
| **Earnings Blackout** | Yahoo Finance | Within 14 days → +2.0 penalty |
| **Insider Trading** | Yahoo Finance | Net buying → -1.0, net selling → +1.5 |
| **News Sentiment** | Yahoo Finance (keyword-based) | >70 bullish → -1.0, <30 bearish → +2.0 |

#### Dimension 5: Macro/Risk (15% weight)

| Regime | VIX | Score | Effect |
|--------|-----|-------|--------|
| BULLISH | <15 | 2.0 | Favorable — full size |
| NEUTRAL | 15-20 | 3.0 | Normal conditions |
| CAUTIOUS | 20-25 | 4.0 | Reduced size (50%) |
| VOLATILE | 25-30 | 6.0 | Minimal size (25%) |
| BEARISH | >30 | 8.0 | No new positions |

#### Contract Penalty (added to ticker score)

Adjusts for contract-specific characteristics. Lower penalty = better contract.

| Factor | Penalty |
|--------|---------|
| DTE 30-45 (sweet spot) | **-0.5** bonus |
| DTE 14-21 (short) | +1.5 penalty |
| DTE <7 (gamma risk) | **+99** hard block |
| OI <100 (illiquid) | +1.5 penalty |
| Bid-Ask >5% (wide) | +2.0 penalty |
| RoC >24% (high yield) | **-1.5** bonus |
| IV >35% (elevated) | **-0.5** bonus |
| Volume <10 | +2.0 penalty |

final_score = ticker_score + contract_penalty

**New candidates score lower (better) because they start fresh** — full theta curve ahead, optimal DTE, no bagged P&L.

### Why Your Portfolio Shows 4–5 While Screener Shows 1–3

This is **expected and correct.** The screener finds fresh entries at peak conditions (DTE 30-45, good IV, tight spreads). Your existing positions have already decayed — some are winners ready to close, some are neutral holds, one is underwater. The system is telling you: **close the 50%+ winners, free up capital, redeploy into fresh 1–3 score candidates.**

---

## Complete Scoring Pipeline — End to End

Every candidate goes through this 6-stage pipeline. Here's exactly what happens, in order.

```
┌─────────────────────────────────────────────────────────────────────┐
│ STAGE 1: RAW DATA (moomoo OpenD)                                     │
│   Stock snapshot → price, spread, volume_ratio, market cap, PE       │
│   Price history (252d) → OHLCV for RSI, SMA, MACD, ADX, HV, Beta    │
│   Option chain (7-90 DTE) → strike, expiry, bid/ask, delta, IV, OI  │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ STAGE 2: COMPUTE INDICATORS (src/data/compute.py)                    │
│   enrich_stock_snapshot(snap, history, spy_history)                  │
│                                                                      │
│   ┌──────────┬─────────────────┬──────────────────────────────┐     │
│   │ Indicator│ Formula          │ Used in                       │     │
│   ├──────────┼─────────────────┼──────────────────────────────┤     │
│   │ RSI(14)  │ Wilder smoothing │ Technical score (35% weight)  │     │
│   │ SMA 20/  │ Simple moving    │ Trend alignment score         │     │
│   │ 50/200   │ average          │ (30% weight)                  │     │
│   │ MACD     │ 12/26/9 EMA      │ Trend composite               │     │
│   │ ADX(14)  │ +DI/-DI smoothed │ Trend strength (20% weight)   │     │
│   │ HV(30d)  │ std(log_ret)×√252│ VRP Gate (see Stage 3)        │     │
│   │ Beta     │ covariance/var   │ Options quality (25% weight)  │     │
│   │ Bollinger│ 20d SMA ± 2σ     │ Informational only             │     │
│   └──────────┴─────────────────┴──────────────────────────────┘     │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ STAGE 3: TICKER SCORE (1-10, lower = better)                        │
│   _compute_ticker_score(snap, trend_comp, analyst, earnings, ...)   │
│                                                                      │
│   Technical (25%):     RSI(35%) + Trend(30%) + ADX(20%) + Vol(15%)  │
│   Options Eco (25%):   Spread(25%) + IV Rank(25%) + Cap(25%) + β    │
│   Fundamental (15%):   PE(40%) + Dividend(30%) + EPS(30%)            │
│   External (20%):      Analyst + Upside + Blackout + Insider + News  │
│   Macro/Risk (15%):    VIX regime table lookup                       │
│                                                                      │
│   → Output: ticker_score (e.g. 3.0 for MSFT, 4.2 for TSLA)          │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ STAGE 4: CONTRACT FILTERS (hard gates — fail = skip)                 │
│   For each option contract in the chain:                             │
│                                                                      │
│   ┌─────────────────┬──────────────────────┬────────────────────┐    │
│   │ Gate             │ Condition            │ Fails if           │    │
│   ├─────────────────┼──────────────────────┼────────────────────┤    │
│   │ Liquidity        │ bid > 0, OI ≥ 10,    │ Can't enter/exit   │    │
│   │                  │ Vol ≥ 10             │ at fair price      │    │
│   │ Delta (CSP)      │ config range per     │ Too aggressive or  │    │
│   │                  │ regime (e.g. 0.15-   │ too conservative   │    │
│   │                  │ 0.30 for NEUTRAL)    │ for regime         │    │
│   │ Delta (CC)       │ config range per     │ Same — per regime  │    │
│   │                  │ regime               │                    │    │
│   │ IV Sanity        │ 0 < IV < 500%        │ Bad data from API  │    │
│   │ VRP Gate          │ IV > HV(30d) × 0.8  │ Options too cheap  │    │
│   │                  │                      │ vs actual vol      │    │
│   │ GEX Gate (CSP)   │ Chain gamma > 0      │ Dealers short γ,   │    │
│   │                  │                      │ amplifying moves   │    │
│   │ RoC Min (CSP)    │ RoC ≥ 12% annualized │ Not enough return  │    │
│   │ RoC Min (CC)     │ RoC ≥ 8% annualized  │ Not enough return  │    │
│   └─────────────────┴──────────────────────┴────────────────────┘    │
│                                                                      │
│   → Surviving contracts move to Stage 5                              │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ STAGE 5: CONTRACT PENALTY + FINAL SCORE                              │
│   For each contract that passed all gates:                           │
│                                                                      │
│   final_score = ticker_score + contract_penalty                      │
│                                                                      │
│   Contract penalty adjustments:                                      │
│   ┌────────────────────┬────────────────────────────────────────┐    │
│   │ DTE 30-45          │ -0.5 (sweet spot bonus)                │    │
│   │ DTE 14-21          │ +1.5 (short duration penalty)          │    │
│   │ DTE <7              │ +99 (hard block — gamma explosion)     │    │
│   │ OI < 100            │ +1.5 (illiquid)                        │    │
│   │ OI < 500            │ +0.5 (moderate liquidity)              │    │
│   │ Bid-Ask > 5%        │ +2.0 (wide spread)                     │    │
│   │ Bid-Ask > 2%        │ +1.0 (moderate spread)                 │    │
│   │ RoC > 24%           │ -1.5 (high return bonus)               │    │
│   │ RoC > 18%           │ -0.8 (good return bonus)               │    │
│   │ IV > 35%            │ -0.5 (elevated premium bonus)          │    │
│   │ Volume < 50         │ +1.0 (low activity)                    │    │
│   │ Volume < 10         │ +2.0 (illiquid)                        │    │
│   │ Delta < 0.15        │ +0.5 (too far OTM)                     │    │
│   └────────────────────┴────────────────────────────────────────┘    │
│                                                                      │
│   → Output: final_score (e.g. 3.0 + (-0.5) = 2.5)                   │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ STAGE 6: GUARDRAILS + RANKING (src/data/guardrails.py)               │
│                                                                      │
│   1. Dedup: best contract per ticker per strategy (CSP + CC)         │
│   2. Sort all candidates by final_score (lowest = best)              │
│   3. Apply position limits:                                          │
│      ┌─────────────────────┬──────────────────────────────────┐      │
│      │ Single position     │ ≤ 15% of net liquidation value   │      │
│      │ Sector concentration│ ≤ 25% of portfolio               │      │
│      │ CSP capital deployed│ ≤ 25% of NLV                     │      │
│      │ Open positions      │ ≤ 8 total                        │      │
│      │ Daily new trades    │ ≤ 2 per cycle                    │      │
│      │ Cash buffer         │ ≥ 25% recommended, ≥ 10% block   │      │
│      │ CSP capital req     │ ≤ 80% of available cash          │      │
│      └─────────────────────┴──────────────────────────────────┘      │
│   4. Use --force to skip all guardrails                              │
│                                                                      │
│   → Output: TOP N ranked, deduped, guardrail-cleared candidates      │
└─────────────────────────────────────────────────────────────────────┘
```

### Example: How MU CSP $820 Gets Scored (1.3 — Excellent)

Walking through the pipeline for MU on 2026-07-12 (VIX 15.0, CAUTIOUS regime):

```
STAGE 1 — Raw Data (moomoo OpenD)
  Price: $979.30 | Spread: 0.03% | Vol Ratio: 0.66 | PE(TTM): 22.1 | MCap: $1.1T

STAGE 2 — Compute (enrich_stock_snapshot)
  RSI(14)=40.4 | SMA50=$898.92 | SMA200=$464.30 | ADX=46.3 | HV(30d)=116.6% | Beta=3.20

STAGE 3 — Ticker Score (5 dimensions, weighted)
  Technical (25%):
    RSI(40.4)=3.0 (40-60 range)  |  Trend(price>SMA50>SMA200)=1.0
    ADX(46.3)=1.0 (strong trend) |  VolRatio(0.66)=7.0 (below avg)
    → raw = 3.0×0.35 + 1.0×0.30 + 1.0×0.20 + 7.0×0.15 = 2.60
    → weighted = 2.60 × 0.25 = 0.650

  Options Eco (25%):
    Spread(0.03%)=1.0 (tight) | IV Rank(50)=1.0 (sweet spot)
    MCap($1.1T)=1.0 (mega-cap) | Beta(3.20)=9.0 (very volatile ⚠️)
    → raw = 1.0×0.25 + 1.0×0.25 + 1.0×0.25 + 9.0×0.25 = 3.00
    → weighted = 3.00 × 0.25 = 0.750

  Fundamental (15%):
    PE(22.1)=1.0 (reasonable) | Div(0.05%)=5.0 (near-zero) | EPS(+$7.59)=1.0
    → raw = 1.0×0.40 + 5.0×0.30 + 1.0×0.30 = 2.20
    → weighted = 2.20 × 0.15 = 0.330

  External Sentiment (20%) — live yfinance data:
    Consensus=STRONG_BUY (-1.5) + Upside=+51.7% (-1.0) + No blackout + Neutral insider + News=51
    → base 4.0 → 4.0 - 1.5 - 1.0 = 1.5
    → weighted = 1.5 × 0.20 = 0.300

  Macro/Risk (15%):
    Regime=CAUTIOUS → base=4.0 (between NEUTRAL 3.0 and VOLATILE 6.0)
    → weighted = 4.0 × 0.15 = 0.600

  ticker_score = 0.650 + 0.750 + 0.330 + 0.300 + 0.600 = 2.63

STAGE 4 — Contract Filters (for CSP $820, 2026-08-22, DTE=41)
  bid=$50.55 ✓ | OI=1153 ✓ | Vol=108 ✓ | Δ=0.235 (in CAUTIOUS range 0.10-0.25) ✓
  IV=97% sane ✓ | VRP: 97% > 116.6%×0.8(=93.3%) ✓ | RoC=54.9% > 12% ✓

STAGE 5 — Contract Penalty (per-contract adjustments to ticker score)
  DTE=41 (sweet spot 30-45) → -0.5 bonus
  OI=1153 (>500) → no penalty
  Spread=4.92% (>2%, <5%) → +1.0 medium spread penalty
  Delta=0.235 (>0.15) → no penalty
  RoC=54.9% (>24%) → -1.5 high return bonus
  IV=97% (>35%) → -0.5 elevated IV bonus
  Volume=108 (>50) → no penalty
  → total penalty = -0.5 + 0 + 1.0 + 0 + (-1.5) + (-0.5) + 0 = -1.5

  final_score = 2.63 + (-1.5) = 1.13 → rounds to 1.3 ⭐

  Why not 1.0? The remaining drag comes from:
    • Beta 3.20 → 9.0/10 in Options Eco (MU is 3× as volatile as SPY)
    • CAUTIOUS regime → macro base 4.0 (vs 2.0 for BULLISH)
    • Contract spread 4.92% → barely avoided the +2.0 wide-spread penalty
    • Low volume ratio (0.66×) → 7.0 in Technical
    • No dividend (0.05%) → 5.0 in Fundamental

STAGE 6 — Guardrails
  MU not in portfolio → concentration check passes
  CSP capital ($82,000) vs NLV → must be ≤ 15% of net liquidation
  Cash buffer check → applied at runtime per regime rules
```

### Scoring Dependency Map

```
                          ┌─────────────────────────────┐
                          │     FINAL SCORE (1-10)       │
                          │  ticker_score + penalty      │
                          └─────────────┬───────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
    ┌─────────▼─────────┐   ┌──────────▼──────────┐   ┌─────────▼─────────┐
    │  TICKER SCORE      │   │  CONTRACT PENALTY    │   │   GUARDRAILS      │
    │  (5 dimensions)    │   │  (12 adjustments)    │   │   (7 checks)      │
    └─────────┬─────────┘   └──────────┬──────────┘   └─────────┬─────────┘
              │                        │                         │
    ┌─────────┼─────────┐     ┌───────┼────────┐       ┌───────┼────────┐
    │         │         │     │       │        │       │       │        │
    ▼         ▼         ▼     ▼       ▼        ▼       ▼       ▼        ▼
Technical  Options  Fund    DTE     OI     Spread   Concen-  Cash    Daily
 (25%)     Eco (25%)(15%)  bonus  penalty  penalty  tration  buffer  limit
    │         │         │     │       │        │       │       │        │
    ▼         ▼         ▼     ▼       ▼        ▼       ▼       ▼        ▼
  RSI       Spread     PE    -0.5    +1.5     +2.0    15% max  25%    2/day
  SMA       IV Rank    Div   to      to       to               min
  ADX       MCap       EPS   +99     +0.5     +1.0
  VolRatio  Beta                   (OI<500) (spread>2%)
    │                              │
    │                              ▼
    │                         Vol <50 → +1.0
    │                         Vol <10 → +2.0
    │
    ▼
  All computed by
  enrich_stock_snapshot()
  from price history
```

---

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

### `src/data/oie_db.py` — Paper Portfolio DB

CRUD for `paper_positions`, `paper_trades`, `paper_snapshots`, `paper_state` tables. Methods: `open_position()`, `close_position()`, `expire_position()`, `assign_position()`, `get_active_positions()`, `save_snapshot()`.

### `src/data/guardrails.py` — GuardrailChecker

Position sizing, sector concentration, cash buffer, CSP stress test. Shared by screener and OIE engine. Instantiate with any portfolio — real or paper.

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

## How The Screener Works

The screener (`scripts/screener.py`) is the core decision engine. Every trade candidate flows through this pipeline:

```
Watchlist (moomoo) → Stock Snapshot → 5-Dimension Ticker Score
                                         │
                    Option Chain (7-90 DTE) → Per-Contract Filters
                                         │
                    Final Score = Ticker Score + Contract Penalty
                                         │
                    Dedup (best per ticker) → Ranked Top N
```

### Step 1: VIX → Regime → Gates

VIX determines the market regime, which controls everything downstream. Thresholds aligned with standard VIX interpretation:

| VIX | Standard Meaning | Our Regime | Size | CSP Delta | CC Delta | CSP:CC |
|-----|-----------------|------------|:---:|:---:|:---:|:---:|
| < 12 | Extreme complacency | **BULLISH** | 100% | 0.15-0.30 | 0.20-0.30 | 60:40 |
| 12-20 | Normal, healthy | **NEUTRAL** | 75% | 0.15-0.30 | 0.20-0.30 | 50:50 |
| 20-25 | Rising anxiety | **CAUTIOUS** | 50% | 0.10-0.25 | 0.25-0.35 | 30:70 |
| 25-30 | Elevated swings | **VOLATILE** | 25% | 0.05-0.20 | 0.30-0.40 | 10:90 |
| > 30 | Panic, fear | **BEARISH** | 0% | NONE | existing only | 0:100 |

> **How regime is determined:** The regime isn't just VIX alone. `yfinance_client.py` computes a composite `regime_score` from 5 factors: Fear & Greed Index, SPY price vs SMA trend, VIX level, yield curve (10Y-2Y), and credit spreads. Each votes, and the combined score maps to the regime label. This means VIX can be 15 (normally NEUTRAL) but if Fear & Greed is in Extreme Fear and credit spreads are stressed, the composite can push the regime to CAUTIOUS.

**How VIX/Regime flows into scoring:**

| Mechanism | Impact | Example (VIX 15, NEUTRAL) |
|-----------|--------|---------------------------|
| Delta range gate | Narrows/allows option candidates | CSP: only 0.15-0.30 delta pass |
| RoC multiplier | Reduces effective return | 24% RoC × 0.75 = 18% (still passes 12% min) |
| Macro score (15% weight) | Boosts/penalizes ticker score | NEUTRAL = 3.0 × 0.15 = +0.45 to score |
| CSP pause trigger | Hard blocks new CSPs | VIX > 25 → no CSPs. Not triggered at 15. |
| Position sizing | Caps capital per trade | 75% size = tighter capital allocation |

### Step 2: Contract Filters (Per Option)

Each option contract must pass these gates before scoring:

| Gate | Rule | Config Key |
|------|------|------------|
| Bid > 0 | Must have a market price | — |
| OI ≥ 500 | Liquid enough to trade | `options.liquidity.open_interest_min` |
| Volume ≥ 10 | Recent activity | `options.liquidity.volume_min` |
| Delta in range | Per regime (see table above) | `options.delta.csp` / `options.delta.cc` |
| IV sane | 0 < IV < 500% | — |
| VRP gate | IV > 80% of historical vol | — |
| RoC minimum | CSP ≥ 12%, CC ≥ 8% | `options.roc_min` |

### Step 3: 5-Dimension Ticker Score

| Dimension | Weight | What It Measures |
|-----------|:---:|-----------------|
| Technical | 25% | RSI, trend alignment (SMA stack), ADX, volume ratio |
| Options Quality | 25% | Bid-ask spread, IV rank, market cap, beta |
| Fundamental | 15% | P/E ratio, dividend yield |
| External Sentiment | 20% | Analyst consensus, earnings blackout, news score |
| Macro/Risk | 15% | VIX regime (see table), position multiplier |

Base score starts at 5.0. Positive factors subtract (better), negative add (worse). Range: 1 (best) to 10 (worst).

### Step 4: Contract Penalties

Added to ticker score per contract. Rewards good contracts, penalizes risky ones:

| Factor | Penalty/Bonus |
|--------|:---:|
| DTE 30-45 (sweet spot) | **−0.5** bonus |
| DTE < 21 | +1.5 penalty |
| DTE < 7 | **+99** hard block |
| OI < 100 | +1.5 penalty |
| Spread > 5% | +2.0 penalty |
| RoC > 24% | **−1.5** bonus |
| IV > 35% | **−0.5** bonus |

### Step 5: Dedup → Output

- One best candidate per ticker (lowest score wins)
- Skip tickers with existing option positions
- Rank by score ascending → top N

```bash
# Normal run (respects all gates):
python3 scripts/screener.py --top 10

# Show what's mathematically available (skip position/cash limits):
python3 scripts/screener.py --top 10 --ignore-guardrails

# Covered calls only:
python3 scripts/screener.py --cc-only --top 5
```

### Stop-Loss System — Layered Defense

Every option position checked by `portfolio_check.py` runs through two stop-loss layers. **Stop-loss triggers override profit targets** — risk management takes priority over profit taking.

#### Layer 1: Premium Multiple Stop (DTE-Adjusted)

Loss is measured as a multiple of the premium collected. If you sold for $2.00 and the buyback is $6.00, that's a 2× loss.

| DTE | Alert At | Close At | Logic |
|-----|:---:|:---:|-------|
| **> 30 days** | 2× premium | **3× premium** | Plenty of time. Let theta work. |
| **21-30 days** | 1× premium | **2× premium** | Rolling window. Consider rolling for credit. |
| **< 21 days** | 0.5× premium | **1.5× premium** | Gamma risk. Don't let small become catastrophic. |

**Example**: AAPL P300, entry $2.50, DTE 25, current bid $5.00
- Loss = ($5.00 - $2.50) / $2.50 = **1× loss** → ⚠️ STOP ALERT at 21-30 DTE
- If bid rises to $7.50 → 2× loss → 🛑 STOP LOSS — close immediately

#### Layer 2: Delta Gate

Directional risk regardless of P&L. Delta measures assignment probability.

| Strategy | Threshold | Action |
|----------|:---:|--------|
| **CSP** | \|Δ\| ≥ 0.60 | 🛑 DELTA STOP — 60% assignment risk, too directional. Cut it. |
| **CSP** | \|Δ\| ≥ 0.50 | ⚠️ ITM — monitor closely, prepare for assignment |
| **CC** | Δ ≥ 0.50 | ⚠️ DELTA WARN — 50% chance called away. Prepare shares. |

#### Decision Priority (Highest Wins)

```
🛑 STOP LOSS      >  🛑 DELTA STOP     >  ✅ CLOSE (50% profit)
  >  🔄 ROLL       >  ⚠️ ALERT         >  👍 HOLD
```

Loss prevention beats profit taking. A stop-loss trigger at 3× will override a 50% profit close decision.

#### Config

All thresholds in `config/rules.yaml` → `stop_loss:` section. Tweak without code changes:

```yaml
stop_loss:
  premium_stop:
    far_dte: 30, far_alert: 2.0, far_close: 3.0
    mid_dte: 21, mid_alert: 1.0, mid_close: 2.0
    near_alert: 0.5, near_close: 1.5
  delta:
    csp_critical: 0.60, cc_critical: 0.50
```

## Decision Framework

### Layer 1: Macro Regime Gate → Position Sizing

5-condition voting (VIX + yield curve + credit + DXY + VVIX) determines regime, position size, and delta.

| Regime | VIX | Size | Cash Reserve | CSP Delta | CC Delta | CSP:CC Ratio |
|--------|-----|:---:|:---:|:---:|:---:|:---:|
| BULLISH | < 12 | 100% | ≥ 15% | 0.15-0.30 | 0.20-0.30 | 60:40 |
| NEUTRAL | 12-20 | 75% | ≥ 20% | 0.15-0.30 | 0.20-0.30 | 50:50 |
| CAUTIOUS | 20-25 | 50% | ≥ 25% | 0.10-0.25 | 0.25-0.35 | 30:70 |
| VOLATILE | 25-30 | 25% | ≥ 30% | 0.05-0.20 | 0.30-0.40 | 10:90 |
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
