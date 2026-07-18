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
    accumulated_premium_per_share: float = 0.0,
) -> dict:
    """
    Handle CSP assignment: cash decreases, shares added.

    Cost basis = strike - premium_per_share - accumulated_premium_per_share.
    `accumulated_premium_per_share` carries prior campaign premium (earlier
    puts/calls on this ticker's wheel cycle) so the adjusted basis reflects the
    whole campaign, not just the assigning put (playbook §4).
    """
    updated = deepcopy(portfolio)
    cash_required = strike * contracts * 100
    updated['cash'] = portfolio['cash'] - cash_required
    updated['holdings'] = dict(portfolio.get('holdings', {}))
    updated['holdings'][ticker] = updated['holdings'].get(ticker, 0) + (contracts * 100)

    if 'cost_basis' not in updated:
        updated['cost_basis'] = {}
    updated['cost_basis'][ticker] = strike - premium_per_share - accumulated_premium_per_share

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


# ============================================================
# Roll-Chain / Campaign Accounting (playbook §3-§4)
# ============================================================
# A "campaign" is one wheel cycle on a ticker: the chain of option legs from
# first open to net-zero contracts. Track it as ONE position — the chain's net
# credit and roll count decide whether rolling is discipline or denial.
# UNVALIDATED — backtest pending. DB persistence is a follow-up; these are the
# pure accounting rules.

def campaign_net_credit(premiums_per_share: list) -> float:
    """
    Net credit across a roll campaign, per share.

    Positive entries = premium received (sells), negative = debits paid
    (buy-to-close legs). A negative total means the campaign has paid to stay
    alive — a broken position wearing a roll costume.
    """
    return sum(premiums_per_share)


def campaign_adjusted_basis(strike: float, premiums_per_share: list) -> float:
    """
    Adjusted share basis if assigned at `strike`: strike − campaign net credit.

    This is the sunk-cost-honest number: it reflects real cash collected, but
    exit decisions should compare CURRENT price vs recovery prospects, not
    cling to this anchor (playbook §4).
    """
    return strike - campaign_net_credit(premiums_per_share)


def is_roll_chain_broken(strike_history: list) -> bool:
    """
    3+ legs with successively LOWER strikes = broken thesis, not bad luck
    (playbook §3). Chasing the price down is the death-spiral signature.
    """
    if len(strike_history) < 3:
        return False
    return all(strike_history[i + 1] < strike_history[i]
               for i in range(len(strike_history) - 1))


def check_roll_discipline(
    roll_count: int,
    net_credit_per_share: float,
    extension_days: int,
    max_rolls: int = 2,
    min_extension_days: int = 30,
) -> tuple[bool, list]:
    """
    Evaluate a PROPOSED roll against the discipline rules. Returns (allowed, violations).

    Rules (config/rules.yaml `rolling`): net credit only; max rolls per
    campaign; minimum extension (weeklies churn commissions). Any violation →
    close or take assignment instead of rolling.
    """
    violations = []
    if net_credit_per_share <= 0:
        violations.append('net debit roll — never pay to roll (GOAL.md §6)')
    if roll_count >= max_rolls:
        violations.append(f'already rolled {roll_count}× (max {max_rolls}) — close or take assignment')
    if extension_days < min_extension_days:
        violations.append(f'extension {extension_days}d < {min_extension_days}d minimum')
    return len(violations) == 0, violations
