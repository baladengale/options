"""
Holdings exit framework tests — the stock-leg loss rules.

Validates (loss-management-playbook.md §4-§6, Decision #10):
- Drawdown-from-basis math
- 200 SMA slope helper
- Price backstops: -40% circuit breaker (unconditional), -30% conditional
- Dead-zone detection
- Months-to-recover capacity math
- Time stop vs premium-yield alternative
- Composite severity ordering
- Roll-chain / campaign accounting (risk.monitor)
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================
# Drawdown & SMA Slope
# ============================================================

class TestDrawdownFromBasis:

    def test_underwater(self):
        from risk.holdings_exit import drawdown_from_basis
        assert drawdown_from_basis(price=70.0, adjusted_basis=100.0) == pytest.approx(0.30)

    def test_at_basis(self):
        from risk.holdings_exit import drawdown_from_basis
        assert drawdown_from_basis(price=100.0, adjusted_basis=100.0) == 0.0

    def test_above_basis_clamps_to_zero(self):
        from risk.holdings_exit import drawdown_from_basis
        assert drawdown_from_basis(price=120.0, adjusted_basis=100.0) == 0.0

    def test_invalid_inputs(self):
        from risk.holdings_exit import drawdown_from_basis
        assert drawdown_from_basis(price=0.0, adjusted_basis=100.0) == 0.0
        assert drawdown_from_basis(price=50.0, adjusted_basis=0.0) == 0.0


class TestSmaSlope:

    def test_declining_sma(self):
        from risk.holdings_exit import sma_slope
        closes = list(range(400, 180, -1))  # 220 falling closes
        assert sma_slope(closes, period=200, lookback=20) < 0

    def test_rising_sma(self):
        from risk.holdings_exit import sma_slope
        closes = list(range(100, 320))  # 220 rising closes
        assert sma_slope(closes, period=200, lookback=20) > 0

    def test_insufficient_data(self):
        from risk.holdings_exit import sma_slope
        assert sma_slope([100.0] * 219, period=200, lookback=20) is None


# ============================================================
# Price Backstops
# ============================================================

class TestPriceBackstop:

    def test_circuit_breaker_unconditional(self):
        """-40% triggers regardless of trend data."""
        from risk.holdings_exit import check_price_backstop
        triggered, reason = check_price_backstop(price=60.0, adjusted_basis=100.0)
        assert triggered is True
        assert 'CIRCUIT BREAKER' in reason

    def test_conditional_backstop_in_downtrend(self):
        """-30% + below declining 200 SMA → exit signal."""
        from risk.holdings_exit import check_price_backstop
        triggered, reason = check_price_backstop(
            price=68.0, adjusted_basis=100.0, sma_200=80.0, sma_200_slope=-1.5)
        assert triggered is True
        assert 'BACKSTOP' in reason

    def test_conditional_not_triggered_when_sma_rising(self):
        """-32% but 200 SMA rising → stops hurt in non-trending regimes, no signal."""
        from risk.holdings_exit import check_price_backstop
        triggered, _ = check_price_backstop(
            price=68.0, adjusted_basis=100.0, sma_200=80.0, sma_200_slope=+0.5)
        assert triggered is False

    def test_conditional_not_triggered_above_sma(self):
        from risk.holdings_exit import check_price_backstop
        triggered, _ = check_price_backstop(
            price=68.0, adjusted_basis=100.0, sma_200=65.0, sma_200_slope=-1.0)
        assert triggered is False

    def test_conditional_needs_trend_data(self):
        """-35% with no SMA data → conditional layer cannot fire (only the -40% hard layer can)."""
        from risk.holdings_exit import check_price_backstop
        triggered, _ = check_price_backstop(price=65.0, adjusted_basis=100.0)
        assert triggered is False

    def test_moderate_drawdown_no_trigger(self):
        from risk.holdings_exit import check_price_backstop
        triggered, _ = check_price_backstop(
            price=80.0, adjusted_basis=100.0, sma_200=90.0, sma_200_slope=-1.0)
        assert triggered is False


# ============================================================
# Dead Zone & Capacity
# ============================================================

class TestDeadZone:

    def test_dead_zone(self):
        from risk.holdings_exit import is_dead_zone
        assert is_dead_zone(price=80.0, adjusted_basis=100.0) is True   # 20% > 15%

    def test_moderate_not_dead_zone(self):
        from risk.holdings_exit import is_dead_zone
        assert is_dead_zone(price=90.0, adjusted_basis=100.0) is False  # 10%

    def test_boundary_exactly_15pct(self):
        """Exactly at threshold is NOT dead zone (strict >)."""
        from risk.holdings_exit import is_dead_zone
        assert is_dead_zone(price=85.0, adjusted_basis=100.0) is False


class TestMonthsToRecover:

    def test_basic_math(self):
        """$20 gap at $1.60/mo premium → 12.5 months."""
        from risk.holdings_exit import months_to_recover
        assert months_to_recover(80.0, 100.0, 1.60) == pytest.approx(12.5)

    def test_not_underwater(self):
        from risk.holdings_exit import months_to_recover
        assert months_to_recover(105.0, 100.0, 1.0) == 0.0

    def test_no_premium_income(self):
        from risk.holdings_exit import months_to_recover
        assert months_to_recover(80.0, 100.0, None) is None
        assert months_to_recover(80.0, 100.0, 0.0) is None


class TestTimeStop:

    def test_stagnant_capital_flagged(self):
        """14 months, -2% return vs 12%/yr alternative (14% over the window) → flag."""
        from risk.holdings_exit import check_time_stop
        flagged, reason = check_time_stop(
            months_held=14, position_return_pct=-2.0, alt_yield_pct_annual=12.0)
        assert flagged is True
        assert 'TIME STOP' in reason

    def test_too_early(self):
        from risk.holdings_exit import check_time_stop
        flagged, _ = check_time_stop(
            months_held=6, position_return_pct=-10.0, alt_yield_pct_annual=12.0)
        assert flagged is False

    def test_outperforming_alternative(self):
        from risk.holdings_exit import check_time_stop
        flagged, _ = check_time_stop(
            months_held=14, position_return_pct=20.0, alt_yield_pct_annual=12.0)
        assert flagged is False


# ============================================================
# Composite Evaluation — Severity Ordering
# ============================================================

class TestEvaluateHoldingExit:

    def test_circuit_breaker_wins(self):
        from risk.holdings_exit import evaluate_holding_exit
        rep = evaluate_holding_exit('TEST', price=55.0, adjusted_basis=100.0,
                                    sma_200=70.0, sma_200_slope=-1.0)
        assert rep.decision == 'CIRCUIT_BREAKER'
        assert rep.drop_pct == pytest.approx(0.45)

    def test_backstop_exit(self):
        from risk.holdings_exit import evaluate_holding_exit
        rep = evaluate_holding_exit('TEST', price=68.0, adjusted_basis=100.0,
                                    sma_200=80.0, sma_200_slope=-1.0)
        assert rep.decision == 'BACKSTOP_EXIT'

    def test_dead_zone_with_recovery_math(self):
        from risk.holdings_exit import evaluate_holding_exit
        rep = evaluate_holding_exit('TEST', price=80.0, adjusted_basis=100.0,
                                    monthly_premium_per_share=1.0)
        assert rep.decision == 'DEAD_ZONE'
        assert any('20 months' in r for r in rep.reasons)
        assert any('REDEPLOY' in r for r in rep.reasons)   # 20 > 12 flag

    def test_healthy_holding_ok(self):
        from risk.holdings_exit import evaluate_holding_exit
        rep = evaluate_holding_exit('TEST', price=95.0, adjusted_basis=100.0)
        assert rep.decision == 'OK'

    def test_deep_drop_but_uptrend_is_dead_zone_not_backstop(self):
        """-32%, SMA rising → conditional backstop suppressed, falls through to dead zone."""
        from risk.holdings_exit import evaluate_holding_exit
        rep = evaluate_holding_exit('TEST', price=68.0, adjusted_basis=100.0,
                                    sma_200=80.0, sma_200_slope=+1.0)
        assert rep.decision == 'DEAD_ZONE'


# ============================================================
# Roll-Chain / Campaign Accounting (risk.monitor)
# ============================================================

class TestCampaignAccounting:

    def test_net_credit_sums_legs(self):
        """Sold 2.00, BTC -3.50, sold 2.80 → net +1.30."""
        from risk.monitor import campaign_net_credit
        assert campaign_net_credit([2.00, -3.50, 2.80]) == pytest.approx(1.30)

    def test_adjusted_basis_from_campaign(self):
        """Assigned at $100 with $3.30 campaign credit → $96.70 basis."""
        from risk.monitor import campaign_adjusted_basis
        assert campaign_adjusted_basis(100.0, [2.00, -1.50, 2.80]) == pytest.approx(96.70)

    def test_csp_assignment_with_accumulated_premium(self):
        """Campaign premium deepens the basis reduction beyond the assigning put."""
        from risk.monitor import handle_csp_assignment
        portfolio = {'cash': 50_000.0, 'holdings': {}}
        updated = handle_csp_assignment(portfolio, 'AMD', strike=480.0, contracts=1,
                                        premium_per_share=5.0,
                                        accumulated_premium_per_share=3.15)
        assert updated['cost_basis']['AMD'] == pytest.approx(471.85)
        assert updated['cash'] == pytest.approx(50_000.0 - 48_000.0)

    def test_csp_assignment_backward_compatible(self):
        """Without accumulated premium, behavior is unchanged."""
        from risk.monitor import handle_csp_assignment
        portfolio = {'cash': 50_000.0, 'holdings': {}}
        updated = handle_csp_assignment(portfolio, 'V', strike=280.0, contracts=1,
                                        premium_per_share=4.0)
        assert updated['cost_basis']['V'] == pytest.approx(276.0)


class TestRollDiscipline:

    def test_valid_roll(self):
        from risk.monitor import check_roll_discipline
        ok, violations = check_roll_discipline(
            roll_count=0, net_credit_per_share=0.55, extension_days=35)
        assert ok is True
        assert violations == []

    def test_debit_roll_blocked(self):
        from risk.monitor import check_roll_discipline
        ok, violations = check_roll_discipline(
            roll_count=0, net_credit_per_share=-0.20, extension_days=35)
        assert ok is False
        assert any('debit' in v for v in violations)

    def test_max_rolls_exhausted(self):
        from risk.monitor import check_roll_discipline
        ok, violations = check_roll_discipline(
            roll_count=2, net_credit_per_share=0.40, extension_days=35)
        assert ok is False
        assert any('rolled 2' in v for v in violations)

    def test_short_extension_blocked(self):
        """Weekly rolls churn commissions — minimum 30-day extension."""
        from risk.monitor import check_roll_discipline
        ok, violations = check_roll_discipline(
            roll_count=0, net_credit_per_share=0.40, extension_days=7)
        assert ok is False
        assert any('extension' in v for v in violations)

    def test_chain_broken_by_falling_strikes(self):
        """3 legs chasing the price down = death-spiral signature."""
        from risk.monitor import is_roll_chain_broken
        assert is_roll_chain_broken([100.0, 95.0, 90.0]) is True

    def test_chain_healthy_when_strikes_hold(self):
        from risk.monitor import is_roll_chain_broken
        assert is_roll_chain_broken([100.0, 95.0, 95.0]) is False
        assert is_roll_chain_broken([100.0, 95.0]) is False  # only 2 legs
