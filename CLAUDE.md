# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Options trading analysis and execution system with a hard constraint: **only Covered Call and Cash Secured Put strategies are permitted.** No naked options, no spreads, no margin trading.

The system is a **deterministic data-driven engine** that:
1. Syncs watchlist, portfolio, orders, and funds from moomoo into a local SQLite database
2. Screens all watchlist tickers for CC and CSP candidates
3. Computes trend/momentum, sentiment, and composite scores via deterministic formulas
4. Generates a daily digest with portfolio summary, signals, and action items
5. Uses AI ONLY at runtime for sentiment narrative and macro reasoning (never for scoring math)

## Hard Constraints (Non-Negotiable)

- **Covered Calls only**: must own 100 shares of the underlying per contract before selling a call.
- **Cash Secured Puts only**: must hold enough cash to buy 100 shares at the strike price per contract.
- **No margin, no naked options, no spreads, no iron condors, no butterflies.**
- Every trade recommendation must include a collar check: verify the position remains covered/cash-secured at all legs.
- Do NOT suggest trades that violate these constraints, even hypothetically.
- **Data freshness**: NEVER proceed with analysis on stale data. Sync from moomoo before every run. If sync fails, abort — do not use cached data silently.

## Portfolio Starting State

| Asset | Quantity / Value | Notes |
|-------|-----------------|-------|
| V (Visa Inc.) | ~430 shares | Core holding to diversify away from |
| Cash | ~$45,000 USD | Dry powder for CSP assignment and new positions |

## High-Level Goal

**Diversify out of concentrated Visa position into other high-quality tech stocks** while generating income through covered calls and cash secured puts. Target allocation is a basket of tech stocks with:
- Strong balance sheets and moats
- Reasonable valuations (P/E, FCF yield, PEG)
- Liquid options chains with tight bid-ask spreads

## Key Metrics for Option Selection

- **Delta**: 0.20–0.30 for covered calls (income tilt) or 0.15–0.25 for CSPs (higher probability OTM).
- **DTE**: 30–45 days for optimal theta decay.
- **IV Rank > 30**: sell premium when volatility is elevated relative to its own history.
- **Annualized return on capital**: target >12% annualized for CSPs, >8% for covered calls.
- **Earnings blackout**: no new positions 2 weeks before earnings unless explicitly researched.

## Local File-Based Database (SQLite)

- **Location**: `db/options.db` — created at runtime, NEVER committed (.gitignored)
- **Schema**: defined in `src/db/schema.py` — code IS the schema documentation
- **Tables**: portfolio_snapshots, holdings, orders, open_positions, watchlist, options_chain_cache, price_history, signals_log, daily_digest
- **Freshness**: Every table row has `synced_at`. Before any analysis, sync from moomoo and verify freshness. Stale data = abort.
- **Sync engine**: `src/db/sync.py` orchestrates all data refresh. Runs before every recommendation/digest.

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

Target tech stocks with liquid options chains. Example universe (research required before acting):
- MSFT, GOOGL, AAPL, AMZN, NVDA, META, AVGO, ADBE, CRM, AMD

Not an endorsement — each must pass the full research workflow below.

## Research Workflow (Per Stock)

```
Sync from Moomoo → Trend/Momentum → Options Chain → Fundamental → Correlation → Sentiment → Signal → Score
```

## Scoring Engine (Deterministic)

The WHEEL_SCORE is a weighted composite (0-100) with 5 components:
- **Trend/Momentum** (25%): SMA alignment (20/50/200), ADX strength, RSI(14), MACD
- **Options Chain** (25%): spread, OI, volume, IV/HV, term structure
- **Fundamental** (20%): revenue growth, EPS quality, FCF yield, D/E, PEG
- **Sentiment** (15%): deterministic composite of trend + momentum + IV + volume + price action
- **Correlation** (15%): 1Y rolling correlation vs V — if >0.8, hard fail

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
- **DB-driven**: all operational data in SQLite. YAML configs are for initial seed and manual overrides only. The DB is the runtime source of truth.
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
- `tests/test_scoring.py` — composite scoring with exact expected values
- `tests/test_constraints.py` — all 14 constraint gates
- `tests/test_trend.py` — SMA/MACD/RSI/ADX formula correctness
- `tests/test_sentiment.py` — sentiment score sub-components
- `tests/test_db_sync.py` — DB sync, freshness, staleness rejection
- `tests/test_signals.py` — signal generator decision matrix
- `tests/test_risk.py` — collar check, coverage, assignment handling

## Implementation Order

Follow the phases in SPECS.md Section 16. Build analysis modules before execution modules. Every module must have tests before it's considered complete.
