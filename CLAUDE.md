# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Options trading analysis and execution system with a hard constraint: **only Covered Calls, Cash Secured Puts, and (suggestion-only) put credit spreads are permitted.** No naked options, no debit spreads, no iron condors/butterflies, no margin trading.

Put credit spreads are a **defined-risk, cash-backed, suggestion-only** income supplement (UNVALIDATED — paper-trade before live). They cap downside at `max_loss = width − credit` instead of the full strike, and `max_loss` must be 100% cash-backed (honors GOAL.md #4 "never prefer margin"). They are surfaced by `scripts/screener.py --ps-only` and never auto-executed.

The system is a **deterministic data-driven engine** that:
1. Polls watchlist, portfolio, orders, and funds live from moomoo on every script run
2. Screens all watchlist tickers for CC and CSP candidates
3. Computes trend/momentum, sentiment, and composite scores via deterministic formulas
4. Generates portfolio summaries with signals and action items
5. Uses AI ONLY at runtime for sentiment narrative and macro reasoning (never for scoring math)

## Architecture (2026-07-19)

The codebase follows a strict layered architecture:

```
scripts/          ← THIN wrappers: argparse → fetch data → call src/ → display
src/filters/      ← Shared contract gates: liquidity, delta, IV, VRP, RoC, concentration
src/scoring/      ← Ticker scoring + contract penalty + holding decisions
src/strategies/   ← Strategy-specific scoring: put credit spreads (credit_spread.py)
src/data/         ← Data access: moomoo, yfinance, portfolio, watchlist, guardrails
src/risk/         ← Risk: overlap detection, exit framework
src/analysis/     ← Sentiment, macro context, thesis evaluation, profit & exit management
config/rules.yaml ← Single source of truth for ALL thresholds, weights, limits
```

**Rules**:
- **No script imports from another script.** `oie_engine.py` imports from `src/`, not from `scripts/screener.py`.
- **No hardcoded thresholds.** Every gate, penalty, weight, and limit lives in `config/rules.yaml` → `src/config.py`.
- **All contract filtering uses `src/filters/contract_filters.py`.** Single source of truth for OI, delta, IV sanity, VRP, RoC, concentration, and cash buffer gates.
- **Shared dataclasses** in `src/data/models.py`: `StockSnapshot`, `OptionSnapshot`, `TradeCandidate`.
- **Shared data functions** in `src/data/`: `fetch_live_portfolio()`, `fetch_live_watchlist()`, `get_option_snapshots_resilient()`.

## Response Protocol — ALWAYS Follow This Order

**For ANY portfolio, trading, or position question, you MUST run the local engine first. Never give generic advice — always ground answers in the user's actual portfolio data, their config rules, and their GOAL.md actions.**

### Step 1: Load Portfolio State
```bash
python3 scripts/portfolio.py
```
This pulls live positions, orders, and cash from moomoo via `src/data/portfolio_loader.py`. If OpenD isn't running or returns errors → abort. Never use stale data.

### Step 2: Run the Relevant Analysis

| User Asks | Run This | Why |
|-----------|----------|-----|
| "What should I trade?" / "Any recommendations?" | `python3 scripts/screener.py --top 10` | Scores watchlist → ranked CC/CSP/PS candidates |
| "Any put credit spreads?" / "Defined-risk ideas?" | `python3 scripts/screener.py --ps-only` | Put credit spreads only (defined-risk income) |
| "How are my positions?" / "Portfolio health?" | `python3 scripts/portfolio.py --health` | Scores every holding → decisions + overlap + guardrails |
| "What's my P&L?" / "Show me everything" | `python3 scripts/portfolio.py` | Full picture: positions, scores, exit decisions, overlap, income, guardrails |
| "What's V doing?" / "Check AAPL" | `python3 scripts/market_data.py TICKER --options` | Deep dive one ticker |
| "What's the macro?" / "Market outlook?" | `python3 scripts/market_sentiment.py` | VIX, yields, regime, sentiment |
| "Quick check on my options" | `python3 scripts/portfolio.py --fast` | Fast P&L table + assignment cost |

### Step 3: Apply Rules (from GOAL.md + config/rules.yaml)
- Check regime → position sizing. Is CSP allowed right now?
- Check concentration → is any position > 15%? Any sector > 25%?
- Check CSP pause triggers → VIX > 25? Cash < 20%?
- Every recommendation must reference the specific rule that allows or blocks it.

### Step 4: Supplement with Web Search (only if needed)
Use WebSearch to gather current news, analyst actions, and sector context for the stocks in play. This adds narrative depth after the deterministic engine has run.

**Never use web search INSTEAD of the local engine. Use it to ADD context after the local engine runs.**

### Step 5: Format the Answer
1. Portfolio snapshot (cash, positions, CSP liability)
2. Regime check (VIX, position size allowed, CSP pause status)
3. Specific recommendations with rule references
4. Risk alerts (concentration, margin, earnings, expiry)

### Anti-Patterns — NEVER Do This
- ❌ Give generic options advice without running local scripts
- ❌ Suggest a trade without checking config/rules.yaml constraints
- ❌ Recommend a stock not in the watchlist without flagging it
- ❌ Skip the CSP pause check before suggesting new CSPs
- ❌ Use web search sentiment/news as the primary decision driver

## Hard Constraints (Non-Negotiable)

- **Covered Calls only**: must own 100 shares of the underlying per contract before selling a call.
- **Cash Secured Puts only**: must hold enough cash to buy 100 shares at the strike price per contract.
- **Put credit spreads (PS)** — suggestion-only, defined-risk: `max_loss = width − net_credit` must be 100% cash-backed; net credit ≥ 1/3 of width; width ≤ config cap; short-leg delta in regime range. Not auto-executed. See `src/strategies/credit_spread.py`.
- **No margin, no naked options, no debit spreads, no iron condors, no butterflies.**
- Every trade recommendation must include a collar check: verify the position remains covered/cash-secured at all legs.
- Do NOT suggest trades that violate these constraints, even hypothetically.
- **Data freshness**: NEVER proceed with analysis on stale data. Sync from moomoo before every run. If sync fails, abort — do not use cached data silently.

### When to use a put credit spread vs a plain CSP

A put credit spread (PCS) is the right substitute for a CSP when you want the same directional/IV thesis but **not** the full-strike capital outlay or the assignment obligation:

| Situation | Use |
|-----------|-----|
| CSP is paused (VIX>25 / cash<20% / VOLATILE+ regime / stock >15% off basis) | **PCS** — defined-risk income while the wheel can't run |
| You want the thesis but NOT the shares (pure premium play) | **PCS** — no assignment beyond the long strike |
| Strike too large to fully cash-secure with available cash | **PCS** — capital at risk is max_loss, a fraction of the strike |
| Bullish regime, you WANT the shares at a good basis (wheel entry), CSP allowed | **CSP** — don't spread what you want to be assigned |

## Portfolio Starting State

| Asset | Quantity / Value | Notes |
|-------|-----------------|-------|
| V (Visa Inc.) | ~430 shares | Core holding to diversify away from |
| Cash | ~$45,000 USD | Dry powder for CSP assignment and new positions |

## High-Level Goal

**Diversify out of concentrated Visa position into other high-quality stocks** while generating income through covered calls and cash secured puts. Target allocation diversified across sectors (tech, financial, healthcare, consumer, energy) with:
- Strong balance sheets and moats
- Reasonable valuations (P/E, FCF yield, PEG)
- Liquid options chains with tight bid-ask spreads

## Key Metrics for Option Selection

- **Delta**: CC 0.20–0.30, CSP 0.20–0.30 (normal regime). See regime-adjusted deltas below.
- **DTE**: 30–45 days for optimal theta decay.
- **IV Rank > 30**: sell premium when volatility is elevated relative to its own history.
- **Annualized return on capital**: target >12% annualized for CSPs, >8% for covered calls.
- **Earnings blackout**: no new positions 2 weeks before earnings unless explicitly researched.

## CC/CSP Allocation & Regime Rules (from GOAL.md Actions)

**Always reference GOAL.md Actions section before making any trade recommendation.** These rules override any conflicting defaults.

### Capital Allocation by Regime

| Regime | VIX | Position Size | Cash Reserve | CSP Delta | CC Delta |
|--------|-----|:---:|:---:|:---:|:---:|
| BULLISH | < 12 | 80% | ≥ 15% | 0.20-0.30 | 0.20-0.30 |
| NEUTRAL | 12-20 | 75% | ≥ 20% | 0.20-0.30 | 0.20-0.30 |
| CAUTIOUS | 20-25 | 50% | ≥ 25% | 0.15-0.25 | 0.25-0.35 |
| VOLATILE | 25-30 | 25% | ≥ 30% | 0.10-0.20 | 0.30-0.40 |
| BEARISH | > 30 | 0% | ≥ 35% | NONE | existing only |

### CSP Pause Triggers (stop new CSPs if ANY true)
- VIX > 25 | SPY < 200 SMA | Regime ≤ -2 | Cash reserve < 20% | Stock > 15% below cost basis

### Credit-Stress Hard Gate
- HYG/IEF credit regime STRESSED → position size capped at 50% regardless of vote tally (`regime.credit_stress_position_mult_cap`)

### Hard Position Limits (config/rules.yaml is the source of truth)
- Single position ≤ 25% net liq hard cap (15% in EMERGENCY stage) | Sector ≤ 25% | CSP deployed ≤ 25% net liq (≤ 10% volatile, ≤ 15% EMERGENCY) | Open option positions ≤ 10
- Collar rule: CC only on ≥100 FREE shares (net of shares committed to open short calls)

## Paper Portfolio Database (SQLite — OIE only)

- **Location**: `db/oie_paper.db` — created at runtime, NEVER committed (.gitignored)
- **Schema**: defined in `src/data/oie_db.py` `_init_schema` — code IS the schema documentation
- **Tables**: `paper_state` (key-value), `paper_positions`, `paper_trades` (full audit log — every cash movement is a `cash_change` row), `paper_snapshots`
- **Cash invariant**: single-writer — all cash flows through `_log_trade(cash_impact=...)`; derived cash (`seeded_cash + Σ cash_change`) always equals stored state. The engine never mutates cash locally.
- **Real-account data is NOT persisted** — portfolio/order/position state is fetched live from moomoo each run (see `specs/architecture-spec.md`).

## Data Sources

### Primary: Moomoo API (via MCP server)
- MCP server: https://github.com/baladengale/moomoo-api-mcp
- API docs: https://openapi.moomoo.com/moomoo-api-doc/en/intro/intro.html
- Quote API: https://openapi.moomoo.com/moomoo-api-doc/en/quote/overview.html
- Trade API: https://openapi.moomoo.com/moomoo-api-doc/en/trade/overview.html

### Fallback: Yahoo Finance (yfinance)
- Only when moomoo is unreachable
- Mark data_source as 'YFINANCE_FALLBACK' in sync report

### Other Free Sources (last resort)
- Yahoo Finance, Google Finance, or other free sources if both moomoo and yfinance fail

## Watchlist Universe

Watchlist lives in `config/rules.yaml → watchlist` (moomoo group `Options` is the live source; YAML is the fallback). Default universe (research required before acting):
- V, MSFT, GOOGL, AAPL, AMZN, NVDA, META, AVGO, CRM, AMD, TSLA, BE
- Diversification candidates per sector: `watchlist.diversify` (JPM/BAC/GS · ABBV/JNJ/UNH · KO/WMT/PG · XOM/CVX · CAT/GE/HON)

Not an endorsement — each must pass the full research workflow below.

## Research Workflow (Per Stock)

```
Sync from Moomoo → Trend/Momentum → Options Chain → Fundamental → Correlation → Sentiment → Signal → Score
```

## Scoring Engine (Deterministic)

The WHEEL_SCORE is a weighted composite (0-100) with 5 components (weights in `config/rules.yaml → scoring.weights`):
- **Trend/Momentum** (25%): SMA alignment (20/50/200), ADX strength, RSI(14), MACD
- **Options Chain** (25%): spread, OI, volume, IV/HV, term structure
- **Fundamental** (15%): revenue growth, EPS quality, FCF yield, D/E, PEG
- **External Sentiment** (20%): deterministic composite of trend + momentum + IV + volume + price action
- **Macro Risk** (15%): regime positioning (VIX band, breadth, credit)

14 hard constraint gates. Any single failure → score = 0, signal = AVOID.

## Signal Generator

Signals are deterministic from thresholds:
- **STRONG_WRITE**: all systems go — trend, IV, sentiment, options quality all above thresholds
- **WRITE**: cautious green — proceed with reduced size
- **HOLD**: wait — one or more factors not supportive
- **AVOID**: red light — constraint failure or sentiment too negative

## Daily Digest

Generated once per day. Contains:
- Portfolio summary (total value, cash, positions)
- Market regime classification (BULLISH/NEUTRAL/VOLATILE/BEARISH from VIX + SPY trend)
- Top signals ranked by composite score
- Action items (specific trade recommendations)
- Risk alerts
- AI-generated narrative (runtime only, appended after deterministic content)

## Architecture Principles

- **Analysis-first**: research modules before execution modules. No trade code without analysis backing.
- **Python-first**: use Python for data analysis, backtesting, and option chain processing (pandas, numpy, yfinance, openbb or similar).
- **Notebooks for research**: each stock under consideration gets its own research notebook tracking the analysis journey and decision rationale.
- **Config-driven parameters**: portfolio state, watchlists, position sizing rules, and strategy parameters live in config files, never hardcoded.
- **Paper-trade before live**: every strategy change runs against historical data before touching real money.
- **Config-driven**: all thresholds live in `config/rules.yaml`; YAML is the single source of truth for limits, and the OIE paper DB (`db/oie_paper.db`) is the audit trail. Real-account state is always fetched live, never persisted.
- **Freshness-first**: sync from moomoo before every analysis run. Never proceed with stale data.

## AI Runtime Boundary

AI is used ONLY for (after all deterministic computation is complete):
1. **Sentiment narrative**: human-readable explanation of the deterministic sentiment score
2. **Macro reasoning**: market context for the daily digest
3. **Edge case judgment**: explaining data conflicts (the math still drives the decision)

AI is NEVER used for:
- Computing any numerical score (all formula-driven)
- Checking constraints (all boolean logic)
- Position sizing (all arithmetic)
- Signal generation (all threshold-based)
- Trade execution (all rule-based)

## Trade Execution Rules

1. **One trade at a time**: throughly analyze overall margin and trade status before each trade.
2. **Pre-trade checklist**: all 14 constraints must pass + collar check must return all_clear.
3. **Capital preservation**: priority goal. Earn premium and let stocks assign if required.
4. **Wheel rotation**: CC on V → assigned → CSP on new stock → assigned → CC on new stock → repeat.

## Test Quality Gates

Before any commit:
```bash
pytest tests/ -v --tb=short
pytest tests/ --cov=src --cov-report=term --cov-fail-under=85
```

Key test files and what they validate:
- `tests/test_screener_scoring.py` — ticker scoring + contract penalty with exact expected values
- `tests/test_screener_score.py` — scoring engine integration tests
- `tests/test_holding_score.py` — holding/option scoring with stop-loss decisions
- `tests/test_profit_management.py` — trend-modulated profit-target decision core
- `tests/test_exit_management.py` — single exit decision core (profit + loss sides); the 5 V-call CC-autonomy regression fixtures
- `tests/test_trend.py` — SMA/MACD/RSI/ADX formula correctness + signal generator
- `tests/test_oie_db.py` — OIE paper DB CRUD operations
- `tests/test_oie_simulation.py` — OIE simulation + lifecycle tests
- `tests/test_overlap.py` — Put/call overlap analysis
- `tests/test_holdings_exit.py` — Exit framework: dead zone, backstop, circuit breaker
- `tests/test_portfolio_loader.py` — Portfolio loader data fetching
- `tests/test_portfolio_summary.py` — P&L/income/sector computation

## Architecture Principles (Code)

- **Layered**: `src/` owns all logic; `scripts/` are thin orchestrators only
- **No cross-script imports**: `oie_engine.py` imports from `src/`, never from `scripts/screener.py`
- **Config-driven**: every threshold in `config/rules.yaml` → `src/config.py`. No hardcoded values in scripts.
- **Shared filters**: `src/filters/contract_filters.py` is the single source of truth for all contract gates
- **Shared models**: `src/data/models.py` — `StockSnapshot`, `OptionSnapshot`, `TradeCandidate`
- **Shared data**: `src/data/` — `fetch_live_portfolio()`, `fetch_live_watchlist()`, `get_option_snapshots_resilient()`
- **Paper-trade before live**: every strategy change runs against historical data before touching real money
- **Freshness-first**: sync from moomoo before every analysis run. Never proceed with stale data
