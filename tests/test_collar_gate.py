"""Tests for the CC collar gate — no CC on shares already committed to open
short calls (SPECS §12.1). Regression guard for the naked-call near-miss:
the --health holdings table once recommended a 6th CC on 500 V shares that
were 100% encumbered by 5 open CCs.
"""

import pytest

from src.data.models import StockSnapshot, OptionSnapshot
from src.scoring.holding_score import _find_best_cc
from src.risk.collar_check import check_cc_coverage, check_cc_coverage_multi


def _snap(last_price=100.0):
    return StockSnapshot(ticker='X', name='X', last_price=last_price,
                         rsi_14=50, sma_50=98, sma_200=95, volume_ratio=1.0)


class FakeMoomoo:
    """Only implements get_option_snapshots — returns a canned chain."""

    def __init__(self, contracts):
        self._contracts = contracts

    def get_option_snapshots(self, ticker, dte_min=7, dte_max=60):
        return self._contracts


class ExplodingMoomoo:
    """Fails the test if the chain is fetched — proves the gate fires first."""

    def get_option_snapshots(self, ticker, dte_min=7, dte_max=60):
        raise AssertionError("collar gate must reject BEFORE fetching chains")


def _cc(strike, bid, delta, dte=40, oi=1000, vol=200, iv=30):
    return OptionSnapshot(
        code=f'C{strike}', name='c', underlying='X', option_type='CALL',
        strike=strike, expiry='2099-01-01', dte=dte, area_type='AMERICAN',
        bid=bid, ask=bid + 0.2, open_interest=oi, volume=vol,
        implied_vol=iv, delta=delta)


# ── collar_check primitives ───────────────────────────────────────

def test_check_cc_coverage_exact_100_ok():
    assert check_cc_coverage(100, 1).ok


def test_check_cc_coverage_99_fails():
    rep = check_cc_coverage(99, 1)
    assert not rep.ok and '99' in rep.reason and '100' in rep.reason


def test_check_cc_coverage_multi_aggregates_per_ticker():
    positions = [
        {'ticker': 'V', 'contracts': 3},
        {'ticker': 'V', 'contracts': 2},
        {'ticker': 'AAPL', 'contracts': 1},
    ]
    # V needs 500, has 500 → ok; AAPL needs 100, has 99 → fail
    rep = check_cc_coverage_multi(positions, {'V': 500, 'AAPL': 99})
    assert not rep.ok and 'AAPL' in rep.reason


# ── _find_best_cc encumbrance netting ─────────────────────────────

def test_fully_encumbered_shares_return_none_before_chain_fetch():
    """500 shares, 5 open CCs → no 6th CC. The gate must fire before any
    option-chain fetch (the ExplodingMoomoo proves ordering)."""
    assert _find_best_cc(ExplodingMoomoo(), 'X', _snap(), shares=500, cost_basis=90,
                         yf_client=None, regime='NEUTRAL', regime_mult=1.0,
                         open_cc_contracts=5) is None


def test_partially_encumbered_shares_still_recommend():
    """500 shares, 4 open CCs → 100 free shares → a 5th CC is legitimate."""
    moomoo = FakeMoomoo([_cc(strike=110, bid=2.0, delta=0.25)])
    best = _find_best_cc(moomoo, 'X', _snap(), shares=500, cost_basis=90,
                         yf_client=None, regime='NEUTRAL', regime_mult=1.0,
                         open_cc_contracts=4)
    assert best is not None and best['strike'] == 110


def test_199_shares_1_open_cc_return_none():
    """199 shares with 1 open CC → 99 free → no room for another contract."""
    moomoo = FakeMoomoo([_cc(strike=110, bid=2.0, delta=0.25)])
    assert _find_best_cc(moomoo, 'X', _snap(), shares=199, cost_basis=90,
                         yf_client=None, regime='NEUTRAL', regime_mult=1.0,
                         open_cc_contracts=1) is None


def test_zero_open_contracts_unchanged_behavior():
    """Default open_cc_contracts=0 keeps the legacy path (regression safety)."""
    moomoo = FakeMoomoo([_cc(strike=105, bid=2.0, delta=0.30)])
    best = _find_best_cc(moomoo, 'X', _snap(), shares=200, cost_basis=90,
                         yf_client=None, regime='NEUTRAL', regime_mult=1.0)
    assert best is not None and best['strike'] == 105
