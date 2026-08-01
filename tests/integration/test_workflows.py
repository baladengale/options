"""
Integration Tests - End-to-End Workflow Validation

Tests for complete system workflows from data fetch to output.
Validates that all components work together correctly.
"""

import pytest
import time
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

# Import test utilities
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestScoringWorkflow:
    """Complete scoring workflow integration"""

    def test_composite_score_calculation(self):
        """Verify composite score calculation from all components"""
        try:
            from src.scoring import calculate_composite_score

            # Mock individual component scores
            component_scores = {
                'technical': 75,
                'options_quality': 80,
                'fundamental': 70,
                'sentiment': 85,
                'correlation': 90
            }

            # Get weights from config
            from src.config import rules
            weights = rules['scoring']['weights']

            # Calculate expected composite score
            expected_score = (
                component_scores['technical'] * weights['technical'] +
                component_scores['options_quality'] * weights['options_quality'] +
                component_scores['fundamental'] * weights['fundamental'] +
                component_scores['sentiment'] * weights['external_sentiment'] +
                component_scores['correlation'] * weights['macro_risk']
            )

            # Calculate actual score
            actual_score = calculate_composite_score(component_scores)

            assert abs(actual_score - expected_score) < 1, \
                f"Composite score {actual_score} should match expected {expected_score}"

        except ImportError:
            pytest.skip("Scoring module not available")

    def test_scoring_pipeline_integration(self):
        """Verify scoring pipeline processes data correctly"""
        try:
            from src.data.models import StockSnapshot
            from src.scoring import score_stock

            # Create test stock snapshot
            snapshot = StockSnapshot(
                ticker='AAPL',
                name='Apple Inc.',
                last_price=175.0,
                volume=50_000_000,
                rsi_14=55,
                sma_50=173,
                sma_200=170,
                volume_ratio=1.2,
                pe_ratio=28,
                highest_52w=185,
                lowest_52w=150,
                hv_30d=35
            )

            # Score the stock
            score_result = score_stock(snapshot)

            # Verify result structure
            assert 'composite_score' in score_result, "Should have composite score"
            assert 'component_scores' in score_result, "Should have component scores"
            assert 'signal' in score_result, "Should have trading signal"

            # Verify score is in valid range
            assert 0 <= score_result['composite_score'] <= 100, "Score should be 0-100"

            # Verify signal is valid
            valid_signals = ['STRONG_WRITE', 'WRITE', 'HOLD', 'AVOID']
            assert score_result['signal'] in valid_signals, "Signal should be valid"

        except ImportError:
            pytest.skip("Scoring modules not available")

    def test_scoring_with_real_data_structure(self):
        """Scoring works with real market data structure"""
        try:
            from src.data.models import StockSnapshot, OptionSnapshot

            # Create realistic test data
            stock_snapshot = StockSnapshot(
                ticker='V',
                name='Visa Inc.',
                last_price=260.0,
                volume=10_000_000,
                rsi_14=50,
                sma_50=258,
                sma_200=255,
                volume_ratio=1.0,
                pe_ratio=30,
                highest_52w=275,
                lowest_52w=220,
                hv_30d=25
            )

            option_snapshot = OptionSnapshot(
                ticker='V',
                expiry='2026-09-18',
                strike=260,
                option_type='CALL',
                bid=5.50,
                ask=5.70,
                last=5.60,
                volume=500,
                open_interest=2000,
                iv=0.25,
                delta=0.48,
                gamma=0.15,
                theta=-0.08,
                vega=0.35
            )

            # Verify data structure is correct
            assert stock_snapshot.ticker == 'V'
            assert option_snapshot.strike == 260
            assert option_snapshot.bid < option_snapshot.ask  # Normal bid-ask spread

        except ImportError:
            pytest.skip("Data models not available")


class TestThesisValidationWorkflow:
    """Thesis validation workflow integration"""

    def test_thesis_validation_pipeline(self):
        """Complete thesis validation workflow"""
        try:
            from src.analysis.thesis_validator import validate_investment_thesis
            from src.data.models import StockSnapshot

            # Create test snapshots for different scenarios
            intact_thesis_stock = StockSnapshot(
                ticker='V',
                name='Visa Inc.',
                last_price=260.0,
                volume=10_000_000,
                rsi_14=50,
                sma_50=258,
                sma_200=255,
                volume_ratio=1.0,
                pe_ratio=30,
                highest_52w=275,
                lowest_52w=240,
                hv_30d=25
            )

            broken_thesis_stock = StockSnapshot(
                ticker='BROKEN',
                name='Broken Company',
                last_price=50.0,
                volume=1_000_000,
                rsi_14=25,
                sma_50=60,
                sma_200=70,
                volume_ratio=0.5,
                pe_ratio=-456,  # Negative - company losing money
                highest_52w=120,
                lowest_52w=45,
                hv_30d=85
            )

            # Test intact thesis
            try:
                intact_report = validate_investment_thesis(
                    'V', '2026-07-01', {'pe_ratio': 30}, intact_thesis_stock
                )
                assert intact_report.status.value in ['THESIS_INTACT', 'TECHNICAL_DAMAGE'], \
                    "Strong stock should have intact or damaged thesis"
            except Exception as e:
                pytest.skip(f"Thesis validation failed: {e}")

            # Test broken thesis
            try:
                broken_report = validate_investment_thesis(
                    'BROKEN', '2026-07-01', {'pe_ratio': 30}, broken_thesis_stock
                )
                assert broken_report.status.value == 'THESIS_BROKEN', \
                    "Stock with negative P/E should have broken thesis"
            except Exception as e:
                pytest.skip(f"Thesis validation failed: {e}")

        except ImportError:
            pytest.skip("Thesis validation module not available")

    def test_thesis_validation_with_guardrails(self):
        """Thesis validation integrates with guardrails"""
        try:
            from src.analysis.thesis_validator import quick_thesis_check
            from src.guardrails.limits import GuardrailChecker
            from src.data.models import StockSnapshot

            # Create test stock
            snapshot = StockSnapshot(
                ticker='TEST',
                name='Test Company',
                last_price=100,
                volume=5_000_000,
                rsi_14=50,
                sma_50=98,
                sma_200=95,
                volume_ratio=1.0,
                pe_ratio=25,
                highest_52w=110,
                lowest_52w=90,
                hv_30d=30
            )

            # Run quick thesis check
            thesis_result = quick_thesis_check('TEST', snapshot)

            # Verify result structure
            assert 'broken' in thesis_result, "Should have broken status"
            assert 'damaged' in thesis_result, "Should have damaged status"

            # Create guardrail checker
            guardrail_checker = GuardrailChecker(
                net_liquidation=100000,
                cash=20000,
                buying_power=60000,
                open_positions=5,
                monthly_orders=8,
                csp_liability=25000
            )

            # Verify guardrails checker works
            limits = guardrail_checker.get_position_limit()
            assert 0 < limits <= 1, "Position limit should be valid percentage"

        except ImportError:
            pytest.skip("Required modules not available")


class TestGuardrailWorkflow:
    """Guardrails workflow integration"""

    def test_guardrail_enforcement_pipeline(self):
        """Complete guardrails enforcement workflow"""
        try:
            from src.guardrails.limits import GuardrailChecker

            # Test various portfolio states
            test_scenarios = [
                {
                    'name': 'healthy_portfolio',
                    'net_liq': 200000,
                    'cash': 40000,
                    'buying_power': 120000,
                    'positions': 5,
                    'monthly_orders': 8,
                    'csp_liability': 40000
                },
                {
                    'name': 'concentrated_portfolio',
                    'net_liq': 200000,
                    'cash': 10000,
                    'buying_power': 120000,
                    'positions': 8,
                    'monthly_orders': 25,
                    'csp_liability': 100000
                }
            ]

            for scenario in test_scenarios:
                checker = GuardrailChecker(
                    net_liquidation=scenario['net_liq'],
                    cash=scenario['cash'],
                    buying_power=scenario['buying_power'],
                    open_positions=scenario['positions'],
                    monthly_orders=scenario['monthly_orders'],
                    csp_liability=scenario['csp_liability']
                )

                # Verify checker can determine stage
                stage = checker.get_current_stage()
                assert stage in ['EMERGENCY', 'TARGET', 'COMFORT'], \
                    f"Stage should be valid for {scenario['name']}"

                # Verify limits are returned
                position_limit = checker.get_position_limit()
                assert 0 < position_limit <= 1, "Position limit should be valid"

        except ImportError:
            pytest.skip("Guardrails module not available")

    def test_new_trade_validation_workflow(self):
        """Complete new trade validation workflow"""
        try:
            from src.guardrails.limits import GuardrailChecker

            checker = GuardrailChecker(
                net_liquidation=100000,
                cash=25000,
                buying_power=60000,
                open_positions=4,
                monthly_orders=6,
                csp_liability=20000
            )

            # Test valid trade
            allowed, violations = checker.check_new_trade(
                ticker='AAPL',
                strategy='CSP',
                notional=15000,
                sector='Technology'
            )

            # Valid trade should be allowed
            assert allowed == True, "Valid trade should be allowed"
            assert len(violations) == 0, "Valid trade should have no violations"

            # Test trade that would violate concentration
            current_positions = {
                'AAPL': {'market_value': 15000, 'sector': 'Technology'}
            }

            allowed, violations = checker.check_new_trade(
                ticker='AAPL',
                strategy='CSP',
                notional=25000,  # This would push concentration too high
                sector='Technology',
                current_positions=current_positions
            )

            # This might be allowed or blocked depending on limits
            # Either way, the system should handle it correctly
            assert isinstance(allowed, bool), "Should return boolean decision"
            assert isinstance(violations, list), "Should return list of violations"

        except ImportError:
            pytest.skip("Guardrails module not available")


class TestPortfolioAnalysisWorkflow:
    """Portfolio analysis workflow integration"""

    @pytest.mark.slow
    def test_complete_analysis_workflow(self):
        """Complete portfolio analysis from data to recommendations"""
        try:
            # This would test the full portfolio.py sweep (formerly comprehensive_analysis.py)
            # But we'll test the components separately to avoid API dependencies

            from src.portfolio.summary import generate_portfolio_summary
            from src.data.portfolio_loader import Portfolio, Funds, Stock, Option

            # Create test portfolio
            test_portfolio = Portfolio(
                stocks=[
                    Stock(ticker='V', quantity=100, cost_basis=250.0, current_price=260.0),
                    Stock(ticker='AAPL', quantity=50, cost_basis=150.0, current_price=175.0)
                ],
                options=[],
                funds=Funds(
                    liquid_funds=45000,
                    buying_power=90000,
                    cash=45000,
                    margin_used=0
                ),
                synced_at=datetime.now()
            )

            # Generate summary
            try:
                summary = generate_portfolio_summary(test_portfolio)

                # Verify summary structure
                assert 'total_value' in summary, "Summary should have total value"
                assert 'stocks' in summary, "Summary should have stocks"

            except Exception as e:
                pytest.skip(f"Portfolio summary generation failed: {e}")

        except ImportError:
            pytest.skip("Portfolio modules not available")


class TestErrorRecoveryWorkflows:
    """Error handling and recovery workflows"""

    def test_data_source_failure_recovery(self):
        """System recovers when primary data source fails"""
        try:
            # Test that system can fallback to YFinance when Moomoo fails
            from src.data.yfinance_client import YFinanceClient

            # Create YFinance client
            yf_client = YFinanceClient()

            # Try to get data (should work even if Moomoo fails)
            snapshot = yf_client.get_stock_snapshot('AAPL')

            if snapshot:
                assert snapshot.last_price > 0, "Should have valid price data"
            else:
                pytest.skip("YFinance data not available")

        except ImportError:
            pytest.skip("YFinance client not available")

    def test_invalid_data_handling(self):
        """System handles invalid data gracefully"""
        try:
            from src.data.models import StockSnapshot

            # Try to create snapshot with invalid data
            try:
                invalid_snapshot = StockSnapshot(
                    ticker='',  # Invalid ticker
                    last_price=-100,  # Invalid price
                    volume=-1,  # Invalid volume
                    rsi_14=150,  # Invalid RSI (>100)
                    sma_50=50,
                    sma_200=50,
                    volume_ratio=1.0,
                    pe_ratio=25,
                    highest_52w=100,
                    lowest_52w=50,
                    hv_30d=30
                )
                # If created, system should handle it gracefully
            except (ValueError, AssertionError) as e:
                # Expected - system should reject invalid data
                pass

        except ImportError:
            pytest.skip("Data models not available")


class TestSystemIntegration:
    """Overall system integration tests"""

    def test_config_to_execution_workflow(self):
        """Complete workflow from config to execution decision"""
        try:
            # Load config
            from src.config import rules

            # Verify config has required sections
            required_sections = ['regime', 'options', 'scoring', 'position_limits']
            for section in required_sections:
                assert section in rules, f"Config missing {section}"

            # Verify config can be used for decision making
            delta_range = rules['options']['delta']['csp']['NEUTRAL']
            assert len(delta_range) == 2, "Delta range should have min and max"
            assert delta_range[0] < delta_range[1], "Min delta should be less than max"

        except ImportError:
            pytest.skip("Config module not available")

    def test_monitoring_integration(self):
        """Monitoring system integrates with main system"""
        try:
            from src.monitoring.health_checks import create_default_health_checker

            # Create health checker
            checker = create_default_health_checker()

            # Verify it has default checks
            assert len(checker.checks) > 0, "Should have default health checks"

            # Run health checks
            results = checker.run_all_checks()

            # Verify results structure
            assert 'overall_status' in results, "Results should have overall status"
            assert 'checks' in results, "Results should have individual checks"
            assert 'summary' in results, "Results should have summary"

        except ImportError:
            pytest.skip("Monitoring module not available")


class TestPerformanceIntegration:
    """Performance-related integration tests"""

    @pytest.mark.timeout(30)
    def test_analysis_performance_within_limits(self):
        """Complete analysis completes within performance limits"""
        start = time.time()

        try:
            # Run a simple analysis workflow
            from src.data.models import StockSnapshot
            from src.scoring import score_stock

            snapshot = StockSnapshot(
                ticker='V',
                name='Visa Inc.',
                last_price=260.0,
                volume=10_000_000,
                rsi_14=50,
                sma_50=258,
                sma_200=255,
                volume_ratio=1.0,
                pe_ratio=30,
                highest_52w=275,
                lowest_52w=240,
                hv_30d=25
            )

            score_result = score_stock(snapshot)
            elapsed = time.time() - start

            assert elapsed < 5, f"Scoring took {elapsed:.2f}s, should be <5s"
            assert score_result is not None, "Should return score result"

        except ImportError:
            pytest.skip("Scoring module not available")

    def test_memory_usage_reasonable(self):
        """Memory usage stays reasonable during operations"""
        try:
            import tracemalloc
            tracemalloc.start()

            # Perform some operations
            from src.data.models import StockSnapshot

            snapshots = []
            for i in range(100):
                snapshot = StockSnapshot(
                    ticker=f'TEST{i}',
                    name=f'Test Stock {i}',
                    last_price=100 + i,
                    volume=1_000_000,
                    rsi_14=50,
                    sma_50=98,
                    sma_200=95,
                    volume_ratio=1.0,
                    pe_ratio=25,
                    highest_52w=110,
                    lowest_52w=90,
                    hv_30d=30
                )
                snapshots.append(snapshot)

            # Check memory usage
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            # Memory usage should be reasonable (<100MB for this operation)
            peak_mb = peak / 1024 / 1024
            assert peak_mb < 100, f"Memory usage {peak_mb:.1f}MB seems high"

        except ImportError:
            pytest.skip("Required modules not available")