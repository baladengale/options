# Guardrails & Risk Spec — Two Layers + Holdings Exit

**Status**: Implemented & audited (2026-08-09)
**Files**: `src/data/guardrails.py`, `src/guardrails/limits.py`, `src/risk/{holdings_exit,collar_check,monitor,overlap}.py`
**Config**: `config/rules.yaml → position_limits`, `guardrail_limits`, `holdings_exit`, `rolling`, `csp_pause`

> Risk is enforced in **two complementary guardrail layers** (per-trade block/warn, and staged recovery) plus a stock-leg holdings-exit framework and a coverage (collar) check. The two guardrail classes are easy to confuse — this spec names them precisely.

---

## 1. The two guardrail classes (don't confuse them)

| | **Layer 1 — per-trade / daily** | **Layer 2 — staged recovery** |
|---|---|---|
| **Class** | `GuardrailChecker` in `src/data/guardrails.py` | `StagedGuardrails` + `PositionLimits` in `src/guardrails/limits.py` |
| **Scope** | Called every OIE cycle + by screener, per proposed trade | Portfolio-health narrative; adapts limits to cash-buffer tier |
| **Severity** | Binary BLOCK / WARN | CRITICAL / BLOCK / WARN with `required_action` |
| **Config section** | `position_limits` | `guardrail_limits` |
| **Wired into engine loop?** | **Yes** (`oie_engine.py:647`, with `regime=` threaded so the CSP-deployment block tightens in VOLATILE/BEARISH) | No (display/advisory) |
| **Output** | `GuardrailReport(blocks, warnings, ...)` | `List[GuardrailViolation]` |

---

## 2. Layer 1 — `src/data/guardrails.py` (the live per-trade gate)

### BLOCKs (hard — stop new trades)
| Check | Rule |
|-------|------|
| Cash buffer | `< cash_buffer_critical` (10% NLV) |
| CSP deployed | `> max_csp_deployed_pct` (50% normal; **10% when the checker is constructed with `regime='VOLATILE'/'BEARISH'`** — the OIE engine threads its regime, wired 2026-08-16) |

### WARNs (soft — surface but don't block)
| Check | Rule |
|-------|------|
| Cash buffer | `< cash_buffer_warn` (15%) |
| Margin | `> max_margin_pct` (30%) |
| Open positions | `> max_open_positions` (10) |
| Daily new orders | `≥ max_daily_new_positions` (10) |
| Single position | `> max_single_position_pct` (25%) |
| Single sector | `> max_sector_pct` (25%) |
| **Worst-case assignment** | models ALL CSPs assigning simultaneously; shortfall > 0 |

### Worst-case assignment stress test (`:175`)
```
available = cash + buying_power × bp_margin_buffer (0.50) + cc_notional × cc_assignment_buffer (0.50)
shortfall = total_csp_liability − available
shortfall > 0  → WARN with remediation
```
The 0.50 haircuts are conservative: only half of margin BP and half of CC-assignment notional count as available.

### `check_new_trade()` (`:207`)
- If a ticker is already over `max_single_position_pct` → only **CC** is allowed (CSP/stock buys blocked).
- Then simulates adding the new position and re-runs `check()`.

> **CC exemption in the OIE loop** (`oie_engine.py:670–678`): CCs are share-secured, not cash-secured — so the engine treats cash-buffer and CSP-deployment blocks as **skippable for CCs**. Only ticker/sector concentration blocks apply to CCs.

`SECTOR_MAP` (`:255`) is a hardcoded dict mapping ~30 watchlist tickers to sectors (V→Financial, NVDA/AAPL/MSFT→Technology, TSLA/AMZN→Consumer, etc.).

---

## 3. Layer 2 — `src/guardrails/limits.py` (staged recovery)

`get_current_stage()` (`:151`): **EMERGENCY** if cash < 10%, **TARGET** if cash < 20%, else **COMFORT**. Limits adapt to the stage:

| Stage | Trigger | Position % | Sector % | CSP deploy | Positions | Monthly orders |
|-------|---------|:---:|:---:|:---:|:---:|:---:|
| **EMERGENCY** | cash < 10% | 15% | 40% | 15% (critical) | 10 | 15 |
| **TARGET** | cash < 20% | 25% (30% quality) | 35% | 25%/35% | 10 | 30 |
| **COMFORT** | cash ≥ 30% | 30% (quality) | 35% | 50% | 10 | 30 |

**CSP-deployment is cash-buffer-tiered** (`:71–74`, `:195–211`): `<10% cash → 15% CSP max`; `<15% → 25%`; `<20% → 35%`; `≥30% → 50%`.

`check_all_guardrails` (`:241`) returns `List[GuardrailViolation]` with severity `WARN`/`BLOCK`/`CRITICAL` and a `required_action`. Six checks: cash buffer, CSP deployment, position concentration, sector concentration, position count, monthly orders.

---

## 4. CSP pause triggers (stop new CSPs)

`Config.should_pause_csp(vix, regime_score, cash_reserve_pct, spy_price, spy_sma)` returns `(paused: bool, reasons: list)` — all five triggers implemented (2026-08-16). Paused if ANY:

| Trigger | Threshold | Config |
|---------|-----------|--------|
| VIX too high | `> 25` | `csp_pause.vix_above` |
| SPY below SMA | `< 200 SMA` | `csp_pause.spy_below_sma` |
| Regime too bearish | `≤ −2` (VOLATILE or worse) | `csp_pause.regime_min_score` |
| Cash reserve too thin | `< 20%` NLV | `csp_pause.cash_reserve_below_pct` |
| Stock dropped from basis | `> 15%` (per-ticker) | `csp_pause.stock_drop_from_basis_pct` |

**Wired into the OIE engine** (`_csp_pause_reasons`, `oie_engine.py:921`): the global triggers run once per screen (skipping the whole CSP branch when paused) and again per-trade at execution; the per-ticker basis-drop trigger is checked per candidate. Data-blind triggers (no macro/SPY data available) do not fire — the engine never blocks on unknowns.

When CSP is paused, the engine's `bypass_scarce_when_csp_paused` profit-take path activates (let CSP winners ride rather than close for redeployment — there's no CSP slot to redeploy into).

---

## 5. Holdings-exit framework (stock-leg losses)

**File**: `src/risk/holdings_exit.py` (pure functions). `evaluate_holding_exit()` (`:121`) orders severity **CIRCUIT_BREAKER > BACKSTOP_EXIT > DEAD_ZONE > OK**.

| Tier | Condition | Action |
|------|-----------|--------|
| **Dead zone** | drawdown > `dead_zone_drop_pct` (15%) below adjusted basis | basis-strike CC pays ~$0; hold unencumbered OR thesis-check → exit |
| **Backstop (conditional)** | drawdown ≥ `backstop_conditional_pct` (30%) AND price < 200 SMA AND `sma_200_slope < 0` | exit signal (stops only work in trending markets) |
| **Circuit breaker (hard)** | drawdown ≥ `backstop_hard_pct` (40%) | unconditional exit |
| **Time stop** | held ≥ `time_stop_months` (12) AND return lags `alt_yield × (months/12)` | REDEPLOY review |
| **Capacity flag** | `months_to_recover = price_gap / monthly_premium > 12` | REDEPLOY review |

Helpers: `drawdown_from_basis()` (`:30`), `is_dead_zone()` (`:78`), `months_to_recover()` (`:83`), `check_price_backstop()` (`:51`), `sma_slope()` (`:37`).

**Decision #10 policy** (`config/rules.yaml → holdings_exit`): ≤ dead zone → never CC below basis (unchanged). > dead zone → thesis gates decide (broken → EXIT signal; intact → surface hold-unencumbered vs flagged below-basis CC choice). **The engine never auto-sells stock** — it emits signals.

---

## 6. Thesis-break gates (codable deterioration)

**File**: `src/analysis/thesis.py`. Five gates, each returns `True`=FAIL / `False`=pass / `None`=NO_DATA (never counts as failure). All series oldest→newest.

| Gate | FAIL condition | Evidence |
|------|----------------|----------|
| **Growth stall** | last 2 quarters YoY revenue growth all negative | 93% of stalled companies never regain 2% growth |
| **Dual deceleration** | rev AND EPS growth both decelerating 3 consecutive quarters | confirmed sell signal |
| **Margin erosion** | gross margin −100bps over 2 yrs AND declining YoY 3 quarters | pricing-power loss |
| **Balance sheet** | Debt/EBITDA > 4.5× (or any debt vs non-positive EBITDA) | covenant stress |
| **Cash flow** | FCF yield < 2% AND net capital raising > 0 | strongest combined negative signal |

`evaluate_thesis()` (`:136`): `broken = (growth_stall FAIL) OR (failed_count ≥ broken_min_gates=2)`. `fetch_thesis_inputs()` is best-effort yfinance — missing data → NO_DATA (never fabricated).

**Thesis validator** (`thesis_validator.py:57`) adds 5 CRITICAL/WARNING gates (earnings trend, P/E fundamental, technical damage vs 200 SMA, HV regime, price perf vs 52w high). Status `BROKEN` if any CRITICAL, `DAMAGED` if any WARNING. Trusted tickers (`AMD, TSLA, PLTR`) skip only the high-P/E valuation check — negative-P/E still flags (solvency). P/E basis is **TTM** (`moomoo pe_ttm_ratio`, falling back to the static field; matches the yfinance `trailingPE` fallback) — validated 2026-08-17: ABBV static 105.7 vs TTM 70.5 changed the thesis verdict from BROKEN to DAMAGED.

---

## 7. Coverage (collar) check

**File**: `src/risk/collar_check.py:91`. Returns `ok=True` only if every open position passes. Conceptually required before any new trade.

| Position | Check |
|----------|-------|
| **CC** | shares owned ≥ contracts × 100 (per ticker, aggregated) |
| **CSP** | `available_cash − tied_up_csp ≥ strike × contracts × 100` |
| Unknown strategy | fail |

Total CSP need = `Σ strike × contracts × 100`. Helpers: `check_cc_coverage`, `check_cc_coverage_multi`, `check_csp_coverage`, `check_csp_coverage_multi`.

---

## 8. Margin model (designed; **not fully wired into the OIE loop**)

**File**: `src/risk/monitor.py` — `compute_margin_usage()` (`:103`) stub, plus the spec in `margin-guardrail.md` defining `compute_margin_headroom()`, `compute_csp_expiry_concentration()`, `validate_margin_for_new_csp()`.

```
max_margin_loan   = net_liq × max_margin_pct (0.30)
stock_collateral  = stock_mv × 0.50                    (Reg T 50% lendable)
implied_loan      = max(0, existing_csp_liability − cash)
margin_headroom   = max_margin_loan − implied_loan     (additional CSP assignable before cap)
```

> ⚠ **Status: PROPOSED, partially implemented.** The config has `max_margin_pct: 0.30` and the guardrail *should* BLOCK on it, but: (a) `GuardrailChecker.check()` issues margin as a **WARN, not BLOCK**; (b) the OIE engine calls `check_new_trade` but does **not** call `validate_margin_for_new_csp` before CSP execution; (c) live `margin_used_pct` is fetched from moomoo but not consistently threaded into the engine. **The 30% margin rule is therefore not enforced in the paper loop.** *Mitigation (2026-08-16): the engine passes `buying_power = cash + fund` only — margin BP never extends CSP coverage, so the paper book cannot implicitly borrow; the utilization WARN and 15-day clear window remain open.* See [production-deployment.md](production-deployment.md) §3 for the gap and the fix path. The spec (`margin-guardrail.md`) is the implementation plan.

---

## 9. Overlap detection

**File**: `src/risk/overlap.py:112 analyze_overlap()`. For every ticker with BOTH calls and puts:

| Pattern | Detection |
|---------|-----------|
| **Straddle** | same-strike, same-expiry call+put — reports premium & breakevens |
| **Strangle** | same-expiry calls+puts at different strikes (only when no straddle) |
| **Stacked-call risk** | ≥2 calls → cumulative shares-called-away by expiry |
| **Net share scenarios** | `net_if_calls` (shares − call_shares), `net_if_puts` (shares + put_shares), `net_if_all` |
| **Cash need** | `total_put_assign` = cash needed if all puts assign |

---

## 10. Position limits summary (config)

| Limit | Normal | Volatile | Type |
|-------|:---:|:---:|------|
| Single position | ≤ 25% NLV (target 15%) | — | BLOCK in Layer 2 EMERGENCY (15%) |
| Sector | ≤ 25% | — | WARN |
| CSP deployed | ≤ 50% NLV | ≤ 10% (enforced via `regime=`) | BLOCK |
| Cash buffer | ≥ 15% (warn) | — | BLOCK at < 10% |
| Open positions | ≤ 10 | — | WARN |
| New positions/day | ≤ 10/day config · ≤ 2 per engine cycle (`max_new_positions_per_cycle`) · ≤ 2 profit-closes per ticker/month · 14-day same-strike reopen cooldown | — | cycle/cooldown = engine BLOCK; rest WARN |
| **Margin** | **≤ 30%** (15-day clear) | — | ⚠ WARN (should be BLOCK — see §8). Mitigation: engine BP = cash + fund only, so margin never extends CSP coverage |

---

## 11. Validation

- `tests/test_risk.py` (26) — assignment handlers, margin headroom, concentration, roll discipline.
- `tests/test_holdings_exit.py` (37) — dead zone, conditional backstop, circuit breaker, time stop.
- `tests/test_overlap.py` (9) — straddle/strangle/stacked detection.
- `tests/test_portfolio_monthly_guardrail.py` (6) — monthly order BLOCK.
- `tests/test_thesis_validation.py` (55) — thesis gates + validator.

All pass. Coverage: `collar_check` 100%, `holdings_exit` 100%, `guardrails/limits` 85%, `data/guardrails` 33% (layer exercised through consumers).
