# Profit Target Optimization Spec — Behavioral Guardrails for the Decision Engine

**Version**: v1 (DRAFT — pending review)
**Date**: 2026-08-02
**Status**: PROPOSED — UNVALIDATED. No live-order changes until `pytest` passes and the decision-review backtest (§12) reproduces the predicted improvement.
**Applies to**: Cash Secured Puts (CSP), Covered Calls (CC), Wheel Strategy.
**Supersedes / extends**: [`profit-loss-management-spec.md`](profit-loss-management-spec.md) §4.2 (the trend-modulated target is *coded* there; this spec adds the *behavioral guardrails* that actually enforce it).
**Evidence base**: `scripts/decision_review.py` (60-day live-order analysis) + `db/oie_paper.db`.
**Related docs**: [`loss-management-playbook.md`](loss-management-playbook.md), [`margin-guardrail.md`](margin-guardrail.md), [`research_dte_selection.md`](research_dte_selection.md).

---

## 1. Executive Summary

**Problem.** The decision engine in `src/analysis/profit_management.py` already computes the *correct* trend-modulated profit target (CSP extends 50 → 70 → 85%; CC never extends and rolls up-and-out). But the **scoring layer** that consumes it — `src/scoring/holding_score.py:_score_option()` — collapses that structured decision into a `(float, str)` tuple and applies a **hardcoded `profit_captured >= 70` literal** at `holding_score.py:147`. Net effect: in production, profitable OTM positions get closed at ~50% regardless of trend, while the "extend to 85%" path the engine already computes is never honored. The gap is **behavioral, not architectural**.

**The 60-day data confirms the cost** (`scripts/decision_review.py`):
- **6 contracts HELPED** by active management (avoided ITM assignment): **+$11,871**
- **14 contracts HURT** by premature OTM closes: **−$3,572**
- **19 profitably-closed contracts**: booking at ~50% earned **+$4,777** vs. **+$2,631** hold-to-expiry — but **+$1,440** of that gap was 3 ITM saves (correctly closed early).
- The **15 OTM winners closed early**: holding to expiry would have added **~+$2,100**.

So the engine is **right when it intervenes on ITM risk** and **wrong when it intervenes on far-OTM winners**. The fix is to stop intervening on the latter.

**Core fix — four behavioral guardrails** added on top of the (correct) existing engine:

1. **OTM-only close gate** — do not auto-close a *profitable* position when it is far OTM (`|Δ| < 0.30`) with ample DTE (`> 21`). Let theta keep working.
2. **Honor the trend-extended target in the score** — remove the `>= 70` literal at `holding_score.py:147`; gate the "deep close" weight bump on `pd.target_pct` (which is already trend-aware), not a fixed 70%.
3. **Per-ticker frequency cap** — `max_closes_per_ticker_per_month: 2` and `same_strike_reopen_cooldown_days: 14` to kill V-style churn (14 V contracts traded in 60 days).
4. **Tighten monthly order BLOCK** — EMERGENCY ceiling `20 → 15` (already BLOCK, just tighter).

**Non-negotiables preserved**: 21-DTE gamma floor, net-credit-only rolling, never-sell-CC-below-cost-basis, capital-scarcity override, all loss-side premium-multiple stops. **No change** to `csp_itm` (0.50) or `csp_critical` (0.60) — those are what saved TSLA/AMD.

---

## 2. Corrections to the Original Analysis (read before implementing)

The hand-off analysis this spec is based on contained several statements that do **not** match the codebase. They are corrected here so the implementer doesn't chase phantoms.

| # | Original claim | Reality (verified) | Impact on spec |
|---|---|---|---|
| 1 | "`max_monthly_option_orders` (already in config, but WARN only)" | **Does not exist.** Closest keys are `guardrail_limits.max_monthly_orders_emergency: 20` and `max_monthly_orders_target: 10` (`config/rules.yaml:342-343`). Enforcement is **already BLOCK** in `src/guardrails/limits.py:311-322` and `:411-422` — not WARN. | §10 is **tighten 20→15**, not "upgrade WARN→BLOCK". |
| 2 | "`guardrails.py._filled_orders_this_month()` already exists" | Lives in **`scripts/portfolio.py:604-613`**, not `src/data/guardrails.py`. `src/data/guardrails.py` does **daily** order limits only (WARN). | §9/§10 reference `scripts/portfolio.py`. |
| 3 | "Two guardrail classes" (implied single) | **Two distinct `GuardrailChecker` classes**: `src/data/guardrails.py` (single-stage, daily WARN) vs `src/guardrails/limits.py` (`StagedGuardrails`, monthly BLOCK). Monthly work goes in the **latter**. | §10 names the correct file. |
| 4 | "Override logic at line 148 checks `profit_captured >= 70` ... bypassing the trend extension" | Confirmed — but the deeper issue is `holding_score.py:147` (the `score -= 2.0 if profit_captured >= 70 else 1.5` weight) **and** line 279 collapsing the structured `ProfitDecision` to `(float, str)`. No action enum survives `_score_option`. | §5 specifies both fixes. |
| 5 | Implies `decide_profit_target` uses `delta` for the close gate | The `delta` arg to `decide_profit_target` (`profit_management.py:69`) is **dead weight** — accepted, never read. All delta logic lives in `holding_score._score_option`. | §6 adds the OTM gate **in `_score_option`**, not in `decide_profit_target`. |
| 6 | "Loss-side trend overlay at 2× premium" wiring | `profit_management.loss_alert_should_hard_stop()` exists but is **not called** by `_score_option`'s loss ladder (`holding_score.py:210-241`). | Out of scope here; flagged in §14 as a separate fix. |
| 7 | "Add `close_if_delta_above: 0.30` and `close_if_dte_below: 21`" | Both keys are **net-new** (no placeholder anywhere). Also note the direction: we close only when delta is *high enough* OR DTE *low enough* — so the natural key names are `close_if_delta_above` / `close_if_dte_below`. | §8 gives exact YAML. |

---

## 3. Evidence Synthesis

Source: `scripts/decision_review.py` over a 60-day lookback (`--days 60`). The script pulls **live** order history from moomoo via `fetch_portfolio_and_orders` and underlying prices from yfinance — there is **no persisted CSV**; rerun to reproduce.

### 3.1 Verdict model (decision_review.py:237-243)
```python
diff = actual - pnl_if_held        # actual = premium_recv - buyback_paid
if diff > 1:   verdict = 'DECISION_HELPED'
elif diff < -1: verdict = 'ACTIVE_HURT'
else:          verdict = 'NEUTRAL'
```
`pnl_if_held` models hold-to-expiry: OTM → keep full premium; ITM → assignment P&L (CSP) / called-away P&L (CC). Two non-definitive labels: `UNDECIDED` (expiry still future), `UNDETERMINED` (no expiry price). Per-**ticker** verdict uses a wider **±$50** band (`decision_review.py:425`).

### 3.2 What the 60-day run showed
| Bucket | Contracts | $ impact | Interpretation |
|---|---|---|---|
| HELPED by active mgmt | 6 | **+$11,871** | ITM-assignment saves (TSLA, AMD, GOOG $330). The `csp_itm`/`csp_critical` delta gates + premium stops **earned** this. **Keep them.** |
| HURT by premature close | 14 | **−$3,572** | OTM winners closed at ~50% that would have expired worthless. Avg `|Δ|` at close ≈ **0.22** — well below the 0.40 "decision" gate. **This is the target of §6.** |
| Profitably closed (decided) | 19 | +$4,777 actual vs +$2,631 hold | +$1,440 of the gap is the 3 ITM saves (correct). The other 15 OTM winners left ~+$2,100 on the table. |
| Per-ticker churn | V: 14 contracts in 60d | n/a | One ticker dominated the order book. **Target of §9.** |

### 3.3 The "when is extending worth it" framework
Extending past 50% is **conditionally** positive EV. The conditions, derived from the evidence:
- **OTM status**: position is OTM (theta still decaying the option toward 0). Closing a far-OTM winner at 50% throws away the remaining 50%.
- **Trend direction supports the short**: CSP in uptrend (stock running away from strike); the trend-extension ladder is *already coded* (`_csp_target`).
- **Delta is low**: `|Δ| < 0.30` means the option is far from the strike; assignment risk is negligible, so there is no defensive reason to close.
- **DTE is ample**: `DTE > 21` means gamma has not yet ramped; no gamma-floor reason to close.

**Conversely**, intervene early when **any** of: ITM (`|Δ| ≥ 0.50`), near the gamma floor (`DTE ≤ 21`), or trend broken/bearish on a CSP (the loss-side and the base-50% path apply).

---

## 4. Current-State Audit (exact files, lines, gaps)

| Component | File:line | What it does today | Gap this spec closes |
|---|---|---|---|
| Decision core | `src/analysis/profit_management.py:65-150` `decide_profit_target()` | CSP extends 50→70→85; CC never extends, rolls up-and-out; 21-DTE floor (GATE 1, `:102`); capital scarcity (GATE 2, `:114`). **Correct.** | **None — keep as-is.** (The `delta` param is dead weight; optional cleanup in §11.) |
| Trend ladder | `src/analysis/profit_management.py:155-183` `_csp_target()` | Reads `strong_trend_composite_min:70`, `trend_composite_min:50`, `iv_rank_min:30`, sentiment allow-list. **Correct.** | None. |
| CSP resolution | `src/analysis/profit_management.py:186-211` `_resolve_csp()` | Emits `ACTION_CLOSE` / `ACTION_ROLL_DOWN_OUT` / `ACTION_HOLD` with `extended_by_trend` flag. | None. |
| **Scoring layer** | `src/scoring/holding_score.py:126-279` `_score_option()` | Calls `decide_profit_target` (`:143`); then **discards** the structured action and uses a hardcoded `profit_captured >= 70` (`:147`); returns `(float, str)` (`:279`). | **§5 + §6.** This is the entire behavioral gap. |
| Delta gates (loss/ITM) | `src/scoring/holding_score.py:189-208` | `csp_critical 0.60`, `csp_itm 0.50`, `csp_decision 0.40`, `cc_critical 0.50`, `cc_warn 0.40`. | **§7** revises the *decision/warn* rungs; keeps 0.50/0.60. |
| 21-DTE layer (dup) | `src/scoring/holding_score.py:166-187` | Re-applies a dte ≤ 3/7/14/21 ladder on top of GATE 1. | Acceptable; documented. |
| Loss-side stops | `src/scoring/holding_score.py:210-241` | Premium-multiple ladder; does **not** call `loss_alert_should_hard_stop`. | Out of scope (§14). |
| Monthly orders (BLOCK) | `src/guardrails/limits.py:311-322, 411-422`; `scripts/portfolio.py:604-613` | Already BLOCK at `>20` (EMERGENCY) / `>10` (TARGET). | **§10** tightens EMERGENCY 20→15. |
| Per-ticker frequency | — | **Does not exist.** | **§9** adds it. |
| Config | `config/rules.yaml:198-225` `profit_take`; `:141-162` `stop_loss` | All trend keys present; no `close_if_*`, no per-ticker block. | **§8** adds the new keys. |
| Evidence tool | `scripts/decision_review.py` (731 lines, untracked) | Reproduces the §3 numbers; `--profit-targets` sub-mode compares actual vs @80% vs @100%. | **§12** validation harness. |

---

## 5. Behavioral Gap Analysis — the disconnect at `holding_score.py:147`

### 5.1 The two-line root cause
```python
# src/scoring/holding_score.py   (current)
141:  from src.analysis.profit_management import decide_profit_target, ProfitDecision
142:  pd = decide_profit_target(strategy, profit_captured, dte, delta, trend_ctx, capital_scarcity)
143:  if pd.action == ProfitDecision.ACTION_CLOSE:
144:      # Deeper profit past target = more "decided" ...
147:      score -= 2.0 if profit_captured >= 70 else 1.5          # ← hardcoded 70
148:      decision = f'✅ CLOSE ({profit_captured:.0f}% ≥ {pd.target_pct:.0f}% target)'
...
279:  return max(1.0, min(10.0, score)), decision                  # ← action enum discarded
```
- Line 147 ignores `pd.target_pct`. When the engine extended the CSP target to **85%**, the "deep close" weight still fires at the fixed **70%** mark — so a 72% CSP winner in a strong uptrend is scored as if it had blown past its target, even though the engine said "hold to 85%".
- Line 279 returns only `(score, decision_string)`. Downstream layers (the daily review, the digest, any future auto-executor) see an emoji string, **not** an `ACTION_*` constant. The roll-for-credit semantics baked into `decide_profit_target`'s reasons ("if no credit roll, close") become advisory text with no machine-readable handle.

### 5.2 Why the production trace shows ~50% closes
Because the engine's `ACTION_HOLD` (profit below the *extended* target) is silently overwritten later in `_score_option` by the **delta-gate layer** (`holding_score.py:189-208`) for any position whose `|Δ|` drifts up — and by the dte-layer for anything ≤ 21 DTE. For a far-OTM, far-DTE CSP, neither fires, so the engine's `HOLD` *should* survive — but the human-readable review still shows the close because the **advisory** layer (separate from scoring) books at base 50% by default. The OTM gate (§6) makes the engine's intent binding.

### 5.3 Fix (exact patch, see §11)
1. Replace the `>= 70` literal with `pd.target_pct`-aware weighting.
2. Thread the structured `ProfitDecision` (or at minimum the `ACTION_*` enum) out of `_score_option` so downstream code can switch on it. (Minimal: return a 3-tuple `(score, decision, pd)`; the third element is optional for callers and defaults to `None`.)

---

## 6. Spec — OTM-Only Close Gate

### 6.1 Decision tree (added in `_score_option`, **after** the `decide_profit_target` call at `:143`, **before** emitting any CLOSE)

```
GIVEN: pd = decide_profit_target(...)              # already trend-aware
GIVEN: profit_captured, dte, delta (abs), strategy

IF pd.action == ACTION_CLOSE  AND  profit_captured >= base_target(50%):
    otm_far  = delta < cfg.profit_take('close_if_delta_above', 0.30)
    dte_room = dte  > cfg.profit_take('close_if_dte_below',  21)
    IF otm_far AND dte_room:
        → OVERRIDE to HOLD                            # let theta work
        reason = "OTM (|Δ|<{d}) with {n} DTE — hold past 50%, theta still decaying"
        extended_by_trend flag preserved for display
    ELSE:
        → emit CLOSE / ROLL as pd.action dictated
```

### 6.2 Semantics
- The gate **only** overrides a CLOSE on a *profitable* position. It never overrides `ACTION_MANAGE_DTE` (the 21-DTE floor), `ACTION_ROLL_DOWN_OUT`, `ACTION_ROLL_UP_OUT`, or any loss-side stop. Those keep firing.
- The gate is **independent of trend**: a far-OTM CSP with a broken trend still gets held past 50% *if theta is working* — because closing it at 50% throws away the remaining 50% of premium regardless of trend. Trend only decides whether the *target* is 50/70/85; the gate decides whether to close *at all* before 21 DTE.
- Edge: if `capital_scarcity == SCARCE`, GATE 2 in `decide_profit_target` already forced base 50% — the OTM gate still applies (we'd rather hold a far-OTM winner than close it for redeployment capital), **unless** the redeployment yields a higher-EV trade. This is a human-judgment call; default = honor the gate, surface a `REDEPLOY_CANDIDATE` note.

### 6.3 Config keys (net-new)
```yaml
profit_take:
  # OTM-only close gate (spec §6) — only auto-close a PROFITABLE position
  # when it is near money OR near expiry; otherwise let theta work.
  close_if_delta_above: 0.30     # |Δ| >= this OR ...
  close_if_dte_below:   21       # DTE <= this ... → close eligible; else HOLD
```

### 6.4 Interaction with the 14 "HURT" contracts
The 14 hurt contracts closed at avg `|Δ| ≈ 0.22` with `DTE > 21`. Under §6.1, every one of them hits `otm_far AND dte_room` → OVERRIDE to HOLD. Predicted recovery of the bulk of the **−$3,572** (less the small fraction that did reverse).

---

## 7. Spec — Revised Delta Thresholds

### 7.1 Evidence
The 14 HURT contracts closed at avg `|Δ| ≈ 0.22` — **below** the current `csp_decision: 0.40` gate. The 0.40 rung is firing on positions that were opened at 0.20–0.30 delta and never approached the danger zone. The rungs that actually mattered (`csp_itm: 0.50`, `csp_critical: 0.60`) are **unchanged**.

### 7.2 New delta ladder
| Tier | Key | CSP | CC | Meaning |
|---|---|---|---|---|
| Monitor | (display only) | `|Δ| ≥ 0.30` | `|Δ| ≥ 0.30` | Show in review; no action. Aligns with §6 `close_if_delta_above`. |
| **Decision** | `csp_decision` / `cc_warn` | **0.40 → 0.50** | **0.40 → 0.50** | "Plan roll/assign/exit." Raised so a still-OTM 0.40 position is no longer flagged. |
| ITM | `csp_itm` / `cc_critical` | **0.50 (unchanged)** | **0.50 (unchanged)** | Assignment probable. This is the rung that saved TSLA/AMD. |
| Critical | `csp_critical` | **0.60 (unchanged)** | — | Cut / roll / assign. |

> **DTE interaction (new, encoded in `_score_option` not in YAML):** a given delta is more serious with less DTE. Implement as: `effective_decision_delta = 0.50 if dte > 21 else 0.40`. I.e. inside 21 DTE, drop back to 0.40 because gamma is ramping and there's less time to recover. The gamma floor (≤ 21 DTE → `ACTION_MANAGE_DTE`) already fires first, so this only affects the 22–30 DTE band where the premium-multiple `mid_close` also lives.

### 7.3 Exact YAML diff (`config/rules.yaml:151-155`)
```yaml
  delta:
    csp_critical: 0.60   # CSP |Δ| > 0.60 → too directional, cut it        (unchanged)
    csp_itm: 0.50        # CSP |Δ| ≥ 0.50 → deep ITM, assignment imminent  (unchanged)
    csp_decision: 0.50   # CSP |Δ| ≥ 0.50 → "decision time"  (was 0.40)    §7.2
    cc_critical: 0.50    # CC Δ > 0.50 → prepare for assignment            (unchanged)
    cc_warn: 0.50        # CC Δ > 0.50 → monitor closely     (was 0.40)    §7.2
```

---

## 8. Spec — Trend-Modulated Profit Target (full table; engine already implements)

This section is the **canonical reference** for what `decide_profit_target` already returns. It is included so the §5/§6 fixes are validated against the intended matrix. **No code change here** — only the §6 gate decides whether a given row's action is allowed to fire.

### 8.1 CSP (short put) — trend extension ALLOWED
| `TREND_COMPOSITE` | Sentiment | IV_RANK | DTE | `|Δ|` | Target | Engine action | §6 gate |
|---|---|---|---|---|---|---|---|
| ≥ 70 | BULLISH/NEUTRAL | ≥ 30 | > 21 | any | **85%** | `HOLD` until 85% (then `ROLL_DOWN_OUT` for credit, else `CLOSE`) | HOLD enforced |
| ≥ 50 | BULLISH/NEUTRAL | — | > 21 | any | **70%** | `HOLD` until 70% | HOLD enforced |
| < 50 | any | — | > 21 | < 0.30 | 50% | `CLOSE` at 50% | **OVERRIDE→HOLD** (§6) |
| < 50 | any | — | > 21 | ≥ 0.30 | 50% | `CLOSE` at 50%, redeploy | CLOSE allowed |
| any | CAUTIOUS/BEARISH | — | > 21 | < 0.30 | 50% | `CLOSE` | **OVERRIDE→HOLD** (§6) — theta still works |
| any | any | — | > 21 | ≥ 0.30 | 50% | `CLOSE` | CLOSE allowed |
| any | any | any | ≤ 21 | any | — | `MANAGE_DTE` (close/roll/assign) | Gate does not apply |

### 8.2 CC (short call) — trend extension NOT ALLOWED
| `TREND_COMPOSITE` | Condition | Target | Engine action | §6 gate |
|---|---|---|---|---|
| ≥ 50 (uptrend) | Stock running INTO strike | 50% | `CLOSE` **+ `ROLL_UP_OUT`** for credit (keep shares); if no credit roll, close | CLOSE allowed (CC delta rising = real risk) |
| < 50 | Weak/negative trend | 50% | `CLOSE`, redeploy | Gate uses same `|Δ|`/DTE rule; a far-OTM CC with room is held |
| any | any | any | ≤ 21 DTE | `MANAGE_DTE` |

### 8.3 Rolling-winner lane (already in engine)
When a CSP trend target is hit, `_resolve_csp` (`profit_management.py:189-202`) prefers `ACTION_ROLL_DOWN_OUT` (credit only) over flat `ACTION_CLOSE` — bank the win, stay in the thesis. CC mirrors with `ACTION_ROLL_UP_OUT`. **Net-credit-only** is non-negotiable; reasons already say "if no credit roll, close." §5 fix threads this enum out so an executor can honor it.

---

## 9. Spec — Per-Ticker Frequency Cap

### 9.1 Problem
V accounted for 14 of the 60-day contracts — single-ticker churn that consumes order budget and re-opens the same risk. No per-ticker frequency control exists anywhere in the repo.

### 9.2 Config (net-new)
```yaml
guardrail_limits:
  # Per-ticker frequency cap (spec §9) — kill single-ticker churn
  max_closes_per_ticker_per_month: 2
  same_strike_reopen_cooldown_days: 14
```

### 9.3 Enforcement point
In `_score_option` (or, cleaner, a new helper `_ticker_frequency_ok(pos, today)` called from `_score_option`), check the order history for the position's ticker within the current calendar month (reuse the bucketing pattern from `scripts/portfolio.py:_filled_orders_this_month`). If `closes_this_month(ticker) >= max_closes_per_ticker_per_month`:
- Do **not** suppress a *defensive* close (ITM delta gate, premium stop, `MANAGE_DTE`).
- **Suppress** a *profit-taking* close on a far-OTM winner (it would hit the §6 gate anyway) and instead emit `HOLD (frequency-capped: {n}/{max} {ticker} closes this month)`.

`same_strike_reopen_cooldown_days` is enforced at **entry** (entry signal layer / `check_new_trade`), not at scoring — block re-opening the same ticker+strike+type within the window.

### 9.4 Where the data lives
Order history is fetched live via `fetch_portfolio_and_orders` (`src/data/portfolio_loader.py`). The cap is computed in-memory per review run; no persistence required. (If a persisted counter is later wanted, `db/oie_paper.db`'s `paper_trades` table is the model.)

---

## 10. Spec — Monthly Order Enforcement (tighten, do not add)

### 10.1 Current state (verified)
- Keys: `guardrail_limits.max_monthly_orders_emergency: 20`, `max_monthly_orders_target: 10` (`config/rules.yaml:342-343`).
- Enforcement: `src/guardrails/limits.py:311-322` (`check_all_guardrails`, `>`, BLOCK) and `:411-422` (`check_new_trade`, `>=`, BLOCK). **Already BLOCK** — the original "WARN only" claim is wrong (§2 #1).
- Counter: `scripts/portfolio.py:_filled_orders_this_month` buckets by `YYYY-MM` of fill date, status in `{FILLED_ALL, FILLED_PART}`. Tested by `tests/test_portfolio_monthly_guardrail.py`.
- Advisory (separate): `scripts/portfolio.py:865-869` emits a HIGH rec when `monthly > 10`.

### 10.2 Change
Tighten the EMERGENCY ceiling from 20 → 15. Two coordinated edits so `check_all_guardrails` and `check_new_trade` stay consistent:
```yaml
guardrail_limits:
  max_monthly_orders_emergency: 15   # was 20 (spec §10)
  max_monthly_orders_target: 10      # unchanged
```
```python
# src/guardrails/limits.py:42, :57 (constants must track config — see §11.4)
MAX_MONTHLY_ORDERS_EMERGENCY = 15    # was 20
MAX_MONTHLY_ORDERS_TARGET    = 10    # unchanged
```

### 10.3 Off-by-one note (pre-existing, document not fix here)
`check_all_guardrails` uses `>` (`:313`); `check_new_trade` uses `>=` (`:412`). So a count exactly *equal* to the limit doesn't trip the post-hoc check but *does* block the next new trade. This is intentional (the pre-trade check is stricter) but should be commented. Out of scope to change.

---

## 11. Integration Points (exact edits)

> All line numbers from the explored tree (2026-08-02). Re-confirm with `grep` before editing.

### 11.1 `src/scoring/holding_score.py` — `_score_option`
- **After line 143** (`pd = decide_profit_target(...)`): insert the §6 OTM gate. On override, set `decision` to a HOLD string carrying `pd.extended_by_trend`, and **skip** the close-weight line.
- **Line 147**: replace
  ```python
  score -= 2.0 if profit_captured >= 70 else 1.5
  ```
  with a `pd.target_pct`-aware weight:
  ```python
  depth = profit_captured - pd.target_pct
  score -= 2.0 if depth >= 20 else (1.5 if depth >= 0 else 1.0)
  ```
  (Deep past target → -2.0; at/just past → -1.5; closed at exactly target by a non-trend path → -1.0.)
- **Return signature** (`:279`): change to `(score, decision, pd)` and update callers to accept an optional third element (default `None`) for backward compatibility. Thread `pd.action` into any downstream executor.

### 11.2 `src/scoring/holding_score.py` — delta-gate layer (`:189-208`)
- Read the new `csp_decision: 0.50` / `cc_warn: 0.50` from config (already wired via `cfg.stop_delta_csp_decision` / `cfg.stop_delta_cc_warn`).
- Add the DTE-interaction: `eff_decision = 0.40 if dte <= 21 else cfg.stop_delta_csp_decision` (and CC equivalent). Source the `0.40` as a new `cfg.stop_delta_csp_decision_gamma` or hardcode with a comment referencing §7.2.

### 11.3 `config/rules.yaml`
- `:151-155` delta block → §7.3 diff.
- `:204-225` `profit_take` block → add §6.3 keys.
- `:342-343` `guardrail_limits` → §10.2 diff, plus §9.2 keys.

### 11.4 `src/guardrails/limits.py`
- `:42`, `:57` `MAX_MONTHLY_ORDERS_*` constants → §10.2. **Confirm whether these constants are read from config or hardcoded** — the classmethods in `src/data/guardrails.py:64-99` suggest config-backed, but `limits.py` uses class constants. If hardcoded, update both the constant and (for consistency) point them at config in a follow-up.

### 11.5 `src/scoring/holding_score.py` — per-ticker frequency
- Add `_ticker_frequency_ok(pos, today, orders)` helper; call it inside `_score_option` before emitting a profit-taking CLOSE. Defensive closes (delta gate, premium stop, `MANAGE_DTE`) bypass it.

### 11.6 `src/analysis/profit_management.py` (optional cleanup, not required)
- The `delta` parameter to `decide_profit_target` (`:69`) is unused. Either (a) remove it and update the call site at `holding_score.py:143`, or (b) keep it and add a `# noqa: unused` with a comment that §6 was considered for placement here but lives in the scoring layer for closer access to the order book. **Recommendation: keep the param** — future loss-side delta logic may want it inside the engine.

---

## 12. Validation / Test Plan

### 12.1 Unit tests (new)
- `tests/test_profit_management_otm_gate.py` — table-driven over the §8.1/§8.2 matrices: for each row, assert the final `(action, score-band)` after the §6 override.
  - Far-OTM CSP winner, trend extended to 85%, DTE 40, `|Δ| 0.18` → `HOLD` (not CLOSE).
  - Same but `|Δ| 0.45` → `CLOSE`.
  - Same but DTE 18 → `MANAGE_DTE` (gate does not override floor).
  - CC in uptrend running into strike → `ROLL_UP_OUT` (gate does not override).
  - Capital SCARCE + far-OTM → HOLD with `REDEPLOY_CANDIDATE` note.
- `tests/test_delta_ladder_dte_interaction.py` — `|Δ|=0.45`, DTE 30 → not flagged; DTE 20 → flagged (eff_decision 0.40).
- `tests/test_ticker_frequency_cap.py` — 3rd V close in a month → suppressed for profit-take, allowed for `csp_itm` defensive close.
- Extend `tests/test_portfolio_monthly_guardrail.py` to assert BLOCK fires at 16 (was 21) under EMERGENCY.

### 12.2 Backtest harness
Re-run `scripts/decision_review.py --days 60 --profit-targets` before and after the change. Acceptance criteria:
- The 14 HURT contracts → most reclassify to `NEUTRAL` or `DECISION_HELPED` (held to expiry / to trend target).
- Total `ACTIVE_HURT` dollar sum moves from −$3,572 toward ≥ −$1,000.
- The 6 HELPED (ITM saves) **unchanged** — the defensive gates did not regress.
- Per-ticker V count ≤ 4 in the window.
- Monthly order total ≤ 15.

### 12.3 Regression
- Full `pytest`. Pay attention to anything asserting the old `>= 70` weighting or the 0.40 decision delta.
- Smoke-run the daily review pipeline end-to-end on the current live portfolio; diff the decision column vs. the pre-change run.

---

## 13. Implementation Phases

| Phase | Scope | Risk | Reversibility |
|---|---|---|---|
| **P1 — Config only** | §7.3 delta diff, §8/§10 YAML keys, §9.2 new keys. No Python. | Zero (config is read defensively with defaults). | Revert one file. |
| **P2 — Scoring layer** | §11.1 (OTM gate + `target_pct`-aware weight), §11.2 (delta DTE-interaction). | Medium — touches the hot path. | Feature-flag behind `profit_take.otm_close_gate_enabled: true` (default on after backtest). |
| **P3 — Frequency cap** | §11.5. Needs order-history plumbing into `_score_option`. | Medium — new data dependency. | Flag behind `guardrail_limits.per_ticker_frequency_enabled: true`. |
| **P4 — Monthly tighten** | §11.4. Two-line change. | Low. | Revert. |
| **P5 — Backtest sign-off** | §12.2 acceptance gates. | — | — |
| **P6 (out of scope)** | Thread structured `ProfitDecision` to a real executor; wire `loss_alert_should_hard_stop`. | High — execution-layer change. Separate spec. |

---

## 14. Risks, Non-Goals, Open Questions

### 14.1 Risks
- **Holding winners that reverse.** The §6 gate holds far-OTM winners past 50%; if the underlying gaps through the strike, the saved premium is lost and then some. Mitigation: the delta gates (`csp_itm 0.50`, `csp_critical 0.60`) and the 21-DTE floor still fire; the gate only buys time *while* the position is safe by definition (`|Δ| < 0.30`, `DTE > 21`).
- **Stale trend data.** `TrendContext` is only as good as the last snapshot. If trend is `None`, the engine defaults to base 50% (sentiment allow-list treats unknown as non-blocking) — the §6 gate still correctly holds a far-OTM winner, so this risk is bounded.
- **Frequency cap masking a real signal.** If a ticker genuinely needs a defensive close, the cap must not suppress it — §9.3 exempts defensive closes.

### 14.2 Non-goals (explicitly out of scope)
- Changing `decide_profit_target`'s arithmetic (it's correct).
- Adding new data sources — all inputs (`TREND_COMPOSITE`, `SENTIMENT`, `IV_RANK`, delta, DTE, profit_captured) are already computed.
- Loss-side rule changes (premium stops, ITM/critical delta) — mature, keep.
- Auto-execution — this spec makes the engine's intent **binding in the score**, not auto-trading.

### 14.3 Open questions for review
1. **`_score_option` return shape.** Minimal (3-tuple, third optional) vs. return a small dataclass `OptionScore(score, decision, action, target_pct, extended)`? Recommend the dataclass if any downstream executor is planned within the next quarter; otherwise 3-tuple.
2. **Capital-scarcity + OTM gate.** Default = honor the gate (hold the winner). Alternative = honor scarcity (close to redeploy). Which side does the trader want when both fire? (§6.2 current default: honor the gate, surface a note.)
3. **`same_strike_reopen_cooldown` enforcement layer.** Entry-side (`check_new_trade`) is cleaner but requires the entry pipeline to ingest the cooldown; scoring-side is easier but only warns. Confirm entry-side.
4. **Should §7.2's `0.40`-inside-21-DTE fallback be config (`stop_delta_csp_decision_gamma`) or hardcoded?** Config is more tunable; hardcoded is simpler. Recommend config with default 0.40.
5. **`loss_alert_should_hard_stop`** is currently unused (§2 #6). Worth a follow-up spec to wire it into the loss ladder — but **not** this one.

---

## 15. Change Log

| Date | Version | Author | Notes |
|---|---|---|---|
| 2026-08-02 | v1 DRAFT | ZCode | Initial spec from 60-day decision review; corrected 7 factual errors in the source analysis (§2); pending backtest sign-off (§12.2). |
