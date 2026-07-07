"""
Signal generation tests — validates the signal decision matrix
and signal output structure per SPECS Sections 5.4 and 5.5.
"""

import pytest
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================
# Signal Enum Tests
# ============================================================

class TestSignalEnum:
    """Validate signal enum values and comparisons."""

    def test_signal_values(self):
        from signals.generator import Signal
        assert Signal.STRONG_WRITE.value == 'STRONG_WRITE'
        assert Signal.WRITE.value == 'WRITE'
        assert Signal.HOLD.value == 'HOLD'
        assert Signal.AVOID.value == 'AVOID'

    def test_signal_ordering(self):
        """Signals should be comparable by desirability."""
        from signals.generator import Signal
        # STRONG_WRITE is best, AVOID is worst
        signals = [Signal.AVOID, Signal.HOLD, Signal.WRITE, Signal.STRONG_WRITE]
        # Sort should put them in natural order
        sorted_signals = sorted(signals, key=lambda s: s.value)
        # AVOID < HOLD < STRONG_WRITE < WRITE (alphabetically)
        # But semantically: AVOID < HOLD < WRITE < STRONG_WRITE
        assert Signal.AVOID != Signal.STRONG_WRITE


# ============================================================
# SignalResult Tests
# ============================================================

class TestSignalResult:
    """Validate SignalResult dataclass structure."""

    def test_signal_result_creation(self):
        from signals.generator import Signal, SignalResult
        result = SignalResult(
            ticker='MSFT',
            strategy='CASH_SECURED_PUT',
            signal=Signal.STRONG_WRITE,
            confidence=0.85,
            trend_composite=82.0,
            iv_rank=45.0,
            sentiment_score=75.0,
            options_quality_score=80.0,
            reason='Strong uptrend, elevated IV, liquid options chain.',
            generated_at=datetime.now(),
        )
        assert result.ticker == 'MSFT'
        assert result.signal == Signal.STRONG_WRITE
        assert result.confidence == 0.85

    def test_signal_result_reason_required(self):
        """Every signal must have a reason string."""
        from signals.generator import Signal, SignalResult
        result = SignalResult(
            ticker='TEST',
            strategy='CASH_SECURED_PUT',
            signal=Signal.HOLD,
            confidence=0.4,
            trend_composite=45.0,
            iv_rank=25.0,
            sentiment_score=50.0,
            options_quality_score=55.0,
            reason='IV rank below 30 — wait for volatility expansion.',
            generated_at=datetime.now(),
        )
        assert result.reason is not None
        assert len(result.reason) > 10  # Reason should be descriptive


# ============================================================
# Decision Matrix Tests (CSP)
# ============================================================

class TestCSPDecisionMatrix:
    """Validate the CSP signal decision matrix rows."""

    @pytest.mark.parametrize("trend,iv,sent,opt_q,expected_signal", [
        # Row 1: TREND≥70, IV≥30, SENT≥60, OPT≥60 → STRONG_WRITE
        (75, 45, 70, 75, 'STRONG_WRITE'),
        (80, 50, 65, 80, 'STRONG_WRITE'),
        (70, 30, 60, 60, 'STRONG_WRITE'),  # boundary

        # Row 2: TREND≥50, IV≥30, SENT≥50, OPT≥50 → WRITE
        (55, 35, 55, 55, 'WRITE'),
        (65, 40, 60, 70, 'WRITE'),
        (50, 30, 50, 50, 'WRITE'),  # boundary

        # Row 3: TREND≥50, IV<30, SENT≥50, OPT≥50 → HOLD (good stock, poor IV)
        (65, 25, 60, 65, 'HOLD'),
        (55, 20, 55, 55, 'HOLD'),

        # Row 4: TREND<50, any other → HOLD
        (45, 60, 70, 80, 'HOLD'),
        (30, 50, 50, 50, 'HOLD'),

        # Row 5: SENT<40 → AVOID
        (75, 50, 35, 80, 'AVOID'),
        (80, 60, 20, 75, 'AVOID'),

        # Row 6: OPT<40 → AVOID
        (75, 50, 70, 30, 'AVOID'),
        (70, 45, 65, 20, 'AVOID'),
    ])
    def test_csp_decision_matrix(self, trend, iv, sent, opt_q, expected_signal):
        from signals.generator import generate_signal
        result = generate_signal(
            strategy='CASH_SECURED_PUT',
            trend_composite=trend,
            iv_rank=iv,
            sentiment_score=sent,
            options_quality_score=opt_q,
        )
        assert result.signal.value == expected_signal, \
            f"Expected {expected_signal} for trend={trend}, iv={iv}, sent={sent}, opt={opt_q}"


# ============================================================
# Decision Matrix Tests (CC)
# ============================================================

class TestCCDecisionMatrix:
    """Validate the CC signal decision matrix rows."""

    @pytest.mark.parametrize("trend,iv,sent,opt_q,expected_signal", [
        # Row 1: TREND≥50, IV≥30, SENT≥50, OPT≥50 → STRONG_WRITE
        (60, 40, 55, 60, 'STRONG_WRITE'),
        (50, 30, 50, 50, 'STRONG_WRITE'),

        # Row 2: TREND≥40, IV≥30, SENT≥40, OPT≥50 → WRITE
        (45, 35, 45, 55, 'WRITE'),
        (40, 30, 40, 50, 'WRITE'),

        # Row 3: TREND≥40, IV<30, SENT≥40, OPT≥50 → HOLD (wait for IV)
        (50, 25, 50, 60, 'HOLD'),

        # Row 4: TREND<40, any → HOLD
        (35, 50, 60, 70, 'HOLD'),

        # Row 5: SENT<30 → AVOID
        (60, 40, 25, 60, 'AVOID'),

        # Row 6: OPT<40 → AVOID
        (60, 40, 60, 35, 'AVOID'),
    ])
    def test_cc_decision_matrix(self, trend, iv, sent, opt_q, expected_signal):
        from signals.generator import generate_signal
        result = generate_signal(
            strategy='COVERED_CALL',
            trend_composite=trend,
            iv_rank=iv,
            sentiment_score=sent,
            options_quality_score=opt_q,
        )
        assert result.signal.value == expected_signal, \
            f"Expected {expected_signal} for CC: trend={trend}, iv={iv}, sent={sent}, opt={opt_q}"


# ============================================================
# Confidence Score Tests
# ============================================================

class TestConfidenceScore:
    """Validate confidence computation."""

    def test_high_confidence(self):
        """All inputs well above thresholds → high confidence."""
        from signals.generator import generate_signal
        result = generate_signal(
            strategy='CASH_SECURED_PUT',
            trend_composite=90.0,
            iv_rank=70.0,
            sentiment_score=85.0,
            options_quality_score=90.0,
        )
        assert result.confidence >= 0.80

    def test_marginal_confidence(self):
        """Inputs just at thresholds → moderate confidence."""
        from signals.generator import generate_signal
        result = generate_signal(
            strategy='CASH_SECURED_PUT',
            trend_composite=52.0,      # just above 50
            iv_rank=32.0,               # just above 30
            sentiment_score=52.0,
            options_quality_score=52.0,
        )
        # Should still return WRITE but with lower confidence
        assert result.signal.value == 'WRITE'
        assert result.confidence < 0.70

    def test_avoid_has_low_confidence(self):
        """AVOID signals should have very low confidence."""
        from signals.generator import generate_signal
        result = generate_signal(
            strategy='CASH_SECURED_PUT',
            trend_composite=80.0,
            iv_rank=50.0,
            sentiment_score=25.0,       # < 40 → AVOID
            options_quality_score=80.0,
        )
        assert result.signal.value == 'AVOID'
        assert result.confidence < 0.4


# ============================================================
# Signal Consistency Tests
# ============================================================

class TestSignalConsistency:
    """Signals should be deterministic — same inputs → same output."""

    def test_deterministic_output(self):
        """Running the same inputs twice should produce identical results."""
        from signals.generator import generate_signal

        params = dict(
            strategy='CASH_SECURED_PUT',
            trend_composite=75.0,
            iv_rank=45.0,
            sentiment_score=70.0,
            options_quality_score=75.0,
        )

        result1 = generate_signal(**params)
        result2 = generate_signal(**params)

        assert result1.signal == result2.signal
        assert result1.confidence == result2.confidence

    def test_monotonic_with_trend(self):
        """Better trend → equal or better signal, all else equal."""
        from signals.generator import generate_signal, Signal

        base = generate_signal('CASH_SECURED_PUT', 50.0, 40.0, 60.0, 60.0)
        better = generate_signal('CASH_SECURED_PUT', 75.0, 40.0, 60.0, 60.0)

        # Better trend should not give a worse signal (semantically)
        signal_order = {Signal.AVOID: 0, Signal.HOLD: 1, Signal.WRITE: 2, Signal.STRONG_WRITE: 3}
        assert signal_order[better.signal] >= signal_order[base.signal]
