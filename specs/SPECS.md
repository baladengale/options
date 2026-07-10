# Wheel Strategy Options Trading System — Technical Specification v2

> **Target**: Deterministic data-driven engine that screens Covered Call (CC) and Cash Secured Put (CSP) candidates across a fixed watchlist, synced from moomoo into a local file-based DB, producing scored recommendations, sentiment signals, and a daily portfolio digest.
>
> **Hard boundary**: No naked options. No margin. No spreads. Every position must be covered or cash-secured at all legs.

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Local File-Based Database (SQLite)](#2-local-file-based-database-sqlite)
3. [Data Sync Engine](#3-data-sync-engine)
4. [Data Models](#4-data-models)
5. [Trend, Momentum & Signal Formulas](#5-trend-momentum--signal-formulas)
6. [Sentiment Scoring Engine](#6-sentiment-scoring-engine)
7. [Deterministic Scoring Engine](#7-deterministic-scoring-engine)
8. [Hard Constraint Gates](#8-hard-constraint-gates)
9. [Analysis Pipeline](#9-analysis-pipeline)
10. [Trade Execution Rules](#10-trade-execution-rules)
11. [Daily Digest](#11-daily-digest)
12. [Risk Management](#12-risk-management)
13. [Configuration Contract](#13-configuration-contract)
14. [Moomoo API Integration](#14-moomoo-api-integration)
15. [AI Runtime Boundary](#15-ai-runtime-boundary)
16. [Implementation Phases](#16-implementation-phases)
17. [Test Specifications](#17-test-specifications)
18. [Key References](#18-key-references)

---

## 1. System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     CLI / Notebook Layer                          │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐  │
│  │ daily-digest│  │ recommend    │  │ backtest / paper-trade │  │
│  └─────────────┘  └──────────────┘  └────────────────────────┘  │
├──────────────────────────────────────────────────────────────────┤
│                     Engine Layer                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │Scoring Engine│  │Sentiment Eng │  │  Signal Generator     │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
├──────────────────────────────────────────────────────────────────┤
│                     Analysis Pipeline                             │
│  Trend/Momentum │ Options Chain │ Fundamental │ Correlation      │
├──────────────────────────────────────────────────────────────────┤
│                     Data Sync Layer                               │
│  ┌──────────────────────┐  ┌────────────────────────────────┐   │
│  │ Moomoo API (primary) │  │ Yahoo Finance (fallback)        │   │
│  └──────────────────────┘  └────────────────────────────────┘   │
├──────────────────────────────────────────────────────────────────┤
│                     Local File-Based DB (SQLite)                  │
│  portfolio │ watchlist │ orders │ options_cache │ signals │ digest│
├──────────────────────────────────────────────────────────────────┤
│                     Config                                        │
│  portfolio.yaml │ watchlist.yaml │ strategy_params.yaml           │
└──────────────────────────────────────────────────────────────────┘
```

### 1.1 Project Structure

```
options/
├── config/
│   ├── portfolio.yaml
│   ├── watchlist.yaml
│   └── strategy_params.yaml
├── db/
│   └── options.db                 # SQLite — created at runtime, .gitignored
├── src/
│   ├── __init__.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── schema.py              # SQLite table definitions + migrations
│   │   ├── repository.py          # CRUD operations for all tables
│   │   └── sync.py                # Sync engine: moomoo → local DB
│   ├── data/
│   │   ├── __init__.py
│   │   ├── adapter.py             # DataAdapter abstract base
│   │   ├── moomoo_client.py       # Moomoo API implementation
│   │   ├── yfinance_client.py     # Yahoo Finance fallback
│   │   └── cache.py               # In-memory TTL cache
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── trend.py               # SMA, MACD, momentum, trend scoring
│   │   ├── fundamental.py         # Fundamental screening
│   │   ├── options_chain.py       # Options chain quality + greeks
│   │   ├── correlation.py         # Correlation vs existing holdings
│   │   └── snapshot.py            # Moomoo snapshot data processing
│   ├── signals/
│   │   ├── __init__.py
│   │   ├── generator.py           # Buy/Hold/Write signal generation
│   │   └── sentiment.py           # Deterministic sentiment scoring
│   ├── scoring/
│   │   ├── __init__.py
│   │   ├── engine.py              # Composite WHEEL_SCORE calculator
│   │   └── weights.py             # Scoring weights (config-driven)
│   ├── trade/
│   │   ├── __init__.py
│   │   ├── validator.py           # Pre-trade constraint checks
│   │   ├── position_sizer.py      # Position sizing calculator
│   │   └── executor.py            # Trade execution (paper first)
│   ├── digest/
│   │   ├── __init__.py
│   │   └── daily.py               # Daily digest generator
│   └── risk/
│       ├── __init__.py
│       ├── collar_check.py        # Coverage verification
│       └── monitor.py             # Live position risk monitor
├── notebooks/
│   └── research/                  # Per-stock research notebooks
├── tests/
│   ├── __init__.py
│   ├── conftest.py                # Shared fixtures (mock DB, mock data)
│   ├── test_scoring.py            # Scoring engine tests
│   ├── test_constraints.py        # Hard constraint gate tests
│   ├── test_trend.py              # Trend/momentum formula tests
│   ├── test_sentiment.py          # Sentiment signal tests
│   ├── test_db_sync.py            # DB sync + freshness tests
│   ├── test_signals.py            # Signal generation tests
│   └── test_risk.py               # Risk/coverage tests
├── SPECS.md
├── CLAUDE.md
└── README.md
```

---

## 2. Local File-Based Database (SQLite)

### 2.1 Design Principles

- **Single file**: `db/options.db` — no server, no setup. Created automatically on first run.
- **Always fresh**: Every analysis or recommendation run triggers a sync from moomoo BEFORE any computation.
- **Immutable source of truth**: moomoo is the master. Local DB is a snapshot for computation.
- **Timestamped**: Every row has `synced_at` so stale data is detectable.
- **Gitignored**: The `.db` file is never committed. Schema is code (`src/db/schema.py`).

### 2.2 Schema

```sql
-- ============================================================
-- TABLE: portfolio_snapshots
-- Latest portfolio state synced from moomoo
-- Only ONE row with is_current = 1 at any time
-- ============================================================
CREATE TABLE portfolio_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    total_cash      REAL NOT NULL,              -- USD available (buying power)
    total_market_value REAL NOT NULL,           -- cash + stock_value
    currency        TEXT NOT NULL DEFAULT 'USD',
    synced_at       TEXT NOT NULL,              -- ISO 8601 timestamp
    is_current      INTEGER NOT NULL DEFAULT 1  -- 1 = latest snapshot
);

-- ============================================================
-- TABLE: holdings
-- Current stock positions synced from moomoo
-- ============================================================
CREATE TABLE holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL REFERENCES portfolio_snapshots(id),
    ticker          TEXT NOT NULL,
    shares          REAL NOT NULL,
    avg_cost_basis  REAL NOT NULL,              -- per share
    market_price    REAL NOT NULL,              -- last trade price at sync time
    market_value    REAL NOT NULL,              -- shares × market_price
    unrealized_pnl  REAL NOT NULL,              -- (market_price - cost_basis) × shares
    unrealized_pnl_pct REAL NOT NULL,
    date_acquired   TEXT,                       -- earliest lot date, for tax
    synced_at       TEXT NOT NULL
);

-- ============================================================
-- TABLE: orders
-- Open and recent orders synced from moomoo
-- ============================================================
CREATE TABLE orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT NOT NULL UNIQUE,        -- moomoo order ID
    ticker          TEXT NOT NULL,
    order_type      TEXT NOT NULL,               -- 'BUY' | 'SELL'
    asset_type      TEXT NOT NULL,               -- 'STOCK' | 'OPTION'
    strategy        TEXT,                        -- 'COVERED_CALL' | 'CASH_SECURED_PUT' | NULL
    quantity        REAL NOT NULL,
    price           REAL,                        -- limit price (NULL for market)
    status          TEXT NOT NULL,               -- 'PENDING' | 'FILLED' | 'PARTIAL' | 'CANCELLED' | 'REJECTED'
    filled_qty      REAL DEFAULT 0,
    filled_avg_price REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    synced_at       TEXT NOT NULL
);

-- ============================================================
-- TABLE: open_positions
-- Active option contracts
-- ============================================================
CREATE TABLE open_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    strategy        TEXT NOT NULL CHECK(strategy IN ('COVERED_CALL', 'CASH_SECURED_PUT')),
    strike          REAL NOT NULL,
    expiry          TEXT NOT NULL,               -- ISO date
    contracts       INTEGER NOT NULL,
    premium_received REAL NOT NULL,              -- total premium (contracts × 100 × premium_per_share)
    delta           REAL,                        -- current delta
    opened_at       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'EXPIRED', 'ASSIGNED', 'CLOSED')),
    synced_at       TEXT NOT NULL
);

-- ============================================================
-- TABLE: watchlist
-- Fixed universe of stocks under consideration
-- ============================================================
CREATE TABLE watchlist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL UNIQUE,
    sector          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'UNRESEARCHED'
                    CHECK(status IN ('UNRESEARCHED', 'RESEARCHING', 'APPROVED', 'REJECTED', 'ACTIVE')),
    last_score      REAL,                        -- most recent WHEEL_SCORE
    last_scored_at  TEXT,                        -- when last scored
    notes           TEXT
);

-- ============================================================
-- TABLE: options_chain_cache
-- Cached option chain data for scoring (TTL: 5 min for quotes)
-- ============================================================
CREATE TABLE options_chain_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    expiry          TEXT NOT NULL,
    strike          REAL NOT NULL,
    option_type     TEXT NOT NULL CHECK(option_type IN ('CALL', 'PUT')),
    bid             REAL NOT NULL,
    ask             REAL NOT NULL,
    last_price      REAL,
    delta           REAL,
    gamma           REAL,
    theta           REAL,
    vega            REAL,
    implied_vol     REAL,
    open_interest   INTEGER,
    volume          INTEGER,
    underlying_price REAL NOT NULL,
    synced_at       TEXT NOT NULL,
    UNIQUE(ticker, expiry, strike, option_type)
);

-- ============================================================
-- TABLE: price_history
-- Daily OHLCV for technical analysis (252 days rolling)
-- ============================================================
CREATE TABLE price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    date            TEXT NOT NULL,               -- ISO date
    open            REAL NOT NULL,
    high            REAL NOT NULL,
    low             REAL NOT NULL,
    close           REAL NOT NULL,
    volume          INTEGER NOT NULL,
    UNIQUE(ticker, date)
);

-- ============================================================
-- TABLE: signals_log
-- All generated signals, persisted for audit trail
-- ============================================================
CREATE TABLE signals_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    strategy        TEXT NOT NULL,               -- 'COVERED_CALL' | 'CASH_SECURED_PUT'
    signal          TEXT NOT NULL,               -- 'STRONG_WRITE' | 'WRITE' | 'HOLD' | 'AVOID'
    composite_score REAL NOT NULL,
    trend_score     REAL NOT NULL,
    sentiment_score REAL NOT NULL,
    options_score   REAL NOT NULL,
    fund_score      REAL NOT NULL,
    corr_score      REAL NOT NULL,
    recommended_strike REAL,
    recommended_expiry TEXT,
    annualized_roc_pct REAL,
    all_constraints_pass INTEGER NOT NULL,       -- 1 or 0
    generated_at    TEXT NOT NULL
);

-- ============================================================
-- TABLE: daily_digest
-- One row per day — portfolio-level summary
-- ============================================================
CREATE TABLE daily_digest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL UNIQUE,        -- ISO date
    total_portfolio_value REAL NOT NULL,
    cash_available   REAL NOT NULL,
    cash_tied_up_csp REAL NOT NULL,             -- cash reserved for CSP assignments
    total_premium_collected REAL NOT NULL,       -- YTD or rolling
    open_cc_count   INTEGER NOT NULL,
    open_csp_count  INTEGER NOT NULL,
    portfolio_delta REAL,                        -- net portfolio delta
    portfolio_theta REAL,                        -- net daily theta decay
    top_signal_ticker TEXT,                      -- strongest signal ticker
    top_signal_score REAL,
    market_regime   TEXT,                        -- 'BULLISH' | 'BEARISH' | 'NEUTRAL' | 'VOLATILE'
    vix_level       REAL,
    action_items    TEXT,                        -- JSON list of recommended actions
    ai_narrative    TEXT,                        -- AI-generated summary (runtime only)
    generated_at    TEXT NOT NULL
);

-- ============================================================
-- INDEXES
-- ============================================================
CREATE INDEX idx_holdings_snapshot ON holdings(snapshot_id);
CREATE INDEX idx_holdings_ticker ON holdings(ticker);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_ticker ON orders(ticker);
CREATE INDEX idx_options_chain_ticker_expiry ON options_chain_cache(ticker, expiry);
CREATE INDEX idx_price_history_ticker_date ON price_history(ticker, date);
CREATE INDEX idx_signals_ticker ON signals_log(ticker);
CREATE INDEX idx_open_positions_status ON open_positions(status);
```

### 2.3 Freshness Contract

```
BEFORE any analysis or recommendation run:

1. SYNC portfolio from moomoo → portfolio_snapshots + holdings
2. SYNC open orders from moomoo → orders (UPSERT by order_id)
3. SYNC open positions from moomoo → open_positions
4. VERIFY synced_at for each table is within acceptable staleness:
   - portfolio:       ≤ 5 minutes old
   - orders:          ≤ 5 minutes old
   - options_chain:   ≤ 5 minutes old for the target DTE range
   - price_history:   ≤ 24 hours old (EOD data is fine)
5. If any check fails → re-sync from moomoo (with yfinance fallback)
6. If moomoo AND yfinance both fail → ABORT with error, do NOT proceed with stale data
```

---

## 3. Data Sync Engine

### 3.1 Sync Flow

```python
class SyncEngine:
    """
    Orchestrates data refresh from moomoo (primary) → local SQLite DB.
    Runs BEFORE every analysis/recommendation/digest generation.
    """

    def __init__(self, db: SQLiteRepository, adapter: DataAdapter):
        self.db = db
        self.adapter = adapter  # MoomooAdapter with YFinance fallback

    def sync_all(self, watchlist: list[str]) -> SyncReport:
        """Full sync: portfolio + orders + positions + chains + prices + watchlist."""

    def sync_portfolio(self) -> None:
        """Pull portfolio snapshot + holdings from moomoo, upsert to DB."""

    def sync_orders(self) -> None:
        """Pull open/recent orders, upsert by order_id."""

    def sync_positions(self) -> None:
        """Pull open option positions, update status."""

    def sync_option_chains(self, tickers: list[str], dte_range: tuple[int, int]) -> None:
        """Pull option chains for target DTE range for each ticker."""

    def sync_price_history(self, tickers: list[str], days: int = 252) -> None:
        """Pull daily OHLCV, upsert by (ticker, date)."""

    def verify_freshness(self) -> FreshnessReport:
        """Check all tables have synced_at within acceptable thresholds."""

    def clear_stale_cache(self) -> int:
        """Remove rows where synced_at exceeds TTL. Returns count of deleted rows."""
```

### 3.2 Sync Report Schema

```python
@dataclass
class SyncReport:
    success: bool
    portfolio_synced: bool
    orders_synced: bool
    positions_synced: bool
    chains_synced: dict[str, bool]   # ticker → success
    prices_synced: dict[str, bool]   # ticker → success
    errors: list[str]
    synced_at: datetime
    data_source: str                 # 'MOOMOO' | 'YFINANCE_FALLBACK' | 'MIXED'
```

---

## 4. Data Models

### 4.1 Portfolio State (`config/portfolio.yaml`)

```yaml
version: 2
last_updated: "2026-07-07T00:00:00Z"

portfolio:
  cash: 45000.00
  currency: USD

holdings:
  - ticker: "V"
    shares: 430
    avg_cost_basis: null
    date_acquired: null

open_positions: []

trade_history: []
```

### 4.2 Watchlist (`config/watchlist.yaml`)

```yaml
version: 2

universe:
  - ticker: MSFT
    sector: Technology
  - ticker: GOOGL
    sector: Technology
  - ticker: AAPL
    sector: Technology
  - ticker: AMZN
    sector: Technology
  - ticker: NVDA
    sector: Technology
  - ticker: META
    sector: Technology
  - ticker: AVGO
    sector: Technology
  - ticker: ADBE
    sector: Technology
  - ticker: CRM
    sector: Technology
  - ticker: AMD
    sector: Technology
```

### 4.3 Strategy Parameters (`config/strategy_params.yaml`)

```yaml
version: 2

# ---- Scoring Weights (must sum to 1.0) ----
scoring_weights:
  trend_momentum: 0.25
  options_chain: 0.25
  fundamental: 0.20
  sentiment: 0.15
  correlation: 0.15

# ---- Strategy Parameters ----
covered_call:
  delta_min: 0.20
  delta_max: 0.30
  dte_min: 30
  dte_max: 45
  min_annual_roc_pct: 8.0
  iv_rank_min: 30

cash_secured_put:
  delta_min: 0.15
  delta_max: 0.25
  dte_min: 30
  dte_max: 45
  min_annual_roc_pct: 12.0
  iv_rank_min: 30

# ---- Position Limits ----
position_limits:
  max_pct_per_underlying: 15
  min_pct_per_underlying: 5
  max_csp_cash_tie_up_pct: 80
  max_correlation_vs_visa: 0.8

# ---- Trend / Momentum Thresholds ----
trend:
  sma_short_period: 20
  sma_medium_period: 50
  sma_long_period: 200
  momentum_period: 14          # RSI, MACD signal line
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  trend_strength_min: 25       # ADX threshold for "trending" vs "ranging"

# ---- Sentiment Input Weights (deterministic component) ----
sentiment:
  trend_weight: 0.30
  momentum_weight: 0.25
  iv_rank_weight: 0.20
  volume_oi_weight: 0.15
  price_action_weight: 0.10

# ---- Risk ----
risk:
  earnings_blackout_days: 14
  bid_ask_spread_max_pct: 5.0
  max_portfolio_delta: null
  stop_loss_pct: null

# ---- Daily Digest ----
digest:
  generate_at_hour: 8          # 8 AM local — after market open data is fresh
  alert_if_cash_tied_up_pct_exceeds: 80
  alert_if_single_position_pct_exceeds: 20
```

---

## 5. Trend, Momentum & Signal Formulas

### 5.1 SMA Trend Alignment Score

```
Compute three SMAs: SMA(20), SMA(50), SMA(200)

Trend alignment is a measure of how "stacked" the moving averages are:

BULLISH ALIGNMENT (ideal for CSP entry):
  price > SMA(20) > SMA(50) > SMA(200)  →  strongest uptrend

BEARISH ALIGNMENT:
  price < SMA(20) < SMA(50) < SMA(200)  →  strongest downtrend

TREND_ALIGNMENT_SCORE:

  1. Count aligned pairs among: (price vs SMA20), (SMA20 vs SMA50), (SMA50 vs SMA200)
  2. Each pair where shorter > longer = bullish alignment = +33.33 points
  3. Each pair where shorter < longer = bearish alignment = 0 points (for CSP) or +33.33 points (for CC)

  For CSP (bullish bias):
    TREND_ALIGNMENT = sum of bullish pair scores → [0, 100]
    - 3 bullish alignments → 100
    - 2 bullish, 1 bearish → 67
    - 1 bullish, 2 bearish → 33
    - 0 bullish, 3 bearish → 0

  For CC (any trend, but prefer neutral-to-bullish):
    TREND_ALIGNMENT = same formula but floor at 30 (CC needs shares; you already own them)

TREND_STRENGTH (ADX):
  ADX ≥ 40 → 100  (strong trend — best for directional strategies)
  ADX 25-40 → 75  (moderate trend)
  ADX 20-25 → 50  (weak trend)
  ADX < 20 → 25   (ranging / no trend — worst for premium selling)
```

### 5.2 Momentum Score

```
RSI_SCORE (14-period):

  For CSP (prefer neutral-to-mildly-bullish):
    RSI 45-55 → 100  (neutral — ideal entry, no extreme)
    RSI 40-45 → 85   (mildly oversold — good CSP entry, higher premium)
    RSI 55-60 → 70   (mildly overbought — ok but lower premium)
    RSI 35-40 → 60   (oversold — higher risk of continued drop)
    RSI 60-65 → 50   (overbought — premium low, assignment risk lower too)
    RSI 30-35 → 35   (deeply oversold — risk of catching falling knife)
    RSI 65-75 → 25   (strongly overbought — avoid CSP, low premium)
    RSI < 30 → 15    (extreme oversold — high risk)
    RSI > 75 → 10    (extreme overbought — avoid)

  For CC (prefer neutral-to-mildly-bearish for entry, any for existing):
    RSI 45-55 → 100  (neutral — fair premium)
    RSI 55-65 → 85   (mildly overbought — good CC entry)
    RSI 40-45 → 70   (mildly oversold — lower call premium)
    RSI 65-75 → 60   (overbought — excellent CC entry, rich call premium)
    RSI 35-40 → 50   (oversold — poor CC entry, low premium)
    RSI > 75 → 40    (extreme overbought — great premium but reversal risk)
    RSI < 35 → 20    (extreme oversold — avoid selling calls)

MACD_SCORE:

  MACD = EMA(12) - EMA(26)
  Signal = EMA(9) of MACD
  Histogram = MACD - Signal

  For CSP:
    MACD > Signal AND Histogram > 0 AND Histogram > prev_Histogram → 100  (bullish, accelerating)
    MACD > Signal AND Histogram > 0 AND Histogram ≤ prev_Histogram → 80   (bullish, decelerating)
    MACD > Signal AND Histogram < 0 → 60                                   (just crossed bullish)
    MACD < Signal AND Histogram < 0 AND Histogram < prev_Histogram → 30   (bearish, accelerating)
    MACD < Signal AND Histogram < 0 AND Histogram ≥ prev_Histogram → 40   (bearish, decelerating)
    MACD < Signal AND Histogram > 0 → 50                                   (just crossed bearish)

  For CC:
    Same scoring but inverted — bearish momentum is better for holding CC premium.

MOMENTUM_SCORE = 0.4 × RSI_SCORE + 0.4 × MACD_SCORE + 0.2 × (100 - ADX penalty if < 20)
```

### 5.3 Composite Trend Score

```
TREND_COMPOSITE = 0.5 × TREND_ALIGNMENT + 0.3 × TREND_STRENGTH + 0.2 × MOMENTUM_SCORE
```

### 5.4 Signal Generator

The signal generator combines trend, IV, and options chain data into a directional trading signal:

```
SIGNAL = deterministic decision from:

Inputs:
  - TREND_COMPOSITE (Section 5.3)
  - IV_RANK (percentile of current IV vs 1Y history)
  - SENTIMENT_SCORE (Section 6)
  - OPTIONS_CHAIN_QUALITY (Section 7.2.3)

Decision Matrix for CSP:

  TREND_COMP  IV_RANK   SENTIMENT   OPTIONS_Q   →  SIGNAL
  ─────────────────────────────────────────────────────────
  ≥ 70        ≥ 30      ≥ 60        ≥ 60        →  STRONG_WRITE  (sell CSP)
  ≥ 50        ≥ 30      ≥ 50        ≥ 50        →  WRITE         (sell CSP, reduced size)
  ≥ 50        < 30      ≥ 50        ≥ 50        →  HOLD          (good stock, poor IV)
  < 50        any       any         any          →  HOLD          (trend not favorable)
  any         any       < 40        any          →  AVOID         (sentiment too negative)
  any         any       any         < 40         →  AVOID         (options chain illiquid)

Decision Matrix for CC (on existing shares):

  TREND_COMP  IV_RANK   SENTIMENT   OPTIONS_Q   →  SIGNAL
  ─────────────────────────────────────────────────────────
  ≥ 50        ≥ 30      ≥ 50        ≥ 50        →  STRONG_WRITE  (sell CC)
  ≥ 40        ≥ 30      ≥ 40        ≥ 50        →  WRITE         (sell CC, conservative strike)
  ≥ 40        < 30      ≥ 40        ≥ 50        →  HOLD          (wait for IV to expand)
  < 40        any       any         any          →  HOLD          (don't cap upside in strong rally)
  any         any       < 30        any          →  AVOID         (sentiment too negative — sell?)
  any         any       any         < 40         →  AVOID         (illiquid chain)
```

### 5.5 Signal Schema

```python
from enum import Enum

class Signal(Enum):
    STRONG_WRITE = "STRONG_WRITE"   # Green light — all systems go
    WRITE = "WRITE"                 # Cautious green — proceed, reduced size
    HOLD = "HOLD"                   # Wait — data not supportive enough
    AVOID = "AVOID"                 # Red light — do not trade

@dataclass
class SignalResult:
    ticker: str
    strategy: str                    # 'COVERED_CALL' | 'CASH_SECURED_PUT'
    signal: Signal
    confidence: float                # 0.0 – 1.0 (derived from score levels)
    trend_composite: float
    iv_rank: float
    sentiment_score: float
    options_quality_score: float
    reason: str                      # Human-readable explanation
    generated_at: datetime
```

---

## 6. Sentiment Scoring Engine

### 6.1 Deterministic Sentiment Formula

The sentiment score is a weighted composite of 5 deterministically computed sub-scores. AI adds a narrative layer only — the numerical score is formula-driven.

```
SENTIMENT_SCORE = Σ (sub_score_i × weight_i)   ∈ [0, 100]

Sub-scores:

1. TREND_SENTIMENT (weight: 0.30)
   = TREND_COMPOSITE from Section 5.3
   (already [0, 100])

2. MOMENTUM_SENTIMENT (weight: 0.25)
   = MOMENTUM_SCORE from Section 5.2
   (already [0, 100])

3. IV_SENTIMENT (weight: 0.20)
   IV Rank ≥ 60 → 100   (rich premium, high conviction to sell)
   IV Rank 50-60 → 85
   IV Rank 40-50 → 70
   IV Rank 30-40 → 55
   IV Rank 20-30 → 35
   IV Rank 10-20 → 20
   IV Rank < 10 → 10

   + IV/HV spread adjustment:
     IV > HV by > 20% → -10 penalty  (option may be overpriced for a reason — risk)
     IV > HV by 10-20% → -5
     IV ≈ HV (±10%) → 0             (fairly priced)
     IV < HV by > 10% → +5           (option cheap — good to sell? actually less premium)

4. VOLUME_OI_SENTIMENT (weight: 0.15)
   avg_daily_volume_ratio = today_volume / 90d_avg_volume

   avg_daily_volume_ratio ≥ 1.5 AND OI ≥ 1000 → 100  (strong institutional interest)
   avg_daily_volume_ratio 1.0-1.5 AND OI ≥ 500 → 80
   avg_daily_volume_ratio 0.7-1.0 AND OI ≥ 500 → 60
   avg_daily_volume_ratio 0.5-0.7 OR OI 100-500 → 40
   avg_daily_volume_ratio < 0.5 OR OI < 100 → 20

5. PRICE_ACTION_SENTIMENT (weight: 0.10)
   Compute:
   - pct_from_20day_high = (price - 20d_high) / 20d_high × 100
   - pct_from_20day_low = (price - 20d_low) / 20d_low × 100

   For CSP (prefer near support / off highs):
     price near 20d low (within 5%) → 100      (good CSP entry point)
     price 5-15% above 20d low → 70
     price mid-range → 50
     price 5-15% below 20d high → 40
     price near 20d high (within 5%) → 20      (poor CSP entry)

   For CC (prefer near highs / off lows):
     price near 20d high (within 5%) → 100     (good CC entry, max premium)
     price 5-15% below 20d high → 70
     price mid-range → 50
     price near 20d low → 30                   (poor CC entry, low premium)
```

### 6.2 Sentiment Direction

```
SENTIMENT_DIRECTION:

  SENTIMENT_SCORE ≥ 70 → BULLISH    (favors CSP entry, favors holding CC)
  SENTIMENT_SCORE 45-70 → NEUTRAL   (proceed with caution)
  SENTIMENT_SCORE 30-45 → CAUTIOUS  (consider waiting)
  SENTIMENT_SCORE < 30 → BEARISH    (avoid new positions, consider closing)

Decision integration:
  - CSP: only enter if SENTIMENT_DIRECTION ∈ {BULLISH, NEUTRAL}
  - CC:  only enter new CC if SENTIMENT_DIRECTION ∈ {NEUTRAL, CAUTIOUS} (neutral-to-bearish = better CC premium)
         for existing shares, write CC in any direction except BEARISH (don't cap upside in a crash)
```

### 6.3 Sentiment Output Schema

```python
@dataclass
class SentimentResult:
    ticker: str
    score: float                      # 0-100
    direction: str                    # 'BULLISH' | 'NEUTRAL' | 'CAUTIOUS' | 'BEARISH'
    trend_sentiment: float
    momentum_sentiment: float
    iv_sentiment: float
    volume_oi_sentiment: float
    price_action_sentiment: float
    ai_narrative: str | None          # AI-generated at runtime, nullable
    generated_at: datetime
```

---

## 7. Deterministic Scoring Engine

### 7.1 Composite Score Formula

```
WHEEL_SCORE = Σ (component_i × weight_i) × CONSTRAINT_PASS

Where:
  CONSTRAINT_PASS ∈ {0, 1}  — binary gate; 0 if any hard constraint fails
  Each component_i ∈ [0, 100]
  Σ weights = 1.0
```

### 7.2 Component Scores

#### 7.2.1 Trend & Momentum Score (weight: 0.25)

```
TREND_MOMENTUM_SCORE = TREND_COMPOSITE from Section 5.3
```
(Range [0, 100], computed deterministically from SMA alignment, ADX, RSI, MACD.)

#### 7.2.2 Fundamental Score (weight: 0.20)

```
FUND_SCORE = avg of:

  revenue_growth_score:
    YoY rev growth ≥ 20% → 100 | 15-20% → 80 | 10-15% → 60 | 5-10% → 40 | 0-5% → 20 | negative → 0

  earnings_quality_score:
    4/4 quarters positive EPS → 100 | 3/4 → 75 | 2/4 → 50 | 1/4 → 25 | 0/4 → 0

  fcf_yield_score:
    FCF yield ≥ 5% → 100 | 3-5% → 80 | 1-3% → 60 | 0-1% → 30 | negative → 0

  debt_to_equity_score:
    D/E < 0.3 → 100 | 0.3-0.6 → 80 | 0.6-1.0 → 60 | 1.0-2.0 → 40 | > 2.0 → 20 | negative equity → 0

  peg_ratio_score:
    0 < PEG ≤ 1.0 → 100 | 1.0-1.5 → 80 | 1.5-2.0 → 60 | 2.0-3.0 → 40 | > 3.0 or negative → 20
```

#### 7.2.3 Options Chain Quality Score (weight: 0.25)

```
OPTIONS_SCORE = avg of:

  bid_ask_spread_score:
    spread_pct = (ask - bid) / mid × 100
    spread < 0.5% → 100 | 0.5-1.0% → 80 | 1.0-2.0% → 60 | 2.0-5.0% → 40 | > 5.0% → 20

  open_interest_score:
    OI ≥ 1000 → 100 | 500-1000 → 80 | 100-500 → 60 | 50-100 → 40 | < 50 → 20

  volume_score:
    daily volume ≥ 500 → 100 | 100-500 → 70 | 50-100 → 40 | < 50 → 10

  iv_vs_hv_spread_score:
    |IV - HV| ≤ 5% → 100 | 5-10% → 70 | 10-20% → 40 | > 20% → 20

  term_structure_score:
    front month IV > back month IV (backwardation) → 100
    flat (±2%) → 60
    front month < back month (contango) → 30
```

#### 7.2.4 Sentiment Score (weight: 0.15)

```
SENTIMENT_COMPONENT = SENTIMENT_SCORE from Section 6.1
```
(Deterministically computed. AI narrative is additive, not part of the score.)

#### 7.2.5 Correlation Score (weight: 0.15)

```
CORR_SCORE:

  1. Compute rolling 1Y daily-return Pearson correlation between ticker and V (Visa)
  2. Also compute vs each existing holding

  If any correlation > position_limits.max_correlation_vs_visa (0.8) → CORR_SCORE = 0 (HARD FAIL)
  Else:
    CORR_SCORE = 100 × (1 - max_correlation)

  corr=0.3 → 70, corr=0.5 → 50, corr=0.7 → 30
```

### 7.3 Scoring Output Schema

```python
@dataclass
class WheelScore:
    ticker: str
    strategy: str                     # "COVERED_CALL" | "CASH_SECURED_PUT"
    timestamp: datetime

    # Composite
    composite_score: float            # 0-100, 0 if any constraint fails

    # Components
    trend_momentum_score: float
    fundamental_score: float
    options_chain_score: float
    sentiment_score: float
    correlation_score: float

    # Trade specifics
    recommended_strike: float
    recommended_expiry: date
    recommended_delta: float
    annualized_roc_pct: float
    premium_per_contract: float
    contracts: int
    capital_required: float

    # Signal
    signal: str                       # STRONG_WRITE | WRITE | HOLD | AVOID
    sentiment_direction: str          # BULLISH | NEUTRAL | CAUTIOUS | BEARISH

    # Constraint results
    constraints: dict[str, bool]      # constraint_id → passed
    all_constraints_pass: bool

    # AI-assisted (runtime only)
    ai_narrative: str | None

    # Ranking
    rank: int
```

---

## 8. Hard Constraint Gates

**Any single constraint failure → WHEEL_SCORE = 0 and signal = AVOID.**

| # | Constraint | Check | Error Message |
|---|-----------|-------|---------------|
| C1 | No naked options | strategy ∈ {COVERED_CALL, CASH_SECURED_PUT} | "Strategy not permitted: must be CC or CSP" |
| C2 | No margin | No margin flag on account; all positions fully funded | "Margin detected: all positions must be cash/asset secured" |
| C3 | CC: shares ≥ contracts×100 | holdings.shares ≥ contracts×100 | "CC under-covered: need {shortfall} more shares" |
| C4 | CSP: cash ≥ strike×contracts×100 | portfolio.cash - tied_up_cash ≥ required | "CSP under-secured: need ${shortfall} more cash" |
| C5 | No earnings in blackout window | next_earnings_date - today ≥ risk.earnings_blackout_days | "Earnings blackout: {ticker} reports in {days} days" |
| C6 | DTE in valid range | dte_min ≤ DTE ≤ dte_max | "DTE {dte} outside [{dte_min}, {dte_max}]" |
| C7 | Delta in valid range for strategy | delta_min ≤ delta ≤ delta_max | "Delta {delta} outside valid range for {strategy}" |
| C8 | Position size ≤ max_pct_per_underlying | (strike × 100 × contracts) / portfolio_value ≤ max_pct | "Position size {pct}% exceeds max {max_pct}%" |
| C9 | CSP allocation ≤ max_csp_cash_tie_up_pct | tied_up_csp / total_cash ≤ max_csp_cash_tie_up_pct | "CSP tie-up {pct}% exceeds max {max_pct}%" |
| C10 | IV Rank ≥ minimum | iv_rank ≥ iv_rank_min | "IV Rank {iv_rank} below minimum {iv_rank_min}" |
| C11 | Annualized RoC ≥ minimum | annualized_roc ≥ min_threshold | "RoC {roc}% below minimum {min}% for {strategy}" |
| C12 | Correlation vs V < max | max_corr_vs_visa < correlation_max | "Correlation {corr} exceeds diversification max {max}" |
| C13 | Bid-ask spread acceptable | spread_pct ≤ bid_ask_spread_max_pct | "Spread {spread}% exceeds max {max}%" |
| C14 | Data freshness verified | all tables synced within TTL | "Stale data: {table} last synced {age} ago" |

---

## 9. Analysis Pipeline

### 9.1 End-to-End Flow

```
┌─────────────────────────────────────────────────────────────┐
│ STEP 0: SYNC                                                 │
│   SyncEngine.sync_all(watchlist) → verify freshness          │
│   If any sync fails → ABORT                                  │
├─────────────────────────────────────────────────────────────┤
│ STEP 1: FOR EACH TICKER IN WATCHLIST                         │
│                                                              │
│   1a. Fetch from DB: price_history (252d), fundamentals,     │
│        options_chain_cache (30-45 DTE), holdings             │
│                                                              │
│   1b. Compute TREND_COMPOSITE (SMA alignment + ADX + RSI +  │
│        MACD) → Section 5                                     │
│                                                              │
│   1c. Compute SENTIMENT_SCORE (trend + momentum + IV +       │
│        volume + price action) → Section 6                    │
│                                                              │
│   1d. Compute FUND_SCORE (revenue + EPS + FCF + D/E + PEG)  │
│        → Section 7.2.2                                       │
│                                                              │
│   1e. Compute OPTIONS_SCORE (spread + OI + vol + IV/HV +    │
│        term structure) → Section 7.2.3                       │
│                                                              │
│   1f. For existing holdings: score CC candidates             │
│        For cash available: score CSP candidates              │
│                                                              │
│   1g. Generate SIGNAL → Section 5.4                          │
│                                                              │
│   1h. Check all 14 constraints → Section 8                   │
│                                                              │
│   1i. Compute WHEEL_SCORE → Section 7.1                      │
│                                                              │
│   1j. Persist to signals_log                                 │
│                                                              │
├─────────────────────────────────────────────────────────────┤
│ STEP 2: RANK & RECOMMEND                                     │
│   Sort all WheelScores by composite_score descending         │
│   Filter: only STRONG_WRITE and WRITE signals                │
│   Top result = recommendation                                │
├─────────────────────────────────────────────────────────────┤
│ STEP 3: GENERATE DAILY DIGEST                                │
│   Portfolio summary + top signals + action items             │
│   → Section 11                                               │
├─────────────────────────────────────────────────────────────┤
│ STEP 4: OUTPUT                                               │
│   - Decision matrix (table)                                  │
│   - Top recommendation with full score breakdown             │
│   - Daily digest (portfolio-level)                           │
│   - AI narrative (sentiment + macro, runtime only)           │
└─────────────────────────────────────────────────────────────┘
```

### 9.2 Decision Matrix Output

| Ticker | Signal | Comp | Trend | Fund | Options | Sentiment | Corr | Strategy | Strike | Expiry | Δ | RoC% | Constraints |
|--------|--------|------|-------|------|---------|-----------|------|----------|--------|--------|---|------|-------------|
| MSFT   | STRONG_WRITE | 82 | 85 | 80 | 82 | 75 | 80 | CSP | $420 | 08/15/26 | 0.18 | 14.2 | ✓✓✓✓✓✓✓✓✓✓✓✓✓✓ |
| NVDA   | WRITE | 74 | 70 | 75 | 78 | 68 | 75 | CSP | $128 | 08/15/26 | 0.20 | 13.8 | ✓✓✓✓✓✓✓✓✓✓✓✓✓✓ |
| AAPL   | HOLD | 62 | 55 | 80 | 65 | 60 | 85 | CSP | — | — | — | — | — |
| V      | STRONG_WRITE | 78 | 60 | 85 | 80 | 70 | 100 | CC | $295 | 08/15/26 | 0.25 | 10.5 | ✓✓✓✓✓✓✓✓✓✓✓✓✓✓ |

---

## 10. Trade Execution Rules

### 10.1 Pre-Trade Checklist

```python
def pre_trade_check(trade: ProposedTrade, portfolio: Portfolio) -> CheckResult:
    """
    One trade at a time. Must pass ALL 14 constraints.
    Additionally: collar check before execution.
    """
    for constraint_id, check_fn in ALL_CONSTRAINTS:
        if not check_fn(trade, portfolio):
            return CheckResult(passed=False, failed_constraint=constraint_id)

    # Collar check: verify ALL existing positions remain covered
    collar = collar_check(portfolio)
    if not collar.ok:
        return CheckResult(passed=False, failed_constraint="COLLAR", detail=collar.reason)

    return CheckResult(passed=True)
```

### 10.2 Trade Lifecycle State Machine

```
PROPOSED → VALIDATED → PENDING_SUBMIT → SUBMITTED → FILLED
                                               ↘ REJECTED
FILLED → MONITORING → EXPIRED_WORTHLESS (keep premium)
                   → ASSIGNED
                       CC: shares called away → cash ↑ → evaluate CSP
                       CSP: shares delivered → cash ↓ → evaluate CC
                   → CLOSED_EARLY (manual / risk stop)
```

### 10.3 Wheel Rotation Logic

```
State: 430 V shares + $45,000 cash

Path A — Covered Call on V:
  1. Score V for CC → if SIGNAL ∈ {STRONG_WRITE, WRITE}:
     Sell CC: delta 0.20-0.30, 30-45 DTE
  2. Monitor until expiry:
     a. Expires worthless → keep premium + shares, repeat A
     b. Assigned → V shares called away, cash increases by (strike × 100 × contracts)
        → Proceed to Path B

Path B — Cash Secured Put on new stock:
  1. Score all watchlist tickers for CSP → rank by composite_score
  2. If top signal ∈ {STRONG_WRITE, WRITE}:
     Sell CSP: delta 0.15-0.25, 30-45 DTE
  3. Monitor until expiry:
     a. Expires worthless → keep premium, repeat B or rotate to A if V looks good
     b. Assigned → acquire new shares at (strike - premium), return to Path A
```

---

## 11. Daily Digest

### 11.1 Purpose

A deterministic daily report that summarizes portfolio health, scores all watchlist candidates, and surfaces action items. Generated once per day (configurable hour, default 8 AM).

### 11.2 Digest Generation

```python
class DailyDigestGenerator:
    """
    Generates the daily portfolio digest.
    Runs: sync_all → score_all → summarize → persist → output.
    """

    def generate(self) -> DailyDigest:
        # 1. Sync all data from moomoo
        sync_report = self.sync_engine.sync_all(self.watchlist)

        # 2. Score all tickers for both strategies
        scores: list[WheelScore] = []
        for ticker in self.watchlist:
            if self.portfolio.has_shares(ticker):
                scores.append(self.scoring_engine.score(ticker, 'COVERED_CALL'))
            scores.append(self.scoring_engine.score(ticker, 'CASH_SECURED_PUT'))

        # 3. Sort and rank
        scores.sort(key=lambda s: s.composite_score, reverse=True)

        # 4. Compute portfolio-level metrics
        portfolio_delta = self._compute_portfolio_delta()
        portfolio_theta = self._compute_portfolio_theta()
        market_regime = self._classify_market_regime()

        # 5. Generate action items
        actions = self._generate_action_items(scores, portfolio_delta)

        # 6. Persist digest
        digest = DailyDigest(...)
        self.db.insert_daily_digest(digest)

        return digest
```

### 11.3 Market Regime Classification

```
MARKET_REGIME (deterministic from VIX + SPY trend):

  VIX < 15 AND SPY price > SMA(50) > SMA(200) → BULLISH
  VIX 15-20 AND SPY price > SMA(200) → NEUTRAL (mildly bullish)
  VIX 20-25 AND SPY price near SMA(200) → NEUTRAL (cautious)
  VIX 25-30 → VOLATILE
  VIX > 30 → BEARISH / STRESS

  Regime impact on strategy:
    BULLISH: favor CSP (premium capture, assignment less risky)
    NEUTRAL: balanced CC + CSP
    VOLATILE: favor CC on existing (rich premium), smaller CSP size
    BEARISH: only CC on existing, no new CSP, consider closing CSP early
```

### 11.4 Digest Output Format

```
═══════════════════════════════════════════════════════════════
                    DAILY DIGEST — 2026-07-07
═══════════════════════════════════════════════════════════════

PORTFOLIO SUMMARY
───────────────────────────────────────────────────────────────
  Total Value:       $148,350.00
  Cash Available:    $45,000.00
  Cash Tied (CSP):   $0.00
  Stock Value:       $103,350.00  (430 V @ $240.35)
  Open CC:           0
  Open CSP:          0

MARKET REGIME: NEUTRAL (VIX 18.3, SPY above 200 SMA)

TOP SIGNALS
───────────────────────────────────────────────────────────────
  #1  MSFT  CSP  STRONG_WRITE  82/100  $420 strike  08/15  14.2% RoC
  #2  NVDA  CSP  WRITE         74/100  $128 strike  08/15  13.8% RoC
  #3  V     CC   STRONG_WRITE  78/100  $295 strike  08/15  10.5% RoC
  #4  GOOGL CSP  HOLD          62/100  —            —      —
  #5  AAPL  CSP  HOLD          58/100  —            —      —

ACTION ITEMS
───────────────────────────────────────────────────────────────
  [RECOMMENDED] Sell CSP on MSFT: 2 contracts, $420 strike,
                08/15/26 expiry, delta 0.18, max premium $1,300
  [MONITOR]     V Covered Call: Hold for now — IV rank 28,
                wait for IV expansion above 30 before selling
  [WATCH]       NVDA earnings in 21 days — outside blackout,
                but monitor for volatility crush risk

RISK ALERTS
───────────────────────────────────────────────────────────────
  ✓ All positions covered/cash-secured
  ✓ No earnings blackout violations
  ✓ CSP cash allocation: 0% (under 80% limit)
  ⚠ V position: 69.7% of portfolio (exceeds 20% alert threshold)

AI NARRATIVE (generated at runtime)
───────────────────────────────────────────────────────────────
  [AI-generated macro context and reasoning here...]

═══════════════════════════════════════════════════════════════
```

---

## 12. Risk Management

### 12.1 Collar Check

```python
def collar_check(portfolio: Portfolio) -> CollarReport:
    """
    Verify every open position remains covered/cash-secured.
    Must return all_clear=True before any new trade.
    """
    for pos in portfolio.open_positions:
        if pos.strategy == "COVERED_CALL":
            shares = portfolio.get_shares(pos.ticker)
            required = pos.contracts * 100
            if shares < required:
                return CollarReport(ok=False,
                    reason=f"CC under-covered: {pos.ticker} ({shares} shares < {required} needed)")
        elif pos.strategy == "CASH_SECURED_PUT":
            cash_needed = pos.strike * pos.contracts * 100
            if portfolio.cash < cash_needed:
                return CollarReport(ok=False,
                    reason=f"CSP under-secured: need ${cash_needed:,.2f}, have ${portfolio.cash:,.2f}")
    return CollarReport(ok=True)
```

### 12.2 Assignment Handling

```
CSP assigned:
  - Cash reduced by strike × 100 × contracts
  - Shares added at cost basis = strike - premium_received_per_share
  - Auto-score new shares for CC opportunity
  - Log in trade_history

CC assigned:
  - Shares removed from holdings
  - Cash increased by strike × 100 × contracts (+ premium already collected)
  - Auto-score CSP candidates with freed cash
  - Log in trade_history
```

### 12.3 Portfolio-Level Risk Metrics

```python
@dataclass
class PortfolioRisk:
    net_delta: float               # Σ (position_delta × shares × price)
    net_theta: float               # Σ daily theta (positive = time decay helps us)
    cash_tied_up_pct: float        # % of cash reserved for CSP
    largest_position_pct: float    # largest single stock as % of portfolio
    correlation_matrix: dict       # ticker pairs → correlation
    concentration_risk: str        # 'LOW' | 'MODERATE' | 'HIGH'
    margin_usage_pct: float        # must always be 0%
```

---

## 13. Configuration Contract

### 13.1 `portfolio.yaml` — VERSIONED, NEVER COMMIT LIVE VALUES

```yaml
version: 2
last_updated: "2026-07-07T00:00:00Z"

portfolio:
  cash: 45000.00
  currency: USD

holdings:
  - ticker: V
    shares: 430
    avg_cost_basis: null
    date_acquired: null

open_positions: []
trade_history: []
```

### 13.2 `strategy_params.yaml`

```yaml
version: 2

scoring_weights:
  trend_momentum: 0.25
  options_chain: 0.25
  fundamental: 0.20
  sentiment: 0.15
  correlation: 0.15

covered_call:
  delta_min: 0.20
  delta_max: 0.30
  dte_min: 30
  dte_max: 45
  min_annual_roc_pct: 8.0
  iv_rank_min: 30

cash_secured_put:
  delta_min: 0.15
  delta_max: 0.25
  dte_min: 30
  dte_max: 45
  min_annual_roc_pct: 12.0
  iv_rank_min: 30

position_limits:
  max_pct_per_underlying: 15
  min_pct_per_underlying: 5
  max_csp_cash_tie_up_pct: 80
  max_correlation_vs_visa: 0.8

trend:
  sma_short_period: 20
  sma_medium_period: 50
  sma_long_period: 200
  momentum_period: 14
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  trend_strength_min: 25

sentiment:
  trend_weight: 0.30
  momentum_weight: 0.25
  iv_rank_weight: 0.20
  volume_oi_weight: 0.15
  price_action_weight: 0.10

risk:
  earnings_blackout_days: 14
  bid_ask_spread_max_pct: 5.0
  max_portfolio_delta: null
  stop_loss_pct: null

digest:
  generate_at_hour: 8
  alert_if_cash_tied_up_pct_exceeds: 80
  alert_if_single_position_pct_exceeds: 20
```

---

## 14. Moomoo API Integration

### 14.1 Primary Data Source

MCP server: `https://github.com/baladengale/moomoo-api-mcp`

Official API docs:
- Intro: `https://openapi.moomoo.com/moomoo-api-doc/en/intro/intro.html`
- Quote: `https://openapi.moomoo.com/moomoo-api-doc/en/quote/overview.html`
- Trade: `https://openapi.moomoo.com/moomoo-api-doc/en/trade/overview.html`

### 14.2 Required API Functions

| Category | Function | Data Retrieved | Maps To |
|----------|----------|---------------|---------|
| **Account** | `get_account_balance` | Cash, buying power, margin status | `portfolio_snapshots` |
| **Position** | `get_positions` | Holdings (ticker, shares, cost, market value) | `holdings` |
| **Order** | `get_orders` | Open + recent orders (status, filled qty) | `orders` |
| **Quote** | `get_stock_quote` | Real-time bid/ask/last, change%, volume | `price_history` (latest) |
| **Quote** | `get_option_chain` | All strikes/expiries: bid, ask, delta, gamma, theta, vega, IV, OI, volume | `options_chain_cache` |
| **Quote** | `get_snapshot` | Fundamentals snapshot: P/E, EPS, revenue, D/E, FCF, market cap | Fundamental analysis |
| **Quote** | `get_kline` | Daily OHLCV history (252+ days) | `price_history` |
| **Market** | `get_market_state` | Trading hours, circuit breakers, halts | Pre-trade check |
| **Market** | `get_earnings_calendar` | Upcoming earnings dates | Earnings blackout check |

### 14.3 Moomoo Snapshot Data Mapping

```python
@dataclass
class MoomooSnapshot:
    """Data extracted from moomoo get_snapshot / get_stock_quote."""
    ticker: str
    last_price: float
    bid: float
    ask: float
    bid_ask_spread_pct: float      # computed: (ask-bid)/mid
    change_pct: float               # daily change %
    volume: int                     # today's volume
    avg_volume_90d: int             # 90-day average volume
    high_52w: float
    low_52w: float
    pe_ratio: float | None
    eps_ttm: float | None
    market_cap: float | None
    dividend_yield: float | None
    iv_30d: float | None            # 30-day implied volatility
    iv_rank: float | None           # computed from 1Y IV history
    beta: float | None
```

---

## 15. AI Runtime Boundary

### 15.1 When AI Is Used

AI is invoked ONLY after all deterministic computation is complete:

1. **Sentiment narrative**: Given the SENTIMENT_SCORE breakdown + news headlines + VIX + Fed stance, generate a 2-3 sentence human-readable explanation.
2. **Macro reasoning**: Given market regime + sector performance + economic data, provide macro context for the daily digest.
3. **Edge case judgment**: When data is conflicting (e.g., TREND_COMPOSITE > 70 but SENTIMENT_SCORE < 30), AI provides reasoning on which signal to weight more heavily. The deterministic score still drives the final number — AI explains the conflict.

### 15.2 When AI Is NOT Used

- Computing any numerical score (all formula-driven)
- Checking constraints (all boolean logic)
- Position sizing (all arithmetic)
- Signal generation (all threshold-based)
- Trade execution (all rule-based)
- Collar checks (all arithmetic)

### 15.3 AI Invocation Contract

```python
def invoke_ai_sentiment(context: SentimentContext) -> str:
    """
    PRECONDITION: All deterministic scores already computed.
    POSTCONDITION: Returns narrative string only. Score is NEVER modified by AI.
    """
    prompt = f"""
    Given the following deterministic analysis for {context.ticker}:
    - Trend Composite Score: {context.trend_composite}/100
    - Momentum Score: {context.momentum_score}/100
    - IV Rank: {context.iv_rank}%
    - Sentiment Score: {context.sentiment_score}/100
    - Signal: {context.signal}
    - Market Regime: {context.market_regime}
    - VIX: {context.vix}
    - Recent headlines: {context.headlines}

    Provide a concise 2-3 sentence narrative explaining the current outlook
    for a {context.strategy} position. Do NOT modify any scores or recommend
    specific strikes/quantities — those are already computed.
    """
    return ai_complete(prompt)
```

---

## 16. Implementation Phases

| Phase | Deliverable | Key Files | Dependencies |
|-------|------------|-----------|-------------|
| **P1** | Project scaffold + config schemas + YAML files | `config/`, `src/__init__.py` | None |
| **P2** | SQLite schema + repository layer | `src/db/schema.py`, `src/db/repository.py` | P1 |
| **P3** | DataAdapter ABC + YFinanceAdapter | `src/data/adapter.py`, `src/data/yfinance_client.py` | P1 |
| **P4** | MoomooAdapter (via MCP server) + Sync Engine | `src/data/moomoo_client.py`, `src/db/sync.py` | P2, P3 |
| **P5** | Trend/Momentum formulas + tests | `src/analysis/trend.py` | P3 |
| **P6** | Sentiment scoring engine + tests | `src/signals/sentiment.py` | P5 |
| **P7** | Signal generator + tests | `src/signals/generator.py` | P6 |
| **P8** | Options chain analysis + Fundamental analysis | `src/analysis/options_chain.py`, `src/analysis/fundamental.py` | P3 |
| **P9** | Correlation analysis | `src/analysis/correlation.py` | P3 |
| **P10** | Composite scoring engine (all 5 components) | `src/scoring/engine.py` | P5-P9 |
| **P11** | Trade validator + Position sizer | `src/trade/validator.py`, `src/trade/position_sizer.py` | P10 |
| **P12** | Risk manager + Collar check | `src/risk/collar_check.py`, `src/risk/monitor.py` | P11 |
| **P13** | Daily digest generator | `src/digest/daily.py` | P10, P12 |
| **P14** | AI sentiment integration (runtime only) | `src/signals/sentiment.py` (AI method) | P13 |
| **P15** | Paper trading executor | `src/trade/executor.py` | P11, P12 |
| **P16** | Research notebooks (per watchlist stock) | `notebooks/research/` | P10 |
| **P17** | Backtesting harness | `tests/` integration suite | P15 |
| **P18** | CLI interface (`daily-digest`, `recommend`, `backtest`) | `src/cli.py` | P15, P17 |

---

## 17. Test Specifications

### 17.1 Test Infrastructure

All tests use `pytest` with SQLite `:memory:` database for isolation. Mock moomoo responses via fixtures.

See individual test files:
- [tests/test_scoring.py](tests/test_scoring.py) — Scoring engine with exact expected values
- [tests/test_constraints.py](tests/test_constraints.py) — All 14 constraint gates
- [tests/test_trend.py](tests/test_trend.py) — SMA/MACD/RSI/ADX formulas
- [tests/test_sentiment.py](tests/test_sentiment.py) — Sentiment scoring engine
- [tests/test_db_sync.py](tests/test_db_sync.py) — DB sync + freshness
- [tests/test_signals.py](tests/test_signals.py) — Signal generator decision matrix
- [tests/test_risk.py](tests/test_risk.py) — Collar check, assignment handling

### 17.2 Quality Gates (Must Pass Before Any Commit)

```bash
# All tests must pass
pytest tests/ -v --tb=short

# Coverage minimums
pytest tests/ --cov=src --cov-report=term --cov-fail-under=85

# Specific gate tests
pytest tests/test_constraints.py -v   # All 14 constraints tested
pytest tests/test_scoring.py -v       # All scoring components tested
pytest tests/test_trend.py -v         # All formula edge cases
```

---

## 18. Key References

| Resource | URL |
|----------|-----|
| Moomoo MCP Server | https://github.com/baladengale/moomoo-api-mcp |
| Moomoo API Intro | https://openapi.moomoo.com/moomoo-api-doc/en/intro/intro.html |
| Moomoo Quote API | https://openapi.moomoo.com/moomoo-api-doc/en/quote/overview.html |
| Moomoo Trade API | https://openapi.moomoo.com/moomoo-api-doc/en/trade/overview.html |
