# Options Wheel — Deterministic Screening + Income Engine (OIE)

A **paper-trading options wheel framework** for Covered Calls (CC), Cash-Secured Puts (CSP), and (suggestion-only) Put Credit Spreads (PS). It screens a watchlist, scores every ticker and contract with deterministic formulas, manages exits through a single trend-aware decision core, and runs an autonomous **paper portfolio** (the OIE) that mirrors the strategy end-to-end — without ever touching real money.

> **Hard constraints (non-negotiable)**: no naked options, no margin trading, no debit spreads. CC needs 100 owned shares per contract; CSP needs full cash coverage; PS is a defined-risk, 100%-cash-backed, suggestion-only exception (`max_loss = width − net_credit`). **No script in this repo submits a real order** — every trade is executed manually against the engine's recommendations. The only database is `db/oie_paper.db` (paper trading only).

---

## Table of Contents
- [What this is (and isn't)](#what-this-is-and-isnt)
- [Quick Start](#quick-start)
- [The two surfaces: Live (read-only) vs OIE (paper)](#the-two-surfaces)
- [Scripts](#scripts)
- [Configuration](#configuration)
- [The decision engine in one picture](#the-decision-engine)
- [Guardrails & risk controls](#guardrails--risk-controls)
- [OIE Daily Digest](#oie-daily-digest)
- [Deployment](#deployment)
- [Testing & validation](#testing--validation)
- [Project layout](#project-layout)
- [Strategy across market regimes](#strategy-across-market-regimes)
- [Documentation map](#documentation-map)

---

## What this is (and isn't)

**Is**
- A deterministic scoring + screening engine: every score, signal, gate, and exit decision is a pure formula over fetched data. AI is used only for the digest *narrative*, never for the math.
- A read-only mirror of your **real** moomoo account — fetched live every run, never cached in a real-account DB.
- An autonomous **paper** portfolio (the OIE) that opens *and manages* both CSPs and CCs, rotating the wheel end-to-end on paper (CSP→assign→shares→CC→assign→CSP).
- Guardrailed by two layers (per-trade block/warn + staged monthly recovery) and a holdings-exit framework (thesis-break + price backstops).
- Evidence-based: thresholds come from Tastytrade (200K+ backtested trades), Barchart, ApexVol, and the SPY-wheel literature — see `specs/`.

**Isn't**
- An auto-trading bot for real money. **Read-only on the real account. Manual execution only.**
- A source of live order submission. `moomoo_client.py` wraps only the quote context; `portfolio_loader.py` reads account/positions/orders but never places trades.
- A backtester. The engine runs on live snapshots only; a backtest harness is designed (`specs/research_backtesting_architecture.md`) but **not implemented**. Validate every "PROPOSED/UNVALIDATED" rule on paper first.

---

## Quick Start

```bash
source .venv/bin/activate          # first time: python3 -m venv .venv && pip install -r requirements.txt

# ── Daily workflow ──
python3 scripts/portfolio.py                 # 1. Full state: funds + P&L + health + thesis + recommendations
python3 scripts/screener.py --top 10         # 2. Best CC/CSP candidates across the watchlist

# ── Focused views ──
python3 scripts/portfolio.py --fast          # Funds + P&L only
python3 scripts/portfolio.py --health        # Decisions + overlap + guardrails
python3 scripts/portfolio.py --thesis        # Thesis validation only
python3 scripts/portfolio.py --pnl           # Positions + income
python3 scripts/portfolio.py --orders [TICK] # Order history (90d)

# ── Paper trading (validate the strategy before risking capital) ──
python3 scripts/oie_engine.py reset --force  # Wipe paper portfolio
python3 scripts/oie_engine.py init           # Seed from REAL holdings
python3 scripts/oie_engine.py once --force   # Run one paper cycle
python3 scripts/oie_engine.py status         # Paper positions + P&L
```

**Prerequisites**: moomoo OpenD running on `127.0.0.1:11111`. Python 3.9+. The **REAL portfolio is read live from moomoo each run — there is no real-account DB**. The only database is `db/oie_paper.db` (paper trading).

---

## The two surfaces

This repo deliberately separates **looking at your real account** from **simulating trades on paper**. Confusing them is the most common mistake.

### Surface 1 — Live (read-only): `portfolio.py`, `screener.py`, `market_data.py`, `market_sentiment.py`
These scripts pull funds / positions / orders / option chains live from moomoo on every invocation, run the deterministic engine over them, and **print recommendations**. They never persist anything and never submit orders. Use these to decide what to trade manually.

### Surface 2 — Paper (the OIE): `oie_engine.py`
An autonomous paper portfolio seeded from your real holdings, then left to run the wheel by itself. It opens and manages both CSPs and CCs in `db/oie_paper.db`. After seeding it is **independent of the real account** (the paper share count is the source of truth for CC eligibility), so the wheel rotates end-to-end on paper. `reconcile` non-destructively re-syncs paper stocks + cash to the real account after you make a manual real trade. See [`specs/oie-paper-engine-spec.md`](specs/oie-paper-engine-spec.md).

---

## Scripts

All scripts are **thin display layers** — `argparse → fetch data → call src/ → print`. No business logic lives in `scripts/`. (Enforced: no script imports another script; `grep -rn "from scripts\." scripts/` returns nothing.)

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
- **Fund / Total Assets** = HKD→USD converted using the **live USD/HKD FX rate** (yfinance, cached), falling back to 7.8 offline

**Thesis scope** = holdings + option underlyings + the **live moomoo watchlist group** (the master list — not the static config fallback). Each ticker shows status (`THESIS_INTACT` / `TECHNICAL_DAMAGE` / `THESIS_BROKEN`), an `[eligible]` tag, and an inline **`🚫 DO-NOT-WHEEL until YYYY-MM-DD`** flag when in `config/do_not_wheel.yaml`.

### `screener.py` — Watchlist Screener
Scores every watchlist ticker **1–10 (lower = better)** across 5 weighted dimensions, then ranks the best CC/CSP/PS contracts.

```bash
python3 scripts/screener.py --top 5           # Top 5 trades
python3 scripts/screener.py --cc-only         # Covered calls only
python3 scripts/screener.py --csp-only        # Cash-secured puts only
python3 scripts/screener.py --ps-only         # Put credit spreads (defined-risk, suggestion-only)
python3 scripts/screener.py --validate AMD    # Single ticker deep-dive
python3 scripts/screener.py --no-external     # Offline (skip yfinance)
python3 scripts/screener.py --force           # Skip guardrails + market-hours checks
```

| Dimension | Weight | Measures |
|-----------|:---:|----------|
| Technical | 25% | RSI, SMA stack, ADX, volume ratio |
| Options Quality | 25% | Bid-ask spread, IV rank, market cap, beta |
| Fundamental | 15% | P/E, dividend yield, EPS |
| External Sentiment | 20% | Analyst consensus, earnings blackout, news |
| Macro/Risk | 15% | VIX regime, earnings |

Contract gates (liquidity, delta, IV, VRP, RoC minimums, concentration, cash buffer) come from `config/rules.yaml` via `src/filters/contract_filters.py`. Loss-makers (`net_profit<0 AND eps_ttm<0`, or negative P/E) are auto-skipped. A **negative chain GEX** (< −500k) pauses new CSP candidates. Full formulas in [`specs/scoring-spec.md`](specs/scoring-spec.md).

### `oie_engine.py` — Paper Trading Engine

| Command | Description |
|---------|-------------|
| `init` | Seed paper portfolio from REAL holdings: stocks + cash + existing options |
| `reconcile` | Non-destructively sync paper STOCK rows + cash from REAL account (after manual real trades). Preserves options & P&L history. |
| `once` | Single cycle: mark-to-market → exits → screen → guardrails → paper trades → snapshot |
| `once --dry-run` | Same but writes NOTHING to DB — preview what WOULD happen |
| `run [--interval N]` | Continuous loop (default 30 min) with Ctrl+C graceful stop |
| `status` | Paper positions, cash, P&L |
| `history` | Net-liquidation snapshots over time |
| `reset --force` | Wipe all paper data |
| `test` | Self-check — validates DB, config, scoring (no OpenD needed) |
| `sim open STRAT TICK STRIKE EXPIRY --premium P [--contracts N --delta D --iv V]` | Open a manual paper position (no OpenD) |
| `sim close POS_ID --price P` · `sim expire POS_ID` · `sim list` | Manage manual paper positions |

**Cycle phases** (each `once`/`run` iteration): load state → mark-to-market → **check exits via the single decision core** (`src/analysis/exit_management.py`, composing trend-modulated profit targets with loss-side delta/premium/absolute stops) → screen new opportunities → apply guardrails → execute paper trades → snapshot. CSP cuts at |Δ|≥0.60; **CC rolls up-and-out at Δ≥0.60** to keep shares + recapture upside; DTE≤0 → expire/assign. Full detail in [`specs/oie-paper-engine-spec.md`](specs/oie-paper-engine-spec.md) and [`specs/exit-and-profit-management-spec.md`](specs/exit-and-profit-management-spec.md).

### `decision_review.py` — Decision Retrospective
Reconstructs real option-trading decisions over a window (default 60 days) and compares each against a **hypothetical "hold to expiry"** baseline, using actual underlying prices at each contract's expiry. Answers: *"Did my aggressive closes/rolls actually help, or would holding have been better?"* Read-only.

```bash
python3 scripts/decision_review.py                  # last 60 days
python3 scripts/decision_review.py --days 90        # wider window
python3 scripts/decision_review.py --ticker V       # one ticker
python3 scripts/decision_review.py --profit-targets # 50%/80%/100% booking analysis
```

### `market_data.py` / `market_sentiment.py` — Deep dives
```bash
python3 scripts/market_data.py V --options          # Stock + technicals + option chain (30–45 DTE)
python3 scripts/market_data.py NVDA --all           # Full chain + GEX, PCR, skew, OI walls
python3 scripts/market_sentiment.py AAPL --news     # Macro + AAPL (analysts, earnings, news)
python3 scripts/market_sentiment.py --watchlist     # All watchlist tickers (parallel)
```

---

## Configuration

`config/rules.yaml` is the **single source of truth** for every threshold — regime classification, delta/DTE/IV/RoC gates, scoring weights, position limits, stop-loss layers, rolling discipline, profit-booking targets, thesis checks, and the systematic schedule. Edit here; every script reloads via `src/config.py` (`get_config()` cached singleton; `reload_config()` after edits). **No hardcoded values in scripts.**

`config/do_not_wheel.yaml` is a **hand-edited force-skip list** — the engine never writes to it. Expired entries are ignored. Add a ticker to block it in both the screener (skipped before scoring) and the portfolio (shown inline as `🚫 DO-NOT-WHEEL until DATE`):

```yaml
BE:
  added_date: '2026-08-02'
  expiration_date: '2027-02-02'
  months: 6
  reason: Negative P/E ratio (-556.2) - company losing money
```

`config/email.yaml` (gitignored; copy from `email.yaml.example`) configures SMTP for the digest's `--send`. **Trusted high-conviction names** (AMD, PLTR, TSLA) live in `rules.yaml → thesis_validation.trusted_tickers`, which exempts them from the high-P/E valuation check (negative P/E still flags — that's a solvency check).

---

## The decision engine

Everything you see — screener rankings, portfolio "✅ CLOSE / 🔄 ROLL / 🛑 STOP" decisions, and the OIE's autonomous actions — flows from one set of deterministic modules in `src/`. There is no AI in the math.

```
config/rules.yaml ──▶ src/config.py (typed accessors)
                              │
   ┌──────────────────────────┼──────────────────────────┐
   ▼                          ▼                          ▼
src/data/                 src/filters/                src/scoring/
 moomoo_client            contract_filters            screener_score (ticker 1-10)
 yfinance_client          (liquidity, delta,           holding_score (option decisions)
 portfolio_loader          IV, VRP, RoC, conc.)
 compute (RSI/MACD/                                    src/analysis/
  ADX/HV/IVrank/GEX)                                  profit_management (trend targets)
                                                      exit_management (decision core)
                                                      thesis / thesis_validator
                                                      sentiment / trend
                              │
                              ▼
                     src/analysis/exit_management.decide_exit_action()
                     src/data/guardrails.GuardrailChecker     (single source of exit truth)
                     src/guardrails/limits.StagedGuardrails
```

**The single exit decision core** (`src/analysis/exit_management.py:88` `decide_exit_action`) is the heart of the system. It composes:

1. **Expiry** (DTE ≤ 0) → caller resolves ITM/OTM to ASSIGN/EXPIRE.
2. **Profit side** (`decide_profit_target`) — the **strategy-direction asymmetry**:
   - **CSP in uptrend = good** (stock runs *away* from strike) → extend the 50% target to 70% / 85% when trend + sentiment + IVR all confirm.
   - **CC in uptrend = bad** (stock runs *into* strike) → never extend; instead **roll up-and-out** to keep shares.
   - Hard gates override every extension: **DTE ≤ 21** (gamma floor), **capital scarcity**, **earnings in DTE**.
3. **Loss side** — delta gates (CSP cut at |Δ|≥0.60; CC roll-up-out at Δ≥0.60), premium-multiple stops (DTE-adjusted: 3×/2×/1.5×), absolute catch-all (**premium-tiered**: −$1k / −$2k / −$5k / −$8k by total credit banked, so a large premium isn't cut on ITM noise).

Every threshold above is in `config/rules.yaml`. Full formulas + decision tables in [`specs/exit-and-profit-management-spec.md`](specs/exit-and-profit-management-spec.md).

---

## Guardrails & risk controls

Risk is enforced in **two complementary layers** plus a holdings-exit framework:

| Layer | File | Scope | Severity |
|-------|------|-------|----------|
| **Per-trade / daily** | `src/data/guardrails.py` `GuardrailChecker` | Called every OIE cycle + by screener. Binary BLOCK/WARN on fixed limits. | BLOCK stops new trades; WARN surfaces |
| **Staged recovery** | `src/guardrails/limits.py` `StagedGuardrails` | Adapts limits to portfolio cash-buffer health (EMERGENCY/TARGET/COMFORT). Narrative layer for portfolio health. | CRITICAL / BLOCK / WARN |
| **Holdings exit** | `src/risk/holdings_exit.py` | Stock-leg loss rules: dead zone (>15% below basis), backstop (−30% if below declining 200 SMA), circuit breaker (−40% unconditional), thesis-break gates. | Exit signals |
| **Coverage (collar)** | `src/risk/collar_check.py` | Verifies every CC has ≥100 shares and every CSP has cash — `all_clear` required conceptually before any new trade. | Hard |
| **Roll discipline** | `src/risk/monitor.py` | Net-credit-only, ≤2 rolls/campaign, ≥30-day extension, broken-position detection. | Hard |

**CSP pause triggers** (stop new CSPs if ANY true): VIX > 25 · SPY < 200 SMA · regime ≤ −2 · cash reserve < 20% · stock > 15% below basis.

**Hard position limits**: single position ≤ 15–25% net liq · sector ≤ 25% · CSP deployed ≤ 25% (≤10% volatile) · cash buffer ≥ 10% (block) / 15% (warn) · open positions ≤ 10 · new positions/day ≤ 2 · **margin ≤ 30%** (hard 15-day clear window).

Full detail in [`specs/guardrails-and-risk-spec.md`](specs/guardrails-and-risk-spec.md).

---

## OIE Daily Digest

Chains **portfolio → market_sentiment → market_data → screener → OIE paper cycle** into a single rich HTML report with a 5–10 bullet Daily Decision Abstract, ready to email.

```bash
python3 skills/oie/scripts/daily_digest.py --morning                  # 07:00 pre-market digest
python3 skills/oie/scripts/daily_digest.py --evening                  # 19:00 post-market digest
python3 skills/oie/scripts/daily_digest.py --send                     # email (needs config/email.yaml)
python3 skills/oie/scripts/daily_digest.py --skip-screener --skip-oie # fast mode
```

- Output: `logs/digest-<timestamp>.html` (rich HTML) + `logs/digest-<timestamp>.json` (facts for the GenAI abstract)
- The OIE step always runs `--dry-run` — **paper only, never real orders**
- `--send` delivers the HTML email via SMTP; copy `config/email.yaml.example` → `config/email.yaml` for Gmail setup
- Cron ideas: `0 7 * * 1-5` / `0 19 * * 1-5` (see `skills/oie/`)

---

## Deployment

**Continuous mode — macOS LaunchAgent** (recommended for the OIE):
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

For production-readiness assessment (what's solid, what's a gap, go/no-go gates), see [`specs/production-deployment.md`](specs/production-deployment.md).

---

## Testing & validation

```bash
pytest tests/ -v --tb=short                                  # full suite
pytest tests/ --cov=src --cov-report=term --cov-fail-under=85 # coverage gate (CI)
```

**Current state (audited)**: **715 tests pass, 0 failures, 17 skipped** (skips require live moomoo/yfinance). Pure-logic strategy modules are well covered — `profit_management` 93%, `exit_management`/`trend` 89%, `credit_spread` 90%, `collar_check`/`holdings_exit` 100%, `guardrails/limits` 85%. Overall coverage reads ~57% only because the I/O-heavy client modules (`moomoo_client`, `yfinance_client`, `compute`) need live network to exercise. Run the live-data tests separately on a machine with OpenD up.

Key test files and what they validate:
- `tests/test_screener_scoring.py` — ticker scoring + contract penalty with exact expected values
- `tests/test_holding_score.py` — holding/option scoring with stop-loss decisions
- `tests/test_profit_management.py` — trend-modulated profit-target decision core
- `tests/test_exit_management.py` — single exit decision core (profit + loss sides)
- `tests/test_trend.py` — SMA/MACD/RSI/ADX formula correctness
- `tests/test_oie_db.py` / `tests/test_oie_simulation.py` — OIE paper DB CRUD + lifecycle
- `tests/test_overlap.py` — Put/call overlap analysis
- `tests/test_holdings_exit.py` — Exit framework: dead zone, backstop, circuit breaker
- `tests/test_portfolio_loader.py` / `tests/test_portfolio_summary.py` — data + P&L

CI: `.github/workflows/test-suite.yml`. Pre-commit hooks: `.pre-commit-config.yaml`.

---

## Project layout

```
options/
├── config/           rules.yaml (ALL thresholds) · do_not_wheel.yaml (manual skip) · email.yaml (digest SMTP)
├── src/
│   ├── data/         moomoo + yfinance clients, portfolio_loader, watchlist, compute, oie_db (paper), guardrails
│   ├── filters/      contract_filters — shared gates (liquidity, delta, IV, VRP, RoC, concentration)
│   ├── scoring/      screener_score (ticker+contract), holding_score (existing positions)
│   ├── strategies/   credit_spread — put credit spread (PS) scoring
│   ├── analysis/     profit_management, exit_management, thesis + thesis_validator, trend, sentiment, adaptive_profit, roll_first, correlation
│   ├── risk/         holdings_exit, overlap, monitor (roll discipline), collar_check
│   ├── guardrails/   limits — staged position limits (EMERGENCY/TARGET/COMFORT)
│   ├── portfolio/    summary — income, sector breakdown, decision messages
│   ├── system/       scheduler — daily review cadence
│   └── config.py     typed access to rules.yaml
├── scripts/          thin wrappers: portfolio, screener, oie_engine, market_data, market_sentiment, decision_review
├── skills/           oie (interactive + daily-digest) · moomoo-* anomaly/digest skills · ai-credit-status
├── tests/            700+ unit tests + integration / infrastructure / security suites
├── deploy/           macOS LaunchAgent + systemd unit + run wrapper
├── db/               oie_paper.db (paper trading ONLY — real account has no DB)
├── specs/            full reference: architecture, formulas, scoring, exits, guardrails, OIE, regimes, deployment
├── docs/             profit-loss-management walkthrough
└── GOAL.md           strategy goals + regime/CC-CSP-ratio reference tables
```

**Key principles**: `src/` owns all logic; `scripts/` are thin display layers; `config/rules.yaml` is the single source of truth; everything is **read-only on the real account** — scripts never submit orders; the **same scoring + guardrails** are shared across the screener, portfolio, and paper engine.

---

## Strategy across market regimes

The engine classifies the market into 5 regimes from VIX and adapts position sizing, delta, and the CC/CSP mix accordingly. This is where the framework's behavior changes most across bull/bear/volatile/stagnant conditions.

| Regime | VIX | Position size | Cash reserve | CSP delta | CC delta | CSP:CC |
|--------|-----|:---:|:---:|:---:|:---:|:---:|
| **BULLISH** | < 12 | 80% | ≥ 15% | 0.20–0.30 | 0.20–0.30 | 60:40 |
| **NEUTRAL** | 12–20 | 75% | ≥ 20% | 0.20–0.30 | 0.20–0.30 | 50:50 |
| **CAUTIOUS** | 20–25 | 50% | ≥ 25% | 0.15–0.25 | 0.25–0.35 | 30:70 |
| **VOLATILE** | 25–30 | 25% | ≥ 30% | 0.10–0.20 | 0.30–0.40 | 10:90 |
| **BEARISH** | > 30 | 0% | ≥ 35% | **none** | 0.25–0.35 (existing only) | 0:100 |

For the full per-regime strengths, weaknesses, and known gaps (e.g. the **low-VIX paradox** — the BULLISH row is the dangerous one because sub-15 VIX carries asymmetric jump risk), see [`specs/regime-playbook.md`](specs/regime-playbook.md).

---

## Documentation map

| Document | What it covers |
|----------|----------------|
| **This README** | Getting started, scripts, layout |
| [`specs/architecture-spec.md`](specs/architecture-spec.md) | The actual layered read-only + paper-DB architecture (replaces the stale DB-centric design) |
| [`specs/formulas-reference.md`](specs/formulas-reference.md) | Every formula with derivation, file:line, authoritative source, and a validation note |
| [`specs/scoring-spec.md`](specs/scoring-spec.md) | The 5-dimension ticker score + contract penalty, with exact sub-score tables |
| [`specs/exit-and-profit-management-spec.md`](specs/exit-and-profit-management-spec.md) | The single decision core, trend modulation, loss stops, rolling discipline |
| [`specs/guardrails-and-risk-spec.md`](specs/guardrails-and-risk-spec.md) | Two-layer guardrails, collar, holdings exit, margin model |
| [`specs/oie-paper-engine-spec.md`](specs/oie-paper-engine-spec.md) | Cycle phases, paper DB schema, wheel rotation |
| [`specs/regime-playbook.md`](specs/regime-playbook.md) | Bull / bear / volatile / stagnant behavior matrix + gaps |
| [`specs/production-deployment.md`](specs/production-deployment.md) | Deployment challenges, GEX/backtest/margin gaps, go/no-go gates |
| [`specs/loss-management-playbook.md`](specs/loss-management-playbook.md) | Evidence base (64 cited sources) for the loss/exit rules |
| [`specs/research_dte_selection.md`](specs/research_dte_selection.md) | Evidence base for 30–45 DTE / 21 DTE / 50% rules |
| [`specs/position_sizing_standard.md`](specs/position_sizing_standard.md) | Hard/soft limits + sizing math |
| [`GOAL.md`](GOAL.md) | Strategy goals + regime/CC-CSP-ratio reference tables |
| [`CLAUDE.md`](CLAUDE.md) | Operating protocol for AI assistants working in this repo |

---

*Strategy research, not financial advice. Every "PROPOSED/UNVALIDATED" rule in `config/rules.yaml` must pass `pytest` + a historical backtest before live orders depend on it. The backtest harness is designed but not yet implemented.*
