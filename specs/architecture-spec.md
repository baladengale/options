# SPEC: Architecture — Layered Design (2026-07-19)

**Status**: Implemented
**Applies to**: All `src/` and `scripts/` modules

---

## 1. Layer Model

```
┌──────────────────────────────────────────────┐
│  scripts/  — Thin wrappers (argparse → src/) │
│  portfolio.py, screener.py, oie_engine.py,   │
│  market_data.py, market_sentiment.py         │
├──────────────────────────────────────────────┤
│  src/scoring/  — Scoring engines             │
│  screener_score.py, holding_score.py         │
├──────────────────────────────────────────────┤
│  src/filters/  — Shared contract gates       │
│  contract_filters.py                         │
├──────────────────────────────────────────────┤
│  src/risk/  — Risk analysis                  │
│  overlap.py, holdings_exit.py, monitor.py    │
├──────────────────────────────────────────────┤
│  src/analysis/  — Macro + sentiment          │
│  sentiment.py, thesis.py                     │
├──────────────────────────────────────────────┤
│  src/data/  — Data access layer              │
│  moomoo_client.py, yfinance_client.py,       │
│  portfolio_loader.py, watchlist.py,          │
│  guardrails.py, models.py, compute.py        │
├──────────────────────────────────────────────┤
│  src/portfolio/  — Portfolio display         │
│  summary.py                                  │
├──────────────────────────────────────────────┤
│  config/rules.yaml  — Single source of truth │
│  src/config.py  — Typed accessor             │
└──────────────────────────────────────────────┘
```

**Rule**: Higher layers may import from lower layers. No upward or sideways imports between scripts. `scripts/` never imports from `scripts/`.

---

## 2. Script ↔ src/ Contract

Each script in `scripts/` follows this pattern:

```python
# 1. argparse
# 2. Fetch data from src/data/ (moomoo, yfinance, portfolio)
# 3. Call src/filters/ for gates
# 4. Call src/scoring/ for scores
# 5. Call src/risk/ or src/data/guardrails for risk checks
# 6. Format and print output
```

**No script contains**: gate logic, scoring formulas, contract penalty calculations, position sizing math, RoC formulas. These all live in `src/`.

---

## 3. Module Reference

### `src/filters/contract_filters.py`
**Purpose**: Single source of truth for all contract-level filtering. Used by screener.py, oie_engine.py, and holding_score.py.

**Functions**:
| Function | Signature | Logic |
|----------|-----------|-------|
| `passes_liquidity` | `(contract, cfg) -> bool` | bid > 0, OI ≥ `cfg.oi_min` (500), vol ≥ `cfg.volume_min` (10) |
| `passes_delta` | `(contract, strategy, regime, cfg) -> (bool, str)` | delta in `cfg.delta_range(strategy, regime)`; CSP also `abs_d ≤ 0.70` deep-ITM |
| `iv_sane` | `(contract) -> bool` | `0 < IV < 500` |
| `passes_vrp` | `(contract, hv_30d) -> bool` | `IV > HV × 0.8` |
| `passes_roc` | `(roc, strategy, cfg) -> bool` | RoC ≥ `cfg.roc_min_csp` (12%) or `cfg.roc_min_cc` (8%) |
| `cc_roc` | `(bid, price, dte) -> float` | `(bid/price) × (365/DTE) × 100` |
| `csp_roc` | `(bid, strike, dte) -> float` | `(bid/strike) × (365/DTE) × 100` |
| `passes_concentration` | `(capital, net_liq, cfg) -> bool` | `capital ≤ net_liq × max_single_position_pct` |
| `passes_cash_buffer` | `(capital, cash, net_liq, bp, cfg) -> bool` | cash ≥ 10% NLV, capital ≤ 80% BP |
| `passes_all_gates` | `(contract, strategy, regime, snap, cfg, ...) -> (bool, str)` | Orchestrator — runs all gates, returns reason on failure |

**Code reference**:
```python
# Usage pattern in scripts:
from src.filters.contract_filters import passes_all_gates

ok, reason = passes_all_gates(
    contract, 'CSP', regime, snap,
    skip_concentration=args.force,
    skip_cash_buffer=args.force,
    net_liq=total_nlv, cash=CASH + FUND, buying_power=BUYING_POWER)
if not ok:
    continue  # reason = 'liquidity', 'VRP', 'RoC X% below min', etc.
```

### `src/scoring/screener_score.py`
**Purpose**: Ticker-level scoring + contract penalty. Shared by screener.py and oie_engine.py (direct import).

**Key functions**: `_compute_ticker_score()`, `_contract_penalty()`, `_csp_roc()`, `_score_stars()`, `_reason()`, `_compute_chain_gex()`

**Config dependency**: All threshold values read from `config/rules.yaml` via `_cfg_val()` and `get_config().contract_penalty()`.

### `src/scoring/holding_score.py`
**Purpose**: Score existing holdings + find best CC candidates. Used by portfolio.py.

**Key functions**: `_score_holding()`, `_find_best_cc()`, `_score_option()`

**Uses**: `src/filters/contract_filters` for gates, `screener_score._contract_penalty()` for penalty (no longer has its own).

### `src/data/guardrails.py`
**Purpose**: Portfolio-level risk checks. Used by portfolio.py, screener.py, oie_engine.py.

**Key class**: `GuardrailChecker` — `check()` returns `GuardrailReport` with `blocks` and `warnings`. `check_new_trade()` simulates adding a trade.

**Config dependency**: All limits from `config/rules.yaml` → `position_limits.*`.

### `src/data/models.py`
**Shared dataclasses**: `StockSnapshot` (price, technicals, fundamentals), `OptionSnapshot` (Greeks, IV, OI), `TradeCandidate` (screened option recommendation), `OptionChainBundle`, `MarketRegime`.

### `src/data/watchlist.py`
**Purpose**: Live watchlist fetch from moomoo. Single source — used by both screener.py and oie_engine.py.

**Function**: `fetch_live_watchlist(moomoo_ctx, group_name=None) -> list[str]`

### `src/data/portfolio_loader.py`
**Purpose**: Real portfolio data from moomoo.

**Functions**: `fetch_portfolio() -> Portfolio`, `fetch_live_portfolio() -> tuple`, `fetch_portfolio_and_orders() -> (Portfolio, orders)`

### `src/data/moomoo_client.py`
**Purpose**: Moomoo OpenD data client.

**Key methods**: `get_stock_snapshot()`, `get_stock_snapshots()`, `get_price_history()`, `get_option_snapshots()`, `get_option_snapshots_resilient()`, `get_all_option_snapshots()`.

---

## 4. Import Dependency Graph

```
scripts/screener.py ──────→ src/filters/contract_filters.py
                           src/scoring/screener_score.py
                           src/data/{moomoo_client, watchlist, portfolio_loader, models}
                           src/analysis/sentiment.py

scripts/oie_engine.py ────→ src/filters/contract_filters.py
                           src/scoring/screener_score.py
                           src/data/{moomoo_client, watchlist, portfolio_loader, models, oie_db, guardrails}
                           src/analysis/sentiment.py
                           (NO LONGER imports from scripts.screener ✓)

scripts/portfolio.py ─────→ src/scoring/holding_score.py → src/filters/contract_filters.py
                           src/risk/{holdings_exit, overlap}
                           src/portfolio/summary.py
                           src/data/{portfolio_loader, moomoo_client, guardrails, models}
                           src/analysis/{sentiment, thesis}

scripts/market_data.py ───→ src/data/{moomoo_client, compute}
scripts/market_sentiment.py → src/data/{yfinance_client, moomoo_client}
                              src/analysis/sentiment.py
```

---

## 5. Config Flow

```
config/rules.yaml
       │
       ▼
src/config.py  (typed accessors: cfg.oi_min, cfg.delta_range('csp','NEUTRAL'), ...)
       │
       ├──→ src/filters/contract_filters.py  (gate thresholds)
       ├──→ src/scoring/screener_score.py    (scoring weights + penalty values)
       ├──→ src/scoring/holding_score.py     (stop-loss thresholds)
       ├──→ src/data/guardrails.py           (position limits)
       └──→ src/data/yfinance_client.py      (position_mult per regime)
```

**No hardcoded values in scripts.** Every threshold, weight, penalty, and limit has a path through `rules.yaml` → `config.py` → consumer.

---

## 6. Verification

```bash
# Architecture integrity checks
python3 -c "from src.filters.contract_filters import passes_all_gates"  # filters load
python3 -c "from src.data.models import TradeCandidate"                 # shared models
python3 -c "from src.data.watchlist import fetch_live_watchlist"        # shared data
python3 scripts/oie_engine.py test                                      # OIE self-test
pytest tests/ -v --tb=short                                             # full suite

# No script-to-script imports (enforced by code review)
grep -rn "from scripts\." scripts/  # should return nothing
```
