# SPEC: 30% Margin Guardrail — System-Wide Enforcement

**Status**: Approved — Ready for Implementation
**Date**: 2026-07-18
**Branch**: main

---

## Problem

The config defines `max_margin_pct: 0.30` but **no code enforces it**. The `GuardrailChecker` issues only a WARNING (not a BLOCK), and is always called with `margin_used=0` because no script tracks real margin. CSPs are treated as pure cash-collateral (`strike × 100`), with no concept of margin headroom.

Additionally, CSP expiry dates aren't checked for concentration risk — if all puts expire the same week, simultaneous assignment could blow through the 30% cap even if each individual CSP looks safe.

### User's Rules
- **Never exceed 30% margin** utilization, even in extreme scenarios
- Margin capacity must govern **CSP expiry staggering** (can't cluster all liability in one week)
- Rule must be **config-driven** (`rules.yaml` → all scripts)
- Enforced in: **OIE engine**, **screener**, **portfolio check**

---

## Current State (What Exists)

| File | What's There | What's Missing |
|------|-------------|----------------|
| `config/rules.yaml:121` | `max_margin_pct: 0.30` | ✅ Already correct |
| `src/config.py:179` | `Config.max_margin_pct` property | ✅ Already exposes it |
| `src/data/guardrails.py:136-137` | Margin check as **WARNING** | Needs → BLOCK |
| `src/data/guardrails.py:103` | `margin_used` param, always passed as `0` | Needs real margin data |
| `src/risk/monitor.py:103-107` | `compute_margin_usage()` stub | Needs real margin model |
| `scripts/portfolio_check.py` | No margin display | Needs margin metrics |
| `scripts/screener.py` | No margin filter | Needs margin headroom gate |
| `scripts/oie_engine.py:523` | `buying_power = cash * 2` (hardcoded) | Needs real margin calc |

### Key Discovery
`scripts/portfolio_summary.py` already fetches `margin_used_pct` from moomoo's `accinfo_query` — proving live margin data is available. Currently `portfolio_check.py` calls the same API but discards the margin field.

---

## Margin Model

### How Moomoo Calculates Margin
- Reg T: stocks can be margined at 50% (lendable = stock_value × 0.50)
- Cash is used first for purchases
- Margin loan covers the shortfall
- `margin_used_pct = margin_loan / net_liquidation_value`

### Our Simplified Model (Conservative)
For CSP assignment scenario:
```
stock_collateral = stock_market_value × 0.50
max_margin_loan_allowed = net_liq × 0.30          # user's 30% rule
available_margin = stock_collateral - current_margin_loan
headroom = available_margin - (existing_csp_liability - cash)  # shortfall after cash
```

This is **more conservative** than moomoo's actual calculation — it treats the entire CSP liability as a potential margin loan, which is the worst-case scenario (all CSPs assigned simultaneously).

---

## Implementation

### Step 1: `src/risk/monitor.py` — Foundation Functions

Add after line 107 (after `compute_margin_usage`):

```python
def compute_margin_headroom(
    net_liq: float,
    cash: float,
    stock_mv: float,
    existing_csp_liability: float,
    current_margin_loan: float = 0.0,
    max_margin_pct: float = 0.30,
) -> dict:
    """
    How much additional CSP liability can be taken before exceeding max_margin_pct.

    Returns dict with:
      - headroom: additional CSP capital that can be assigned (float, can be negative)
      - max_margin_loan: the absolute dollar cap (net_liq * max_margin_pct)
      - current_implied_loan: margin loan if all CSPs assigned now
      - projected_loan: loan after absorbing all CSPs + headroom
      - pct_used: projected margin % if headroom fully used
      - available: cash + available_margin (total assignment capacity)
    """
    max_margin_loan = net_liq * max_margin_pct
    stock_collateral = stock_mv * 0.50

    # Implied loan: if all CSPs assigned today, how much would we borrow?
    implied_loan = max(0.0, existing_csp_liability - cash)
    # Available margin: how much we CAN borrow minus what we're already borrowing
    available_margin = max(0.0, stock_collateral - current_margin_loan)

    # Headroom: additional CSP capital we can take assignment on
    # Cap by both the margin limit AND available margin
    margin_headroom = max_margin_loan - implied_loan
    assignable = cash + available_margin  # total assignment absorption capacity

    return {
        'headroom': max(0.0, min(margin_headroom, assignable - existing_csp_liability)),
        'max_margin_loan': max_margin_loan,
        'current_implied_loan': implied_loan,
        'projected_loan': implied_loan,
        'pct_used': (implied_loan / net_liq * 100) if net_liq > 0 else 0.0,
        'available': assignable,
        'stock_collateral': stock_collateral,
        'csp_liability': existing_csp_liability,
    }


def compute_csp_expiry_concentration(
    open_csps: list[dict],
    cash: float,
    margin_headroom: float,
    max_concentration_pct: float = 0.30,
    net_liq: float = 0.0,
) -> dict:
    """
    Group CSPs by ISO week, flag weeks where total liability > capacity.

    Each CSP dict must have: strike, qty (abs), expiry (date or str).
    Returns: {weeks: {week: liability}, max_week: str, warnings: [str], all_clear: bool}
    """
    from collections import defaultdict
    from datetime import date, datetime

    weeks = defaultdict(float)
    for csp in open_csps:
        try:
            expiry = csp.get('expiry')
            if isinstance(expiry, str):
                expiry = date.fromisoformat(expiry) if 'T' not in expiry else datetime.fromisoformat(expiry).date()
            if isinstance(expiry, datetime):
                expiry = expiry.date()
            if isinstance(expiry, date):
                iso_week = expiry.isocalendar()[:2]  # (year, week)
            else:
                continue
        except (ValueError, TypeError):
            continue

        liability = abs(csp.get('strike', 0)) * abs(csp.get('qty', 0)) * 100
        weeks[iso_week] += liability

    warnings = []
    capacity = cash + margin_headroom
    max_liability = net_liq * max_concentration_pct if net_liq > 0 else float('inf')

    for (year, week), liability in sorted(weeks.items()):
        if liability > capacity:
            warnings.append(
                f"Week {week}/{year}: CSP liability ${liability:,.0f} > "
                f"capacity ${capacity:,.0f} (cash + margin headroom)"
            )
        elif net_liq > 0 and liability > max_liability:
            warnings.append(
                f"Week {week}/{year}: CSP liability ${liability:,.0f} > "
                f"{max_concentration_pct:.0%} of NLV (${max_liability:,.0f})"
            )

    max_week = max(weeks, key=weeks.get) if weeks else None
    return {
        'weeks': {f"{y}-W{w:02d}": v for (y, w), v in weeks.items()},
        'max_week': f"{max_week[0]}-W{max_week[1]:02d}" if max_week else None,
        'warnings': warnings,
        'all_clear': len(warnings) == 0,
    }


def validate_margin_for_new_csp(
    cash: float,
    net_liq: float,
    stock_mv: float,
    existing_csp_liability: float,
    new_csp_capital: float,
    current_margin_loan: float = 0.0,
    max_margin_pct: float = 0.30,
) -> tuple[bool, str]:
    """
    Check if adding a new CSP would exceed margin limits.

    Returns (allowed, reason). Blocks if projected margin > max_margin_pct.
    """
    total_liability = existing_csp_liability + new_csp_capital
    projected_loan = max(0.0, total_liability - cash)

    if net_liq <= 0:
        return False, "Net liq unknown — can't compute margin"

    projected_pct = (projected_loan / net_liq) * 100

    if projected_pct > max_margin_pct * 100:
        return False, (
            f"CSP margin {projected_pct:.1f}% > {max_margin_pct*100:.0f}% limit "
            f"(projected loan ${projected_loan:,.0f} on ${net_liq:,.0f} NLV)"
        )

    # Also check stock collateral cap
    stock_collateral = stock_mv * 0.50
    if projected_loan > stock_collateral:
        return False, (
            f"Projected loan ${projected_loan:,.0f} > stock collateral ${stock_collateral:,.0f} "
            f"(50% of ${stock_mv:,.0f} stock value)"
        )

    return True, (
        f"OK — {projected_pct:.1f}% margin, "
        f"${stock_collateral - projected_loan:,.0f} headroom remaining"
    )
```

### Step 2: `src/data/guardrails.py` — Upgrade Margin Checks

**Change 1** — Line 136-137: Margin over 30% becomes a BLOCK:
```python
# BEFORE:
if r.margin_used_pct > self.MAX_MARGIN_PCT() * 100:
    r.warnings.append(f"Margin {r.margin_used_pct:.1f}% > {self.MAX_MARGIN_PCT()*100:.0f}% limit.")

# AFTER:
if r.margin_used_pct > self.MAX_MARGIN_PCT() * 100:
    r.blocks.append(f"Margin {r.margin_used_pct:.1f}% > {self.MAX_MARGIN_PCT()*100:.0f}% limit. "
                    f"Reduce CSP exposure or add cash.")
```

**Change 2** — After line 123 (margin_used_pct calculation), compute implied margin from CSP liability if no real margin data passed:
```python
# AFTER line 123 (r.margin_used_pct = self._margin / self._net_liq * 100):
# If real margin data wasn't passed, estimate from CSP liability
if self._margin <= 0 and self._net_liq > 0:
    total_csp = sum(p.get('csp_liability', 0) for p in self._positions)
    implied_loan = max(0.0, total_csp - self._cash)
    r.margin_used_pct = (implied_loan / self._net_liq) * 100
```

**Change 3** — After line 177 (worst-case assignment), upgrade to BLOCK when shortfall exceeds margin:
```python
# BEFORE:
if r.worst_case_shortfall > 0:
    r.warnings.append(...)

# AFTER:
if r.worst_case_shortfall > 0:
    stock_collateral = sum(p.get('notional', 0) for p in self._positions) * 0.50
    if r.worst_case_shortfall > stock_collateral:
        r.blocks.append(
            f"⚠️  Worst-case CSP assignment ${csp_total:,.0f} exceeds all available funds. "
            f"Shortfall ${r.worst_case_shortfall:,.0f} > stock collateral ${stock_collateral:,.0f}. "
            f"Reduce CSP count immediately.")
    else:
        r.warnings.append(...)
```

**Change 4** — Add CSP expiry concentration import and call (top of file + in check()):
```python
# At top of file, add import:
from src.risk.monitor import compute_csp_expiry_concentration

# In check(), after worst-case assignment (line 183), add:
# ── CSP expiry concentration ──
if self._positions:
    csp_only = [p for p in self._positions if p.get('csp_liability', 0) > 0]
    if csp_only:
        conc = compute_csp_expiry_concentration(
            csp_only, self._cash, 0, self.MAX_MARGIN_PCT(), self._net_liq)
        if not conc['all_clear']:
            r.warnings.extend(conc['warnings'])
```

### Step 3: `scripts/portfolio_check.py` — Display Margin State

**Change 1** — In `_fetch_positions()`, capture `margin_used_pct`:
Find the `accinfo_query` section (around line 40 in `portfolio_summary.py` pattern) and add `margin_used_pct` to the return tuple. Currently the function returns `(stocks, options, cash, bp, fund)`. Change to return `(stocks, options, cash, bp, fund, margin_used_pct)`.

**Change 2** — Display margin in output (after line 62):
```python
margin_used_pct = margin_data.get('margin_used_pct', 0) if isinstance(margin_data, dict) else margin_data
print(f"💰 Liquid: ${liquid:,.0f} | BP: ${bp:,.0f} | "
      f"Margin: {margin_used_pct:.1f}% used / {cfg.max_margin_pct*100:.0f}% max | "
      f"{len(stocks)} stocks, {len(options)} options\n")

# Compute and show headroom
if stocks:
    stock_mv = sum(pos['qty'] * pos.get('price', pos.get('cost', 0)) for pos in stocks.values())
    total_csp = sum(
        abs(pos['strike']) * abs(pos['qty']) * 100
        for pos in options.values() if pos.get('type') == 'PUT'
    )
    from src.risk.monitor import compute_margin_headroom
    hr = compute_margin_headroom(
        net_liq=liquid + stock_mv,
        cash=cash + fund,
        stock_mv=stock_mv,
        existing_csp_liability=total_csp,
        current_margin_loan=0 if margin_used_pct == 0 else (margin_used_pct / 100) * (liquid + stock_mv),
    )
    print(f"🛡️  Margin headroom: ${hr['headroom']:,.0f} additional CSP assignment before 30% cap "
          f"(currently {hr['pct_used']:.1f}% implied)\n")
```

**Change 3** — Pass real margin to GuardrailChecker (around line 260-270 in portfolio_check.py):
```python
gc = GuardrailChecker(
    net_liq=net_liq, cash=liquid, buying_power=bp,
    margin_used=margin_used_pct / 100 * net_liq if margin_used_pct > 0 else 0.0,
    open_positions=gc_positions,
)
```

### Step 4: `scripts/screener.py` — Filter by Margin Headroom

**Change 1** — After fetching portfolio (line 197), compute margin headroom:
```python
from src.risk.monitor import compute_margin_headroom

# Compute existing CSP liability from live options
EXISTING_CSP = sum(
    strike * abs(qty) * 100
    for ticker, (strike, qty, expiry, opt_type) in EXISTING_OPTIONS.items()
    if opt_type == 'PUT'
)
stock_mv = sum(PORTFOLIO.values())
margin_hr = compute_margin_headroom(
    net_liq=stock_mv + CASH + FUND,
    cash=CASH + FUND,
    stock_mv=stock_mv,
    existing_csp_liability=EXISTING_CSP,
)
```

**Change 2** — In the CSP candidate loop (~line 348), add margin gate:
```python
# Add after line 364 (buying power gate):
# Margin headroom gate
if capital > margin_hr['headroom'] + CASH:
    log.debug(f"  {short} CSP ${c.strike:.0f} SKIP: capital ${capital:,.0f} > "
              f"margin headroom ${margin_hr['headroom']:,.0f} + cash ${CASH:,.0f}")
    continue
```

**Change 3** — Add margin info to TradeCandidate and output:
```python
# In TradeCandidate dataclass, add field:
margin_pct: float = 0.0  # % of 30% margin cap this trade would use

# When creating CSP candidate, populate:
margin_pct = round((capital - CASH) / (stock_mv + CASH + FUND) * 100, 1) if capital > CASH else 0.0
```

**Change 4** — Show margin summary in screener output header:
```python
print(f"🛡️  Margin: {margin_hr['pct_used']:.1f}% implied | "
      f"Headroom: ${margin_hr['headroom']:,.0f} | "
      f"CSP liability: ${EXISTING_CSP:,.0f} | "
      f"Max CSP add: ${margin_hr['headroom'] + CASH + FUND:,.0f}")
```

### Step 5: `scripts/oie_engine.py` — Margin-Aware Execution

**Change 1** — Line 523: Replace hardcoded buying power:
```python
# BEFORE:
gc = GuardrailChecker(net_liq=net_liq, cash=cash, buying_power=cash * 2, ...)

# AFTER:
stock_mv_only = sum(
    qty * (self._stock_prices.get(t, 0) or 0)
    for t, qty in self._real_portfolio.items())
margin_bp = cash + stock_mv_only * 0.50  # cash + 50% stock collateral
gc = GuardrailChecker(net_liq=net_liq, cash=cash, buying_power=margin_bp,
                       margin_used=implied_margin_loan, ...)
```

**Change 2** — Add margin validation before CSP execution (~line 550):
```python
# After the cash buffer check, add:
if c.strategy == 'CSP':
    ok, reason = validate_margin_for_new_csp(
        cash=cash, net_liq=net_liq, stock_mv=stock_mv_only,
        existing_csp_liability=existing_csp_total,
        new_csp_capital=c.capital_required,
    )
    if not ok:
        log.warning(f'{c.ticker} CSP BLOCKED: {reason}')
        events.append(f'🛡️ {c.ticker} CSP BLOCKED (margin): {reason}')
        continue
```

**Change 3** — Add CSP pause trigger check at start of `run_cycle()`:
```python
# After computing regime, add:
vix_val = self._macro.get('vix', 20) if self._macro else 20
regime_score = {'BULLISH': 2, 'NEUTRAL': 1, 'CAUTIOUS': 0, 'VOLATILE': -1, 'BEARISH': -2}.get(regime, 0)
csp_paused, pause_reasons = cfg.should_pause_csp(vix_val, regime_score, cash / net_liq)
if csp_paused:
    log.warning(f"CSP PAUSED: {'; '.join(pause_reasons)}")
    events.append(f'⏸️ CSP paused: {"; ".join(pause_reasons[:2])}')
```

**Change 4** — Add CSP expiry concentration check before execution:
```python
# Before executing candidates, check expiry concentration
csp_candidates = [c for c in candidates if c.strategy == 'CSP']
if csp_candidates:
    from src.risk.monitor import compute_csp_expiry_concentration
    # Build CSP list from existing + proposed
    all_csps = [{
        'strike': p['strike'], 'qty': abs(p['qty']),
        'expiry': p.get('expiry', ''),
    } for p in open_options if p['pos_type'] == 'PUT']
    conc = compute_csp_expiry_concentration(all_csps, cash, margin_headroom, 0.30, net_liq)
    if conc['warnings']:
        for w in conc['warnings']:
            events.append(f'⚠️ CSP concentration: {w}')
```

---

## Verification

```bash
# 1. Foundation tests
pytest tests/test_risk.py -v -k "margin"

# 2. Full suite must pass
pytest tests/ -v --tb=short

# 3. Portfolio check should show margin metrics
python3 scripts/portfolio_check.py

# 4. Screener should filter by margin and show headroom
python3 scripts/screener.py --top 5

# 5. OIE engine (dry-run) should enforce margin limits
python3 scripts/oie_engine.py --dry-run

# 6. Verify config tweak: change max_margin_pct in rules.yaml and re-run
#    — all scripts should pick up the new value
```

---

## Files Modified

| File | Changes | Risk |
|------|---------|------|
| `src/risk/monitor.py` | +3 functions (~120 lines) | Low — pure functions, no side effects |
| `src/data/guardrails.py` | 4 changes (~30 lines) | Medium — upgrades WARNING→BLOCK |
| `scripts/portfolio_check.py` | 3 changes (~25 lines) | Low — display + pass data |
| `scripts/screener.py` | 4 changes (~35 lines) | Medium — adds filter gate |
| `scripts/oie_engine.py` | 4 changes (~40 lines) | Medium — adds execution guard |
| `config/rules.yaml` | No changes | — |

---

## Edge Cases

1. **No stock positions**: `stock_collateral = 0` → all CSP must be fully cash-covered. Margin headroom = 0.
2. **Negative cash** (margin loan already exists): `implied_loan` calculation still correct (csp_liability - negative_cash = larger loan).
3. **Zero/unknown net liq**: All `validate_*` functions return `(False, reason)` immediately.
4. **Config change mid-session**: `reload_config()` handles this — all scripts call `get_config()` which caches; call `reload_config()` explicitly after editing `rules.yaml`.
5. **Single CSP already at 29%**: Adding any CSP would exceed 30% → blocked. The error message includes projected percentage.
