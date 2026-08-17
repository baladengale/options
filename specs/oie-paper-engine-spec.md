# OIE Paper Engine Spec — Cycle, DB, Wheel Rotation

**Status**: Implemented & audited (2026-08-09; config-drift fixes 2026-08-16)
**Files**: `scripts/oie_engine.py`, `src/data/oie_db.py`
**Config**: `config/rules.yaml → position_limits`, `options`, `profit_take`, `stop_loss`, `csp_pause`, `cc_management`, `guardrail_limits`

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

Market-hours check (`is_market_open()` `:71`): 9:30–16:00 ET, DST-aware via `zoneinfo('America/New_York')` (`src/data/market_time.py`). `--force` or `--skip-closed` overrides. **Holidays / half-days are NOT handled** (known gap, §7).

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

> **Cash is derived, never stale**: `show_status` (`:1355`) computes `seeded_cash + Σ cash_change` rather than reading a stored value. This means every `cash_change` in `paper_trades` is the audit trail — running total always reconstructable.
>
> **Single-writer invariant (2026-08-16)**: the engine no longer tracks cash in a local variable — every cash movement flows through `oie_db._log_trade(cash_impact=...)`, which applies it to state and the audit row atomically. Closes pass NEGATIVE cash_impact (buyback is a payment); expiry books nothing (premium was credited at open); `reconcile` rebaselines via an adjustment row instead of a raw `set_state`. Stored state and derived cash can no longer diverge.

Key DB methods: `seed_portfolio` (`:126`), `reconcile_stocks` (`:166`, returns added/updated/unchanged), `open_position` / `close_position` / `expire_position` / `assign_position` (`:218–398`), `get_daily_new_count` (`:452`, excludes SEED, US/Eastern day), `get_monthly_profit_closes` / `get_last_exit_within_days` (churn caps), `save_snapshot` (`:467`).

---

## 3. Cycle phases — `run_cycle()` (`oie_engine.py:375`)

Each `once` / `run` iteration:

### Phase 1 — Load state (`:393`)
Read cash/fund from `paper_state`.

### Phase 2 — Load real portfolio + mark-to-market (`:397–442`)
Fetch live stock prices for real holdings + option underlyings (`stock_prices` aliases `self._stock_prices` — the expiry ITM/OTM resolution uses the same live marks). Refresh each open option's `current_bid` / `current_delta` / `current_iv` from moomoo option snapshots.

### Phase 3 — Seed grace period (`:450–489`)
Skip exit decisions for positions created within ±300s of `seeded_at` — so freshly-seeded real positions aren't immediately rolled/closed on the first cycle.

### Phase 4 — Exit decisions (`:458–590`)
Per active option, compute:
```
profit_captured = (entry − current_bid) / entry × 100
delta, pnl_dollars
capital_scarcity = _capital_scarcity()        (:873)
csp_paused       = _csp_paused()              (:891)
```
Then call the single decision core `decide_exit_action(...)` (`:508`) and dispatch on the result:

| Return | DB action |
|--------|-----------|
| `EXPIRE` | `db.expire_position` (full premium kept; no cash row — premium credited at open) |
| `CC_ASSIGN` | `db.assign_position(pos_id, 'CC', price)` — shares called away; audit cash += strike×qty×100 |
| `CSP_ASSIGN` | `db.assign_position(pos_id, 'CSP', price)` — shares added at basis `strike − entry_premium`; audit cash −= strike×qty×100 |
| `STOP_DELTA` / `STOP_LOSS` | `db.close_position` — buy back; audit cash −= current_bid×qty×100 |
| `CLOSE_50PCT` / `CLOSE_TREND` | `db.close_position` — **held** when the per-ticker monthly profit-close cap (`guardrail_limits.max_closes_per_ticker_per_month`) is reached |
| `ROLL_UP_OUT` / `ROLL_DOWN_OUT` | close the winner now; Phase 5 screening opens a fresh contract |

If no decision and `dte ≤ 0`, the caller resolves ITM/OTM to ASSIGN/EXPIRE using the stock price (`:520–532`).

### Phase 5 — Screen new opportunities (`:592 / 1026 _screen_candidates`)
- Fetch watchlist + portfolio snapshots in batch; enrich with 252-day history + SPY history.
- **CC eligibility uses PAPER shares as source of truth**: `free_shares = paper_shares − shares committed to open CCs` (`:1109`).
- Macro context is stored on `self._macro` / `self._regime` — it feeds the CSP-pause triggers and the regime-aware CSP-deployment limit.
- For each viable ticker (skip if `bid_ask_spread_pct > options.liquidity.bid_ask_spread_max_pct`):
  - Compute `_compute_ticker_score` (earnings blackout feeds the penalty; best-effort yfinance).
  - **CSP paused globally** (all 5 GOAL §5 triggers, `_csp_pause_reasons` `:921`) → the CSP branch is skipped for the whole scan.
  - **CC gates** (`cc_management`): never sell below paper cost basis (`never_sell_below_cost_basis`); CCs paused when price > `pause_cc_if_drop_pct` below basis.
  - For each contract (DTE `options.dte.screen_min`–`screen_max`):
    - **CSP path** if `not has_shares`: `passes_all_gates` (incl. IV-rank gate when IVR is known, `options.iv_rank_min`/`iv_rank_required`), earnings-blackout hard gate, `_csp_roc`, `capital = strike × 100`.
    - **CC path** if `has_shares`: `cc_roc`, `capital = last_price × 100`; earnings-blackout hard gate (GOAL §8 checklist applies to any new trade) and the never-below-basis gate.
  - One best candidate per ticker, deduped against existing option signatures.

### Phase 6 — Apply guardrails (`:594–712`)
```
max_new = max(0, min(max_new_positions_per_cycle, max_daily_new_positions − daily_new))
```
`GuardrailChecker` is built with `buying_power = cash + fund` (liquid only — **margin BP never counts**, GOAL #4) and `regime=` so the CSP-deployment block tightens to `max_csp_deployed_volatile_pct` in VOLATILE/BEARISH. Per-trade blocks: single-position % > `max_single_position_pct`; **CSP pause triggers** (re-checked at execution, incl. per-ticker basis drop); **same-strike reopen cooldown** (`guardrail_limits.same_strike_reopen_cooldown_days`); CSP capital > `position_limits.csp_single_cash_fraction` × cash; `gc.check_new_trade(...)`. **CCs skip cash-buffer/CSP-deployment blocks** (share-secured). **`PS` candidates are suggestion-only** — never executed/persisted.

### Phase 7 — Execute (`:714–795`)
Open CC: `db.open_position(... pos_type='CALL', qty=-1 ...)`. Open CSP: `pos_type='PUT', qty=-1`. Cash flows ONLY through `open_position(cash_impact=...)`. In dry-run, only logs.

### Phase 8 — Snapshot (`:797–826`)
`db.save_snapshot` with `total_value = cash + fund + unrealized` (cash re-read from the audit-maintained state); bump `cycle_count`.

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
2. **Margin rule is WARN, not BLOCK** in the OIE loop — see [guardrails-and-risk-spec.md](guardrails-and-risk-spec.md) §8 and [production-deployment.md](production-deployment.md) §3. (Mitigated, not fixed: the engine now passes `buying_power = cash + fund` only, so margin BP never extends CSP coverage; the 30% margin utilization itself is still WARN and `margin_used` is still not threaded from the broker.)
3. **Seed grace (±300s)** can mask a real signal immediately after `init` — re-run `once` after the grace window for a true first decision pass.
4. **PS candidates are surfaced but never executed** — by design. They require manual research before any live use (`GOAL.md` marks them UNVALIDATED).
5. **IV-rank gate is data-conditional** — enforced only when the contract carries IVR data; unknown IVR passes unless `options.iv_rank_required: true`. Same for the earnings blackout (yfinance best-effort).
6. **No US holiday / half-day calendar** — market-hours check treats federal holidays as open; the engine acts on stale quotes those days.
7. **One contract per trade** — `qty=-1` hardcoded at open; multi-contract sizing per `specs/position_sizing_standard.md` is not implemented.

---

## 8. Validation

- `tests/test_oie_db.py` (27) — paper DB CRUD: seed, open/close/expire/assign, snapshots, reconcile.
- `tests/test_oie_simulation.py` (19) — full lifecycle simulation.
- `tests/test_exit_management.py` (31) — the decision core the OIE calls.
- `tests/test_config_driven_gates.py` — config-driven gate plumbing (cash fraction, CSP pause, churn caps, cooldown, IVR).

All pass. Coverage: `oie_db` 83%.
