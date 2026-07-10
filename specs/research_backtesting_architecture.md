# Options Backtesting Architecture — Research Report

**Date**: 2026-07-10
**Status**: Research complete, implementation design approved
**Applies to**: Data pipeline split, historical options simulation, backtesting engine

---

## Executive Summary

**Split the data pipeline into a standalone ETL module. Keep it separate from live trading. Use SQLite for storage, Black-Scholes for option price simulation, and a unified execution engine shared between backtest and paper trading.**

The current architecture (`moomoo_client → dataclasses → compute → scoring`) is built for live/current data only. Moomoo provides zero historical options data — only current snapshots. A backtesting system requires a fundamentally different data pipeline: historical price ingestion, option price simulation (or real historical chains from paid sources), and a time-walk engine.

---

## 1. Architecture Decision: Separate or Integrated?

### 1.1 The "Fetch Once, Read Many" Pattern

```
┌──────────────────────────────────────┐
│         INGESTION (Scheduled)         │
│  Cron/Airflow → APIs → SQLite/Parquet │
│  Runs independently, offline capable  │
└──────────────┬───────────────────────┘
               │ reads only
               ▼
┌──────────────────────────────────────┐
│       LIVE TRADING (Interactive)      │
│  Moomoo OpenD → dataclasses → score   │
│  Freshness-gated, real-time           │
└──────────────────────────────────────┘
┌──────────────────────────────────────┐
│       BACKTESTING (Batch)             │
│  SQLite → time-walk → simulate → P&L  │
│  Reproducible, fast, parameter sweeps │
└──────────────────────────────────────┘
```

**Verdict: Separate.** The ingestion pipeline has different:
- Scheduling (cron/batch vs interactive)
- Data sources (historical APIs vs live OpenD)
- Failure modes (gap-fill vs abort-on-stale)
- Scale (years of data vs current snapshot)

Keeping them together creates tight coupling between live trading reliability and backtest data collection — a failure in one should never block the other.

### 1.2 What Lives Where

| Component | Ingest Pipeline | Live Trading | Backtest Engine |
|-----------|----------------|-------------|-----------------|
| Historical OHLCV fetch | ✅ | | reads from DB |
| Historical options chain fetch | ✅ | | reads from DB |
| Live options snapshot | | ✅ | |
| Price history (moomoo kline) | | ✅ | |
| IV history recording | ✅ (daily cron) | | reads from DB |
| Technical indicator compute | | shared lib | shared lib |
| Option price simulation | | | ✅ |
| Strategy execution | | | ✅ |
| Trade submission | | ✅ | |

---

## 2. Historical Options Data: The Core Challenge

### 2.1 Problem

Moomoo OpenD provides **zero historical options data**. You can only get current snapshots. For backtesting the wheel strategy, you need to know what option prices were at specific points in the past.

### 2.2 Solution Tiers

| Tier | Approach | Cost | Accuracy | Effort |
|------|----------|------|----------|--------|
| **1: Black-Scholes Simulation** | Compute theoretical option prices from historical stock data + volatility surface assumptions | Free | Moderate (no bid-ask spread, no real IV surface) | Low |
| **2: yfinance Historical Chains** | Yahoo Finance provides limited historical options data — spotty, unreliable, no Greeks | Free | Low (gaps, survivorship bias) | Low |
| **3: ORATS Historical Data** | Professional options data back to 2007, with cleaned quotes, full Greeks, IV surface | Paid (~$100 deposit for trial) | High | Medium (API + storage) |
| **4: CBOE DataShop** | Official exchange end-of-day options quotes with Greeks | Paid (one-time purchase or subscription) | Highest | Medium |

### 2.3 Recommended Approach: Tier 1 → Tier 3 Progression

**Start with Black-Scholes simulation for rapid prototyping, graduate to ORATS for production backtests.**

Phase 1 (now): Black-Scholes with flat vol assumption
Phase 2 (1-2 months): Black-Scholes with IV surface interpolated from current data
Phase 3 (when capital is at stake): ORATS historical data subscription

---

## 3. Option Price Simulation (Tier 1 — Black-Scholes)

### 3.1 What You Need

For each backtest date and each candidate option:
- Underlying price (from historical OHLCV — available via moomoo kline or yfinance)
- Strike price (you choose this)
- DTE (you choose this)
- Risk-free rate (from Treasury yields)
- Implied volatility (the hard part — see below)

### 3.2 IV Estimation Approaches

| Method | Accuracy | Data Required |
|--------|----------|---------------|
| **Constant IV** (use current IV for all history) | Poor — IV varies massively over time | Current IV only |
| **IV Rank-adjusted** (scale by VIX ratio) | Moderate | Current IV + historical VIX |
| **Historical IV surface** (ORATS/CBOE data) | High | Paid data |

### 3.3 Black-Scholes Implementation

```python
from math import log, sqrt, exp
from scipy.stats import norm

def black_scholes(S, K, T, r, sigma, option_type='CALL'):
    """S=spot, K=strike, T=years to expiry, r=risk-free rate, sigma=IV"""
    if T <= 0:
        return max(0, S - K) if option_type == 'CALL' else max(0, K - S)
    
    d1 = (log(S / K) + (r + sigma**2 / 2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    
    if option_type == 'CALL':
        return S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2)
    else:
        return K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

def compute_greeks(S, K, T, r, sigma):
    """Returns dict of delta, gamma, theta, vega, rho."""
    d1 = (log(S / K) + (r + sigma**2 / 2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    
    delta = norm.cdf(d1)  # for calls; subtract 1 for puts
    gamma = norm.pdf(d1) / (S * sigma * sqrt(T))
    theta = (-S * norm.pdf(d1) * sigma / (2 * sqrt(T))
             - r * K * exp(-r * T) * norm.cdf(d2)) / 365  # daily
    vega = S * norm.pdf(d1) * sqrt(T) / 100  # per 1% IV change
    rho = K * T * exp(-r * T) * norm.cdf(d2) / 100
    
    return {'delta': delta, 'gamma': gamma, 'theta': theta, 
            'vega': vega, 'rho': rho}
```

### 3.4 Known Limitations of Black-Scholes Simulation

| Limitation | Impact | Mitigation |
|-----------|--------|------------|
| Assumes constant IV across strikes | No vol smile/skew → misprices OTM options | Add skew adjustment from current chain |
| No bid-ask spread | Overstates returns by ~5-10% | Apply realistic spread model |
| No early exercise for American options | Overstates put values slightly | Acceptable for OTM CC/CSP |
| No dividend adjustment | Misstates call value near ex-div | Add dividend yield to BS formula |
| No liquidity constraints | Assumes all contracts tradeable | Filter by OI/volume thresholds |

---

## 4. Backtesting Engine Design

### 4.1 Architecture

```
┌─────────────────────────────────────────────┐
│             DATA STORE (SQLite)               │
│  ┌───────────┐ ┌────────────┐ ┌───────────┐  │
│  │ ohlcv_*   │ │ options_*  │ │ backtest_*│  │
│  │ (per tick)│ │ (simulated)│ │ (results) │  │
│  └───────────┘ └────────────┘ └───────────┘  │
└────────────────────┬────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────┐
│          BACKTEST ENGINE                      │
│  ┌──────────┐ ┌──────────┐ ┌─────────────┐  │
│  │TimeWalk  │ │Strategy  │ │Portfolio    │  │
│  │(iterate  │ │(CC/CSP   │ │(positions,  │  │
│  │ dates)   │ │ rules)   │ │ cash, P&L)  │  │
│  └──────────┘ └──────────┘ └─────────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌─────────────┐  │
│  │Simulator │ │Risk      │ │Reporter     │  │
│  │(BS price │ │(drawdown,│ │(Sharpe,     │  │
│  │ + greeks)│ │ VaR)     │ │ equity curve)│  │
│  └──────────┘ └──────────┘ └─────────────┘  │
└─────────────────────────────────────────────┘
```

### 4.2 Time-Walk Algorithm

```python
def walk_forward(start_date, end_date, tickers, strategy_fn):
    """Walk forward day by day, executing strategy on each date."""
    for date in trading_days(start_date, end_date):
        # 1. Mark-to-market existing positions
        portfolio.mark_to_market(date)
        
        # 2. Check management rules
        for position in portfolio.open_positions:
            action = check_management_rules(position, date)
            if action == 'CLOSE':
                portfolio.close_position(position, date)
            elif action == 'ROLL':
                portfolio.roll_position(position, date)
        
        # 3. Screen for new entries
        candidates = screen_candidates(tickers, date, portfolio)
        
        # 4. Execute new trades
        for candidate in candidates:
            if portfolio.can_open(candidate):
                portfolio.open_position(candidate, date)
        
        # 5. Record daily snapshot
        portfolio.record_snapshot(date)
```

### 4.3 Management Rules to Backtest

From the DTE research (`research_dte_selection.md`):

| Rule | Parameter | Values to Test |
|------|-----------|---------------|
| Entry DTE | 7, 14, 21, 30, 45, 60 | Sweep all |
| Strike delta | 0.15, 0.20, 0.25, 0.30, 0.40 | Sweep all |
| Profit target close | 25%, 50%, 75%, hold to expiry | Sweep all |
| 21 DTE exit | Yes / No | Binary |
| Roll at 21 DTE | Yes / No | Binary |
| IV Rank filter | 20, 30, 40, 50 | Sweep all |

### 4.4 Metrics to Report

| Metric | Formula |
|--------|---------|
| CAGR | (End Value / Start Value)^(1/years) - 1 |
| Sharpe Ratio | (Mean daily return - RF) / Std(daily returns) × √252 |
| Max Drawdown | (Peak - Trough) / Peak |
| Win Rate | Winning trades / Total trades |
| Avg Win / Avg Loss | Mean(winning P&L) / abs(Mean(losing P&L)) |
| Profit Factor | Gross Profit / Gross Loss |
| Premium Capture % | Premium collected / Max possible premium |
| Assignment Rate | Assignments / Total CSPs sold |
| Transaction Costs | Sum of all commissions + slippage |

---

## 5. Database Schema for Backtesting

### 5.1 New Tables (separate from live `db/options.db`)

```sql
-- File: db/backtest.db (separate from live trading DB)

-- Historical OHLCV (from moomoo kline, synced daily)
CREATE TABLE IF NOT EXISTS ohlcv_history (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    adj_close REAL,   -- split/dividend adjusted
    PRIMARY KEY (ticker, date)
);

-- Simulated option chains (generated, not fetched)
CREATE TABLE IF NOT EXISTS option_simulations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    option_type TEXT NOT NULL,  -- CALL / PUT
    strike REAL NOT NULL,
    expiry TEXT NOT NULL,
    dte INTEGER,
    underlying_price REAL,
    bid REAL,            -- simulated
    ask REAL,            -- simulated (bid + spread model)
    delta REAL,
    gamma REAL,
    theta REAL,
    vega REAL,
    rho REAL,
    implied_vol REAL,
    risk_free_rate REAL,
    simulated_at TEXT
);

-- Backtest runs metadata
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    name TEXT,
    start_date TEXT,
    end_date TEXT,
    strategy_params TEXT,  -- JSON
    created_at TEXT
);

-- Trade log from backtest
CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT REFERENCES backtest_runs(run_id),
    ticker TEXT,
    action TEXT,          -- OPEN_CC, OPEN_CSP, CLOSE, ASSIGN, ROLL
    option_type TEXT,
    strike REAL,
    expiry TEXT,
    dte INTEGER,
    premium REAL,
    contracts INTEGER,
    underlying_price REAL,
    entry_date TEXT,
    exit_date TEXT,
    exit_price REAL,
    pnl REAL,
    pnl_pct REAL,
    status TEXT           -- OPEN, CLOSED, ASSIGNED, EXPIRED
);

-- Daily portfolio snapshots
CREATE TABLE IF NOT EXISTS backtest_snapshots (
    run_id TEXT,
    date TEXT,
    total_value REAL,
    cash REAL,
    stock_value REAL,
    option_value REAL,
    positions_open INTEGER,
    drawdown_pct REAL,
    PRIMARY KEY (run_id, date)
);
```

### 5.2 Why Separate DB from Live Trading

| Reason | Detail |
|--------|--------|
| **Isolation** | Backtest corruption must never affect live trading data |
| **Scale** | Backtest DB grows to GBs over years; live DB stays small |
| **Schema divergence** | Live has real-time snapshots; backtest has simulated chains |
| **Deployment** | Backtest can run on a different machine, in CI, offline |
| **Clean slate** | Easy to nuke and rebuild backtest without touching live data |

---

## 6. Ingest Pipeline Design

### 6.1 Script: `scripts/ingest_history.py`

```
Purpose: Daily cron job to collect OHLCV data and store in SQLite
Schedule: Once per day (after market close)
Sources: moomoo kline (primary), yfinance (fallback)
Output: db/backtest.db → ohlcv_history table
```

### 6.2 What It Does

1. For each ticker in watchlist: fetch 5 years of daily OHLCV
2. Upsert into `ohlcv_history` (skip duplicates)
3. Record VIX history for IV regime classification
4. Record Treasury yields for risk-free rate
5. Log sync report with row counts and gaps

### 6.3 Cron Expression

```bash
# Run at 5:07 PM ET weekdays (after market close, off the :00/:30 minute marks)
7 17 * * 1-5 cd /path/to/project && python scripts/ingest_history.py
```

---

## 7. Integration With This Project

### 7.1 New Files to Create

```
scripts/
  ingest_history.py          # Daily OHLCV collection → backtest.db
  backtest_runner.py         # Walk-forward engine
  paper_trading.py           # Moomoo paper trading interface

src/backtest/
  __init__.py
  simulator.py               # Black-Scholes option price simulation
  time_walk.py               # Date iteration + calendar
  portfolio.py               # Virtual portfolio tracking
  reporter.py                # Tearsheet generation

tests/
  test_simulator.py          # BS pricing formula correctness
  test_time_walk.py          # Calendar edge cases
  test_backtest_portfolio.py # P&L calculation accuracy
```

### 7.2 Files to Modify

```
src/data/compute.py          # Add black_scholes() and compute_greeks()
src/data/models.py           # Add BacktestTrade, BacktestRun dataclasses
src/db/sync.py               # Extend SyncReport for backtest DB
CLAUDE.md                    # Document new modules
```

### 7.3 What Stays Unchanged

- `src/data/moomoo_client.py` — still used for live trading
- `src/scoring/engine.py` — scoring logic shared between live and backtest
- `src/signals/generator.py` — signal thresholds shared
- `src/trade/validator.py` — constraint checks shared
- `src/risk/collar_check.py` — collar logic shared

---

## 8. Paper Trading Integration

### 8.1 Moomoo Paper Trading Setup

Moomoo OpenD supports `TrdEnv.SIMULATE` for paper trading. Steps:

1. Connect via `OpenSecTradeContext(host='127.0.0.1', port=11111)`
2. Get simulate account ID from `get_acc_list()`
3. Query existing positions via `position_list_query(trd_env=TrdEnv.SIMULATE)`
4. Clear existing positions (close all open positions)
5. Submit new trades via `place_order()` with SIMULATE environment

### 8.2 Order Types for Wheel Strategy

| Action | Moomoo Order |
|--------|-------------|
| Sell CSP | `place_order(trd_side=SELL, order_type=SHORT, ...)` |
| Buy back CSP (close) | `place_order(trd_side=BUY, order_type=BUY_BACK, ...)` |
| Sell CC | `place_order(trd_side=SELL, order_type=SHORT, ...)` |
| Buy back CC (close) | `place_order(trd_side=BUY, order_type=BUY_BACK, ...)` |
| CSP assigned (buy stock) | Automatic by moomoo at expiry |
| CC assigned (sell stock) | Automatic by moomoo at expiry |

---

## 9. Implementation Phases

| Phase | Deliverable | Timeline |
|-------|------------|----------|
| **P1** | `scripts/ingest_history.py` — daily OHLCV collection to `db/backtest.db` | 1-2 days |
| **P2** | `src/backtest/simulator.py` — Black-Scholes pricing + Greeks | 2-3 days |
| **P3** | `src/backtest/time_walk.py` — date iteration engine | 2-3 days |
| **P4** | `src/backtest/portfolio.py` — virtual portfolio with P&L tracking | 2-3 days |
| **P5** | `scripts/backtest_runner.py` — end-to-end backtest execution | 2-3 days |
| **P6** | `scripts/paper_trading.py` — moomoo paper trading interface | 1-2 days |
| **P7** | `src/backtest/reporter.py` — tearsheets and charts | 1-2 days |
| **P8** | Parameter sweeps — DTE/delta/profit target optimization | Ongoing |

---

## 10. Quick Start: Minimum Viable Backtest

The absolute minimum to get a single backtest running:

```python
# Step 1: Get 5 years of daily OHLCV for MSFT
# Step 2: For each month-end, simulate a 30-delta CSP at 45 DTE
# Step 3: Track: did it expire OTM? What was the premium?
# Step 4: Report: total premium / max drawdown / win rate

# This is ~50 lines of Python, no complex architecture needed
# Build the full engine only after this validates
```

---

## Sources

1. [Building a Market-Data Pipeline: Caching, Rate Limits, Gaps](https://dev.to/pickuma/building-a-market-data-pipeline-caching-rate-limits-and-gaps-16o7)
2. [Towards Open Options Chains: A Data Pipeline Solution](https://hackernoon.com/towards-open-options-chains-a-data-pipeline-solution-for-options-data-part-i)
3. [GitHub: Covered Call Backtest (QQQ) — yudonglu1136](https://github.com/yudonglu1136/Covered_Call_Backtest)
4. [GitHub: Wheel Strategy Toolkit — fbaru-dev](https://github.com/fbaru-dev/wheel-strategy-toolkit)
5. [GitHub: Options-Backtester — philipcardozo](https://github.com/philipcardozo/Options-Backtester)
6. [GitHub: AlphaMatrix Options Trading Platform](https://github.com/cearps/AlphaMatrix)
7. [GitHub: Finmetry — Clean Separation Pattern](https://github.com/dev-ddr/finmetry)
8. [ORATS — Historical Options Quotes and Greeks](https://orats.com/blog/historical-options-quotes-and-greeks)
9. [CBOE DataShop — End of Day Options Quotes](https://datashop.cboe.com/)
10. [ApexVol — Research Methodology (ORATS data)](https://apexvol.com/methodology)
11. [Databento — Computing Option Greeks Using Pathway](https://databento.com/blog/option-greeks)
12. [Moomoo OpenAPI Trade Docs](https://openapi.moomoo.com/moomoo-api-doc/en/trade/overview.html)
