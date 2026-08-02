"""Tests for the profit-target optimization behavioral guardrails.

Spec: specs/profit-target-optimization.md
Covers:
  §6  OTM-only close gate
  §5  Target-aware scoring weight (replaces hardcoded >= 70)
  §7  Revised delta thresholds (0.40→0.50 for decision/warn) + DTE interaction
  §9  Per-ticker frequency cap
  §8  Full trend-modulated matrix (engine already implements; tested end-to-end)
"""

from datetime import date

import pytest

from src.data.models import StockSnapshot, OptionSnapshot
from src.analysis.profit_management import TrendContext, ProfitDecision
from src.scoring.holding_score import (
    _score_option, _ticker_frequency_ok, _OptionCurrent,
)


TODAY = date.today()


# ── Fixtures / helpers ─────────────────────────────────────────

class _NoDataMoomoo:
    """Keep tests offline (same pattern as test_holding_score.py)."""
    def get_stock_snapshot(self, ticker):
        raise RuntimeError("offline test — no moomoo data")
    def close(self):
        pass


@pytest.fixture(autouse=True)
def _stub_moomoo(monkeypatch):
    import src.data.moomoo_client as mc
    monkeypatch.setattr(mc, 'MoomooClient', lambda *a, **kw: _NoDataMoomoo())


def _opt(dte=40, delta=-0.20, bid=5.0):
    """Lightweight _OptionCurrent."""
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


def _strong_trend_ctx():
    return TrendContext(trend_composite=75, sentiment_direction='BULLISH', iv_rank=40)


def _moderate_trend_ctx():
    return TrendContext(trend_composite=55, sentiment_direction='NEUTRAL', iv_rank=None)


def _no_trend_ctx():
    return TrendContext(trend_composite=30, sentiment_direction='CAUTIOUS', iv_rank=None)


# ═══════════════════════════════════════════════════════════════
# §6 — OTM-only close gate
# ═══════════════════════════════════════════════════════════════

class TestOtmCloseGate:
    """OTM gate overrides ACTION_CLOSE when |Δ| < 0.30 and DTE > 21."""

    def test_otm_close_overridden_to_hold(self):
        """CSP at 55%, |Δ|=0.18, DTE=40 → engine says CLOSE, gate says HOLD."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.18), profit_captured=55.0, pl=500,
            today=TODAY, yf_client=None, trend_ctx=_no_trend_ctx())
        assert pd.action == ProfitDecision.ACTION_CLOSE  # engine wanted CLOSE
        assert 'OTM GATE' in dec or 'HOLD' in dec  # gate overrode
        assert 'CLOSE' not in dec  # should NOT emit CLOSE
        assert s > 3.0  # not an urgent action

    def test_otm_close_allowed_when_delta_high(self):
        """CSP at 55%, |Δ|=0.45, DTE=40 → CLOSE allowed (delta above gate)."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.45), profit_captured=55.0, pl=500,
            today=TODAY, yf_client=None, trend_ctx=_no_trend_ctx())
        assert pd.action == ProfitDecision.ACTION_CLOSE
        assert 'CLOSE' in dec
        assert 'OTM GATE' not in dec

    def test_otm_gate_does_not_override_manage_dte(self):
        """CSP at 55%, |Δ|=0.18, DTE=15 → engine MANAGE_DTE. DTE layer may
        soften the message but the position is still managed, not OTM-held."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=15, delta=-0.18), profit_captured=55.0, pl=500,
            today=TODAY, yf_client=None, trend_ctx=_no_trend_ctx())
        assert pd.action == ProfitDecision.ACTION_MANAGE_DTE
        assert 'OTM GATE' not in dec  # OTM gate does not override MANAGE_DTE

    def test_otm_gate_does_not_override_rolls(self):
        """CSP at 90% in strong uptrend → ROLL_DOWN_OUT, not OTM-held."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.18), profit_captured=90.0, pl=800,
            today=TODAY, yf_client=None, trend_ctx=_strong_trend_ctx())
        assert pd.action == ProfitDecision.ACTION_ROLL_DOWN_OUT
        assert 'ROLL' in dec

    def test_loss_side_not_affected_by_otm_gate(self):
        """CSP underwater, |Δ|=0.18 → loss-side stop fires, OTM gate irrelevant."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.18), profit_captured=-300.0, pl=-3000,
            today=TODAY, yf_client=None)
        # OTM gate only fires on ACTION_CLOSE with profit_captured >= base_target
        assert 'STOP' in dec or 'ALERT' in dec

    def test_deep_profit_close_not_overridden(self):
        """CSP at 90%, |Δ|=0.18, DTE=40, no trend → engine extends to 85% via
        _resolve_csp but 90 >= 85 so ACTION_CLOSE. However, this is a CLOSE
        with deep profit. With no trend context, the base is 50% and profit > base,
        so it would be ACTION_CLOSE. But OTM gate should hold it since |Δ| < 0.30."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.18), profit_captured=90.0, pl=800,
            today=TODAY, yf_client=None, trend_ctx=None)
        # With no trend, base target = 50%, 90% >= 50% → ACTION_CLOSE
        # But OTM gate holds: |Δ|=0.18 < 0.30, DTE=40 > 21
        assert 'OTM GATE' in dec or 'HOLD' in dec


# ═══════════════════════════════════════════════════════════════
# §5 — Target-aware scoring weight
# ═══════════════════════════════════════════════════════════════

class TestTargetAwareWeight:
    """Score weight is based on pd.target_pct, not hardcoded 70%."""

    def test_deep_past_target_heavy_weight(self):
        """90% profit vs 50% target (no trend) → depth=40 → score -= 2.0."""
        # Use |Δ|=0.50 which is at the ITM gate boundary. The close weight (-2.0)
        # plus ITM bump (+1.0) nets to ~4.0. Key assertion: it's a low score.
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.50), profit_captured=90.0, pl=800,
            today=TODAY, yf_client=None, trend_ctx=None)
        # |Δ|=0.50 >= 0.30 so OTM gate does NOT fire
        assert pd.target_pct == 50.0  # no trend → base target
        assert s <= 5.0  # deep past target → heavy weight → low score

    def test_at_target_moderate_weight(self):
        """55% profit vs 50% target → depth=5 → score -= 1.5."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.50), profit_captured=55.0, pl=500,
            today=TODAY, yf_client=None, trend_ctx=None)
        assert pd.target_pct == 50.0
        assert 'CLOSE' in dec
        assert 3.0 < s < 5.0  # moderate weight

    def test_trend_extended_target_honored(self):
        """Strong trend → target=85%. 80% profit → depth=-5 → still HOLD, weight 1.0."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.18), profit_captured=80.0, pl=600,
            today=TODAY, yf_client=None, trend_ctx=_strong_trend_ctx())
        assert pd.target_pct == 85.0  # strong trend extension
        # 80% < 85% → engine says HOLD. OTM gate also holds.
        assert 'HOLD' in dec
        assert pd.extended_by_trend is True


# ═══════════════════════════════════════════════════════════════
# §7 — Revised delta thresholds + DTE interaction
# ═══════════════════════════════════════════════════════════════

class TestDeltaThresholds:
    """csp_decision and cc_warn raised to 0.50; DTE ≤ 21 relaxes to 0.40."""

    def test_delta_045_far_dte_no_decision_alert(self):
        """|Δ|=0.45 at DTE=40 — below new csp_decision=0.50 → no alert."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.45), profit_captured=10.0, pl=100,
            today=TODAY, yf_client=None)
        assert 'decision time' not in dec.lower()

    def test_delta_055_far_dte_decision_alert(self):
        """|Δ|=0.55 at DTE=40 — now >= csp_itm=0.50 (ITM gate fires first in the
        ladder, before the decision gate which is also 0.50). Expect ITM alert."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.55), profit_captured=10.0, pl=100,
            today=TODAY, yf_client=None)
        # With csp_decision == csp_itm == 0.50, delta=0.55 hits ITM first
        assert 'ITM' in dec or 'decision time' in dec.lower()

    def test_delta_045_near_dte_decision_alert(self):
        """|Δ|=0.45 at DTE=15 — gamma zone relaxes to 0.40 → decision alert."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=15, delta=-0.45), profit_captured=10.0, pl=100,
            today=TODAY, yf_client=None)
        assert 'decision time' in dec.lower()

    def test_critical_delta_unchanged(self):
        """|Δ|=0.65 at DTE=40 → csp_critical=0.60 unchanged → STOP."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.65), profit_captured=10.0, pl=0,
            today=TODAY, yf_client=None)
        assert 'STOP' in dec or 'exit' in dec or 'roll' in dec or 'assignment' in dec

    def test_itm_delta_unchanged(self):
        """|Δ|=0.55 at DTE=40 — >= csp_itm=0.50. Since csp_decision is also 0.50,
        the ITM gate (checked first) fires."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.55), profit_captured=10.0, pl=100,
            today=TODAY, yf_client=None)
        assert 'ITM' in dec


# ═══════════════════════════════════════════════════════════════
# §9 — Per-ticker frequency cap
# ═══════════════════════════════════════════════════════════════

class TestTickerFrequencyCap:

    def test_frequency_ok_no_orders(self):
        ok, note = _ticker_frequency_ok(_pos(ticker='AVGO'), TODAY, [])
        assert ok is True
        assert note == ''

    def test_frequency_ok_under_limit(self):
        orders = [
            {'date': TODAY.strftime('%Y-%m-01'), 'code': 'AVGO_PUT', 'side': 'BUY',
             'status': 'FILLED_ALL'},
        ]
        ok, note = _ticker_frequency_ok(_pos(ticker='AVGO'), TODAY, orders)
        assert ok is True

    def test_frequency_blocked_at_limit(self):
        orders = [
            {'date': TODAY.strftime('%Y-%m-01'), 'code': 'AVGO_PUT', 'side': 'BUY',
             'status': 'FILLED_ALL'},
            {'date': TODAY.strftime('%Y-%m-05'), 'code': 'AVGO_PUT', 'side': 'BUY_BACK',
             'status': 'FILLED_ALL'},
        ]
        ok, note = _ticker_frequency_ok(_pos(ticker='AVGO'), TODAY, orders)
        assert ok is False
        assert '2/2' in note

    def test_frequency_only_counts_current_month(self):
        orders = [
            # Last month — should not count
            {'date': '2025-12-01', 'code': 'AVGO_PUT', 'side': 'BUY',
             'status': 'FILLED_ALL'},
            {'date': '2025-12-15', 'code': 'AVGO_PUT', 'side': 'BUY_BACK',
             'status': 'FILLED_ALL'},
        ]
        ok, note = _ticker_frequency_ok(_pos(ticker='AVGO'), TODAY, orders)
        assert ok is True

    def test_frequency_only_counts_closes(self):
        orders = [
            # SELL = opening, should not count as a close
            {'date': TODAY.strftime('%Y-%m-01'), 'code': 'AVGO_PUT', 'side': 'SELL',
             'status': 'FILLED_ALL'},
            {'date': TODAY.strftime('%Y-%m-05'), 'code': 'AVGO_PUT', 'side': 'SELL_SHORT',
             'status': 'FILLED_ALL'},
        ]
        ok, note = _ticker_frequency_ok(_pos(ticker='AVGO'), TODAY, orders)
        assert ok is True

    def test_frequency_different_ticker_ignored(self):
        orders = [
            {'date': TODAY.strftime('%Y-%m-01'), 'code': 'GOOG_PUT', 'side': 'BUY',
             'status': 'FILLED_ALL'},
            {'date': TODAY.strftime('%Y-%m-05'), 'code': 'GOOG_PUT', 'side': 'BUY_BACK',
             'status': 'FILLED_ALL'},
        ]
        ok, note = _ticker_frequency_ok(_pos(ticker='AVGO'), TODAY, orders)
        assert ok is True

    def test_frequency_cap_suppresses_profit_close(self):
        """When frequency cap is hit, profit-taking CLOSE is suppressed to HOLD.
        Use |Δ|=0.35 — above OTM gate (0.30) but below ITM gate (0.50) so
        the delta layer doesn't overwrite the frequency-cap decision."""
        orders = [
            {'date': TODAY.strftime('%Y-%m-01'), 'code': 'AVGO_PUT', 'side': 'BUY',
             'status': 'FILLED_ALL'},
            {'date': TODAY.strftime('%Y-%m-05'), 'code': 'AVGO_PUT', 'side': 'BUY_BACK',
             'status': 'FILLED_ALL'},
        ]
        s, dec, pd = _score_option(
            _pos(ticker='AVGO'), _opt(dte=40, delta=-0.35), profit_captured=55.0, pl=500,
            today=TODAY, yf_client=None, orders=orders, trend_ctx=None)
        assert 'FREQ-CAPPED' in dec or 'HOLD' in dec


# ═══════════════════════════════════════════════════════════════
# §8 — Full trend-modulated matrix (end-to-end)
# ═══════════════════════════════════════════════════════════════

class TestTrendModulatedMatrix:

    def test_csp_strong_trend_extends_to_85(self):
        """Strong trend → target 85%. 80% profit → HOLD (below 85%)."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.18), profit_captured=80.0, pl=600,
            today=TODAY, yf_client=None, trend_ctx=_strong_trend_ctx())
        assert pd.target_pct == 85.0
        assert pd.extended_by_trend is True
        assert 'HOLD' in dec

    def test_csp_moderate_trend_extends_to_70(self):
        """Moderate trend → target 70%. 65% profit → HOLD (below 70%)."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.18), profit_captured=65.0, pl=500,
            today=TODAY, yf_client=None, trend_ctx=_moderate_trend_ctx())
        assert pd.target_pct == 70.0
        assert pd.extended_by_trend is True
        assert 'HOLD' in dec

    def test_csp_no_trend_base_50(self):
        """No trend → base 50%. OTM gate holds the close."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.18), profit_captured=55.0, pl=500,
            today=TODAY, yf_client=None, trend_ctx=_no_trend_ctx())
        assert pd.target_pct == 50.0
        assert pd.extended_by_trend is False

    def test_cc_uptrend_roll_up_out(self):
        """CC in uptrend → ROLL_UP_OUT (not CLOSE, not extended target)."""
        s, dec, pd = _score_option(
            _pos(type='CALL'), _opt(dte=40, delta=-0.20), profit_captured=55.0, pl=500,
            today=TODAY, yf_client=None, trend_ctx=_strong_trend_ctx())
        # CC uptrend should trigger roll-up-out
        assert pd.action == ProfitDecision.ACTION_ROLL_UP_OUT

    def test_3rd_element_is_profit_decision(self):
        """The 3rd return element is a ProfitDecision with all fields."""
        s, dec, pd = _score_option(
            _pos(), _opt(dte=40, delta=-0.20), profit_captured=10.0, pl=100,
            today=TODAY, yf_client=None)
        assert isinstance(pd, ProfitDecision)
        assert hasattr(pd, 'action')
        assert hasattr(pd, 'target_pct')
        assert hasattr(pd, 'extended_by_trend')
        assert hasattr(pd, 'strategy')


# ═══════════════════════════════════════════════════════════════
# Score range invariant
# ═══════════════════════════════════════════════════════════════

class TestScoreRange:
    """No combination of inputs should push the score outside 1-10."""

    @pytest.mark.parametrize("pc", (-400, -200, -50, 0, 30, 60, 90))
    @pytest.mark.parametrize("dte", (1, 5, 14, 21, 40, 90))
    @pytest.mark.parametrize("delta", (-0.7, -0.55, -0.35, -0.18, -0.05))
    def test_score_always_in_range(self, pc, dte, delta):
        s, _, _pd = _score_option(
            _pos(), _opt(dte=dte, delta=delta),
            profit_captured=pc, pl=-5000,
            today=TODAY, yf_client=None)
        assert 1.0 <= s <= 10.0, f"score {s} out of range for pc={pc} dte={dte} delta={delta}"
