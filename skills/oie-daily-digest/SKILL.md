---
name: oie-daily-digest
description: >-
  OIE Daily Digest — unified Options Income Engine skill (consolidates oie +
  oie-paper). Runs the full daily workflow (portfolio → market sentiment →
  market data → screener → OIE paper cycle), generates a rich HTML digest with
  a 5–10 bullet Daily Decision Abstract, and emails it at 07:00 and 19:00.
  Use for "daily digest", "what should I trade", "how's my portfolio",
  "morning/evening report", or any OIE / paper-engine question.
---

# OIE — Options Income Engine (Unified)

Consolidates `oie` + `oie-paper`. The GenAI writes only the narrative abstract
— it never computes scores, signals, or trade decisions (deterministic from
`config/rules.yaml`).

## Script Location (flexible)

`daily_digest.py` is embedded at `skills/oie-daily-digest/scripts/` and also
lives at `scripts/`. Both resolve the repo root dynamically: `OIE_REPO` env var
wins, else walk up to the folder with `config/rules.yaml`.

## Daily Digest

```bash
python3 skills/oie-daily-digest/scripts/daily_digest.py --morning  # 07:00
python3 skills/oie-daily-digest/scripts/daily_digest.py --evening  # 19:00
python3 skills/oie-daily-digest/scripts/daily_digest.py --send    # + email (needs config/email.yaml)
```

Chains: portfolio → market_sentiment → market_data → screener → OIE paper cycle.

## Commands

| User Asks | Command |
|-----------|---------|
| Daily digest / morning / evening | `python3 skills/oie-daily-digest/scripts/daily_digest.py --morning\|--evening` |
| What should I trade? | `python3 scripts/screener.py --top 10` |
| Portfolio health? | `python3 scripts/portfolio.py --health` |
| What's my P&L? Full portfolio | `python3 scripts/portfolio.py` |
| Deep dive a ticker | `python3 scripts/market_data.py TICKER --options` |
| Macro / outlook | `python3 scripts/market_sentiment.py` |
| Sentiment on a ticker | `python3 scripts/market_sentiment.py TICKER` |
| News on a ticker | `python3 scripts/market_sentiment.py TICKER --news` |
| Paper engine status | `python3 scripts/oie_engine.py status` |
| Run one paper cycle | `python3 scripts/oie_engine.py once --force` |
| Paper dry-run | `python3 scripts/oie_engine.py once --dry-run --force` |
| Paper P&L history | `python3 scripts/oie_engine.py history` |
| Continuous paper mode | `python3 scripts/oie_engine.py run --interval 30` |

## GenAI Abstract Step

After `daily_digest.py` writes `logs/`:
1. HTML `logs/digest-<ts>.html`; facts `logs/digest-<ts>.json`.
2. Read the facts JSON + tool sections → build a **5–10 bullet Daily Decision
   Abstract**: portfolio status, regime/CSP eligibility, thesis issues, top
   screens, paper-engine next moves.
3. Replace the `<div id="abstract">` bullets in HTML with the GenAI bullets
   (deterministic sections untouched), then email or re-open.

Email sending is opt-in (`--send`); without it the digest only writes HTML+JSON.

## Hard Constraints (rules.yaml / GOAL.md)

- CC: own 100 shares per contract. CSP: full cash to buy 100 shares at strike.
- No margin, no naked options, no spreads. Every trade passes collar check.
- Paper engine is simulated only — never touches the real account.
