# Regime Playbook — Bull / Bear / Volatile / Stagnant

**Status**: Analysis (2026-08-09). The regime table itself is implemented (`config/rules.yaml → regime`); this doc is the *behavioral analysis* of how the framework performs in each regime, and where it has gaps.
**Evidence base**: [loss-management-playbook.md](loss-management-playbook.md), [research_dte_selection.md](research_dte_selection.md)

> The user asked to "think this framework from Bull and bear phase of market and volatile vs stagnant market." This doc does exactly that — for each of the four market types, it states what the engine *does*, what it does *well*, and where it can *hurt you*.

---

## 1. The regime classification

`Config.regime_from_vix(vix)` (`src/config.py`):

| Regime | VIX | Position mult | Cash reserve | CSP:CC |
|--------|-----|:---:|:---:|:---:|
| **BULLISH** | < 12 | 0.80 | ≥ 15% | 60:40 |
| **NEUTRAL** | 12–20 | 0.75 | ≥ 20% | 50:50 |
| **CAUTIOUS** | 20–25 | 0.50 | ≥ 25% | 30:70 |
| **VOLATILE** | 25–30 | 0.25 | ≥ 30% | 10:90 |
| **BEARISH** | > 30 | 0.00 | ≥ 35% | 0:100 |

CSP pause triggers (any one): VIX > 25 · SPY < 200 SMA · regime ≤ −2 · cash < 20% · stock > 15% off basis.

The four market *types* the user named map onto these regimes as:
- **Bull** ≈ BULLISH + NEUTRAL
- **Bear** ≈ VOLATILE + BEARISH
- **Volatile** ≈ VOLATILE (high VIX, large swings)
- **Stagnant** ≈ low-VIX NEUTRAL / BULLISH with low realized vol (the tricky one)

---

## 2. Bull market (VIX < 12–20)

**What the engine does**: full position size (75–80%), normal deltas (CSP 0.20–0.30, CC 0.20–0.30), CSP-favored (60:40 in BULLISH). CSP trend-extension activates — confirmed uptrend lets CSP winners run to 70/85%. CCs that breach Δ 0.60 roll up-and-out to keep shares + recapture upside.

**Does well**:
- ✅ **Rides the trend** — CSP trend extension (85% target) is exactly right when the stock runs away from your short put. The asymmetry insight (extend CSP, never extend CC) is sound.
- ✅ **Keeps shares on CC breaches** — roll-up-out beats flat-close in a durable rally.
- ✅ **Diversification push** — full size + CSP-favored rotates the wheel into new names.

**Can hurt you** — ⚠ **the low-VIX paradox**:
- The BULLISH row (VIX < 12) is **the dangerous one**, not the safe one. Evidence ([loss-management-playbook.md](loss-management-playbook.md) §7): when VIX < 15, a 1% market drop spikes VIX far harder (probability VIX falls on a down day: 1-in-25 at low VIX vs 1-in-4 above 20), and **thin premium doesn't pay for the asymmetric jump risk**.
- The config already reflects this — `position_mult.BULLISH: 0.80` (capped from the original 1.0), with a comment citing the decision. But 0.80 may still be too aggressive; the research suggests practitioners *reduce* size 30–50% below VIX 15, and the optimal zone is VIX 15–22.
- **Assignment risk in a sharp rally**: CSPs you sold at 0.20Δ can go ITM fast in a V-shaped recovery (the 2020 scenario). The delta gates (cut at 0.60) protect, but you book the loss.

**Recommendation**: treat VIX < 13 with extra caution — size down manually even though the engine allows 80%. Consider tightening CSP delta to 0.15–0.20 in deep complacency.

---

## 3. Bear market (VIX > 30, or sustained downtrend)

**What the engine does**: **0% new positions** (position_mult 0.0), 35% cash reserve, CSP:CC 0:100 (existing CCs only). CSP pause triggers fire on VIX > 25 already, so by the time you're BEARISH, the engine has stopped opening CSPs for ~5 VIX points. Existing positions are managed: CSP cuts at |Δ| ≥ 0.60; the holdings-exit framework watches for thesis-break + −30% backstop (if below declining 200 SMA) + −40% circuit breaker.

**Does well**:
- ✅ **Stops the bleeding at the source** — no new CSPs means no new assignment risk into a falling market. This is the single most important bear-market behavior.
- ✅ **Thesis-break exits** — the codable gates (growth stall, dual deceleration, margin erosion, balance sheet, cash flow) catch fundamental breakage *before* the price floor. BROKEN → exit signal even at a loss.
- ✅ **Price backstops** — −40% unconditional circuit breaker prevents the "hold forever" trap. The −30% conditional (only if below a *declining* 200 SMA) correctly avoids false-signaling in V-recoveries.

**Can hurt you**:
- ⚠ **The wheel's losses come from the stock, not the option** ([playbook §1](loss-management-playbook.md)): 94–99% of wheel return is the underlying. In a bear market, **assigned shares are 1.0Δ exposure added exactly as price falls** ("the more it falls, the more delta"). The engine can't save you from assignments already in the book.
- ⚠ **Correlations converge to ~0.95 in crashes** ([playbook §2](loss-management-playbook.md)) — "8 large-cap wheel positions behave like 2–3 independent bets." Your diversification evaporates exactly when you need it. The 15% single-position cap helps but doesn't solve simultaneous assignment.
- ⚠ **The long bear is the fatal regime, not the crash** ([playbook §1](loss-management-playbook.md)): 2000–2013 left the S&P underwater 13 years. Calls struck at old highs collect near-zero premium for a decade. The framework has no defense against a multi-year grind except the thesis-break + time-stop (12-month) exits — which require you to actually act on them.
- ⚠ **2022 stock-bond correlation flipped to +0.30** — cash is the only reliable dry powder. The cash-reserve rule (≥ 35% BEARISH) is the right call.

**Recommendation**: in a confirmed bear, the engine's "do nothing new" is correct — but you must actively work the *existing* book: run thesis checks, take the −30%/−40% exits, and don't average down idiosyncratic losers. The framework gives the signals; execution is manual.

---

## 4. Volatile market (VIX 25–35, large swings)

**What the engine does**: 25% position size, 30% cash reserve, CSP:CC 10:90 (work existing only). CSP deltas tighten to 0.10–0.20; CC deltas widen to 0.30–0.40 (aggressive — rich premium). CSP pause is active (VIX > 25). The OTM-only close gate (hold far-OTM winners past 50% if Δ<0.30 and DTE>21) lets theta work on the decay side.

**Does well**:
- ✅ **Captures the rich premium** — high VIX = fat premiums; the engine keeps selling CCs on existing shares (the 90% side) at elevated IV. This is where the wheel earns its keep.
- ✅ **Tight CSP deltas reduce assignment risk** — 0.10–0.20Δ puts are far OTM, surviving the swings.
- ✅ **Gamma floor at 21 DTE** — the hard rule that overrides every profit extension is most valuable in volatile regimes (gamma doubles 21→7 DTE for ATM options).
- ✅ **Wider CC deltas (0.30–0.40)** — sell richer calls further OTM, lower assignment probability, more room for the volatile swings.

**Can hurt you**:
- ⚠ **Gamma risk on the short-dated leg** — even with the 21-DTE floor, a volatile swing into a 25-DTE position can blow through the short strike fast. The delta gates (cut at 0.60) are the defense, but in a gap they trigger *after* the damage.
- ⚠ **IV crush after the event** — if you sold into event-driven VIX (earnings, macro shock), the premium looks great until vol normalizes and your CC is suddenly far OTM (good) or your CSP is tested (bad). The earnings blackout (14 days) handles earnings; macro shocks have no blackout.
- ⚠ **Whipsaw** — volatile regimes have V-shaped reversals. A CSP you cut at |Δ| 0.60 may have expired worthless if you'd held. The trend-aware loss overlay (trend ≥ 40 → one roll attempt) mitigates but doesn't eliminate this.
- ⚠ **VRP gate can mislead** — `IV > HV × 0.8` passes easily in volatile regimes (both IV and HV are elevated). High VRP doesn't mean *profitable* VRP if realized vol keeps climbing.

**Recommendation**: volatile regimes are premium-rich but gamma-dangerous. Lean toward **longer DTE (45–60)** to dilute gamma, accept slightly lower annualized RoC, and respect the 21-DTE floor religiously.

---

## 5. Stagnant market (low VIX, low realized vol, sideways chop)

**What the engine does**: classified NEUTRAL/BULLISH (VIX 12–20), normal sizing. The engine keeps selling premium; theta decay is the income engine in a flat market.

**Does well**:
- ✅ **Theta is the friend here** — in a sideways market, short premium wins mechanically. The 30–45 DTE entry + 50% profit-take + 21-DTE management is *designed* for this regime. This is the wheel's home turf.
- ✅ **OTM-only close gate shines** — far-OTM winners decay on schedule; holding them past 50% (rather than closing) captures the full theta.
- ✅ **CC income on dead-money shares** — covered calls on holdings you'd hold anyway are pure carry in a flat market.

**Can hurt you** — ⚠ **the stagnant-market traps**:
- ⚠ **Premium is thin** — low IV = small premiums = low RoC. The 12% CSP / 8% CC RoC gates may screen out most candidates, leaving you under-deployed. Don't loosen the gates to "do something" — that's the trap.
- ⚠ **Sudden vol expansion** — stagnant markets end with a vol spike. The CSPs you sold at 0.20Δ in calm conditions are the ones that get tested in the break. The low-VIX paradox (§2) applies.
- ⚠ **Dead-zone CCs on underwater shares** — if a holding is > 15% below basis (the dead zone), the basis-strike CC pays ~$0. The framework correctly says "hold unencumbered or thesis-check," but stagnant underwater positions tie up capital for months. The `months_to_recover > 12` flag is the redeploy signal — act on it.
- ⚠ **Boredom-driven overtrading** — the per-ticker frequency cap (`max_closes_per_ticker_per_month: 2`) and monthly order cap exist precisely to stop V-style churn in a quiet market.

**Recommendation**: stagnant markets are where the wheel *should* earn its keep — be patient, respect the gates, and don't chase thin premium. Use the time to research new watchlist names.

---

## 6. Cross-regime robustness summary

| Behavior | Bull | Bear | Volatile | Stagnant |
|----------|:---:|:---:|:---:|:---:|
| New CSPs allowed | ✅ (trend-ext) | ❌ (paused) | ⚠ minimal | ✅ (theta) |
| CC income on holdings | ✅ (roll up-out) | ⚠ existing only | ✅ rich IV | ✅ carry |
| Assignment defense (Δ 0.60 cut) | ✅ | ✅ | ⚠ gaps | ✅ |
| Thesis-break exits | ✅ | ✅✅ critical | ✅ | ✅ |
| Gamma floor (21 DTE) | ✅ | ✅ | ✅✅ critical | ✅ |
| Main risk | low-VIX jump | long grind | gamma/whipsaw | thin premium + boredom |

**The framework's center of gravity is the NEUTRAL-to-mild-CAUTIOUS range (VIX 15–25)** — that's where every rule is well-calibrated. The extremes (deep bull, deep bear) are where it needs the most human judgment layered on top.

---

## 7. Per-regime watch-items (the human layer the engine can't do)

| Regime | What the engine can't see | Your job |
|--------|---------------------------|----------|
| Bull | The low-VIX jump risk | Size down below VIX 13; tighten CSP delta |
| Bear | The "long bear" duration; correlation convergence | Take thesis-break + time-stop exits; don't average down idiosyncratic losers |
| Volatile | Gap risk through the short strike; event-driven IV | Lean longer DTE; respect 21-DTE floor; check *why* IV is high |
| Stagnant | Boredom; dead-zone capital tie-up | Don't loosen gates; act on `months_to_recover > 12` |

---

## 8. Open questions / proposed regime refinements

These are **UNVALIDATED** (need backtest before live capital depends on them):

1. **Cap BULLISH size lower** — research says VIX < 15 is the asymmetric-risk zone; 0.80 may be too high. Proposal: `position_mult.BULLISH: 0.65` below VIX 13. ([playbook §11 #9](loss-management-playbook.md))
2. **Regime-conditional stops** — Kaminski & Lo: trailing stops only add value in trending markets. The framework's stops are unconditional; consider making the −30% backstop regime-conditional (already partially done via the 200-SMA slope requirement).
3. **VIX9D vs VIX30D for short-dated risk** — academic work (Wysocki, arXiv 2508.16598) finds VIX9D beats VIX30D for short-premium sizing. Currently using VIX30D.
4. **Stagnant-market deployment** — when RoC gates screen everything out, the engine goes quiet. Consider a "minimum-deployment" mode that sells far-OTM CCs on holdings for any positive premium, with explicit thin-premium acknowledgement.

None of these are implemented. Run them through the (future) backtest harness first.
