"""Tests for src/analysis/profit_management.py — the trend-modulated P&L core.

Validates the spec §4.2 decision matrix: the CSP/CC asymmetry, the tiered
targets, and the hard gates (DTE floor, capital scarcity). Plus the loss-side
2× premium trend overlay (spec §5.1). The default `trend_ctx=None` path must be
byte-identical to the old flat-50% behavior (regression safety).
"""

import pytest

from src.analysis.profit_management import (
    TrendContext, ProfitDecision, decide_profit_target, loss_alert_should_hard_stop,
)


PD = ProfitDecision

# Strong-trend stack: composite 75 (≥70), sentiment BULLISH (allowed), IVR 50 (≥30)
STRONG = TrendContext(75, 60, 'BULLISH', 50)
# Confirmed-but-not-strong: composite 60 (≥50, <70), sentiment NEUTRAL (allowed)
TREND = TrendContext(60, 55, 'NEUTRAL', 20)
# No trend: composite 30 (<50)
NOTREND = TrendContext(30, 40, 'CAUTIOUS', 20)


# ── CSP: trend extension allowed ──────────────────────────────────

def test_csp_strong_trend_extends_to_85_below_target_holds():
    d = decide_profit_target('CSP', 40, 40, 0.20, STRONG, 'ABUNDANT')
    assert d.target_pct == 85 and d.action == PD.ACTION_HOLD
    assert d.extended_by_trend is True


def test_csp_strong_trend_at_85_rolls_down_out():
    """CSP winner in strong trend rolls down-and-out instead of flat-closing."""
    d = decide_profit_target('CSP', 85, 40, 0.20, STRONG, 'ABUNDANT')
    assert d.action == PD.ACTION_ROLL_DOWN_OUT and d.target_pct == 85


def test_csp_confirmed_trend_extends_to_70():
    d = decide_profit_target('CSP', 60, 40, 0.20, TREND, 'NORMAL')
    assert d.target_pct == 70 and d.action == PD.ACTION_HOLD and d.extended_by_trend


def test_csp_confirmed_trend_at_70_rolls_winner():
    """CSP winner at the trend target rolls down-and-out (spec §4.3 — roll winners
    for credit to bank the win AND stay in the thesis)."""
    d = decide_profit_target('CSP', 72, 40, 0.20, TREND, 'NORMAL')
    assert d.action == PD.ACTION_ROLL_DOWN_OUT and d.target_pct == 70


def test_csp_no_trend_stays_base_50():
    d = decide_profit_target('CSP', 40, 40, 0.20, NOTREND, 'NORMAL')
    assert d.target_pct == 50 and d.extended_by_trend is False


def test_csp_no_trend_at_55_closes_at_base():
    d = decide_profit_target('CSP', 55, 40, 0.20, NOTREND, 'NORMAL')
    assert d.action == PD.ACTION_CLOSE and d.target_pct == 50


# ── CSP sentiment / IV gates ──────────────────────────────────────

def test_csp_strong_trend_blocked_by_bearish_sentiment():
    """Strong trend but BEARISH sentiment → not allowed → base 50%."""
    bearish = TrendContext(75, 30, 'BEARISH', 50)
    d = decide_profit_target('CSP', 40, 40, 0.20, bearish, 'ABUNDANT')
    assert d.target_pct == 50 and not d.extended_by_trend


def test_csp_strong_trend_blocked_by_low_iv_rank():
    """Strong trend + sentiment ok but IVR < 30 → drops to trend target (70), not 85."""
    low_iv = TrendContext(75, 60, 'BULLISH', 15)   # IVR 15 < 30
    d = decide_profit_target('CSP', 40, 40, 0.20, low_iv, 'ABUNDANT')
    # strong_min requires ivr_ok; falls through to trend≥50 → 70
    assert d.target_pct == 70 and d.extended_by_trend


# ── CC: trend extension NOT allowed ───────────────────────────────

def test_cc_never_extends_target_in_uptrend():
    """CC in strong uptrend still targets base 50% (uptrend is the danger side)."""
    d = decide_profit_target('CC', 40, 40, 0.20, STRONG, 'ABUNDANT')
    assert d.target_pct == 50 and not d.extended_by_trend


def test_cc_winner_in_uptrend_rolls_up_out():
    d = decide_profit_target('CC', 55, 40, 0.20, TREND, 'NORMAL')
    assert d.action == PD.ACTION_ROLL_UP_OUT and d.target_pct == 50


def test_cc_winner_no_trend_closes():
    d = decide_profit_target('CC', 55, 40, 0.20, NOTREND, 'NORMAL')
    assert d.action == PD.ACTION_CLOSE


def test_cc_below_target_holds():
    d = decide_profit_target('CC', 30, 40, 0.20, TREND, 'NORMAL')
    assert d.action == PD.ACTION_HOLD


# ── Hard gates (override everything) ──────────────────────────────

def test_dte_floor_overrides_strong_trend_winner():
    d = decide_profit_target('CSP', 85, 21, 0.20, STRONG, 'ABUNDANT')
    assert d.action == PD.ACTION_MANAGE_DTE and d.target_pct == 0


def test_dte_floor_at_exactly_21():
    d = decide_profit_target('CSP', 10, 21, 0.20, NOTREND, 'NORMAL')
    assert d.action == PD.ACTION_MANAGE_DTE


def test_capital_scarce_overrides_trend_extension():
    """SCARCE capital → book at base 50%, never extend."""
    d = decide_profit_target('CSP', 40, 40, 0.20, STRONG, 'SCARCE')
    assert d.target_pct == 50 and not d.extended_by_trend
    assert d.action == PD.ACTION_HOLD  # 40 < 50


def test_capital_scarce_winner_closes_at_base():
    d = decide_profit_target('CSP', 85, 40, 0.20, STRONG, 'SCARCE')
    assert d.action == PD.ACTION_CLOSE and d.target_pct == 50


# ── Deployment-aware SCARCE bypass (csp_paused + feature flag) ────
# When CSP redeployment is blocked (deployment % over limit) AND the feature
# flag is on, SCARCE is skipped so trend extension applies to qualifying CSPs.
# Rationale: freed capital has no CSP slot to redeploy into, so the
# capital-velocity argument for forcing 50% collapses.

def _set_bypass_flag(monkeypatch, value: bool):
    """Stub the top-level profit_take accessor to control the bypass flag.

    The flag key bypass_scarce_when_csp_paused is read via cfg.profit_take();
    we delegate all other keys to the real config so the rest of the decision
    tree (dte_floor, capital_scarcity_override, etc.) stays live.
    """
    from src.config import get_config
    cfg = get_config()
    real = cfg.profit_take
    monkeypatch.setattr(
        cfg, 'profit_take',
        lambda k, d=None: value if k == 'bypass_scarce_when_csp_paused' else real(k, d),
    )


def test_scarce_bypass_when_csp_paused_and_flag_on(monkeypatch):
    """SCARCE + strong trend + csp_paused + flag ON → trend extension applies (85%)."""
    _set_bypass_flag(monkeypatch, True)
    d = decide_profit_target('CSP', 40, 40, 0.20, STRONG, 'SCARCE', csp_paused=True)
    assert d.target_pct == 85 and d.extended_by_trend
    assert d.action == PD.ACTION_HOLD  # 40 < 85


def test_scarce_bypass_disabled_when_flag_off(monkeypatch):
    """SCARCE + strong trend + csp_paused + flag OFF → current behavior (50%).

    Guards against accidentally flipping the default — the bypass only fires
    when the operator explicitly enables it via rules.yaml.
    """
    _set_bypass_flag(monkeypatch, False)
    d = decide_profit_target('CSP', 40, 40, 0.20, STRONG, 'SCARCE', csp_paused=True)
    assert d.target_pct == 50 and not d.extended_by_trend


def test_scarce_still_applies_when_csp_not_paused(monkeypatch):
    """SCARCE + strong trend + csp NOT paused + flag ON → still 50% (no bypass).

    The bypass is conditional on redeployment actually being blocked. When CSP
    slots are available, the SCARCE override fires normally even with the flag on.
    """
    _set_bypass_flag(monkeypatch, True)
    d = decide_profit_target('CSP', 40, 40, 0.20, STRONG, 'SCARCE', csp_paused=False)
    assert d.target_pct == 50 and not d.extended_by_trend


# ── Backward compatibility: trend_ctx=None → flat 50% ─────────────

def test_none_trend_ctx_csp_below_50_holds():
    d = decide_profit_target('CSP', 30, 40, 0.20, None)
    assert d.action == PD.ACTION_HOLD and d.target_pct == 50 and not d.extended_by_trend


def test_none_trend_ctx_csp_at_55_closes():
    d = decide_profit_target('CSP', 55, 40, 0.20, None)
    assert d.action == PD.ACTION_CLOSE and d.target_pct == 50


def test_none_trend_ctx_cc_at_55_closes():
    """No trend → CC never rolls up-out, just closes."""
    d = decide_profit_target('CC', 55, 40, 0.20, None)
    assert d.action == PD.ACTION_CLOSE and d.target_pct == 50


def test_none_capital_scarcity_treated_as_normal():
    d = decide_profit_target('CSP', 40, 40, 0.20, STRONG, None)
    assert d.target_pct == 85  # trend extension applies (NORMAL)


# ── Loss-side: 2× premium trend overlay (spec §5.1) ───────────────

def test_loss_overlay_low_trend_is_hard_stop():
    hard, _ = loss_alert_should_hard_stop(TrendContext(30), 0)
    assert hard is True


def test_loss_overlay_strong_trend_allows_one_roll():
    hard, _ = loss_alert_should_hard_stop(TrendContext(60), 0)
    assert hard is False


def test_loss_overlay_strong_trend_exhausted_rolls_hard_stops():
    hard, _ = loss_alert_should_hard_stop(TrendContext(60), 1)
    assert hard is True


def test_loss_overlay_no_trend_data_hard_stops():
    hard, _ = loss_alert_should_hard_stop(None, 0)
    assert hard is True


# ── Config-driven: thresholds come from rules.yaml ────────────────

def test_csp_extension_can_be_disabled(monkeypatch):
    """Disabling CSP trend_extension forces base 50% even with a strong trend."""
    from src.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, 'profit_take_csp',
                        lambda k, d=None: False if k == 'trend_extension_enabled' else d)
    d = decide_profit_target('CSP', 40, 40, 0.20, STRONG, 'ABUNDANT')
    assert d.target_pct == 50 and not d.extended_by_trend


def test_targets_read_from_config(monkeypatch):
    """The 85/70/50 numbers are not hardcoded — they come from config."""
    from src.config import get_config
    cfg = get_config()
    fake = {
        'base_pct': 50, 'strong_trend_target_pct': 90, 'trend_target_pct': 75,
        'trend_extension_enabled': True,
    }
    monkeypatch.setattr(cfg, 'profit_take_csp', lambda k, d=None: fake.get(k, d))
    d = decide_profit_target('CSP', 40, 40, 0.20, STRONG, 'ABUNDANT')
    assert d.target_pct == 90


# ── Strategy normalization ────────────────────────────────────────

def test_strategy_case_insensitive():
    d1 = decide_profit_target('csp', 40, 40, 0.20, STRONG, 'ABUNDANT')
    d2 = decide_profit_target('CSP', 40, 40, 0.20, STRONG, 'ABUNDANT')
    assert d1.target_pct == d2.target_pct == 85


def test_unknown_strategy_defaults_to_csp():
    d = decide_profit_target('WIDGET', 55, 40, 0.20, NOTREND, 'NORMAL')
    assert d.strategy == 'CSP' and d.action == PD.ACTION_CLOSE
