"""
Sentiment scoring engine tests — validates the deterministic sentiment
formula per SPECS Section 6.

The sentiment score is a composite of 5 sub-components:
- Trend sentiment (30%)
- Momentum sentiment (25%)
- IV sentiment (20%)
- Volume/OI sentiment (15%)
- Price action sentiment (10%)
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================
# Sub-Component Tests
# ============================================================

class TestIVSentiment:
    """Validate IV Rank → sentiment mapping."""

    def test_iv_rank_high(self):
        """IV Rank ≥ 60 → 100 (rich premium, high conviction)."""
        from signals.sentiment import compute_iv_sentiment
        assert compute_iv_sentiment(65) == 100
        assert compute_iv_sentiment(60) == 100

    def test_iv_rank_moderate(self):
        from signals.sentiment import compute_iv_sentiment
        assert compute_iv_sentiment(55) == 85   # 50-60
        assert compute_iv_sentiment(45) == 70   # 40-50
        assert compute_iv_sentiment(35) == 55   # 30-40

    def test_iv_rank_low(self):
        from signals.sentiment import compute_iv_sentiment
        assert compute_iv_sentiment(25) == 35   # 20-30
        assert compute_iv_sentiment(15) == 20   # 10-20
        assert compute_iv_sentiment(5) == 10    # < 10

    def test_iv_hv_adjustment(self):
        """IV > HV by >20% → -10 penalty."""
        from signals.sentiment import compute_iv_sentiment
        # IV rank 65 → base 100, but IV 50% vs HV 25% (25% diff > 20%) → -10
        score = compute_iv_sentiment(65, iv=0.50, hv=0.25)
        assert score == 90  # 100 - 10

    def test_iv_hv_fair_value(self):
        """IV ≈ HV → no adjustment."""
        from signals.sentiment import compute_iv_sentiment
        score = compute_iv_sentiment(45, iv=0.30, hv=0.29)
        assert score == 70  # 70 + 0 adjustment


class TestVolumeOISentiment:
    """Validate volume/OI sentiment mapping."""

    def test_strong_institutional_interest(self):
        """Volume ratio ≥ 1.5 AND OI ≥ 1000 → 100."""
        from signals.sentiment import compute_volume_oi_sentiment
        assert compute_volume_oi_sentiment(volume_ratio=1.8, oi=2000) == 100

    def test_normal_volume(self):
        from signals.sentiment import compute_volume_oi_sentiment
        assert compute_volume_oi_sentiment(volume_ratio=1.2, oi=600) == 80

    def test_low_volume(self):
        from signals.sentiment import compute_volume_oi_sentiment
        assert compute_volume_oi_sentiment(volume_ratio=0.6, oi=200) == 40

    def test_very_low_volume(self):
        from signals.sentiment import compute_volume_oi_sentiment
        assert compute_volume_oi_sentiment(volume_ratio=0.3, oi=50) == 20

    def test_high_oi_but_low_volume(self):
        """High OI but low relative volume → mixed signal."""
        from signals.sentiment import compute_volume_oi_sentiment
        # OI ≥ 1000 but volume ratio < 0.5 → bucket 3 or 4 based on OI
        # OI ≥ 1000 with vol ratio 0.5-0.7 → actually that's bucket 3 → 40
        # Wait: vol_ratio 0.6 falls in 0.5-0.7 OR OI 100-500 → 40
        # But OI is 1500, so it's not "OR OI 100-500"
        # The spec says: vol 0.5-0.7 OR OI 100-500 → 40
        # We need to interpret: the condition uses OR
        assert compute_volume_oi_sentiment(volume_ratio=0.6, oi=1500) == 40


class TestPriceActionSentiment:
    """Validate price action sentiment for CSP vs CC."""

    def test_near_support_csp(self):
        """Price within 5% of 20d low → 100 for CSP (good entry)."""
        from signals.sentiment import compute_price_action_sentiment
        # price = 105, 20d low = 100 → 5% above → 100
        score = compute_price_action_sentiment(
            price=105.0, high_20d=130.0, low_20d=100.0,
            strategy='CASH_SECURED_PUT'
        )
        assert score == 100

    def test_near_high_csp(self):
        """Price near 20d high → 20 for CSP (poor entry)."""
        from signals.sentiment import compute_price_action_sentiment
        score = compute_price_action_sentiment(
            price=128.0, high_20d=130.0, low_20d=100.0,
            strategy='CASH_SECURED_PUT'
        )
        assert score == 20

    def test_near_high_cc(self):
        """Near 20d high → 100 for CC (good premium)."""
        from signals.sentiment import compute_price_action_sentiment
        score = compute_price_action_sentiment(
            price=128.0, high_20d=130.0, low_20d=100.0,
            strategy='COVERED_CALL'
        )
        assert score == 100

    def test_near_low_cc(self):
        """Near 20d low → 30 for CC (poor premium)."""
        from signals.sentiment import compute_price_action_sentiment
        score = compute_price_action_sentiment(
            price=102.0, high_20d=130.0, low_20d=100.0,
            strategy='COVERED_CALL'
        )
        assert score == 30

    def test_mid_range(self):
        """Price in middle of range — neither near low nor near high."""
        from signals.sentiment import compute_price_action_sentiment
        # Use a price clearly in between: pct_above_low > 15 and pct_below_high > 15
        # Range 100-200: price=150: pct_above_low=50%, pct_below_high=25% → mid-range
        score = compute_price_action_sentiment(
            price=150.0, high_20d=200.0, low_20d=100.0,
            strategy='CASH_SECURED_PUT'
        )
        assert score == 50


# ============================================================
# Composite Sentiment Score Tests
# ============================================================

class TestSentimentComposite:
    """Validate SENTIMENT_SCORE formula with weights."""

    def test_perfect_sentiment(self):
        """
        All sub-components at 100:
        SENTIMENT = 0.30×100 + 0.25×100 + 0.20×100 + 0.15×100 + 0.10×100 = 100
        """
        from signals.sentiment import compute_sentiment_score
        score = compute_sentiment_score(
            trend_sentiment=100.0,
            momentum_sentiment=100.0,
            iv_sentiment=100.0,
            volume_oi_sentiment=100.0,
            price_action_sentiment=100.0,
        )
        assert score == 100.0

    def test_mixed_sentiment(self):
        """
        0.30×80 + 0.25×70 + 0.20×60 + 0.15×50 + 0.10×40
        = 24 + 17.5 + 12 + 7.5 + 4 = 65.0
        """
        from signals.sentiment import compute_sentiment_score
        score = compute_sentiment_score(
            trend_sentiment=80.0,
            momentum_sentiment=70.0,
            iv_sentiment=60.0,
            volume_oi_sentiment=50.0,
            price_action_sentiment=40.0,
        )
        assert score == pytest.approx(65.0, abs=0.01)

    def test_sentiment_direction_bullish(self):
        """Score ≥ 70 → BULLISH."""
        from signals.sentiment import sentiment_direction
        assert sentiment_direction(75.0) == 'BULLISH'
        assert sentiment_direction(70.0) == 'BULLISH'

    def test_sentiment_direction_neutral(self):
        """Score 45-70 → NEUTRAL."""
        from signals.sentiment import sentiment_direction
        assert sentiment_direction(60.0) == 'NEUTRAL'
        assert sentiment_direction(45.0) == 'NEUTRAL'

    def test_sentiment_direction_cautious(self):
        """Score 30-45 → CAUTIOUS."""
        from signals.sentiment import sentiment_direction
        assert sentiment_direction(40.0) == 'CAUTIOUS'
        assert sentiment_direction(30.0) == 'CAUTIOUS'

    def test_sentiment_direction_bearish(self):
        """Score < 30 → BEARISH."""
        from signals.sentiment import sentiment_direction
        assert sentiment_direction(25.0) == 'BEARISH'
        assert sentiment_direction(0.0) == 'BEARISH'

    def test_sentiment_bounds(self):
        """Score must stay in [0, 100]."""
        from signals.sentiment import compute_sentiment_score
        score = compute_sentiment_score(0, 0, 0, 0, 0)
        assert 0.0 <= score <= 100.0
        score = compute_sentiment_score(100, 100, 100, 100, 100)
        assert 0.0 <= score <= 100.0


# ============================================================
# Strategy-Specific Sentiment Rules
# ============================================================

class TestStrategySentimentRules:
    """Validate that CSP and CC have different sentiment entry rules."""

    def test_csp_enter_only_bullish_neutral(self):
        """CSP: only enter if direction is BULLISH or NEUTRAL."""
        from signals.sentiment import can_enter_csp
        assert can_enter_csp('BULLISH') is True
        assert can_enter_csp('NEUTRAL') is True
        assert can_enter_csp('CAUTIOUS') is False
        assert can_enter_csp('BEARISH') is False

    def test_cc_enter_neutral_cautious(self):
        """CC: enter new CC if NEUTRAL or CAUTIOUS (neutral-to-bearish = better premium)."""
        from signals.sentiment import can_enter_cc
        assert can_enter_cc('BULLISH') is False   # don't cap strong upside
        assert can_enter_cc('NEUTRAL') is True
        assert can_enter_cc('CAUTIOUS') is True
        assert can_enter_cc('BEARISH') is False   # don't sell calls into a crash

    def test_cc_existing_shares_always_write_except_bearish(self):
        """For existing shares, write CC in any direction except BEARISH."""
        from signals.sentiment import can_write_cc_on_existing
        assert can_write_cc_on_existing('BULLISH') is True
        assert can_write_cc_on_existing('NEUTRAL') is True
        assert can_write_cc_on_existing('CAUTIOUS') is True
        assert can_write_cc_on_existing('BEARISH') is False
