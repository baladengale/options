"""
Collar check — verifies all open positions remain covered/cash-secured.

Per SPECS Section 12.1: must return all_clear before any new trade.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class CollarReport:
    ok: bool
    reason: Optional[str] = None


# ============================================================
# Single Position Checks
# ============================================================

def check_cc_coverage(shares_owned: float, contracts: int) -> CollarReport:
    """Verify enough shares to cover CC contracts."""
    required = contracts * 100
    if shares_owned < required:
        return CollarReport(
            ok=False,
            reason=f"CC under-covered: {shares_owned} shares < {required} needed"
        )
    return CollarReport(ok=True)


def check_cc_coverage_multi(
    positions: list[dict],
    shares_by_ticker: dict[str, float],
) -> CollarReport:
    """Verify CC coverage across multiple positions per ticker."""
    contracts_by_ticker: dict[str, int] = {}
    for pos in positions:
        ticker = pos['ticker']
        contracts_by_ticker[ticker] = contracts_by_ticker.get(ticker, 0) + pos['contracts']

    for ticker, total_contracts in contracts_by_ticker.items():
        shares = shares_by_ticker.get(ticker, 0)
        required = total_contracts * 100
        if shares < required:
            return CollarReport(
                ok=False,
                reason=f"CC under-covered: {ticker} ({shares} shares < {required} needed)"
            )

    return CollarReport(ok=True)


def check_csp_coverage(
    available_cash: float,
    strike: float,
    contracts: int,
    tied_up_csp: float = 0.0,
) -> CollarReport:
    """Verify enough cash to secure CSP assignment."""
    cash_needed = strike * contracts * 100
    free_cash = available_cash - tied_up_csp

    if free_cash < cash_needed:
        return CollarReport(
            ok=False,
            reason=f"CSP under-secured: need ${cash_needed:,.2f}, have ${free_cash:,.2f} available"
        )
    return CollarReport(ok=True)


def check_csp_coverage_multi(
    positions: list[dict],
    available_cash: float,
) -> CollarReport:
    """Verify CSP cash coverage across multiple positions."""
    total_needed = sum(p['strike'] * p['contracts'] * 100 for p in positions)

    if available_cash < total_needed:
        return CollarReport(
            ok=False,
            reason=f"CSP under-secured: total need ${total_needed:,.2f}, available ${available_cash:,.2f}"
        )
    return CollarReport(ok=True)


# ============================================================
# Full Collar Check
# ============================================================

def collar_check(
    open_positions: list[dict],
    portfolio_cash: float,
    holdings: dict[str, float],
) -> CollarReport:
    """
    Verify ALL open positions are covered/cash-secured.

    Args:
        open_positions: list of dicts with keys: ticker, strategy, strike, contracts
        portfolio_cash: available cash
        holdings: ticker → shares mapping

    Returns:
        CollarReport with ok=True only if all positions pass.
    """
    if not open_positions:
        return CollarReport(ok=True)

    # Separate CC and CSP positions
    cc_positions = [p for p in open_positions if p['strategy'] == 'COVERED_CALL']
    csp_positions = [p for p in open_positions if p['strategy'] == 'CASH_SECURED_PUT']
    other_positions = [
        p for p in open_positions
        if p['strategy'] not in ('COVERED_CALL', 'CASH_SECURED_PUT')
    ]

    # Unknown strategies → fail
    if other_positions:
        return CollarReport(
            ok=False,
            reason=f"Unknown strategy: {other_positions[0]['strategy']}"
        )

    # Check CC coverage
    if cc_positions:
        contracts_by_ticker: dict[str, int] = {}
        for pos in cc_positions:
            ticker = pos['ticker']
            contracts_by_ticker[ticker] = contracts_by_ticker.get(ticker, 0) + pos['contracts']

        for ticker, total_contracts in contracts_by_ticker.items():
            shares = holdings.get(ticker, 0)
            required = total_contracts * 100
            if shares < required:
                return CollarReport(
                    ok=False,
                    reason=f"CC under-covered: {ticker} ({shares} shares < {required} needed)"
                )

    # Check CSP coverage
    if csp_positions:
        total_needed = sum(p['strike'] * p['contracts'] * 100 for p in csp_positions)
        if portfolio_cash < total_needed:
            return CollarReport(
                ok=False,
                reason=f"CSP under-secured: need ${total_needed:,.2f}, have ${portfolio_cash:,.2f}"
            )

    return CollarReport(ok=True)
