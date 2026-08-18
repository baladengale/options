"""Tests for src/scoring/holding_score.py — position decision engine."""

from datetime import date

import pytest

from src.data.models import StockSnapshot, OptionSnapshot
from src.scoring.holding_score import (
    _score_holding, _score_option, _find_best_cc, _OptionCurrent,
)


TODAY = date.today()


class _NoDataMoomoo:
    """Stand-in for MoomooClient so the heavy-loss thesis path stays offline.

    `_score_option`'s catch-all instantiates MoomooClient() and asks for a
    snapshot; raising here routes it to the deterministic UNDERWATER fallback
    (the `except` branch) instead of blocking on the network when OpenD isn't
    running.
    """
    def get_stock_snapshot(self, ticker):
        raise RuntimeError("offline test — no moomoo data")

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _stub_moomoo(monkeypatch):
    """Keep every test in this module offline and deterministic.

    The heavy-loss catch-all in _score_option does a local
    `from src.data.moomoo_client import MoomooClient`, so we patch the symbol at
    its source. The stub's get_stock_snapshot raises, routing the thesis check
    to its deterministic UNDERWATER fallback instead of blocking on the network.
    """
    import src.data.moomoo_client as mc
    monkeypatch.setattr(mc, 'MoomooClient', lambda *a, **kw: _NoDataMoomoo())


def _opt(dte=40, delta=-0.20, bid=5.0):
    """Build a lightweight _OptionCurrent with the fields _score_option reads."""
    return _OptionCurrent({
        'bid_price': bid, 'ask_price': bid + 0.5, 'last_price': bid,
        'option_delta': delta, 'option_gamma': 0.0,
        'option_implied_volatility': 30, 'option_open_interest': 1000,
        'volume': 200, 'option_expiry_date_distance': dte,
        'option_strike_price': 100, 'option_type': 'PUT',
    })


def _pos(ticker='AVGO', type='PUT', strike=350.0, expiry='2099-01-01', qty=-1, cost=11.2):
    return {'ticker': ticker, 'type': type, 'strike': strike, 'expiry': expiry,
            'qty': qty, 'cost': cost, 'pl': 0.0, 'pl_pct': 0.0}


# ── _score_option decision matrix ────────────────────────────────

def test_option_70pct_profit_close():
    s, dec, _pd = _score_option(_pos(), _opt(dte=40), profit_captured=75.0, pl=700,
                           today=TODAY, yf_client=None)
    # With no trend context, 75% exceeds the 50% base target → engine says CLOSE.
    # But OTM gate (spec §6): |Δ|=0.20 < 0.30, DTE=40 > 21 → HOLD override.
    assert 'HOLD' in dec or 'OTM GATE' in dec
    assert 'CLOSE' not in dec


def test_option_50pct_profit_close():
    s, dec, _pd = _score_option(_pos(), _opt(dte=40), profit_captured=55.0, pl=500,
                           today=TODAY, yf_client=None)
    # OTM gate (spec §6): |Δ|=0.20 < 0.30, DTE=40 > 21 → HOLD, not CLOSE
    assert 'HOLD' in dec or 'OTM GATE' in dec


def test_option_csp_delta_stop():
    # CSP, |delta| >= 0.60 → stop / assignment decision
    s, dec, _pd = _score_option(_pos(), _opt(dte=40, delta=-0.65), profit_captured=10.0,
                           pl=0, today=TODAY, yf_client=None)
    assert 'roll' in dec or 'assignment' in dec or 'STOP' in dec or 'exit' in dec
    assert s >= 7.0


def test_option_far_dte_3x_stop_tier():
    # Underwater 3× loss, >30 DTE → 3× STOP TIER
    s, dec, _pd = _score_option(_pos(), _opt(dte=40, delta=-0.20), profit_captured=-300.0,
                           pl=-3000, today=TODAY, yf_client=None)
    assert 'STOP TIER' in dec
    assert s >= 7.0


def test_option_near_dte_15x_stop_gamma():
    # Underwater 1.5× loss, <=21 DTE → 1.5× STOP TIER (gamma)
    s, dec, _pd = _score_option(_pos(), _opt(dte=10, delta=-0.20), profit_captured=-150.0,
                           pl=-1500, today=TODAY, yf_client=None)
    assert '1.5× STOP TIER' in dec
    assert s >= 7.0


def test_option_heavy_loss_catchall():
    # The absolute catch-all is now premium-tiered: a $1,120 credit (cost=11.2 ×
    # qty=1 × 100) lands in the $500–$2000 band whose floor is $2,000. So pl
    # must clear −$2,000 (not the legacy −$1,000) to trip it.
    s, dec, _pd = _score_option(_pos(), _opt(dte=40, delta=-0.20), profit_captured=-5.0,
                           pl=-2500, today=TODAY, yf_client=None)
    # When MoomooClient is available: thesis-aware path ("Position Down — Thesis intact")
    # When unavailable: fallback ("UNDERWATER — Monitor thesis, not price")
    assert 'UNDERWATER' in dec or 'Position Down' in dec or 'Thesis' in dec


def test_option_underwater_suppresses_trend_extended_label():
    # Regression (2026-08-17 digest): AVGO CSP at -58% showed
    # "HOLD (-58% < 85% target, trend-extended)" — a loss compared against a
    # profit target. The tag describes the target, not the position; on an
    # underwater row the decision must show loss posture instead.
    from src.analysis.profit_management import TrendContext
    ctx = TrendContext(trend_composite=75, sentiment_direction='BULLISH', iv_rank=40)
    s, dec, pd = _score_option(_pos(), _opt(dte=32, delta=-0.44), profit_captured=-58.0,
                          pl=-700, today=TODAY, yf_client=None, trend_ctx=ctx)
    assert pd.extended_by_trend is True      # target genuinely extended to 85%…
    assert 'trend-extended' not in dec       # …but the label must not leak onto a loser
    assert 'underwater' in dec
    assert '0.58' in dec                     # ×-multiple of premium lost
    assert '85%' not in dec                  # no loss-vs-target comparison


def test_option_small_profit_keeps_trend_extended_label():
    # Profitable but below the extended target (GOOG CSP at +3.6%) — here the
    # tag is the decision-relevant fact: don't close at 50%, ride to 85%.
    from src.analysis.profit_management import TrendContext
    ctx = TrendContext(trend_composite=75, sentiment_direction='BULLISH', iv_rank=40)
    s, dec, pd = _score_option(_pos(), _opt(dte=32, delta=-0.29), profit_captured=3.6,
                          pl=35, today=TODAY, yf_client=None, trend_ctx=ctx)
    assert pd.extended_by_trend is True
    assert 'trend-extended' in dec
    assert '85%' in dec


def test_option_underwater_stop_alert_still_overrides():
    # The underwater HOLD posture must never mask a firing stop tier: a 2.5×
    # loss at 32 DTE crosses the 2.0× far-alert → STOP ALERT wins the label.
    from src.analysis.profit_management import TrendContext
    ctx = TrendContext(trend_composite=75, sentiment_direction='BULLISH', iv_rank=40)
    s, dec, pd = _score_option(_pos(), _opt(dte=32, delta=-0.30), profit_captured=-250.0,
                          pl=-2800, today=TODAY, yf_client=None, trend_ctx=ctx)
    assert 'STOP' in dec
    assert 'underwater' not in dec


def test_option_normal_hold():
    # Healthy CSP, mid-DTE, small profit → HOLD-ish, score near neutral
    s, dec, _pd = _score_option(_pos(), _opt(dte=40, delta=-0.20), profit_captured=10.0,
                           pl=100, today=TODAY, yf_client=None)
    assert 1.0 <= s <= 10.0
    assert 'HOLD' in dec or 'monitor' in dec.lower() or 'captured' in dec.lower() or 'Management Point' in dec


# ── trend tag (strategy-aware, always-on for non-action decisions) ──

def test_trend_label_strategy_aware():
    from src.scoring.holding_score import _trend_label
    assert _trend_label('CSP', 75) == 'trend 75▲'
    assert _trend_label('CC', 75) == 'trend 75▲⚠️'     # uptrend hurts short calls
    assert _trend_label('CSP', 30) == 'trend 30▼⚠️'    # downtrend hurts short puts
    assert _trend_label('CC', 30) == 'trend 30▼'
    assert _trend_label('CSP', 50) == 'trend 50→'
    assert _trend_label('CC', None) == 'trend —'


def test_option_underwater_gets_strategy_aware_trend_tag():
    # Underwater HOLD rows carry the trend tag — the same signal that explains
    # WHY the loser is underwater. CSP + uptrend = tailwind, no warning.
    from src.analysis.profit_management import TrendContext
    ctx = TrendContext(trend_composite=72, sentiment_direction='BULLISH', iv_rank=40)
    s, dec, _pd = _score_option(_pos(), _opt(dte=32, delta=-0.30), profit_captured=-20.0,
                          pl=-200, today=TODAY, yf_client=None, trend_ctx=ctx)
    assert 'underwater' in dec
    assert 'trend 72▲' in dec


def test_option_cc_uptrend_tag_flags_headwind():
    # For a short call the SAME uptrend is a headwind — the tag must say so.
    from src.analysis.profit_management import TrendContext
    ctx = TrendContext(trend_composite=72, sentiment_direction='BULLISH', iv_rank=40)
    s, dec, _pd = _score_option(_pos(type='CALL'), _opt(dte=32, delta=-0.20),
                          profit_captured=-28.0, pl=-300, today=TODAY, yf_client=None,
                          trend_ctx=ctx)
    assert 'trend 72▲⚠️' in dec


def test_option_no_trend_data_shows_dash():
    # No trend context → the tag renders as 'trend —' so missing data is
    # visible instead of silently absent.
    s, dec, _pd = _score_option(_pos(), _opt(dte=32, delta=-0.30), profit_captured=-20.0,
                          pl=-200, today=TODAY, yf_client=None)
    assert 'trend —' in dec


def test_option_action_decisions_skip_trend_tag():
    # CLOSE/ROLL/STOP-tier rows are actions, not holds — no trend tag.
    from src.analysis.profit_management import TrendContext
    ctx = TrendContext(trend_composite=72, sentiment_direction='BULLISH', iv_rank=40)
    s, dec, _pd = _score_option(_pos(), _opt(dte=40, delta=-0.20), profit_captured=-300.0,
                          pl=-3000, today=TODAY, yf_client=None, trend_ctx=ctx)
    assert 'STOP TIER' in dec
    assert 'trend' not in dec


def test_option_score_always_in_range():
    """No combination of inputs should push the score outside 1-10."""
    for pc in (-400, -200, -50, 0, 30, 60, 90):
        for dte in (1, 5, 14, 21, 40, 90):
            for d in (-0.7, -0.45, -0.2, -0.05):
                s, _, _pd = _score_option(_pos(), _opt(dte=dte, delta=d),
                                     profit_captured=pc, pl=-5000,
                                     today=TODAY, yf_client=None)
                assert 1.0 <= s <= 10.0, f"score {s} out of range for pc={pc} dte={dte} d={d}"


# ── _score_holding ───────────────────────────────────────────────

def _snap(**kw):
    base = dict(ticker='X', name='X', last_price=100, rsi_14=50,
                sma_50=98, sma_200=95, volume_ratio=1.0)
    base.update(kw)
    return StockSnapshot(**base)


def test_holding_neutral_scores_mid():
    s = _score_holding(_snap(), 'X', yf_client=None, regime='NEUTRAL', regime_mult=1.0)
    assert 1.0 <= s <= 10.0


def test_holding_bearish_regime_penalizes():
    s_neutral = _score_holding(_snap(), 'X', yf_client=None, regime='NEUTRAL', regime_mult=1.0)
    s_bear = _score_holding(_snap(), 'X', yf_client=None, regime='BEARISH', regime_mult=1.0)
    assert s_bear > s_neutral


def test_holding_uptrend_improves_score():
    s_down = _score_holding(_snap(sma_50=98, sma_200=95, last_price=90), 'X',
                            yf_client=None, regime='NEUTRAL', regime_mult=1.0)
    s_up = _score_holding(_snap(sma_50=98, sma_200=95, last_price=120), 'X',
                          yf_client=None, regime='NEUTRAL', regime_mult=1.0)
    assert s_up < s_down


# ── _find_best_cc ────────────────────────────────────────────────

class FakeMoomoo:
    """Only implements get_option_snapshots — returns a canned chain."""
    def __init__(self, contracts):
        self._contracts = contracts

    def get_option_snapshots(self, ticker, dte_min=7, dte_max=60):
        return self._contracts


def _cc(strike, bid, delta, dte=40, oi=1000, vol=200, iv=30):
    return OptionSnapshot(
        code=f'C{strike}', name='c', underlying='X', option_type='CALL',
        strike=strike, expiry='2099-01-01', dte=dte, area_type='AMERICAN',
        bid=bid, ask=bid + 0.2, open_interest=oi, volume=vol,
        implied_vol=iv, delta=delta)


def test_find_best_cc_picks_lowest_penalty():
    snap = _snap(last_price=100)
    moomoo = FakeMoomoo([
        _cc(strike=105, bid=2.0, delta=0.30, dte=40),   # optimal DTE, decent roc
        _cc(strike=110, bid=0.5, delta=0.16, dte=10),   # short DTE penalty, low roc
        _cc(strike=120, bid=0.0, delta=0.10),           # filtered (bid<=0 / delta<0.15)
    ])
    best = _find_best_cc(moomoo, 'X', snap, shares=200, cost_basis=90,
                         yf_client=None, regime='NEUTRAL', regime_mult=1.0)
    assert best is not None
    assert best['strike'] == 105


def test_find_best_cc_respects_above_basis():
    """Without allow_below_basis, strikes <= cost_basis are excluded."""
    snap = _snap(last_price=100)
    moomoo = FakeMoomoo([
        _cc(strike=95, bid=5.0, delta=0.30),    # below basis → excluded
        _cc(strike=110, bid=2.0, delta=0.25),   # above basis → ok
    ])
    best = _find_best_cc(moomoo, 'X', snap, shares=200, cost_basis=100,
                         yf_client=None, regime='NEUTRAL', regime_mult=1.0)
    assert best['strike'] == 110


def test_find_best_cc_allows_below_basis_when_flagged():
    snap = _snap(last_price=100)
    moomoo = FakeMoomoo([_cc(strike=90, bid=5.0, delta=0.30)])
    best = _find_best_cc(moomoo, 'X', snap, shares=200, cost_basis=100,
                         yf_client=None, regime='NEUTRAL', regime_mult=1.0,
                         allow_below_basis=True)
    assert best is not None and best['strike'] == 90


def test_find_best_cc_none_if_under_100_shares():
    snap = _snap(last_price=100)
    moomoo = FakeMoomoo([_cc(strike=110, bid=2.0, delta=0.25)])
    assert _find_best_cc(moomoo, 'X', snap, shares=50, cost_basis=90,
                         yf_client=None, regime='NEUTRAL', regime_mult=1.0) is None


def test_find_best_cc_none_if_no_qualified_contract():
    snap = _snap(last_price=100)
    moomoo = FakeMoomoo([_cc(strike=110, bid=0.05, delta=0.25)])  # bid too low
    assert _find_best_cc(moomoo, 'X', snap, shares=200, cost_basis=90,
                         yf_client=None, regime='NEUTRAL', regime_mult=1.0) is None
