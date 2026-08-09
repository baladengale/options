# OIE Paper Engine Spec — Cycle, DB, Wheel Rotation

**Status**: Implemented & audited (2026-08-09)
**Files**: `scripts/oie_engine.py`, `src/data/oie_db.py`
**Config**: `config/rules.yaml → position_limits`, `options`, `profit_take`, `stop_loss`

> The OIE (Options Income Engine) is an **autonomous paper portfolio** that mirrors the wheel strategy end-to-end. After seeding from your real holdings, it is **independent of the real account** — the paper share count is the source of truth for CC eligibility, so the wheel rotates (CSP→assign→shares→CC→assign→CSP) without depending on the real book. **It never touches real money.**

---

## 1. Commands

| Command | Description |
|---------|-------------|
| `init` | Seed paper portfolio from REAL holdings: stocks + cash + existing options |
| `reconcile` | Non-destructively sync paper STOCK rows + cash from REAL account (after manual real trades). Preserves options & P&L history. |
| `once` | Single cycle (§3) |
| `once --dry-run` | Same, writes NOTHING to DB — preview |
| `run [--interval N]` | Continuous loop (default 30 min); Ctrl+C graceful stop |
| `status` | Paper positions, cash, P&L |
| `history` | Net-liquidation snapshots over time |
| `reset --force` | Wipe all paper data |
| `test` | Self-check — DB, config, scoring (no OpenD needed) |
| `sim open STRAT TICK STRIKE EXPIRY --premium P [--contracts N --delta D --iv V]` | Open a manual paper position (no OpenD) |
| `sim close POS_ID --price P` · `sim expire POS_ID` · `sim list` | Manage manual positions |

Constructor flags: `--no-external` (skip yfinance), `--dry-run`, `--force` (skip guardrails + market-hours).

Market-hours check (`is_market_open()` `:71`): 9:30–16:00 ET, rough EDT/EST detection by month. `--force` or `--skip-closed` overrides.

---

## 2. The paper DB — `db/oie_paper.db`

**File**: `src/data/oie_db.py`. Four tables (schema `_init_schema` `:43`):

### `paper_state` (key-value)
`cash, fund, seeded_at, seeded_cash, seeded_fund, last_cycle, cycle_count`

### `paper_positions`
| Column | Notes |
|--------|-------|
| `id, ticker` | |
| `pos_type` | `STOCK` / `CALL` / `PUT` |
| `status` | `ACTIVE` / `CLOSED` / `EXPIRED` / `ASSIGNED` |
| `qty` | shares > 0; short-option **contracts < 0** (e.g. −1 = short 1 contract) |
| `cost_price, strike, expiry, dte_initial` | |
| `entry_premium, current_bid, current_delta, current_iv` | refreshed each cycle |
| `exit_price, exit_date, exit_reason` | `CLOSE_50PCT` / `EXPIRE` / `ASSIGN` / `STOP_LOSS` / ... |
| `realized_pnl, created_at` | |

Indexes on `status`, `ticker`.

### `paper_trades` (full audit log)
`ts, event, ticker, pos_id (FK), detail, cash_change, created_at`. Events: `SEED, OPEN_CALL, OPEN_PUT, CLOSE, EXPIRE, ASSIGN_CSP, ASSIGN_CC, SNAPSHOT, ERROR, CYCLE, RECONCILE`. Index on `event`.

### `paper_snapshots`
`ts, total_value, cash, stock_value, fund_value, option_premium_received, option_liability, unrealized_pnl, realized_pnl_total, open_positions`. Index on `ts`.

> **Cash is derived, never stale**: `show_status` (`:1154`) computes `seeded_cash + Σ cash_change` rather than reading a stored value. This means every `cash_change` in `paper_trades` is the audit trail — running total always reconstructable.

Key DB methods: `seed_portfolio` (`:126`), `reconcile_stocks` (`:166`, returns added/updated/unchanged), `open_position` / `close_position` / `expire_position` / `assign_position` (`:218–398`), `get_daily_new_count` (`:452`, excludes SEED), `save_snapshot` (`:467`).

---

## 3. Cycle phases — `run_cycle()` (`oie_engine.py:370`)

Each `once` / `run` iteration:

### Phase 1 — Load state (`:388`)
Read cash/fund from `paper_state`.

### Phase 2 — Load real portfolio + mark-to-market (`:392–437`)
Fetch live stock prices for real holdings + option underlyings. Refresh each open option's `current_bid` / `current_delta` / `current_iv` from moomoo option snapshots.

### Phase 3 — Seed grace period (`:447–486`)
Skip exit decisions for positions created within ±300s of `seeded_at` — so freshly-seeded real positions aren't immediately rolled/closed on the first cycle.

### Phase 4 — Exit decisions (`:455–580`)
Per active option, compute:
```
profit_captured = (entry − current_bid) / entry × 100
delta, pnl_dollars
capital_scarcity = _capital_scarcity()        (:821)
csp_paused       = _csp_paused()              (:839)
```
Then call the single decision core `decide_exit_action(...)` (`:500`) and dispatch on the result:

| Return | DB action |
|--------|-----------|
| `EXPIRE` | `db.expire_position` (full premium kept) |
| `CC_ASSIGN` | `db.assign_position(pos_id, 'CC', price)` — shares called away; cash += strike×qty×100 |
| `CSP_ASSIGN` | `db.assign_position(pos_id, 'CSP', price)` — shares added at basis `strike − entry_premium`; cash −= strike×qty×100 |
| `STOP_DELTA` / `STOP_LOSS` | `db.close_position` — buy back; cash −= current_bid×qty×100 |
| `CLOSE_50PCT` / `CLOSE_TREND` | `db.close_position` |
| `ROLL_UP_OUT` / `ROLL_DOWN_OUT` | close the winner now; Phase 5 screening opens a fresh contract |

If no decision and `dte ≤ 0`, the caller resolves ITM/OTM to ASSIGN/EXPIRE using the stock price (`:512–524`).

### Phase 5 — Screen new opportunities (`:585 / 865 _screen_candidates`)
- Fetch watchlist + portfolio snapshots in batch; enrich with 252-day history + SPY history.
- **CC eligibility uses PAPER shares as source of truth**: `free_shares = paper_shares − shares committed to open CCs` (`:882–888, :937`).
- For each viable ticker (skip if `bid_ask_spread_pct > 5.0`):
  - Compute `_compute_ticker_score`.
  - For each contract (DTE 7–90):
    - **CSP path** if `not has_shares`: `passes_all_gates`, `_csp_roc`, `capital = strike × 100`.
    - **CC path** if `has_shares`: `cc_roc`, `capital = last_price × 100`.
  - One best candidate per ticker, deduped against existing option signatures.

### Phase 6 — Apply guardrails (`:587–685`)
```
max_new = max(0, min(2, max_daily_new_positions − daily_new))
```
Per-trade: single-position % > `max_single_position_pct` → block; CSP capital > 80% of cash → block; `gc.check_new_trade(...)`. **CCs skip cash-buffer/CSP-deployment blocks** (share-secured). **`PS` candidates are suggestion-only** (`:641–643`) — never executed/persisted.

### Phase 7 — Execute (`:626–725`)
Open CC: `db.open_position(... pos_type='CALL', qty=-1 ...)`. Open CSP: `pos_type='PUT', qty=-1`. In dry-run, only logs.

### Phase 8 — Snapshot (`:727–758`)
`db.save_snapshot` with `total_value = cash + fund + unrealized`; bump `cycle_count`.

---

## 4. Wheel rotation (why the paper book is the source of truth)

```
State: shares + cash
  │
  ├── CSP assigned → db.assign_position('CSP')
  │     shares += contracts×100  at basis (strike − entry_premium)
  │     cash   −= strike × contracts × 100
  │     → now eligible to sell CC
  │
  └── CC assigned → db.assign_position('CC')
        shares removed (deducted from earliest STOCK rows; over-deduction guarded + logged)
        cash += strike × contracts × 100
        → back to cash, eligible to sell CSP
```

So the wheel rotates end-to-end on paper. `reconcile()` (`:224`) non-destructively re-syncs paper STOCK rows + cash to the REAL account after manual real trades (preserves options + history). Returns `(added, updated, unchanged)`.

---

## 5. Continuous mode — macOS LaunchAgent

```bash
cp deploy/com.oie.engine.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.oie.engine.plist
```

Wrapper `deploy/run_oie.sh`:
1. **Port-check heuristic**: OpenD up on 11111 → crash-recovery → start engine immediately; OpenD down → cold boot → 10-min settle → launch OpenD via bundle ID `com.moomoo.opend` → wait up to 90s for port.
2. Activate venv, set `OPTIONS_HOME`, start: `python3 scripts/oie_engine.py run --interval 60 --skip-closed`.
3. On crash, launchd restarts (`KeepAlive`, `ThrottleInterval 60s`).

Logs: `logs/oie_launchd.log` (wrapper + engine stdout), `logs/options.log` (per-module DEBUG). Audit trail: `db/oie_paper.db → paper_trades`. systemd unit also provided (`deploy/oie-engine.service`).

---

## 6. The OIE Daily Digest

**File**: `skills/oie/scripts/daily_digest.py`. Chains `portfolio → market_sentiment → market_data → screener → OIE paper cycle` into one HTML report.

- `--morning` (07:00) / `--evening` (19:00) / `--send` (email) / `--skip-screener --skip-oie` (fast).
- Output: `logs/digest-<ts>.html` (rich HTML) + `.json` (facts for the GenAI abstract).
- **The OIE step always runs `--dry-run`** — paper only, never real orders.
- `auto_abstract()` (`:109`) is the deterministic fallback abstract (scans for `THESIS_BROKEN`, CSP liability vs liquid, top screen, regime); the GenAI *replaces* the `<div id="abstract">` bullets.
- `--send` without `--html` is **rejected** (`:221–229`) — the duplicate-email guard. Workflow: run → edit HTML abstract → `--send --html <file>` once.
- SMTP via `config/email.yaml` (smtp.gmail.com:587 STARTTLS; use a Gmail App Password).

Cron ideas: `0 7 * * 1-5` (pre-market), `0 19 * * 1-5` (post-market).

---

## 7. Known limitations / gaps

1. **No backtest harness** — the OIE runs on live snapshots only; you cannot replay history through it. (`specs/research_backtesting_architecture.md` designs it.)
2. **Margin rule is WARN, not BLOCK** in the OIE loop — see [guardrails-and-risk-spec.md](guardrails-and-risk-spec.md) §8 and [production-deployment.md](production-deployment.md) §3.
3. **Seed grace (±300s)** can mask a real signal immediately after `init` — re-run `once` after the grace window for a true first decision pass.
4. **PS candidates are surfaced but never executed** — by design. They require manual research before any live use (`GOAL.md` marks them UNVALIDATED).

---

## 8. Validation

- `tests/test_oie_db.py` (27) — paper DB CRUD: seed, open/close/expire/assign, snapshots, reconcile.
- `tests/test_oie_simulation.py` (19) — full lifecycle simulation.
- `tests/test_exit_management.py` (31) — the decision core the OIE calls.

All pass. Coverage: `oie_db` 83%.
