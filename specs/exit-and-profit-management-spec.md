# Exit & Profit Management Spec — The Decision Core

**Status**: Implemented & audited (2026-08-09)
**Files**: `src/analysis/exit_management.py`, `src/analysis/profit_management.py`, `src/scoring/holding_score.py`
**Config**: `config/rules.yaml → profit_take`, `stop_loss`, `rolling`, `cc_management`, `holdings_exit`
**Evidence base**: [loss-management-playbook.md](loss-management-playbook.md) (64 cited sources), [research_dte_selection.md](research_dte_selection.md)

> This is the heart of the system. **One pure function** — `decide_exit_action()` — produces every exit/roll/hold decision the OIE engine and portfolio display show. It composes the profit side (`decide_profit_target`) with the loss side (delta gates + premium stops + absolute catch-all). All thresholds from config; no AI.

---

## 1. The strategy-direction asymmetry (the one insight)

A short option's response to an uptrend depends on **which option you sold**:

| Strategy | What an uptrend does | Effect | Engine response |
|----------|---------------------|--------|-----------------|
| **CSP** (short put) | Stock runs *away* from strike → decay accelerates, Δ → 0 | **Helps** | **Extend** the 50% target (hold for 70–85%) |
| **CC** (short call) | Stock runs *into* strike → Δ+γ climb, upside capped | **Hurts** | **Never extend**; instead **roll up-and-out** to keep shares |

This is why the framework computes a rich signal stack (trend, sentiment, IV) for *entry* and now wires the **same stack into exit**. The exit layer was historically the last place the signal stack was unused; this spec closed that gap — conditionally, with hard gates that override greed.

---

## 2. The decision core — `decide_exit_action()`

**File**: `src/analysis/exit_management.py:88`. Returns an `ExitDecision` (dataclass `:68`) carrying **exactly one** of `close_reason` / `roll_decision`, plus an optional `warn`.

**Priority order** (first match wins):

```
1. EXPIRY         dte ≤ 0                                      → no-action (caller resolves ITM/OTM → ASSIGN/EXPIRE)
2. PROFIT SIDE    decide_profit_target(...) acts?              → CLOSE_50PCT / CLOSE_TREND / ROLL_UP_OUT / ROLL_DOWN_OUT
3. LOSS SIDE:
   3a DELTA       |Δ| ≥ critical                                → STOP_DELTA (CSP 0.60) / ROLL_UP_OUT (CC 0.60, DTE-aware)
   3b PREMIUM     CSP only, loss_multiple ≥ DTE-tier close      → STOP_LOSS
   3c ABSOLUTE    pnl_dollars < −heavy_loss_for_premium(premium_collected)  → STOP_LOSS (both CC & CSP)
                  (premium-tiered floor: −$1k / −$2k / −$5k / −$8k by total credit banked)
```

**Action vocabulary** (`:53–65`): `CLOSE_50PCT`, `CLOSE_TREND`, `STOP_DELTA`, `STOP_LOSS`, `EXPIRE`, `CC_ASSIGN`, `CSP_ASSIGN`, `ROLL_UP_OUT`, `ROLL_DOWN_OUT`, `WARN_CC_DELTA`.

> **The "dead-end bug" fix**: `MANAGE_DTE` (the 21-DTE gamma floor, returned by the profit side) deliberately *falls through* to the loss tiers rather than short-circuiting. A winner inside 21 DTE used to rot because the profit side returned MANAGE_DTE and nothing acted on it. Now it falls through so the delta/premium tiers can still cut a winner that has flipped ITM.

---

## 3. Profit side — `decide_profit_target()`

**File**: `src/analysis/profit_management.py:65`. Returns a `ProfitDecision` with `action`, `target_pct`, `extended_by_trend`, `reason`.

### 3.1 Gates (hard — override every extension)

| Gate | Condition | Result |
|------|-----------|--------|
| **GATE 1 — DTE floor** | `dte ≤ dte_floor` (21) | `MANAGE_DTE` ("manage today: close/roll/assign") — overrides everything |
| **GATE 2 — Capital scarcity** | `scarcity == SCARCE` | base 50% CLOSE (capital opportunity cost beats trend greed). **Deployment-aware bypass**: if `bypass_scarce_when_csp_paused` AND CSP is paused, skip this gate — freed capital has no CSP slot → the capital-velocity argument for booking at 50% collapses → let winners ride. |

### 3.2 CSP — trend extension allowed

After gates pass, `_csp_target()` (`:169`) computes the target:

| Condition | Target | Engine action |
|-----------|:---:|----------------|
| `trend_composite ≥ strong_min (70)` AND sentiment ∈ {BULLISH, NEUTRAL} AND `IVR ≥ iv_rank_min (30)` | **85%** | HOLD to 85% → `ROLL_DOWN_OUT` for credit (bank win, stay in trend) |
| `trend_composite ≥ trend_min (50)` AND sentiment ∈ {BULLISH, NEUTRAL} | **70%** | HOLD to 70% |
| otherwise | **50%** (base) | CLOSE at 50%, redeploy |

When an extended target is *hit*, the engine prefers `ROLL_DOWN_OUT` (net credit) over flat CLOSE — bank the win *and* stay in the thesis.

### 3.3 CC — trend extension NOT allowed

Base 50% always (uptrend is the danger side). When `profit_captured ≥ 50%` AND `roll_up_out_on_trend` AND `trend_composite ≥ 50` → `ROLL_UP_OUT` (keep shares, recapture upside); else CLOSE at base.

### 3.4 Config (`config/rules.yaml → profit_take`)

```yaml
profit_take:
  csp: { base_pct: 50, strong_trend_target_pct: 85, trend_target_pct: 70, trend_extension_enabled: true }
  cc:  { base_pct: 50, trend_extension_enabled: false, roll_up_out_on_trend: true }
  trend_inputs: { strong_trend_composite_min: 70, trend_composite_min: 50,
                  sentiment_direction_allow: [BULLISH, NEUTRAL], iv_rank_min: 30 }
  dte_floor: 21
  capital_scarcity_override: SCARCE
  bypass_scarce_when_csp_paused: true     # PROPOSED — paper-trading the new logic
  loss_alert_trend_overlay: { trend_composite_hard_stop_min: 40, max_extra_roll_attempts: 1 }
  close_if_delta_above: 0.30              # OTM-only close gate
  close_if_dte_below: 21
```

---

## 4. The OTM-only close gate

**File**: `src/scoring/holding_score.py:189–213`. The behavioral guardrail that prevents the most common costly mistake: closing a far-OTM winner at 50% when theta would have done the rest.

```
GIVEN: pd = decide_profit_target(...)              # already trend-aware
IF pd.action == ACTION_CLOSE  AND  profit_captured ≥ base (50%):
    otm_far  = |Δ| < close_if_delta_above (0.30)
    dte_room = dte  > close_if_dte_below   (21)
    IF otm_far AND dte_room:
        → OVERRIDE to HOLD                          # let theta work
    ELSE:
        → emit CLOSE / ROLL as pd dictated
```

**Semantics**:
- Only overrides a CLOSE on a **profitable** position. Never overrides `MANAGE_DTE`, `ROLL_*`, or any loss-side stop.
- **Independent of trend** — a far-OTM CSP with a broken trend still gets held past 50% *because closing at 50% throws away the remaining 50% of premium regardless of trend*.
- Edge: capital SCARCE + far-OTM → HOLD by default (we'd rather keep the winner than close it for redeployment), but surface a `REDEPLOY_CANDIDATE` note.

**Evidence**: a 60-day decision review (`scripts/decision_review.py`) found **14 contracts HURT** by premature OTM closes (avg `|Δ| ≈ 0.22` at close) costing −$3,572 — every one would have hit `otm_far AND dte_room` → OVERRIDE to HOLD. See [profit-target-optimization.md](profit-target-optimization.md) §3.2 (historical analysis; the spec is the forward fix).

---

## 5. Loss side — delta gates, premium stops, absolute

### 5.1 Delta gates (`exit_management.py:199 _delta_gate`)

| Strategy | Condition | Action |
|----------|-----------|--------|
| **CSP** | `|Δ| ≥ stop_delta_csp_critical (0.60)` | `STOP_DELTA` ("cut it — no offsetting asset") |
| **CSP** | `|Δ| ∈ [csp_decision (0.50), 0.60)` | warn only |
| **CC** | `Δ ≥ cc_close (0.60)` AND `DTE ≤ cc_assign_dte (14)` | HOLD + warn ("assignment imminent; letting the wheel turn") |
| **CC** | `Δ ≥ cc_close (0.60)` AND `DTE > 14` | `ROLL_UP_OUT` (net-credit-only) |
| **CC** | `Δ ∈ [cc_decision (0.50), 0.60)` | warn only |

> The CC wheel-turn fix (2026-08-08): a deep-ITM CC inside 14 DTE is held for assignment (the wheel rotating); beyond 14 DTE it's rolled up-and-out. The Δ 0.50–0.60 band stays warn-only — CC assignment is often a desired wheel outcome.

### 5.2 Premium-multiple stops (`exit_management.py:244 _premium_tier_stop`)

**CSP only** — CC is exempt (option loss offset by share gains). `loss_multiple = |profit_captured|/100`.

| DTE tier | Alert | Close |
|----------|:---:|:---:|
| `> 30` (far) | 2.0× (`far_alert`) | 3.0× (`far_close`) |
| `21–30` (mid) | 1.0× (`mid_alert`) | 2.0× (`mid_close`) |
| `≤ 21` (near, gamma) | 0.5× (`near_alert`) | 1.5× (`near_close`) |

### 5.3 Absolute catch-all — premium-tiered (`exit_management.py:162`)

`pnl_dollars < −heavy_loss_for_premium(premium_collected)` → `STOP_LOSS`, both CC and CSP.

The floor **scales with total premium collected** (`entry × qty × 100`) so a large credit isn't cut on normal ITM drift. The flat `−$1,000` predecessor fired at a 0.17× give-back on a $6,000 CSP — pure theta noise — and because it sat *after* the premium tier (5.2) in precedence, it pre-empted the 1.5×/2×/3× tiers for any premium above ~$667, making them dead code on every meaningful trade.

| Total premium collected | Max-loss floor | As % of premium |
|---|---|---|
| < $500 | $1,000 | ≥ 200% (premium tier governs) |
| $500 – $2,000 | $2,000 | 100–400% |
| $2,000 – $5,000 | $5,000 | 100–250% |
| > $5,000 | $8,000 | ≤ 133% (e.g. a $6k AMD CSP cuts at −$8k, ~1.33×) |

The bottom band preserves the legacy `−$1,000` behavior for small trades; the top band lets the DTE-adjusted premium tier (5.2) be the binding constraint for ordinary losses, with the floor reserved for a genuine rout. **Config key**: `stop_loss.delta.heavy_loss_bands` (list of `{premium_max, max_loss}` rows). Legacy `heavy_loss_abs: N` (single scalar) still works — collapsed to one band at `.inf`.

### 5.4 Loss-side trend overlay (`profit_management.py:256 loss_alert_should_hard_stop`)

At the 2× premium alert: if `trend_composite < 40` → treat as hard stop (close/roll immediately); if `trend ≥ 40` → one extra roll attempt, then forced decision. **Trend never overrides the 3× / critical-delta hard stops** decided upstream.

**Config** (`stop_loss`): `premium_stop.far_dte: 30`, `mid_dte: 21`, alert/close multiples as above; `delta.csp_critical: 0.60`, `csp_itm: 0.50`, `csp_decision: 0.50`, `cc_critical: 0.50`, `cc_close: 0.60`, `cc_assign_dte: 14`, `heavy_loss_bands: [{≤500→1000}, {≤2000→2000}, {≤5000→5000}, {>5000→8000}]` (legacy `heavy_loss_abs` scalar still accepted as a single-band fallback).

---

## 6. Rolling discipline

**File**: `src/risk/monitor.py:152 check_roll_discipline`. Enforced on any proposed roll.

| Rule | Config | Rationale |
|------|--------|-----------|
| **Net credit only** | `rolling.net_credit_only: true` | Never pay a debit to roll — worsens breakeven, re-opens a <50%-POP trade |
| **≤ 2 rolls/campaign** | `max_rolls_per_campaign: 2` | >2 rolls = broken thesis, not bad luck |
| **≥ 30-day extension** | `min_extension_days: 30` | Rolling weeklies churns commissions |
| **Broken-position test** | `broken_if_credit_requires_dte_gt: 90` | If only a 90+ DTE roll gets a credit → close instead |

Campaign accounting: `campaign_net_credit()` sums all per-share premiums across the roll chain (negative = "broken position wearing a roll costume"); `is_roll_chain_broken()` flags ≥3 legs with successively *lower* strikes (the death-spiral signature — never ratchet a CC strike *down* to harvest premium on a falling stock).

**Evidence**: [loss-management-playbook.md](loss-management-playbook.md) §3 (Tastytrade, Options Trading IQ).

---

## 7. Full CSP / CC decision matrices

### CSP (short put) — trend extension allowed
| `trend_composite` | Sentiment | IVR | DTE | `|Δ|` | Target | Action | OTM gate |
|---|---|---|---|---|---|---|---|
| ≥ 70 | BULL/NEUT | ≥30 | >21 | any | **85%** | HOLD→ROLL_DOWN_OUT | HOLD enforced |
| ≥ 50 | BULL/NEUT | — | >21 | any | **70%** | HOLD | HOLD enforced |
| < 50 | any | — | >21 | <0.30 | 50% | CLOSE | **OVERRIDE→HOLD** |
| < 50 | any | — | >21 | ≥0.30 | 50% | CLOSE, redeploy | CLOSE allowed |
| any | CAUT/BEAR | — | >21 | <0.30 | 50% | CLOSE | **OVERRIDE→HOLD** (theta works) |
| any | any | any | ≤21 | any | — | MANAGE_DTE | gate N/A |
| — | — | — | any | ≥0.60 | — | STOP_DELTA | — |
| — | — | — | any | loss ≥ tier | — | STOP_LOSS | — |

### CC (short call) — trend extension NOT allowed
| `trend_composite` | Condition | Target | Action | OTM gate |
|---|---|---|---|---|
| ≥ 50 (uptrend) | stock INTO strike | 50% | CLOSE **+ ROLL_UP_OUT** (keep shares); else close | CLOSE allowed (CC Δ rising = real risk) |
| < 50 | weak/negative | 50% | CLOSE, redeploy | far-OTM with room → HOLD |
| any | Δ ≥ 0.60, DTE ≤ 14 | — | HOLD for assignment (wheel turn) | — |
| any | Δ ≥ 0.60, DTE > 14 | — | ROLL_UP_OUT (credit only) | — |
| any | any | any | ≤21 DTE | MANAGE_DTE |

---

## 8. Hard gates that override every extension (re-stated)

1. **DTE ≤ 21** → manage today (gamma floor). Overrides every profit extension.
2. **Capital SCARCE** → book at base 50% (unless `bypass_scarce_when_csp_paused` + CSP paused).
3. **≥ 2× credit lost** → loss-alert decision tree; trend never overrides 3×/critical-Δ hard stops.
4. **Earnings within DTE** → close before earnings (`auto_exit_triggers.earnings_imminent`).

---

## 9. Validation

- `tests/test_exit_management.py` (31 tests) — the single decision core across the full matrix above; the **5 V-call CC-autonomy regression fixtures** (the seed-book case where deep-ITM V calls must roll up-and-out).
- `tests/test_profit_management.py` (31 tests) — `decide_profit_target` trend extension, the OTM gate, capital scarcity.
- `tests/test_profit_target_optimization.py` (236 tests) — table-driven over the §7 matrices.
- `tests/test_holdings_exit.py` (37 tests) — stock-leg dead zone / backstop / circuit breaker.
- `tests/test_risk.py` (26 tests) — assignment handlers, roll discipline, coverage checks.

All pass. Coverage: `profit_management` 93%, `exit_management` 89%, `holdings_exit` 100%.
