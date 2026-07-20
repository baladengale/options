---
name: daily-digest
description: >-
  Daily trading digest — market sentiment, portfolio health, and top trade recommendations
  in one shot. Use when user asks for overall market update, portfolio summary,
  P&L, or "what's next" actions. Combines market_sentiment.py, portfolio.py, and
  screener.py into a comprehensive briefing.
---

# Daily Trading Digest

Run these three scripts in order to give the user a complete picture.

## Step 1: Market Sentiment (Macro Context)
```bash
python3 scripts/market_sentiment.py
```
This gives:
- VIX level and regime (BULLISH/NEUTRAL/CAUTIOUS/VOLATILE/BEARISH)
- Position sizing guidance
- Yield curve (10Y-2Y)
- Fear & Greed Index
- Credit spreads stress level

## Step 2: Portfolio Health (Your Positions)
```bash
python3 scripts/portfolio.py
```
This gives:
- Account funds (cash, buying power, net liq)
- Stock positions with P&L
- Option positions with DTE, P&L, decisions
- All-time income (premium collected vs buybacks)
- Sector breakdown (concentration check)
- Stock/Option decisions with scores
- Overlap analysis (put/call stacking)
- Guardrails (position limits, cash buffer)

## Step 3: Top Trade Candidates (Next Actions)
```bash
python3 scripts/screener.py --top 5
```
This gives:
- Top 5 CC/CSP candidates ranked by score
- Specific strikes, expiries, deltas
- RoC (return on capital)
- Why each candidate qualifies

## Format the Output

Present in this order:

### 1. Market Snapshot
```
🌍 MACRO
  VIX: X.X | Regime: XXXX | Size: XX%
  10Y: X.X% | Fear & Greed: XX (Xxxx)
```

### 2. Portfolio Snapshot
```
💰 ACCOUNT
  Cash: $XX,XXX | Net Liq: $XXX,XXX
  CSP Liability: $XX,XXX | Positions: XX
```

### 3. Top Actions (from screener)
Present as actionable items:
- "💡 CSP AAPL $180 — 24% RoC, DTE 41, Δ 0.24"
- "💡 CC NVDA $220 — 18% RoC, DTE 36, Δ 0.32"

### 4. Risk Alerts
- Concentration warnings (>15% single position, >25% sector)
- CSP pause triggers (VIX > 25, cash < 20%)
- Earnings blackout
- Expiry warnings (<7 DTE)

## Architecture Note

All scripts poll live from moomoo OpenD. No stale data. If OpenD fails, abort and tell user to check connection.
