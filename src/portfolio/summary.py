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
from typing import Optional

from src.data.guardrails import SECTOR_MAP
from src.data.portfolio_loader import is_option_code

# Order statuses that represent a fill (money actually moved).
FILLED_STATUSES = ('FILLED_ALL', 'FILLED_PART')


@dataclass
class IncomeSummary:
    """Realized trading income tallied from order history."""
    premium_collected: float = 0.0    # SELL_SHORT gross (options × 100)
    premium_paid: float = 0.0         # BUY_BACK gross (options × 100)
    stock_bought: float = 0.0         # BUY gross
    stock_sold: float = 0.0           # SELL gross
    filled_order_count: int = 0
    monthly: dict = field(default_factory=dict)   # 'YYYY-MM' -> {'collected','buyback'}

    @property
    def net_option_income(self) -> float:
        return self.premium_collected - self.premium_paid


def compute_income(orders: list[dict]) -> IncomeSummary:
    """Tally realized income from normalized order rows (see portfolio_loader.fetch_orders).

    Mirrors the former portfolio_summary.py logic:
      SELL_SHORT → premium collected, BUY_BACK → premium paid,
      BUY → stock bought, SELL → stock sold.
    Monthly breakdown is OPTION-only (SELL_SHORT/BUY_BACK on option codes).
    """
    s = IncomeSummary()
    for o in orders:
        if o.get('status') not in FILLED_STATUSES:
            continue
        side = o.get('side', '')
        qty = abs(o.get('qty', 0) or 0)
        price = o.get('price', 0) or 0
        opt = is_option_code(o.get('code', ''))
        gross = qty * price * (100 if opt else 1)
        month = (o.get('date', '') or '')[:7]

        if side == 'SELL_SHORT':
            s.premium_collected += gross
            if opt and month:
                bucket = s.monthly.setdefault(month, {'collected': 0.0, 'buyback': 0.0})
                bucket['collected'] += qty * price * 100
        elif side == 'BUY_BACK':
            s.premium_paid += gross
            if opt and month:
                bucket = s.monthly.setdefault(month, {'collected': 0.0, 'buyback': 0.0})
                bucket['buyback'] += qty * price * 100
        elif side == 'BUY':
            s.stock_bought += gross
        elif side == 'SELL':
            s.stock_sold += gross

        s.filled_order_count += 1
    return s


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
        return "📋 Position On Track — Weekly monitoring scheduled"

    # Long-Term (> 21 DTE)
    if dte > 21:
        return "📊 Long-Term Position — Monthly review scheduled"

    return "✅ Position Status — No action required"
