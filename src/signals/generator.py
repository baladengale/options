"""
Signal generator per SPECS Section 5.4.

Produces deterministic trading signals (STRONG_WRITE, WRITE, HOLD, AVOID)
from trend composite, IV rank, sentiment score, and options quality.
"""

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime


class Signal(Enum):
    STRONG_WRITE = "STRONG_WRITE"
    WRITE = "WRITE"
    HOLD = "HOLD"
    AVOID = "AVOID"


@dataclass
class SignalResult:
    ticker: str
    strategy: str
    signal: Signal
    confidence: float
    trend_composite: float
    iv_rank: float
    sentiment_score: float
    options_quality_score: float
    reason: str
    generated_at: datetime = field(default_factory=datetime.now)


def generate_signal(
    strategy: str,
    trend_composite: float,
    iv_rank: float,
    sentiment_score: float,
    options_quality_score: float,
) -> SignalResult:
    """
    Generate trading signal from component scores.

    Decision matrix per SPECS Section 5.4.
    """
    if strategy not in ('COVERED_CALL', 'CASH_SECURED_PUT'):
        return SignalResult(
            ticker='UNKNOWN',
            strategy=strategy,
            signal=Signal.AVOID,
            confidence=0.0,
            trend_composite=trend_composite,
            iv_rank=iv_rank,
            sentiment_score=sentiment_score,
            options_quality_score=options_quality_score,
            reason=f'Unknown strategy: {strategy}',
        )

    if strategy == 'CASH_SECURED_PUT':
        return _generate_csp_signal(trend_composite, iv_rank, sentiment_score, options_quality_score)
    else:
        return _generate_cc_signal(trend_composite, iv_rank, sentiment_score, options_quality_score)


def _generate_csp_signal(
    trend_composite: float,
    iv_rank: float,
    sentiment_score: float,
    options_quality_score: float,
) -> SignalResult:
    """CSP decision matrix."""
    # Row 5: SENT < 40 → AVOID
    if sentiment_score < 40:
        return SignalResult(
            ticker='', strategy='CASH_SECURED_PUT', signal=Signal.AVOID,
            confidence=0.2, trend_composite=trend_composite, iv_rank=iv_rank,
            sentiment_score=sentiment_score, options_quality_score=options_quality_score,
            reason='Sentiment too negative — avoid new CSP positions.',
        )

    # Row 6: OPT < 40 → AVOID
    if options_quality_score < 40:
        return SignalResult(
            ticker='', strategy='CASH_SECURED_PUT', signal=Signal.AVOID,
            confidence=0.2, trend_composite=trend_composite, iv_rank=iv_rank,
            sentiment_score=sentiment_score, options_quality_score=options_quality_score,
            reason='Options chain illiquid — avoid trading.',
        )

    # Row 4: TREND < 50 → HOLD
    if trend_composite < 50:
        return SignalResult(
            ticker='', strategy='CASH_SECURED_PUT', signal=Signal.HOLD,
            confidence=0.3, trend_composite=trend_composite, iv_rank=iv_rank,
            sentiment_score=sentiment_score, options_quality_score=options_quality_score,
            reason='Trend not favorable — wait for stronger uptrend.',
        )

    # Row 3: TREND ≥ 50, IV < 30 → HOLD
    if iv_rank < 30:
        return SignalResult(
            ticker='', strategy='CASH_SECURED_PUT', signal=Signal.HOLD,
            confidence=0.35, trend_composite=trend_composite, iv_rank=iv_rank,
            sentiment_score=sentiment_score, options_quality_score=options_quality_score,
            reason=f'IV rank {iv_rank:.0f} below 30 — wait for volatility expansion.',
        )

    # Row 1: TREND ≥ 70, IV ≥ 30, SENT ≥ 60, OPT ≥ 60 → STRONG_WRITE
    if trend_composite >= 70 and sentiment_score >= 60 and options_quality_score >= 60:
        # Calculate confidence based on how far above thresholds
        confidence = 0.70 + min(0.25, (trend_composite - 70) / 120 +
                                          (sentiment_score - 60) / 160 +
                                          (options_quality_score - 60) / 160)
        return SignalResult(
            ticker='', strategy='CASH_SECURED_PUT', signal=Signal.STRONG_WRITE,
            confidence=min(0.95, confidence), trend_composite=trend_composite,
            iv_rank=iv_rank, sentiment_score=sentiment_score,
            options_quality_score=options_quality_score,
            reason='Strong uptrend, elevated IV, bullish sentiment, liquid chain — ideal CSP entry.',
        )

    # Row 2: TREND ≥ 50, IV ≥ 30, SENT ≥ 50, OPT ≥ 50 → WRITE
    if trend_composite >= 50 and sentiment_score >= 50 and options_quality_score >= 50:
        confidence = 0.50 + min(0.25, (trend_composite - 50) / 200 +
                                          (sentiment_score - 50) / 200 +
                                          (options_quality_score - 50) / 200)
        return SignalResult(
            ticker='', strategy='CASH_SECURED_PUT', signal=Signal.WRITE,
            confidence=min(0.75, confidence), trend_composite=trend_composite,
            iv_rank=iv_rank, sentiment_score=sentiment_score,
            options_quality_score=options_quality_score,
            reason='Adequate conditions for CSP — proceed with reduced size.',
        )

    # Fallthrough → HOLD
    return SignalResult(
        ticker='', strategy='CASH_SECURED_PUT', signal=Signal.HOLD,
        confidence=0.3, trend_composite=trend_composite, iv_rank=iv_rank,
        sentiment_score=sentiment_score, options_quality_score=options_quality_score,
        reason='Mixed signals — wait for clearer setup.',
    )


def _generate_cc_signal(
    trend_composite: float,
    iv_rank: float,
    sentiment_score: float,
    options_quality_score: float,
) -> SignalResult:
    """CC decision matrix."""
    # Row 5: SENT < 30 → AVOID
    if sentiment_score < 30:
        return SignalResult(
            ticker='', strategy='COVERED_CALL', signal=Signal.AVOID,
            confidence=0.2, trend_composite=trend_composite, iv_rank=iv_rank,
            sentiment_score=sentiment_score, options_quality_score=options_quality_score,
            reason='Sentiment too negative — avoid selling calls.',
        )

    # Row 6: OPT < 40 → AVOID
    if options_quality_score < 40:
        return SignalResult(
            ticker='', strategy='COVERED_CALL', signal=Signal.AVOID,
            confidence=0.2, trend_composite=trend_composite, iv_rank=iv_rank,
            sentiment_score=sentiment_score, options_quality_score=options_quality_score,
            reason='Options chain illiquid — avoid trading.',
        )

    # Row 4: TREND < 40 → HOLD
    if trend_composite < 40:
        return SignalResult(
            ticker='', strategy='COVERED_CALL', signal=Signal.HOLD,
            confidence=0.3, trend_composite=trend_composite, iv_rank=iv_rank,
            sentiment_score=sentiment_score, options_quality_score=options_quality_score,
            reason='Trend too weak — don\'t cap upside in a potential rally.',
        )

    # Row 3: IV < 30 → HOLD
    if iv_rank < 30:
        return SignalResult(
            ticker='', strategy='COVERED_CALL', signal=Signal.HOLD,
            confidence=0.35, trend_composite=trend_composite, iv_rank=iv_rank,
            sentiment_score=sentiment_score, options_quality_score=options_quality_score,
            reason=f'IV rank {iv_rank:.0f} below 30 — wait for volatility expansion.',
        )

    # Row 1: TREND ≥ 50, IV ≥ 30, SENT ≥ 50, OPT ≥ 50 → STRONG_WRITE
    if trend_composite >= 50 and sentiment_score >= 50 and options_quality_score >= 50:
        confidence = 0.60 + min(0.30, (trend_composite - 50) / 200 +
                                          (sentiment_score - 50) / 200 +
                                          (options_quality_score - 50) / 200)
        return SignalResult(
            ticker='', strategy='COVERED_CALL', signal=Signal.STRONG_WRITE,
            confidence=min(0.90, confidence), trend_composite=trend_composite,
            iv_rank=iv_rank, sentiment_score=sentiment_score,
            options_quality_score=options_quality_score,
            reason='Good conditions for covered call — collect premium.',
        )

    # Row 2: TREND ≥ 40, IV ≥ 30, SENT ≥ 40, OPT ≥ 50 → WRITE
    if trend_composite >= 40 and sentiment_score >= 40 and options_quality_score >= 50:
        confidence = 0.45 + min(0.20, (trend_composite - 40) / 200 +
                                          (sentiment_score - 40) / 200)
        return SignalResult(
            ticker='', strategy='COVERED_CALL', signal=Signal.WRITE,
            confidence=min(0.70, confidence), trend_composite=trend_composite,
            iv_rank=iv_rank, sentiment_score=sentiment_score,
            options_quality_score=options_quality_score,
            reason='Adequate CC conditions — consider conservative strike.',
        )

    # Fallthrough → HOLD
    return SignalResult(
        ticker='', strategy='COVERED_CALL', signal=Signal.HOLD,
        confidence=0.3, trend_composite=trend_composite, iv_rank=iv_rank,
        sentiment_score=sentiment_score, options_quality_score=options_quality_score,
        reason='Mixed signals — wait for clearer setup.',
    )
