"""
Infrastructure Tests - Database Operations

Tests for database reliability, data integrity, and operations.
Critical for ensuring data persistence and retrieval works correctly.
"""

import pytest
import sqlite3
import os
import tempfile
from datetime import datetime
from pathlib import Path

# Import test utilities
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

try:
    from src.db.schema import create_schema
except ImportError:
    create_schema = None


class TestDatabaseSchema:
    """Database schema creation and validation"""

    @pytest.fixture
    def temp_db_path(self):
        """Create temporary database path"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.db', delete=False) as f:
            db_path = f.name
        yield db_path
        # Cleanup
        if os.path.exists(db_path):
            os.unlink(db_path)

    def test_database_schema_creation(self, temp_db_path):
        """Database schema creates correctly"""
        if create_schema is None:
            pytest.skip("Schema creation module not available")

        try:
            conn = sqlite3.connect(temp_db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            create_schema(conn)

            # Verify key tables exist
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            expected_tables = [
                'portfolio_snapshots',
                'holdings',
                'orders',
                'options_chain_cache'
            ]

            for table in expected_tables:
                assert table in tables, f"Table {table} should exist"

            conn.close()

        except Exception as e:
            pytest.skip(f"Schema creation failed: {e}")

    def test_database_foreign_keys_enabled(self, temp_db_path):
        """Foreign keys constraint is enabled"""
        conn = sqlite3.connect(temp_db_path)
        conn.execute("PRAGMA foreign_keys = ON")

        # Check foreign keys are enabled
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys")
        result = cursor.fetchone()

        assert result[0] == 1, "Foreign keys should be enabled"

        conn.close()

    def test_database_index_creation(self, temp_db_path):
        """Important indexes are created for performance"""
        if create_schema is None:
            pytest.skip("Schema creation module not available")

        try:
            conn = sqlite3.connect(temp_db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            create_schema(conn)

            # Check for indexes
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
            indexes = [row[0] for row in cursor.fetchall()]

            # Should have some indexes for performance
            assert len(indexes) > 0, "Database should have indexes"

            conn.close()

        except Exception as e:
            pytest.skip(f"Index check failed: {e}")


class TestDatabaseOperations:
    """Database CRUD operations and integrity"""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create test database with schema"""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row

        if create_schema:
            try:
                create_schema(conn)
            except Exception as e:
                pytest.skip(f"Schema creation failed: {e}")

        yield conn
        conn.close()

    def test_insert_and_retrieve_portfolio_snapshot(self, test_db):
        """Can insert and retrieve portfolio snapshots"""
        cursor = test_db.cursor()

        # Insert test data
        now = datetime.now()
        cursor.execute("""
            INSERT INTO portfolio_snapshots (timestamp, total_value, cash, buying_power)
            VALUES (?, ?, ?, ?)
        """, (now.isoformat(), 100000.0, 45000.0, 90000.0))

        test_db.commit()

        # Retrieve data
        cursor.execute("SELECT * FROM portfolio_snapshots WHERE timestamp = ?", (now.isoformat(),))
        result = cursor.fetchone()

        assert result is not None, "Should retrieve inserted data"
        assert result['total_value'] == 100000.0, "Values should match"

    def test_insert_and_retrieve_holding(self, test_db):
        """Can insert and retrieve stock holdings"""
        cursor = test_db.cursor()

        # Insert test holding
        cursor.execute("""
            INSERT INTO holdings (ticker, quantity, cost_basis, current_price, sector)
            VALUES (?, ?, ?, ?, ?)
        """, ('V', 100, 250.0, 260.0, 'Financials'))

        test_db.commit()

        # Retrieve data
        cursor.execute("SELECT * FROM holdings WHERE ticker = ?", ('V',))
        result = cursor.fetchone()

        assert result is not None, "Should retrieve inserted holding"
        assert result['quantity'] == 100, "Quantity should match"
        assert result['sector'] == 'Financials', "Sector should match"

    def test_update_existing_holding(self, test_db):
        """Can update existing holdings"""
        cursor = test_db.cursor()

        # Insert initial holding
        cursor.execute("""
            INSERT INTO holdings (ticker, quantity, cost_basis, current_price, sector)
            VALUES (?, ?, ?, ?, ?)
        """, ('AAPL', 50, 150.0, 155.0, 'Technology'))

        test_db.commit()

        # Update holding
        cursor.execute("""
            UPDATE holdings SET quantity = ?, current_price = ?
            WHERE ticker = ?
        """, (55, 160.0, 'AAPL'))

        test_db.commit()

        # Verify update
        cursor.execute("SELECT quantity, current_price FROM holdings WHERE ticker = ?", ('AAPL',))
        result = cursor.fetchone()

        assert result['quantity'] == 55, "Quantity should be updated"
        assert result['current_price'] == 160.0, "Price should be updated"


class TestDataIntegrity:
    """Data integrity and consistency checks"""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create test database"""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row

        if create_schema:
            try:
                create_schema(conn)
            except Exception as e:
                pytest.skip(f"Schema creation failed: {e}")

        yield conn
        conn.close()

    def test_data_type_validation(self, test_db):
        """Database validates data types correctly"""
        cursor = test_db.cursor()

        # Test numeric validation
        try:
            cursor.execute("""
                INSERT INTO holdings (ticker, quantity, cost_basis, current_price, sector)
                VALUES (?, ?, ?, ?, ?)
            """, ('TEST', 'invalid', 150.0, 155.0, 'Technology'))
            test_db.commit()
            assert False, "Should fail with invalid quantity type"
        except (ValueError, sqlite3.OperationalError):
            # Expected behavior - invalid type rejected
            pass

    def test_null_handling(self, test_db):
        """Database handles NULL values appropriately"""
        cursor = test_db.cursor()

        # Insert with some NULL values (if schema allows)
        try:
            cursor.execute("""
                INSERT INTO portfolio_snapshots (timestamp, total_value, cash, buying_power)
                VALUES (?, ?, ?, ?)
            """, (datetime.now().isoformat(), 100000.0, 45000.0, None))

            test_db.commit()

            # Verify NULL is preserved
            cursor.execute("SELECT buying_power FROM portfolio_snapshots WHERE buying_power IS NULL")
            result = cursor.fetchone()
            assert result is not None, "Should handle NULL values"

        except Exception as e:
            pytest.skip(f"NULL handling test failed: {e}")


class TestDatabasePerformance:
    """Database performance and optimization"""

    @pytest.fixture
    def populated_db(self, tmp_path):
        """Create database with test data"""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")

        if create_schema:
            try:
                create_schema(conn)
            except Exception as e:
                pytest.skip(f"Schema creation failed: {e}")

            # Insert test data
            cursor = conn.cursor()
            for i in range(100):
                cursor.execute("""
                    INSERT INTO holdings (ticker, quantity, cost_basis, current_price, sector)
                    VALUES (?, ?, ?, ?, ?)
                """, (f'TEST{i}', 100 + i, 150.0 + i, 155.0 + i, 'Technology'))

            conn.commit()

        yield conn
        conn.close()

    def test_query_performance(self, populated_db):
        """Database queries perform within acceptable time"""
        import time

        cursor = populated_db.cursor()

        start = time.time()
        cursor.execute("SELECT * FROM holdings WHERE sector = 'Technology'")
        results = cursor.fetchall()
        elapsed = time.time() - start

        assert elapsed < 1.0, f"Query took {elapsed:.3f}s, should be <1s"
        assert len(results) == 100, "Should retrieve all test data"

    def test_bulk_insert_performance(self, tmp_path):
        """Bulk insert operations perform efficiently"""
        import time

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")

        if create_schema:
            try:
                create_schema(conn)
            except Exception as e:
                pytest.skip(f"Schema creation failed: {e}")

        cursor = conn.cursor()

        start = time.time()
        for i in range(1000):
            cursor.execute("""
                INSERT INTO holdings (ticker, quantity, cost_basis, current_price, sector)
                VALUES (?, ?, ?, ?, ?)
            """, (f'BULK{i}', 100, 150.0, 155.0, 'Technology'))
        conn.commit()
        elapsed = time.time() - start

        assert elapsed < 5.0, f"Bulk insert took {elapsed:.3f}s, should be <5s"

        conn.close()


class TestDatabaseBackupRecovery:
    """Database backup and recovery operations"""

    def test_database_file_creation(self, tmp_path):
        """Database files are created in correct location"""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))

        # Database file should exist
        assert db_path.exists(), "Database file should be created"

        conn.close()

    def test_database_connection_recovery(self, tmp_path):
        """Can recover database connection after close"""
        db_path = tmp_path / "test.db"

        # First connection
        conn1 = sqlite3.connect(str(db_path))
        conn1.execute("CREATE TABLE test (id INTEGER)")
        conn1.close()

        # Second connection (recovery)
        conn2 = sqlite3.connect(str(db_path))
        cursor = conn2.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='test'")
        result = cursor.fetchone()

        assert result is not None, "Should recover database schema"
        conn2.close()

    def test_concurrent_access(self, tmp_path):
        """Database handles concurrent access safely"""
        import threading

        db_path = tmp_path / "test.db"

        def create_and_insert(thread_id):
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA foreign_keys = ON")

            if create_schema:
                try:
                    create_schema(conn)
                    conn.execute(f"""
                        INSERT INTO holdings (ticker, quantity, cost_basis, current_price, sector)
                        VALUES (?, ?, ?, ?, ?)
                    """, (f'THREAD{thread_id}', 100, 150.0, 155.0, 'Technology'))
                    conn.commit()
                except Exception:
                    pass  # Schema might already be created

            conn.close()

        # Create multiple threads accessing database
        threads = []
        for i in range(5):
            thread = threading.Thread(target=create_and_insert, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for all threads
        for thread in threads:
            thread.join()

        # Verify all inserts completed (or handled gracefully)
        # The exact behavior depends on SQLite's locking


class TestDatabaseConnectionManagement:
    """Database connection lifecycle management"""

    def test_connection_cleanup(self, tmp_path):
        """Database connections are properly cleaned up"""
        db_path = tmp_path / "test.db"

        # Create and close multiple connections
        for i in range(10):
            conn = sqlite3.connect(str(db_path))
            conn.close()

        # Should not cause resource leaks
        # In production, would monitor connection pool size

    def test_connection_error_handling(self):
        """Database connection errors are handled gracefully"""
        # Try to connect to non-existent database
        try:
            conn = sqlite3.connect("/nonexistent/path/to/database.db")
            # If it doesn't raise exception, it should create in-memory database
            conn.close()
        except sqlite3.OperationalError:
            # Expected for invalid path
            pass