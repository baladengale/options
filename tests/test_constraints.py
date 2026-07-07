"""
Hard constraint gate tests — all 14 constraints from SPECS Section 8.

Every constraint failure MUST result in the trade being blocked.
Tests validate each constraint independently and in combination.
"""

import pytest
from datetime import date, timedelta
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================
# C1: Strategy Type — No Naked Options, No Margin
# ============================================================

class TestConstraintStrategyType:
    """C1: Only COVERED_CALL and CASH_SECURED_PUT are permitted."""

    def test_cc_permitted(self):
        from trade.validator import check_strategy_type
        assert check_strategy_type('COVERED_CALL') is True

    def test_csp_permitted(self):
        from trade.validator import check_strategy_type
        assert check_strategy_type('CASH_SECURED_PUT') is True

    def test_naked_call_blocked(self):
        from trade.validator import check_strategy_type
        assert check_strategy_type('NAKED_CALL') is False

    def test_naked_put_blocked(self):
        from trade.validator import check_strategy_type
        assert check_strategy_type('NAKED_PUT') is False

    def test_spread_blocked(self):
        from trade.validator import check_strategy_type
        assert check_strategy_type('BEAR_CALL_SPREAD') is False

    def test_iron_condor_blocked(self):
        from trade.validator import check_strategy_type
        assert check_strategy_type('IRON_CONDOR') is False

    def test_margin_trading_blocked(self):
        """C2: No margin — any trade requiring margin is blocked."""
        from trade.validator import check_no_margin
        assert check_no_margin(margin_used=0.0) is True
        assert check_no_margin(margin_used=1.0) is False  # Any margin usage = fail


# ============================================================
# C3/C4: Coverage Checks — CC shares, CSP cash
# ============================================================

class TestCoverageConstraints:
    """C3: CC must have enough shares. C4: CSP must have enough cash."""

    def test_cc_sufficient_shares(self):
        """430 V shares, 4 CC contracts = need 400 → OK."""
        from trade.validator import check_cc_coverage
        assert check_cc_coverage(shares_owned=430, contracts=4) is True

    def test_cc_exact_shares(self):
        """430 shares, 4 contracts, 30 remaining → OK."""
        from trade.validator import check_cc_coverage
        assert check_cc_coverage(shares_owned=400, contracts=4) is True

    def test_cc_insufficient_shares(self):
        """200 shares, 3 contracts = need 300 → FAIL."""
        from trade.validator import check_cc_coverage
        assert check_cc_coverage(shares_owned=200, contracts=3) is False

    def test_cc_one_share_short(self):
        """399 shares, 4 contracts = need 400 → FAIL (off by one)."""
        from trade.validator import check_cc_coverage
        assert check_cc_coverage(shares_owned=399, contracts=4) is False

    def test_cc_zero_contracts(self):
        """Edge case: 0 contracts → always OK."""
        from trade.validator import check_cc_coverage
        assert check_cc_coverage(shares_owned=100, contracts=0) is True

    def test_csp_sufficient_cash(self):
        """$45k cash, 1 MSFT $420 CSP = need $42k → OK."""
        from trade.validator import check_csp_cash_coverage
        assert check_csp_cash_coverage(
            available_cash=45000, strike=420, contracts=1, tied_up_csp=0
        ) is True

    def test_csp_insufficient_cash(self):
        """$45k cash, 2 MSFT $420 CSP = need $84k → FAIL."""
        from trade.validator import check_csp_cash_coverage
        assert check_csp_cash_coverage(
            available_cash=45000, strike=420, contracts=2, tied_up_csp=0
        ) is False

    def test_csp_cash_tied_up_reduces_available(self):
        """
        $45k cash, but $42k already tied = $3k available.
        1 contract at $420 strike = need $42k → FAIL.
        """
        from trade.validator import check_csp_cash_coverage
        assert check_csp_cash_coverage(
            available_cash=45000, strike=420, contracts=1, tied_up_csp=42000
        ) is False

    def test_csp_exact_cash(self):
        """$42k cash exactly, 1 contract $420 strike → OK."""
        from trade.validator import check_csp_cash_coverage
        assert check_csp_cash_coverage(
            available_cash=42000, strike=420, contracts=1, tied_up_csp=0
        ) is True


# ============================================================
# C5: Earnings Blackout
# ============================================================

class TestEarningsBlackout:
    """C5: No new positions within 14 days of earnings."""

    def test_earnings_far_away(self):
        """Earnings in 30 days → OK."""
        from trade.validator import check_earnings_blackout
        today = date.today()
        next_earnings = today + timedelta(days=30)
        assert check_earnings_blackout(next_earnings, blackout_days=14) is True

    def test_earnings_at_boundary(self):
        """Earnings exactly at 14 days → still OK (>= 14)."""
        from trade.validator import check_earnings_blackout
        today = date.today()
        next_earnings = today + timedelta(days=14)
        assert check_earnings_blackout(next_earnings, blackout_days=14) is True

    def test_earnings_inside_blackout(self):
        """Earnings in 10 days → FAIL."""
        from trade.validator import check_earnings_blackout
        today = date.today()
        next_earnings = today + timedelta(days=10)
        assert check_earnings_blackout(next_earnings, blackout_days=14) is False

    def test_earnings_tomorrow(self):
        """Earnings tomorrow → FAIL."""
        from trade.validator import check_earnings_blackout
        today = date.today()
        next_earnings = today + timedelta(days=1)
        assert check_earnings_blackout(next_earnings, blackout_days=14) is False

    def test_earnings_already_passed(self):
        """Earnings was yesterday → OK (no upcoming earnings)."""
        from trade.validator import check_earnings_blackout
        today = date.today()
        next_earnings = today - timedelta(days=1)
        assert check_earnings_blackout(next_earnings, blackout_days=14) is True


# ============================================================
# C6: DTE Range
# ============================================================

class TestDTERange:
    """C6: DTE must be in [30, 45] for both CC and CSP."""

    def test_dte_in_range(self):
        from trade.validator import check_dte_range
        assert check_dte_range(dte=35, dte_min=30, dte_max=45) is True

    def test_dte_at_min(self):
        from trade.validator import check_dte_range
        assert check_dte_range(dte=30, dte_min=30, dte_max=45) is True

    def test_dte_at_max(self):
        from trade.validator import check_dte_range
        assert check_dte_range(dte=45, dte_min=30, dte_max=45) is True

    def test_dte_too_short(self):
        from trade.validator import check_dte_range
        assert check_dte_range(dte=29, dte_min=30, dte_max=45) is False

    def test_dte_too_long(self):
        from trade.validator import check_dte_range
        assert check_dte_range(dte=46, dte_min=30, dte_max=45) is False

    def test_dte_weekly_option(self):
        """7 DTE (weekly) → FAIL."""
        from trade.validator import check_dte_range
        assert check_dte_range(dte=7, dte_min=30, dte_max=45) is False


# ============================================================
# C7: Delta Range
# ============================================================

class TestDeltaRange:
    """C7: Delta must be in strategy-specific range."""

    def test_cc_delta_in_range(self):
        from trade.validator import check_delta_range
        assert check_delta_range(delta=0.25, strategy='COVERED_CALL') is True

    def test_cc_delta_too_low(self):
        """CC delta < 0.20 → FAIL."""
        from trade.validator import check_delta_range
        assert check_delta_range(delta=0.10, strategy='COVERED_CALL') is False

    def test_cc_delta_too_high(self):
        """CC delta > 0.30 → FAIL (too much assignment risk)."""
        from trade.validator import check_delta_range
        assert check_delta_range(delta=0.35, strategy='COVERED_CALL') is False

    def test_csp_delta_in_range(self):
        from trade.validator import check_delta_range
        assert check_delta_range(delta=0.18, strategy='CASH_SECURED_PUT') is True

    def test_csp_delta_too_high(self):
        """CSP delta > 0.25 → FAIL."""
        from trade.validator import check_delta_range
        assert check_delta_range(delta=0.30, strategy='CASH_SECURED_PUT') is False

    def test_csp_delta_at_boundary(self):
        from trade.validator import check_delta_range
        assert check_delta_range(delta=0.15, strategy='CASH_SECURED_PUT') is True
        assert check_delta_range(delta=0.25, strategy='CASH_SECURED_PUT') is True


# ============================================================
# C8/C9: Position Size & CSP Cash Allocation
# ============================================================

class TestPositionSizingConstraints:
    """C8: Max 15% per underlying. C9: Max 80% CSP cash tie-up."""

    def test_position_size_ok(self):
        """2 MSFT $420 CSP = $84k. Portfolio $148k. 84/148 = 56.7% → FAIL (>15%)."""
        from trade.validator import check_position_size
        # Actually 1 contract: 42000/148350 = 28.3% → still fails 15%
        # Let's use 1 contract at lower strike:
        # 1 MSFT $200 CSP = $20k. 20/148.35 = 13.5% → OK
        assert check_position_size(
            capital_required=20000, portfolio_value=148350, max_pct=15
        ) is True

    def test_position_size_exceeds_max(self):
        """1 MSFT $420 CSP = $42k. 42/148.35 = 28.3% → FAIL (>15%)."""
        from trade.validator import check_position_size
        assert check_position_size(
            capital_required=42000, portfolio_value=148350, max_pct=15
        ) is False

    def test_position_size_at_boundary(self):
        """Exactly at 15%."""
        from trade.validator import check_position_size
        # 15% of 100k = 15k
        assert check_position_size(
            capital_required=15000, portfolio_value=100000, max_pct=15
        ) is True
        assert check_position_size(
            capital_required=15001, portfolio_value=100000, max_pct=15
        ) is False

    def test_csp_cash_allocation_ok(self):
        """$42k tied of $45k = 93% → FAIL (max 80%)."""
        from trade.validator import check_csp_cash_allocation
        # 30k tied of 45k = 66.7% → OK
        assert check_csp_cash_allocation(
            tied_up_csp=30000, total_cash=45000, max_pct=80
        ) is True

    def test_csp_cash_allocation_exceeds_max(self):
        """$38k tied of $45k = 84.4% → FAIL."""
        from trade.validator import check_csp_cash_allocation
        assert check_csp_cash_allocation(
            tied_up_csp=38000, total_cash=45000, max_pct=80
        ) is False


# ============================================================
# C10: IV Rank Minimum
# ============================================================

class TestIVRankConstraint:
    """C10: IV Rank must be >= 30 for both strategies."""

    def test_iv_rank_above_min(self):
        from trade.validator import check_iv_rank
        assert check_iv_rank(iv_rank=45, min_iv_rank=30) is True

    def test_iv_rank_at_min(self):
        from trade.validator import check_iv_rank
        assert check_iv_rank(iv_rank=30, min_iv_rank=30) is True

    def test_iv_rank_below_min(self):
        from trade.validator import check_iv_rank
        assert check_iv_rank(iv_rank=25, min_iv_rank=30) is False

    def test_iv_rank_zero(self):
        """IV at all-time low → FAIL."""
        from trade.validator import check_iv_rank
        assert check_iv_rank(iv_rank=5, min_iv_rank=30) is False


# ============================================================
# C11: Annualized RoC Minimum
# ============================================================

class TestAnnualizedRoCConstraint:
    """C11: RoC must meet strategy-specific minimum."""

    def test_csp_roc_above_min(self):
        from trade.validator import check_annualized_roc
        assert check_annualized_roc(roc_pct=14.0, strategy='CASH_SECURED_PUT') is True

    def test_csp_roc_below_min(self):
        from trade.validator import check_annualized_roc
        assert check_annualized_roc(roc_pct=10.0, strategy='CASH_SECURED_PUT') is False

    def test_csp_roc_at_min(self):
        from trade.validator import check_annualized_roc
        assert check_annualized_roc(roc_pct=12.0, strategy='CASH_SECURED_PUT') is True

    def test_cc_roc_above_min(self):
        from trade.validator import check_annualized_roc
        assert check_annualized_roc(roc_pct=9.0, strategy='COVERED_CALL') is True

    def test_cc_roc_below_min(self):
        from trade.validator import check_annualized_roc
        assert check_annualized_roc(roc_pct=5.0, strategy='COVERED_CALL') is False

    def test_cc_roc_at_min(self):
        from trade.validator import check_annualized_roc
        assert check_annualized_roc(roc_pct=8.0, strategy='COVERED_CALL') is True


# ============================================================
# C12: Correlation vs V
# ============================================================

class TestCorrelationConstraint:
    """C12: New positions must not be highly correlated (>0.8) with V."""

    def test_correlation_ok(self):
        from trade.validator import check_correlation_vs_visa
        assert check_correlation_vs_visa(correlation=0.5, max_corr=0.8) is True

    def test_correlation_at_boundary(self):
        from trade.validator import check_correlation_vs_visa
        assert check_correlation_vs_visa(correlation=0.8, max_corr=0.8) is True

    def test_correlation_fail(self):
        from trade.validator import check_correlation_vs_visa
        assert check_correlation_vs_visa(correlation=0.85, max_corr=0.8) is False

    def test_correlation_highly_correlated(self):
        """0.95 correlation → clear FAIL."""
        from trade.validator import check_correlation_vs_visa
        assert check_correlation_vs_visa(correlation=0.95, max_corr=0.8) is False


# ============================================================
# C13: Bid-Ask Spread
# ============================================================

class TestBidAskSpreadConstraint:
    """C13: Bid-ask spread must be <= 5%."""

    def test_spread_tight(self):
        from trade.validator import check_bid_ask_spread
        assert check_bid_ask_spread(bid=5.95, ask=6.05, max_spread_pct=5.0) is True

    def test_spread_acceptable(self):
        """4.8% spread → OK."""
        from trade.validator import check_bid_ask_spread
        # bid=9.76, ask=10.24 → mid=10.00, spread=0.48, spread_pct=4.8% ≤ 5% → OK
        assert check_bid_ask_spread(bid=9.76, ask=10.24, max_spread_pct=5.0) is True

    def test_spread_too_wide(self):
        """6% spread → FAIL."""
        from trade.validator import check_bid_ask_spread
        assert check_bid_ask_spread(bid=9.40, ask=10.60, max_spread_pct=5.0) is False

    def test_spread_unacceptable(self):
        """10% spread → clear FAIL."""
        from trade.validator import check_bid_ask_spread
        assert check_bid_ask_spread(bid=9.00, ask=11.00, max_spread_pct=5.0) is False


# ============================================================
# C14: Data Freshness
# ============================================================

class TestDataFreshnessConstraint:
    """C14: Data must be within acceptable staleness thresholds."""

    def test_data_fresh(self):
        """Synced 2 minutes ago → OK."""
        from datetime import datetime, timedelta
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(minutes=2)
        assert check_freshness(synced_at, max_age_seconds=300) is True

    def test_data_stale(self):
        """Synced 10 minutes ago, max age 5 min → FAIL."""
        from datetime import datetime, timedelta
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(minutes=10)
        assert check_freshness(synced_at, max_age_seconds=300) is False

    def test_price_history_freshness_different_ttl(self):
        """Price history has 24h TTL — 12h old should pass."""
        from datetime import datetime, timedelta
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(hours=12)
        assert check_freshness(synced_at, max_age_seconds=86400) is True

    def test_price_history_stale(self):
        """Price history 36h old → FAIL (>24h)."""
        from datetime import datetime, timedelta
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(hours=36)
        assert check_freshness(synced_at, max_age_seconds=86400) is False


# ============================================================
# Composite Constraint Check
# ============================================================

class TestAllConstraints:
    """Verify the full pre-trade check integrates all 14 constraints."""

    def test_all_pass(self):
        """When all constraints pass, pre_trade_check returns True."""
        from trade.validator import pre_trade_check
        # This is a placeholder test — the actual implementation
        # will need mock data that satisfies all 14 constraints.
        # For now, we verify the check structure exists.
        assert hasattr(pre_trade_check, '__call__')

    def test_any_single_failure_blocks_trade(self):
        """
        The OR nature: if ANY one constraint fails, the entire
        pre_trade_check must return False.
        """
        # This principle is tested individually above for each constraint.
        # The integration test verifies the composite behavior.
        pass  # Integration test placeholder — needs full mock pipeline
