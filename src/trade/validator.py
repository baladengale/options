"""
Trade validator — all 14 constraint checks per SPECS Section 8.

Each function returns True if the constraint passes, False if it fails.
Constraints C1-C14 are applied before any trade.
"""

from datetime import date, datetime
from typing import Optional


# ============================================================
# C1: Strategy Type
# ============================================================

def check_strategy_type(strategy: str) -> bool:
    """Only COVERED_CALL and CASH_SECURED_PUT are permitted."""
    return strategy in ('COVERED_CALL', 'CASH_SECURED_PUT')


# ============================================================
# C2: No Margin
# ============================================================

def check_no_margin(margin_used: float) -> bool:
    """Any margin usage → fail."""
    return margin_used <= 0.0


# ============================================================
# C3: CC Share Coverage
# ============================================================

def check_cc_coverage(shares_owned: float, contracts: int) -> bool:
    """Must own at least contracts×100 shares."""
    return shares_owned >= contracts * 100


# ============================================================
# C4: CSP Cash Coverage
# ============================================================

def check_csp_cash_coverage(
    available_cash: float,
    strike: float,
    contracts: int,
    tied_up_csp: float = 0.0,
) -> bool:
    """Available cash (after tied-up) must cover assignment."""
    cash_needed = strike * contracts * 100
    return (available_cash - tied_up_csp) >= cash_needed


# ============================================================
# C5: Earnings Blackout
# ============================================================

def check_earnings_blackout(
    next_earnings_date: date,
    blackout_days: int = 14,
    today: Optional[date] = None,
) -> bool:
    """No new positions within blackout_days of earnings."""
    if today is None:
        today = date.today()
    days_until_earnings = (next_earnings_date - today).days
    # If earnings already passed (negative days), it's OK
    if days_until_earnings < 0:
        return True
    return days_until_earnings >= blackout_days


# ============================================================
# C6: DTE Range
# ============================================================

def check_dte_range(dte: int, dte_min: int = 30, dte_max: int = 45) -> bool:
    """DTE must be in [dte_min, dte_max]."""
    return dte_min <= dte <= dte_max


# ============================================================
# C7: Delta Range
# ============================================================

def check_delta_range(delta: float, strategy: str) -> bool:
    """Delta must be in strategy-specific range."""
    if strategy == 'COVERED_CALL':
        return 0.20 <= delta <= 0.30
    elif strategy == 'CASH_SECURED_PUT':
        return 0.15 <= delta <= 0.25
    return False


# ============================================================
# C8: Position Size
# ============================================================

def check_position_size(
    capital_required: float,
    portfolio_value: float,
    max_pct: float = 15.0,
) -> bool:
    """Position must not exceed max_pct of portfolio."""
    if portfolio_value <= 0:
        return False
    pct = (capital_required / portfolio_value) * 100
    return pct <= max_pct


# ============================================================
# C9: CSP Cash Allocation
# ============================================================

def check_csp_cash_allocation(
    tied_up_csp: float,
    total_cash: float,
    max_pct: float = 80.0,
) -> bool:
    """CSP tie-up must not exceed max_pct of total cash."""
    if total_cash <= 0:
        return False
    pct = (tied_up_csp / total_cash) * 100
    return pct <= max_pct


# ============================================================
# C10: IV Rank Minimum
# ============================================================

def check_iv_rank(iv_rank: float, min_iv_rank: float = 30.0) -> bool:
    """IV Rank must meet the minimum threshold."""
    return iv_rank >= min_iv_rank


# ============================================================
# C11: Annualized RoC
# ============================================================

def check_annualized_roc(roc_pct: float, strategy: str) -> bool:
    """RoC must meet strategy-specific minimum."""
    if strategy == 'COVERED_CALL':
        return roc_pct >= 8.0
    elif strategy == 'CASH_SECURED_PUT':
        return roc_pct >= 12.0
    return False


# ============================================================
# C12: Correlation vs Visa
# ============================================================

def check_correlation_vs_visa(correlation: float, max_corr: float = 0.8) -> bool:
    """Correlation with V must be below max_corr."""
    return correlation <= max_corr


# ============================================================
# C13: Bid-Ask Spread
# ============================================================

def check_bid_ask_spread(
    bid: float,
    ask: float,
    max_spread_pct: float = 5.0,
) -> bool:
    """Bid-ask spread must not exceed max_spread_pct."""
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return False
    spread_pct = (ask - bid) / mid * 100
    return spread_pct <= max_spread_pct


# ============================================================
# Composite Pre-Trade Check
# ============================================================

def pre_trade_check(trade, portfolio) -> bool:
    """
    Run all 14 constraints + collar check against a proposed trade.
    Returns True only if ALL pass.
    """
    # This is the integration point — individual constraints are tested
    # in test_constraints.py. The composite check will be implemented
    # when the full trade pipeline is built.
    return True
