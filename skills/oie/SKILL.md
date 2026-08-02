---
name: oie
description: >-
  Interactive Options Income Engine — run an ad-hoc portfolio check, screener,
  single-ticker deep dive, or macro read in this session. Gives specific CC/CSP
  trade recommendations grounded in actual positions, cash, and config. Use for
  on-the-spot questions: "what should I trade?", "how does my portfolio look?",
  "check AAPL", "what's the macro?". For the scheduled morning/evening HTML
  email digest, use the `oie-daily-digest` skill instead.
---

Run the OIE engine in this exact order. Never skip steps, never give generic advice, never use stale data.

## Step 1 — Load Portfolio State
```bash
python3 scripts/portfolio.py --fast
```
If OpenD isn't running on `127.0.0.1:11111` or returns errors → **abort**. Tell the user to start OpenD and retry. Never recommend from memory or yesterday's numbers.

## Step 2 — Run the Relevant Analysis

| User asks | Command |
|-----------|---------|
| "What should I trade?" / "Any recommendations?" | `python3 scripts/screener.py --top 10` |
| "How are my positions?" / "Portfolio health?" | `python3 scripts/portfolio.py --health` |
| "What's my P&L?" / "Show me everything" | `python3 scripts/portfolio.py` |
| "Just my cash / positions" | `python3 scripts/portfolio.py --fast` |
| "Check my thesis" | `python3 scripts/portfolio.py --thesis` |
| "Check AAPL" / "Deep dive NVDA" / "What's V doing?" | `python3 scripts/market_data.py TICKER --options` |
| "What's the macro?" / "Market outlook?" | `python3 scripts/market_sentiment.py` |
| "Sentiment on V" / "Analyst ratings MSFT" | `python3 scripts/market_sentiment.py TICKER` |
| "News on NVDA" | `python3 scripts/market_sentiment.py TICKER --news` |

Ticker normalization is handled by the scripts (`V` → `US.V`). For a single-ticker screen deep-dive use `scripts/screener.py --validate TICKER`.

## Step 3 — Apply Rules (from GOAL.md + config/rules.yaml)

Every recommendation must reference the specific rule that allows or blocks it:

- **Regime** (VIX → BULLISH/NEUTRAL/CAUTIOUS/VOLATILE/BEARISH) → position-size multiplier + CC/CSP ratio.
- **CSP pause** → VIX > 25, SPY < 200 SMA, regime ≤ −2, cash reserve < 20%, or stock > 15% below basis for that ticker.
- **Concentration** → any single position > 15% of net liq? Any sector > 25%?
- **Contract gates** → delta in regime range, DTE 7–90 (sweet spot 30–45), IV rank ≥ 30, bid-ask spread < 5%, OI ≥ 500, RoC ≥ 12% (CSP) / 8% (CC), no earnings in the 14-day blackout.
- **Pre-trade checklist** → `GOAL.md §8`. Always confirm cash + stock value > 70% of CSP liability.

If a gate fails, say so explicitly and do not soften the block.

## Step 4 — Supplement with WebSearch (only if needed)

Use WebSearch *after* the deterministic engine runs — for current news, analyst actions, or sector narrative that adds depth. Never use web search **instead of** the local engine, and never let a web headline override a config gate.

## Step 5 — Format the Answer

1. **Portfolio snapshot** — cash, buying power, net liquidation, CSP liability
2. **Regime check** — VIX, regime, position size allowed, CSP-pause status
3. **Recommendations** — specific strike / expiry / delta / RoC, each tied to the rule that permits it
4. **Risk alerts** — concentration, margin, earnings within blackout, expiry risk, thesis damage, Do-Not-Wheel flags

---

## Architecture Note

All business logic lives in `src/` (filters, scoring, data, risk, analysis, guardrails). Scripts in `scripts/` are thin wrappers — they call `src/` modules, never reimplement logic. Every threshold is in `config/rules.yaml`. See `specs/architecture-spec.md` for the full reference and `README.md` for the project map.

## Hand-off

- **Scheduled HTML digest + email** (morning/evening report) → `oie-daily-digest` skill (`skills/oie-daily-digest/scripts/daily_digest.py`).
- **Paper-trading engine** (`init`, `once`, `run`, `status`, `history`, `sim`) → `scripts/oie_engine.py` directly. The OIE paper cycle always runs `--dry-run` in the digest; for live paper cycles use `oie_engine.py once`.

## Hard Constraints

- Covered Calls only — must own 100 shares per contract.
- Cash-Secured Puts only — must hold cash to buy 100 shares at the strike.
- No margin, no naked options, no spreads.
- Never sell a CC below cost basis; every trade must pass the collar check.
- **No script submits real orders** — recommendations only; the user executes manually.
