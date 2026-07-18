"""Parity tests for src/scoring/screener_score.py — the moved scoring engine.

The bulk of scoring coverage lives in test_screener_scoring.py (which imports via
scripts.screener). This file pins the src module directly so the engine is tested
independently of the re-export shim, and asserts the shim points at the SAME
function objects (not copies).
"""

import pytest

from src.data.models import StockSnapshot, OptionSnapshot
from src.scoring import screener_score as ss


def test_shim_re_exports_same_objects():
    """scripts.screener must re-export the exact src function objects (identity)."""
    import scripts.screener as sc
    for name in [
        '_compute_ticker_score', '_contract_penalty', '_trend_composite',
        '_score_technical', '_score_options_eco', '_score_fundamental',
        '_score_external', '_score_macro', '_csp_roc', '_score_stars', '_reason',
    ]:
        assert getattr(sc, name) is getattr(ss, name), (
            f"{name} in scripts.screener is not the src object — shim is wrong")


def test_csp_roc_golden():
    assert ss._csp_roc(5.0, 100.0, 42) == pytest.approx(43.452, abs=0.01)
    assert ss._csp_roc(0, 100, 42) == 0.0
    assert ss._csp_roc(5.0, 0, 42) == 0.0


def test_score_stars_thresholds():
    # Bands are <= inclusive on the upper bound: 2.0 lands in the ⭐1 band.
    assert ss._score_stars(1.5) == '⭐1'
    assert ss._score_stars(2.0) == '⭐1'
    assert ss._score_stars(2.5) == '⭐2'
    assert ss._score_stars(3.5) == '⭐3'
    assert ss._score_stars(9.0) == ' 8+'


def test_reason_bands():
    assert 'Excellent' in ss._reason(5.0, 1.5, 'CSP')
    assert 'Strong' in ss._reason(5.0, 3.0, 'CSP')
    assert 'Marginal' in ss._reason(5.0, 8.0, 'CC')


def test_regime_multiplier():
    assert ss._regime_multiplier('BULLISH') == 0.85
    assert ss._regime_multiplier('NEUTRAL') == 1.0
    assert ss._regime_multiplier('UNKNOWN') == 1.0


def test_compute_ticker_score_returns_in_range():
    """A neutral snapshot across all dimensions should land mid-scale (1-10)."""
    snap = StockSnapshot(ticker='X', name='X', last_price=100,
                         sma_50=98, sma_200=95, rsi_14=50, adx_14=30,
                         volume_ratio=1.0, bid_ask_spread_pct=1.0,
                         market_cap=200e9, beta_vs_spy=1.0,
                         pe_ttm=20, dividend_yield_ttm=2.5, eps_ttm=5.0)
    score = ss._compute_ticker_score(
        snap, trend_composite=60.0, analyst_consensus='HOLD',
        earnings_blackout=False, insider_sentiment='NEUTRAL',
        target_upside=None, news_score=50.0,
        regime='NEUTRAL', regime_mult=1.0, iv_rank=50.0)
    assert 1.0 <= score <= 10.0


def test_contract_penalty_dte_hard_block():
    """<7 DTE must attract the +99 hard-block penalty from config (and dominate)."""
    c = OptionSnapshot(code='X', name='X', underlying='X', option_type='PUT',
                       strike=100, expiry='2099-01-01', dte=5, area_type='AMERICAN',
                       bid_ask_spread_pct=1.0, open_interest=2000, volume=200,
                       implied_vol=30)
    # roc=10 (no bonus), delta=0.25 (no low-delta penalty) → only hard_block applies.
    p = ss._contract_penalty(c, delta=0.25, roc=10.0)
    assert p == 99
    # Even with a high-RoC bonus offsetting it, a <7 DTE contract stays blocked.
    p2 = ss._contract_penalty(c, delta=0.25, roc=26.0)
    assert p2 >= 90


def test_contract_penalty_optimal_dte_bonus():
    """30-45 DTE sweet spot should attract the optimal bonus (negative penalty)."""
    c = OptionSnapshot(code='X', name='X', underlying='X', option_type='PUT',
                       strike=100, expiry='2099-01-01', dte=40, area_type='AMERICAN',
                       bid_ask_spread_pct=0.3, open_interest=5000, volume=500,
                       implied_vol=40)
    p = ss._contract_penalty(c, delta=0.25, roc=26.0)
    # high_roc_bonus + high_iv_bonus + optimal bonus → strongly negative
    assert p < 0
