# Options Wheel Strategy — Deterministic Trading Engine

Covered Call (CC) & Cash-Secured Put (CSP) screening, scoring, and portfolio management with a systematic review cadence.

**Hard constraints**: no naked options, no margin trading, no spreads. CC needs 100 owned shares per contract; CSP needs full cash coverage. **No script submits orders** — every trade is executed manually. The engine only *recommends*.

---

## Quick Start

```bash
source .venv/bin/activate          # first time: python3 -m venv .venv && pip install -r requirements.txt

# ── Daily Workflow ──
python3 scripts/portfolio.py                 # 1. Full state: funds + P&L + health + thesis + recommendations
python3 scripts/screener.py --top 10         # 2. Best CC/CSP candidates across the watchlist

# ── Focused Views ──
python3 scripts/portfolio.py --fast          # Funds + P&L only
python3 scripts/portfolio.py --health        # Decisions + overlap + guardrails
python3 scripts/portfolio.py --thesis        # Thesis validation only
python3 scripts/portfolio.py --pnl           # Positions + income
python3 scripts/portfolio.py --orders [TICK] # Order history (90d)

# ── Paper Trading (validate the strategy before risking capital) ──
python3 scripts/oie_engine.py reset --force  # Wipe paper portfolio
python3 scripts/oie_engine.py init           # Seed from REAL holdings
python3 scripts/oie_engine.py once --force   # Run one paper cycle
python3 scripts/oie_engine.py status         # Paper positions + P&L
```

**Prerequisites**: moomoo OpenD running on `127.0.0.1:11111`. Python 3.9+. The **REAL portfolio is read live from moomoo each run — no local DB**. The only database is `db/oie_paper.db` (paper trading).

### OIE Daily Digest — Full Engine in One HTML Report

Chains **portfolio → market_sentiment → market_data → screener → OIE paper cycle** into a single rich HTML file with a 5–10 bullet Daily Decision Abstract, ready to email.

```bash
python3 skills/oie-daily-digest/scripts/daily_digest.py --morning                  # 07:00 pre-market digest
python3 skills/oie-daily-digest/scripts/daily_digest.py --evening                  # 19:00 post-market digest
python3 skills/oie-daily-digest/scripts/daily_digest.py --send                     # email (needs config/email.yaml)
python3 skills/oie-daily-digest/scripts/daily_digest.py --skip-screener --skip-oie # fast mode
```

- Output: `logs/digest-<timestamp>.html` (rich HTML) + `logs/digest-<timestamp>.json` (facts for the GenAI abstract)
- The OIE step always runs `--dry-run` — **paper only, never real orders**
- `--send` delivers the HTML email via SMTP; copy `config/email.yaml.example` → `config/email.yaml` for Gmail setup
- Cron ideas: `0 7 * * 1-5` / `0 19 * * 1-5` (see `skills/oie-daily-digest/`)

---

## Scripts

All scripts are **thin display layers** — argparse → fetch data → call `src/` → print. No business logic lives in `scripts/`.

### `portfolio.py` — Real Account Dashboard

Full sweep: **funds → P&L → health/guardrails → timeline → thesis → recommendations**.

| Flag | Shows |
|------|-------|
| (bare) | Full sweep |
| `--fast` | Funds + P&L only (no scoring) |
| `--funds` | Account funds only |
| `--pnl` | Stock/option positions, all-time & monthly income, sector breakdown |
| `--health` | Holding scores + CC hunt + exit framework + option decisions + overlap + guardrails (two layers) |
| `--thesis` | Thesis validation on holdings + option underlyings + live watchlist |
| `--schedule` | Systematic review timeline |
| `--orders [TICK]` | Filled order history (last 90 days) |
| `--no-external` | Skip yfinance (offline; skips thesis deep-checks) |

**Funds legend** (validated against moomoo upstream):
- **Buying Power** = margin-inclusive `power` converted to USD (matches the moomoo app)
- **Cash Buying Power** = cash-only `usd_net_cash_power` (already USD — never ÷7.8)
- **Fund / Total Assets** = HKD→USD converted using the **live USD/HKD FX rate** (yfinance, cached), falling back to 7.8 offline — so figures match the moomoo app

**Thesis scope** = holdings + option underlyings + the **live moomoo watchlist group** (the master list — not the static config fallback, so stale names don't appear). Each ticker shows status (`THESIS_INTACT` / `TECHNICAL_DAMAGE` / `THESIS_BROKEN`), an `[eligible]` tag, and an inline **`🚫 DO-NOT-WHEEL until YYYY-MM-DD`** flag when in `config/do_not_wheel.yaml`.

### `screener.py` — Watchlist Screener

Scores every watchlist ticker 1–10 (**lower = better**) across 5 weighted dimensions, then ranks the best CC/CSP contracts.

```bash
python3 scripts/screener.py --top 5           # Top 5 trades
python3 scripts/screener.py --cc-only         # Covered calls only
python3 scripts/screener.py --csp-only        # Cash-secured puts only
python3 scripts/screener.py --validate AMD    # Single ticker deep-dive
python3 scripts/screener.py --no-external     # Offline (skip yfinance)
python3 scripts/screener.py --force           # Skip guardrails + market-hours checks
```

| Dimension | Weight | Measures |
|-----------|:---:|----------|
| Technical | 25% | RSI, SMA stack, ADX, volume ratio |
| Options Quality | 25% | Bid-ask spread, IV rank, market cap, beta |
| Fundamental | 15% | P/E, dividend yield, earnings consistency |
| External Sentiment | 20% | Analyst consensus, earnings blackout, news |
| Macro/Risk | 15% | VIX regime, yield curve, credit spreads |

Contract gates (liquidity, delta, IV, VRP, RoC minimums) come from `config/rules.yaml`. Loss-makers (`net_profit<0 AND eps_ttm<0`, or negative P/E) are auto-skipped via `is_wheel_eligible()`. A **negative chain GEX** (< −500k) pauses new CSP candidates.

### `oie_engine.py` — Paper Trading Engine

An autonomous paper portfolio that mirrors the strategy with the **same scoring + guardrails** as the real pipeline. **Never touches real money.**

| Command | Description |
|---------|-------------|
| `init` | Seed paper portfolio from REAL stock holdings + cash (options NOT copied) |
| `once` | Single cycle: mark-to-market → exits → screen → guardrails → paper trades → snapshot |
| `once --dry-run` | Same but writes NOTHING to DB — preview what WOULD happen |
| `run [--interval N]` | Continuous loop (default 30 min) with Ctrl+C graceful stop |
| `status` | Paper positions, cash, P&L |
| `history` | Net-liquidation snapshots over time |
| `reset --force` | Wipe all paper data |
| `test` | Self-check — validates DB, config, scoring (no OpenD needed) |
| `sim open STRAT TICK STRIKE EXPIRY --premium P [--contracts N --delta D --iv V]` | Open a manual paper position (no OpenD) |
| `sim close POS_ID --price P` · `sim expire POS_ID` · `sim list` | Manage manual paper positions |

**Continuous mode — macOS LaunchAgent** (recommended):
```bash
cp deploy/com.oie.engine.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.oie.engine.plist

launchctl list | grep oie                                       # live PID + exit code
launchctl unload ~/Library/LaunchAgents/com.oie.engine.plist    # stop
launchctl load  ~/Library/LaunchAgents/com.oie.engine.plist     # start / restart

tail -f logs/oie_launchd.log        # wrapper + engine stdout (heartbeat, per-cycle lines)
tail -f logs/options.log            # structured DEBUG logs per module
```

LaunchAgent behavior:
- **Boot**: starts at login → wrapper (`deploy/run_oie.sh`) waits 10 min for the system to settle → launches OpenD via bundle ID `com.moomoo.opend` → waits up to 90s for port 11111 → starts the engine
- **Crash**: launchd auto-restarts (`KeepAlive`); the wrapper sees OpenD still up and skips the 10-min delay for fast recovery
- **Market hours**: the engine sleeps 60s while US markets are closed (weekends/nights), fires a full cycle at market open and every 60 min after
- **Visibility**: cycle errors → `logs/options.log`; process crashes → `logs/oie_launchd.log`; trade audit → `db/oie_paper.db` → `paper_trades`

Manual / ad-hoc mode instead of the daemon:
```bash
tmux new -s oie
python3 scripts/oie_engine.py run --interval 60 --skip-closed
```

**Cycle phases** (each `once`/`run` iteration): load state → mark-to-market → check exits (trend-modulated profit targets; DTE≤0 → expire/assign; premium-multiple & delta stop-layers) → screen new opportunities → apply guardrails (15% concentration, cash buffer, max 8 positions, 2 trades/day) → execute paper trades → snapshot.

### `decision_review.py` — Decision Retrospective

Reconstructs real option-trading decisions over a window (default 60 days) and compares each against a **hypothetical "hold to expiry"** baseline, using actual underlying prices at each contract's expiry. Answers: *"Did my aggressive closes/rolls actually help, or would holding have been better?"*

```bash
python3 scripts/decision_review.py                  # last 60 days
python3 scripts/decision_review.py --days 90        # wider window
python3 scripts/decision_review.py --ticker V       # one ticker
python3 scripts/decision_review.py --profit-targets # 50%/80%/100% booking analysis
python3 scripts/decision_review.py --no-current     # skip live-price fetches
```

**Per contract**: entry premium, buy-back cost, actual realized P&L vs hypothetical hold-to-expiry P&L, and the **decision impact** (`✅ HELPED` / `❌ HURT` / `⏳ UNDECIDED`), rolled up by ticker and decision type, with worst/best decision deep-dives. `--profit-targets` simulates holding profitable closes to 80% / 100% (expiry) and flags **opportunity cost** where theta kept decaying.

Read-only — pulls live moomoo order history + yfinance historical prices. Never submits orders.

### `market_data.py` — Single Ticker Deep Dive

```bash
python3 scripts/market_data.py V                  # Stock + technicals + fundamentals
python3 scripts/market_data.py V --options        # + option chain (30–45 DTE)
python3 scripts/market_data.py NVDA --all         # Full chain + GEX, PCR, skew, OI walls
python3 scripts/market_data.py V --chain 30 45    # Custom DTE range
```

### `market_sentiment.py` — Macro + Sentiment

```bash
python3 scripts/market_sentiment.py               # Macro only (VIX, yields, Fear&Greed, regime)
python3 scripts/market_sentiment.py AAPL          # Macro + AAPL (analysts, earnings, institutions)
python3 scripts/market_sentiment.py AAPL --news   # + recent news (keyword-classified)
python3 scripts/market_sentiment.py --watchlist   # All watchlist tickers (parallel)
```

---

## Configuration

`config/rules.yaml` is the **single source of truth** for every threshold — regime classification, delta/DTE/IV/RoC gates, scoring weights, position limits, stop-loss layers, rolling discipline, profit-booking targets, thesis checks, and the systematic schedule. Edit here; every script reloads via `src/config.py`. No hardcoded values.

`config/do_not_wheel.yaml` is a **hand-edited force-skip list** — the engine never writes to it. Expired entries are ignored. Add a ticker to block it in both the screener (skipped before scoring) and the portfolio (shown inline as `🚫 DO-NOT-WHEEL until DATE`):

```yaml
BE:
  added_date: '2026-08-02'
  expiration_date: '2027-02-02'
  months: 6
  reason: Negative P/E ratio (-556.2) - company losing money
```

**Trusted high-conviction names** (AMD, PLTR, TSLA) are *not* listed there — they live in `config/rules.yaml → thesis_validation.trusted_tickers`, which exempts them from the high-P/E valuation check (negative P/E still flags — that's a solvency check).

---

## Thesis Validation

Every position is validated against 5 dimensions. BROKEN → exit signal + Do-Not-Wheel flag; DAMAGED → weekly monitoring, re-evaluate in 7 days.

| Check | CRITICAL (BROKEN) | WARNING (DAMAGED) |
|-------|:---:|:---:|
| Earnings trend | Analyst cuts >20% | Cuts >10% |
| Fundamental health | P/E negative or > `pe_ratio_critical` (100) | P/E > `pe_ratio_warning` (50) |
| Technical damage | >25% below 200 SMA | >15% below 200 SMA |
| Volatility regime | — | HV >100% |
| Price performance | >40% off 52w high | >25% off 52w high |

A deeper codable thesis layer (`src/analysis/thesis.py`) scores fundamental *deterioration* gates (growth stall, dual deceleration, margin erosion, balance sheet, cash flow) — BROKEN when ≥2 gates fail. All thresholds + trusted tickers configurable.

---

## Systematic Timeline

The `src/system/scheduler.py` module enforces a single daily 09:00 UTC review cadence (thesis + guardrails together), eliminating ad-hoc daily decisions.

| Review | When | Actions |
|--------|------|---------|
| **Daily Status** | 09:00 daily | Status check only — NO trading decisions |
| **Weekly Thesis** | Mon 09:00 | Validate thesis, exit signal if broken |
| **Expiry Processing** | Fri 16:00 | Process expirations / assignments only |
| **Monthly Guardrail** | 1st of month | Concentrations, CSP deployment, cash buffer |

The regime → position-size → CC/CSP-ratio tables that recommendations reference live in `GOAL.md` (backed by Tastytrade / Barchart / SPY-wheel research).

---

## Project Layout

```
options/
├── config/           rules.yaml (ALL thresholds) · do_not_wheel.yaml (manual skip) · email.yaml (digest SMTP)
├── src/
│   ├── data/         moomoo + yfinance clients, portfolio_loader, watchlist, compute, oie_db (paper), guardrails
│   ├── filters/      contract_filters — shared gates (liquidity, delta, IV, VRP, RoC, concentration)
│   ├── scoring/      screener_score (ticker+contract), holding_score (existing positions)
│   ├── analysis/     profit_management, thesis + thesis_validator, trend, sentiment, adaptive_profit, roll_first
│   ├── risk/         holdings_exit, overlap, monitor (roll discipline), collar_check
│   ├── guardrails/   limits — staged position limits (EMERGENCY/TARGET/COMFORT)
│   ├── portfolio/    summary — income, sector breakdown, decision messages
│   ├── system/       scheduler — daily review cadence
│   └── config.py     typed access to rules.yaml
├── scripts/          thin wrappers: portfolio, screener, oie_engine, market_data, market_sentiment, decision_review
├── skills/           oie (interactive) · oie-daily-digest (HTML+email) · moomoo-* · ai-credit-status
├── tests/            590+ unit tests + integration / infrastructure / security suites
├── deploy/           macOS LaunchAgent + systemd unit + run wrapper
├── db/               oie_paper.db (paper trading ONLY)
├── docs/ specs/      architecture-spec, loss-management playbook, profit/position-sizing research
└── GOAL.md           strategy goals + regime/CC-CSP-ratio reference tables
```

**Key principles**: `src/` owns all logic; `scripts/` are thin display layers; `config/rules.yaml` is the single source of truth; everything is **read-only** — scripts never submit orders; the **same scoring + guardrails** are shared across the screener, portfolio, and paper engine. See `specs/architecture-spec.md` for the full reference.
