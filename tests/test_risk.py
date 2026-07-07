"""
Risk management and collar check tests.

Validates:
- CC coverage verification
- CSP cash coverage verification
- Collar check for all open positions
- Assignment handling logic
- Portfolio risk metrics
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================
# CC Coverage Tests
# ============================================================

class TestCoveredCallCoverage:
    """Validate that CC positions are fully share-covered."""

    def test_sufficient_coverage(self):
        """430 V shares cover 4 contracts (need 400)."""
        from risk.collar_check import check_cc_coverage
        result = check_cc_coverage(shares_owned=430, contracts=4)
        assert result.ok is True

    def test_exact_coverage(self):
        """400 shares for 4 contracts — exactly covered."""
        from risk.collar_check import check_cc_coverage
        result = check_cc_coverage(shares_owned=400, contracts=4)
        assert result.ok is True

    def test_insufficient_coverage(self):
        """300 shares for 4 contracts — 100 shares short."""
        from risk.collar_check import check_cc_coverage
        result = check_cc_coverage(shares_owned=300, contracts=4)
        assert result.ok is False
        assert '100' in result.reason or 'shares' in result.reason.lower()

    def test_zero_contracts_always_ok(self):
        from risk.collar_check import check_cc_coverage
        result = check_cc_coverage(shares_owned=100, contracts=0)
        assert result.ok is True

    def test_zero_shares_with_contracts(self):
        """No shares but have contracts → definitely fail."""
        from risk.collar_check import check_cc_coverage
        result = check_cc_coverage(shares_owned=0, contracts=1)
        assert result.ok is False

    def test_multiple_cc_positions_same_ticker(self):
        """Two CC positions on same ticker: combined contracts ≤ shares."""
        from risk.collar_check import check_cc_coverage_multi
        positions = [
            {'ticker': 'V', 'contracts': 2, 'strike': 280},
            {'ticker': 'V', 'contracts': 2, 'strike': 290},
        ]
        result = check_cc_coverage_multi(positions, shares_by_ticker={'V': 430})
        assert result.ok is True

    def test_multiple_cc_positions_insufficient(self):
        """Combined 5 contracts vs 430 shares → fail."""
        from risk.collar_check import check_cc_coverage_multi
        positions = [
            {'ticker': 'V', 'contracts': 3, 'strike': 280},
            {'ticker': 'V', 'contracts': 2, 'strike': 290},
        ]
        result = check_cc_coverage_multi(positions, shares_by_ticker={'V': 430})
        assert result.ok is False


# ============================================================
# CSP Coverage Tests
# ============================================================

class TestCashSecuredPutCoverage:
    """Validate that CSP positions are fully cash-secured."""

    def test_sufficient_cash(self):
        """$45k cash, 1 contract $420 strike → need $42k → OK."""
        from risk.collar_check import check_csp_coverage
        result = check_csp_coverage(
            available_cash=45000, strike=420, contracts=1, tied_up_csp=0
        )
        assert result.ok is True

    def test_insufficient_cash(self):
        """$45k cash, 2 contracts $420 strike → need $84k → FAIL."""
        from risk.collar_check import check_csp_coverage
        result = check_csp_coverage(
            available_cash=45000, strike=420, contracts=2, tied_up_csp=0
        )
        assert result.ok is False

    def test_tied_up_cash_reduces_available(self):
        """$45k total, $42k tied → $3k free. Need $42k → FAIL."""
        from risk.collar_check import check_csp_coverage
        result = check_csp_coverage(
            available_cash=45000, strike=420, contracts=1, tied_up_csp=42000
        )
        assert result.ok is False

    def test_exact_cash_match(self):
        """Exactly enough cash."""
        from risk.collar_check import check_csp_coverage
        result = check_csp_coverage(
            available_cash=42000, strike=420, contracts=1, tied_up_csp=0
        )
        assert result.ok is True

    def test_multiple_csp_positions(self):
        """Two CSP positions: combined cash requirement ≤ available."""
        from risk.collar_check import check_csp_coverage_multi
        positions = [
            {'ticker': 'MSFT', 'strike': 420, 'contracts': 1},   # $42k
            {'ticker': 'AAPL', 'strike': 180, 'contracts': 1},   # $18k
        ]
        # Total needed: $60k, available: $45k → FAIL
        result = check_csp_coverage_multi(positions, available_cash=45000)
        assert result.ok is False
        # But with $60k available:
        result2 = check_csp_coverage_multi(positions, available_cash=60000)
        assert result2.ok is True


# ============================================================
# Full Collar Check Tests
# ============================================================

class TestCollarCheck:
    """Validate the complete collar check across all positions."""

    def test_empty_positions_all_clear(self):
        """No open positions → always all clear."""
        from risk.collar_check import collar_check
        result = collar_check(open_positions=[], portfolio_cash=45000, holdings={})
        assert result.ok is True

    def test_single_cc_position_covered(self):
        """1 CC position, enough shares → all clear."""
        from risk.collar_check import collar_check
        positions = [
            {'ticker': 'V', 'strategy': 'COVERED_CALL', 'strike': 280,
             'contracts': 4, 'expiry': '2026-08-15'}
        ]
        holdings = {'V': 430}
        result = collar_check(positions, portfolio_cash=45000, holdings=holdings)
        assert result.ok is True

    def test_single_csp_position_secured(self):
        """1 CSP position, enough cash → all clear."""
        from risk.collar_check import collar_check
        positions = [
            {'ticker': 'MSFT', 'strategy': 'CASH_SECURED_PUT', 'strike': 420,
             'contracts': 1, 'expiry': '2026-08-15'}
        ]
        holdings = {'V': 430}
        result = collar_check(positions, portfolio_cash=45000, holdings=holdings)
        assert result.ok is True

    def test_mixed_positions_all_covered(self):
        """CC + CSP positions, all properly covered → all clear."""
        from risk.collar_check import collar_check
        positions = [
            {'ticker': 'V', 'strategy': 'COVERED_CALL', 'strike': 280,
             'contracts': 4, 'expiry': '2026-08-15'},
            {'ticker': 'MSFT', 'strategy': 'CASH_SECURED_PUT', 'strike': 400,
             'contracts': 1, 'expiry': '2026-08-15'},
        ]
        holdings = {'V': 430}
        # CC needs $112k coverage via shares (430 × 280 = $120,400 — actually it's share count not dollar)
        # CC needs 400 shares, we have 430 → OK
        # CSP needs $40k cash, we have $45k → OK
        result = collar_check(positions, portfolio_cash=45000, holdings=holdings)
        assert result.ok is True

    def test_cc_under_covered_fails_collar(self):
        """One under-covered CC → collar fails."""
        from risk.collar_check import collar_check
        positions = [
            {'ticker': 'V', 'strategy': 'COVERED_CALL', 'strike': 280,
             'contracts': 5, 'expiry': '2026-08-15'},  # need 500, have 430
        ]
        holdings = {'V': 430}
        result = collar_check(positions, portfolio_cash=45000, holdings=holdings)
        assert result.ok is False
        assert 'V' in result.reason

    def test_csp_under_secured_fails_collar(self):
        """One under-secured CSP → collar fails."""
        from risk.collar_check import collar_check
        positions = [
            {'ticker': 'MSFT', 'strategy': 'CASH_SECURED_PUT', 'strike': 420,
             'contracts': 2, 'expiry': '2026-08-15'},  # need $84k, have $45k
        ]
        holdings = {'V': 430}
        result = collar_check(positions, portfolio_cash=45000, holdings=holdings)
        assert result.ok is False
        assert '84,000' in result.reason or '84000' in result.reason or 'CSP' in result.reason

    def test_collar_check_unknown_strategy(self):
        """Unknown strategy type should be flagged."""
        from risk.collar_check import collar_check
        positions = [
            {'ticker': 'TEST', 'strategy': 'IRON_CONDOR', 'strike': 100,
             'contracts': 1, 'expiry': '2026-08-15'},
        ]
        holdings = {}
        result = collar_check(positions, portfolio_cash=45000, holdings=holdings)
        assert result.ok is False  # Unknown strategy → fail closed


# ============================================================
# Assignment Handling Tests
# ============================================================

class TestAssignmentHandling:
    """Validate portfolio state updates on assignment."""

    def test_csp_assignment_adds_shares(self):
        """
        CSP assigned: cash decreases, shares added.
        MSFT $420 strike, premium $6.50/share
        Shares cost basis = strike - premium = $413.50
        """
        from risk.monitor import handle_csp_assignment

        portfolio = {'cash': 45000.0, 'holdings': {'V': 430}}
        updated = handle_csp_assignment(
            portfolio=portfolio,
            ticker='MSFT',
            strike=420.0,
            contracts=2,
            premium_per_share=6.50,
        )

        # Cash: $45k - ($420 × 200) = 45,000 - 84,000 = -39,000? No...
        # Actually: cash required = 420 × 100 × 2 = $84,000
        # But we only had $45k — in reality this would have failed collar check
        # For the test, assume enough cash
        portfolio_with_cash = {'cash': 100000.0, 'holdings': {'V': 430}}
        updated = handle_csp_assignment(
            portfolio=portfolio_with_cash,
            ticker='MSFT',
            strike=420.0,
            contracts=2,
            premium_per_share=6.50,
        )

        assert updated['cash'] == 100000.0 - 84000.0  # $16,000
        assert 'MSFT' in updated['holdings']
        assert updated['holdings']['MSFT'] == 200  # 2 contracts × 100 shares
        assert updated['cost_basis']['MSFT'] == 413.50  # strike - premium

    def test_cc_assignment_removes_shares(self):
        """
        CC assigned: shares removed, cash increased by strike × 100 × contracts.
        """
        from risk.monitor import handle_cc_assignment

        portfolio = {'cash': 45000.0, 'holdings': {'V': 430}}
        updated = handle_cc_assignment(
            portfolio=portfolio,
            ticker='V',
            strike=280.0,
            contracts=4,  # 400 shares called away
        )

        assert updated['cash'] == 45000.0 + (280.0 * 400)  # $45k + $112k = $157k
        assert updated['holdings'].get('V', 0) == 30  # 430 - 400 = 30 remaining


# ============================================================
# Portfolio Risk Metrics Tests
# ============================================================

class TestPortfolioRisk:
    """Validate portfolio-level risk calculations."""

    def test_concentration_high(self):
        """Single position > 20% of portfolio → HIGH concentration risk."""
        from risk.monitor import compute_concentration_risk
        risk = compute_concentration_risk(
            position_values={'V': 100000.0},
            total_value=148350.0,
        )
        # V is 67.4% → HIGH
        assert risk == 'HIGH'

    def test_concentration_moderate(self):
        """Largest position 10-20% → MODERATE."""
        from risk.monitor import compute_concentration_risk
        risk = compute_concentration_risk(
            position_values={'V': 20000.0, 'MSFT': 15000.0},
            total_value=148350.0,
        )
        # V is 13.5% → MODERATE
        assert risk == 'MODERATE'

    def test_concentration_low(self):
        """All positions < 10% → LOW."""
        from risk.monitor import compute_concentration_risk
        risk = compute_concentration_risk(
            position_values={'V': 10000.0, 'MSFT': 10000.0, 'AAPL': 10000.0},
            total_value=148350.0,
        )
        # Largest is 6.7% → LOW
        assert risk == 'LOW'

    def test_csp_cash_tied_up_pct(self):
        """Percentage of cash tied up in CSP assignments."""
        from risk.monitor import compute_csp_tie_up_pct
        pct = compute_csp_tie_up_pct(tied_up_csp=30000.0, total_cash=45000.0)
        assert pct == pytest.approx(66.67, abs=0.01)

    def test_margin_always_zero(self):
        """Margin usage must always be 0%."""
        from risk.monitor import compute_margin_usage
        usage = compute_margin_usage(margin_used=0.0, portfolio_value=148350.0)
        assert usage == 0.0
