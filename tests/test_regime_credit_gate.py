"""Tests for the credit-stress hard gate on regime position sizing.

2026-08-14: VIX 14.5 (calm) + HYG/IEF -14.5% (STRESSED credit) produced a
+1 vote tally → NEUTRAL → 75% position size. Credit stress only counted as
one -1 vote, so full-size positioning could coexist with stressed credit.
The hard cap (rules.yaml regime.credit_stress_position_mult_cap) clamps it.
"""

from src.data.yfinance_client import MacroData, apply_credit_stress_cap


def _macro(credit='STRESSED', mult=0.75):
    return MacroData(vix=14.5, credit_regime=credit, position_mult=mult,
                     market_regime='NEUTRAL', regime_score=1)


def test_stressed_credit_caps_size():
    m = _macro(mult=0.75)
    apply_credit_stress_cap(m, cap=0.50)
    assert m.position_mult == 0.50
    assert 'STRESSED' in m.sizing_gate_note and '50%' in m.sizing_gate_note


def test_healthy_credit_not_capped():
    m = _macro(credit='HEALTHY', mult=0.75)
    apply_credit_stress_cap(m, cap=0.50)
    assert m.position_mult == 0.75 and m.sizing_gate_note == ''


def test_none_cap_disables_gate():
    m = _macro(mult=0.75)
    apply_credit_stress_cap(m, cap=None)
    assert m.position_mult == 0.75 and m.sizing_gate_note == ''


def test_already_below_cap_untouched_and_unnoted():
    """VOLATILE (25%) under a 50% cap: no clamp, no gate note (nothing changed)."""
    m = _macro(mult=0.25)
    apply_credit_stress_cap(m, cap=0.50)
    assert m.position_mult == 0.25 and m.sizing_gate_note == ''


def test_config_cap_wired():
    """rules.yaml carries the gate and src.config exposes it."""
    from src.config import get_config
    cap = get_config().credit_stress_position_mult_cap
    assert cap is not None and 0.0 < cap < 1.0
