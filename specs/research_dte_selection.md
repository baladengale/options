# Optimal DTE Selection for Options Income — Research Report

**Date**: 2026-07-10
**Status**: Research complete, ready for implementation
**Applies to**: Covered Calls, Cash Secured Puts, Wheel Strategy

---

## Executive Summary

**30–45 DTE is the consensus optimal entry. Manage at 21 DTE or 50% profit — whichever hits first.**

This is the most backtested framework in retail options, validated across 200K+ trades by TastyTrade and independently replicated by multiple sources. Weeklies (0–7 DTE) produce higher gross returns on paper but worse net risk-adjusted returns in practice due to gamma blow-ups, transaction costs, and lack of recovery time.

---

## 1. Theta Decay Mechanics

Theta follows a ~1/√(time) curve — it accelerates exponentially near expiration, not linearly.

| DTE | Premium Remaining | Theta as %/day | Regime |
|-----|-------------------|----------------|--------|
| 45 | 100% (entry) | −1.5% | Slow, steady burn |
| 30 | ~80% | −2.5% | Acceleration begins |
| 21 | ~65% | −4.5% | **Exit point** — gamma rising fast |
| 14 | ~50% | −7% | Danger zone for sellers |
| 7 | ~32% | −14% | Fast decay, extreme gamma |
| 3 | ~15% | −30% | Coin-flip territory |
| 1 | ~5% | −80% | Pure gamma |

*Source: ORATS institutional data via ApexVol*

**Key insight**: Theta decay peaks in absolute dollar terms around 21–30 DTE. But the **theta/gamma ratio** — premium collected per unit of directional risk — is optimal at 30–45 DTE. At 7 DTE, theta is ~4× faster but gamma is ~8× worse. The ratio is net negative.

---

## 2. Gamma Risk Tradeoff

ATM option comparison on SPY:

| Metric | Weekly (7 DTE) | Monthly (30 DTE) |
|--------|---------------|-------------------|
| Gamma | 0.08 (High) | 0.024 (Moderate) |
| Theta/day ($) | −$0.34 | −$0.22 |
| Theta as % premium/day | −14% | −3.4% |
| Vega | +8 | +22 |

*Source: ApexVol / ORATS*

**Practical impact**: A 1% move in the underlying at 7 DTE can swing a position from 30% profit to 50% loss in minutes. At 35 DTE, the same move barely registers. A quiet week of theta accrual is wiped out by a single gap move in the final 3 days.

**Real backtest evidence** (SPY, Oct–Dec 2025, $10K per strategy):

| Strategy | Wins | Losses | Win Rate | Net P&L |
|----------|------|--------|----------|---------|
| Weekly (7 DTE, 20Δ) | 9 | 3 | 75% | **−$942.60** |
| Monthly (45 DTE, 20Δ) | 2 | 1 | 67% | **+$294.40** |

Weeklies won more often but losses were so large they overwhelmed the gains. This is the core fallacy: "quicker premium = better" ignores the gamma asymmetry.

---

## 3. Expert Frameworks

### 3.1 TastyTrade (200K+ trades backtested)

| Rule | Value | Rationale |
|------|-------|-----------|
| Entry DTE | **45** | Peak theta/gamma ratio |
| Strike delta | 15–30 | Balance premium vs assignment probability |
| Exit trigger | **21 DTE or 50% profit** | Avoid gamma explosion inside 21 DTE |
| Roll rule | Only for net credit | Never pay to extend a losing position |

**Win rate data** (SPY short put spreads, ~20 delta):
- 45 DTE: ~78% win rate, expected value **15× higher** than 7 DTE
- 7 DTE: ~71% win rate, expected value ≈ near zero

### 3.2 IV-Adjusted DTE (TastyTrade — Kai Zeng Research)

Fixed 45 DTE works, but adaptive DTE produces smoother daily P/L:

| IV Rank | Use This DTE | Why |
|---------|-------------|-----|
| IVR < 30 (low) | **60 DTE** | Low IV = cheap premium → extend duration to compensate |
| IVR 30–50 (normal) | **45 DTE** | Baseline sweet spot |
| IVR > 50 (high) | **30 DTE** | High IV = inflated premium → shorter duration locks it in faster |

Universal rule: exit at 21 DTE regardless of entry DTE. This equalizes daily P/L across all three regimes.

### 3.3 Option Alpha

Same core framework: 30–45 DTE entry, 25–50% profit target, manage/roll at 21 DTE. Emphasis on **systematic execution** — pick a rule and apply it mechanically. Consistency beats optimization.

---

## 4. Management Rules

### 4.1 The 50% Profit Rule

**Close credit positions at 50% of max profit.** If you collect $2.00 in premium, close when the position is worth $1.00.

The math of holding past 50%:

| Profit Captured | Risk to Capture Remainder | Risk/Reward Ratio |
|----------------|--------------------------|-------------------|
| 50% | Risk $4.00 to make $1.00 | 4:1 against you |
| 80% | Risk $4.60 to make $0.40 | 11.5:1 against you |
| 95% | Risk $4.90 to make $0.10 | 49:1 against you |

Closing at 50% profit improves annual returns by **10–15%** compared to holding to expiration (TastyTrade research).

### 4.2 Decision Matrix at 21 DTE

| Scenario | Action | Rationale |
|----------|--------|-----------|
| Profit ≥ 50% | **Close** | Take the win, redeploy capital |
| Profit < 50%, stock neutral | **Hold** | Let remaining theta work |
| Profit < 50%, moving against | **Close** | Don't let a small loser become a big loser |
| Small winner, IVR still high | **Consider rolling** | Maintain exposure while IV elevated |
| Small winner, IVR collapsed | **Close** | Opportunity cost > remaining premium |

### 4.3 Rolling Rules

- **Only roll for net credit.** If credit is unavailable, close the position.
- Roll **down + out** on puts (lower strike, later expiration)
- Roll **up + out** on calls (higher strike, later expiration)
- **Never roll inside 3–5 DTE** — gamma is too extreme
- **Never roll into earnings** — adds uncompensated event risk
- Target roll window: 21–28 DTE, at 30–50% of max profit

### 4.4 Covered Call Specific: Assignment Management

- If stock rallies past short call strike: roll **up and out** for credit if you want to keep shares
- If price > cost basis and profit target hit: **let assignment happen**, restart the wheel
- **Never sell calls below cost basis** — this locks in a loss
- Pre-dividend: roll ITM calls early if dividend > remaining extrinsic value (avoids early assignment)

---

## 5. Return Expectations

ApexVol 5-year backtest (2020–2024), wheel strategy:

| Style | Delta | Annual Return | Win Rate | Max Drawdown (2022) |
|-------|-------|---------------|----------|---------------------|
| Conservative | 0.15–0.20 | 12–18% | 85% | −15% |
| **Moderate (recommended)** | **0.25–0.30** | **20–28%** | **75%** | **−25%** |
| Aggressive | 0.35–0.45 | 30–45% | 65% | −40% |

Monthly income target: **1–3% on deployed capital** at moderate risk.

---

## 6. Weekly vs Monthly: Full Comparison

| Factor | Weekly (0–7 DTE) | Monthly (30–45 DTE) |
|--------|------------------|---------------------|
| Gross annual return (theoretical) | ~26% | ~24% |
| Win rate | ~71% | ~78% |
| Expected value per trade | Near zero | **15× higher** |
| Management decisions/year | 52 | 12 |
| Annual transaction costs | ~$1,310 | ~$302 |
| Assignment frequency | ~12/year | ~3–4/year |
| Recovery time after dip | None | 3–4 weeks of theta |
| Survives volatility spike? | No — blowup risk | Yes — time to adjust |
| Mental/decision fatigue | High | Low |
| Best account size | $5K–$50K | $25K+ |
| Best stock type | High-IV momentum | Stable, dividend payers |

The ~2% gross edge on weeklies is theoretical only. After transaction costs, slippage, and blow-up losses, monthlies win decisively on **net risk-adjusted returns**.

---

## 7. Transaction Cost Asymmetry

Weekly trading incurs hidden costs that erode the gross return edge:

| Cost Type | Weekly (52 cycles/yr) | Monthly (12 cycles/yr) |
|-----------|----------------------|-----------------------|
| Annual commissions (SPY spreads) | ~$270 | ~$62 |
| Annual slippage (bid-ask) | ~$1,040 | ~$240 |
| **Total annual friction** | **~$1,310** | **~$302** |

Slippage alone costs ~$1,000/year more on weeklies. On a $100K account, that's a 1% annual drag just from execution.

---

## 8. Recommendations for This Project

### 8.1 Account Profile
- **Size**: ~$100K
- **Holdings**: V (430 shares), $45K cash
- **Watchlist**: MSFT, GOOGL, AAPL, AMZN, NVDA, META, AVGO, ADBE, CRM, AMD
- **Strategy**: Wheel (CC on V → CSP on target → CC on assigned)
- **Goal**: Diversify out of concentrated V position while generating monthly income

### 8.2 Primary DTE Framework

| Parameter | Value |
|-----------|-------|
| Entry DTE | 30–45 |
| Strike delta | 0.20–0.30 |
| Exit trigger | 50% of max profit or 21 DTE (whichever first) |
| Roll constraint | Net credit only; never inside 5 DTE |

### 8.3 IV-Adjusted Overlay

| Condition | DTE Adjustment |
|-----------|---------------|
| Normal IVR (30–50) | 30–45 DTE baseline |
| Low IVR (< 30) | Extend to 45–60 DTE |
| High IVR (> 50) | Shorten to 21–30 DTE |
| Post-earnings IV crush | Skip cycle or go 45+ DTE |
| Pre-earnings (≤ 2 weeks) | **No new positions** (existing rule) |

### 8.4 Position Sizing
- 5–7 positions across different names (diversification)
- ~$15–20K notional per position
- $10–15K cash buffer for assignment/management
- **Hard constraint**: never exceed 100% capital coverage (CC = must own shares; CSP = must hold cash)

### 8.5 Hybrid Approach (Optional, for Active Management)
- **70%** capital → 30–45 DTE core positions (predictable base income)
- **30%** capital → opportunistic 14–21 DTE during IV spikes only
- The hybrid satisfies the "faster premium" itch without endangering the core book

---

## 9. Hard Constraints (Non-Negotiable)

These align with the project's CLAUDE.md constraints:

1. **CC only if 100 shares owned** per contract
2. **CSP only if full cash secured** at strike price
3. **No naked options, no spreads, no margin**
4. **No new positions within 2 weeks of earnings**
5. **Never sell calls below cost basis** (locks in loss)
6. **Data must be fresh** — sync before every run, abort on stale data

---

## 10. Implementation Impact

This research directly informs the following project components:

| Component | Impact |
|-----------|--------|
| `src/data/compute.py` | DTE filter for option chain screening |
| `src/data/models.py` | DTE field validation (reject < 7 DTE, warn < 14 DTE) |
| Constraint gates | Add DTE constraint: trades outside 21–60 DTE = hard fail or score penalty |
| Scoring engine | IV-adjusted DTE selection as a score booster |
| Daily digest | Flag positions approaching 21 DTE for management action |
| Management alerts | Auto-flag when 50% profit reached or 21 DTE approaching |

---

## 11. What to Avoid

| Anti-Pattern | Why |
|-------------|-----|
| 0–7 DTE on single-name tech | One NVDA/AVGO gap = months of premium wiped |
| 60+ DTE without low IV | Capital tied up, slow theta, more event risk |
| Holding past 21 DTE for last $0.20 | Risk/reward inverts catastrophically |
| Selling calls below cost basis | Locks in a loss on the wheel |
| Rolling for a debit | Paying to extend a loser — never |
| Chasing IV spikes blindly | Premium looks juicy until it's not; check why IV is high |

---

## Sources

1. [TastyTrade — How IV Impacts DTE Selection](https://www.tastylive.com/news-insights/how-iv-impacts-dte-selection)
2. [TastyTrade — Managing Short Vertical Spreads (50% profit rule)](https://www.tastylive.com/news-insights/managing-short-vertical-spreads)
3. [TastyTrade — Managing Small Winners at 21 DTE](https://www.tastylive.com/shows/from-theory-to-practice/episodes/managing-small-winners-at-21-dte-06-02-2023)
4. [DaysToExpiry — Best DTE for Covered Calls: Weekly vs Monthly (2025)](https://www.daystoexpiry.com/blog/covered-calls-by-expiration)
5. [DaysToExpiry — Best DTE for Credit Spreads: Data-Driven Comparison](https://www.daystoexpiry.com/blog/best-dte-for-credit-spreads-a-data-driven-comparison-of-30-45-and-60-day-trades)
6. [DaysToExpiry — Options Greeks by DTE: Quick Reference Tables](https://www.daystoexpiry.com/blog/greeks-by-dte-reference)
7. [ApexVol — Wheel Strategy: 1-3% Monthly Income Guide](https://apexvol.com/strategies/wheel-strategy)
8. [ApexVol — Weekly vs Monthly Options: Theta, Gamma & Trading Style](https://apexvol.com/compare/weekly-vs-monthly-options)
9. [ApexVol — Not Managing Winning Trades (risk/reward inversion)](https://apexvol.com/avoid/not-managing-winners)
10. [ImpliedOptions — Best Time to Roll Options: 21–28 DTE, 30–50% Profit Targets](https://impliedoptions.com/blog/best-time-to-roll-options-2025-09-05)
11. [TradeAlgo — Weekly vs Monthly Options for Income](https://www.tradealgo.com/trading-guides/options/weekly-vs-monthly-options-income)
