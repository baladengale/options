---
name: oie-paper
description: >-
  OIE Paper Trading Engine — manage the simulated options portfolio. Seed from
  real account, run cycles, check status, view history, or reset. Use when the
  user asks about the paper engine, paper trading, simulation, or OIE status.
---

Manage the OIE paper trading engine (`scripts/oie_engine.py`). All trades are simulated — nothing touches the real moomoo account.

## Commands

| User Asks | Command |
|-----------|---------|
| "Paper engine status" / "How's the simulation?" | `python3 scripts/oie_engine.py status` |
| "Start fresh paper portfolio" | `python3 scripts/oie_engine.py reset --force && python3 scripts/oie_engine.py init` |
| "Run one paper cycle" | `python3 scripts/oie_engine.py once --force` |
| "Dry-run a cycle (no changes)" | `python3 scripts/oie_engine.py once --dry-run --force` |
| "Paper P&L history" | `python3 scripts/oie_engine.py history` |
| "Start continuous mode" | `python3 scripts/oie_engine.py run --interval 30` |
| "Compare paper vs real" | Run `portfolio.py --health` + `oie_engine.py status` side by side |

## Key Facts
- DB: `db/oie_paper.db` (separate from real portfolio)
- Paper cash = us_cash + fund assets (combined into one positive pool)
- Imports from `src/` directly — shares filters, scoring, and models with the live screener
- Same contract gates (`src/filters/contract_filters.py`) as the live screener
- Same scoring (`src/scoring/screener_score.py`) + guardrails as the live screener
- Exit triggers: 70% profit → close, 50% profit → close, DTE ≤ 0 → expire/assign
- Stop-loss: delta gates (CSP Δ≥0.60, CC Δ≥0.50) + premium-multiple stops + $1,000 absolute catch-all

## Self-Test (no OpenD needed)
```bash
python3 scripts/oie_engine.py test
```
