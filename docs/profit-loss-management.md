# Profit & Loss Decision System

**How the engine decides when to take profit, when to roll, and when to cut a loss.**
Implementation of [`specs/profit-loss-management-spec.md`](../specs/profit-loss-management-spec.md).
Evidence base: [`specs/loss-management-playbook.md`](../specs/loss-management-playbook.md).

---

## 1. The one insight that drives everything: the strategy-direction asymmetry

A short option's response to an uptrend depends on **which option you sold**:

| Strategy | What an uptrend does to your short | Trend effect | Engine response |
|---|---|---|---|
| **CSP** (short put) | Stock runs *away* from strike → decay accelerates, delta → 0 | **Helps** | **Extend** the 50% target (hold for 70–85%) |
| **CC** (short call) | Stock runs *into* strike → delta+gamma climb, upside capped | **Hurts** | **Never extend**; instead **roll up-and-out** to keep shares |

This is why the framework computes a rich signal stack (trend, sentiment, IV) for *entry* but historically applied none of it to the *exit*. The exit layer was the last place that stack was unused. The P&L decision system closes that gap — conditionally, with hard gates that override greed.

---

## 2. The decision core — `src/analysis/profit_management.py`

A single pure-function module, shared by **all three surfaces** (portfolio, screener, OIE paper engine). No I/O — callers assemble the inputs the engine already computes.

```python
decide_profit_target(strategy, profit_captured, dte, delta, trend_ctx, capital_scarcity) -> ProfitDecision
```

**Priority order — hard gates win first:**

1. **DTE ≤ 21** → `MANAGE_DTE` (gamma floor — overrides *everything*: close / roll / assign)
2. **Capital SCARCE** → base 50% `CLOSE` (capital opportunity cost beats trend greed)
3. **Earnings in DTE** → close before earnings (existing `auto_exit_triggers`)
4. **CSP + trend extension enabled**: strong trend (composite ≥70 + sentiment ok + IVR ≥30) → **85%**; confirmed trend (composite ≥50 + sentiment ok) → **70%**; else 50%
5. **CC**: always base 50% (uptrend is the danger side), but `ROLL_UP_OUT` when trend ≥50 (keep shares, recapture upside)
6. **Loss-side** (`profit_captured < 0`): existing premium-multiple stops unchanged; adds the 2× trend overlay (below)

### The action vocabulary

| Action | Meaning | Where it fires |
|---|---|---|
| `CLOSE` | Book profit now, redeploy capital | winner at target, no trend extension |
| `HOLD` | Below target — keep collecting theta | the new behavior in trends (CSP at 55% targeting 85%) |
| `ROLL_DOWN_OUT` | CSP winner: roll to lower strike + more DTE for credit | CSP at trend target in uptrend |
| `ROLL_UP_OUT` | CC winner: roll to higher strike + more DTE for credit | CC winner in uptrend |
| `MANAGE_DTE` | ≤21 DTE — close/roll/assign | gamma floor |

---

## 3. The rolling-winner lane (the real "book more" answer)

Closing at 50% in ~15 days enables ~2–3 cycles per entry — the **capital velocity**, not the target level, drives the +73% P&L/day edge (eDeltaPro backtest). Rolling a winner captures **both** velocity (you bank the win) **and** trend (you stay in the thesis) — strictly better than either flat-close or hold-to-expiry **when a credit roll exists**.

- **CSP winner in uptrend** → roll **down-and-out** (lower strike, 30–45 DTE) for net credit. Tightens delta toward 0.20–0.30, re-collects premium, keeps the uptrend thesis.
- **CC winner in uptrend** → roll **up-and-out** (higher strike, 30–45 DTE) for net credit. Keeps shares, books profit, raises the ceiling. If no credit roll exists → close and hold shares unencumbered (never sell a *lower* strike — the death-spiral guard).

**Gating** (all pre-existing in `config/rules.yaml rolling`, unchanged): net-credit-only, ≤2 rolls per campaign, ≥30-day extension, never roll into earnings.

---

## 4. Loss management (integrated — thresholds unchanged)

The loss side was already mature. The P&L system adds **one** trend input and changes **no** thresholds.

| Layer | Rule | Where | Status |
|---|---|---|---|
| Premium-multiple stops | Far 3×/Mid 2×/Near 1.5× (DTE-adjusted) | `holding_score._score_option` + `stop_loss` config | unchanged |
| Delta gates | CSP critical 0.60 / ITM 0.50 · CC critical 0.50 | `stop_loss.delta` | unchanged |
| Roll-first discipline | Attempt roll before hard stop; credit-only; ≤2 rolls | `roll_first.py` + `rolling` config | unchanged |
| **2× trend overlay (NEW)** | At 2× alert: trend <40 → hard stop; trend ≥40 → one roll, then forced | `profit_management.loss_alert_should_hard_stop` | **added** |
| Holdings exit (stock leg) | -30% backstop below declining SMA, -40% breaker, dead zone, thesis gates | `holdings_exit` config | unchanged |

Trend **never** overrides the 3× / critical-delta hard stop — those always win.

---

## 5. How each surface uses it

| Surface | Behavior | File |
|---|---|---|
| **Real portfolio** (`portfolio.py --health`) | **Recommends**. Option decisions show trend-modulated targets + roll advice. A "Roll winners" recommendation lists tickers to roll. **No live orders** (repo rule). | `scripts/portfolio.py` `_score_options`, `_print_recommendations` |
| **Screener** (`screener.py`) | For top candidates you already hold, prints a "🔄 HELD WINNERS in trend" hint to consider a net-credit roll. | `scripts/screener.py` |
| **OIE paper engine** (`oie_engine.py`) | **Paper-executes**. Exit phase calls `decide_profit_target`: CLOSE → close; ROLL_* → close + redeploy in screen phase; HOLD → let it run (captures more theta in trends). Fully isolated from the real account. | `scripts/oie_engine.py` PHASE 3 |

### Where the trend signal comes from

Each surface reuses `src.scoring.screener_score._trend_composite(snap)` — the **same** 0–100 composite the entry layer scores on. There is no second definition. `TrendContext` is assembled per-underlying from an enriched `StockSnapshot` + sentiment via `trend_context_from_snapshot()`. If trend data is unavailable, the decision falls back to flat 50% (backward compatible).

---

## 6. Configuration — `config/rules.yaml` `profit_take`

```yaml
profit_take:
  csp:
    base_pct: 50                      # Tastytrade base
    strong_trend_target_pct: 85       # composite ≥70 + sentiment ok + IVR ≥30
    trend_target_pct: 70             # composite ≥50 + sentiment ok
    trend_extension_enabled: true    # CSP: trend raises target
  cc:
    base_pct: 50                      # CC never extends a short call in uptrend
    trend_extension_enabled: false
    roll_up_out_on_trend: true       # close + roll up-and-out for credit
  trend_inputs:
    strong_trend_composite_min: 70
    trend_composite_min: 50
    sentiment_direction_allow: [BULLISH, NEUTRAL]
    iv_rank_min: 30
  dte_floor: 21                       # overrides all extensions
  capital_scarcity_override: SCARCE  # SCARCE → book at base 50%
  loss_alert_trend_overlay:
    trend_composite_hard_stop_min: 40
    max_extra_roll_attempts: 1
```

Typed accessors in `src/config.py`: `profit_take()`, `profit_take_csp()`, `profit_take_cc()`, `profit_take_trend()`.

---

## 7. Worked example

CSP on MU, entry $50.55, 41 DTE, VIX 15 (CAUTIOUS), TREND_COMPOSITE 72, sentiment BULLISH, IVR 50:

- **Old behavior**: at 50% captured ($25.28 bid) → flat `CLOSE`. Money left on the table as MU kept rising.
- **New behavior**: strong-trend gate passes (72≥70, BULLISH allowed, IVR 50≥30) → target **85%**. The position `HOLD`s at 50%, 60%, 70%… At 85% ($7.58 bid) → `ROLL_DOWN_OUT` for credit (bank the $42.97 win, sell a fresh $800 put 45 DTE). If no credit roll exists → close.

Same position but TREND_COMPOSITE 30, sentiment CAUTIOUS → target stays **50%**, closes at the base. The trend extension only opens when the evidence stacks — and closes the moment any condition breaks.

---

## 8. Non-negotiables (preserved)

- **21-DTE gamma floor** — overrides every profit extension.
- **Net-credit-only rolling** — never pay a debit to roll.
- **Never sell CC below cost basis** (except the flagged dead-zone path).
- **Capital scarcity** — SCARCE capital books at base 50% regardless of trend.
- **Real portfolio never auto-executes** — recommend-only; only the OIE paper engine executes.

## Tests

`tests/test_profit_management.py` (28 cases) covers the full decision matrix: CSP/CC asymmetry, tiered targets, sentiment/IV gates, DTE/capital overrides, the loss-side overlay, config-driven thresholds, and `trend_ctx=None` backward compatibility (byte-identical to the old flat-50% behavior).
