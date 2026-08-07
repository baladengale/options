---
name: oie
description: >-
  Options Income Engine — the single OIE skill. Two modes: (1) interactive — run
  an ad-hoc portfolio check, screener, single-ticker deep dive, or macro read;
  (2) daily digest — chain the full engine into a rich HTML report and email it.
  Both give specific CC/CSP trade recommendations grounded in actual positions,
  cash, and config. Use for "what should I trade?", "how does my portfolio
  look?", "check AAPL", "what's the macro?", "morning/evening digest".
---

The GenAI writes only the narrative digest abstract — it never computes scores, signals, or trade decisions (all deterministic from `config/rules.yaml`). Never give generic advice, never use stale data.

## Mode A — Interactive (ad-hoc questions)

Run in this exact order.

### Step 1 — Load Portfolio State
```bash
python3 scripts/portfolio.py --fast
```
If OpenD isn't running on `127.0.0.1:11111` or returns errors → **abort**. Tell the user to start OpenD and retry. Never recommend from memory or yesterday's numbers.

### Step 2 — Run the Relevant Analysis

| User asks | Command |
|-----------|---------|
| "What should I trade?" / "Any recommendations?" | `python3 scripts/screener.py --top 10` |
| "How are my positions?" / "Portfolio health?" | `python3 scripts/portfolio.py --health` |
| "What's my P&L?" / "Show me everything" | `python3 scripts/portfolio.py` |
| "Just my cash / positions" | `python3 scripts/portfolio.py --fast` |
| "Check my thesis" | `python3 scripts/portfolio.py --thesis` |
| "Check AAPL" / "Deep dive NVDA" | `python3 scripts/market_data.py TICKER --options` |
| "What's the macro?" / "Market outlook?" | `python3 scripts/market_sentiment.py` |
| "Sentiment on V" / "Analyst ratings MSFT" | `python3 scripts/market_sentiment.py TICKER` |
| "News on NVDA" | `python3 scripts/market_sentiment.py TICKER --news` |
| Paper engine status / P&L / history | `python3 scripts/oie_engine.py status\|history` |

Ticker normalization is handled by the scripts (`V` → `US.V`).

### Step 3 — Apply Rules (from GOAL.md + config/rules.yaml)

Every recommendation must reference the specific rule that allows or blocks it:

- **Regime** (VIX → BULLISH/NEUTRAL/CAUTIOUS/VOLATILE/BEARISH) → position-size multiplier + CC/CSP ratio.
- **CSP pause** → VIX > 25, SPY < 200 SMA, regime ≤ −2, cash reserve < 20%, or stock > 15% below basis for that ticker.
- **Concentration** → any single position > 15% of net liq? Any sector > 25%?
- **Contract gates** → delta in regime range, DTE 7–90 (sweet spot 30–45), IV rank ≥ 30, bid-ask spread < 5%, OI ≥ 500, RoC ≥ 12% (CSP) / 8% (CC), no earnings in the 14-day blackout.
- **Pre-trade checklist** → `GOAL.md §8`. Always confirm cash + stock value > 70% of CSP liability.

If a gate fails, say so explicitly and do not soften the block.

### Step 4 — Supplement with WebSearch (only if needed)

Use WebSearch *after* the deterministic engine runs — for current news, analyst actions, or sector narrative. Never use it **instead of** the local engine, and never let a web headline override a config gate.

### Step 5 — Format the Answer

1. **Portfolio snapshot** — cash, buying power, net liquidation, CSP liability
2. **Regime check** — VIX, regime, position size allowed, CSP-pause status
3. **Recommendations** — specific strike / expiry / delta / RoC, each tied to the rule that permits it
4. **Risk alerts** — concentration, margin, earnings within blackout, expiry risk, thesis damage, Do-Not-Wheel flags

---

## Mode B — Daily Digest (scheduled HTML + email)

Chains **portfolio → market_sentiment → market_data → screener → OIE paper cycle** into one rich HTML report with a 5–10 bullet Daily Decision Abstract. The OIE step always runs `--dry-run` — **paper only, never real orders**.

```bash
python3 skills/oie/scripts/daily_digest.py --morning                  # 07:00 pre-market digest
python3 skills/oie/scripts/daily_digest.py --evening                  # 19:00 post-market digest
python3 skills/oie/scripts/daily_digest.py --skip-screener --skip-oie # fast mode
```

**⚠️ Single-email workflow (no duplicates):**
1. Run the digest **without** `--send` → writes `logs/digest-<ts>.html` + `.json`. The run never emails.
2. GenAI reads the facts JSON + section text, then replaces the `<div id="abstract">` bullets in the HTML with a 5–10 bullet Daily Decision Abstract (portfolio status, regime/CSP eligibility, thesis issues, top screens, paper-engine next moves). Deterministic sections stay untouched.
3. Send the **edited** file exactly once:
   ```bash
   python3 skills/oie/scripts/daily_digest.py --send --html logs/digest-<ts>.html
   ```
   Needs `config/email.yaml` (copy `config/email.yaml.example`). `--send` without `--html` is rejected — the digest run itself never emails. Cron ideas: `0 7 * * 1-5` / `0 19 * * 1-5`.

---

## Architecture Note

All business logic lives in `src/` (filters, scoring, data, risk, analysis, guardrails). Scripts in `scripts/` are thin wrappers — they call `src/` modules, never reimplement logic. Every threshold is in `config/rules.yaml`. See `specs/architecture-spec.md` and `README.md`.

## Hard Constraints

- Covered Calls only — must own 100 shares per contract.
- Cash-Secured Puts only — must hold cash to buy 100 shares at the strike.
- No margin, no naked options, no spreads.
- Never sell a CC below cost basis; every trade must pass the collar check.
- **No script submits real orders** — recommendations only; the user executes manually. Paper trading stays in `db/oie_paper.db` only.

## Three-Tool Split

- **`oie` (`scripts/oie_engine.py`)** — the autonomous **paper** engine. Full control of the paper book: opens AND manages both CSPs and CCs. The paper share count is the source of truth for CC eligibility, so the wheel rotates end-to-end on paper. CCs that go deep ITM (Δ≥0.60) are rolled up-and-out for credit; CSPs cut at |Δ|≥0.60. Run `oie reconcile` to re-sync paper STOCK rows + cash to the real account after manual trades (non-destructive; preserves options & history).
- **`portfolio` (`scripts/portfolio.py`)** — **live** account health: P&L, orders, thesis, funds, guardrails. Read-only.
- **`screener` (`scripts/screener.py`)** — watchlist **opportunities**: ranked CC/CSP/PS candidates with the same scoring + guardrails as the engine.

