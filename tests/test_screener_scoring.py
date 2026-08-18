"""
Screener scoring engine — unit tests for pure functions.

Tests: _csp_roc, _trend_composite, _score_technical, _score_macro,
_score_external, _contract_penalty, _reason, _score_stars.

All functions tested are pure — no moomoo connection needed.
"""

import pytest
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.data.models import StockSnapshot, OptionSnapshot

# Import scoring functions from screener
from scripts.screener import (
    _csp_roc, _reason, _score_stars,
    _score_macro, _score_external, _score_technical, _score_fundamental,
    _score_options_eco, _trend_composite, _contract_penalty,
    _compute_ticker_score,
)


# ═══════════════════════════════════════════════════════════════
# HELPER: build minimal StockSnapshot
# ═══════════════════════════════════════════════════════════════

def _stock_snap(**kwargs) -> StockSnapshot:
    defaults = {
        'ticker': 'TEST', 'name': 'Test', 'last_price': 100.0,
        'rsi_14': 50.0, 'sma_50': 95.0, 'sma_200': 90.0,
        'adx_14': 25.0, 'volume_ratio': 1.0,
        'bid_ask_spread_pct': 0.3, 'beta_vs_spy': 1.0,
        'market_cap': 500e9, 'pe_ttm': 20.0,
        'dividend_yield_ttm': 1.5, 'eps_ttm': 5.0,
        'hv_30d': 0.25, 'macd': 2.0, 'macd_signal': 1.5,
        'bollinger_mid': 98.0, 'bollinger_upper': 105.0, 'bollinger_lower': 91.0,
    }
    defaults.update(kwargs)
    # Remove None values for Optional fields
    return StockSnapshot(**{k: v for k, v in defaults.items() if v is not None})


def _opt_snap(**kwargs) -> OptionSnapshot:
    defaults = {
        'code': 'US.TEST260821C100000', 'name': '', 'underlying': 'US.TEST',
        'option_type': 'PUT', 'strike': 100.0, 'expiry': '2026-08-21',
        'dte': 42, 'bid': 5.00, 'ask': 5.20,
        'delta': -0.25, 'implied_vol': 30.0,
        'open_interest': 1000, 'volume': 500,
        'bid_ask_spread_pct': 2.0,
        'area_type': 'AMERICAN',
    }
    defaults.update(kwargs)
    return OptionSnapshot(**{k: v for k, v in defaults.items() if v is not None})


# ═══════════════════════════════════════════════════════════════
# CSP RoC
# ═══════════════════════════════════════════════════════════════

def test_csp_roc_basic():
    """CSP RoC = (bid / strike) * (365 / dte) * 100."""
    roc = _csp_roc(bid=5.0, strike=100.0, dte=42)
    expected = (5.0 / 100.0) * (365.0 / 42.0) * 100
    assert abs(roc - expected) < 0.01


def test_csp_roc_high_yield():
    """High premium relative to strike = high RoC."""
    roc = _csp_roc(bid=10.0, strike=50.0, dte=30)
    assert roc > 100  # > 100% annualized


def test_csp_roc_zero_bid():
    """Zero bid = zero RoC."""
    assert _csp_roc(bid=0, strike=100, dte=30) == 0.0


def test_csp_roc_zero_dte():
    """Zero DTE = zero RoC (avoid div by zero)."""
    assert _csp_roc(bid=5.0, strike=100.0, dte=0) == 0.0


# ═══════════════════════════════════════════════════════════════
# REGIME
# ═══════════════════════════════════════════════════════════════
# TREND COMPOSITE
# ═══════════════════════════════════════════════════════════════

def test_trend_composite_bullish():
    """Price > SMA50 > SMA200 = strong uptrend."""
    snap = _stock_snap(last_price=110, sma_50=100, sma_200=90, rsi_14=55)
    score = _trend_composite(snap)
    assert score > 65  # strong signal


def test_trend_composite_bearish():
    """Price < SMA50 < SMA200 = downtrend."""
    snap = _stock_snap(last_price=80, sma_50=100, sma_200=110, rsi_14=40)
    score = _trend_composite(snap)
    assert score < 40  # weak signal


def test_trend_composite_missing_sma_is_none():
    """No SMA anchors → None, never a silent neutral 50-60 that could be
    misread as a confirmed trend by the profit-target gates."""
    snap = _stock_snap(sma_50=None, sma_200=None)
    assert _trend_composite(snap) is None


# ═══════════════════════════════════════════════════════════════
# SCORING SUB-COMPONENTS
# ═══════════════════════════════════════════════════════════════

def test_score_technical_neutral():
    """Neutral RSI + aligned SMAs = low score (good)."""
    snap = _stock_snap(rsi_14=50, last_price=105, sma_50=100, sma_200=95, adx_14=25)
    score = _score_technical(snap, _trend_composite(snap))
    assert 1.0 <= score <= 3.0  # very good — neutral RSI + uptrend


def test_score_technical_extreme_rsi():
    """Extreme RSI scores worse than neutral RSI."""
    snap_neutral = _stock_snap(rsi_14=50, last_price=100, sma_50=95, sma_200=90)
    snap_extreme = _stock_snap(rsi_14=85, last_price=100, sma_50=95, sma_200=90)
    score_neutral = _score_technical(snap_neutral, _trend_composite(snap_neutral))
    score_extreme = _score_technical(snap_extreme, _trend_composite(snap_extreme))
    assert score_extreme > score_neutral  # extreme RSI is higher (worse)


def test_score_options_eco_good():
    """Tight spread + good IV rank + large cap + low beta = excellent."""
    snap = _stock_snap(bid_ask_spread_pct=0.2, beta_vs_spy=0.8, market_cap=1e12)
    score = _score_options_eco(snap, iv_rank=50.0)
    assert score < 3.0


def test_score_options_eco_poor():
    """Wide spread + low cap + high beta = poor."""
    snap = _stock_snap(bid_ask_spread_pct=6.0, beta_vs_spy=2.5, market_cap=5e9)
    score = _score_options_eco(snap, iv_rank=10.0)
    assert score > 6.0


def test_score_fundamental_strong():
    """Good P/E + dividend + EPS = strong."""
    snap = _stock_snap(pe_ttm=18, dividend_yield_ttm=2.5, eps_ttm=8.0)
    score = _score_fundamental(snap)
    assert score < 3.0


def test_score_fundamental_weak():
    """High P/E + no dividend + negative EPS = weak."""
    snap = _stock_snap(pe_ttm=80, dividend_yield_ttm=0, eps_ttm=-2.0)
    score = _score_fundamental(snap)
    assert score > 6.0


def test_score_macro_bullish():
    """Bullish regime = favorable for CSP/CC."""
    score = _score_macro('BULLISH', 1.0, False)
    assert score < 3.0


def test_score_macro_bearish():
    """Bearish regime = unfavorable."""
    score = _score_macro('BEARISH', 0.0, False)
    assert score > 6.0


def test_score_macro_blackout():
    """Earnings blackout increases risk score."""
    score_normal = _score_macro('NEUTRAL', 0.75, False)
    score_blackout = _score_macro('NEUTRAL', 0.75, True)
    assert score_blackout > score_normal


def test_score_external_bullish_analyst():
    """Strong buy + upside + bullish news = low score."""
    score = _score_external('STRONG_BUY', False, 'NEUTRAL', 20.0, 80)
    assert score < 2.5


def test_score_external_bearish():
    """Sell rating + blackout + bearish news = high score."""
    score = _score_external('SELL', True, 'SELLING', -15.0, 20)
    assert score > 7.0


# ═══════════════════════════════════════════════════════════════
# CONTRACT PENALTY
# ═══════════════════════════════════════════════════════════════

def test_contract_penalty_optimal_dte():
    """30-45 DTE = sweet spot, gets bonus (negative penalty)."""
    opt = _opt_snap(dte=35, open_interest=2000, volume=800)
    penalty = _contract_penalty(opt, delta=0.25, roc=15.0)
    assert penalty < 0  # bonus for optimal DTE


def test_contract_penalty_short_dte():
    """DTE < 7 = hard block."""
    opt = _opt_snap(dte=5)
    penalty = _contract_penalty(opt, delta=0.25, roc=15.0)
    assert penalty >= 99  # hard block


def test_contract_penalty_low_oi():
    """Low OI < 100 = penalty."""
    opt = _opt_snap(open_interest=50, volume=500, dte=35)
    penalty = _contract_penalty(opt, delta=0.25, roc=15.0)
    assert penalty > 0


def test_contract_penalty_high_roc():
    """High RoC > 24% = bonus."""
    opt = _opt_snap(dte=35, open_interest=2000, volume=800)
    penalty_high = _contract_penalty(opt, delta=0.25, roc=30.0)
    penalty_low = _contract_penalty(opt, delta=0.25, roc=10.0)
    assert penalty_high < penalty_low  # higher RoC = better (lower penalty)


def test_contract_penalty_wide_spread():
    """Bid/ask spread > 5% = penalty."""
    opt = _opt_snap(bid_ask_spread_pct=8.0, dte=35, open_interest=2000)
    penalty = _contract_penalty(opt, delta=0.25, roc=15.0)
    assert penalty > 1.0


# ═══════════════════════════════════════════════════════════════
# TICKER COMPOSITE SCORE
# ═══════════════════════════════════════════════════════════════

def test_compute_ticker_score_range():
    """Ticker score should always be 1-10."""
    snap = _stock_snap()
    score = _compute_ticker_score(
        snap=snap,
        trend_composite=60.0,
        analyst_consensus='HOLD',
        earnings_blackout=False,
        insider_sentiment='NEUTRAL',
        target_upside=5.0,
        news_score=50,
        regime='NEUTRAL',
        regime_mult=0.75,
        iv_rank=50.0,
    )
    assert 1.0 <= score <= 10.0


def test_compute_ticker_score_bullish_vs_bearish():
    """Bullish ticker scores better than bearish."""
    bull_snap = _stock_snap(rsi_14=50, last_price=105, sma_50=100, sma_200=95)
    bear_snap = _stock_snap(rsi_14=80, last_price=80, sma_50=100, sma_200=110, pe_ttm=100, eps_ttm=-3)

    bull_score = _compute_ticker_score(
        bull_snap, 75.0, 'STRONG_BUY', False, 'NEUTRAL', 15.0, 70, 'BULLISH', 1.0, 50.0)
    bear_score = _compute_ticker_score(
        bear_snap, 25.0, 'SELL', True, 'SELLING', -20.0, 20, 'BEARISH', 0.0, 10.0)

    assert bull_score < bear_score  # lower = better


# ═══════════════════════════════════════════════════════════════
# REASON / STARS (display helpers)
# ═══════════════════════════════════════════════════════════════

def test_reason_excellent():
    assert 'Excellent' in _reason(5.0, 1.5, 'CSP')


def test_reason_marginal():
    assert 'Marginal' in _reason(5.0, 8.0, 'CC')


def test_score_stars():
    assert '⭐1' in _score_stars(1.5)
    assert '⭐2' in _score_stars(2.5)
    assert '⭐3' in _score_stars(3.5)
    assert '4' in _score_stars(4.5)
    assert '8+' in _score_stars(8.5)
