# V Redeployment Plan — CC-Ladder Exit & Diversification

**Created**: 2026-08-14 (from live moomoo state)
**Goal reference**: GOAL.md #9 — "sell Visa stock and diversify into other good
fundamental equities which can give good premium earning options"
**Status**: PLAN — every tranche below must re-pass the pre-trade checklist on
the day it executes. This document does not authorize trades; the engine gates do.

---

## 1. Situation (2026-08-14)

| Item | Value | Rule status |
|---|---|---|
| V position | 500 sh @ $284.37 basis, ~$365 px (+28.5%) = $182.7K | 76.9% of net liq vs ≤25% cap 🔴 |
| V encumbrance | **5 open short calls = all 500 shares committed** | Collar gate: no new V CC possible |
| Liquid cash | $23.0K (9.7%) | < 10% critical 🔴 EMERGENCY |
| CSP liability | $119K (50.2%) — AMD 470P, AVGO 390P, GOOG 330P (all 09-18) | > 25% limit 🔴 |

The V CC ladder IS the exit plan. All five strikes (360–365) are at/near-ITM
with Δ 0.54–0.87: if V holds ≥ $365, assignments roll ~100 shares/week.

## 2. Assignment ladder (expected cash arrivals, if V ≥ strikes)

| Expiry | Strike | Δ (08-14) | Shares out | Strike cash in |
|---|---|---|---|---|
| 2026-08-14 | 362.5 | 0.87 | 100 | ~$36.3K |
| 2026-08-21 | 360 | 0.73 | 100 | ~$36.0K |
| 2026-08-28 | 360 | 0.68 | 100 | ~$36.0K |
| 2026-09-11 | 365 | 0.54 | 100 | ~$36.5K |
| 2026-09-18 | 360 | 0.64 | 100 | ~$36.0K |

Plus: premiums already collected on all five (kept if assigned).
On 09-18 the three CSPs also resolve — if assigned, AMD 100@$470 / AVGO 100@$390
/ GOOG 100@$330 arrive (aligned with GOAL #9: acquire AMD/AVGO), consuming cash.

**Contingency**: if V drops below a strike near expiry, that tranche expires
unassigned → shares stay, premium kept → re-sell the CC next cycle (collar gate
re-arms automatically once the old contract is gone). Do NOT sell unencumbered
shares below basis-adjusted targets in a hurry; the ladder is the mechanism.

## 3. Redeployment tranches — gated, staggered, sector-diversified

Targets from `config/rules.yaml watchlist.diversify` + GOAL.md #7
(minimum 3 sectors, no sector > 25%).

Each tranche = ONE new position per week maximum (monthly order guardrail:
15 EMERGENCY / 30 target; ≤ 2 profit-taking closes per ticker per month).

| Tranche | Trigger (after) | Action | Sector target |
|---|---|---|---|
| T0 — Repair | now | No new positions. Let 08-14 assignment restore cash ≥ 15%. Close/hold per engine decisions on existing positions only. | — |
| T1 | cash ≥ 15% AND CSP liability ≤ 25% | Start ONE CSP (30–45 DTE, Δ 0.20–0.30, IVR ≥ 30) on a diversification name — Consumer (KO/WMT/PG) or Industrial (CAT/GE/HON) | new sector ≠ Financial/Tech |
| T2 | next weekly review | ONE CSP or 100-sh stock start in Healthcare (ABBV/JNJ/UNH) | ≤25% sector |
| T3 | next weekly review | ONE position in Energy (XOM/CVX) or Financial-small (JPM) | ≤25% sector |
| T4+ | repeat | One name/week until: 3+ new sectors, each ≤25%, single name ≤15% target | |

Rules baked into every tranche:
- **Single name ≤ 15% target** (EMERGENCY stage cap; 25% absolute) — at ~$200K
  net liq that is ≤ $30K name size → typically ONE CSP contract on a ≤$300 name,
  or 100 shares of a ≤$300 name.
- **CSP total ≤ 25% of net liq** and 100% cash-secured by liquid (margin BP does
  not secure — GOAL #4).
- **Credit-stress gate**: if HYG/IEF STRESSED at entry, size at 50% cap.
- **Earnings blackout**: no entry whose DTE window crosses earnings (14-day rule).
- **Never re-concentrate into V** — V is on the reduce-side of GOAL #9; if a
  partial V residual remains (assignments missed), re-ladder CCs rather than
  buying more.

## 4. Target end-state (illustrative, ~$200K net liq)

| Bucket | Target | Notes |
|---|---|---|
| Cash reserve | ≥ 20% (NEUTRAL) | regime-scaled |
| CSP deployed | ≤ 25% | ≤ 3–4 concurrent CSPs at this size |
| Sectors | ≥ 3 non-Financial each ≤ 25% | per rules.yaml diversify lists |
| V residual | 0–100 sh | whatever the ladder leaves; CC any unencumbered 100-lot |
| Open options | ≤ 10 | bandwidth guardrail |

## 5. Explicit do-NOTs

- ❌ No lump-sum redeployment of the first $36K into one name/sector.
- ❌ No new CSPs while cash < 15% or CSP liability > 25% (pause triggers).
- ❌ No margin to "bridge" a tranche (GOAL #4).
- ❌ No V CC beyond free shares (collar gate — naked call).
- ❌ No chasing July-style churn: ≤ 2 closes/ticker/month, 14-day same-strike
  cooldown, watch the monthly Capture column (goal ≥ 30%).

## 6. Tax note (outside engine scope, plan for it)

500 V shares carry ~+$40K embedded gain (+28.5%). Laddered assignment over
5 weeks realizes it in tax-year 2026. Consider: (a) confirming lot method with
the broker (FIFO vs specific-ID), (b) whether any offsetting losers exist
(GOOG -2.9%, SKHY -4.7% are small), (c) estimated-tax impact. The engine does
not model taxes — this is an operator checklist item, not advice.
