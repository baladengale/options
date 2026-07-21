## Foundational Goals

1. Consistently generate income over already-bought equities which are fundamentally strong and intended to hold for long term
2. Apply options wheel strategy to trade in fundamentally strong stocks only and ready to accept assignment if trade goes against our bet
3. Never trade only looking at premium and high returns — carefully screen option expiry, strike price, macro and technical parameters
4. Always keep margin usage in mind, NEVER prefer margin. Trade based on cash and equity. 30% margin max, hard 15-day window to clear by selling equity
5. Document every position, trade, premium collected along with total profit and loss for tracking
6. Every decision should be based on actual numbers or sentiments only, do not bypass this

## Actions — CC/CSP Allocation Rules (Always Referenced)

These rules are based on industry research (TastyTrade 200K+ backtested trades, Barchart bull strangle 367 trades, Intrinsic Investor SPY wheel 2018-2025). **Every recommendation must reference these before suggesting any trade.**

### 1. Capital Allocation by Market Regime

| Regime | VIX | Position Size | Cash Reserve | Action |
|--------|-----|:---:|:---:|--------|
| BULLISH | < 12 | 80% | ≥ 15% | Full size, normal delta |
| NEUTRAL | 15-20 | 75% | ≥ 20% | Normal entries, standard delta |
| CAUTIOUS | 20-25 | 50% | ≥ 25% | Reduced size, tighter delta |
| VOLATILE | 25-30 | 25% | ≥ 30% | Minimal new entries, tighter stops |
| BEARISH | > 30 | 0% | ≥ 35% | NO new positions, manage existing only |

### 2. CC-to-CSP Ratio by Market Regime

| Regime | CSP (new entries) | CC (existing shares) | Rationale |
|--------|:---:|:---:|-----------|
| BULLISH | 60-70% | 30-40% | Add exposure, ride upside |
| NEUTRAL | 50% | 50% | Balanced wheel rotation |
| CAUTIOUS | 30% | 70% | Reduce new risk, extract premium from holdings |
| VOLATILE | 10% or 0 | 90%+ | Work existing positions only |
| BEARISH | **0%** | 100% (no new) | Preserve capital, no new assignments |

### 3. Position Sizing Hard Limits

| Rule | Limit | Type |
|------|-------|------|
| Single stock position | ≤ 15% of net liquidation | 🔴 BLOCK |
| Sector concentration | ≤ 25% of portfolio | 🟡 WARN |
| CSP capital deployed | ≤ 25% of net liq (normal), ≤ 10% (volatile) | 🔴 BLOCK |
| Cash reserve minimum | See regime table above | 🔴 BLOCK |
| Open positions (stocks + options) | ≤ 8 total | 🟡 WARN |
| New positions per day | ≤ 2 | 🟡 WARN |

### 4. Delta Adjustments by Regime

| Regime | CSP Delta | CC Delta | DTE Range |
|--------|:---:|:---:|:---:|
| BULLISH | 0.20-0.30 | 0.20-0.30 | 30-45 |
| NEUTRAL | 0.20-0.30 | 0.20-0.30 | 30-45 |
| CAUTIOUS | 0.15-0.25 | 0.25-0.35 (closer to money) | 30-45 |
| VOLATILE | 0.10-0.20 | 0.30-0.40 (aggressive) | 45-60 |
| BEARISH | NONE | 0.25-0.35 (existing only) | N/A |

### 5. CSP Pause Triggers

Stop opening new CSPs immediately if ANY of these are true:
- VIX > 25
- SPY below 200 SMA
- Regime score ≤ -2 (VOLATILE or worse)
- Cash reserve < 20% of net liq
- Single stock drop > 15% from cost basis (for that ticker only)

### 6. CC Management on Existing Positions

- **Never sell CC below cost basis** — locks in loss
- Profit ≥ 50% → close immediately, redeploy capital
- DTE ≤ 21 and underwater → consider rolling for net credit
- DTE ≤ 7 and ITM (Δ > 0.50) → prepare for assignment
- Stock > 25% below cost basis → pause CCs, hold shares unencumbered until recovery

### 7. Watchlist Diversification

Target: minimum 3 sectors, no sector > 25%. Non-tech candidates for diversification:

| Sector | Tickers |
|--------|---------|
| Financial | JPM, BAC, GS |
| Healthcare | ABBV, JNJ, UNH |
| Consumer Defensive | KO, WMT, PG |
| Energy | XOM, CVX |
| Industrial | CAT, GE, HON |

### 8. Pre-Trade Checklist

Before any new trade, verify ALL of these:
- [ ] Regime allows new positions (not BEARISH/VOLATILE)
- [ ] Cash reserve ≥ regime minimum
- [ ] CSP capital deployed ≤ regime limit
- [ ] Single position ≤ 15% of net liq
- [ ] Sector concentration ≤ 25%
- [ ] No earnings within DTE window (14 days)
- [ ] IV Rank ≥ 30
- [ ] Delta within regime range
- [ ] Bid-ask spread < 5%
- [ ] OI ≥ 500, volume ≥ 10
- [ ] RoC ≥ 12% (CSP) or ≥ 8% (CC)
- [ ] Would I be happy owning this stock for 1 year if assigned?
- [ ] ALWAYS consider my Cash + Stock values > 70% of my CSP liability
 
### 9. My reasoning behind few posistons - Do consider this while recommendation
- [] Goal is to sell Visa stock and diversify into other good fundamental equities which can give good premium earning options
- [] Goal is to aquired AMD and AVGO stocks as those are fundamental in Semiconductor industry for AI