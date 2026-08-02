"""
Portfolio summary computations — realized option income, monthly breakdown,
sector concentration, and unrealized P&L roll-ups.

Pure functions over data from src.data.portfolio_loader (funds, positions, orders)
and src.data.guardrails (SECTOR_MAP). No moomoo calls, no printing — those are the
wrapper's job. Extracted from the former scripts/portfolio_summary.py.

Usage:
    from src.data.portfolio_loader import fetch_orders
    from src.portfolio.summary import compute_income, compute_sector_breakdown
    income = compute_income(fetch_orders(trd))
    sectors = compute_sector_breakdown(pf.stocks)
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from src.data.guardrails import SECTOR_MAP
from src.data.portfolio_loader import is_option_code, parse_option_code

# Order statuses that represent a fill (money actually moved).
FILLED_STATUSES = ('FILLED_ALL', 'FILLED_PART')


@dataclass
class IncomeSummary:
    """Realized trading income tallied from order history."""
    premium_collected: float = 0.0    # option sells (SELL/SELL_SHORT) × 100
    premium_paid: float = 0.0         # option buys  (BUY/BUY_BACK)   × 100
    stock_bought: float = 0.0         # stock BUY
    stock_sold: float = 0.0           # stock SELL
    filled_order_count: int = 0
    monthly: dict = field(default_factory=dict)   # 'YYYY-MM' -> {'collected','buyback'}

    @property
    def net_option_income(self) -> float:
        return self.premium_collected - self.premium_paid


def compute_income(orders: list[dict]) -> IncomeSummary:
    """Tally realized income from normalized order rows (see portfolio_loader.fetch_orders).

    Classification keys on the instrument first, then the trade side:
      * OPTION code + (SELL or SELL_SHORT) → premium_collected  (×100)
      * OPTION code + (BUY  or BUY_BACK)   → premium_paid       (×100)
      * STOCK  code + BUY                   → stock_bought       (×1)
      * STOCK  code + SELL                  → stock_sold         (×1)

    Why option-first: moomoo returns covered-call sells as plain ``SELL`` (not
    ``SELL_SHORT``), and CSP/CC buy-to-close as ``BUY`` (not ``BUY_BACK``).
    Keying only on SELL_SHORT/BUY_BACK (the previous logic) misclassified that
    option premium into the stock buckets — understating premium_collected and
    inflating stock_sold by the ×100 contract multiplier. Monthly breakdown is
    option-only, as before.
    """
    s = IncomeSummary()
    for o in orders:
        if o.get('status') not in FILLED_STATUSES:
            continue
        side = o.get('side', '')
        code = o.get('code', '')
        qty = abs(o.get('qty', 0) or 0)
        price = o.get('price', 0) or 0
        opt = is_option_code(code)
        gross = qty * price * (100 if opt else 1)
        month = (o.get('date', '') or '')[:7]

        is_sell = side in ('SELL', 'SELL_SHORT')
        is_buy = side in ('BUY', 'BUY_BACK')

        if opt and is_sell:
            s.premium_collected += gross
            if month:
                bucket = s.monthly.setdefault(month, {'collected': 0.0, 'buyback': 0.0})
                bucket['collected'] += gross
        elif opt and is_buy:
            s.premium_paid += gross
            if month:
                bucket = s.monthly.setdefault(month, {'collected': 0.0, 'buyback': 0.0})
                bucket['buyback'] += gross
        elif side == 'BUY':
            s.stock_bought += gross
        elif side == 'SELL':
            s.stock_sold += gross

        s.filled_order_count += 1
    return s


@dataclass
class OrderRow:
    """One displayable filled order, parsed for the --orders view."""
    order_id: str
    date: str
    create_time: str
    ticker: str
    expiry: str
    opt_type: str           # 'C' / 'P' for options, '' for stock
    strike: float
    side: str               # raw trd_side (SELL/BUY/SELL_SHORT/BUY_BACK)
    is_option: bool
    qty: float
    price: float
    amount: float           # signed: + collected, − paid (×100 for options)
    action: str             # 'SOLD' / 'BOUGHT' / raw side


def order_income_breakdown(
    orders: list[dict],
    ticker_filter: Optional[str] = None,
    days: int = 90,
    today: Optional[date] = None,
) -> tuple[list[OrderRow], IncomeSummary]:
    """Filter shared order history for the --orders view and tally its income.

    Single source of truth for the order table: it reuses ``compute_income``'s
    classification (option-first) so the --orders totals always agree with --pnl.
    Returns (rows newest-first, IncomeSummary over the same filtered set).

    Args:
        orders: normalized rows from ``portfolio_loader.fetch_orders`` (all-time).
        ticker_filter: restrict to one underlying ticker (None / 'ALL' = all).
        days: only include fills within the last ``days`` days (default 90).
        today: override "now" for deterministic tests.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=days)
    _SELL = {'SELL', 'SELL_SHORT'}
    _BUY = {'BUY', 'BUY_BACK'}

    rows: list[OrderRow] = []
    summary = IncomeSummary()
    for o in orders:
        if o.get('status') not in FILLED_STATUSES:
            continue
        code = str(o.get('code', ''))
        is_opt = is_option_code(code)

        # Date window — only filled orders with a parseable date within range.
        d = o.get('date', '') or ''
        try:
            fill_date = date.fromisoformat(d[:10])
        except (ValueError, TypeError):
            continue
        if fill_date < cutoff:
            continue

        # Ticker filter applies to the underlying for both options and stocks.
        if ticker_filter and ticker_filter != 'ALL':
            if is_opt:
                parsed = parse_option_code(code)
                underlying = parsed[0] if parsed else ''
            else:
                underlying = code.replace('US.', '')
            if ticker_filter.upper() != underlying.upper():
                continue

        side = str(o.get('side', ''))
        qty = abs(o.get('qty', 0) or 0)
        price = o.get('price', 0) or 0
        mult = 100 if is_opt else 1
        amount = qty * price * mult

        if is_opt:
            parsed = parse_option_code(code)
            tic, expiry, opt_type, strike = parsed if parsed else ('', '', '', 0.0)
        else:
            tic, expiry, opt_type, strike = code.replace('US.', ''), '', '', 0.0

        if side in _SELL:
            action = 'SOLD'
            signed = amount
        elif side in _BUY:
            action = 'BOUGHT'
            signed = -amount
        else:
            action = side
            signed = amount

        rows.append(OrderRow(
            order_id=str(o.get('order_id', '')),
            date=d[:10],
            create_time=str(o.get('date', ''))[:10],   # fetch_orders exposes date only
            ticker=tic, expiry=expiry, opt_type=opt_type, strike=strike,
            side=side, is_option=is_opt, qty=qty, price=price,
            amount=signed, action=action,
        ))

        # Tally with the SAME classification as compute_income.
        if is_opt and side in _SELL:
            summary.premium_collected += amount
            bucket = summary.monthly.setdefault(d[:7], {'collected': 0.0, 'buyback': 0.0})
            bucket['collected'] += amount
        elif is_opt and side in _BUY:
            summary.premium_paid += amount
            bucket = summary.monthly.setdefault(d[:7], {'collected': 0.0, 'buyback': 0.0})
            bucket['buyback'] += amount
        elif side == 'BUY':
            summary.stock_bought += amount
        elif side == 'SELL':
            summary.stock_sold += amount
        summary.filled_order_count += 1

    rows.sort(key=lambda r: r.date, reverse=True)
    return rows, summary


def compute_sector_breakdown(
    stocks: dict,
    sector_map: Optional[dict] = None,
) -> dict[str, float]:
    """Map each stock holding's market value to its sector. Returns {sector: value}.

    Tickers without a known sector land in 'Other'. sector_map defaults to
    guardrails.SECTOR_MAP.
    """
    smap = sector_map if sector_map is not None else SECTOR_MAP
    sv: dict[str, float] = defaultdict(float)
    for ticker, pos in stocks.items():
        sector = smap.get(ticker, 'Other')
        sv[sector] += pos.get('mv', 0) if isinstance(pos, dict) else 0
    return dict(sv)


def unrealized_stock_pl(stocks: dict) -> float:
    """Sum of unrealized P&L across stock positions."""
    return sum(
        (pos.get('pl', 0) if isinstance(pos, dict) else 0)
        for pos in stocks.values()
    )


def unrealized_option_pl(options: dict) -> float:
    """Sum of unrealized P&L across option positions."""
    return sum(
        (pos.get('pl', 0) if isinstance(pos, dict) else 0)
        for pos in options.values()
    )


def stock_market_value(stocks: dict) -> float:
    return sum(
        (pos.get('mv', 0) if isinstance(pos, dict) else 0)
        for pos in stocks.values()
    )


# ── Thesis-Aware Decision Messages ──────────────────────────────────
# These functions replace trading-encouraging messages ("close or roll",
# "evaluate exit") with monitoring-focused, thesis-aware language.
# They delegate to src.analysis.thesis_validator for actual thesis checks.


def check_thesis_quick(ticker: str) -> dict:
    """
    Quick thesis check for decision messages (non-blocking).

    Wraps src.analysis.thesis_validator.quick_thesis_check() with a
    fallback-friendly interface. Returns a simple dict with boolean flags
    suitable for use in portfolio display logic.

    Returns:
        dict with 'broken' and 'damaged' boolean flags
    """
    try:
        from src.analysis.thesis_validator import quick_thesis_check
        from src.data.moomoo_client import MoomooClient

        moomoo = MoomooClient()
        snapshot = moomoo.get_stock_snapshot(f"US.{ticker}")
        if snapshot:
            result = quick_thesis_check(ticker, snapshot)
            return {'broken': result['broken'], 'damaged': result['damaged']}
    except Exception:
        pass

    # Default to intact if data unavailable
    return {'broken': False, 'damaged': False}


def generate_option_decision_message(position: dict) -> str:
    """
    Generate Wheel-appropriate decision messages for a single option position.

    Replaces the old trading-encouraging messages ("close or roll", "evaluate
    exit") with thesis-aware, monitoring-focused language. Assignment is
    framed as an expected Wheel outcome, not a warning.

    Args:
        position: dict with keys:
            days_to_expiry (dte), delta, unrealized_pnl, option_type, ticker

    Returns:
        Human-readable decision message string
    """
    dte = position.get('days_to_expiry', 0)
    delta = abs(position.get('delta', 0))
    unrealized_pnl = position.get('unrealized_pnl', 0)
    option_type = position.get('option_type', 'PUT')
    ticker = position.get('ticker', '')

    # Thesis-aware check
    thesis_check = check_thesis_quick(ticker) if ticker else {'broken': False, 'damaged': False}

    # Expiring Soon (≤3 DTE)
    if dte <= 3:
        if thesis_check['broken']:
            return "🚨 THESIS BROKEN — Auto-close required"
        return "📅 Expiring Soon — Let expire/assign naturally"

    # High Assignment Probability (|delta| ≥ 0.50)
    if delta >= 0.50:
        if option_type == 'CALL':
            return "📈 High Assignment Probability — Expected CC outcome"
        else:
            return "📈 High Assignment Probability — Expected CSP outcome"

    # Underwater Positions
    if unrealized_pnl < 0:
        if thesis_check['broken']:
            return "🚨 THESIS BROKEN — Exit Wheel position"
        elif thesis_check['damaged']:
            return "⚠️  THESIS DAMAGED — Monitor, no action needed"
        else:
            return "📊 Position Down — Thesis intact, hold position"

    # Standard Monitoring (7-21 DTE)
    if 7 <= dte <= 21:
        return "📋 Position On Track — Daily review covers thesis + guardrails"

    # Long-Term (> 21 DTE)
    if dte > 21:
        return "📊 Long-Term Position — Daily review covers thesis + guardrails"

    return "✅ Position Status — No action required"
