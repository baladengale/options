"""
Position sizing and return-on-capital calculations per SPECS Section 9.
"""


def compute_csp_roc(
    premium_per_share: float,
    strike: float,
    dte: int,
) -> float:
    """
    CSP Annualized RoC = (premium_per_share / strike) × (365 / DTE) × 100

    Example: MSFT CSP strike $420, premium $6.50, DTE 42
    RoC = (6.50 / 420) × (365 / 42) × 100 = 13.44%
    """
    if strike <= 0 or dte <= 0:
        return 0.0
    return (premium_per_share / strike) * (365.0 / dte) * 100.0


def compute_cc_roc(
    premium_per_share: float,
    cost_basis: float,
    dte: int,
) -> float:
    """
    CC Annualized RoC = (premium_per_share / cost_basis) × (365 / DTE) × 100

    Example: V CC cost basis $270, premium $3.20, DTE 35
    RoC = (3.20 / 270) × (365 / 35) × 100 = 12.36%
    """
    if cost_basis <= 0 or dte <= 0:
        return 0.0
    return (premium_per_share / cost_basis) * (365.0 / dte) * 100.0
