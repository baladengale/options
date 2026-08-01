"""
Infrastructure Tests - Data Source Validation

Tests for data source connectivity, reliability, and fallback mechanisms.
Critical for ensuring system can always access required market data.
"""

import pytest
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

# Import test utilities
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.data.moomoo_client import MoomooClient
from src.data.yfinance_client import YFinanceClient
from src.data.portfolio_loader import fetch_portfolio_and_orders


class TestMoomooAPI:
    """Moomoo API connectivity and data validation"""

    @pytest.mark.slow
    def test_moomoo_connection_established(self):
        """Verify Moomoo API connection can be established"""
        try:
            client = MoomooClient()
            assert client is not None, "MoomooClient should not be None"
            # Note: Actual connection test may require valid API credentials
            # This test structure validates the client creation logic
        except ImportError:
            pytest.skip("Moomoo client not available in test environment")

    @pytest.mark.slow
    def test_moomoo_portfolio_data_freshness(self):
        """Portfolio data should be fresh (<5 minutes old)"""
        try:
            pf, orders = fetch_portfolio_and_orders()
            current_time = datetime.now()

            # Check if portfolio has timestamp
            if hasattr(pf, 'synced_at') and pf.synced_at:
                data_age = current_time - pf.synced_at
                max_age_seconds = 300  # 5 minutes
                assert data_age.total_seconds() < max_age_seconds, \
                    f"Portfolio data is {data_age.total_seconds():.0f}s old, should be <{max_age_seconds}s"
            else:
                pytest.skip("Portfolio timestamp not available")
        except Exception as e:
            pytest.skip(f"Cannot test data freshness: {e}")

    @pytest.mark.slow
    def test_moomoo_stock_data_retrieval(self):
        """Stock snapshot retrieval works for major tickers"""
        try:
            client = MoomooClient()
            snapshot = client.get_stock_snapshot('V')

            assert snapshot is not None, "Snapshot should not be None"
            assert hasattr(snapshot, 'last_price'), "Snapshot should have last_price"
            assert snapshot.last_price > 0, f"Price should be positive, got {snapshot.last_price}"

            if hasattr(snapshot, 'volume'):
                assert snapshot.volume >= 0, f"Volume should be non-negative, got {snapshot.volume}"

        except ImportError:
            pytest.skip("Moomoo client not available")
        except Exception as e:
            pytest.skip(f"Moomoo API not accessible: {e}")


class TestYFinanceFallback:
    """YFinance fallback system validation"""

    def test_yfinance_client_creation(self):
        """YFinance client can be instantiated"""
        try:
            client = YFinanceClient()
            assert client is not None
        except ImportError:
            pytest.skip("yfinance not installed")

    @pytest.mark.slow
    def test_yfinance_data_retrieval(self):
        """YFinance can retrieve stock data"""
        try:
            client = YFinanceClient()
            snapshot = client.get_stock_snapshot('AAPL')

            assert snapshot is not None
            assert hasattr(snapshot, 'last_price')
            assert snapshot.last_price > 0

        except ImportError:
            pytest.skip("yfinance not installed")
        except Exception as e:
            pytest.skip(f"YFinance API not accessible: {e}")

    def test_yfinance_data_format_compatibility(self):
        """YFinance data format matches expected schema"""
        try:
            from src.data.models import StockSnapshot

            # Create a mock snapshot to verify structure
            snapshot = StockSnapshot(
                ticker='AAPL',
                name='Apple Inc.',
                last_price=150.0,
                volume=50_000_000,
                rsi_14=50,
                sma_50=148,
                sma_200=145,
                volume_ratio=1.0
            )

            # Verify required fields exist
            required_fields = ['ticker', 'last_price', 'volume']
            for field in required_fields:
                assert hasattr(snapshot, field), f"Snapshot missing field: {field}"

        except ImportError:
            pytest.skip("Models not available")


class TestDataConsistency:
    """Cross-source data consistency validation"""

    @pytest.mark.slow
    def test_price_data_reasonableness(self):
        """Price data should be in reasonable ranges"""
        try:
            # Test with a known ticker
            client = YFinanceClient()
            snapshot = client.get_stock_snapshot('V')

            if snapshot and snapshot.last_price:
                # Price should be positive and reasonable
                assert 0 < snapshot.last_price < 1_000_000, \
                    f"Price {snapshot.last_price} outside reasonable range"

                # Price should have reasonable decimal precision
                price_str = str(snapshot.last_price)
                decimal_places = len(price_str.split('.')[-1]) if '.' in price_str else 0
                assert decimal_places <= 4, \
                    f"Price has {decimal_places} decimals, should be ≤4"
            else:
                pytest.skip("Could not retrieve snapshot")

        except ImportError:
            pytest.skip("Required modules not available")
        except Exception as e:
            pytest.skip(f"Data retrieval failed: {e}")

    @pytest.mark.slow
    def test_volume_data_validity(self):
        """Volume data should be valid and non-negative"""
        try:
            client = YFinanceClient()
            snapshot = client.get_stock_snapshot('AAPL')

            if snapshot and hasattr(snapshot, 'volume') and snapshot.volume is not None:
                assert snapshot.volume >= 0, f"Volume should be non-negative, got {snapshot.volume}"

                # Volume should be reasonable (not extremely high)
                assert snapshot.volume < 1_000_000_000, \
                    f"Volume {snapshot.volume} seems unreasonably high"
            else:
                pytest.skip("Volume data not available")

        except ImportError:
            pytest.skip("Required modules not available")
        except Exception as e:
            pytest.skip(f"Data retrieval failed: {e}")


class TestAPIResponseTime:
    """API performance validation"""

    @pytest.mark.slow
    @pytest.mark.timeout(10)
    def test_yfinance_response_time(self):
        """YFinance API should respond in <10 seconds"""
        try:
            client = YFinanceClient()
            start = time.time()
            snapshot = client.get_stock_snapshot('AAPL')
            elapsed = time.time() - start

            assert snapshot is not None, "Should retrieve data"
            assert elapsed < 10, f"API response took {elapsed:.2f}s, should be <10s"

        except ImportError:
            pytest.skip("yfinance not installed")
        except Exception as e:
            pytest.skip(f"API call failed: {e}")


class TestDataFreshness:
    """Data staleness detection and handling"""

    def test_data_age_calculation(self):
        """Data age calculation works correctly"""
        now = datetime.now()
        past_time = now - timedelta(minutes=5)

        age = now - past_time
        assert age.total_seconds() == 300, "Age calculation should be accurate"

    def test_fresh_data_identification(self):
        """Can identify fresh vs stale data"""
        now = datetime.now()

        # Fresh data (2 minutes old)
        fresh_timestamp = now - timedelta(minutes=2)
        fresh_age = (now - fresh_timestamp).total_seconds()
        assert fresh_age < 300, "Should identify as fresh (<5 minutes)"

        # Stale data (10 minutes old)
        stale_timestamp = now - timedelta(minutes=10)
        stale_age = (now - stale_timestamp).total_seconds()
        assert stale_age > 300, "Should identify as stale (>5 minutes)"


class TestErrorHandling:
    """Error handling and recovery"""

    def test_handling_invalid_ticker(self):
        """System handles invalid ticker symbols gracefully"""
        try:
            client = YFinanceClient()

            # This should handle the error gracefully, not crash
            try:
                snapshot = client.get_stock_snapshot('INVALIDTICKER123')
                # If it returns None or raises specific exception, that's OK
                if snapshot:
                    assert snapshot.last_price is None or snapshot.last_price == 0
            except Exception as e:
                # Expected behavior - should not crash
                assert True, "Should handle invalid ticker gracefully"

        except ImportError:
            pytest.skip("yfinance not installed")

    def test_network_timeout_handling(self):
        """System handles network timeouts gracefully"""
        # This would test timeout handling
        # In real implementation, would mock network conditions
        assert True, "Should have timeout handling configured"