# SPEC: Config/Rules.yaml Audit — Patch to be Applied

**Status**: Pending — 12 items identified, phased by priority
**Date**: 2026-07-19
**Audit scope**: `config/rules.yaml` vs GOAL.md, CLAUDE.md, `loss-management-playbook.md`, `margin-guardrail.md`, and actual code behavior across 7 source files

---

## Phase 1: Wire the Critical Disconnects (config says X, code does Y)

### Patch 1a — `regime.position_mult` NEVER Read by Code
- **Config**: BULLISH=0.80, NEUTRAL=0.75, CAUTIOUS=0.50, VOLATILE=0.25, BEARISH=0.0
- **Actual runtime** (`yfinance_client.py:660-669`): hardcodes based on `regime_score`: >=3->1.0, >=1->0.75, >=-1->0.50, >=-3->0.25, else->0.0
- **Result**: BULLISH runs at 1.0 (100%) despite config saying 0.80. Decision #9 (cap BULLISH at 80%) was added to config but code never wired.
- **Dead code**: `_regime_multiplier()` in `screener_score.py:311-312` hardcodes {BULLISH:0.85, NEUTRAL:1.0, VOLATILE:1.2, BEARISH:1.5} — imported but never called.
- **Fix**: Have `yfinance_client.py:660-669` read from `cfg.position_mult(regime_label)` instead of hardcoding. Delete dead `_regime_multiplier()`.

### Patch 1b — OI Gate: Config Says 500, Code Uses 10
- **Config**: `options.liquidity.open_interest_min: 500`
- **Code**: `screener.py:285,344`, `oie_engine.py:781`, `holding_score.py:90` all hardcode `(c.open_interest or 0) < 10`
- **Config's 500** only used in `_contract_penalty()` as a scoring tier (OI<500 adds +0.5 penalty), NOT as a gate
- **Fix**: Replace hardcoded `10` with `cfg.oi_min` (500) in all 3 files. Note: this will significantly reduce candidate count — verify with a screener run after applying.

### Patch 1c — OIE Engine Ignores Config Delta Ranges
- **Config**: per-regime CSP/CC delta ranges
- **oie_engine.py:796**: hardcodes CSP `[0.05, 0.30]` for ALL regimes
- **oie_engine.py:823**: hardcodes CC `[0.15, 0.35]` for ALL regimes
- **Fix**: Replace hardcoded ranges with `cfg.delta_range('csp', regime)` and `cfg.delta_range('cc', regime)`. Paper engine should screen identically to the real screener.

---

## Phase 2: Sync the Documentation (GOAL.md / CLAUDE.md vs config)

### Patch 2a — GOAL.md: VIX BULLISH Threshold
- GOAL.md: BULLISH = VIX < 15
- rules.yaml: `vix.complacent: 12` (BULLISH = VIX < 12)
- **Fix**: Update GOAL.md row 1: `< 15` -> `< 12`. Config is correct per playbook Section 7 (low-VIX = asymmetric jump risk).

### Patch 2b — GOAL.md: BULLISH Position Size
- GOAL.md: BULLISH = 100%
- rules.yaml: `position_mult.BULLISH: 0.80` (Decision #9)
- **Fix**: Update GOAL.md row 1: `100%` -> `80%`. Already applied in config.

### Patch 2c — GOAL.md vs Config: CSP Delta Lower Bounds
- GOAL.md: BULLISH/NEUTRAL CSP = 0.20-0.30, CAUTIOUS = 0.15-0.25, VOLATILE = 0.10-0.20
- rules.yaml: BULLISH/NEUTRAL = [0.15, 0.30], CAUTIOUS = [0.10, 0.25], VOLATILE = [0.05, 0.20]
- **Fix**: Tighten config TO match GOAL.md (the tighter floors are safer). Change `options.delta.csp.BULLISH` and `NEUTRAL` from `[0.15, 0.30]` -> `[0.20, 0.30]`.

### Patch 2d — CLAUDE.md: Fix CSP Delta Self-Contradiction
- Line 86 text: "CSP 0.15-0.25 (normal regime)"
- Line 100-101 table: CSP 0.20-0.30 for BULLISH and NEUTRAL
- **Fix**: Update line 86 to match the table: "CSP 0.20-0.30 (normal regime)".

### Patch 2e — CLAUDE.md: Fix Scoring Weights
- CLAUDE.md: Fundamental 20%, Sentiment 15%
- rules.yaml: `fundamental: 0.15`, `external_sentiment: 0.20`
- **Fix**: Update CLAUDE.md lines 150-155. Config is source of truth (code reads from config).

---

## Phase 3: Wire the Loose Ends (hardcoded values that should read config)

### Patch 3a — Cash Buffer Thresholds Hardcoded in Guardrails
- `guardrails.py:78-83`: `MIN_CASH_BUFFER_WARN = 0.25`, `MIN_CASH_BUFFER_CRITICAL = 0.10`
- Config has regime-specific `cash_reserve_pct` (0.15/0.20/0.25/0.30/0.35) — never read by guardrails
- **Fix**: Read from `cfg.cash_reserve_pct(regime)` instead of hardcoding.

### Patch 3b — `max_csp_deployed_pct` — Dead Config
- `max_csp_deployed_pct: 0.25` and `max_csp_deployed_volatile_pct: 0.10` defined in config, exposed in config.py
- Never referenced by any Python file outside config.py
- **Fix**: Wire into GuardrailChecker.check() as a CSP concentration check.

### Patch 3c — Premium Stop-Loss Thresholds Hardcoded
- `holding_score.py:192-216`: stop-loss multiples (3x, 2x, 1.5x) hardcoded
- Config has identical values in `stop_loss.premium_stop` but code never reads them
- **Fix**: Read from `cfg._data['stop_loss']['premium_stop']` instead of hardcoding.

### Patch 3d — `never_sell_below_cost_basis` Defined But Never Read
- Config: `cc_management.never_sell_below_cost_basis: true`
- Code: `holding_score.py:72` hardcodes `allow_below_basis: bool = False` as function default
- **Fix**: Read from config in `_find_best_cc()` instead of hardcoding the default.

---

## What's Already Correct (No Changes Needed)

- `holdings_exit.*` — correctly read from config in portfolio.py
- `position_limits.max_single_position_pct` — correctly consumed everywhere
- `rolling.*` — correctly structured (UNVALIDATED but complete)
- `stop_loss.delta.*` — correctly read by holding_score.py
- `options.dte.*` — correctly consumed by screener penalty logic
- `scoring.weights` — correctly read by `_compute_ticker_score()`
- `cc_management.close_at_profit_pct` — code applies to both CC and CSP in oie_engine.py
- CSP-to-CC ratios — correctly set per regime
- Regime VIX thresholds (12/20/25/30) — correct per playbook evidence

---

## Verification

```bash
# After Phase 1:
python3 -c "from src.config import get_config; c=get_config(); \
  print('OI min:', c.oi_min); \
  print('CSP delta BULLISH:', c.delta_range('csp','BULLISH')); \
  print('Position mult BULLISH:', c.position_mult('BULLISH'))"

pytest tests/ -v --tb=short
python3 scripts/screener.py --top 5 --no-external  # verify OI=500 gate doesn't kill all candidates
python3 scripts/oie_engine.py test                  # verify paper engine still works
```
