"""
Deterministic scoring engine — computes WHEEL_SCORE per SPECS Section 7.

All formulas are purely mathematical. No AI, no external calls, no randomness.
"""

from typing import Optional, Union
from dataclasses import dataclass, field
from datetime import date, datetime


# ============================================================
# Fundamental Score (SPECS Section 7.2.2)
# ============================================================

def _revenue_growth_score(yoy_growth_pct: float) -> float:
    """Bucket revenue growth YoY into 0-100."""
    if yoy_growth_pct >= 20:
        return 100
    elif yoy_growth_pct >= 15:
        return 80
    elif yoy_growth_pct >= 10:
        return 60
    elif yoy_growth_pct >= 5:
        return 40
    elif yoy_growth_pct >= 0:
        return 20
    else:
        return 0


def _eps_quality_score(positive_quarters: int) -> float:
    """Score EPS consistency (positive quarters out of last 4)."""
    buckets = {4: 100, 3: 75, 2: 50, 1: 25, 0: 0}
    return buckets.get(positive_quarters, 0)


def _fcf_yield_score(fcf_yield_pct: float) -> float:
    """Bucket FCF yield into 0-100."""
    if fcf_yield_pct >= 5:
        return 100
    elif fcf_yield_pct >= 3:
        return 80
    elif fcf_yield_pct >= 1:
        return 60
    elif fcf_yield_pct >= 0:
        return 30
    else:
        return 0


def _debt_to_equity_score(de_ratio: float) -> float:
    """Bucket debt-to-equity into 0-100."""
    if de_ratio < 0:
        return 0  # negative equity
    elif de_ratio < 0.3:
        return 100
    elif de_ratio < 0.6:
        return 80
    elif de_ratio < 1.0:
        return 60
    elif de_ratio < 2.0:
        return 40
    else:
        return 20


def _peg_ratio_score(peg: Optional[float]) -> float:
    """Bucket PEG ratio into 0-100."""
    if peg is None:
        return 50  # no estimate → neutral
    if peg < 0:
        return 20  # negative earnings
    if peg <= 1.0:
        return 100
    elif peg <= 1.5:
        return 80
    elif peg <= 2.0:
        return 60
    elif peg <= 3.0:
        return 40
    else:
        return 20


def compute_fundamental_score(fundamentals: dict) -> float:
    """
    Compute FUND_SCORE as average of 5 sub-components.
    Each sub-component ∈ [0, 100].
    """
    scores = [
        _revenue_growth_score(fundamentals.get('revenue_growth_yoy_pct', 0)),
        _eps_quality_score(fundamentals.get('eps_quarters_positive', 0)),
        _fcf_yield_score(fundamentals.get('fcf_yield_pct', 0)),
        _debt_to_equity_score(fundamentals.get('debt_to_equity', 0)),
        _peg_ratio_score(fundamentals.get('peg_ratio')),
    ]
    return sum(scores) / len(scores)


# ============================================================
# Composite Wheel Score (SPECS Section 7.1)
# ============================================================

# Weights from strategy_params.yaml
DEFAULT_WEIGHTS = {
    'trend_momentum': 0.25,
    'fundamental': 0.20,
    'options_chain': 0.25,
    'sentiment': 0.15,
    'correlation': 0.15,
}


def compute_wheel_score(
    ticker: str,
    strategy: str,
    trend_momentum: float,
    fundamental: float,
    options_chain: float,
    sentiment: float,
    correlation: float,
    constraints_pass: bool,
    weights: Optional[dict] = None,
) -> dict:
    """
    Compute WHEEL_SCORE per SPECS Section 7.1.

    WHEEL_SCORE = Σ(component_i × weight_i) × CONSTRAINT_PASS

    Returns dict with composite_score, signal, and all component scores.
    """
    w = weights or DEFAULT_WEIGHTS

    if not constraints_pass:
        return {
            'ticker': ticker,
            'strategy': strategy,
            'composite_score': 0.0,
            'trend_momentum_score': trend_momentum,
            'fundamental_score': fundamental,
            'options_chain_score': options_chain,
            'sentiment_score': sentiment,
            'correlation_score': correlation,
            'signal': 'AVOID',
            'all_constraints_pass': False,
        }

    composite = (
        trend_momentum * w['trend_momentum'] +
        fundamental * w['fundamental'] +
        options_chain * w['options_chain'] +
        sentiment * w['sentiment'] +
        correlation * w['correlation']
    )

    # Clamp to [0, 100]
    composite = max(0.0, min(100.0, composite))

    # Signal from composite score
    if composite >= 75:
        signal = 'STRONG_WRITE'
    elif composite >= 60:
        signal = 'WRITE'
    elif composite >= 40:
        signal = 'HOLD'
    else:
        signal = 'AVOID'

    return {
        'ticker': ticker,
        'strategy': strategy,
        'composite_score': round(composite, 2),
        'trend_momentum_score': trend_momentum,
        'fundamental_score': fundamental,
        'options_chain_score': options_chain,
        'sentiment_score': sentiment,
        'correlation_score': correlation,
        'signal': signal,
        'all_constraints_pass': True,
    }
