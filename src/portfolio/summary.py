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
