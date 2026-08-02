#!/usr/bin/env python3
"""
Decision Review — compare actual option-trading decisions (close / roll / re-open)
against a hypothetical "hold to expiry" baseline over the past 2 months.

For every option contract that was opened (SOLD / SELL_SHORT) and closed
(BOUGHT / BUY_BACK) during the window, this script:

  1. Reconstructs the lifecycle: entry premium, exit cost, actual realized P&L.
  2. Looks up the underlying's price at the ORIGINAL expiry date (yfinance).
  3. Computes what would have happened IF the position had been held to expiry:
       OTM  → keep full premium (no buy-back cost).
       ITM  → assigned (CSP buy shares / CC shares called) — nets the missed
              upside or avoided downside against the premium kept.
  4. Attribute the difference to the decision (close / roll / re-open).
  5. Rolls everything up by contract, by ticker, and by decision type.

Usage:
    python3 scripts/decision_review.py                 # default: 60 days
    python3 scripts/decision_review.py --days 90       # wider window
    python3 scripts/decision_review.py --ticker V      # single ticker
"""
import argparse
import sys
import os
import warnings
from collections import defaultdict
from datetime import date, timedelta

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.portfolio_loader import (
    fetch_portfolio_and_orders, fetch_portfolio,
    parse_option_code, is_option_code,
)
from src.portfolio.summary import compute_income

# ────────────────────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────────────────────

class ContractLifecycle:
    """A single contract code (ticker + expiry + type + strike) across the window."""
    def __init__(self, code: str):
        self.code = code
        parsed = parse_option_code(code)
        self.ticker, self.expiry, self.opt_type, self.strike = parsed if parsed else ('', '', '', 0.0)
        self.entries: list[dict] = []      # each SOLD leg: {date, qty, price}
        self.exits: list[dict] = []        # each BOUGHT leg: {date, qty, price}
        self.realized_pnl = 0.0
        # Cash-flow convention: short option → sells credit cash in, buys take cash out.

    @property
    def is_cc(self) -> bool:
        """Covered Call = CALL (short call against owned shares)."""
        return self.opt_type == 'CALL'

    @property
    def is_csp(self) -> bool:
        """Cash-Secured Put = PUT (short put against cash)."""
        return self.opt_type == 'PUT'

    @property
    def total_premium_received(self) -> float:
        return sum(e['price'] * e['qty'] * 100 for e in self.entries)

    @property
    def total_buyback_paid(self) -> float:
        return sum(x['price'] * x['qty'] * 100 for x in self.exits)

    @property
    def contract_count(self) -> float:
        """Number of contracts that had an OPENING sell during the window."""
        return sum(e['qty'] for e in self.entries)

    @property
    def contract_count_closed(self) -> float:
        """Number of contracts actually closed (bought back) during the window."""
        return sum(x['qty'] for x in self.exits)

    @property
    def is_fully_closed(self) -> bool:
        """All contracts that were opened during the window also got closed."""
        return abs(self.contract_count - self.contract_count_closed) < 0.01 and self.contract_count > 0

    @property
    def entry_dates(self) -> list[str]:
        return [e['date'] for e in self.entries]

    @property
    def exit_dates(self) -> list[str]:
        return [x['date'] for x in self.exits]

    @property
    def decision_label(self) -> str:
        """Classify the lifecycle's overall decision."""
        if not self.entries:
            return 'NO_OPEN'
        if self.is_fully_closed:
            return 'CLOSED'
        # Some contracts still open → were rolled or are still running
        closed = self.contract_count_closed
        opened = self.contract_count
        if closed > 0 and opened > 0:
            return 'PARTIAL_CLOSE'
        return 'STILL_OPEN'


def _fetch_history(ticker: str, start: str, end: str) -> dict:
    """Fetch daily closes from yfinance for [start, end). Returns {ISO-date: close}."""
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        hist = t.history(start=start, end=end, interval='1d')
        if hist is None or len(hist) == 0:
            return {}
        out = {}
        for dt, row in hist.iterrows():
            # yfinance returns tz-aware datetime index
            out[dt.strftime('%Y-%m-%d')] = float(row.get('Close', 0) or 0)
        return out
    except Exception:
        return {}


def _price_on(history: dict, target: str) -> float | None:
    """Get the close on target date, or the last available close <= target."""
    if target in history:
        return history[target]
    before = [k for k in sorted(history) if k <= target]
    if before:
        return history[before[-1]]
    return None

# ────────────────────────────────────────────────────────────────────
# Core analysis
# ────────────────────────────────────────────────────────────────────

def analyze_lifecycle(lc: ContractLifecycle, stock_history: dict, today: date,
                      current_price: float | None = None) -> dict:
    """Analyze one contract lifecycle against a hold-to-expiry baseline.

    Returns a dict with:
        actual_realized, hold_pnl, diff, verdict, expiry_price, otm_at_expiry.
    Only contracts whose expiry has PASSED get a definitive verdict — contracts
    with future expiry are marked 'UNDECIDED' (their hold outcome is unknown).
    """
    # ── Actual realized P&L across the window ──
    actual = lc.total_premium_received - lc.total_buyback_paid

    # ── Hypothetical: hold every contract to ORIGINAL expiry ──
    pnl_if_held = None
    expiry_price = None
    otm_at_expiry = None
    expiry_dt = None
    try:
        expiry_dt = date.fromisoformat(lc.expiry) if lc.expiry else None
    except ValueError:
        expiry_dt = None

    # Premium kept if never bought back
    premium_kept = lc.total_premium_received

    # Determine if the outcome is knowable
    expiry_passed = bool(expiry_dt and expiry_dt < today)

    if expiry_passed:
        # Expiry has passed — we know the exact outcome.
        expiry_price = _price_on(stock_history, lc.expiry)
        if expiry_price is None and current_price is not None:
            # No yfinance history but we have a live snapshot — use it as proxy
            expiry_price = current_price

        if expiry_price is not None:
            if lc.is_csp:  # PUT
                if expiry_price >= lc.strike:
                    otm_at_expiry = True
                    pnl_if_held = premium_kept   # expired worthless, keep all premium
                else:
                    otm_at_expiry = False
                    # Assigned at strike when stock is below → mark-to-market loss
                    shares = lc.contract_count * 100
                    pnl_if_held = premium_kept + (expiry_price - lc.strike) * shares
            else:  # CALL
                if expiry_price <= lc.strike:
                    otm_at_expiry = True
                    pnl_if_held = premium_kept   # expired worthless, keep all premium
                else:
                    otm_at_expiry = False
                    # Shares called away at strike → missed upside above strike
                    shares = lc.contract_count * 100
                    pnl_if_held = premium_kept - (expiry_price - lc.strike) * shares
        # else: no price data → undetermined (can't say either way)
        elif expiry_price is None:
            # Fall back to premium-kept as a rough proxy in the undetermined set
            pnl_if_held = premium_kept
            verdict = 'UNDETERMINED'
            return {
                'lc': lc,
                'actual_realized': actual,
                'hold_pnl': pnl_if_held,
                'diff': 0.0,
                'verdict': verdict,
                'expiry_price': None,
                'otm_at_expiry': None,
                'expiry_passed': True,
                'expiry_dt': expiry_dt,
            }
    else:
        # Expiry still in the future — outcome unknowable.
        # We can still show the ACTUAL realized result of the management,
        # but we cannot claim hold would have been better/worse.
        verdict = 'UNDECIDED'
        expiry_price = current_price  # reference only, not an outcome
        if current_price is not None:
            # Estimate direction for context
            if lc.is_csp:
                otm_at_expiry = current_price >= lc.strike
            else:
                otm_at_expiry = current_price <= lc.strike

        return {
            'lc': lc,
            'actual_realized': actual,
            'hold_pnl': None,          # unknown
            'diff': 0.0,
            'verdict': verdict,
            'expiry_price': expiry_price,
            'otm_at_expiry': otm_at_expiry,
            'expiry_passed': False,
            'expiry_dt': expiry_dt,
        }

    diff = actual - pnl_if_held
    if diff > 1:
        verdict = 'DECISION_HELPED'      # active mgmt beat holding
    elif diff < -1:
        verdict = 'ACTIVE_HURT'          # holding was better
    else:
        verdict = 'NEUTRAL'              # about the same

    return {
        'lc': lc,
        'actual_realized': actual,
        'hold_pnl': pnl_if_held,
        'diff': diff,
        'verdict': verdict,
        'expiry_price': expiry_price,
        'otm_at_expiry': otm_at_expiry,
        'expiry_passed': True,
        'expiry_dt': expiry_dt,
    }


def load_orders(start_date: str):
    """Fetch portfolio + orders from moomoo for the window."""
    pf, orders = fetch_portfolio_and_orders(start=start_date)
    return pf, orders


def build_lifecycles(orders: list[dict]) -> dict[str, ContractLifecycle]:
    """Group option orders into per-contract lifecycles."""
    lcs: dict[str, ContractLifecycle] = {}
    for o in orders:
        if o.get('status') not in ('FILLED_ALL', 'FILLED_PART'):
            continue
        code = str(o.get('code', ''))
        if not is_option_code(code):
            continue
        side = str(o.get('side', ''))
        qty = abs(o.get('qty', 0) or 0)
        price = o.get('price', 0) or 0
        odate = str(o.get('date', ''))[:10]
        if qty <= 0:
            continue

        lc = lcs.setdefault(code, ContractLifecycle(code))
        if side in ('SELL', 'SELL_SHORT'):
            lc.entries.append({'date': odate, 'qty': qty, 'price': price})
        elif side in ('BUY', 'BUY_BACK'):
            lc.exits.append({'date': odate, 'qty': qty, 'price': price})
    return lcs


# ────────────────────────────────────────────────────────────────────
# Printing
# ────────────────────────────────────────────────────────────────────

def fmt_money(v: float, width: int = 10) -> str:
    return f'{v:>+{width},.0f}'


def print_report(lcs: dict[str, ContractLifecycle], today: date,
                 current_prices: dict[str, float]) -> None:
    # ── Build analysis rows ──
    rows = []
    for code, lc in sorted(lcs.items()):
        hist = _cached_history(lc.ticker)
        cp = current_prices.get(lc.ticker)
        rows.append(analyze_lifecycle(lc, hist, today, cp))

    # Filter: only lifecycles with an OPEN (sell) during the window
    rows = [r for r in rows if r['lc'].entries]

    # Sort: closed first, then by expiry, then ticker
    rows.sort(key=lambda r: (not r['lc'].is_fully_closed, r['lc'].expiry, r['lc'].ticker, r['lc'].strike))

    # ════════════════════════════════════════════════════════════════
    # HEADER
    # ════════════════════════════════════════════════════════════════
    print(f"{'='*120}")
    print(f"  🔍 DECISION REVIEW — HOLD-to-EXPIRY vs ACTUAL MANAGEMENT")
    print(f"  Window: {min((e['date'] for lc in lcs.values() for e in lc.entries), default='?')} → {today.isoformat()}")
    print(f"  {len(rows)} option contracts opened in the window")
    print(f"{'='*120}\n")

    # ════════════════════════════════════════════════════════════════
    # PER-CONTRACT TABLE
    # ════════════════════════════════════════════════════════════════
    print(f"{'Contract':<34s} {'Type':<4s} {'Qty':>3s} {'Expiry':<12s} "
          f"{'Entry$':>8s} {'Exit$':>8s} {'Actual$':>10s} {'Hold$':>10s} "
          f"{'Diff$':>10s} {'Verdict':<16s} {'ExpiryPx':>9s}")
    print(f"{'-'*34} {'-'*4} {'-'*3} {'-'*12} {'-'*8} {'-'*8} {'-'*10} {'-'*10} "
          f"{'-'*10} {'-'*16} {'-'*9}")
    totals_actual = 0.0
    totals_hold = 0.0
    verdict_counts = defaultdict(int)
    for r in rows:
        lc = r['lc']
        code_label = f"{lc.ticker} {'P' if lc.is_csp else 'C'} ${lc.strike:.0f} {lc.expiry}"
        entry_avg = (lc.total_premium_received / lc.contract_count / 100) if lc.contract_count else 0
        exit_avg = (lc.total_buyback_paid / lc.contract_count_closed / 100) if lc.contract_count_closed else 0
        verdict_emoji_map = {
            'DECISION_HELPED': '✅ HELPED',
            'ACTIVE_HURT': '❌ HURT',
            'NEUTRAL': '➖ NEUTRAL',
            'UNDECIDED': '⏳ UNDECIDED',
            'UNDETERMINED': '❔ UNDETERMINED',
        }
        verdict_emoji = verdict_emoji_map.get(r['verdict'], '❓ UNKNOWN')
        ep = r['expiry_price']
        ep_str = f'{ep:,.0f}' if ep else 'N/A'
        hold_str = fmt_money(r['hold_pnl']) if r['hold_pnl'] is not None else f'{"?":>10s}'
        diff_str = fmt_money(r['diff']) if r['diff'] is not None else f'{"?":>10s}'
        # Current status hint for undecided: OTM = likely expire worthless, ITM = would assign
        cur_hint = ''
        if r['verdict'] in ('UNDECIDED', 'UNDETERMINED') and r['otm_at_expiry'] is not None:
            cur_hint = ' (OTM now)' if r['otm_at_expiry'] else ' (ITM now)'
        print(f"{code_label:<34s} {'CSP' if lc.is_csp else 'CC ':<4s} {lc.contract_count:>3.0f} "
              f"{lc.expiry:<12s} {entry_avg:>7,.2f}$ {exit_avg:>7,.2f}$ "
              f"{fmt_money(r['actual_realized']):>10s} {hold_str:>10s} "
              f"{diff_str:>10s} {verdict_emoji:<16s} {ep_str:>9s}{cur_hint}")
        totals_actual += r['actual_realized']
        if r['hold_pnl'] is not None:
            totals_hold += r['hold_pnl']
        verdict_counts[r['verdict']] += 1

    print(f"{'-'*34} {'-'*4} {'-'*3} {'-'*12} {'-'*8} {'-'*8} {'-'*10} {'-'*10} "
          f"{'-'*10} {'-'*16} {'-'*9}")
    print(f"{'TOTAL':<34s} {'':<4s} {'':<3s} {'':<12s} {'':<8s} {'':<8s} "
          f"{fmt_money(totals_actual):>10s} {fmt_money(totals_hold):>10s} "
          f"{fmt_money(totals_actual - totals_hold):>10s}  (decided contracts only)")
    print()

    # ════════════════════════════════════════════════════════════════
    # SUMMARY — VERDICT BREAKDOWN
    # ════════════════════════════════════════════════════════════════
    decided = [r for r in rows if r['verdict'] in ('DECISION_HELPED', 'ACTIVE_HURT', 'NEUTRAL')]
    helped = sum(r['diff'] for r in rows if r['verdict'] == 'DECISION_HELPED')
    hurt = sum(r['diff'] for r in rows if r['verdict'] == 'ACTIVE_HURT')
    decided_actual = sum(r['actual_realized'] for r in decided)
    decided_hold = sum(r['hold_pnl'] for r in decided)
    undecided_actual = sum(r['actual_realized'] for r in rows if r['verdict'] in ('UNDECIDED', 'UNDETERMINED'))
    undecided_n = sum(1 for r in rows if r['verdict'] in ('UNDECIDED', 'UNDETERMINED'))
    print(f"{'='*120}")
    print(f"  📊 SUMMARY")
    print(f"{'='*120}")
    print(f"  Contracts with DEFINITIVE verdicts:  {len(decided)}")
    print(f"  Active mgmt realized P&L:     {fmt_money(decided_actual):>12s}")
    print(f"  Hypothetical hold-to-expiry:  {fmt_money(decided_hold):>12s}")
    print(f"  Net decision impact:          {fmt_money(decided_actual - decided_hold):>12s}")
    print()
    print(f"  Contracts where ACTIVE mgmt HELPED:  {verdict_counts['DECISION_HELPED']}  (sum {fmt_money(helped):>9s})")
    print(f"  Contracts where ACTIVE mgmt HURT:    {verdict_counts['ACTIVE_HURT']}  (sum {fmt_money(hurt):>9s})")
    print(f"  Neutral:                            {verdict_counts['NEUTRAL']}")
    print(f"  ⏳ Undecided (expiry not passed yet): {undecided_n}  "
          f"(their actual realized = {fmt_money(undecided_actual)})")
    print()

    # ════════════════════════════════════════════════════════════════
    # BY TICKER
    # ════════════════════════════════════════════════════════════════
    print(f"{'='*120}")
    print(f"  🏷️  BY TICKER (decided contracts only in Hold/Diff columns; "
          f"UNDECIDED shown for tickers with no passed expiry yet)")
    print(f"{'='*120}")
    tickers = defaultdict(lambda: {'actual': 0.0, 'hold': 0.0, 'n': 0, 'ndecided': 0, 'helped': 0, 'hurt': 0})
    for r in rows:
        t = r['lc'].ticker
        tickers[t]['actual'] += r['actual_realized']
        if r['hold_pnl'] is not None:
            tickers[t]['hold'] += r['hold_pnl']
            tickers[t]['ndecided'] += 1
        tickers[t]['n'] += 1
        if r['verdict'] == 'DECISION_HELPED':
            tickers[t]['helped'] += 1
        elif r['verdict'] == 'ACTIVE_HURT':
            tickers[t]['hurt'] += 1
    print(f"  {'Ticker':<8s} {'#':>3s} {'Actual$':>10s} {'Hold$':>10s} {'Diff$':>10s}  {'Verdict'}")
    for t in sorted(tickers):
        r = tickers[t]
        if r['ndecided'] == 0:
            # No definitive verdict yet — all expired contracts are still in the future
            print(f"  {t:<8s} {r['n']:>3d} {fmt_money(r['actual']):>10s} "
                  f"{'?':>10s} {'?':>10s}  ⏳ UNDECIDED")
        else:
            diff = r['actual'] - r['hold']
            # Only count actual from decided contracts for fair comparison
            decided_actual = sum(x['actual_realized'] for x in rows
                                 if x['lc'].ticker == t and x['hold_pnl'] is not None)
            diff2 = decided_actual - r['hold']
            verdict = '✅ HELPED' if diff2 > 50 else ('❌ HURT' if diff2 < -50 else '➖ NEUTRAL')
            tag = f" ({r['ndecided']}/{r['n']} decided)" if r['ndecided'] != r['n'] else ''
            print(f"  {t:<8s} {r['n']:>3d} {fmt_money(r['actual']):>10s} {fmt_money(r['hold']):>10s} "
                  f"{fmt_money(diff2):>10s}  {verdict}{tag}")
    print()

    # ════════════════════════════════════════════════════════════════
    # BY DECISION TYPE
    # ════════════════════════════════════════════════════════════════
    print(f"{'='*120}")
    print(f"  🎯 BY DECISION TYPE")
    print(f"{'='*120}")
    by_dec = defaultdict(lambda: {'actual': 0.0, 'hold': 0.0, 'n': 0, 'ndecided': 0,
                                  'decided_actual': 0.0})
    for r in rows:
        label = r['lc'].decision_label
        by_dec[label]['actual'] += r['actual_realized']
        if r['hold_pnl'] is not None:
            by_dec[label]['hold'] += r['hold_pnl']
            by_dec[label]['decided_actual'] += r['actual_realized']
            by_dec[label]['ndecided'] += 1
        by_dec[label]['n'] += 1
    for d in sorted(by_dec):
        v = by_dec[d]
        tag = f" ({v['ndecided']}/{v['n']} decided)" if v['ndecided'] != v['n'] else ''
        diff = v.get('decided_actual', v['actual']) - v['hold']
        # Show only decided numbers for diff when partial
        if v['ndecided'] != v['n']:
            print(f"  {d:<18s} n={v['n']:>2d}  actual(all)={fmt_money(v['actual']):>10s}  "
                  f"hold={fmt_money(v['hold']):>10s}  diff={fmt_money(diff):>10s}{tag}")
        else:
            print(f"  {d:<18s} n={v['n']:>2d}  actual={fmt_money(v['actual']):>10s}  "
                  f"hold={fmt_money(v['hold']):>10s}  diff={fmt_money(diff):>10s}")
    print()

    # ════════════════════════════════════════════════════════════════
    # DETAIL — WORST DECISIONS (active hurt the most)
    # ════════════════════════════════════════════════════════════════
    worst = sorted([r for r in rows if r['verdict'] == 'ACTIVE_HURT'], key=lambda r: r['diff'])[:8]
    if worst:
        print(f"{'='*120}")
        print(f"  ⚠️  WORST DECISIONS — where holding to expiry would have been better")
        print(f"{'='*120}")
        for r in worst:
            lc = r['lc']
            print(f"\n  ❌ {lc.ticker} {'CSP' if lc.is_csp else 'CC'} ${lc.strike:.0f} exp {lc.expiry}")
            print(f"     Actual realized:  {fmt_money(r['actual_realized'])}  (prem {fmt_money(lc.total_premium_received)} − buyback {fmt_money(lc.total_buyback_paid)})")
            print(f"     Hold to expiry:   {fmt_money(r['hold_pnl'])}")
            print(f"     Decision impact:  {fmt_money(r['diff'])}")
            if r['expiry_price']:
                status = 'OTM' if r['otm_at_expiry'] else 'ITM/assigned'
                print(f"     Underlying @ expiry: ${r['expiry_price']:.2f} vs strike ${lc.strike:.0f} → {status}")

    # ════════════════════════════════════════════════════════════════
    # DETAIL — BEST DECISIONS (active helped the most)
    # ════════════════════════════════════════════════════════════════
    best = sorted([r for r in rows if r['verdict'] == 'DECISION_HELPED'], key=lambda r: r['diff'], reverse=True)[:8]
    if best:
        print(f"\n{'='*120}")
        print(f"  ✅ BEST DECISIONS — where active management beat holding")
        print(f"{'='*120}")
        for r in best:
            lc = r['lc']
            print(f"\n  ✅ {lc.ticker} {'CSP' if lc.is_csp else 'CC'} ${lc.strike:.0f} exp {lc.expiry}")
            print(f"     Actual realized:  {fmt_money(r['actual_realized'])}  (prem {fmt_money(lc.total_premium_received)} − buyback {fmt_money(lc.total_buyback_paid)})")
            print(f"     Hold to expiry:   {fmt_money(r['hold_pnl'])}")
            print(f"     Decision impact:  {fmt_money(r['diff'])}")
            if r['expiry_price']:
                status = 'OTM' if r['otm_at_expiry'] else 'ITM/assigned'
                print(f"     Underlying @ expiry: ${r['expiry_price']:.2f} vs strike ${lc.strike:.0f} → {status}")


# ────────────────────────────────────────────────────────────────────
# PROFIT TARGET ANALYSIS — 50% vs 80% vs 100% (expiry)
# ────────────────────────────────────────────────────────────────────

def analyze_profit_targets(lcs: dict[str, ContractLifecycle], today: date,
                           current_prices: dict[str, float]) -> None:
    """Compare booking at 50% profit vs holding to 80% or to 100% expiry.

    For each contract that had BOTH an opening sell AND a buy-back (a
    management decision), if the buy-back cost was less than the entry premium
    (i.e. it was profitable when closed), simulate:
      - ACTUAL: what was realized by booking at whatever % captured
      - 80% TARGET: if the option was OTM at expiry, holding to 80% was
        definitely achievable (premium kept decaying to ~0). If ITM at
        expiry, the profitable exit is uncertain — use the expiry outcome.
      - 100% (EXPIRY): full hold-to-expiry economic result (same model as
        analyze_lifecycle).
    Reports the opportunity cost of early booking.
    """
    rows = []
    for code, lc in sorted(lcs.items()):
        if not lc.entries or not lc.exits:
            continue  # only contracts with both open AND close actions
        # Was it closed at a profit? (buyback total < premium received total)
        actual = lc.total_premium_received - lc.total_buyback_paid
        if actual <= 0:
            continue  # not a profitable close — skip loss-side analysis

        hist = _cached_history(lc.ticker)
        cp = current_prices.get(lc.ticker)
        expiry_dt = None
        try:
            expiry_dt = date.fromisoformat(lc.expiry) if lc.expiry else None
        except ValueError:
            expiry_dt = None

        # Just use the full lifecycle analysis to get expiry outcome
        full = analyze_lifecycle(lc, hist, today, cp)

        # The 80% scenario:
        #   If OTM at expiry → the option decayed to ~0 → 80% was certain.
        #   If ITM at expiry → we cannot guarantee 80%; use expiry outcome.
        if full['otm_at_expiry'] is True:
            pnl_at_80 = lc.total_premium_received * 0.80
        else:
            pnl_at_80 = full['hold_pnl'] if full['hold_pnl'] is not None else lc.total_premium_received * 0.80

        pnl_at_100 = full['hold_pnl'] if full['hold_pnl'] is not None else lc.total_premium_received

        # Profit captured % at actual close (per entry premium)
        profit_captured_pct = (actual / lc.total_premium_received * 100) if lc.total_premium_received else 0

        rows.append({
            'lc': lc,
            'actual': actual,
            'pnl_80': pnl_at_80,
            'pnl_100': pnl_at_100,
            'profit_captured_pct': profit_captured_pct,
            'otm_at_expiry': full['otm_at_expiry'],
            'expiry_price': full['expiry_price'],
            'expiry_passed': full['expiry_passed'],
        })

    if not rows:
        print("No profitably-closed contracts found in the window.")
        return

    rows.sort(key=lambda r: (r['actual'] - r['pnl_100'], r['lc'].ticker, r['lc'].expiry))

    print(f"{'='*120}")
    print(f"  🎯 PROFIT TARGET ANALYSIS — 50% Booked vs 80% vs 100% Expiry")
    print(f"  Contracts closed at a PROFIT during window: {len(rows)}")
    print(f"  Assumption: OTM @ expiry → option decayed to ~0, so 80% was certainly reachable.")
    print(f"{'='*120}\n")

    print(f"{'Contract':<34s} {'Type':<4s} {'Capt%':>6s} {'Actual$':>9s} "
          f"{'@80%$':>9s} {'@100%$':>9s} {'OppCost80':>10s} {'OppCost100':>11s}  {'ExpiryPx':>8s}")
    print(f"{'-'*34} {'-'*4} {'-'*6} {'-'*9} {'-'*9} {'-'*9} {'-'*10} {'-'*11} {'-'*8}")

    tot_actual = tot_80 = tot_100 = 0.0
    opp_80_total = opp_100_total = 0.0
    n_80_better = n_100_better = n_actual_better = 0

    for r in rows:
        lc = r['lc']
        code_label = f"{lc.ticker} {'P' if lc.is_csp else 'C'} ${lc.strike:.0f} {lc.expiry}"
        opp_80 = r['actual'] - r['pnl_80']       # positive = booking 50% beat holding to 80%
        opp_100 = r['actual'] - r['pnl_100']     # positive = booking beat holding to expiry

        ep = r['expiry_price']
        ep_str = f'{ep:,.0f}' if ep else 'N/A'
        status_tag = ''
        if r['expiry_passed']:
            status_tag = ' OTM' if r['otm_at_expiry'] is True else ' ITM'
            if r['otm_at_expiry'] is None:
                status_tag = ' ?'
        else:
            status_tag = ' (future)'

        print(f"{code_label:<34s} {'CSP' if lc.is_csp else 'CC ':<4s} "
              f"{r['profit_captured_pct']:>5.0f}% {fmt_money(r['actual']):>9s} "
              f"{fmt_money(r['pnl_80']):>9s} {fmt_money(r['pnl_100']):>9s} "
              f"{fmt_money(opp_80):>10s} {fmt_money(opp_100):>11s}  {ep_str:>8s}{status_tag}")

        tot_actual += r['actual']
        tot_80 += r['pnl_80']
        tot_100 += r['pnl_100']
        opp_80_total += opp_80
        opp_100_total += opp_100
        if opp_80 > 1:
            n_80_better += 1
        if opp_100 > 1:
            n_100_better += 1
        if opp_100 < -1:
            n_actual_better += 1

    print(f"{'-'*34} {'-'*4} {'-'*6} {'-'*9} {'-'*9} {'-'*9} {'-'*10} {'-'*11} {'-'*8}")
    print(f"{'TOTAL':<34s} {'':<4s} {'':<6s} {fmt_money(tot_actual):>9s} "
          f"{fmt_money(tot_80):>9s} {fmt_money(tot_100):>9s} "
          f"{fmt_money(opp_80_total):>10s} {fmt_money(opp_100_total):>11s}")
    print()

    print(f"{'='*120}")
    print(f"  📊 PROFIT-TARGET SUMMARY")
    print(f"{'='*120}")
    print(f"  Actual (booked at ~50%):     {fmt_money(tot_actual):>12s}")
    print(f"  If held to 80% of premium:   {fmt_money(tot_80):>12s}   (diff {fmt_money(tot_80 - tot_actual):>10s})")
    print(f"  If held to 100% (expiry):    {fmt_money(tot_100):>12s}   (diff {fmt_money(tot_100 - tot_actual):>10s})")
    print()
    print(f"  Contracts where booking at 50% was BETTER than 80%:   {n_80_better}")
    print(f"  Contracts where booking at 50% was BETTER than 100%:  {n_100_better}")
    print(f"  Contracts where 100% (expiry) was better:             {n_actual_better}")
    print()

    # Opportunity-cost detail — where the 50% booking left money on the table
    opp_cost = [r for r in rows if r['pnl_100'] - r['actual'] > 50]
    opp_cost.sort(key=lambda r: r['pnl_100'] - r['actual'], reverse=True)
    if opp_cost:
        print(f"{'='*120}")
        print(f"  💸 OPPORTUNITY COST — contracts where holding to expiry would have earned MORE")
        print(f"{'='*120}")
        for r in opp_cost[:10]:
            lc = r['lc']
            lost = r['pnl_100'] - r['actual']
            print(f"\n  💸 {lc.ticker} {'CSP' if lc.is_csp else 'CC'} ${lc.strike:.0f} exp {lc.expiry}")
            print(f"     Booked at {r['profit_captured_pct']:.0f}% → realized {fmt_money(r['actual'])}")
            print(f"     Held to 80% → {fmt_money(r['pnl_80'])}  |  Held to expiry → {fmt_money(r['pnl_100'])}")
            print(f"     Opportunity cost (100% - actual): {fmt_money(lost)}")
            if r['expiry_price']:
                status = 'OTM' if r['otm_at_expiry'] else 'ITM/assigned'
                print(f"     Underlying @ expiry: ${r['expiry_price']:.2f} vs strike ${lc.strike:.0f} → {status}")


# ── History cache ───────────────────────────────────────────────────
_history_cache = {}

def _cached_history(ticker: str) -> dict:
    if ticker not in _history_cache:
        # Fetch 1 year of history to cover all expiries in the window
        _history_cache[ticker] = _fetch_history(ticker, '2026-01-01', '2026-12-31')
    return _history_cache[ticker]


def main():
    parser = argparse.ArgumentParser(description='Decision review: actual vs hold-to-expiry')
    parser.add_argument('--days', type=int, default=60, help='Window size in days (default 60)')
    parser.add_argument('--ticker', type=str, default=None, help='Filter to one ticker')
    parser.add_argument('--no-current', action='store_true', help='Skip live current-price snapshots')
    parser.add_argument('--profit-targets', action='store_true',
                        help='Also run 50%%/80%%/100%% profit-booking hypothetical')
    args = parser.parse_args()

    today = date.today()
    start = (today - timedelta(days=args.days)).isoformat()

    print(f"📋 Loading portfolio + orders since {start}...", end=' ', flush=True)
    pf, orders = fetch_portfolio_and_orders(start=start)
    if not pf.stocks and pf.funds.cash == 0 and pf.funds.fund == 0 and not orders:
        print('❌ Cannot connect to moomoo OpenD. Aborting.')
        sys.exit(1)
    print(f"{len(pf.stocks)} stocks, {len(pf.options)} options, {len(orders)} orders")

    # Build lifecycles
    lcs = build_lifecycles(orders)
    if args.ticker:
        tup = args.ticker.upper().replace('US.', '')
        lcs = {c: lc for c, lc in lcs.items() if lc.ticker == tup}
    if not lcs:
        print("No option contracts found in the window.")
        return

    # Current prices from live portfolio, then yfinance for the rest (or skip)
    current_prices = {}
    if not args.no_current:
        for t in {lc.ticker for lc in lcs.values()}:
            pos = pf.stocks.get(t)
            if pos:
                current_prices[t] = pos.get('price', 0)
        for code, o in pf.options.items():
            t = o.get('ticker', '')
            if t and t not in current_prices:
                current_prices[t] = o.get('price', 0)
        # Fetch from yfinance for tickers not held in the portfolio
        missing = {lc.ticker for lc in lcs.values()} - set(current_prices)
        if missing:
            print(f"📡 Fetching current prices for {sorted(missing)}...", end=' ', flush=True)
            import yfinance as yf
            for t in sorted(missing):
                try:
                    hist = yf.Ticker(t).history(period='5d', interval='1d')
                    if hist is not None and len(hist) > 0:
                        current_prices[t] = float(hist.iloc[-1]['Close'])
                except Exception:
                    pass
            print("done")

    print_report(lcs, today, current_prices)

    # ── Profit-target hypothetical (optional) ──
    if args.profit_targets:
        print()
        analyze_profit_targets(lcs, today, current_prices)

    # ── Income context ──
    income = compute_income(orders)
    print()
    print(f"{'='*120}")
    print(f"  💵 NET OPTION INCOME for the window:")
    print(f"      Collected: ${income.premium_collected:>12,.2f}")
    print(f"      Paid back:  ${income.premium_paid:>12,.2f}")
    print(f"      Net:        ${income.net_option_income:>12,.2f}")
    print(f"{'='*120}")


if __name__ == '__main__':
    main()