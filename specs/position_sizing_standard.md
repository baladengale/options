# Position Sizing & Capital Allocation Standard

**Version**: 1.0
**Date**: 2026-07-10
**Applies to**: All trade decisions — screener, portfolio_check, daily_run

---

## 1. Core Principles

1. **Capital preservation is priority #1.** Premium collection is secondary. A single blown-up position can wipe out months of theta income.
2. **Diversification is the only free lunch.** No single stock, sector, or strategy should dominate the portfolio.
3. **Cash is a position.** Unallocated cash = dry powder for assignment + opportunity when IV spikes.
4. **Worst-case first.** Model every trade as if ALL CSPs will be assigned simultaneously in a 15% correction.
5. **Process over outcome.** A well-sized losing trade is better than an oversized winning trade. The latter breeds overconfidence.

---

## 2. Hard Limits (BLOCK — no new trades if violated)

| # | Rule | Limit | Rationale |
|---|------|-------|-----------|
| G1 | **Single ticker concentration** | ≤ 15% of net liq | V at 68% = one bad week wipes portfolio. Pro standard: 20%. Conservative: 15% |
| G2 | **Cash buffer critical** | ≥ 10% of net liq | Below 10% = margin call risk in a market correction. Cannot cover assignments |
| G3 | **CSP worst-case coverage** | 100% coverable | All CSPs assigned simultaneously = must have funds without forced liquidation |
| G4 | **No earnings in DTE** | Blackout 14 days | Binary gap risk. Already in SPECS constraint C5 |
| G5 | **DTE < 7** | Blocked | Gamma explosion zone. Not worth the risk for wheel strategy |

## 3. Soft Limits (WARN — proceed with caution)

| # | Rule | Limit | Rationale |
|---|------|-------|-----------|
| G6 | **Cash buffer recommended** | ≥ 25% of net liq | Dry powder. Pro standard: 25%. Aggressive: 10% |
| G7 | **Open positions max** | ≤ 8 | Management bandwidth. Beyond 8 = missed rolls, surprise assignments |
| G8 | **Sector concentration** | ≤ 25% per sector | Tech at 90% = correlation risk. 2022 tech correction: -33% |
| G9 | **Daily new orders** | ≤ 2 | Overtrading prevention. Each entry should be deliberate |
| G10 | **Margin utilization** | ≤ 30% | Emergency only. Must clear within 15 days |

## 4. Position Sizing Calculation

### Net Liquidation Value
```
Net Liq = Σ(stock_qty × current_price) + cash + fund_assets
```

### Per-Position Maximum
```
Max Per Position = Net Liq × 0.15
```

For CSP: `strike × 100 × contracts ≤ Max Per Position`
For CC: shares already owned, no additional capital. CC strike must be above cost basis.

### Cash Buffer
```
Cash Buffer % = (unallocated_cash + cash_equivalent_fund) / Net Liq × 100
```

Where `unallocated_cash` excludes cash tied up in CSP assignment coverage.

### Worst-Case Assignment
```
CSP Liability = Σ(strike × 100 × contracts) for all open short puts
Available = cash + BP × 0.50 (margin buffer)
Shortfall = CSP Liability − Available
```

If Shortfall > 0 → 🔴 BLOCK all new CSP positions until resolved.

## 5. Daily Trading Limits

| Limit | Value | Notes |
|-------|-------|-------|
| Max new positions per day | 2 | Prevents overtrading. Roll/adjust existing = unlimited |
| Max positions total | 8 | Beyond this, attention fragments |
| Max per ticker across strategies | 15% | Includes stock + all options on same underlying |

## 6. Guardrail Integration Points

### Screener (`screener.py`)
- Before output: check all hard limits against current portfolio + proposed trade
- Hard violations → exclude candidate from results, log reason
- Soft violations → include candidate but flag with warning icon

### Daily Run (`daily_run.py`)
- After portfolio snapshot: run all guardrails
- Print guardrail report with 🔴 BLOCKED and 🟡 WARNINGS
- Track daily order count from trade_log (count RECOMMENDED entries today)
- Stress test: model all CSPs assigned simultaneously

### Portfolio Check (`portfolio_check.py`)
- Flag positions exceeding 15% concentration
- Flag positions approaching 30% drawdown from cost basis

## 7. Recovery Rules (When Limits Are Breached)

| Breach | Recovery Action |
|--------|----------------|
| Cash < 10% | Close all positions at 30%+ profit immediately. No new entries until ≥ 25% cash |
| Single ticker > 15% | Aggressively sell CCs on that ticker. Let shares get called away |
| > 8 positions | Let expiries close naturally. Do not roll underwater positions — close them |
| CSP shortfall > 0 | Close CSPs closest to expiry first. Reduce CSP count until fully covered |
| Margin > 30% | 15-day hard deadline. Sell stocks or close options to clear |

## 8. Position Sizing by Account Size

| Net Liq | Max Per Position (15%) | Max Positions | Recommended Contracts |
|---------|----------------------|---------------|----------------------|
| $50K | $7,500 | 4-5 | 1 contract |
| $100K | $15,000 | 5-6 | 1-2 contracts |
| $200K | $30,000 | 6-8 | 2-4 contracts |
| $500K | $75,000 | 8-10 | 4-8 contracts |

For the user's current portfolio (~$221K net liq, 68% in V):
- **Max per new position**: $33,150
- **Target**: reduce V to ≤$33K (sell 96 shares or let CCs get assigned)
- **Cash target**: $55K cash buffer (25%)

## 9. Sources

1. TastyTrade — 200K+ backtested trades, 45 DTE short premium framework
2. ApexVol — Wheel Strategy capital allocation, drawdown limits
3. Option Alpha — Systematic execution, position sizing
4. Schwab — Concentration risk, sector diversification for options sellers
5. Bull Strangle Strategy — 25% cash buffer rule, 75% max allocation
