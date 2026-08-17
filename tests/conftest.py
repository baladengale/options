"""
Shared test fixtures for the options trading system.

Provides:
- In-memory SQLite database with full schema
- Mock portfolio with 430 V shares + $45k cash
- Mock price history (252 days)
- Mock options chains
- Mock fundamentals
"""

import pytest
import sqlite3
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
import json
import sys
import os

# Ensure src/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================
# Config singleton hygiene (autouse)
# ============================================================

@pytest.fixture(autouse=True)
def _scrub_config_singleton():
    """Remove instance-attribute shadows leaked onto the Config singleton.

    ``monkeypatch.setattr(cfg_instance, 'attr', ...)`` on the cached Config
    singleton restores the OLD value as an INSTANCE attribute on teardown
    (a bound method), permanently shadowing the class attribute. Any later
    test that patches the class is then silently ignored — e.g.
    test_eligibility.test_all_blocks_disabled failed whenever
    test_thesis_validation.test_fundamental_health_thresholds_from_config
    ran first. Scrub instance attrs that shadow class attrs after every test.
    """
    yield
    try:
        from src.config import get_config
        cfg = get_config()
        cls = type(cfg)
        leaked = [k for k in vars(cfg) if hasattr(cls, k)]
        for k in leaked:
            try:
                delattr(cfg, k)
            except AttributeError:
                pass
    except Exception:
        pass  # config not loaded (pure-fixture tests) — nothing to scrub


# ============================================================
# Database Fixtures
# ============================================================

@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite database with full schema."""
    db_file = tmp_path / "test_options.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    _create_schema(conn)
    yield str(db_file)
    conn.close()


@pytest.fixture
def db_conn(db_path):
    """Return a connection to the test database."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _create_schema(conn):
    """Create all tables from schema definition."""
    conn.executescript("""
    CREATE TABLE portfolio_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        total_cash REAL NOT NULL,
        total_market_value REAL NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD',
        synced_at TEXT NOT NULL,
        is_current INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE holdings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id INTEGER NOT NULL REFERENCES portfolio_snapshots(id),
        ticker TEXT NOT NULL,
        shares REAL NOT NULL,
        avg_cost_basis REAL NOT NULL,
        market_price REAL NOT NULL,
        market_value REAL NOT NULL,
        unrealized_pnl REAL NOT NULL,
        unrealized_pnl_pct REAL NOT NULL,
        date_acquired TEXT,
        synced_at TEXT NOT NULL
    );

    CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL UNIQUE,
        ticker TEXT NOT NULL,
        order_type TEXT NOT NULL,
        asset_type TEXT NOT NULL,
        strategy TEXT,
        quantity REAL NOT NULL,
        price REAL,
        status TEXT NOT NULL,
        filled_qty REAL DEFAULT 0,
        filled_avg_price REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        synced_at TEXT NOT NULL
    );

    CREATE TABLE open_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        strategy TEXT NOT NULL CHECK(strategy IN ('COVERED_CALL', 'CASH_SECURED_PUT')),
        strike REAL NOT NULL,
        expiry TEXT NOT NULL,
        contracts INTEGER NOT NULL,
        premium_received REAL NOT NULL,
        delta REAL,
        opened_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'EXPIRED', 'ASSIGNED', 'CLOSED')),
        synced_at TEXT NOT NULL
    );

    CREATE TABLE watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL UNIQUE,
        sector TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'UNRESEARCHED'
            CHECK(status IN ('UNRESEARCHED', 'RESEARCHING', 'APPROVED', 'REJECTED', 'ACTIVE')),
        last_score REAL,
        last_scored_at TEXT,
        notes TEXT
    );

    CREATE TABLE options_chain_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        expiry TEXT NOT NULL,
        strike REAL NOT NULL,
        option_type TEXT NOT NULL CHECK(option_type IN ('CALL', 'PUT')),
        bid REAL NOT NULL,
        ask REAL NOT NULL,
        last_price REAL,
        delta REAL,
        gamma REAL,
        theta REAL,
        vega REAL,
        implied_vol REAL,
        open_interest INTEGER,
        volume INTEGER,
        underlying_price REAL NOT NULL,
        synced_at TEXT NOT NULL,
        UNIQUE(ticker, expiry, strike, option_type)
    );

    CREATE TABLE price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        date TEXT NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume INTEGER NOT NULL,
        UNIQUE(ticker, date)
    );

    CREATE TABLE signals_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        strategy TEXT NOT NULL,
        signal TEXT NOT NULL,
        composite_score REAL NOT NULL,
        trend_score REAL NOT NULL,
        sentiment_score REAL NOT NULL,
        options_score REAL NOT NULL,
        fund_score REAL NOT NULL,
        corr_score REAL NOT NULL,
        recommended_strike REAL,
        recommended_expiry TEXT,
        annualized_roc_pct REAL,
        all_constraints_pass INTEGER NOT NULL,
        generated_at TEXT NOT NULL
    );

    CREATE TABLE daily_digest (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL UNIQUE,
        total_portfolio_value REAL NOT NULL,
        cash_available REAL NOT NULL,
        cash_tied_up_csp REAL NOT NULL,
        total_premium_collected REAL NOT NULL,
        open_cc_count INTEGER NOT NULL,
        open_csp_count INTEGER NOT NULL,
        portfolio_delta REAL,
        portfolio_theta REAL,
        top_signal_ticker TEXT,
        top_signal_score REAL,
        market_regime TEXT,
        vix_level REAL,
        action_items TEXT,
        ai_narrative TEXT,
        generated_at TEXT NOT NULL
    );

    CREATE INDEX idx_holdings_snapshot ON holdings(snapshot_id);
    CREATE INDEX idx_holdings_ticker ON holdings(ticker);
    CREATE INDEX idx_orders_status ON orders(status);
    CREATE INDEX idx_orders_ticker ON orders(ticker);
    CREATE INDEX idx_options_chain_ticker_expiry ON options_chain_cache(ticker, expiry);
    CREATE INDEX idx_price_history_ticker_date ON price_history(ticker, date);
    CREATE INDEX idx_signals_ticker ON signals_log(ticker);
    CREATE INDEX idx_open_positions_status ON open_positions(status);
    """)


# ============================================================
# Portfolio Fixtures
# ============================================================

@pytest.fixture
def portfolio_cash():
    """Available cash for CSP assignment."""
    return 45000.00


@pytest.fixture
def portfolio_visa_shares():
    """Visa shares held."""
    return 430


@pytest.fixture
def portfolio_visa_cost_basis():
    """Visa average cost basis per share."""
    return 270.00


@pytest.fixture
def visa_market_price():
    """Current V market price."""
    return 240.35


@pytest.fixture
def seeded_db(db_conn, portfolio_cash, portfolio_visa_shares,
              portfolio_visa_cost_basis, visa_market_price):
    """Database seeded with a portfolio snapshot and V holding."""
    now = datetime.now().isoformat()

    # Insert portfolio snapshot
    db_conn.execute(
        "INSERT INTO portfolio_snapshots (total_cash, total_market_value, synced_at) VALUES (?, ?, ?)",
        (portfolio_cash, portfolio_cash + portfolio_visa_shares * visa_market_price, now)
    )
    snapshot_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Insert V holding
    db_conn.execute(
        """INSERT INTO holdings
           (snapshot_id, ticker, shares, avg_cost_basis, market_price, market_value,
            unrealized_pnl, unrealized_pnl_pct, synced_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            snapshot_id, "V", portfolio_visa_shares, portfolio_visa_cost_basis,
            visa_market_price, portfolio_visa_shares * visa_market_price,
            (visa_market_price - portfolio_visa_cost_basis) * portfolio_visa_shares,
            (visa_market_price - portfolio_visa_cost_basis) / portfolio_visa_cost_basis * 100,
            now
        )
    )

    db_conn.commit()
    return db_conn


# ============================================================
# Price History Fixtures
# ============================================================

def _generate_mock_price_history(
    ticker: str,
    start_price: float,
    days: int = 252,
    trend: float = 0.0002,  # slight daily upward drift
    volatility: float = 0.015,
    seed: int = 42
) -> list[dict]:
    """Generate deterministic mock price history for testing."""
    import random
    rng = random.Random(seed)
    prices = []
    price = start_price
    end_date = date.today()

    for i in range(days, -1, -1):
        d = end_date - timedelta(days=i)
        # Skip weekends
        if d.weekday() >= 5:
            continue

        daily_return = rng.gauss(trend, volatility)
        price = price * (1 + daily_return)
        daily_volume = int(rng.gauss(5_000_000, 1_500_000))
        daily_volume = max(daily_volume, 500_000)

        high = price * (1 + abs(rng.gauss(0, 0.01)))
        low = price * (1 - abs(rng.gauss(0, 0.01)))
        open_price = low + rng.random() * (high - low)

        prices.append({
            'ticker': ticker,
            'date': d.isoformat(),
            'open': round(open_price, 2),
            'high': round(high, 2),
            'low': round(low, 2),
            'close': round(price, 2),
            'volume': daily_volume,
        })

    return prices


@pytest.fixture
def msft_price_history():
    """252 days of mock MSFT price data (uptrend)."""
    return _generate_mock_price_history("MSFT", start_price=400.0, trend=0.0008, seed=1)


@pytest.fixture
def nvda_price_history():
    """252 days of mock NVDA price data (volatile uptrend)."""
    return _generate_mock_price_history("NVDA", start_price=100.0, trend=0.0012, volatility=0.025, seed=2)


@pytest.fixture
def bearish_stock_history():
    """252 days of a bearish stock (downtrend)."""
    return _generate_mock_price_history("BEAR", start_price=100.0, trend=-0.0010, volatility=0.02, seed=3)


@pytest.fixture
def sideways_stock_history():
    """252 days of a sideways stock (no trend)."""
    return _generate_mock_price_history("SIDE", start_price=100.0, trend=0.0, volatility=0.01, seed=4)


@pytest.fixture
def visa_price_history():
    """252 days of mock V price data."""
    return _generate_mock_price_history("V", start_price=230.0, trend=0.0003, seed=5)


@pytest.fixture
def seeded_price_db(db_conn, msft_price_history, nvda_price_history,
                    bearish_stock_history, sideways_stock_history, visa_price_history):
    """Database seeded with price history for multiple tickers."""
    all_histories = (
        msft_price_history + nvda_price_history +
        bearish_stock_history + sideways_stock_history +
        visa_price_history
    )

    for row in all_histories:
        db_conn.execute(
            """INSERT OR REPLACE INTO price_history
               (ticker, date, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (row['ticker'], row['date'], row['open'], row['high'],
             row['low'], row['close'], row['volume'])
        )
    db_conn.commit()
    return db_conn


# ============================================================
# Options Chain Fixtures
# ============================================================

@pytest.fixture
def liquid_options_chain():
    """Mock liquid options chain for MSFT CSP at various strikes."""
    today = date.today()
    expiry = today + timedelta(days=40)
    return {
        'ticker': 'MSFT',
        'expiry': expiry.isoformat(),
        'underlying_price': 425.00,
        'options': [
            # PUT options for CSP screening
            {'strike': 400, 'type': 'PUT', 'bid': 5.80, 'ask': 6.10, 'delta': -0.15,
             'gamma': 0.002, 'theta': -0.35, 'vega': 0.45, 'iv': 0.28,
             'oi': 2500, 'volume': 800},
            {'strike': 410, 'type': 'PUT', 'bid': 7.20, 'ask': 7.55, 'delta': -0.20,
             'gamma': 0.003, 'theta': -0.42, 'vega': 0.50, 'iv': 0.29,
             'oi': 1800, 'volume': 600},
            {'strike': 420, 'type': 'PUT', 'bid': 8.80, 'ask': 9.20, 'delta': -0.25,
             'gamma': 0.004, 'theta': -0.50, 'vega': 0.55, 'iv': 0.30,
             'oi': 3200, 'volume': 1200},
            {'strike': 430, 'type': 'PUT', 'bid': 10.50, 'ask': 11.00, 'delta': -0.30,
             'gamma': 0.005, 'theta': -0.58, 'vega': 0.60, 'iv': 0.31,
             'oi': 1500, 'volume': 450},
            # CALL options for CC screening
            {'strike': 440, 'type': 'CALL', 'bid': 8.50, 'ask': 8.90, 'delta': 0.22,
             'gamma': 0.003, 'theta': -0.40, 'vega': 0.48, 'iv': 0.27,
             'oi': 2000, 'volume': 700},
            {'strike': 450, 'type': 'CALL', 'bid': 6.20, 'ask': 6.50, 'delta': 0.17,
             'gamma': 0.002, 'theta': -0.32, 'vega': 0.42, 'iv': 0.26,
             'oi': 2800, 'volume': 950},
        ]
    }


@pytest.fixture
def illiquid_options_chain():
    """Mock illiquid options chain (wide spreads, low OI)."""
    today = date.today()
    expiry = today + timedelta(days=40)
    return {
        'ticker': 'ILLIQ',
        'expiry': expiry.isoformat(),
        'underlying_price': 50.00,
        'options': [
            {'strike': 45, 'type': 'PUT', 'bid': 0.80, 'ask': 1.50, 'delta': -0.25,
             'gamma': 0.01, 'theta': -0.10, 'vega': 0.15, 'iv': 0.55,
             'oi': 30, 'volume': 5},
            {'strike': 50, 'type': 'PUT', 'bid': 1.50, 'ask': 2.80, 'delta': -0.35,
             'gamma': 0.02, 'theta': -0.15, 'vega': 0.20, 'iv': 0.60,
             'oi': 45, 'volume': 8},
        ]
    }


@pytest.fixture
def seeded_options_db(db_conn, liquid_options_chain):
    """Database seeded with a liquid options chain."""
    now = datetime.now().isoformat()
    for opt in liquid_options_chain['options']:
        db_conn.execute(
            """INSERT OR REPLACE INTO options_chain_cache
               (ticker, expiry, strike, option_type, bid, ask, last_price,
                delta, gamma, theta, vega, implied_vol, open_interest, volume,
                underlying_price, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                liquid_options_chain['ticker'],
                liquid_options_chain['expiry'],
                opt['strike'],
                opt['type'],
                opt['bid'],
                opt['ask'],
                (opt['bid'] + opt['ask']) / 2,
                opt['delta'],
                opt['gamma'],
                opt['theta'],
                opt['vega'],
                opt['iv'],
                opt['oi'],
                opt['volume'],
                liquid_options_chain['underlying_price'],
                now
            )
        )
    db_conn.commit()
    return db_conn


# ============================================================
# Fundamentals Fixtures
# ============================================================

@pytest.fixture
def strong_fundamentals():
    """Mock strong fundamental data (like MSFT)."""
    return {
        'ticker': 'MSFT',
        'revenue_growth_yoy_pct': 18.5,   # strong growth
        'eps_quarters_positive': 4,        # all 4 quarters profitable
        'fcf_yield_pct': 3.8,             # healthy FCF
        'debt_to_equity': 0.25,           # low leverage
        'peg_ratio': 1.2,                 # reasonable valuation
        'pe_ratio': 32.5,
        'market_cap': 3.2e12,
        'dividend_yield_pct': 0.8,
    }


@pytest.fixture
def weak_fundamentals():
    """Mock weak fundamental data."""
    return {
        'ticker': 'WEAK',
        'revenue_growth_yoy_pct': -5.0,   # declining
        'eps_quarters_positive': 1,        # only 1 of 4 profitable
        'fcf_yield_pct': -2.0,            # negative FCF
        'debt_to_equity': 3.5,            # highly leveraged
        'peg_ratio': -5.0,                # negative earnings
        'pe_ratio': None,
        'market_cap': 500e6,
        'dividend_yield_pct': 0.0,
    }


# ============================================================
# Seeded Watchlist Fixture
# ============================================================

@pytest.fixture
def seeded_watchlist(db_conn):
    """Seed watchlist with tech stocks."""
    tickers = [
        ('MSFT', 'Technology'),
        ('GOOGL', 'Technology'),
        ('AAPL', 'Technology'),
        ('AMZN', 'Technology'),
        ('NVDA', 'Technology'),
        ('META', 'Technology'),
        ('AVGO', 'Technology'),
        ('ADBE', 'Technology'),
        ('CRM', 'Technology'),
        ('AMD', 'Technology'),
    ]
    for ticker, sector in tickers:
        db_conn.execute(
            "INSERT OR IGNORE INTO watchlist (ticker, sector) VALUES (?, ?)",
            (ticker, sector)
        )
    db_conn.commit()


# ============================================================
# Full Seeded DB (portfolio + prices + options + watchlist)
# ============================================================

@pytest.fixture
def full_seeded_db(seeded_db, seeded_price_db, seeded_options_db, seeded_watchlist):
    """Database fully seeded with portfolio, prices, options, and watchlist."""
    return seeded_db
