"""Tests for src/risk/overlap.py — put/call overlap analysis."""

from datetime import date, timedelta

import pytest

from src.risk.overlap import analyze_overlap

TODAY = date.today()
EXP = (TODAY + timedelta(days=30)).isoformat()
EXP2 = (TODAY + timedelta(days=60)).isoformat()


def _opt(code, ticker, type, strike, expiry=EXP, qty=-1, cost=5.0):
    return {code: {'code': code, 'ticker': ticker, 'type': type, 'strike': strike,
                   'expiry': expiry, 'qty': qty, 'cost': cost, 'pl': 0, 'pl_pct': 0}}


# ── grouping / single-sided ──────────────────────────────────────

def test_single_sided_no_report():
    options = {**_opt('C1', 'V', 'CALL', 360)}
    reports = analyze_overlap(options, {}, today=TODAY)
    assert reports == []


def test_no_overlap_between_tickers():
    options = {
        **_opt('C1', 'AAPL', 'CALL', 200),
        **_opt('P1', 'MSFT', 'PUT', 400),
    }
    reports = analyze_overlap(options, {}, today=TODAY)
    assert reports == []   # different tickers, no per-ticker overlap


# ── straddle detection ───────────────────────────────────────────

def test_straddle_detected_same_strike_and_expiry():
    options = {
        **_opt('C1', 'V', 'CALL', 360, cost=5.0),
        **_opt('P1', 'V', 'PUT', 360, cost=4.0),
    }
    reports = analyze_overlap(options, {'V': {'qty': 430, 'price': 348.0}}, today=TODAY)
    assert len(reports) == 1
    r = reports[0]
    assert r.ticker == 'V'
    assert len(r.straddles) == 1
    s = r.straddles[0]
    assert s.strike == 360.0
    assert s.premium == pytest.approx(9.0 * 100)        # (5+4)*100
    assert s.breakeven_low == pytest.approx(360 - 5 - 4)
    assert s.breakeven_high == pytest.approx(360 + 5 + 4)
    assert r.strangles == []                             # straddle present → no strangle bucket


def test_strangle_when_different_strikes_same_expiry():
    options = {
        **_opt('C1', 'V', 'CALL', 370, cost=3.0),
        **_opt('P1', 'V', 'PUT', 350, cost=4.0),
    }
    reports = analyze_overlap(options, {'V': {'qty': 100, 'price': 360.0}}, today=TODAY)
    r = reports[0]
    assert r.straddles == []
    assert len(r.strangles) == 1
    assert r.strangles[0].call_strikes == [370.0]
    assert r.strangles[0].put_strikes == [350.0]


# ── net scenarios ────────────────────────────────────────────────

def test_net_scenarios():
    # 300 shares, 2 short calls (200 shares owed), 1 short put (100 shares to buy @ 350)
    options = {
        **_opt('C1', 'V', 'CALL', 360, qty=-2, cost=5.0),
        **_opt('P1', 'V', 'PUT', 350, qty=-1, cost=4.0),
    }
    r = analyze_overlap(options, {'V': {'qty': 300, 'price': 348.0}}, today=TODAY)[0]
    assert r.shares == 300
    assert r.call_shares == 200          # 2 contracts
    assert r.put_shares == 100           # 1 contract
    assert r.total_put_assign == pytest.approx(350 * 100)
    assert r.net_if_calls == 100         # 300 - 200
    assert r.net_if_puts == 400          # 300 + 100
    assert r.net_if_all == 200           # 300 - 200 + 100


# ── stacked calls ────────────────────────────────────────────────

def test_stacked_calls_cumulative():
    options = {
        **_opt('C1', 'V', 'CALL', 360, expiry=EXP, qty=-1),
        **_opt('C2', 'V', 'CALL', 370, expiry=EXP2, qty=-2),
        **_opt('P1', 'V', 'PUT', 340, expiry=EXP, qty=-1),   # makes it an overlap ticker
    }
    r = analyze_overlap(options, {'V': {'qty': 500, 'price': 348.0}}, today=TODAY)[0]
    assert len(r.stacked_calls) == 2
    # Sorted by expiry: C1 (1 contract) then C2 (2 contracts)
    assert r.stacked_calls[0].shares_called == 100
    assert r.stacked_calls[0].cumulative_called == 100
    assert r.stacked_calls[0].shares_remaining == 400
    assert r.stacked_calls[1].cumulative_called == 300      # 100 + 200
    assert r.stacked_calls[1].shares_remaining == 200


def test_no_stacked_calls_when_single_call():
    options = {
        **_opt('C1', 'V', 'CALL', 360),
        **_opt('P1', 'V', 'PUT', 340),
    }
    r = analyze_overlap(options, {'V': {'qty': 100, 'price': 348.0}}, today=TODAY)[0]
    assert r.stacked_calls == []


# ── delta from snapshots ─────────────────────────────────────────

def test_delta_pulled_from_snapshots():
    options = {
        **_opt('C1', 'V', 'CALL', 360),
        **_opt('P1', 'V', 'PUT', 340),
    }
    snaps = {'C1': {'option_delta': 0.42}, 'P1': {'option_delta': -0.55}}
    r = analyze_overlap(options, {'V': {'qty': 100, 'price': 348.0}},
                        snapshots=snaps, today=TODAY)[0]
    call = next(c for c in r.calls if c.code == 'C1')
    put = next(p for p in r.puts if p.code == 'P1')
    assert call.delta == pytest.approx(0.42)
    assert put.delta == pytest.approx(-0.55)


def test_dte_computed_from_expiry():
    options = {
        **_opt('C1', 'V', 'CALL', 360, expiry=EXP),
        **_opt('P1', 'V', 'PUT', 340, expiry=EXP),
    }
    r = analyze_overlap(options, {'V': {'qty': 100, 'price': 348.0}}, today=TODAY)[0]
    assert all(leg.dte == 30 for leg in r.calls + r.puts)
