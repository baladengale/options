"""Tests for src/portfolio/summary.py — income, monthly, sector, P&L roll-ups."""

from datetime import date, timedelta

import pytest

from src.portfolio.summary import (
    compute_income, compute_sector_breakdown,
    unrealized_stock_pl, unrealized_option_pl, stock_market_value,
    order_income_breakdown,
)


def _order(side, code, qty, price, status='FILLED_ALL', date='2026-07-15', oid=None):
    return {
        'order_id': oid or f'{side}-{code}-{qty}-{price}-{date}',
        'date': date, 'code': code, 'side': side,
        'qty': qty, 'price': price, 'status': status,
    }


# ── compute_income ───────────────────────────────────────────────

def test_income_option_premium_collected_and_paid():
    orders = [
        _order('SELL_SHORT', 'US.AVGO260731P350000', qty=1, price=11.20),   # +$1120
        _order('BUY_BACK',   'US.AVGO260731P350000', qty=1, price=4.00),    # -$400
    ]
    s = compute_income(orders)
    assert s.premium_collected == pytest.approx(1120.0)   # 1 × 11.20 × 100
    assert s.premium_paid == pytest.approx(400.0)
    assert s.net_option_income == pytest.approx(720.0)
    assert s.filled_order_count == 2


def test_income_stock_buys_and_sells():
    orders = [
        _order('BUY',  'US.V',  qty=100, price=270.0),   # $27,000
        _order('SELL', 'US.V',  qty=10,  price=350.0),   # $3,500
    ]
    s = compute_income(orders)
    assert s.stock_bought == pytest.approx(27000.0)
    assert s.stock_sold == pytest.approx(3500.0)
    assert s.premium_collected == 0
    assert s.premium_paid == 0


def test_income_ignores_unfilled_orders():
    orders = [_order('SELL_SHORT', 'US.V260821C380000', qty=1, price=5.0, status='CANCELLED')]
    assert compute_income(orders).filled_order_count == 0


def test_income_monthly_option_only_bucketing():
    orders = [
        _order('SELL_SHORT', 'US.AVGO260630P350000', qty=1, price=10.0, date='2026-06-10'),
        _order('SELL_SHORT', 'US.AVGO260731P350000', qty=2, price=5.0,  date='2026-07-01'),
        _order('BUY_BACK',   'US.AVGO260731P350000', qty=1, price=2.0,  date='2026-07-20'),
        # Stock sell in July — should NOT appear in monthly option buckets
        _order('SELL', 'US.V', qty=10, price=350.0, date='2026-07-22'),
    ]
    s = compute_income(orders)
    assert set(s.monthly.keys()) == {'2026-06', '2026-07'}
    assert s.monthly['2026-06']['collected'] == pytest.approx(1000.0)   # 1×10×100
    assert s.monthly['2026-06']['buyback'] == 0
    assert s.monthly['2026-07']['collected'] == pytest.approx(1000.0)   # 2×5×100
    assert s.monthly['2026-07']['buyback'] == pytest.approx(200.0)      # 1×2×100


def test_income_empty():
    s = compute_income([])
    assert s.net_option_income == 0
    assert s.monthly == {}
    assert s.filled_order_count == 0


# ── sector breakdown ─────────────────────────────────────────────

def test_sector_breakdown_groups_by_sector():
    stocks = {
        'V':    {'mv': 100000.0},
        'MSFT': {'mv': 50000.0},
        'GOOGL': {'mv': 30000.0},
        'BE':   {'mv': 5000.0},     # Energy
        'UNKW': {'mv': 2000.0},     # not in map → Other
    }
    sec = compute_sector_breakdown(stocks)
    assert sec['Financial'] == pytest.approx(100000.0)
    assert sec['Technology'] == pytest.approx(80000.0)   # MSFT + GOOGL
    assert sec['Energy'] == pytest.approx(5000.0)
    assert sec['Other'] == pytest.approx(2000.0)


def test_sector_breakdown_custom_map():
    sec = compute_sector_breakdown({'X': {'mv': 100}}, sector_map={'X': 'Custom'})
    assert sec == {'Custom': 100.0}


# ── unrealized P&L + market value ────────────────────────────────

def test_unrealized_and_mv():
    stocks = {'V': {'pl': 33540.0, 'mv': 149640.0}, 'MSFT': {'pl': -1000.0, 'mv': 50000.0}}
    options = {'US.AVGO260731P350000': {'pl': 705.0}, 'US.V260821C380000': {'pl': -200.0}}
    assert unrealized_stock_pl(stocks) == pytest.approx(32540.0)
    assert unrealized_option_pl(options) == pytest.approx(505.0)
    assert stock_market_value(stocks) == pytest.approx(199640.0)


# ── option-first classification (the P&L discrepancy fix) ─────────
# Regression: covered-call sells come back from moomoo as plain SELL (not
# SELL_SHORT), and CC buy-to-close as plain BUY. The old code misclassified
# those into stock_sold / stock_bought (×100), understating premium and
# inflating stock totals. Option-code SELL/BUY must route to premium buckets.

def test_income_covered_call_sell_classified_as_premium():
    """A CC sold against held shares (trd_side=SELL) is option premium."""
    orders = [
        _order('SELL', 'US.V260821C380000', qty=1, price=8.30),   # CC sold → +$830
    ]
    s = compute_income(orders)
    assert s.premium_collected == pytest.approx(830.0)
    assert s.stock_sold == 0                       # NOT misclassified as stock
    assert s.net_option_income == pytest.approx(830.0)


def test_income_option_buy_classified_as_premium_paid():
    """An option buy-to-close (trd_side=BUY) is premium paid, not stock bought."""
    orders = [
        _order('BUY', 'US.AVGO260731P350000', qty=1, price=4.00),  # buy-back → -$400
    ]
    s = compute_income(orders)
    assert s.premium_paid == pytest.approx(400.0)
    assert s.stock_bought == 0


def test_income_stock_sell_still_routes_to_stock_bucket():
    """A genuine STOCK sell (non-option code) must still go to stock_sold."""
    orders = [_order('SELL', 'US.V', qty=10, price=350.0)]   # $3,500
    s = compute_income(orders)
    assert s.stock_sold == pytest.approx(3500.0)
    assert s.premium_collected == 0


def test_income_cc_and_csp_agree_across_side_labels():
    """Both SELL/SELL_SHORT collects and BUY/BUY_BACK pays — unified."""
    orders = [
        _order('SELL',       'US.V260821C380000',   qty=1, price=8.30),   # CC +830
        _order('SELL_SHORT', 'US.AVGO260731P350000', qty=1, price=11.20), # CSP +1120
        _order('BUY',        'US.AVGO260731P350000', qty=1, price=4.00),  # close -400
        _order('BUY_BACK',   'US.V260821C380000',   qty=1, price=2.00),   # close -200
    ]
    s = compute_income(orders)
    assert s.premium_collected == pytest.approx(830.0 + 1120.0)
    assert s.premium_paid == pytest.approx(400.0 + 200.0)
    assert s.net_option_income == pytest.approx(1350.0)
    assert s.stock_sold == 0 and s.stock_bought == 0


# ── order_income_breakdown (shared --orders view) ─────────────────

def _order_full(side, code, qty, price, status='FILLED_ALL', d=None, oid=None):
    d = d or date.today().isoformat()
    return {'order_id': oid or f'{side}-{code}-{d}',
            'date': d, 'code': code, 'side': side,
            'qty': qty, 'price': price, 'status': status}


def test_breakdown_totals_match_compute_income():
    """The --orders view must agree with --pnl (same classification)."""
    today = date.today()
    orders = [
        _order_full('SELL', 'US.V260821C380000', 1, 8.30, d=(today - timedelta(days=5)).isoformat()),
        _order_full('SELL_SHORT', 'US.AVGO260731P350000', 1, 11.20, d=(today - timedelta(days=10)).isoformat()),
        _order_full('BUY', 'US.AVGO260731P350000', 1, 4.00, d=(today - timedelta(days=2)).isoformat()),
    ]
    full = compute_income(orders)
    _, breakdown = order_income_breakdown(orders, days=90, today=today)
    assert breakdown.premium_collected == pytest.approx(full.premium_collected)
    assert breakdown.premium_paid == pytest.approx(full.premium_paid)
    assert breakdown.net_option_income == pytest.approx(full.net_option_income)


def test_breakdown_filters_outside_90_day_window():
    """Orders older than the window are excluded."""
    today = date.today()
    orders = [
        _order_full('SELL', 'US.V260821C380000', 1, 8.30,
                    d=(today - timedelta(days=200)).isoformat()),   # too old
        _order_full('SELL', 'US.V260821C380000', 1, 5.00,
                    d=(today - timedelta(days=10)).isoformat()),    # in window
    ]
    rows, summary = order_income_breakdown(orders, days=90, today=today)
    assert summary.filled_order_count == 1
    assert summary.premium_collected == pytest.approx(500.0)
    assert len(rows) == 1


def test_breakdown_ticker_filter_matches_underlying():
    """--orders V filters by the option's underlying, not the raw code string."""
    today = date.today()
    orders = [
        _order_full('SELL', 'US.V260821C380000', 1, 8.30, d=today.isoformat()),    # V
        _order_full('SELL', 'US.AVGO260731P350000', 1, 11.20, d=today.isoformat()), # AVGO
    ]
    rows, summary = order_income_breakdown(orders, ticker_filter='V', days=90, today=today)
    assert summary.filled_order_count == 1
    assert all(r.ticker == 'V' for r in rows)


def test_breakdown_rows_newest_first_and_signed_amount():
    today = date.today()
    orders = [
        _order_full('SELL', 'US.V260821C380000', 1, 8.30,
                    d=(today - timedelta(days=20)).isoformat()),
        _order_full('BUY', 'US.V260821C380000', 1, 2.00,
                    d=(today - timedelta(days=1)).isoformat()),
    ]
    rows, _ = order_income_breakdown(orders, days=90, today=today)
    assert rows[0].date >= rows[1].date                      # newest first
    assert rows[0].amount < 0 and rows[1].amount > 0         # BUY negative, SELL positive
