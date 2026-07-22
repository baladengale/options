"""Tests for src/data/portfolio_loader.py — the shared REAL-account reader."""

import pandas as pd
import pytest

from moomoo import RET_OK

from src.data.portfolio_loader import (
    Funds, Portfolio, parse_option_code, is_option_code,
    fetch_funds, fetch_positions, fetch_portfolio, fetch_orders,
)


# ── parse_option_code ──────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    # CALL, single-char ticker
    ("US.V260918C360000", ("V", "2026-09-18", "CALL", 360.0)),
    # PUT, multi-char ticker (non-greedy backtracks to digit boundary)
    ("US.GOOGL260717P335000", ("GOOGL", "2026-07-17", "PUT", 335.0)),
    ("US.AVGO260731P350000", ("AVGO", "2026-07-31", "PUT", 350.0)),
    # strike not a round number (÷1000)
    ("US.NVDA260814C220500", ("NVDA", "2026-08-14", "CALL", 220.5)),
    # 3-char ticker
    ("US.AMD260805P470000", ("AMD", "2026-08-05", "PUT", 470.0)),
    # short-encoded strike (e.g. $5 → 005000)
    ("US.BE260717P005000", ("BE", "2026-07-17", "PUT", 5.0)),
])
def test_parse_option_code_valid(code, expected):
    assert parse_option_code(code) == expected


@pytest.mark.parametrize("code", [
    "", None,
    "US.V",                       # plain stock, not an option
    "US.MSFT",                    # stock
    "US.AAPL260717",              # no C/P + strike
    "GOOGL260717P335000",         # missing US. prefix
    "US.GOOGL260717X335000",      # invalid type letter (X)
    "US..GOOGL260717P335000",     # double-dot guard
    "US.V..260918C360000",
])
def test_parse_option_code_invalid(code):
    assert parse_option_code(code) is None


def test_is_option_code():
    assert is_option_code("US.V260918C360000") is True
    assert is_option_code("US.GOOGL260717P335000") is True
    assert is_option_code("US.V") is False
    assert is_option_code("") is False


# ── Mock trade context ────────────────────────────────────────────

class FakeTrd:
    """Mimics moomoo OpenSecTradeContext: methods return (RET_OK, DataFrame)."""

    def __init__(self, positions=None, funds=None, with_simulate=True,
                 history_orders=None, live_orders=None):
        rows = []
        if with_simulate:
            rows.append({'trd_env': 'SIMULATE', 'acc_id': 999})
        rows.append({'trd_env': 'REAL', 'acc_id': 1})
        self._acc_list = pd.DataFrame(rows)
        self._positions = pd.DataFrame(positions or [])
        self._funds = pd.DataFrame([funds] if funds else [])
        self._hist = pd.DataFrame(history_orders or [])
        self._live = pd.DataFrame(live_orders or [])
        self.closed = False

    def get_acc_list(self):
        return RET_OK, self._acc_list

    def accinfo_query(self, trd_env=None, acc_id=None, refresh_cache=False):
        return RET_OK, self._funds

    def position_list_query(self, trd_env=None, acc_id=None, refresh_cache=False):
        return RET_OK, self._positions

    def history_order_list_query(self, trd_env=None, acc_id=None, start=None, end=None):
        return RET_OK, self._hist

    def order_list_query(self, trd_env=None, acc_id=None, refresh_cache=False):
        return RET_OK, self._live

    def close(self):
        self.closed = True


def _pos(code, qty, cost=10.0, nominal=10.0, pl_val=0.0, pl_ratio=0.0):
    return {
        'code': code, 'qty': qty, 'cost_price': cost,
        'nominal_price': nominal, 'pl_val': pl_val, 'pl_ratio': pl_ratio,
    }


# ── fetch_funds ───────────────────────────────────────────────────

def test_fetch_funds_usd():
    trd = FakeTrd(funds={
        'us_cash': 817.0, 'usd_net_cash_power': 48638.89,
        'fund_assets': 48500.0, 'currency': 'USD',
        'total_assets': 100000, 'total_liabilities': 5000,
        'net_assets': 95000, 'margin_used_pct': 12.3,
    })
    f = fetch_funds(trd)
    assert f.cash == 817.0
    assert f.buying_power == 48638.89
    assert f.fund == 48500.0          # no conversion for USD
    assert f.total_assets == 100000
    assert f.net_assets == 95000
    assert f.margin_used_pct == 12.3
    assert f.liquid == pytest.approx(49317.0)


def test_fetch_funds_hkd_normalizes_to_usd():
    trd = FakeTrd(funds={
        'us_cash': 0, 'usd_net_cash_power': 0,
        'fund_assets': 378300.0, 'currency': 'HKD',
    })
    f = fetch_funds(trd)
    assert f.fund == pytest.approx(48500.0)      # 378300 / 7.8
    assert f.currency == 'HKD'


def test_fetch_funds_skips_simulate_account():
    # Only SIMULATE account present → empty Funds
    trd = FakeTrd(funds={'us_cash': 100, 'currency': 'USD'}, with_simulate=True)
    trd._acc_list = pd.DataFrame([{'trd_env': 'SIMULATE', 'acc_id': 999}])
    assert fetch_funds(trd) == Funds()


# ── fetch_positions ───────────────────────────────────────────────

def test_fetch_positions_stocks_and_options():
    trd = FakeTrd(positions=[
        _pos('US.V', 430, cost=270.0, nominal=348.0, pl_val=33540, pl_ratio=28.7),
        _pos('US.AVGO260731P350000', -1, cost=11.2, pl_val=705, pl_ratio=62.9),
        _pos('US.V260821C380000', -1, cost=5.15, pl_val=255.5, pl_ratio=49.6),
        _pos('US.GOOG260717P335000', -1, cost=1.22, pl_val=-33, pl_ratio=-27.0),
        _pos('US.MSFT', 0),             # zero-qty → skipped
        _pos('US..BAD260717P335000', -1),  # double-dot → skipped
    ])
    stocks, options = fetch_positions(trd)

    assert set(stocks.keys()) == {'V'}
    assert stocks['V']['qty'] == 430
    assert stocks['V']['cost'] == 270.0
    assert stocks['V']['price'] == 348.0
    assert stocks['V']['mv'] == pytest.approx(430 * 348.0)
    assert stocks['V']['pl_pct'] == pytest.approx(28.7)

    assert set(options.keys()) == {
        'US.AVGO260731P350000', 'US.V260821C380000', 'US.GOOG260717P335000',
    }
    avgo = options['US.AVGO260731P350000']
    assert avgo['ticker'] == 'AVGO'
    assert avgo['type'] == 'PUT'
    assert avgo['strike'] == 350.0
    assert avgo['expiry'] == '2026-07-31'
    assert avgo['qty'] == -1
    assert avgo['cost'] == 11.2

    call = options['US.V260821C380000']
    assert call['type'] == 'CALL'
    assert call['strike'] == 380.0
    assert call['pl_pct'] == pytest.approx(49.6)


def test_fetch_positions_empty():
    trd = FakeTrd(positions=[])
    stocks, options = fetch_positions(trd)
    assert stocks == {}
    assert options == {}


# ── Portfolio aggregates ──────────────────────────────────────────

def test_portfolio_aggregates():
    pf = Portfolio(
        funds=Funds(cash=817.0, fund=48500.0),
        stocks={'V': {'qty': 430, 'cost': 270.0, 'price': 348.0, 'mv': 149640.0, 'pl': 0, 'pl_pct': 0}},
        options={
            'US.AVGO260731P350000': {'ticker': 'AVGO', 'type': 'PUT', 'strike': 350.0, 'qty': -1},
            'US.V260821C380000': {'ticker': 'V', 'type': 'CALL', 'strike': 380.0, 'qty': -1},
        },
    )
    assert pf.funds.liquid == pytest.approx(49317.0)
    assert pf.stock_value == pytest.approx(149640.0)
    assert pf.net_liquidation == pytest.approx(198957.0)
    assert pf.option_tickers == {'AVGO', 'V'}
    # CSP liability = only the PUT, 1 contract × 350 strike × 100
    assert pf.csp_liability == pytest.approx(35000.0)


def test_fetch_portfolio_closes_context(monkeypatch):
    """fetch_portfolio opens/closes its own context and returns a Portfolio."""
    import src.data.portfolio_loader as pl

    fake = FakeTrd(
        positions=[_pos('US.V', 430, cost=270.0, nominal=348.0)],
        funds={'us_cash': 817.0, 'usd_net_cash_power': 48638.89,
               'fund_assets': 48500.0, 'currency': 'USD'},
    )
    monkeypatch.setattr(pl, 'OpenSecTradeContext', lambda **kw: fake)
    pf = fetch_portfolio()
    assert fake.closed is True
    assert pf.funds.cash == 817.0
    assert 'V' in pf.stocks


def test_fetch_portfolio_returns_empty_on_failure(monkeypatch):
    import src.data.portfolio_loader as pl

    def _boom(**kw):
        raise ConnectionError("OpenD not running")

    monkeypatch.setattr(pl, 'OpenSecTradeContext', _boom)
    pf = fetch_portfolio()
    assert pf == Portfolio()
    assert pf.stocks == {}


# ── fetch_orders ──────────────────────────────────────────────────

def _ord(oid, side, code, qty, price, status='FILLED_ALL', ts='2026-07-15 10:00:00'):
    return {'order_id': oid, 'trd_side': side, 'code': code, 'qty': qty,
            'dealt_avg_price': price, 'order_status': status, 'updated_time': ts}


def test_fetch_orders_merges_history_and_live_and_dedups():
    trd = FakeTrd(
        history_orders=[
            _ord('A1', 'SELL_SHORT', 'US.AVGO260731P350000', 1, 11.2),
            _ord('A2', 'BUY',        'US.V',                   100, 270.0),
        ],
        live_orders=[
            _ord('A2', 'BUY', 'US.V', 100, 270.0),   # dup of A2 → dropped
            _ord('A3', 'BUY_BACK', 'US.AVGO260731P350000', 1, 4.0),
        ],
    )
    orders = fetch_orders(trd)
    ids = [o['order_id'] for o in orders]
    assert ids == ['A1', 'A2', 'A3']          # deduped, order preserved
    o0 = orders[0]
    assert o0['side'] == 'SELL_SHORT'
    assert o0['price'] == 11.2
    assert o0['date'] == '2026-07-15'
    assert o0['code'] == 'US.AVGO260731P350000'


def test_fetch_orders_empty():
    assert fetch_orders(FakeTrd()) == []
