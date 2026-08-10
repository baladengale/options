# Wheel vs Buy-and-Hold P&L System + Per-CC Cap Cost Column

## Two outputs, one consistent foundation

**Output 1 — Per-open-CC "Cap Cost" column** in the OPTION POSITIONS table: shows foregone upside on each *current* covered call = `max(0, current_price − strike) × qty × 100`. Instantly answers "this CC earned me $X premium but is capping $Y of share gain."

**Output 2 — All-time "Wheel vs Buy-and-Hold"** block: compares your actual Wheel economics against a hypothetical buy-and-hold of the same foundational shares. Reuses the `real_lots` ledger as the single source of truth (every CC_EXIT and CSP_ASSIGN row already has ticker, qty, strike, date).

## Files to change

### 1. `src/data/real_lots.py` — new query methods
- `get_cc_exits()` → `SELECT * FROM real_lots WHERE source='CC_EXIT' ORDER BY date` (uses existing `idx_rl_source` index). Returns `list[sqlite3.Row]`. Each row's `price_per_share` IS the strike.
- `get_csp_assigns()` → same for CSP_ASSIGN rows.
- `get_assignment_lots()` → convenience: both, with a `source` tag. For the all-time block.

These are pure reads — no schema change. The ledger already stores everything needed (strike in `price_per_share`, qty, ticker, date).

### 2. New `src/portfolio/wheel_vs_hold.py` — the comparison engine
A pure module (no I/O) computing the buy-and-hold comparison from three inputs:
- `lots` (CC_EXIT + CSP_ASSIGN rows from the ledger)
- `prices: dict[ticker, float]` (current price map, fetched by caller)
- `net_option_income: float` + `assignment_realized_pl: float` (already computed)

**Methodology** (the part that needs to be right):
```
For each CC_EXIT lot (shares called away at strike S on date D):
  foregone_upside = max(0, current_price − S) × qty
  (You sold at S; buy-and-hold would still hold them at current_price.)

For each CSP_ASSIGN lot (shares received at strike S, basis S−premium):
  held_gain_bh = (current_price − S) × qty          # what buy-and-hold-at-strike would've made
  wheel_gain   = (current_price − (S−premium)) × qty  # what wheel actually makes (cheaper basis)
  csp_advantage = wheel_gain − held_gain_bh = premium × qty   # wheel beats BH by the premium

WHEEL_RETURN   = net_option_income + assignment_realized_pl + unrealized_open
BUYHOLD_RETURN = Σ (current_price − acquisition_basis) × qty  over foundational lots
                 − foregone_upside on called-away shares
                 + capped upside on open CCs
```

Returns a dataclass: `{wheel_return, buyhold_return, wheel_advantage, total_foregone_upside, per_ticker: [...]}`. Pure function — easy to unit test with synthetic lots.

### 3. `scripts/portfolio.py` — two display additions

**(a) OPTION POSITIONS table (lines 219-262):** add a "Cap Cost" column for CALL positions. The table is already 87 chars wide; I'll widen to ~104 and add `CapCost` after Assign$. Needs current prices for each CALL's underlying:
- Build `stock_px: dict[ticker, price]` from two sources: `pf.stocks[ticker]['price']` for held names (free), batch-fetch unheld CALL underlyings via `MoomooClient.get_stock_snapshots` (the research confirms this is the batch method, `src/data/moomoo_client.py:97`).
- Per row: `cap = max(0, stock_px[ticker] − strike) × abs(qty) × 100` for CALLs; blank for PUTs.
- Subtotal row: `Σ CapCost` across open CCs.

**(b) New "WHEEL vs BUY-AND-HOLD" block** after the FOUNDATIONAL EQUITY block (lines ~290):
```
==========================================================================================
  🎯 WHEEL vs BUY-AND-HOLD (all-time)
==========================================================================================
  Net option income:              $X,XXX
  + Realized on assignments:      $X,XXX
  + Unrealized (open options):    $X,XXX
  + Unrealized (open shares):     $X,XXX
  ─────────────────────────────────────────
  WHEEL TOTAL RETURN:             $X,XXX

  If you'd simply bought & held the same shares:
  BUY-AND-HOLD RETURN:            $X,XXX

  Foregone upside (CCs called away):  $X,XXX   ← the cost of capping
  CSP basis advantage:               $X,XXX   ← the benefit of wheel basis
  ─────────────────────────────────────────
  WHEEL ADVANTAGE:                    $X,XXX   ← positive = wheel won
```

### 4. Tests — `tests/test_wheel_vs_hold.py`
Pure-function tests of the comparison engine with synthetic lots:
- Single CC_EXIT: S=$200, price=$240, qty=100 → foregone = $4,000
- Single CSP_ASSIGN: S=$200, premium=$5, price=$210, qty=100 → CSP advantage = $500 (the premium)
- Mixed: foundational lot + CC_EXIT + CSP_ASSIGN → wheel_advantage computed correctly
- Edge: CC called away when price < strike → foregone = 0 (no upside given up)
- Edge: CSP assigned then price below strike → buy-hold would've lost, wheel lost less by premium

### 5. `scripts/decision_review.py` — reuse, don't duplicate
The historical price fetch (`_fetch_history`/`_price_on`, lines 112-136) and the CC foregone-upside formula (lines 187-195) already exist. I'll extract the formula into `wheel_vs_hold.py` and have decision_review import it, rather than copying. This keeps one source of truth for "foregone upside."

## Design decisions baked in
- **"Foundational shares" = the SEED lots + CSP_ASSIGN lots still open** (from `get_open_lots()`). This is the buy-and-hold baseline. CC_EXIT lots are shares that left — their foregone upside is the cost column.
- **Pricing**: current price from moomoo batch snapshot (live), with yfinance fallback already in the client. For historical accuracy on called-away lots, "current price" is the right benchmark (opportunity cost is measured against what you *could have today*).
- **No double-counting**: CSP_ASSIGN `realized_pnl` in the ledger is the premium (already in option income), so the comparison uses `basis = strike − premium` for share appreciation, NOT the premium again.

## Verification
- `pytest tests/test_wheel_vs_hold.py` (new) + full suite green
- `python3 scripts/portfolio.py --pnl` shows the new Cap Cost column on open CCs and the WHEEL vs BUY-AND-HOLD block
- Sanity: wheel_advantage should be roughly `net_option_income + CSP_basis_advantage − CC_foregone_upside`. If it's wildly off, the per-ticker breakdown in the block lets you eyeball which line item is wrong.

## Out of scope
- **Per-position Cap Cost on CSPs** — CSPs cap *downside* (good thing), not upside, so there's no "foregone gain" to show. The CSP basis advantage is already captured in the all-time block.
- **Tax/forex adjustments** — pure pre-tax USD comparison.
- **Backtesting the comparison over time** — this is a point-in-time snapshot; a time series would need historical NLV snapshots (the OIE has these for paper, not for real).