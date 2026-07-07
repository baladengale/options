"""
Portfolio risk monitoring — assignment handling and risk metrics.

Per SPECS Sections 7.3 and 12.2-12.3.
"""

from copy import deepcopy


# ============================================================
# Assignment Handling (SPECS Section 12.2)
# ============================================================

def handle_csp_assignment(
    portfolio: dict,
    ticker: str,
    strike: float,
    contracts: int,
    premium_per_share: float,
) -> dict:
    """
    Handle CSP assignment: cash decreases, shares added.

    Cost basis = strike - premium_per_share (premium reduces effective cost).
    """
    updated = deepcopy(portfolio)
    cash_required = strike * contracts * 100
    updated['cash'] = portfolio['cash'] - cash_required
    updated['holdings'] = dict(portfolio.get('holdings', {}))
    updated['holdings'][ticker] = updated['holdings'].get(ticker, 0) + (contracts * 100)

    if 'cost_basis' not in updated:
        updated['cost_basis'] = {}
    updated['cost_basis'][ticker] = strike - premium_per_share

    return updated


def handle_cc_assignment(
    portfolio: dict,
    ticker: str,
    strike: float,
    contracts: int,
) -> dict:
    """
    Handle CC assignment: shares removed, cash increased.

    Cash increases by strike × 100 × contracts.
    Premium was already collected at open.
    """
    updated = deepcopy(portfolio)
    cash_added = strike * contracts * 100
    updated['cash'] = portfolio['cash'] + cash_added
    updated['holdings'] = dict(portfolio.get('holdings', {}))
    shares_removed = contracts * 100
    updated['holdings'][ticker] = updated['holdings'].get(ticker, 0) - shares_removed

    if updated['holdings'][ticker] <= 0:
        del updated['holdings'][ticker]

    return updated


# ============================================================
# Portfolio Risk Metrics (SPECS Section 12.3)
# ============================================================

def compute_concentration_risk(
    position_values: dict[str, float],
    total_value: float,
) -> str:
    """
    Classify concentration risk based on largest position % of portfolio.

    > 20% → HIGH
    10-20% → MODERATE
    < 10% → LOW
    """
    if total_value <= 0 or not position_values:
        return 'LOW'

    largest_pct = max(position_values.values()) / total_value * 100

    if largest_pct > 20:
        return 'HIGH'
    elif largest_pct >= 10:
        return 'MODERATE'
    else:
        return 'LOW'


def compute_csp_tie_up_pct(tied_up_csp: float, total_cash: float) -> float:
    """Percentage of cash tied up in CSP assignments."""
    if total_cash <= 0:
        return 0.0
    return (tied_up_csp / total_cash) * 100.0


def compute_margin_usage(margin_used: float, portfolio_value: float) -> float:
    """Margin usage as % of portfolio — must always be 0%."""
    if portfolio_value <= 0:
        return 0.0
    return (margin_used / portfolio_value) * 100.0
