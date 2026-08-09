# Profit & Loss Management Spec — Trend-Modulated Profit Booking + Integrated Loss Rules

> **⚠ STATUS NOTE (2026-08-09)**: This is the **original PROPOSED design spec** that drove the implementation. The trend-modulated profit booking it describes is **now implemented** in `src/analysis/profit_management.py` + `src/analysis/exit_management.py` + `src/scoring/holding_score.py`, and is documented canonically in [`exit-and-profit-management-spec.md`](exit-and-profit-management-spec.md). Read *this* doc for the design rationale and evidence; read the canonical doc for what the code actually does today. The "backtest pending" caveat below **remains true** — the trend thresholds (70/85%, trend_composite ≥ 50/70) are not yet validated on your own data (see [`production-deployment.md`](production-deployment.md) Gap A).

**Version**: v1
**Date**: 2026-08-01
**Original status**: PROPOSED — UNVALIDATED, backtest pending (repo convention: no live-order changes until `pytest` + historical backtest pass)
**Implementation status**: Implemented in code; rule thresholds still UNVALIDATED by backtest.
**Applies to**: Covered Calls, Cash Secured Puts, Wheel Strategy
**Related docs**: [`exit-and-profit-management-spec.md`](exit-and-profit-management-spec.md) (canonical current-state), [`loss-management-playbook.md`](loss-management-playbook.md) (evidence base), [`research_dte_selection.md`](research_dte_selection.md), [`margin-guardrail.md`](margin-guardrail.md), [`../GOAL.md`](../GOAL.md)

---

## 1. Executive Summary

**Problem**: The engine books profit at a fixed 50% of max premium. In a genuine uptrend with DTE still available and market sentiment supportive, that leaves money on the table — the position would keep decaying (CSP) or requires a roll (CC) rather than a flat close.

**Root cause found in audit**: The framework computes everything needed for a *trend-aware exit* — `TREND_COMPOSITE` (SMA alignment + ADX + RSI + MACD), `SENTIMENT_SCORE`, `IV_RANK`, and capital scarcity — but wires **none of them into the profit-taking decision**. `adaptive_profit.py` modulates the 50% target only by capital scarcity. `holding_score._score_option` closes at a flat `profit_captured >= 50` with no trend input. The exit/management layer is the last place the rich signal stack is not applied.

**Core fix (the asymmetry insight)**: trend extension of the 50% target is **strategy-direction-specific**:

| Strategy | Uptrend effect on short position | Trend extension of 50% target? |
|---|---|---|
| **CSP** (short put) | Stock runs *away* from strike → decay accelerates, delta heads to 0. Trend helps. | **YES** — raise target to 70–85% when `TREND_COMPOSITE >= 50` and sentiment supports |
| **CC** (short call) | Stock runs *into* strike → delta+gamma climb against you, upside capped. Trend hurts. | **NO** — hold 50% discipline in uptrend; instead **roll up-and-out** for credit to capture more upside while keeping shares |

**Secondary fix — rolling winners instead of closing**: when a winning CSP in an uptrend hits 50%, the highest-expected-value move is often **roll down-and-out for a credit** (same thesis, lower strike, more theta) rather than flat-close. Same logic for CC in uptrend: **roll up-and-out for credit**. This is the practitioner answer to "why not book more?" — you book the accrued profit *and* stay in the trend.

**Non-negotiables preserved**: 21-DTE gamma floor (hard rule), net-credit-only rolling, never sell CC below cost basis (except the flagged dead-zone path), capital-scarcity modulation remains as the final gate.

---

## 2. The Evidence: Why 50% Is the Base, and Where It Stops Being Optimal

The 50% rule is one of the most backtested rules in retail options. It is NOT wrong — it is unconditional. The trend overlay makes it conditional on regime. Evidence already vetted in this repo:

- **50% close wins on P&L/day**: 16Δ, 45-DTE backtest (2005–2018): closing at 50% gave **$2.04/day vs $1.18/day holding to expiry (+73%)**, duration 45 → ~15 days, risk flat or lower. — [eDeltaPro](https://www.edeltapro.com/blog/managing-winners) (playbook §3)
- **Risk/reward inverts past 50%**: at 50% profit you risk ~4:1 against for the remainder; at 80%, ~11.5:1. — [ApexVol](https://apexvol.com/strategies/wheel-strategy) (playbook §3)
- **21 DTE is the gamma cliff**: gamma roughly doubles between 21 and 7 DTE for ATM options; a 200K+ trade study found closing at 21 DTE improved risk-adjusted returns 15–20% vs holding to expiration. — [DaysToExpiry](https://www.daystoexpiry.com/blog/the-21-dte-rule-explained-when-and-why-to-close-options-positions-early) (playbook §3)
- **Managing early collapses probability-of-touch**: realized probability-of-touch drops to ~0.8× delta for puts when managed at 21 DTE vs ~2× delta held to expiry. — [tastylive](https://www.tastylive.com/news-insights/options-trading-exploring-probability-touch-various-deltas) (playbook §3)
- **Stops/profit rules are regime-contingent**: stop-losses only added value in trending (momentum) markets; a 10% trailing stop added 50–100bps/month in momentum regimes but *reduced* expected return in random-walk conditions. The corollary: profit-target *extension* is also regime-contingent — extend only when a real trend is confirmed. — [Kaminski & Lo](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338) (playbook §6)
- **Option Alpha consensus**: 30–45 DTE entry, 25–50% profit target, manage/roll at 21 DTE, emphasize *systematic* execution. — [specs/research_dte_selection.md §3.3](research_dte_selection.md)
- **The "let winners run" counter-evidence is the CCP-with-trend problem by default** — the win-rate illusion (96% win rate; the rare losses decide everything) means the danger case is *holding a position that reverses*, not *closing early*. — [The Intrinsic Investor](https://theintrinsicinvestor.com/research/wheel-strategy/) (playbook §1, GOAL.md)
- **The wheel's return is the stock**: 94–99% of wheel total return is attributable to the underlying; the option leg is a risk-damper. Profit booking that *protects the stock-leg trend* (CC roll up/out instead of assignment) is therefore more valuable than squeezing the last dollar of premium. — [Spintwig](https://spintwig.com/spy-wheel-45-dte-options-backtest/) (playbook §1)

**Synthesis**: 50% is the correct *unconditional* base. The evidence that it wins is driven by (a) the risk/reward inversion past 50%, and (b) the gamma cliff at 21 DTE. **Both of those risks are conditional** — they shrink when (1) the underlying is moving away from your short strike (CSP in uptrend), (2) the position is far OTM with low DTE remaining, and (3) the trend is confirmed by multiple independent signals (SMA stack + ADX + MACD + sentiment). When all three hold, the conditional expected value of extending is positive. That is precisely the lane this spec opens — and closes it again the moment any of the conditions break.

---

## 3. Current-State Audit (exact files/gaps)

| Component | File | What it does today | Gap |
|---|---|---|---|
| Adaptive profit target | `src/analysis/adaptive_profit.py` | 50% base; 70%/85% only when capital abundant; 21 DTE hard floor | **No trend/sentiment/IV input**; extension is capital-only |
| Option exit scoring | `src/scoring/holding_score.py` `_score_option` | `profit_captured >= 50` → CLOSE; `>= 70` → CLOSE; thesis check only for `pl < -$1000` | **Flat profit threshold**; trend-aware thesis is loss-side only |
| CC config | `config/rules.yaml` `cc_management` | `close_at_profit_pct: 0.50` | No CSP profit rule; no trend modulation |
| CSP config | `config/rules.yaml` | — | **No explicit CSP profit-booking rule at all** |
| Entry signals | `src/signals/generator.py` + `src/analysis/trend.py` | `TREND_COMPOSITE`, `SENTIMENT_SCORE`, `IV_RANK` drive STRONG_WRITE/WRITE/HOLD/AVOID | **Computed for entry only — never reused for exit** |
| Loss-side rules | `config/rules.yaml` `stop_loss` + `src/scoring/holding_score.py` + `src/risk/holdings_exit.py` + `src/analysis/roll_first.py` | Layered premium-multiple stops, delta gates, backstops, roll-first discipline | Mature; **keep**. Loss-side stays loss-side — no change needed beyond wiring trend into the 2x-alert review |
| Capital scarcity | `src/analysis/adaptive_profit.py` | Position-utilization + cash-buffer → 50/70/85% | Keep as the final gate after trend |

**The single architectural fix**: extend the exit computation with a `TrendContext` (already computed: `trend_composite`, `sentiment_score`, `iv_rank`, `days_to_expiry`, `current_delta`, `profit_captured`) and let the **strategy-direction asymmetry** decide the target.

---

## 4. Proposed Framework — Tiered Trend-Modulated Profit Booking

### 4.1 Inputs (all already computed by the engine — zero new data sources)

```
TREND_COMPOSITE   <- src/analysis/trend.py          [0-100]
SENTIMENT_SCORE   <- src/signals/sentiment.py       [0-100] (direction: BULLISH/NEUTRAL/CAUTIOUS/BEARISH)
IV_RANK           <- options chain                  [0-100]
DAYS_TO_EXPIRY    <- open position                   [int]
CURRENT_DELTA     <- open position greeks            [float]
PROFIT_CAPTURED   <- 1 - (current_bid / entry_credit) [0-100]
CAPITAL_SCARCITY  <- src/analysis/adaptive_profit.py [SCARCE/NORMAL/ABUNDANT]
```

### 4.2 Decision Matrix — Profit Booking

**CSP (short put) — trend extension ALLOWED:**

| Condition | Profit target | Action | Rationale |
|---|---|---|---|
| `TREND_COMPOSITE >= 70` AND `SENTIMENT_DIRECTION in {BULLISH, NEUTRAL}` AND `IV_RANK >= 30` | **85%** | Hold; GTC at 85%. If 50% hit < 21 DTE and trend intact -> **roll down-and-out for credit** instead of closing | Strong stacking; stock running away from strike; decay accelerating |
| `TREND_COMPOSITE >= 50` AND `SENTIMENT_DIRECTION in {BULLISH, NEUTRAL}` | **70%** | Hold; GTC at 70% | Confirmed-but-not-perfect trend |
| Anything else (trend < 50, or sentiment CAUTIOUS/BEARISH) | **50%** (base) | Close at 50%, redeploy | Use the evidence base unchanged |
| `DTE <= 21` | **CLOSE / ROLL / ASSIGN** regardless of profit | Hard floor (gamma) | Unchanged — non-negotiable |
| Capital scarcity = SCARCE | -> override trend; book at base 50% | Free slots + cash | Capital gate wins over trend extension |

**CC (short call) — trend extension NOT ALLOWED:**

| Condition | Profit target | Action | Rationale |
|---|---|---|---|
| `TREND_COMPOSITE >= 50` (uptrend) | **50%** (base) | Close AND **roll up-and-out for credit** to re-capture upside while keeping shares; if no credit roll available -> close and let shares ride | Uptrend is the *danger* side for CC; never extend a short call in an uptrend |
| Trend weak/negative (`TREND_COMPOSITE < 50`) | **50%** (base) | Close and redeploy | Book the win before the stock-leg trend turns |
| `DTE <= 21` | **CLOSE / ROLL / ASSIGN** | Hard floor | Unchanged |
| Capital scarcity = SCARCE | base 50% | Free slots + cash | Unchanged |

### 4.3 The Rolling-Winner Lane (the actual "book more" answer)

Rather than flat-close at 50% when the trend supports more:

- **CSP winner in uptrend**: **roll down-and-out** (lower strike, 30–45 DTE extension) for a net credit. Captures the accrued profit, tightens delta toward 0.20–0.30, re-collects premium, and keeps participating in the uptrend thesis. Rules: net credit only; <= 2 rolls per campaign; >= 30-day extension; never roll into earnings (all already in `config/rules.yaml` `rolling` + `src/analysis/roll_first.py`).
- **CC winner in uptrend**: **roll up-and-out** (higher strike, 30–45 DTE) for a net credit. This is the classic covered-call answer to a breached/approaching call — you keep shares, book profit, and extend the ceiling. If no up-and-out credit is available, close and hold shares unencumbered (do NOT sell a lower strike — the death-spiral guard, [thetagang bot](https://explore.market.dev/ecosystems/python/projects/thetagang), playbook §4).
- **The point of the 50% rule is capital velocity**: closing at 50% in ~15 days enables ~2–3 cycles per entry window; the compounding of redeployed capital, not the level of the target, drives the +73% P&L/day edge. Rolling a winner captures BOTH velocity (you bank the win) AND trend (you stay in the thesis) — it is strictly better than either flat-close or hold-to-expiry when a credit roll exists. If a credit roll does NOT exist, the evidence says close.

### 4.4 DTE Floor + Final Gates (unchanged, re-stated for completeness)

1. `DTE <= 21` -> manage today: close / roll (credit-only) / assign. **This overrides every profit extension.** — [DaysToExpiry](https://www.daystoexpiry.com/blog/the-21-dte-rule-explained-when-and-why-to-close-options-positions-early)
2. `CAPITAL_SCARCITY == SCARCE` -> book at base 50%. Capital opportunity cost beats trend greed.
3. Any option at **>= 2x credit received** -> loss-alert decision tree (roll / assign / exit, §3), trend input optional but NOT allowed to justify holding a 2x loser.
4. Earnings within DTE -> close before earnings (`auto_exit_triggers.earnings_imminent`), regardless of trend.

---

## 5. Loss Management — Integrated Spec (option leg + stock leg)

Loss-side rules are **already mature** in this framework. The spec re-affirms them and adds exactly one trend input (the 2x-alert review). No loss thresholds change.

### 5.1 Option leg (short premium)

| Layer | Rule | Config | Status |
|---|---|---|---|
| Premium-multiple stops | Far (>30 DTE): alert 2x / close 3x · Mid (21–30): alert 1x / close 2x · Near (<21): alert 0.5x / close 1.5x | `rules.yaml stop_loss.premium_stop` | keep |
| Delta gates | CSP: warn 0.40 decision · ITM 0.50 · critical 0.60 · CC: warn 0.40 · critical 0.50 | `rules.yaml stop_loss.delta` | keep |
| Roll-first discipline | Attempt roll before hard-stop buyback; net credit only; <= 2 rolls; >= 30-day extension | `src/analysis/roll_first.py` + `rules.yaml rolling` | keep |
| **NEW: trend input at 2x alert** | When a position hits the 2x-alert level, evaluate `TREND_COMPOSITE`: if trend < 40 -> treat as hard-stop (close/roll immediately); if trend >= 40 -> allowed to run one roll attempt only, then forced decision. Trend NEVER overrides the 3x/critical-delta hard stop. | new `stop_loss.trend_alert_overlay` | PROPOSED — UNVALIDATED |

### 5.2 Stock leg (assigned shares)

Unchanged — the holdings-exit framework is already trend-aware: — [playbook §4–§6](loss-management-playbook.md)

| Rule | Config | Status |
|---|---|---|
| Never sell CC below cost basis (except flagged dead-zone path) | `rules.yaml cc_management.never_sell_below_cost_basis` | keep |
| -30% backstop *if* below declining 200 SMA; -40% unconditional circuit breaker | `rules.yaml holdings_exit.backstop_*` | keep |
| Dead zone > 15% below basis -> hold unencumbered or exit | `rules.yaml holdings_exit.dead_zone_drop_pct` | keep |
| Months-to-recover > 12 -> REDEPLOY review | `rules.yaml holdings_exit.months_to_recover_flag` | keep |
| Thesis-break gates (2+ of 6 codable) -> exit despite loss | `rules.yaml holdings_exit.thesis` | keep |

---

## 6. Config Changes — `config/rules.yaml` (PROPOSED, not applied)

```yaml
# ── Trend-Modulated Profit Booking ──
# UNVALIDATED — backtest pending. Do not rely on live orders until validated.
profit_take:
  csp:
    base_pct: 50                      # Tastytrade base (unchanged)
    strong_trend_target_pct: 85       # TREND_COMPOSITE >= 70 + SENT BULLISH/NEUTRAL + IVR >= 30
    trend_target_pct: 70              # TREND_COMPOSITE >= 50 + SENT BULLISH/NEUTRAL
    trend_extension_enabled: true
  cc:
    base_pct: 50                      # Tastytrade base (unchanged)
    trend_extension_enabled: false    # Uptrend is the danger side for CC; never extend
    roll_up_out_on_trend: true        # Close + roll up-and-out for credit when TREND_COMPOSITE >= 50
  trend_inputs:
    strong_trend_composite_min: 70
    trend_composite_min: 50
    sentiment_direction_allow: [BULLISH, NEUTRAL]
    iv_rank_min: 30
  # Final gates (hard)
  dte_floor: 21                       # overrides all extensions
  capital_scarcity_override: SCARCE   # SCARCE -> book at base 50%

  # NEW: trend-aware loss-alert overlay (2x premium)
  loss_alert_trend_overlay:
    trend_composite_hard_stop_min: 40   # trend < 40 at 2x alert -> treat as hard stop
    max_extra_roll_attempts: 1
```
