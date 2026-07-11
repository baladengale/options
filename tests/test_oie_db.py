"""
OIE Paper Portfolio Database — unit tests.

Validates: schema creation, seed portfolio, position lifecycle,
P&L calculations, snapshots, audit trail, engine state, reset.
No moomoo connection needed — uses in-memory SQLite.
"""

import pytest
import os
import sys
import tempfile
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.data.oie_db import OIEDB


@pytest.fixture
def db():
    """Create a temporary OIE DB for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        path = f.name
    oie = OIEDB(path)
    yield oie
    oie.close()
    os.unlink(path)


# ═══════════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════════

def test_schema_created(db):
    """All 4 tables should exist on init."""
    tables = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r['name'] for r in tables}
    assert 'paper_state' in names
    assert 'paper_positions' in names
    assert 'paper_trades' in names
    assert 'paper_snapshots' in names


def test_is_seeded_false_initially(db):
    """is_seeded() returns False before seed_portfolio is called."""
    assert not db.is_seeded()


# ═══════════════════════════════════════════════════════════════
# SEED
# ═══════════════════════════════════════════════════════════════

def test_seed_portfolio_basic(db):
    """Seed with simple stock + cash."""
    stocks = {
        'V': {'qty': 100, 'cost': 270.0},
        'AAPL': {'qty': 50, 'cost': 190.0},
    }
    db.seed_portfolio(stocks, cash=45000.0, fund=0.0)

    assert db.is_seeded()
    assert db.get_state('cash') == '45000.0'

    active = db.get_active_positions()
    assert len(active) == 2
    assert all(p['pos_type'] == 'STOCK' for p in active)
    assert all(p['status'] == 'ACTIVE' for p in active)

    # Verify stock details
    v_pos = [p for p in active if p['ticker'] == 'V'][0]
    assert v_pos['qty'] == 100
    assert v_pos['cost_price'] == 270.0

    aapl_pos = [p for p in active if p['ticker'] == 'AAPL'][0]
    assert aapl_pos['qty'] == 50
    assert aapl_pos['cost_price'] == 190.0


def test_seed_portfolio_empty_stocks(db):
    """Seed with only cash, no stocks."""
    db.seed_portfolio({}, cash=50000.0, fund=0.0)
    assert db.is_seeded()
    assert len(db.get_active_positions()) == 0
    assert db.get_state('cash') == '50000.0'


def test_seed_portfolio_creates_trades(db):
    """Seed should log SEED events to audit trail."""
    stocks = {'V': {'qty': 10, 'cost': 250.0}}
    db.seed_portfolio(stocks, cash=10000.0, fund=0.0)

    events = db.get_recent_events(10)
    seed_events = [e for e in events if e['event'] == 'SEED']
    assert len(seed_events) >= 1  # at least one SEED event


def test_seed_state_persisted(db):
    """Seed state keys should be set."""
    stocks = {'V': {'qty': 5, 'cost': 200.0}}
    db.seed_portfolio(stocks, cash=5000.0, fund=1000.0)

    assert db.get_state('seeded_at') != ''
    assert db.get_state('seeded_cash') == '5000.0'
    assert db.get_state('seeded_fund') == '1000.0'


# ═══════════════════════════════════════════════════════════════
# POSITIONS — open, close, expire, assign
# ═══════════════════════════════════════════════════════════════

def test_open_option_position(db):
    """Open a CSP position and verify in DB."""
    db.set_state('cash', '50000')
    pos_id = db.open_position(
        ticker='AAPL', pos_type='PUT', qty=-1,
        cost_price=200.0, strike=200.0,
        expiry='2026-08-21', dte=42,
        entry_premium=5.50, delta=-0.20,
        iv=28.0, cash_impact=550.0,
        note='Test CSP')

    assert pos_id > 0

    pos = db.get_position(pos_id)
    assert pos['ticker'] == 'AAPL'
    assert pos['pos_type'] == 'PUT'
    assert pos['status'] == 'ACTIVE'
    assert pos['qty'] == -1
    assert pos['strike'] == 200.0
    assert pos['entry_premium'] == 5.50

    active = db.get_active_options()
    assert len(active) == 1
    assert active[0]['id'] == pos_id


def test_open_cc_position(db):
    """Open a covered call position."""
    pos_id = db.open_position(
        ticker='V', pos_type='CALL', qty=-1,
        cost_price=350.0, strike=350.0,
        expiry='2026-08-21', dte=42,
        entry_premium=7.20, delta=0.28,
        iv=24.5,
        note='Test CC')
    assert pos_id > 0

    active = db.get_active_options()
    assert len(active) == 1
    assert active[0]['pos_type'] == 'CALL'


def test_close_position_profit(db):
    """Close a short put at a profit (bought back cheaper)."""
    pos_id = db.open_position(
        ticker='SOFI', pos_type='PUT', qty=-1,
        cost_price=17.0, strike=17.0,
        expiry='2026-08-21', dte=42,
        entry_premium=0.85, delta=-0.25,
        note='Test CSP')

    # Buy back at 0.30 (profit = (0.85 - 0.30) * 1 * 100 = $55)
    pnl = db.close_position(pos_id, exit_price=0.30, reason='CLOSE_50PCT')

    assert pnl == 55.0  # (0.85 - 0.30) * 1 * 100

    pos = db.get_position(pos_id)
    assert pos['status'] == 'CLOSED'
    assert pos['exit_reason'] == 'CLOSE_50PCT'
    assert pos['realized_pnl'] == 55.0

    # Should be included in closed P&L
    assert db.get_closed_pnl() == 55.0


def test_close_position_loss(db):
    """Close a short put at a loss."""
    pos_id = db.open_position(
        ticker='NVDA', pos_type='PUT', qty=-1,
        cost_price=200.0, strike=200.0,
        expiry='2026-08-21', dte=42,
        entry_premium=3.00, delta=-0.15,
        note='Test CSP')

    # Buy back at 5.00 (loss = (3.00 - 5.00) * 1 * 100 = -$200)
    pnl = db.close_position(pos_id, exit_price=5.00, reason='STOP_LOSS')

    assert pnl == -200.0
    assert db.get_closed_pnl() == -200.0


def test_close_multiple_contracts(db):
    """Close 2 contracts at a profit."""
    pos_id = db.open_position(
        ticker='AMZN', pos_type='PUT', qty=-2,
        cost_price=230.0, strike=230.0,
        expiry='2026-08-21', dte=42,
        entry_premium=6.65, note='Test CSP x2')

    pnl = db.close_position(pos_id, exit_price=2.00, reason='CLOSE_70PCT')
    assert abs(pnl - 930.0) < 0.01  # (6.65 - 2.00) * 2 * 100 = $930


def test_expire_position(db):
    """Option expires OTM — keep full premium."""
    pos_id = db.open_position(
        ticker='MSFT', pos_type='PUT', qty=-1,
        cost_price=400.0, strike=400.0,
        expiry='2026-07-11', dte=0,
        entry_premium=4.50, note='Test CSP')

    pnl = db.expire_position(pos_id)
    assert pnl == 450.0  # 4.50 * 1 * 100

    pos = db.get_position(pos_id)
    assert pos['status'] == 'EXPIRED'
    assert pos['realized_pnl'] == 450.0


def test_assign_csp(db):
    """CSP assigned — stock added at effective cost basis."""
    pos_id = db.open_position(
        ticker='IREN', pos_type='PUT', qty=-1,
        cost_price=30.0, strike=30.0,
        expiry='2026-07-11', dte=0,
        entry_premium=2.00, note='Test CSP')

    new_stock_id = db.assign_position(pos_id, 'CSP', stock_price=28.0)
    assert new_stock_id > 0

    # CSP position should be ASSIGNED
    pos = db.get_position(pos_id)
    assert pos['status'] == 'ASSIGNED'

    # New stock should be ACTIVE with effective cost basis = strike - premium = 28.0
    stock = db.get_position(new_stock_id)
    assert stock['pos_type'] == 'STOCK'
    assert stock['status'] == 'ACTIVE'
    assert stock['qty'] == 100  # 1 contract = 100 shares
    assert stock['cost_price'] == 28.0  # 30 - 2


def test_assign_cc(db):
    """CC assigned — shares called away at strike."""
    pos_id = db.open_position(
        ticker='V', pos_type='CALL', qty=-1,
        cost_price=350.0, strike=350.0,
        expiry='2026-07-11', dte=0,
        entry_premium=5.00, note='Test CC')

    result = db.assign_position(pos_id, 'CC', stock_price=360.0)
    assert result == 0  # no new stock

    pos = db.get_position(pos_id)
    assert pos['status'] == 'ASSIGNED'
    assert pos['exit_reason'] == 'CC_ASSIGN'


# ═══════════════════════════════════════════════════════════════
# QUERIES
# ═══════════════════════════════════════════════════════════════

def test_get_active_positions_empty(db):
    """Empty portfolio returns empty list."""
    assert db.get_active_positions() == []
    assert db.get_active_options() == []
    assert db.get_active_stocks() == []


def test_get_shares(db):
    """get_shares sums all ACTIVE stock positions for a ticker."""
    db.seed_portfolio({'V': {'qty': 100, 'cost': 270.0}}, cash=10000, fund=0)
    assert db.get_shares('V') == 100
    assert db.get_shares('AAPL') == 0


def test_get_open_option_tickers(db):
    """get_open_option_tickers returns set of tickers with active options."""
    db.open_position(ticker='SOFI', pos_type='PUT', qty=-1,
                     cost_price=17.0, strike=17.0, expiry='2026-08-21',
                     dte=42, entry_premium=0.85, note='Test')
    db.open_position(ticker='AMZN', pos_type='PUT', qty=-1,
                     cost_price=230.0, strike=230.0, expiry='2026-08-21',
                     dte=42, entry_premium=6.65, note='Test')

    tickers = db.get_open_option_tickers()
    assert tickers == {'SOFI', 'AMZN'}


def test_get_daily_new_count(db):
    """get_daily_new_count counts positions opened today."""
    assert db.get_daily_new_count() == 0

    db.open_position(ticker='AAPL', pos_type='PUT', qty=-1,
                     cost_price=200.0, strike=200.0, expiry='2026-08-21',
                     dte=42, entry_premium=5.50, note='Test')
    assert db.get_daily_new_count() == 1


# ═══════════════════════════════════════════════════════════════
# SNAPSHOTS
# ═══════════════════════════════════════════════════════════════

def test_save_and_get_snapshots(db):
    """Save portfolio snapshots and retrieve them."""
    db.save_snapshot(total_value=100000, cash=45000, stock_value=55000,
                     fund_value=0, option_premium=500, option_liability=200,
                     unrealized_pnl=300, realized_pnl=100, open_positions=5)
    db.save_snapshot(total_value=101000, cash=46000, stock_value=55000,
                     fund_value=0, option_premium=400, option_liability=100,
                     unrealized_pnl=300, realized_pnl=200, open_positions=4)

    snaps = db.get_snapshots(10)
    assert len(snaps) == 2
    assert snaps[0]['total_value'] == 100000
    assert snaps[1]['total_value'] == 101000
    assert snaps[1]['realized_pnl_total'] == 200


def test_snapshot_count(db):
    """get_snapshot_count returns correct count."""
    assert db.get_snapshot_count() == 0
    db.save_snapshot(100000, 45000, 55000, 0, 500, 200, 300, 100, 5)
    assert db.get_snapshot_count() == 1
    db.save_snapshot(101000, 46000, 55000, 0, 400, 100, 300, 200, 4)
    assert db.get_snapshot_count() == 2


# ═══════════════════════════════════════════════════════════════
# ENGINE STATE
# ═══════════════════════════════════════════════════════════════

def test_state_set_get(db):
    """Key-value state persists and retrieves correctly."""
    db.set_state('cycle_count', '42')
    assert db.get_state('cycle_count') == '42'
    assert db.get_state('nonexistent', 'default') == 'default'


def test_state_overwrite(db):
    """set_state overwrites existing key."""
    db.set_state('cash', '10000')
    db.set_state('cash', '20000')
    assert db.get_state('cash') == '20000'


# ═══════════════════════════════════════════════════════════════
# RESET
# ═══════════════════════════════════════════════════════════════

def test_reset_all(db):
    """reset_all clears all data."""
    db.seed_portfolio({'V': {'qty': 100, 'cost': 270.0}}, cash=45000, fund=0)
    db.save_snapshot(100000, 45000, 55000, 0, 0, 0, 0, 0, 1)
    db.set_state('test', 'value')

    assert db.is_seeded()
    assert len(db.get_active_positions()) > 0
    assert db.get_snapshot_count() > 0

    db.reset_all()

    assert not db.is_seeded()
    assert len(db.get_active_positions()) == 0
    assert db.get_snapshot_count() == 0
    assert db.get_state('test', '') == ''


# ═══════════════════════════════════════════════════════════════
# AUDIT TRAIL
# ═══════════════════════════════════════════════════════════════

def test_audit_events_logged(db):
    """Every action should create audit events."""
    db.seed_portfolio({'V': {'qty': 100, 'cost': 270.0}}, cash=45000, fund=0)
    pos_id = db.open_position(
        ticker='SOFI', pos_type='PUT', qty=-1,
        cost_price=17.0, strike=17.0,
        expiry='2026-08-21', dte=42,
        entry_premium=0.85, note='Test')
    db.close_position(pos_id, exit_price=0.30, reason='CLOSE_50PCT')

    events = db.get_recent_events(50)
    event_types = [e['event'] for e in events]
    assert 'SEED' in event_types
    assert 'OPEN_PUT' in event_types
    assert 'CLOSE' in event_types


def test_recent_events_limit(db):
    """get_recent_events respects the limit."""
    for i in range(10):
        db._log_trade(datetime.now().isoformat(), 'TEST', 'TICK', None,
                      f'Event {i}', cash_change=0)
    events = db.get_recent_events(3)
    assert len(events) == 3


# ═══════════════════════════════════════════════════════════════
# P&L INTEGRATION
# ═══════════════════════════════════════════════════════════════

def test_pnl_lifecycle(db):
    """Full lifecycle: open → close multiple positions, verify P&L."""
    # Open and close 3 CSPs
    p1 = db.open_position(ticker='A', pos_type='PUT', qty=-1,
                          cost_price=100, strike=100, expiry='2026-08-21',
                          dte=42, entry_premium=3.00, note='Test')
    p2 = db.open_position(ticker='B', pos_type='PUT', qty=-1,
                          cost_price=200, strike=200, expiry='2026-08-21',
                          dte=42, entry_premium=5.00, note='Test')
    p3 = db.open_position(ticker='C', pos_type='PUT', qty=-2,
                          cost_price=50, strike=50, expiry='2026-08-21',
                          dte=42, entry_premium=1.50, note='Test')

    # Close with different P&Ls
    db.close_position(p1, exit_price=1.00, reason='CLOSE_50PCT')   # +200
    db.close_position(p2, exit_price=7.00, reason='STOP_LOSS')     # -200
    db.expire_position(p3)                                          # +300 (1.50 * 2 * 100)

    # Total realized P&L: 200 - 200 + 300 = 300
    assert db.get_closed_pnl() == 300.0


def test_combined_stock_and_option_positions(db):
    """Mix of stocks and options in active positions."""
    db.seed_portfolio({'V': {'qty': 100, 'cost': 270.0}}, cash=45000, fund=0)
    db.open_position(ticker='V', pos_type='CALL', qty=-1,
                     cost_price=350, strike=350, expiry='2026-08-21',
                     dte=42, entry_premium=5.00, note='CC on V')

    active = db.get_active_positions()
    assert len(active) == 2  # 1 stock + 1 option
    assert len(db.get_active_stocks()) == 1
    assert len(db.get_active_options()) == 1
    assert db.get_shares('V') == 100  # stock still held
    assert 'V' in db.get_open_option_tickers()  # option exists
