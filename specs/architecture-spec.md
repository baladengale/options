# SPEC: Architecture — Layered, Read-Only + Paper-DB

**Status**: Implemented & audited (2026-08-09)
**Applies to**: All `src/` and `scripts/` modules
**Supersedes**: the DB-centric design in the old `SPECS.md` (see `specs/README.md`)

---

## 1. The one-sentence summary

> The real account is **read live from moomoo on every run** — there is no real-account database. The only database is `db/oie_paper.db`, owned exclusively by the OIE paper engine. Everything else is a deterministic function over freshly-fetched data.

This is the single most important architectural fact, and the place where the original `SPECS.md` was wrong: it described a `SyncEngine` writing into SQLite tables (`portfolio_snapshots`, `holdings`, `orders`, `options_chain_cache`, `price_history`, `signals_log`, `daily_digest`). **None of those tables exist.** That design is preserved in `research_backtesting_architecture.md` as a *future* backtest-harness reference, not a description of today's system.

---

## 2. Layer model

```
┌─────────────────────────────────────────────────────────────────┐
│  scripts/  — Thin wrappers (argparse → src/ → print)            │
│  portfolio.py · screener.py · oie_engine.py                     │
│  market_data.py · market_sentiment.py · decision_review.py      │
├─────────────────────────────────────────────────────────────────┤
│  src/analysis/  — The decision core (pure functions)            │
│  exit_management · profit_management · thesis · thesis_validator│
│  trend · sentiment · adaptive_profit · roll_first · correlation │
├─────────────────────────────────────────────────────────────────┤
│  src/scoring/  — Ticker + contract + holding scoring            │
│  screener_score (ticker 1-10 + contract penalty)                │
│  holding_score (option CLOSE/HOLD/ROLL decisions)               │
├─────────────────────────────────────────────────────────────────┤
│  src/filters/  — Shared contract gates                          │
│  contract_filters (liquidity, delta, IV, VRP, RoC, conc., cash)│
├─────────────────────────────────────────────────────────────────┤
│  src/strategies/ — Strategy-specific scoring                    │
│  credit_spread (put credit spread / PS, suggestion-only)        │
├─────────────────────────────────────────────────────────────────┤
│  src/risk/  — Risk analysis                                     │
│  holdings_exit · overlap · monitor (roll discipline)            │
│  collar_check (coverage verification)                           │
├─────────────────────────────────────────────────────────────────┤
│  src/guardrails/ — Staged position limits                       │
│  limits (StagedGuardrails: EMERGENCY/TARGET/COMFORT)            │
├─────────────────────────────────────────────────────────────────┤
│  src/data/  — Data access (the ONLY layer that touches I/O)     │
│  moomoo_client (read-only quotes) · yfinance_client (fallback)  │
│  portfolio_loader (read-only account) · watchlist · compute     │
│  guardrails (per-trade checker) · models · oie_db (paper ONLY)  │
├─────────────────────────────────────────────────────────────────┤
│  src/portfolio/summary · src/system/scheduler                   │
├─────────────────────────────────────────────────────────────────┤
│  config/rules.yaml — Single source of truth                     │
│  src/config.py — Typed accessor (cached singleton)              │
└─────────────────────────────────────────────────────────────────┘
```

**Rule**: Higher layers may import from lower layers. No upward or sideways imports. **`scripts/` never imports from `scripts/`.** Verified by: `grep -rn "from scripts\." scripts/` → nothing.

---

## 3. Script ↔ src/ contract

Every script follows this pattern:
```python
# 1. argparse
# 2. Fetch data from src/data/ (moomoo, yfinance, portfolio)
# 3. Call src/filters/ for gates
# 4. Call src/scoring/ for scores
# 5. Call src/analysis/ for exits, src/risk or src/data/guardrails for risk
# 6. Format and print output
```

**No script contains**: gate logic, scoring formulas, contract penalty calculations, position sizing math, RoC formulas, exit decisions. These all live in `src/`.

---

## 4. Module reference

### Data layer — `src/data/` (the only I/O boundary)

| Module | Role | I/O |
|--------|------|-----|
| `moomoo_client.py` | Read-only wrapper over `OpenQuoteContext` (quotes/chain/history). **Never submits orders.** Caches chain codes + price history per session. ~3 calls/sec throttle. | moomoo OpenD `127.0.0.1:11111` |
| `portfolio_loader.py` | Read-only wrapper over `OpenSecTradeContext` (account/positions/orders). **Never submits orders.** Handles HKD→USD FX (live yfinance rate, banded 7.5–8.2, fallback 7.8). | moomoo OpenD |
| `yfinance_client.py` | Fallback for fundamentals/sentiment/price history when moomoo is unreachable. | yfinance API |
| `compute.py` | **Pure math** — SMA, EMA, RSI (Wilder), MACD, ATR, ADX, Bollinger, HV(30), beta, IV Rank/Percentile, max pain, ATM IV, 25Δ skew, term structure, GEX. No I/O. | — |
| `watchlist.py` | `fetch_live_watchlist()` — reads the moomoo watchlist group; falls back to `config/rules.yaml → watchlist.default`. | moomoo |
| `guardrails.py` | `GuardrailChecker` — per-trade/daily block/warn checks. Used by OIE + screener. | — |
| `oie_db.py` | **Paper DB only** — SQLite at `db/oie_paper.db`. 4 tables (see §6). | local file |
| `models.py` | Shared dataclasses: `StockSnapshot`, `OptionSnapshot`, `TradeCandidate`, `OptionChainBundle`, `MarketRegime`, `Funds`, `Portfolio`. | — |

### Filters — `src/filters/contract_filters.py`
Single source of truth for all contract gates. Pure functions, thresholds from config.
- `passes_liquidity`, `passes_delta`, `iv_sane`, `passes_vrp`, `passes_roc`, `passes_concentration`, `passes_cash_buffer`
- `csp_roc(bid, strike, dte)`, `cc_roc(bid, price, dte)` — annualized RoC formulas
- `passes_all_gates(...)` — orchestrator returning `(bool, reason)`

### Scoring — `src/scoring/`
- `screener_score.py` — `_compute_ticker_score()` (5-dimension 1–10, lower=better), `_contract_penalty()`, `_csp_roc()`, `_score_stars()`, `_compute_chain_gex()`. Shared by `screener.py` and `oie_engine.py` (direct import).
- `holding_score.py` — `_score_holding()`, `_find_best_cc()`, `_score_option()` (the option decision layer that consumes `profit_management`).

### Analysis — `src/analysis/` (the decision core)
- `exit_management.py:88` — **`decide_exit_action()`** the single exit decision core (profit + loss).
- `profit_management.py:65` — **`decide_profit_target()`** trend-modulated profit booking (the strategy-direction asymmetry).
- `thesis.py` — 5 codable deterioration gates (growth stall, dual deceleration, margin erosion, balance sheet, cash flow).
- `thesis_validator.py` — 5 CRITICAL/WARNING gates (earnings, fundamental/P/E, technical, volatility, price perf).
- `trend.py` — SMA alignment, ADX strength, RSI score, MACD score, composite.
- `sentiment.py` — deterministic fetcher + analyst/news/earnings scorers (feeds ticker-score dim 4).
- `adaptive_profit.py`, `roll_first.py`, `correlation.py` — supporting analysis.

### Risk — `src/risk/`
- `holdings_exit.py` — stock-leg loss framework (dead zone, conditional backstop, hard circuit breaker, time stop).
- `overlap.py` — straddle/strangle/stacked-call detection across a book.
- `monitor.py` — assignment handlers, roll-discipline check, campaign accounting, concentration.
- `collar_check.py` — verify every CC has shares and every CSP has cash.

### Guardrails — two distinct classes
- `src/data/guardrails.py` `GuardrailChecker` — **per-trade/daily**, binary BLOCK/WARN on fixed limits. Wired into the OIE cycle and screener.
- `src/guardrails/limits.py` `StagedGuardrails` — **staged recovery** (EMERGENCY/TARGET/COMFORT), adapts limits to cash-buffer health. Narrative layer for portfolio health.

> Both classes load from `config/rules.yaml` but read different sections (`position_limits` vs `guardrail_limits`). See [guardrails-and-risk-spec.md](guardrails-and-risk-spec.md).

---

## 5. Import dependency graph

```
scripts/screener.py ──────→ src/filters/contract_filters.py
                           src/scoring/screener_score.py
                           src/data/{moomoo_client, watchlist, portfolio_loader, models}
                           src/analysis/sentiment.py
                           src/strategies/credit_spread.py

scripts/oie_engine.py ────→ src/filters/contract_filters.py
                           src/scoring/screener_score.py
                           src/data/{moomoo_client, watchlist, portfolio_loader, models, oie_db, guardrails}
                           src/analysis/{exit_management, sentiment}
                           (NO import from scripts.screener ✓)

scripts/portfolio.py ─────→ src/scoring/holding_score.py → src/filters/contract_filters.py
                           src/risk/{holdings_exit, overlap, collar_check}
                           src/portfolio/summary.py
                           src/data/{portfolio_loader, moomoo_client, guardrails, models}
                           src/analysis/{sentiment, thesis, thesis_validator}

scripts/market_data.py ───→ src/data/{moomoo_client, compute}
scripts/market_sentiment.py → src/data/{yfinance_client, moomoo_client} · src/analysis/sentiment.py
scripts/decision_review.py → src/data/portfolio_loader · yfinance (historical prices)
```

---

## 6. The paper DB — `db/oie_paper.db`

The **only** SQLite database in the system. Four tables (schema in `src/data/oie_db.py:43` `_init_schema`):

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `paper_state` | Key-value engine state | `cash, fund, seeded_at, seeded_cash, seeded_fund, last_cycle, cycle_count` |
| `paper_positions` | Stocks + options | `id, ticker, pos_type (STOCK/CALL/PUT), status (ACTIVE/CLOSED/EXPIRED/ASSIGNED), qty (shares>0 / contracts<0 short), cost_price, strike, expiry, dte_initial, entry_premium, current_bid, current_delta, exit_reason, realized_pnl` |
| `paper_trades` | Full audit log | `ts, event (SEED/OPEN_CALL/OPEN_PUT/CLOSE/EXPIRE/ASSIGN_CSP/ASSIGN_CC/SNAPSHOT/ERROR/CYCLE/RECONCILE), ticker, pos_id, detail, cash_change` |
| `paper_snapshots` | Net-liq over time | `ts, total_value, cash, stock_value, fund_value, option_premium_received, option_liability, unrealized_pnl, realized_pnl_total, open_positions` |

**Cash is derived, never stale**: `show_status` computes `seeded_cash + Σ cash_change` rather than reading a stored value. See [oie-paper-engine-spec.md](oie-paper-engine-spec.md).

---

## 7. Config flow

```
config/rules.yaml
       │
       ▼
src/config.py  (typed accessors: cfg.oi_min, cfg.delta_range('csp','NEUTRAL'), cfg.profit_take('csp.base_pct') ...)
       │
       ├──→ src/filters/contract_filters.py  (gate thresholds)
       ├──→ src/scoring/screener_score.py    (scoring weights + penalty values)
       ├──→ src/scoring/holding_score.py     (stop-loss thresholds)
       ├──→ src/analysis/profit_management.py (profit targets + trend inputs)
       ├──→ src/analysis/exit_management.py   (delta gates, premium stops)
       ├──→ src/data/guardrails.py            (position limits — per-trade layer)
       ├──→ src/guardrails/limits.py          (staged limits — recovery layer)
       └──→ src/risk/{holdings_exit, monitor} (exit framework, roll discipline)
```

**No hardcoded values in scripts or analysis modules.** Every threshold, weight, penalty, and limit has a path through `rules.yaml` → `config.py` → consumer.

---

## 8. Verification commands

```bash
# Architecture integrity
python3 -c "from src.filters.contract_filters import passes_all_gates"  # filters load
python3 -c "from src.data.models import TradeCandidate"                 # shared models
python3 -c "from src.data.watchlist import fetch_live_watchlist"        # shared data
python3 scripts/oie_engine.py test                                      # OIE self-test (no OpenD)

# No script-to-script imports (enforced)
grep -rn "from scripts\." scripts/   # must return nothing

# No real-order submission surface
grep -rn "place_order\|TrdEnv.SIMULATE" src/ scripts/ skills/  # only in docs/backtest research
```
