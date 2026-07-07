"""
Scoring engine tests — concrete deterministic formula validation.

Each test verifies one sub-component of the WHEEL_SCORE with
exact numerical inputs and expected outputs.
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================
# Fundamental Score Tests
# ============================================================

class TestFundamentalScore:
    """Validate fundamental sub-score calculation (Section 7.2.2 of SPECS)."""

    def test_fund_score_strong(self, strong_fundamentals):
        """
        Strong fundamentals (like MSFT):
        - Revenue growth 18.5% → bucket 15-20% → 80
        - EPS 4/4 positive → 100
        - FCF yield 3.8% → bucket 3-5% → 80
        - D/E 0.25 → bucket <0.3 → 100
        - PEG 1.2 → bucket 1.0-1.5 → 80
        Expected avg: (80+100+80+100+80)/5 = 88.0
        """
        from scoring.engine import compute_fundamental_score
        score = compute_fundamental_score(strong_fundamentals)
        assert score == pytest.approx(88.0, abs=0.01)
        assert 80 <= score <= 100

    def test_fund_score_weak(self, weak_fundamentals):
        """
        Weak fundamentals:
        - Revenue growth -5% → negative → 0
        - EPS 1/4 positive → 25
        - FCF yield -2% → negative → 0
        - D/E 3.5 → >2.0 → 20
        - PEG -5.0 → >3.0 or negative → 20
        Expected avg: (0+25+0+20+20)/5 = 13.0
        """
        from scoring.engine import compute_fundamental_score
        score = compute_fundamental_score(weak_fundamentals)
        assert score == pytest.approx(13.0, abs=0.01)
        assert score < 40  # Should trigger REJECT threshold

    def test_fund_score_perfect(self):
        """All buckets at max → 100."""
        perfect = {
            'revenue_growth_yoy_pct': 25.0,
            'eps_quarters_positive': 4,
            'fcf_yield_pct': 6.0,
            'debt_to_equity': 0.1,
            'peg_ratio': 0.8,
        }
        from scoring.engine import compute_fundamental_score
        score = compute_fundamental_score(perfect)
        assert score == 100.0

    def test_fund_score_revenue_growth_buckets(self):
        """Each revenue growth bucket maps correctly."""
        from scoring.engine import _revenue_growth_score
        assert _revenue_growth_score(25.0) == 100  # ≥ 20%
        assert _revenue_growth_score(18.0) == 80   # 15-20%
        assert _revenue_growth_score(12.0) == 60   # 10-15%
        assert _revenue_growth_score(7.0) == 40    # 5-10%
        assert _revenue_growth_score(3.0) == 20    # 0-5%
        assert _revenue_growth_score(0.0) == 20    # 0% → edge of 0-5%
        assert _revenue_growth_score(-3.0) == 0    # negative

    def test_fund_score_eps_buckets(self):
        """EPS quality buckets."""
        from scoring.engine import _eps_quality_score
        assert _eps_quality_score(4) == 100
        assert _eps_quality_score(3) == 75
        assert _eps_quality_score(2) == 50
        assert _eps_quality_score(1) == 25
        assert _eps_quality_score(0) == 0

    def test_fund_score_fcf_buckets(self):
        """FCF yield buckets."""
        from scoring.engine import _fcf_yield_score
        assert _fcf_yield_score(6.0) == 100
        assert _fcf_yield_score(4.0) == 80
        assert _fcf_yield_score(2.0) == 60
        assert _fcf_yield_score(0.5) == 30
        assert _fcf_yield_score(0.0) == 30  # edge
        assert _fcf_yield_score(-1.0) == 0

    def test_fund_score_debt_to_equity_buckets(self):
        """D/E buckets."""
        from scoring.engine import _debt_to_equity_score
        assert _debt_to_equity_score(0.1) == 100
        assert _debt_to_equity_score(0.5) == 80
        assert _debt_to_equity_score(0.8) == 60
        assert _debt_to_equity_score(1.5) == 40
        assert _debt_to_equity_score(3.0) == 20
        # Negative equity
        assert _debt_to_equity_score(-0.5) == 0

    def test_fund_score_peg_buckets(self):
        """PEG ratio buckets."""
        from scoring.engine import _peg_ratio_score
        assert _peg_ratio_score(0.5) == 100
        assert _peg_ratio_score(1.2) == 80
        assert _peg_ratio_score(1.8) == 60
        assert _peg_ratio_score(2.5) == 40
        assert _peg_ratio_score(4.0) == 20
        assert _peg_ratio_score(-1.0) == 20  # negative = poor
        assert _peg_ratio_score(None) == 50  # no estimate = neutral


# ============================================================
# Trend / Momentum Score Tests
# ============================================================

class TestTrendAlignment:
    """Validate SMA alignment scoring (Section 5.1 of SPECS)."""

    def test_bullish_alignment_csp(self):
        """Price > SMA20 > SMA50 > SMA200 → 3 bullish → 100 for CSP."""
        from analysis.trend import compute_trend_alignment
        score = compute_trend_alignment(
            price=430.0, sma20=420.0, sma50=410.0, sma200=390.0,
            strategy='CASH_SECURED_PUT'
        )
        assert score == 100.0

    def test_mixed_alignment_csp(self):
        """2 bullish pairs, 1 bearish → 67 for CSP."""
        from analysis.trend import compute_trend_alignment
        # price > SMA20, SMA20 > SMA50, BUT SMA50 < SMA200
        score = compute_trend_alignment(
            price=430.0, sma20=420.0, sma50=410.0, sma200=415.0,  # SMA50 < SMA200
            strategy='CASH_SECURED_PUT'
        )
        assert score == pytest.approx(66.67, abs=0.1)

    def test_bearish_alignment_csp(self):
        """Price < SMA20 < SMA50 < SMA200 → 0 bullish → 0 for CSP."""
        from analysis.trend import compute_trend_alignment
        score = compute_trend_alignment(
            price=380.0, sma20=390.0, sma50=400.0, sma200=410.0,
            strategy='CASH_SECURED_PUT'
        )
        assert score == 0.0

    def test_cc_floor_at_30(self):
        """CC trend alignment floors at 30 (you already own shares)."""
        from analysis.trend import compute_trend_alignment
        score = compute_trend_alignment(
            price=380.0, sma20=390.0, sma50=400.0, sma200=410.0,
            strategy='COVERED_CALL'
        )
        assert score == 30.0  # floored


class TestADXStrength:
    """Validate ADX trend strength scoring."""

    def test_adx_strong_trend(self):
        from analysis.trend import compute_adx_strength_score
        assert compute_adx_strength_score(45) == 100

    def test_adx_moderate_trend(self):
        from analysis.trend import compute_adx_strength_score
        assert compute_adx_strength_score(30) == 75

    def test_adx_weak_trend(self):
        from analysis.trend import compute_adx_strength_score
        assert compute_adx_strength_score(22) == 50

    def test_adx_ranging(self):
        from analysis.trend import compute_adx_strength_score
        assert compute_adx_strength_score(15) == 25


class TestRSIScore:
    """Validate RSI scoring buckets (Section 5.2)."""

    def test_rsi_ideal_csp(self):
        """RSI 45-55 is ideal for CSP → 100."""
        from analysis.trend import compute_rsi_score
        assert compute_rsi_score(50, 'CASH_SECURED_PUT') == 100

    def test_rsi_mildly_oversold_csp(self):
        """RSI 40-45 for CSP → 85."""
        from analysis.trend import compute_rsi_score
        assert compute_rsi_score(42, 'CASH_SECURED_PUT') == 85

    def test_rsi_extreme_oversold_csp(self):
        """RSI < 30 for CSP → 15."""
        from analysis.trend import compute_rsi_score
        assert compute_rsi_score(25, 'CASH_SECURED_PUT') == 15

    def test_rsi_extreme_overbought_csp(self):
        """RSI > 75 for CSP → 10."""
        from analysis.trend import compute_rsi_score
        assert compute_rsi_score(80, 'CASH_SECURED_PUT') == 10

    def test_rsi_ideal_cc(self):
        """RSI 45-55 for CC → 100 (neutral is also fine)."""
        from analysis.trend import compute_rsi_score
        assert compute_rsi_score(50, 'COVERED_CALL') == 100

    def test_rsi_overbought_cc(self):
        """RSI > 75 for CC → 40 (great premium but reversal risk)."""
        from analysis.trend import compute_rsi_score
        assert compute_rsi_score(78, 'COVERED_CALL') == 40


class TestMACDScore:
    """Validate MACD scoring logic."""

    def test_macd_bullish_accelerating_csp(self):
        """MACD > Signal, histogram positive and growing → 100 for CSP."""
        from analysis.trend import compute_macd_score
        score = compute_macd_score(
            macd=2.5, signal=1.0, histogram=1.5, prev_histogram=1.0,
            strategy='CASH_SECURED_PUT'
        )
        assert score == 100

    def test_macd_bearish_accelerating_csp(self):
        """MACD < Signal, histogram negative and shrinking → 30 for CSP."""
        from analysis.trend import compute_macd_score
        score = compute_macd_score(
            macd=-2.0, signal=-1.0, histogram=-1.0, prev_histogram=-0.5,
            strategy='CASH_SECURED_PUT'
        )
        assert score == 30


# ============================================================
# Options Chain Quality Tests
# ============================================================

class TestOptionsChainQuality:
    """Validate options chain quality scoring (Section 7.2.3)."""

    def test_bid_ask_spread_tight(self):
        from analysis.options_chain import compute_spread_score
        # spread = (ask - bid) / mid
        # mid = (6.10 + 5.80)/2 = 5.95, spread = 0.30/5.95 = 5.04% → wait
        # Let's use a tight spread: bid=5.98, ask=6.02, mid=6.00, spread=0.04/6=0.67%
        assert compute_spread_score(5.98, 6.02) == pytest.approx(80, abs=1)

    def test_bid_ask_spread_institutional(self):
        from analysis.options_chain import compute_spread_score
        # spread = 0.02/10.00 = 0.2%
        assert compute_spread_score(9.99, 10.01) == 100

    def test_bid_ask_spread_wide(self):
        from analysis.options_chain import compute_spread_score
        # spread = 2.0/50.0 = 4% → bucket 2-5%
        assert compute_spread_score(49.00, 51.00) == 40

    def test_bid_ask_spread_unacceptable(self):
        from analysis.options_chain import compute_spread_score
        # spread > 5%
        assert compute_spread_score(8.00, 10.00) == 20

    def test_open_interest_score(self):
        from analysis.options_chain import compute_oi_score
        assert compute_oi_score(2000) == 100
        assert compute_oi_score(750) == 80
        assert compute_oi_score(300) == 60
        assert compute_oi_score(75) == 40
        assert compute_oi_score(30) == 20

    def test_volume_score(self):
        from analysis.options_chain import compute_volume_score
        assert compute_volume_score(800) == 100
        assert compute_volume_score(300) == 70
        assert compute_volume_score(75) == 40
        assert compute_volume_score(20) == 10

    def test_iv_hv_spread_score(self):
        from analysis.options_chain import compute_iv_hv_spread_score
        assert compute_iv_hv_spread_score(0.30, 0.28) == 100  # diff 2 percentage pts → ≤ 5
        assert compute_iv_hv_spread_score(0.35, 0.28) == 70   # diff 7 percentage pts → 5-10
        assert compute_iv_hv_spread_score(0.40, 0.28) == 40   # diff 12 percentage pts → 10-20
        assert compute_iv_hv_spread_score(0.55, 0.28) == 20   # diff 27 percentage pts → > 20

    def test_options_chain_composite_liquid(self, liquid_options_chain):
        """Full options chain quality for a liquid ticker should score well."""
        from analysis.options_chain import compute_options_chain_score
        # Test the MSFT $420 PUT contract (the most liquid in our fixture)
        contract = liquid_options_chain['options'][2]  # strike 420 PUT
        score = compute_options_chain_score(contract)
        # High OI (3200), high volume (1200), tight spread, fair IV→ should be ≥ 80
        assert score >= 75

    def test_options_chain_composite_illiquid(self, illiquid_options_chain):
        """Illiquid chain should score poorly."""
        from analysis.options_chain import compute_options_chain_score
        contract = illiquid_options_chain['options'][0]  # strike 45 PUT
        score = compute_options_chain_score(contract)
        # Wide spread, low OI (30), low volume (5)
        assert score <= 40


# ============================================================
# Composite WHEEL_SCORE Tests
# ============================================================

class TestWheelScore:
    """Validate composite WHEEL_SCORE computation."""

    def test_all_components_max_all_constraints_pass(self):
        """
        When all components are 100 and all constraints pass,
        composite should be 100.
        """
        from scoring.engine import compute_wheel_score
        result = compute_wheel_score(
            ticker='PERFECT',
            strategy='CASH_SECURED_PUT',
            trend_momentum=100.0,
            fundamental=100.0,
            options_chain=100.0,
            sentiment=100.0,
            correlation=100.0,
            constraints_pass=True,
        )
        assert result['composite_score'] == 100.0

    def test_any_constraint_fail_zeros_score(self):
        """
        Even with perfect component scores, a single constraint
        failure must force composite_score = 0.
        """
        from scoring.engine import compute_wheel_score
        result = compute_wheel_score(
            ticker='FAILED',
            strategy='CASH_SECURED_PUT',
            trend_momentum=95.0,
            fundamental=90.0,
            options_chain=88.0,
            sentiment=85.0,
            correlation=92.0,
            constraints_pass=False,  # <-- GATE CLOSED
        )
        assert result['composite_score'] == 0.0
        assert result['signal'] == 'AVOID'

    def test_weighted_average(self):
        """
        Verify the weighted calculation:
        weights: trend=0.25, options=0.25, fund=0.20, sentiment=0.15, corr=0.15
        (80×0.25) + (70×0.25) + (90×0.20) + (60×0.15) + (85×0.15)
        = 20 + 17.5 + 18 + 9 + 12.75 = 77.25
        """
        from scoring.engine import compute_wheel_score
        result = compute_wheel_score(
            ticker='TEST',
            strategy='CASH_SECURED_PUT',
            trend_momentum=80.0,
            fundamental=90.0,
            options_chain=70.0,
            sentiment=60.0,
            correlation=85.0,
            constraints_pass=True,
        )
        assert result['composite_score'] == pytest.approx(77.25, abs=0.01)

    def test_signal_strong_write(self):
        """Composite ≥ 70 with all constraints → STRONG_WRITE or WRITE."""
        from scoring.engine import compute_wheel_score
        result = compute_wheel_score(
            ticker='MSFT',
            strategy='CASH_SECURED_PUT',
            trend_momentum=85.0,
            fundamental=80.0,
            options_chain=82.0,
            sentiment=75.0,
            correlation=80.0,
            constraints_pass=True,
        )
        # (85×0.25)+(80×0.20)+(82×0.25)+(75×0.15)+(80×0.15) = 21.25+16+20.5+11.25+12 = 81.0
        assert result['composite_score'] == pytest.approx(81.0, abs=0.01)
        assert result['signal'] in ('STRONG_WRITE', 'WRITE')

    def test_signal_hold(self):
        """Composite in middle range → HOLD."""
        from scoring.engine import compute_wheel_score
        result = compute_wheel_score(
            ticker='MID',
            strategy='CASH_SECURED_PUT',
            trend_momentum=55.0,
            fundamental=60.0,
            options_chain=55.0,
            sentiment=50.0,
            correlation=60.0,
            constraints_pass=True,
        )
        # (55×0.25)+(60×0.20)+(55×0.25)+(50×0.15)+(60×0.15) = 13.75+12+13.75+7.5+9 = 56.0
        assert result['composite_score'] == pytest.approx(56.0, abs=0.01)
        assert result['signal'] == 'HOLD'

    def test_signal_avoid_on_constraint_fail(self):
        """Any constraint fail → AVOID, regardless of score."""
        from scoring.engine import compute_wheel_score
        result = compute_wheel_score(
            ticker='FAIL',
            strategy='CASH_SECURED_PUT',
            trend_momentum=90.0,
            fundamental=90.0,
            options_chain=90.0,
            sentiment=90.0,
            correlation=90.0,
            constraints_pass=False,
        )
        assert result['composite_score'] == 0.0
        assert result['signal'] == 'AVOID'


# ============================================================
# Correlation Score Tests
# ============================================================

class TestCorrelationScore:
    """Validate correlation penalty scoring."""

    def test_low_correlation_good(self):
        """Correlation 0.3 → (1-0.3)×100 = 70."""
        from analysis.correlation import compute_correlation_score
        score = compute_correlation_score(
            correlations={'V': 0.30},
            max_threshold=0.8
        )
        assert score == 70.0

    def test_moderate_correlation(self):
        """Correlation 0.5 → (1-0.5)×100 = 50."""
        from analysis.correlation import compute_correlation_score
        score = compute_correlation_score(
            correlations={'V': 0.50},
            max_threshold=0.8
        )
        assert score == 50.0

    def test_high_correlation_hard_fail(self):
        """Correlation > 0.8 → 0 (hard fail)."""
        from analysis.correlation import compute_correlation_score
        score = compute_correlation_score(
            correlations={'V': 0.85},
            max_threshold=0.8
        )
        assert score == 0.0  # HARD FAIL

    def test_correlation_at_threshold(self):
        """Correlation exactly at 0.8 should still pass."""
        from analysis.correlation import compute_correlation_score
        score = compute_correlation_score(
            correlations={'V': 0.80},
            max_threshold=0.8
        )
        assert score == 20.0  # (1-0.8)×100 = 20, not zero

    def test_no_correlation(self):
        """Correlation 0.0 → 100."""
        from analysis.correlation import compute_correlation_score
        score = compute_correlation_score(
            correlations={'V': 0.0},
            max_threshold=0.8
        )
        assert score == 100.0


# ============================================================
# Annualized RoC Tests
# ============================================================

class TestAnnualizedRoC:
    """Validate annualized Return on Capital formulas (SPECS Section 9)."""

    def test_csp_roc(self):
        """
        CSP RoC = (premium_per_share / strike) × (365 / DTE) × 100
        MSFT CSP: strike $420, premium $6.50, DTE 42
        RoC = (6.50 / 420) × (365 / 42) × 100 = 13.44%
        """
        from trade.position_sizer import compute_csp_roc
        roc = compute_csp_roc(premium_per_share=6.50, strike=420.0, dte=42)
        assert roc == pytest.approx(13.44, abs=0.01)

    def test_cc_roc(self):
        """
        CC RoC = (premium_per_share / cost_basis) × (365 / DTE) × 100
        V CC: cost basis $270, premium $3.20, DTE 35
        RoC = (3.20 / 270) × (365 / 35) × 100 = 12.36%
        """
        from trade.position_sizer import compute_cc_roc
        roc = compute_cc_roc(premium_per_share=3.20, cost_basis=270.0, dte=35)
        assert roc == pytest.approx(12.36, abs=0.01)

    def test_csp_roc_below_minimum(self):
        """CSP RoC below 12% should fail constraint."""
        from trade.position_sizer import compute_csp_roc
        roc = compute_csp_roc(premium_per_share=2.00, strike=420.0, dte=45)
        assert roc == pytest.approx(3.86, abs=0.1)
        assert roc < 12.0  # Below CSP minimum

    def test_cc_roc_below_minimum(self):
        """CC RoC below 8% should fail constraint."""
        from trade.position_sizer import compute_cc_roc
        roc = compute_cc_roc(premium_per_share=1.00, cost_basis=270.0, dte=45)
        assert roc == pytest.approx(3.00, abs=0.1)
        assert roc < 8.0  # Below CC minimum
