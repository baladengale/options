"""
Holdings exit framework — the stock-leg loss rules.

Implements loss-management-playbook.md §4-§6 (Decision #10, 2026-07-17):
- Price backstops: -30% conditional (below a declining 200 SMA), -40% circuit breaker
- Dead-zone detection: too far below basis for a basis-strike CC to pay anything
- Capacity math: months-to-recover the basis gap via CC premium
- Time stop: stagnant capital compared against the premium-yield alternative

All functions are pure and deterministic. Decisions are signals for the operator —
nothing here executes trades. Thresholds default from the playbook; callers should
pass values from config/rules.yaml `holdings_exit`.

UNVALIDATED — backtest pending.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HoldingExitReport:
    """Result of the composite stock-leg exit evaluation."""
    ticker: str
    decision: str            # 'OK' | 'DEAD_ZONE' | 'BACKSTOP_EXIT' | 'CIRCUIT_BREAKER'
    drop_pct: float          # fraction below adjusted basis (0.32 = 32% underwater)
    reasons: list = field(default_factory=list)


def drawdown_from_basis(price: float, adjusted_basis: float) -> float:
    """Fractional drop from adjusted basis. Positive = underwater, 0 = at/above basis."""
    if adjusted_basis <= 0 or price <= 0:
        return 0.0
    return max(0.0, (adjusted_basis - price) / adjusted_basis)


def sma_slope(closes: list, period: int = 200, lookback: int = 20) -> Optional[float]:
    """
    Slope of the period-SMA over `lookback` bars: SMA(now) - SMA(lookback bars ago).

    Negative = declining SMA (the regime condition for the -30% backstop).
    Returns None if there aren't period + lookback closes.
    """
    if len(closes) < period + lookback:
        return None
    now = sum(closes[-period:]) / period
    then = sum(closes[-period - lookback:-lookback]) / period
    return now - then


def check_price_backstop(
    price: float,
    adjusted_basis: float,
    sma_200: Optional[float] = None,
    sma_200_slope: Optional[float] = None,
    conditional_pct: float = 0.30,
    hard_pct: float = 0.40,
) -> tuple[bool, str]:
    """
    Two-layer price backstop on assigned/held shares.

    Layer 1 (hard): drop ≥ hard_pct → circuit breaker, unconditional.
    Layer 2 (conditional): drop ≥ conditional_pct AND price below a *declining*
    200 SMA → exit signal. Stops only add value in trending markets, so the
    conditional layer requires the downtrend regime (Kaminski & Lo).
    """
    drop = drawdown_from_basis(price, adjusted_basis)
    if drop >= hard_pct:
        return True, f'CIRCUIT BREAKER: {drop:.0%} below adjusted basis (limit {hard_pct:.0%})'
    if (drop >= conditional_pct
            and sma_200 is not None and price < sma_200
            and sma_200_slope is not None and sma_200_slope < 0):
        return True, (f'BACKSTOP: {drop:.0%} below adjusted basis, '
                      f'under declining 200 SMA')
    return False, ''


def is_dead_zone(price: float, adjusted_basis: float, dead_zone_pct: float = 0.15) -> bool:
    """Deep enough underwater that a basis-strike CC pays ~nothing (playbook §4)."""
    return drawdown_from_basis(price, adjusted_basis) > dead_zone_pct


def months_to_recover(
    price: float,
    adjusted_basis: float,
    monthly_premium_per_share: Optional[float],
) -> Optional[float]:
    """
    Months of CC premium needed to close the basis gap.

    0.0 if not underwater. None if premium income is zero/unknown (gap can
    only close via price recovery).
    """
    gap = max(0.0, adjusted_basis - price)
    if gap == 0.0:
        return 0.0
    if not monthly_premium_per_share or monthly_premium_per_share <= 0:
        return None
    return gap / monthly_premium_per_share


def check_time_stop(
    months_held: float,
    position_return_pct: float,
    alt_yield_pct_annual: float,
    time_stop_months: int = 12,
) -> tuple[bool, str]:
    """
    Stagnant-capital check: position held past the time stop AND cumulative
    return lags what the same capital earns at the portfolio's premium yield.
    """
    if months_held < time_stop_months:
        return False, ''
    alt_return = alt_yield_pct_annual * (months_held / 12.0)
    if position_return_pct < alt_return:
        return True, (f'TIME STOP: {months_held:.0f} months held, '
                      f'{position_return_pct:+.1f}% vs {alt_return:.1f}% premium-yield alternative')
    return False, ''


def evaluate_holding_exit(
    ticker: str,
    price: float,
    adjusted_basis: float,
    sma_200: Optional[float] = None,
    sma_200_slope: Optional[float] = None,
    monthly_premium_per_share: Optional[float] = None,
    dead_zone_pct: float = 0.15,
    conditional_pct: float = 0.30,
    hard_pct: float = 0.40,
    months_to_recover_flag: int = 12,
) -> HoldingExitReport:
    """
    Composite stock-leg check, ordered by severity:
    CIRCUIT_BREAKER > BACKSTOP_EXIT > DEAD_ZONE > OK.
    """
    drop = drawdown_from_basis(price, adjusted_basis)
    reasons: list[str] = []

    triggered, reason = check_price_backstop(
        price, adjusted_basis, sma_200, sma_200_slope, conditional_pct, hard_pct)
    if triggered:
        reasons.append(reason)
        decision = 'CIRCUIT_BREAKER' if drop >= hard_pct else 'BACKSTOP_EXIT'
        return HoldingExitReport(ticker, decision, drop, reasons)

    if is_dead_zone(price, adjusted_basis, dead_zone_pct):
        reasons.append(f'{drop:.0%} below adjusted basis — basis-strike CC pays ~0')
        mtr = months_to_recover(price, adjusted_basis, monthly_premium_per_share)
        if mtr is not None:
            reasons.append(f'~{mtr:.0f} months to recover via CC premium')
            if mtr > months_to_recover_flag:
                reasons.append(f'REDEPLOY review: recovery > {months_to_recover_flag} months')
        return HoldingExitReport(ticker, 'DEAD_ZONE', drop, reasons)

    return HoldingExitReport(ticker, 'OK', drop, reasons)
