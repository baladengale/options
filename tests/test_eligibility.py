"""Tests for the daily Wheel eligibility check (src/data/do_not_wheel_list.py).

The watchlist is the master list; ``is_wheel_eligible`` is a read-only filter
that auto-skips clear loss-makers using moomoo snapshot data only. It must NOT
block profitable high-growth names (AMD/PLTR/TSLA) just for having high P/E.
"""

from types import SimpleNamespace

import pytest

from src.data.do_not_wheel_list import is_wheel_eligible


def _snap(net_profit=1e9, eps_ttm=2.5, pe_ratio=30.0, ticker='TEST'):
    """A moomoo-style snapshot duck-typed to the fields is_wheel_eligible reads."""
    return SimpleNamespace(net_profit=net_profit, eps_ttm=eps_ttm,
                           pe_ratio=pe_ratio, ticker=ticker)


# ── Eligible (the common case) ────────────────────────────────────

def test_healthy_company_eligible():
    ok, _ = is_wheel_eligible(_snap(net_profit=12e9, eps_ttm=8.5, pe_ratio=30.0), 'V')
    assert ok is True


def test_high_pe_growth_stock_eligible():
    """AMD/PLTR/TSLA: high P/E but profitable → eligible (the fix)."""
    ok, _ = is_wheel_eligible(_snap(net_profit=1e9, eps_ttm=2.5, pe_ratio=184.0), 'AMD')
    assert ok is True


def test_zero_profit_no_eps_still_eligible_when_no_loss():
    """A company breaking even (net profit 0, eps 0) is not a loss-maker."""
    ok, _ = is_wheel_eligible(_snap(net_profit=0, eps_ttm=0, pe_ratio=0), 'FLAT')
    assert ok is True


# ── Not eligible (clear loss-makers) ──────────────────────────────

def test_unprofitable_loss_maker_blocked():
    """net_profit < 0 AND eps < 0 → blocked (e.g. BE)."""
    ok, reason = is_wheel_eligible(_snap(net_profit=-5e8, eps_ttm=-5.4, pe_ratio=-12.0), 'BE')
    assert ok is False
    assert 'unprofitable' in reason.lower()


def test_negative_pe_blocked():
    """Negative P/E (company losing money) → blocked."""
    ok, reason = is_wheel_eligible(_snap(net_profit=-1e6, eps_ttm=-0.2, pe_ratio=-8.0), 'LOSS')
    assert ok is False
    assert 'negative' in reason.lower() or 'unprofitable' in reason.lower()


# ── Not a false positive: one-off charges ─────────────────────────

def test_one_off_loss_net_profit_negative_eps_positive_eligible():
    """net_profit < 0 but eps still positive (one-off charge) → eligible.
    A single bad quarter is not a broken business."""
    ok, _ = is_wheel_eligible(_snap(net_profit=-1e6, eps_ttm=0.5, pe_ratio=40.0), 'CHRG')
    assert ok is True


def test_positive_profit_negative_eps_eligible():
    """net_profit > 0 but eps < 0 (accounting anomaly, dilution) → eligible."""
    ok, _ = is_wheel_eligible(_snap(net_profit=5e6, eps_ttm=-0.1, pe_ratio=50.0), 'DIL')
    assert ok is True


# ── Config toggles ────────────────────────────────────────────────

def test_unprofitable_block_disabled(monkeypatch):
    """With unprofitable_block=false, a loss-maker becomes eligible."""
    from src.config import get_config
    monkeypatch.setattr(type(get_config()), 'thesis_validation',
                        lambda self, k, d=None: False if k == 'unprofitable_block' else d)
    ok, _ = is_wheel_eligible(_snap(net_profit=-5e8, eps_ttm=-5.4, pe_ratio=-12.0), 'BE')
    # unprofitable disabled, but pe_negative_critical still on (pe -12 < 0)
    # → still blocked by the P/E rule. Disable that too to confirm toggle.
    assert ok is False


def test_all_blocks_disabled(monkeypatch):
    """With both profitability + negative-PE checks off, a loss-maker is eligible."""
    from src.config import get_config
    fake = lambda self, k, d=None: False if k in ('unprofitable_block', 'pe_negative_critical') else d
    monkeypatch.setattr(type(get_config()), 'thesis_validation', fake)
    ok, _ = is_wheel_eligible(_snap(net_profit=-5e8, eps_ttm=-5.4, pe_ratio=-12.0), 'BE')
    assert ok is True


# ── Robustness ────────────────────────────────────────────────────

def test_none_snapshot_eligible():
    """No snapshot → don't block (can't evaluate; let the watchlist decide)."""
    ok, _ = is_wheel_eligible(None, 'UNK')
    assert ok is True


def test_missing_fields_eligible():
    """Snapshot without profitability fields → eligible (no data to block on)."""
    ok, _ = is_wheel_eligible(SimpleNamespace(ticker='X'), 'X')
    assert ok is True


def test_nan_fields_eligible():
    """NaN fields (moomoo sometimes returns NaN) → treated as missing → eligible."""
    ok, _ = is_wheel_eligible(_snap(net_profit=float('nan'), eps_ttm=float('nan'),
                                   pe_ratio=float('nan')), 'NAN')
    assert ok is True
