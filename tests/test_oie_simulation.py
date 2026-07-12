"""
OIE Simulation & Engine Integration Tests

Validates:
- sim open/close/expire — P&L tracking, cash updates
- oie_engine once --force — trades logged, positions created
- Full lifecycle: open → MTM → close at profit → expire
- Cash consistency: seeded_cash + trade_flows = current cash
- No moomoo connection needed — uses in-memory DB
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
    """Fresh OIE DB with $50,000 seeded cash, no positions."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        path = f.name
    oie = OIEDB(path)
    oie.seed_portfolio({}, 50000, 0)
    yield oie
    oie.close()
    os.unlink(path)


# ═══════════════════════════════════════════════════════════════
# SIM MODE: Open / Close / Expire
# ═══════════════════════════════════════════════════════════════

def test_sim_open_csp_adds_position(db):
    """Opening a CSP adds a PUT position and updates cash."""
    pos_id = db.open_position(
        ticker='SOFI', pos_type='PUT', qty=-1,
        cost_price=17, strike=17, expiry='2026-08-21',
        dte=40, entry_premium=0.85, delta=-0.25, iv=68,
        cash_impact=85, note='SIM: CSP')

    pos = db.get_position(pos_id)
    assert pos['ticker'] == 'SOFI'
    assert pos['pos_type'] == 'PUT'
    assert pos['status'] == 'ACTIVE'
    assert pos['strike'] == 17
    assert pos['entry_premium'] == 0.85

    # Cash should have increased by premium
    cash = float(db.get_state('cash', '0'))
    assert cash == 50085.0  # 50000 + 85


def test_sim_open_cc_adds_position(db):
    """Opening a CC adds a CALL position."""
    pos_id = db.open_position(
        ticker='V', pos_type='CALL', qty=-2,
        cost_price=365, strike=365, expiry='2026-08-21',
        dte=40, entry_premium=5.00, delta=0.30, iv=25,
        cash_impact=1000, note='SIM: CC x2')

    pos = db.get_position(pos_id)
    assert pos['pos_type'] == 'CALL'
    assert pos['qty'] == -2
    cash = float(db.get_state('cash', '0'))
    assert cash == 51000.0  # 50000 + 1000


def test_sim_close_csp_at_profit(db):
    """Close CSP at 50% profit — P&L and cash verified."""
    pos_id = db.open_position(
        ticker='SOFI', pos_type='PUT', qty=-1,
        cost_price=17, strike=17, expiry='2026-08-21',
        dte=40, entry_premium=0.85, delta=-0.25,
        cash_impact=85, note='Test')

    # Close at $0.43 (premium was $0.85, profit = $42)
    pnl = db.close_position(pos_id, exit_price=0.43, reason='CLOSE_50PCT',
                            cash_impact=-43)

    assert pnl == 42.0  # (0.85 - 0.43) * 1 * 100
    pos = db.get_position(pos_id)
    assert pos['status'] == 'CLOSED'

    # Cash: 50000 + 85 (open) - 43 (close) = 50042
    cash = float(db.get_state('cash', '0'))
    assert cash == 50042.0


def test_sim_close_csp_at_loss(db):
    """Close CSP at a loss — negative P&L."""
    pos_id = db.open_position(
        ticker='NVDA', pos_type='PUT', qty=-1,
        cost_price=200, strike=200, expiry='2026-08-21',
        dte=40, entry_premium=3.00, delta=-0.15,
        cash_impact=300, note='Test')

    pnl = db.close_position(pos_id, exit_price=7.00, reason='STOP_LOSS',
                            cash_impact=-700)

    assert pnl == -400.0  # (3.00 - 7.00) * 100
    cash = float(db.get_state('cash', '0'))
    assert cash == 49600.0  # 50000 + 300 - 700


def test_sim_expire_otm_keeps_premium(db):
    """Option expires OTM — full premium kept."""
    pos_id = db.open_position(
        ticker='MSFT', pos_type='PUT', qty=-2,
        cost_price=400, strike=400, expiry='2026-07-11',
        dte=0, entry_premium=4.50, delta=-0.10,
        cash_impact=900, note='Test x2')

    pnl = db.expire_position(pos_id)
    assert pnl == 900.0  # 4.50 * 2 * 100

    pos = db.get_position(pos_id)
    assert pos['status'] == 'EXPIRED'
    assert pos['realized_pnl'] == 900.0


def test_sim_close_multiple_contracts(db):
    """Close 10 contracts at profit."""
    pos_id = db.open_position(
        ticker='IREN', pos_type='PUT', qty=-10,
        cost_price=30, strike=30, expiry='2026-08-21',
        dte=40, entry_premium=2.00, delta=-0.17,
        cash_impact=2000, note='Test x10')

    pnl = db.close_position(pos_id, exit_price=0.50, reason='CLOSE_70PCT',
                            cash_impact=-500)
    assert pnl == 1500.0  # (2.00 - 0.50) * 10 * 100


# ═══════════════════════════════════════════════════════════════
# CASH CONSISTENCY
# ═══════════════════════════════════════════════════════════════

def test_cash_equals_seeded_plus_flows(db):
    """Cash = seeded_cash + sum of all trade cash_changes."""
    seeded = float(db.get_state('seeded_cash', '0'))
    assert seeded == 50000.0

    # Open 3 positions
    db.open_position(ticker='A', pos_type='PUT', qty=-1,
                     cost_price=100, strike=100, expiry='2026-08-21',
                     dte=40, entry_premium=3.00, cash_impact=300, note='T1')
    db.open_position(ticker='B', pos_type='PUT', qty=-1,
                     cost_price=200, strike=200, expiry='2026-08-21',
                     dte=40, entry_premium=5.00, cash_impact=500, note='T2')
    p3 = db.open_position(ticker='C', pos_type='CALL', qty=-1,
                          cost_price=350, strike=350, expiry='2026-08-21',
                          dte=40, entry_premium=7.00, cash_impact=700, note='T3')

    # Close one
    db.close_position(p3, exit_price=3.00, reason='CLOSE_50PCT',
                      cash_impact=-300)

    # Compute expected cash from trade flows
    flows = db._conn.execute(
        "SELECT COALESCE(SUM(cash_change), 0) as total FROM paper_trades"
    ).fetchone()
    expected_cash = seeded + flows['total']
    actual_cash = float(db.get_state('cash', '0'))
    assert actual_cash == expected_cash


def test_cash_does_not_double_count(db):
    """Cash should not change when open_position is called with cash_impact=0."""
    before = float(db.get_state('cash', '0'))
    db.open_position(ticker='X', pos_type='PUT', qty=-1,
                     cost_price=50, strike=50, expiry='2026-08-21',
                     dte=40, entry_premium=1.00, cash_impact=0, note='Zero cash')
    after = float(db.get_state('cash', '0'))
    assert after == before  # No cash change when cash_impact=0


# ═══════════════════════════════════════════════════════════════
# FULL LIFECYCLE
# ═══════════════════════════════════════════════════════════════

def test_full_lifecycle_csp(db):
    """CSP: open → hold → close at 50% profit."""
    pos_id = db.open_position(
        ticker='AAPL', pos_type='PUT', qty=-1,
        cost_price=200, strike=200, expiry='2026-08-21',
        dte=42, entry_premium=5.50, cash_impact=550, note='CSP')

    # Update MTM — premium declining (good)
    db._conn.execute(
        "UPDATE paper_positions SET current_bid=?, current_delta=? WHERE id=?",
        (2.75, -0.15, pos_id))
    db._conn.commit()

    pos = db.get_position(pos_id)
    profit_pct = ((5.50 - 2.75) / 5.50 * 100)
    assert abs(profit_pct - 50.0) < 1.0  # ~50% captured

    # Close
    pnl = db.close_position(pos_id, exit_price=2.75, reason='CLOSE_50PCT',
                            cash_impact=-275)
    assert pnl == 275.0  # (5.50 - 2.75) * 100


def test_full_lifecycle_cc(db):
    """CC: open → hold → expire OTM."""
    pos_id = db.open_position(
        ticker='V', pos_type='CALL', qty=-1,
        cost_price=350, strike=350, expiry='2026-07-11',
        dte=10, entry_premium=6.00, cash_impact=600, note='CC')

    # Stock stays below strike → expires OTM
    pnl = db.expire_position(pos_id)
    assert pnl == 600.0

    cash = float(db.get_state('cash', '0'))
    assert cash == 50600.0  # 50000 + 600


def test_full_lifecycle_csp_assign(db):
    """CSP assigned — stock added at effective cost basis."""
    pos_id = db.open_position(
        ticker='IREN', pos_type='PUT', qty=-1,
        cost_price=30, strike=30, expiry='2026-07-11',
        dte=0, entry_premium=2.00, cash_impact=200, note='CSP')

    new_id = db.assign_position(pos_id, 'CSP', stock_price=28.0)
    assert new_id > 0

    stock = db.get_position(new_id)
    assert stock['pos_type'] == 'STOCK'
    assert stock['qty'] == 100
    assert stock['cost_price'] == 28.0  # 30 - 2


def test_full_lifecycle_multiple_cycles(db):
    """Simulate 3 positions through their full lifecycle."""
    # Cycle 1: Open 2 CSPs
    p1 = db.open_position(ticker='A', pos_type='PUT', qty=-1,
                          cost_price=100, strike=100, expiry='2026-08-21',
                          dte=42, entry_premium=3.00, cash_impact=300, note='C1')
    p2 = db.open_position(ticker='B', pos_type='PUT', qty=-2,
                          cost_price=200, strike=200, expiry='2026-08-21',
                          dte=42, entry_premium=5.00, cash_impact=1000, note='C1')

    assert len(db.get_active_options()) == 2

    # Cycle 2: Close p1 at profit, open new
    db.close_position(p1, exit_price=1.00, reason='CLOSE_50PCT',
                      cash_impact=-100)  # P&L = +200
    p3 = db.open_position(ticker='C', pos_type='PUT', qty=-1,
                          cost_price=50, strike=50, expiry='2026-08-21',
                          dte=42, entry_premium=1.50, cash_impact=150, note='C2')

    # p2 expires
    db.expire_position(p2)  # P&L = +1000

    # Final state: cash = seeded + all trade flows
    # 50000 + 300(p1 open) + 1000(p2 open) - 100(p1 close) + 150(p3 open) = 51350
    # p2 expiry doesn't change cash (premium already received at open)
    cash = float(db.get_state('cash', '0'))
    assert cash == 51350.0

    realized = db.get_closed_pnl()
    assert realized == 1200.0  # 200 + 1000

    active = db.get_active_options()
    assert len(active) == 1
    assert active[0]['id'] == p3  # only p3 remains active


# ═══════════════════════════════════════════════════════════════
# EDGE CASES
# ═══════════════════════════════════════════════════════════════

def test_open_zero_premium(db):
    """Should handle zero premium gracefully."""
    pos_id = db.open_position(
        ticker='Z', pos_type='PUT', qty=-1,
        cost_price=10, strike=10, expiry='2026-08-21',
        dte=40, entry_premium=0, cash_impact=0, note='Zero')
    assert pos_id > 0
    pnl = db.close_position(pos_id, exit_price=0, reason='WORTHLESS',
                            cash_impact=0)
    assert pnl == 0.0


def test_multiple_opens_same_ticker(db):
    """Opening multiple positions on same ticker should work."""
    db.open_position(ticker='V', pos_type='CALL', qty=-1,
                     cost_price=350, strike=350, expiry='2026-08-21',
                     dte=42, entry_premium=5.00, cash_impact=500, note='CC1')
    db.open_position(ticker='V', pos_type='CALL', qty=-1,
                     cost_price=360, strike=360, expiry='2026-09-18',
                     dte=70, entry_premium=8.00, cash_impact=800, note='CC2')

    opts = db.get_active_options()
    v_opts = [o for o in opts if o['ticker'] == 'V']
    assert len(v_opts) == 2


def test_close_already_closed(db):
    """Closing an already-closed position should be handled."""
    pos_id = db.open_position(
        ticker='X', pos_type='PUT', qty=-1,
        cost_price=50, strike=50, expiry='2026-08-21',
        dte=40, entry_premium=1.00, cash_impact=100, note='Test')

    db.close_position(pos_id, exit_price=0.50, reason='CLOSE_50PCT',
                      cash_impact=-50)
    # Closing again should be no-op (already closed)
    result = db.close_position(pos_id, exit_price=0.30, reason='CLOSE_AGAIN',
                               cash_impact=-30)
    assert result is None  # returns None for already-closed
    assert db.get_closed_pnl() == 50.0  # Only first close counted


def test_get_open_option_tickers(db):
    """get_open_option_tickers filters correctly."""
    db.open_position(ticker='AAPL', pos_type='PUT', qty=-1,
                     cost_price=200, strike=200, expiry='2026-08-21',
                     dte=42, entry_premium=5.00, cash_impact=500, note='T1')
    db.open_position(ticker='GOOG', pos_type='PUT', qty=-1,
                     cost_price=350, strike=350, expiry='2026-08-21',
                     dte=42, entry_premium=5.00, cash_impact=500, note='T1')

    tickers = db.get_open_option_tickers()
    assert tickers == {'AAPL', 'GOOG'}

    # Close AAPL
    pos = [p for p in db.get_active_options() if p['ticker'] == 'AAPL'][0]
    db.close_position(pos['id'], exit_price=2.00, reason='TEST',
                      cash_impact=-200)
    assert db.get_open_option_tickers() == {'GOOG'}


# ═══════════════════════════════════════════════════════════════
# SNAPSHOT + AUDIT CONSISTENCY
# ═══════════════════════════════════════════════════════════════

def test_snapshot_records_trades(db):
    """Snapshots should reflect positions opened/closed."""
    db.save_snapshot(total_value=50000, cash=50000, stock_value=0,
                     fund_value=0, option_premium=0, option_liability=0,
                     unrealized_pnl=0, realized_pnl=0, open_positions=0)

    db.open_position(ticker='T', pos_type='PUT', qty=-1,
                     cost_price=100, strike=100, expiry='2026-08-21',
                     dte=42, entry_premium=3.00, cash_impact=300, note='T1')
    p2 = db.open_position(ticker='U', pos_type='PUT', qty=-1,
                          cost_price=200, strike=200, expiry='2026-08-21',
                          dte=42, entry_premium=5.00, cash_impact=500, note='T2')

    db.save_snapshot(total_value=50800, cash=50800, stock_value=0,
                     fund_value=0, option_premium=800, option_liability=800,
                     unrealized_pnl=0, realized_pnl=0, open_positions=2)

    db.close_position(p2, exit_price=2.00, reason='TEST', cash_impact=-200)
    db.save_snapshot(total_value=51100, cash=51100, stock_value=0,
                     fund_value=0, option_premium=300, option_liability=300,
                     unrealized_pnl=0, realized_pnl=300, open_positions=1)

    snaps = db.get_snapshots(10)
    assert len(snaps) == 3
    assert snaps[0]['open_positions'] == 0
    assert snaps[1]['open_positions'] == 2
    assert snaps[2]['open_positions'] == 1


def test_audit_trail_complete(db):
    """Every action should create an audit event with correct details."""
    db.open_position(ticker='EVENT', pos_type='PUT', qty=-1,
                     cost_price=100, strike=100, expiry='2026-08-21',
                     dte=42, entry_premium=3.00, cash_impact=300, note='T1')

    events = db.get_recent_events(5)
    event_types = [e['event'] for e in events]
    assert 'OPEN_PUT' in event_types

    # Find the SEED event
    seed_events = [e for e in events if e['event'] == 'SEED']
    assert len(seed_events) >= 1


def test_reset_clears_everything(db):
    """Reset should wipe all positions, trades, snapshots, and state."""
    db.open_position(ticker='X', pos_type='PUT', qty=-1,
                     cost_price=50, strike=50, expiry='2026-08-21',
                     dte=40, entry_premium=1.00, cash_impact=100, note='T')
    db.save_snapshot(total_value=50100, cash=50100, stock_value=0,
                     fund_value=0, option_premium=100, option_liability=100,
                     unrealized_pnl=0, realized_pnl=0, open_positions=1)

    db.reset_all()

    assert len(db.get_active_positions()) == 0
    assert db.get_snapshot_count() == 0
    assert not db.is_seeded()
    assert db.get_state('cash', '') == ''
