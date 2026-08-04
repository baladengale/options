"""Put Credit Spread scoring — unit tests for pure functions + end-to-end pairing.

Tests: put_spread_roc formula, credit_ratio gate, width gate, long-leg
liquidity gate, short-leg-strikes-above-long invariant, and the full
score_put_credit_spreads orchestrator on a synthetic PUT chain.

All functions tested are pure — no moomoo connection. Legs built via the same
_opt_snap template used by tests/test_screener_scoring.py.
"""

import os
import sys
from typing import Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.data.models import StockSnapshot, OptionSnapshot
from src.config import reload_config
from src.strategies.credit_spread import (
    put_spread_roc,
    _spread_penalty,
    _short_leg_eligible,
    _pick_long_leg,
    score_put_credit_spreads,
)


# ═══════════════════════════════════════════════════════════════
# HELPERS — mirror tests/test_screener_scoring.py builders
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
    }
    defaults.update(kwargs)
    return StockSnapshot(**{k: v for k, v in defaults.items() if v is not None})


def _put(strike: float, bid: float, ask: float, **kwargs) -> OptionSnapshot:
    """Build a PUT OptionSnapshot. Common overrides: dte, expiry, delta,
    implied_vol, open_interest, volume, bid_ask_spread_pct."""
    defaults = {
        'code': f'US.TEST260821P{int(strike * 1000):08d}',
        'name': '', 'underlying': 'US.TEST',
        'option_type': 'PUT', 'strike': strike,
        'expiry': '2026-08-21', 'dte': 42,
        'bid': bid, 'ask': ask,
        'delta': -0.25, 'implied_vol': 30.0,
        'open_interest': 1000, 'volume': 500,
        'bid_ask_spread_pct': 2.0, 'area_type': 'AMERICAN',
    }
    defaults.update(kwargs)
    # Rebuild code if strike changed after default
    defaults['code'] = f"US.TEST{defaults['expiry'].replace('-', '')}P{int(strike * 1000):08d}"
    return OptionSnapshot(**{k: v for k, v in defaults.items() if v is not None})


@pytest.fixture(autouse=True)
def _reload_cfg():
    """Each test starts from a fresh config load (config is cached)."""
    reload_config()
    yield


# ═══════════════════════════════════════════════════════════════
# put_spread_roc
# ═══════════════════════════════════════════════════════════════

def test_put_spread_roc_basic():
    """RoC = (net_credit / max_loss) * (365 / dte) * 100."""
    # width=5, credit=1.5 → max_loss=3.5
    roc = put_spread_roc(net_credit=1.5, width=5.0, dte=42)
    expected = (1.5 / 3.5) * (365.0 / 42.0) * 100
    assert abs(roc - expected) < 0.01


def test_put_spread_roc_credit_dominates():
    """A fat credit (close to width) → tiny max_loss → very high RoC."""
    roc_fat = put_spread_roc(net_credit=4.5, width=5.0, dte=42)   # max_loss=0.5
    roc_thin = put_spread_roc(net_credit=1.0, width=5.0, dte=42)  # max_loss=4.0
    assert roc_fat > roc_thin * 10


def test_put_spread_roc_zero_credit():
    assert put_spread_roc(net_credit=0.0, width=5.0, dte=42) == 0.0


def test_put_spread_roc_zero_width():
    assert put_spread_roc(net_credit=1.0, width=0.0, dte=42) == 0.0


def test_put_spread_roc_zero_dte():
    assert put_spread_roc(net_credit=1.5, width=5.0, dte=0) == 0.0


def test_put_spread_roc_credit_ge_width():
    """net_credit >= width → max_loss <= 0 → degenerate, return 0."""
    assert put_spread_roc(net_credit=5.0, width=5.0, dte=42) == 0.0
    assert put_spread_roc(net_credit=6.0, width=5.0, dte=42) == 0.0


# ═══════════════════════════════════════════════════════════════
# _spread_penalty — credit-ratio + width + long-leg quality
# ═══════════════════════════════════════════════════════════════

def test_spread_penalty_thin_credit_worse_than_fat():
    """A thin credit (ratio < 1/3) incurs a penalty; a fat credit gets a bonus."""
    short = _put(100, bid=2.0, ask=2.1, delta=-0.25)
    long_fat = _put(95, bid=0.20, ask=0.50)   # net_credit=1.50, width=5 → ratio 0.30 (thin)
    long_thin = _put(95, bid=1.80, ask=2.00)  # net_credit=0.00 → degenerate, skip in scorer
    long_mid = _put(95, bid=0.20, ask=0.30)   # net_credit=1.70, width=5 → ratio 0.34 (borderline)

    p_thin = _spread_penalty(short, long_fat, net_credit=1.50, width=5.0)
    p_mid = _spread_penalty(short, long_mid, net_credit=1.70, width=5.0)
    # Thin credit (ratio 0.30 < 1/3) penalized harder than borderline (0.34).
    assert p_thin > p_mid


def test_spread_penalty_width_out_of_band():
    """Width outside [min, max] incurs a penalty."""
    short = _put(100, bid=2.0, ask=2.1, delta=-0.25)
    long_ok = _put(95, bid=0.20, ask=0.40)    # width 5 (in band)
    long_wide = _put(85, bid=0.05, ask=0.10)  # width 15 (out of band, > 10 max)

    p_ok = _spread_penalty(short, long_ok, net_credit=1.60, width=5.0)
    p_wide = _spread_penalty(short, long_wide, net_credit=1.90, width=15.0)
    assert p_wide > p_ok


def test_spread_penalty_thin_long_liquidity():
    """A protective leg with no OI/volume is penalized."""
    short = _put(100, bid=2.0, ask=2.1, delta=-0.25)
    long_liquid = _put(95, bid=0.20, ask=0.40, open_interest=1000, volume=500)
    long_thin = _put(95, bid=0.20, ask=0.40, open_interest=5, volume=0)

    p_liq = _spread_penalty(short, long_liquid, net_credit=1.60, width=5.0)
    p_thin = _spread_penalty(short, long_thin, net_credit=1.60, width=5.0)
    assert p_thin > p_liq


# ═══════════════════════════════════════════════════════════════
# _short_leg_eligible — reuses shared gates, skips CSP RoC gate
# ═══════════════════════════════════════════════════════════════

def test_short_leg_eligible_passes_good_contract():
    snap = _stock_snap()
    leg = _put(100, bid=2.0, ask=2.1, delta=-0.25, implied_vol=30.0)
    ok, reason = _short_leg_eligible(leg, 'NEUTRAL', snap)
    assert ok, f"expected pass, got: {reason}"


def test_short_leg_eligible_rejects_no_liquidity():
    snap = _stock_snap()
    leg = _put(100, bid=0.0, ask=0.0, open_interest=0, volume=0)
    ok, reason = _short_leg_eligible(leg, 'NEUTRAL', snap)
    assert not ok and 'liquidity' in reason


def test_short_leg_eligible_rejects_out_of_range_delta():
    snap = _stock_snap()
    # delta -0.50 is above the 0.30 max for NEUTRAL CSP range
    leg = _put(100, bid=5.0, ask=5.1, delta=-0.50, implied_vol=30.0)
    ok, reason = _short_leg_eligible(leg, 'NEUTRAL', snap)
    assert not ok
    assert 'above max' in reason.lower() or 'δ' in reason.lower()


def test_short_leg_eligible_allows_low_premium():
    """A short leg with low premium (would fail the CSP RoC gate) is still
    eligible as a spread short leg — the RoC gate is applied on max_loss."""
    snap = _stock_snap()
    # bid=0.10 on a $100 strike = 0.10% → would fail CSP RoC gate badly
    leg = _put(100, bid=0.10, ask=0.15, delta=-0.25, implied_vol=30.0)
    ok, reason = _short_leg_eligible(leg, 'NEUTRAL', snap)
    assert ok, f"spread short leg must skip CSP RoC gate, got: {reason}"


# ═══════════════════════════════════════════════════════════════
# _pick_long_leg — invariant + greedy selection
# ═══════════════════════════════════════════════════════════════

def test_pick_long_leg_below_short():
    """Long strike must be strictly below the short strike."""
    short = _put(100, bid=2.0, ask=2.1)
    # Only candidate is ABOVE short — must return None
    above = _put(105, bid=0.5, ask=0.6)
    assert _pick_long_leg(short, [above]) is None


def test_pick_long_leg_maximizes_credit_ratio():
    """Among valid long legs, the one with the highest credit/width wins."""
    short = _put(100, bid=2.0, ask=2.1)
    # Two long legs, both width 5 (strike 95). The cheaper ask → higher credit.
    expensive = _put(95, bid=0.50, ask=1.20)   # net_credit = 0.80, ratio 0.16
    cheap = _put(95, bid=0.30, ask=0.40)       # net_credit = 1.60, ratio 0.32
    chosen = _pick_long_leg(short, [expensive, cheap])
    assert chosen is not None
    assert chosen.strike == 95
    assert chosen.ask == pytest.approx(0.40)


def test_pick_long_leg_respects_width_band():
    """A long leg outside the width band is rejected."""
    short = _put(100, bid=2.0, ask=2.1)
    too_far = _put(80, bid=0.05, ask=0.10)   # width 20 > max 10
    in_band = _put(95, bid=0.30, ask=0.40)   # width 5
    chosen = _pick_long_leg(short, [too_far, in_band])
    assert chosen is not None and chosen.strike == 95


def test_pick_long_leg_requires_liquidity():
    """A long leg with no OI/volume is skipped."""
    short = _put(100, bid=2.0, ask=2.1)
    thin = _put(95, bid=0.30, ask=0.40, open_interest=5, volume=0)
    liquid = _put(90, bid=0.10, ask=0.20, open_interest=1000, volume=500)
    chosen = _pick_long_leg(short, [thin, liquid])
    assert chosen is not None and chosen.strike == 90


# ═══════════════════════════════════════════════════════════════
# score_put_credit_spreads — end-to-end orchestrator
# ═══════════════════════════════════════════════════════════════

def _build_chain() -> list:
    """A synthetic PUT chain around a $100 stock, 42 DTE, elevated IV.

    The short leg that passes the NEUTRAL CSP delta range [0.20, 0.30] is the
    97.5 strike (Δ -0.25). At 45% IV its bid is fat enough that pairing it 5
    wide with the 92.5 put clears the 1/3 credit-ratio floor
    (net_credit 1.70 / width 5.0 = 0.34). The 100 strike is ATM (Δ -0.50, out
    of the short-leg delta range) — present to prove it's skipped, not used.
    """
    return [
        _put(100, bid=4.80, ask=4.90, delta=-0.50, implied_vol=45.0),  # ATM, not a short-leg
        _put(97.5, bid=2.00, ask=2.10, delta=-0.25, implied_vol=45.0),  # eligible short
        _put(95, bid=0.90, ask=1.00, delta=-0.18),                      # 97.5/95 → ratio 0.22 (fails)
        _put(92.5, bid=0.20, ask=0.30, delta=-0.14),                    # 97.5/92.5 → ratio 0.34 (passes)
        _put(90, bid=0.08, ask=0.15, delta=-0.10),                      # 92.5/90 → too thin
        _put(85, bid=0.02, ask=0.06, delta=-0.05),
    ]


def test_score_spreads_returns_one_best_per_ticker():
    snap = _stock_snap()
    cands = score_put_credit_spreads(
        _build_chain(), snap, ticker='TEST', ticker_score=3.0,
        regime='NEUTRAL', net_liq=100000, cash=50000,
    )
    assert len(cands) >= 1
    assert all(c.strategy == 'PS' for c in cands)
    assert len(cands) <= 1  # default max_per_ticker = 1


def test_score_spreads_populates_spread_fields():
    snap = _stock_snap()
    cands = score_put_credit_spreads(
        _build_chain(), snap, ticker='TEST', ticker_score=3.0,
        regime='NEUTRAL', net_liq=100000, cash=50000,
    )
    assert cands, "expected at least one spread candidate"
    c = cands[0]
    assert c.long_strike is not None and c.long_strike < c.strike
    assert c.spread_width == pytest.approx(c.strike - c.long_strike)
    assert c.net_credit is not None and c.net_credit > 0
    assert c.max_loss == pytest.approx(c.spread_width - c.net_credit)
    # capital_required is max_loss × 100, NOT the full strike
    assert c.capital_required == pytest.approx(c.max_loss * 100, rel=0.01)
    assert c.capital_required < c.strike * 100  # strictly less than a CSP


def test_score_spreads_roc_on_max_loss():
    """Reported RoC must be computed on max_loss, not the short strike."""
    snap = _stock_snap()
    cands = score_put_credit_spreads(
        _build_chain(), snap, ticker='TEST', ticker_score=3.0,
        regime='NEUTRAL', net_liq=100000, cash=50000,
    )
    assert cands
    c = cands[0]
    expected_roc = put_spread_roc(c.net_credit, c.spread_width, c.dte)
    assert c.annualized_roc_pct == pytest.approx(expected_roc, abs=0.1)


def test_score_spreads_credit_ratio_floor():
    """No candidate should have net_credit/width below credit_ratio_min (1/3)."""
    snap = _stock_snap()
    cands = score_put_credit_spreads(
        _build_chain(), snap, ticker='TEST', ticker_score=3.0,
        regime='NEUTRAL', net_liq=100000, cash=50000,
    )
    for c in cands:
        assert c.net_credit / c.spread_width >= 0.333 - 1e-9


def test_score_spreads_cash_backed_gate():
    """When cash is too small to cover max_loss × 100, no candidate emerges."""
    snap = _stock_snap()
    # cash=1 → cannot back any spread's max_loss
    cands = score_put_credit_spreads(
        _build_chain(), snap, ticker='TEST', ticker_score=3.0,
        regime='NEUTRAL', net_liq=100000, cash=1.0,
    )
    assert cands == []


def test_score_spreads_disabled_returns_empty():
    """When credit_spread.enabled is False, return []."""
    snap = _stock_snap()
    cfg = reload_config()
    cfg._data['credit_spread']['enabled'] = False
    cands = score_put_credit_spreads(
        _build_chain(), snap, ticker='TEST', ticker_score=3.0,
        regime='NEUTRAL', net_liq=100000, cash=50000, cfg=cfg,
    )
    assert cands == []


def test_score_spreads_gex_negative_skips():
    """Dealer short gamma (gex_negative) suppresses PCS, same as CSP."""
    snap = _stock_snap()
    cands = score_put_credit_spreads(
        _build_chain(), snap, ticker='TEST', ticker_score=3.0,
        regime='NEUTRAL', net_liq=100000, cash=50000, gex_negative=True,
    )
    assert cands == []


def test_score_spreads_short_leg_delta_in_regime_range():
    """The chosen short-leg delta must fall in the CSP regime delta range."""
    snap = _stock_snap()
    cands = score_put_credit_spreads(
        _build_chain(), snap, ticker='TEST', ticker_score=3.0,
        regime='NEUTRAL', net_liq=100000, cash=50000,
    )
    assert cands
    # NEUTRAL CSP delta range is [0.20, 0.30]
    assert 0.20 <= cands[0].delta <= 0.30


def test_score_spreads_empty_chain():
    snap = _stock_snap()
    assert score_put_credit_spreads(
        [], snap, ticker='TEST', ticker_score=3.0, regime='NEUTRAL') == []


def test_score_spreads_ignores_calls():
    """Non-PUT contracts in the list must be ignored."""
    snap = _stock_snap()
    call = OptionSnapshot(
        code='US.TEST260821C100000', name='', underlying='US.TEST',
        option_type='CALL', strike=100.0, expiry='2026-08-21', dte=42,
        bid=2.0, ask=2.1, delta=0.25, implied_vol=30.0,
        open_interest=1000, volume=500, bid_ask_spread_pct=2.0,
        area_type='AMERICAN',
    )
    cands = score_put_credit_spreads(
        [call], snap, ticker='TEST', ticker_score=3.0, regime='NEUTRAL',
        net_liq=100000, cash=50000,
    )
    assert cands == []
