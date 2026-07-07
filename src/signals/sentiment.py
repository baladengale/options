"""
Deterministic sentiment scoring engine per SPECS Section 6.

SENTIMENT_SCORE = weighted composite of 5 sub-scores:
- Trend sentiment (30%)
- Momentum sentiment (25%)
- IV sentiment (20%)
- Volume/OI sentiment (15%)
- Price action sentiment (10%)
"""

from typing import Optional


# ============================================================
# IV Sentiment (SPECS Section 6.1, Sub-score 3)
# ============================================================

def compute_iv_sentiment(
    iv_rank: float,
    iv: Optional[float] = None,
    hv: Optional[float] = None,
) -> float:
    """Score IV rank + IV/HV spread adjustment."""
    # Base score from IV rank
    if iv_rank >= 60:
        base = 100
    elif iv_rank >= 50:
        base = 85
    elif iv_rank >= 40:
        base = 70
    elif iv_rank >= 30:
        base = 55
    elif iv_rank >= 20:
        base = 35
    elif iv_rank >= 10:
        base = 20
    else:
        base = 10

    # IV/HV spread adjustment
    if iv is not None and hv is not None and hv > 0:
        spread_pct = (iv - hv) / hv * 100
        if spread_pct > 20:
            base -= 10
        elif spread_pct > 10:
            base -= 5
        elif spread_pct < -10:
            base += 5
        # else: no adjustment (±10% is fair value)

    return max(0.0, min(100.0, base))


# ============================================================
# Volume/OI Sentiment (SPECS Section 6.1, Sub-score 4)
# ============================================================

def compute_volume_oi_sentiment(volume_ratio: float, oi: int) -> float:
    """Score volume ratio and open interest."""
    if volume_ratio >= 1.5 and oi >= 1000:
        return 100
    elif volume_ratio >= 1.0 and oi >= 500:
        return 80
    elif volume_ratio >= 0.7 and oi >= 500:
        return 60
    elif volume_ratio >= 0.5 or (oi >= 100 and oi <= 500):
        return 40
    else:
        return 20


# ============================================================
# Price Action Sentiment (SPECS Section 6.1, Sub-score 5)
# ============================================================

def compute_price_action_sentiment(
    price: float,
    high_20d: float,
    low_20d: float,
    strategy: str,
) -> float:
    """Score price position within 20-day range. Percentage is relative to low/high."""
    if high_20d <= low_20d or low_20d <= 0 or high_20d <= 0:
        return 50  # insufficient data

    pct_above_low = (price - low_20d) / low_20d * 100
    pct_below_high = (high_20d - price) / high_20d * 100

    if strategy == 'CASH_SECURED_PUT':
        # Prefer near support (low) for CSP entry
        # Boundaries use strict inequality at upper end (e.g., 5-15% means 5 < x < 15)
        if pct_above_low <= 5:
            return 100        # near 20d low (within 5%)
        elif pct_above_low < 15:
            return 70         # 5-15% above 20d low
        elif pct_below_high <= 5:
            return 20         # near 20d high (within 5%) — poor CSP entry
        elif pct_below_high < 15:
            return 40         # 5-15% below 20d high
        else:
            return 50         # mid-range
    elif strategy == 'COVERED_CALL':
        # Prefer near resistance (high) for CC entry
        if pct_below_high <= 5:
            return 100        # near 20d high (within 5%) — excellent CC entry
        elif pct_below_high < 15:
            return 70         # 5-15% below 20d high
        elif pct_above_low <= 5:
            return 30         # near 20d low (within 5%) — poor CC entry
        elif pct_above_low < 15:
            return 40         # 5-15% above 20d low
        else:
            return 50         # mid-range
    else:
        return 50


# ============================================================
# Composite Sentiment Score (SPECS Section 6.1)
# ============================================================

# Default weights from strategy_params.yaml
DEFAULT_SENTIMENT_WEIGHTS = {
    'trend': 0.30,
    'momentum': 0.25,
    'iv_rank': 0.20,
    'volume_oi': 0.15,
    'price_action': 0.10,
}


def compute_sentiment_score(
    trend_sentiment: float,
    momentum_sentiment: float,
    iv_sentiment: float,
    volume_oi_sentiment: float,
    price_action_sentiment: float,
    weights: Optional[dict] = None,
) -> float:
    """
    SENTIMENT_SCORE = Σ (sub_score_i × weight_i)

    All sub-scores should be in [0, 100].
    """
    w = weights or DEFAULT_SENTIMENT_WEIGHTS
    score = (
        trend_sentiment * w['trend'] +
        momentum_sentiment * w['momentum'] +
        iv_sentiment * w['iv_rank'] +
        volume_oi_sentiment * w['volume_oi'] +
        price_action_sentiment * w['price_action']
    )
    return max(0.0, min(100.0, score))


# ============================================================
# Sentiment Direction (SPECS Section 6.2)
# ============================================================

def sentiment_direction(score: float) -> str:
    """Map sentiment score to direction label."""
    if score >= 70:
        return 'BULLISH'
    elif score >= 45:
        return 'NEUTRAL'
    elif score >= 30:
        return 'CAUTIOUS'
    else:
        return 'BEARISH'


# ============================================================
# Strategy Entry Rules (SPECS Section 6.2)
# ============================================================

def can_enter_csp(direction: str) -> bool:
    """CSP entry only when BULLISH or NEUTRAL."""
    return direction in ('BULLISH', 'NEUTRAL')


def can_enter_cc(direction: str) -> bool:
    """New CC entry only when NEUTRAL or CAUTIOUS (neutral-to-bearish = better CC premium)."""
    return direction in ('NEUTRAL', 'CAUTIOUS')


def can_write_cc_on_existing(direction: str) -> bool:
    """On existing shares, write CC in any direction EXCEPT BEARISH."""
    return direction != 'BEARISH'
