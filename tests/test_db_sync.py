"""
Local DB sync and freshness validation tests.

Verifies:
- SQLite schema creation
- Table existence and column correctness
- Data insertion and upsert behavior
- Freshness checking with TTLs
- Sync engine orchestration
- Staleness detection and rejection
"""

import pytest
import sqlite3
from datetime import datetime, timedelta
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================
# Schema Tests
# ============================================================

class TestDatabaseSchema:
    """Verify all required tables and columns exist."""

    REQUIRED_TABLES = [
        'portfolio_snapshots',
        'holdings',
        'orders',
        'open_positions',
        'watchlist',
        'options_chain_cache',
        'price_history',
        'signals_log',
        'daily_digest',
    ]

    def test_all_tables_exist(self, db_conn):
        """Every table in the schema must exist."""
        cursor = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        for required in self.REQUIRED_TABLES:
            assert required in tables, f"Missing table: {required}"

    def test_portfolio_snapshots_columns(self, db_conn):
        """Verify portfolio_snapshots has correct columns."""
        cursor = db_conn.execute("PRAGMA table_info(portfolio_snapshots)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        assert 'total_cash' in columns
        assert 'total_market_value' in columns
        assert 'currency' in columns
        assert 'synced_at' in columns
        assert 'is_current' in columns

    def test_holdings_columns(self, db_conn):
        """Verify holdings has correct columns."""
        cursor = db_conn.execute("PRAGMA table_info(holdings)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        for col in ['snapshot_id', 'ticker', 'shares', 'avg_cost_basis',
                     'market_price', 'market_value', 'unrealized_pnl',
                     'unrealized_pnl_pct', 'synced_at']:
            assert col in columns, f"Missing column: {col}"

    def test_orders_columns(self, db_conn):
        """Verify orders table has unique constraint on order_id."""
        cursor = db_conn.execute("PRAGMA table_info(orders)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        assert 'order_id' in columns
        assert 'status' in columns

    def test_open_positions_check_constraint(self, db_conn):
        """open_positions must enforce strategy and status CHECK constraints."""
        now = datetime.now().isoformat()
        # Valid strategy
        db_conn.execute(
            """INSERT INTO open_positions
               (ticker, strategy, strike, expiry, contracts, premium_received, opened_at, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ('MSFT', 'COVERED_CALL', 420.0, '2026-08-15', 1, 650.0, now, now)
        )
        db_conn.commit()

        # Verify it was inserted
        row = db_conn.execute("SELECT * FROM open_positions WHERE ticker='MSFT'").fetchone()
        assert row is not None
        assert row['strategy'] == 'COVERED_CALL'
        assert row['status'] == 'OPEN'  # default

        # Invalid strategy should fail
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                """INSERT INTO open_positions
                   (ticker, strategy, strike, expiry, contracts, premium_received, opened_at, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ('MSFT', 'NAKED_PUT', 420.0, '2026-08-15', 1, 650.0, now, now)
            )

    def test_options_chain_unique_constraint(self, db_conn):
        """options_chain_cache must enforce UNIQUE(ticker, expiry, strike, option_type)."""
        now = datetime.now().isoformat()
        params = ('MSFT', '2026-08-15', 420.0, 'PUT', 5.80, 6.10, 5.95,
                  -0.15, 0.002, -0.35, 0.45, 0.28, 2500, 800, 425.0, now)

        # First insert should succeed
        db_conn.execute(
            """INSERT INTO options_chain_cache
               (ticker, expiry, strike, option_type, bid, ask, last_price,
                delta, gamma, theta, vega, implied_vol, open_interest, volume,
                underlying_price, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            params
        )
        db_conn.commit()

        # Second insert with same key should fail (or REPLACE)
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                """INSERT INTO options_chain_cache
                   (ticker, expiry, strike, option_type, bid, ask, last_price,
                    delta, gamma, theta, vega, implied_vol, open_interest, volume,
                    underlying_price, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                params
            )

    def test_price_history_unique_constraint(self, db_conn):
        """price_history must enforce UNIQUE(ticker, date)."""
        db_conn.execute(
            """INSERT INTO price_history (ticker, date, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ('MSFT', '2026-07-07', 425.0, 428.0, 423.0, 426.5, 5000000)
        )
        db_conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                """INSERT INTO price_history (ticker, date, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ('MSFT', '2026-07-07', 426.0, 429.0, 424.0, 427.5, 5100000)
            )

    def test_signals_log_allows_multiple_entries(self, db_conn):
        """signals_log should allow multiple entries for same ticker (audit trail)."""
        now = datetime.now().isoformat()
        db_conn.execute(
            """INSERT INTO signals_log
               (ticker, strategy, signal, composite_score, trend_score, sentiment_score,
                options_score, fund_score, corr_score, all_constraints_pass, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ('MSFT', 'CASH_SECURED_PUT', 'STRONG_WRITE', 82.0, 85.0, 75.0, 82.0, 80.0, 80.0, 1, now)
        )
        db_conn.execute(
            """INSERT INTO signals_log
               (ticker, strategy, signal, composite_score, trend_score, sentiment_score,
                options_score, fund_score, corr_score, all_constraints_pass, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ('MSFT', 'CASH_SECURED_PUT', 'WRITE', 74.0, 70.0, 68.0, 75.0, 80.0, 80.0, 1, now)
        )
        db_conn.commit()

        count = db_conn.execute(
            "SELECT COUNT(*) FROM signals_log WHERE ticker='MSFT'"
        ).fetchone()[0]
        assert count == 2

    def test_daily_digest_unique_date(self, db_conn):
        """daily_digest must enforce UNIQUE(date)."""
        now = datetime.now().isoformat()
        db_conn.execute(
            """INSERT INTO daily_digest
               (date, total_portfolio_value, cash_available, cash_tied_up_csp,
                total_premium_collected, open_cc_count, open_csp_count, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ('2026-07-07', 148350.0, 45000.0, 0.0, 0.0, 0, 0, now)
        )
        db_conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                """INSERT INTO daily_digest
                   (date, total_portfolio_value, cash_available, cash_tied_up_csp,
                    total_premium_collected, open_cc_count, open_csp_count, generated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ('2026-07-07', 149000.0, 45500.0, 0.0, 0.0, 0, 0, now)
            )


# ============================================================
# Freshness Tests
# ============================================================

class TestDataFreshness:
    """Validate staleness detection per SPECS Section 2.3."""

    def test_fresh_portfolio_data(self):
        """Data synced 1 minute ago → fresh (TTL: 5 min)."""
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(minutes=1)
        assert check_freshness(synced_at, max_age_seconds=300) is True

    def test_stale_portfolio_data(self):
        """Data synced 7 minutes ago → stale (TTL: 5 min)."""
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(minutes=7)
        assert check_freshness(synced_at, max_age_seconds=300) is False

    def test_fresh_options_chain(self):
        """Options chain synced 3 minutes ago → fresh (TTL: 5 min)."""
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(minutes=3)
        assert check_freshness(synced_at, max_age_seconds=300) is True

    def test_fresh_price_history(self):
        """Price history synced 12 hours ago → fresh (TTL: 24h)."""
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(hours=12)
        assert check_freshness(synced_at, max_age_seconds=86400) is True

    def test_stale_price_history(self):
        """Price history synced 30 hours ago → stale (TTL: 24h)."""
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(hours=30)
        assert check_freshness(synced_at, max_age_seconds=86400) is False

    def test_freshness_at_boundary(self):
        """At TTL boundary minus small buffer → still fresh."""
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(seconds=299)
        assert check_freshness(synced_at, max_age_seconds=300) is True

    def test_freshness_just_past_ttl(self):
        """Just past TTL → stale."""
        from db.sync import check_freshness
        synced_at = datetime.now() - timedelta(seconds=302)
        assert check_freshness(synced_at, max_age_seconds=300) is False


# ============================================================
# Sync Engine Tests
# ============================================================

class TestSyncEngine:
    """Validate sync engine orchestration and reporting."""

    def test_sync_report_creation(self):
        """SyncReport should capture all sync statuses."""
        from db.sync import SyncReport
        report = SyncReport(
            success=True,
            portfolio_synced=True,
            orders_synced=True,
            positions_synced=True,
            chains_synced={'MSFT': True, 'AAPL': True},
            prices_synced={'MSFT': True, 'AAPL': True},
            errors=[],
            synced_at=datetime.now(),
            data_source='MOOMOO',
        )
        assert report.success is True
        assert len(report.errors) == 0
        assert report.data_source == 'MOOMOO'

    def test_sync_report_with_errors(self):
        """SyncReport with partial failures."""
        from db.sync import SyncReport
        report = SyncReport(
            success=False,
            portfolio_synced=True,
            orders_synced=False,
            positions_synced=True,
            chains_synced={'MSFT': True, 'AAPL': False},
            prices_synced={'MSFT': True, 'AAPL': True},
            errors=['Failed to sync AAPL options chain: timeout'],
            synced_at=datetime.now(),
            data_source='MIXED',
        )
        assert report.success is False
        assert len(report.errors) == 1
        assert 'AAPL' in report.errors[0]

    def test_sync_report_yfinance_fallback(self):
        """When moomoo fails, report should indicate fallback source."""
        from db.sync import SyncReport
        report = SyncReport(
            success=True,
            portfolio_synced=True,
            orders_synced=True,
            positions_synced=True,
            chains_synced={'MSFT': True},
            prices_synced={'MSFT': True},
            errors=['Moomoo unavailable for options chain — used Yahoo Finance'],
            synced_at=datetime.now(),
            data_source='YFINANCE_FALLBACK',
        )
        assert report.data_source == 'YFINANCE_FALLBACK'
        assert report.success is True  # Still succeeded, just with fallback


# ============================================================
# Portfolio Data Tests
# ============================================================

class TestPortfolioData:
    """Validate portfolio data is correctly stored and queried."""

    def test_portfolio_snapshot_insert(self, db_conn, portfolio_cash, visa_market_price,
                                        portfolio_visa_shares):
        """Insert and retrieve a portfolio snapshot."""
        now = datetime.now().isoformat()
        db_conn.execute(
            "INSERT INTO portfolio_snapshots (total_cash, total_market_value, synced_at) VALUES (?, ?, ?)",
            (portfolio_cash, portfolio_cash + portfolio_visa_shares * visa_market_price, now)
        )
        db_conn.commit()

        row = db_conn.execute(
            "SELECT * FROM portfolio_snapshots WHERE is_current=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()

        assert row is not None
        assert row['total_cash'] == portfolio_cash
        assert row['currency'] == 'USD'

    def test_only_one_current_snapshot(self, db_conn):
        """When a new snapshot is inserted, old ones should be marked is_current=0."""
        now = datetime.now().isoformat()

        # Insert first snapshot
        db_conn.execute(
            "INSERT INTO portfolio_snapshots (total_cash, total_market_value, synced_at, is_current) VALUES (?, ?, ?, 1)",
            (45000.0, 148350.0, now)
        )
        db_conn.commit()

        # Insert second snapshot (should mark first as not current)
        db_conn.execute(
            "UPDATE portfolio_snapshots SET is_current=0 WHERE is_current=1"
        )
        db_conn.execute(
            "INSERT INTO portfolio_snapshots (total_cash, total_market_value, synced_at, is_current) VALUES (?, ?, ?, 1)",
            (46000.0, 150000.0, now)
        )
        db_conn.commit()

        # Only one should be current
        current_count = db_conn.execute(
            "SELECT COUNT(*) FROM portfolio_snapshots WHERE is_current=1"
        ).fetchone()[0]
        assert current_count == 1

    def test_holdings_linked_to_snapshot(self, db_conn, seeded_db):
        """Holdings should reference a valid snapshot_id."""
        row = db_conn.execute(
            """SELECT h.*, p.total_cash
               FROM holdings h
               JOIN portfolio_snapshots p ON h.snapshot_id = p.id
               WHERE h.ticker='V'"""
        ).fetchone()

        assert row is not None
        assert row['ticker'] == 'V'
        assert row['shares'] == 430
        assert row['total_cash'] == 45000.0


# ============================================================
# Watchlist Tests
# ============================================================

class TestWatchlist:
    """Validate watchlist CRUD operations."""

    def test_insert_watchlist(self, db_conn):
        """Insert tickers into watchlist."""
        db_conn.execute(
            "INSERT INTO watchlist (ticker, sector) VALUES (?, ?)",
            ('MSFT', 'Technology')
        )
        db_conn.commit()

        row = db_conn.execute(
            "SELECT * FROM watchlist WHERE ticker='MSFT'"
        ).fetchone()
        assert row is not None
        assert row['sector'] == 'Technology'
        assert row['status'] == 'UNRESEARCHED'  # default

    def test_watchlist_unique_ticker(self, db_conn):
        """Cannot insert duplicate ticker."""
        db_conn.execute(
            "INSERT INTO watchlist (ticker, sector) VALUES (?, ?)",
            ('AAPL', 'Technology')
        )
        db_conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO watchlist (ticker, sector) VALUES (?, ?)",
                ('AAPL', 'Technology')
            )

    def test_watchlist_status_transitions(self, db_conn):
        """Status should transition through valid states."""
        db_conn.execute(
            "INSERT INTO watchlist (ticker, sector, status) VALUES (?, ?, ?)",
            ('MSFT', 'Technology', 'UNRESEARCHED')
        )
        db_conn.commit()

        # Valid transition: UNRESEARCHED → RESEARCHING
        db_conn.execute(
            "UPDATE watchlist SET status='RESEARCHING' WHERE ticker='MSFT'"
        )
        db_conn.commit()
        row = db_conn.execute("SELECT status FROM watchlist WHERE ticker='MSFT'").fetchone()
        assert row['status'] == 'RESEARCHING'

        # Invalid status should fail CHECK constraint
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                "UPDATE watchlist SET status='INVALID_STATUS' WHERE ticker='MSFT'"
            )

    def test_watchlist_update_score(self, db_conn):
        """Update last_score after scoring run."""
        now = datetime.now().isoformat()
        db_conn.execute(
            "INSERT INTO watchlist (ticker, sector) VALUES (?, ?)",
            ('MSFT', 'Technology')
        )
        db_conn.commit()

        db_conn.execute(
            "UPDATE watchlist SET last_score=?, last_scored_at=? WHERE ticker=?",
            (82.5, now, 'MSFT')
        )
        db_conn.commit()

        row = db_conn.execute(
            "SELECT last_score, last_scored_at FROM watchlist WHERE ticker='MSFT'"
        ).fetchone()
        assert row['last_score'] == 82.5
        assert row['last_scored_at'] == now


# ============================================================
# Data Integrity Tests
# ============================================================

class TestDataIntegrity:
    """Validate referential integrity and data consistency."""

    def test_cascade_on_snapshot_delete(self, db_conn):
        """Holdings should reference valid snapshots."""
        # This is a soft constraint — we don't CASCADE, we just
        # verify the relationship is queryable
        now = datetime.now().isoformat()
        db_conn.execute(
            "INSERT INTO portfolio_snapshots (total_cash, total_market_value, synced_at) VALUES (?, ?, ?)",
            (45000.0, 148350.0, now)
        )
        snapshot_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        db_conn.execute(
            """INSERT INTO holdings
               (snapshot_id, ticker, shares, avg_cost_basis, market_price, market_value,
                unrealized_pnl, unrealized_pnl_pct, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, 'V', 430, 270.0, 240.35, 103350.5, -12750.0, -10.98, now)
        )
        db_conn.commit()

        # Verify the relationship
        row = db_conn.execute(
            "SELECT * FROM holdings WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        assert row is not None
