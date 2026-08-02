# Options Wheel Strategy — Deterministic Trading Engine

Covered Call (CC) & Cash-Secured Put (CSP) screening, scoring, and portfolio management with a systematic review cadence.

**Hard constraints**: no naked options, no margin trading, no spreads. CC needs 100 owned shares; CSP needs full cash coverage. **No script submits orders** — all trades are executed manually.

---

## Quick Start

```bash
source .venv/bin/activate          # first time: python3 -m venv .venv && pip install -r requirements.txt

# ── Daily Workflow ──
python3 scripts/portfolio.py                 # 1. Full portfolio state + P&L + health + thesis
python3 scripts/screener.py --top 10         # 2. Best CC/CSP candidates across the watchlist

# ── Focused Views ──
python3 scripts/portfolio.py --fast          # Funds + P&L only
python3 scripts/portfolio.py --health        # Decisions + overlap + guardrails
python3 scripts/portfolio.py --thesis        # Thesis validation only
python3 scripts/portfolio.py --pnl           # Positions + income only
python3 scripts/portfolio.py --orders        # Order history (90d)

# ── Paper Trading (validate before live) ──
python3 scripts/oie_engine.py reset --force  # Wipe paper portfolio
python3 scripts/oie_engine.py init           # Seed from REAL holdings
python3 scripts/oie_engine.py once --force   # Run one paper cycle
python3 scripts/oie_engine.py status         # Paper positions + P&L
```

**Prerequisites**: moomoo OpenD on `127.0.0.1:11111`. Python 3.9+. The REAL portfolio is read live from moomoo each run — no local DB. The only database is `db/oie_paper.db` for paper trading.

### OIE Daily Digest — Full Engine in One HTML Report

Chains **portfolio → market_sentiment → market_data → screener → OIE paper cycle** into a single rich HTML file with a 5–10 bullet Daily Decision Abstract, ready to be emailed.

```bash
python3 skills/oie-daily-digest/scripts/daily_digest.py --morning                  # 07:00 pre-market digest
python3 skills/oie-daily-digest/scripts/daily_digest.py --evening                  # 19:00 post-market digest
python3 skills/oie-daily-digest/scripts/daily_digest.py --send                     # also email (needs config/email.yaml)
python3 skills/oie-daily-digest/scripts/daily_digest.py --skip-screener --skip-oie # fast mode
```

- Output: `logs/digest-<timestamp>.html` (rich HTML) + `logs/digest-<timestamp>.json` (facts for the GenAI abstract)
- OIE step always runs `--dry-run` — **paper only, never real orders**
- Check `config/email.yaml.example` for Gmail SMTP setup; `--send` delivers the HTML email
- Cron ideas: `0 7 * * 1-5` and `0 19 * * 1-5` for morning/evening digests (see skill `skills/oie-daily-digest/`)
- The script resolves the repo root dynamically (`OIE_REPO` env override or walk-up to `config/rules.yaml`)

---

## Scripts

### `portfolio.py` — Real Account Dashboard

Full sweep: **funds → P&L → health/guardrails → timeline → thesis → recommendations**.

| Flag | Shows |
|------|-------|
| (bare) | Full sweep |
| `--fast` | Funds + P&L only (no scoring) |
| `--pnl` | Stock/option positions, all-time income, monthly income, sector breakdown |
| `--health` | Score + CC hunt + exit framework + option decisions + overlap + guardrails |
| `--thesis` | Thesis validation on stocks + option underlyings + watchlist |
| `--schedule` | Systematic review timeline |
| `--orders [TICK]` | Filled order history (last 90 days) |
| `--no-external` | Skip yfinance (offline; skips thesis deep-checks) |

**Funds legend** (validated against moomoo upstream):
- **Buying Power** = margin-inclusive `power` converted to USD (matches moomoo app)
- **Cash Buying Power** = cash-only `usd_net_cash_power` (already USD — never ÷7.8)
- **Fund Assets / Total Assets** = HKD→USD converted using the **live USD/HKD FX rate** (yfinance, cached), falling back to 7.8 offline — so figures match the moomoo app display

**Thesis validation scope** = holdings + option underlyings + the **live moomoo watchlist group** (the master list — not the static config fallback, so stale names like BE/CRM don't appear). Output shows each ticker's status (`THESIS_INTACT` / `TECHNICAL_DAMAGE` / `THESIS_BROKEN`), an `[eligible]` tag (loss-maker check), and an inline **`🚫 DO-NOT-WHEEL until YYYY-MM-DD`** flag when the ticker is in `config/do_not_wheel.yaml`.

### `screener.py` — Watchlist Screener

Scores every watchlist ticker 1–10 (**lower = better**) across 5 weighted dimensions, then ranks best CC/CSP contracts.

```bash
python3 scripts/screener.py --top 5           # Top 5 trades
python3 scripts/screener.py --cc-only         # Covered calls only
python3 scripts/screener.py --csp-only        # Cash-secured puts only
python3 scripts/screener.py --validate AMD    # Single ticker deep-dive
python3 scripts/screener.py --no-external     # Offline (skip yfinance)
```

| Dimension | Weight | Measures |
|-----------|--------|----------|
| Technical | 25% | RSI, SMA stack, ADX, volume ratio |
| Options Quality | 25% | Bid-ask spread, IV rank, market cap, beta |
| Fundamental | 15% | P/E, dividend yield, earnings consistency |
| External Sentiment | 20% | Analyst consensus, earnings blackout, news |
| Macro/Risk | 15% | VIX regime, yield curve, credit spreads |

Contract filters (liquidity, delta, IV, VRP gate, RoC minimums) come from `config/rules.yaml`. Loss-makers (`net_profit<0 AND eps_ttm<0`, negative P/E) are auto-skipped via `is_wheel_eligible()`.

### `oie_engine.py` — Paper Trading Engine

Autonomous paper portfolio that mirrors the strategy using the same scoring + guardrails as the real pipeline. **Never touches real money.**

| Command | Description |
|---------|-------------|
| `init` | Seed paper portfolio from REAL stock holdings + cash (options NOT copied) |
| `once` | Single cycle: mark-to-market → check exits → screen → guardrails → paper trades → snapshot |
| `once --dry-run` | Same but writes NOTHING to DB — preview what WOULD happen |
| `run [--interval N]` | Continuous loop (default 30 min) with Ctrl+C graceful stop |
| `status` | Paper positions, cash, P&L |
| `history` | Portfolio value snapshots over time |
| `reset --force` | Wipe all paper data |
| `test` | Self-check (no OpenD needed) |

**Automatic mode** (recommended):
```bash
# tmux / continuous
tmux new -s oie
python3 scripts/oie_engine.py run --interval 30 --skip-closed

# OR cron (runs during US market hours, your local timezone)
*/30 21-7 * * 1-5 cd ~/options && python3 scripts/oie_engine.py once --skip-closed >> logs/oie.log 2>&1
```

**Manual mode** (ad-hoc review):
```bash
python3 scripts/oie_engine.py once --dry-run   # What would the engine do?
python3 scripts/oie_engine.py once             # Execute one real paper cycle
python3 scripts/oie_engine.py status           # Check results
```

**Cycle phases** (each `once`/`run` iteration): load state → mark-to-market → check exits (≥50% profit → close, DTE≤0 → expire/assign) → screen new opportunities → apply guardrails (15% concentration, cash buffer, max 8 positions, 2 trades/day) → execute paper trades → snapshot.

### `market_data.py` — Single Ticker Deep Dive

```bash
python3 scripts/market_data.py V                  # Stock only
python3 scripts/market_data.py V --options        # + option chain (30–45 DTE)
python3 scripts/market_data.py NVDA --options --all  # Full chain + GEX, PCR, skew
```

### `market_sentiment.py` — Macro + Sentiment

```bash
python3 scripts/market_sentiment.py               # Macro only
python3 scripts/market_sentiment.py AAPL          # Macro + AAPL
python3 scripts/market_sentiment.py --watchlist   # All watchlist tickers
```

---

## Do-Not-Wheel — Manual Override Only

`config/do_not_wheel.yaml` is a **hand-edited force-skip list**. The engine never writes to it automatically. Expired entries are ignored.

Add a ticker here to block it in both the screener (skipped before scoring) and portfolio (shown inline as `🚫 DO-NOT-WHEEL until DATE` with the reason):

```yaml
# config/do_not_wheel.yaml
BE:
  added_date: '2026-08-02'
  expiration_date: '2027-02-02'
  months: 6
  reason: Negative P/E ratio (-556.2) - company losing money
```

**Trusted high-conviction names** (AMD, PLTR, TSLA) are *not* listed here — they live in `config/rules.yaml → thesis_validation.trusted_tickers`, which exempts them from the high-P/E valuation check (negative P/E still flags — that's solvency).

---

## Thesis Validation

Every position is validated against 5 dimensions:

| Check | CRITICAL (BROKEN) | WARNING (DAMAGED) |
|-------|:---:|:---:|
| Earnings trend | Analyst cuts >20% | Cuts >10% |
| Fundamental health | P/E negative or > `pe_ratio_critical` (100) | P/E > `pe_ratio_warning` (50) |
| Technical damage | >25% below 200 SMA | >15% below 200 SMA |
| Volatility regime | — | HV >100% |
| Price performance | >40% off 52w high | >25% off 52w high |

**Action**: BROKEN → exit + flag; DAMAGED → weekly monitoring, re-evaluate in 7 days. All thresholds + trusted tickers configurable in `config/rules.yaml`.

---

## Systematic Timeline

| Review | When | Actions |
|--------|------|---------|
| **Daily Status** | 9 AM daily | Status check only — NO trading decisions |
| **Weekly Thesis** | Mon 9 AM | Validate thesis, auto-exit if broken |
| **Expiry Processing** | Fri 4 PM | Process expirations/assignments only |
| **Monthly Guardrail** | 1st of month | Concentrations, CSP deployment, cash buffer |

---

## Project Layout

```
options/
├── config/            # rules.yaml (ALL thresholds) + do_not_wheel.yaml (manual skip)
├── src/               # All business logic (data/scoring/analysis/risk/guardrails)
├── scripts/           # Thin wrappers: portfolio, screener, oie_engine, market_data, market_sentiment
├── tests/             # 560+ unit tests + integration/infrastructure suites
├── db/                # oie_paper.db (paper trading only)
├── docs/ specs/       # Research docs, loss-management playbook, architecture
└── README.md
```

**Key principles**: `src/` owns all logic, `scripts/` are thin display layers. Config is the single source of truth. Read-only — scripts never submit orders. Same scoring + guardrails shared between screener, portfolio, and paper engine.