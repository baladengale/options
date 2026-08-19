"""
Trend, momentum, and signal formula validation tests.

Validates SMA alignment, ADX, RSI, MACD, and the composite
TREND_COMPOSITE formula per SPECS Section 5.
"""

import pytest
import math
from datetime import date, timedelta
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================
# SMA Calculation Tests
# ============================================================

class TestSMACalculation:
    """Validate Simple Moving Average computation."""

    def test_sma_basic(self):
        """SMA of [10, 20, 30] = 20."""
        from analysis.trend import compute_sma
        prices = [10.0, 20.0, 30.0]
        assert compute_sma(prices, period=3) == 20.0

    def test_sma_partial_window(self):
        """SMA with fewer prices than period returns avg of available."""
        from analysis.trend import compute_sma
        prices = [10.0, 20.0]
        assert compute_sma(prices, period=3) == 15.0

    def test_sma_single_price(self):
        from analysis.trend import compute_sma
        assert compute_sma([100.0], period=20) == 100.0

    def test_sma_20_period(self):
        """20-period SMA of 20 identical prices = that price."""
        from analysis.trend import compute_sma
        prices = [50.0] * 20
        assert compute_sma(prices, period=20) == 50.0

    def test_sma_from_price_history(self, msft_price_history):
        """SMA computed from mock MSFT price history."""
        from analysis.trend import compute_sma, compute_sma_from_history
        closes = [row['close'] for row in msft_price_history]
        sma20 = compute_sma(closes, period=20)
        sma50 = compute_sma(closes, period=50)
        sma200 = compute_sma(closes, period=200)

        # With upward drift, SMA20 should be > SMA50 > SMA200 (bullish alignment)
        assert sma20 > 0
        assert sma50 > 0
        assert sma200 > 0
        # The newest prices are higher due to drift, so shorter SMA should be higher
        assert sma20 > sma200  # bullish: shorter MA above longer MA

    def test_sma_from_bearish_history(self, bearish_stock_history):
        """SMA with bearish drift: SMA20 < SMA50 < SMA200."""
        from analysis.trend import compute_sma, compute_sma_from_history
        closes = [row['close'] for row in bearish_stock_history]
        sma20 = compute_sma(closes, period=20)
        sma200 = compute_sma(closes, period=200)

        # Bearish: shorter SMA should be below longer SMA
        assert sma20 < sma200


# ============================================================
# Trend Alignment Tests
# ============================================================

class TestTrendAlignment:
    """Validate SMA alignment scoring per SPECS Section 5.1."""

    def test_full_bullish_csp(self):
        """price > SMA20 > SMA50 > SMA200 → 100 for CSP."""
        from analysis.trend import compute_trend_alignment
        score = compute_trend_alignment(
            price=450.0, sma20=430.0, sma50=410.0, sma200=380.0,
            strategy='CASH_SECURED_PUT'
        )
        assert score == 100.0

    def test_two_of_three_bullish_csp(self):
        """2 bullish alignments, 1 bearish → ~67."""
        from analysis.trend import compute_trend_alignment
        # price > SMA20 ✓, SMA20 > SMA50 ✓, SMA50 < SMA200 ✗
        score = compute_trend_alignment(
            price=450.0, sma20=420.0, sma50=410.0, sma200=415.0,
            strategy='CASH_SECURED_PUT'
        )
        assert score == pytest.approx(66.67, abs=0.1)

    def test_full_bearish_csp(self):
        """price < SMA20 < SMA50 < SMA200 → 0 for CSP."""
        from analysis.trend import compute_trend_alignment
        score = compute_trend_alignment(
            price=350.0, sma20=380.0, sma50=400.0, sma200=420.0,
            strategy='CASH_SECURED_PUT'
        )
        assert score == 0.0

    def test_cc_trend_floor(self):
        """CC trend alignment has floor at 30."""
        from analysis.trend import compute_trend_alignment
        score = compute_trend_alignment(
            price=350.0, sma20=380.0, sma50=400.0, sma200=420.0,
            strategy='COVERED_CALL'
        )
        assert score == 30.0

    def test_cc_bullish_above_floor(self):
        """CC bullish alignment scores 100 (above floor)."""
        from analysis.trend import compute_trend_alignment
        score = compute_trend_alignment(
            price=450.0, sma20=430.0, sma50=410.0, sma200=380.0,
            strategy='COVERED_CALL'
        )
        assert score == 100.0  # Not floored — natural score exceeds 30

    def test_alignment_tie_price_equals_sma(self):
        """When price == SMA20, that pair is not counted as either bullish or bearish."""
        from analysis.trend import compute_trend_alignment
        # price = SMA20, SMA20 > SMA50, SMA50 > SMA200 → 2 bullish, 1 skipped
        score = compute_trend_alignment(
            price=430.0, sma20=430.0, sma50=410.0, sma200=380.0,
            strategy='CASH_SECURED_PUT'
        )
        # 2 bullish pairs out of 3 total → 66.67
        assert score == pytest.approx(66.67, abs=0.1)


# ============================================================
# ADX Calculation Tests
# ============================================================

class TestADX:
    """Validate ADX trend strength scoring."""

    def test_adx_strong(self):
        from analysis.trend import compute_adx_strength_score
        assert compute_adx_strength_score(50) == 100

    def test_adx_moderate(self):
        from analysis.trend import compute_adx_strength_score
        assert compute_adx_strength_score(30) == 75

    def test_adx_weak(self):
        from analysis.trend import compute_adx_strength_score
        assert compute_adx_strength_score(22) == 50

    def test_adx_ranging(self):
        from analysis.trend import compute_adx_strength_score
        assert compute_adx_strength_score(10) == 25

    def test_adx_boundary_25(self):
        from analysis.trend import compute_adx_strength_score
        assert compute_adx_strength_score(25) == 75  # ≥ 25 → moderate
        assert compute_adx_strength_score(24) == 50  # < 25 → weak

    def test_adx_boundary_20(self):
        from analysis.trend import compute_adx_strength_score
        assert compute_adx_strength_score(20) == 50  # ≥ 20 → weak
        assert compute_adx_strength_score(19) == 25  # < 20 → ranging


# ============================================================
# RSI Bucket Tests
# ============================================================

class TestRSIBuckets:
    """Validate RSI scoring buckets for both strategies."""

    @pytest.mark.parametrize("rsi,strategy,expected", [
        # CSP RSI buckets
        (50, 'CASH_SECURED_PUT', 100),   # neutral = ideal
        (42, 'CASH_SECURED_PUT', 85),    # mildly oversold
        (58, 'CASH_SECURED_PUT', 70),    # mildly overbought
        (38, 'CASH_SECURED_PUT', 60),    # oversold
        (62, 'CASH_SECURED_PUT', 50),    # overbought
        (33, 'CASH_SECURED_PUT', 35),    # deeply oversold
        (70, 'CASH_SECURED_PUT', 25),    # strongly overbought
        (25, 'CASH_SECURED_PUT', 15),    # extreme oversold
        (80, 'CASH_SECURED_PUT', 10),    # extreme overbought

        # CC RSI buckets (different preferences)
        (50, 'COVERED_CALL', 100),       # neutral = fine
        (60, 'COVERED_CALL', 85),        # mildly overbought = good CC entry
        (43, 'COVERED_CALL', 70),        # mildly oversold
        (70, 'COVERED_CALL', 60),        # overbought = excellent CC entry
        (38, 'COVERED_CALL', 50),        # oversold = poor CC entry
        (78, 'COVERED_CALL', 40),        # extreme overbought
        (30, 'COVERED_CALL', 20),        # extreme oversold
    ])
    def test_rsi_buckets(self, rsi, strategy, expected):
        from analysis.trend import compute_rsi_score
        assert compute_rsi_score(rsi, strategy) == expected

    def test_rsi_invalid_strategy_raises(self):
        from analysis.trend import compute_rsi_score
        with pytest.raises(ValueError, match="Unknown strategy"):
            compute_rsi_score(50, 'NAKED_PUT')


# ============================================================
# MACD Scoring Tests
# ============================================================

class TestMACDScoring:
    """Validate MACD signal scoring per SPECS Section 5.2."""

    @pytest.mark.parametrize("macd,signal,hist,prev_hist,strategy,expected", [
        # CSP: bullish accelerating
        (3.0, 1.0, 2.0, 1.0, 'CASH_SECURED_PUT', 100),
        # CSP: bullish decelerating
        (3.0, 1.0, 2.0, 2.5, 'CASH_SECURED_PUT', 80),
        # CSP: just crossed bullish (MACD > Signal but histogram still negative)
        (0.5, -1.0, 1.5, None, 'CASH_SECURED_PUT', 80),  # Actually: MACD>Signal, hist>0 AND prev_hist < unknown
        # CSP: bearish accelerating
        (-3.0, -1.0, -2.0, -1.0, 'CASH_SECURED_PUT', 30),
        # CSP: bearish decelerating
        (-2.0, -1.0, -1.0, -2.0, 'CASH_SECURED_PUT', 40),
    ])
    def test_macd_buckets(self, macd, signal, hist, prev_hist, strategy, expected):
        from analysis.trend import compute_macd_score
        score = compute_macd_score(macd, signal, hist, prev_hist, strategy)
        assert score == expected

    def test_macd_cross_above_signal(self):
        """MACD just crossed above signal line → bullish signal."""
        from analysis.trend import compute_macd_score
        # MACD > Signal, histogram just turned positive
        score = compute_macd_score(
            macd=0.2, signal=-0.5, histogram=0.7, prev_histogram=-0.1,
            strategy='CASH_SECURED_PUT'
        )
        # MACD > Signal, hist > 0, hist > prev_hist → accelerating → 100
        assert score == 100

    def test_macd_cross_below_signal(self):
        """MACD just crossed below signal line → bearish signal."""
        from analysis.trend import compute_macd_score
        score = compute_macd_score(
            macd=-0.2, signal=0.5, histogram=-0.7, prev_histogram=0.1,
            strategy='CASH_SECURED_PUT'
        )
        # MACD < Signal, hist < 0, hist < prev_hist → bearish accelerating → 30
        assert score == 30


# ============================================================
# Composite Trend/Momentum Formula
# ============================================================

class TestTrendComposite:
    """Validate TREND_COMPOSITE = 0.5×ALIGNMENT + 0.3×ADX + 0.2×MOMENTUM."""

    def test_composite_formula(self):
        """
        TREND_COMPOSITE = 0.5×100 + 0.3×100 + 0.2×100 = 100
        """
        from analysis.trend import compute_trend_composite
        score = compute_trend_composite(
            trend_alignment=100.0,
            adx_strength=100.0,
            momentum=100.0,
        )
        assert score == 100.0

    def test_composite_mixed(self):
        """
        TREND_COMPOSITE = 0.5×80 + 0.3×60 + 0.2×70
        = 40 + 18 + 14 = 72
        """
        from analysis.trend import compute_trend_composite
        score = compute_trend_composite(
            trend_alignment=80.0,
            adx_strength=60.0,
            momentum=70.0,
        )
        assert score == pytest.approx(72.0, abs=0.01)

    def test_composite_floor_zero(self):
        """All zeros → zero."""
        from analysis.trend import compute_trend_composite
        score = compute_trend_composite(0.0, 0.0, 0.0)
        assert score == 0.0

    def test_composite_clamped_to_100(self):
        """Should not exceed 100."""
        from analysis.trend import compute_trend_composite
        score = compute_trend_composite(200.0, 200.0, 200.0)
        assert score <= 100.0


# ============================================================
# Canonical snapshot → composite wiring (SPECS §5.3)
# ============================================================

class TestTrendCompositeFromSnapshot:
    """Validate trend_composite_from_snapshot — the single definition shared
    by the entry layer and the trend-modulated exit layer."""

    def _snap(self, **kw):
        from src.data.models import StockSnapshot
        d = dict(
            ticker='TEST', name='Test', last_price=110.0,
            sma_20=105.0, sma_50=100.0, sma_200=90.0,
            adx_14=40.0, rsi_14=50.0,
            macd=2.0, macd_signal=1.0, macd_histogram=1.0,
            history_points=252,
        )
        d.update(kw)
        return StockSnapshot(**d)

    def test_full_bullish_composite(self):
        """alignment 100 + ADX 100 + momentum (0.5×100 RSI + 0.5×80 MACD)=90
        → 0.5×100 + 0.3×100 + 0.2×90 = 98."""
        from analysis.trend import trend_composite_from_snapshot
        assert trend_composite_from_snapshot(self._snap()) == pytest.approx(98.0)

    def test_bearish_composite(self):
        """Price below the whole SMA stack, strong ADX, oversold RSI,
        accelerating-bearish MACD."""
        from analysis.trend import trend_composite_from_snapshot
        snap = self._snap(
            last_price=80.0, sma_20=95.0, sma_50=100.0, sma_200=110.0,
            adx_14=40.0, rsi_14=25.0,
            macd=-2.0, macd_signal=-1.0, macd_histogram=-1.0,
        )
        # alignment 0, ADX 100, RSI(CSP,25)=15, MACD bearish accelerating=30
        # momentum = 22.5 → 0 + 30 + 4.5 = 34.5
        assert trend_composite_from_snapshot(snap) == pytest.approx(34.5)

    def test_missing_sma_returns_none(self):
        """No SMA anchors → None, never a silent neutral-bullish number."""
        from analysis.trend import trend_composite_from_snapshot
        assert trend_composite_from_snapshot(self._snap(sma_50=None)) is None
        assert trend_composite_from_snapshot(self._snap(sma_200=None)) is None
        assert trend_composite_from_snapshot(self._snap(sma_20=None)) is None

    def test_short_history_returns_none(self):
        """A 'SMA200' averaged over <200 sessions is a mislabeled short
        average — composite must refuse (no-data honesty)."""
        from analysis.trend import trend_composite_from_snapshot
        assert trend_composite_from_snapshot(self._snap(history_points=120)) is None

    def test_missing_momentum_degrades_to_neutral_not_bullish(self):
        """RSI/MACD absent → momentum 50 → 0.5×100 + 0.3×100 + 0.2×50 = 90.
        The composite stays available on alignment+ADX alone but is NOT
        inflated by absent signals."""
        from analysis.trend import trend_composite_from_snapshot
        snap = self._snap(rsi_14=None, macd=None, macd_signal=None, macd_histogram=None)
        assert trend_composite_from_snapshot(snap) == pytest.approx(90.0)
