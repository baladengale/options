---
name: oie
description: >-
  Options Income Engine — run portfolio health check, screener, or quick
  ticker/macro lookup. Gives specific CC/CSP trade recommendations grounded in
  actual positions, cash, and config. Use when the user asks what to trade, how
  their portfolio looks, to check a specific stock, or for any options
  recommendation.
---

Run the OIE engine in this exact order. Never skip steps or give generic advice.

## Step 1: Load Portfolio State
```bash
python3 scripts/portfolio.py --fast
```
If OpenD isn't running or returns errors → abort. Never use stale data.

## Step 2: Run the Relevant Analysis

| User Asks | Command |
|-----------|---------|
| "What should I trade?" / "Any recommendations?" | `python3 scripts/screener.py --top 10` |
| "How are my positions?" / "Portfolio health?" | `python3 scripts/portfolio.py --health` |
| "What's my P&L?" / "Show me everything" | `python3 scripts/portfolio.py` |
| "Check on my options" | `python3 scripts/portfolio.py --fast` |
| "Check AAPL" / "Deep dive NVDA" / "What's V doing?" | `python3 scripts/market_data.py TICKER --options` |
| "What's the macro?" / "Market outlook?" | `python3 scripts/market_sentiment.py` |
| "Sentiment on V" / "Analyst ratings MSFT" | `python3 scripts/market_sentiment.py TICKER` |
| "News on NVDA" | `python3 scripts/market_sentiment.py TICKER --news` |

## Step 3: Apply Rules (from GOAL.md + config/rules.yaml)
- Check regime → position sizing. Is CSP allowed right now?
- Check concentration → any position > 15%? Any sector > 25%?
- Check CSP pause triggers → VIX > 25? Cash < 20%?
- Every recommendation must reference the specific rule that allows or blocks it.

## Step 4: Supplement with WebSearch (only if needed)
Gather current news, analyst actions, and sector context. Adds narrative depth after the deterministic engine runs. Never use web search INSTEAD of the local engine.

## Step 5: Format the Answer
1. Portfolio snapshot (cash, positions, CSP liability)
2. Regime check (VIX, position size allowed, CSP pause status)
3. Specific recommendations with rule references
4. Risk alerts (concentration, margin, earnings, expiry)

## Architecture Note
All business logic lives in `src/` (filters, scoring, data, risk, analysis). Scripts in `scripts/` are thin wrappers — they call `src/` modules, never reimplement logic. Every threshold is in `config/rules.yaml`. See `specs/architecture-spec.md` for full reference.

## Hard Constraints
- Covered Calls only: must own 100 shares per contract.
- Cash Secured Puts only: must hold enough cash to buy 100 shares at strike.
- No margin, no naked options, no spreads.
- Every trade must pass the collar check.
