"""
Tests for thesis validation, guardrails, and decision messages.

Covers:
- src/analysis/thesis_validator.py — ThesisStatus, ThesisCheck, ThesisReport,
  quick_thesis_check, _check_fundamental_health, _check_price_performance,
  _check_volatility_regime, _generate_overall_assessment, _generate_recommended_action
- src/guardrails/limits.py — GuardrailViolation, PositionLimits, GuardrailChecker
- src/portfolio/summary.py — generate_option_decision_message
"""

import pytest
from datetime import datetime

from src.analysis.thesis_validator import (
    ThesisStatus,
    ThesisCheck,
    ThesisReport,
    quick_thesis_check,
    _check_fundamental_health,
    _check_price_performance,
    _check_volatility_regime,
    _generate_overall_assessment,
    _generate_recommended_action,
)
from src.data.models import StockSnapshot
from src.guardrails.limits import (
    GuardrailViolation,
    PositionLimits,
    GuardrailChecker,
)
from src.portfolio.summary import generate_option_decision_message


# ── helpers ────────────────────────────────────────────────────────

def _snap(**kw):
    """Build a StockSnapshot with test-friendly defaults."""
    base = dict(
        ticker='TEST', name='TestCo', last_price=100.0,
        rsi_14=50, sma_50=98, sma_200=95, volume_ratio=1.0,
        pe_ratio=20.0, highest_52w=120.0, lowest_52w=80.0,
        hv_30d=30.0,
    )
    base.update(kw)
    return StockSnapshot(**base)


# ── ThesisStatus enum ──────────────────────────────────────────────

def test_thesis_status_values():
    assert ThesisStatus.INTACT.value == "THESIS_INTACT"
    assert ThesisStatus.BROKEN.value == "THESIS_BROKEN"
    assert ThesisStatus.DAMAGED.value == "TECHNICAL_DAMAGE"


# ── ThesisCheck dataclass ──────────────────────────────────────────

def test_thesis_check_creation():
    check = ThesisCheck(
        metric="fundamentals_pe",
        current_value=-456.0,
        threshold=100,
        severity="CRITICAL",
        status="FAILED",
        message="P/E ratio extremely high (-456.0) - speculative valuation",
    )
    assert check.metric == "fundamentals_pe"
    assert check.severity == "CRITICAL"
    assert check.status == "FAILED"


# ── ThesisReport dataclass ─────────────────────────────────────────

def test_thesis_report_creation():
    now = datetime.now()
    report = ThesisReport(
        ticker="BE",
        status=ThesisStatus.BROKEN,
        checks=[],
        timestamp=now,
        overall_assessment="THESIS BROKEN",
        recommended_action="Exit position",
    )
    assert report.ticker == "BE"
    assert report.status == ThesisStatus.BROKEN
    assert report.timestamp == now


# ── _check_fundamental_health ──────────────────────────────────────

def test_fundamental_health_negative_pe():
    """P/E < 0 → CRITICAL (company losing money)."""
    snap = _snap(pe_ratio=-456.0)
    check = _check_fundamental_health('TEST', snap)
    assert check is not None
    assert check.severity == "CRITICAL"
    assert check.metric == "fundamentals_pe"
    assert "Negative" in check.message


def test_fundamental_health_extreme_pe():
    """P/E > 100 → CRITICAL (speculative valuation)."""
    snap = _snap(pe_ratio=150.0)
    check = _check_fundamental_health('TEST', snap)
    assert check is not None
    assert check.severity == "CRITICAL"


def test_fundamental_health_elevated_pe():
    """P/E > 50 → WARNING (concerning valuation)."""
    snap = _snap(pe_ratio=65.0)
    check = _check_fundamental_health('TEST', snap)
    assert check is not None
    assert check.severity == "WARNING"


def test_fundamental_health_normal_pe():
    """P/E 20 → no check returned (healthy)."""
    snap = _snap(pe_ratio=20.0)
    check = _check_fundamental_health('TEST', snap)
    assert check is None


def test_fundamental_health_missing_pe():
    """Missing P/E → None (can't evaluate)."""
    snap = _snap()
    # Force pe_ratio to None
    snap.pe_ratio = None
    check = _check_fundamental_health('TEST', snap)
    assert check is None


def test_fundamental_health_trusted_ticker_skips_pe(monkeypatch):
    """A trusted ticker skips the high-P/E valuation check (user accepts it)."""
    from src.config import get_config
    # Trust AMD even though its P/E would normally be CRITICAL
    monkeypatch.setattr(type(get_config()), 'trusted_tickers', {'AMD'})
    snap = _snap(pe_ratio=184.0)   # > 100 critical threshold
    assert _check_fundamental_health('AMD', snap) is None


def test_fundamental_health_trusted_ticker_still_flags_negative_pe(monkeypatch):
    """Trust skips valuation, not solvency: negative P/E still CRITICAL."""
    from src.config import get_config
    monkeypatch.setattr(type(get_config()), 'trusted_tickers', {'AMD'})
    snap = _snap(pe_ratio=-456.0)
    check = _check_fundamental_health('AMD', snap)
    assert check is not None and check.severity == "CRITICAL"


def test_fundamental_health_thresholds_from_config(monkeypatch):
    """P/E thresholds are read from config, not hardcoded."""
    from src.config import get_config
    cfg = get_config()
    # Raise the critical threshold so P/E 150 is only a WARNING, not CRITICAL
    # Patch the CLASS, not the singleton instance — an instance patch leaves
    # a shadow attribute on the cached Config that outlives monkeypatch
    # teardown and silently overrides later class patches (see conftest).
    monkeypatch.setattr(type(cfg), 'thesis_validation',
                        lambda self, k, d=None: {
                            'pe_ratio_warning': 50, 'pe_ratio_critical': 250,
                            'pe_negative_critical': True,
                        }.get(k, d))
    monkeypatch.setattr(type(cfg), 'trusted_tickers', set())
    snap = _snap(pe_ratio=150.0)
    check = _check_fundamental_health('TEST', snap)
    assert check is not None and check.severity == "WARNING"


# ── _check_price_performance ───────────────────────────────────────

def test_price_performance_critical_drop():
    """>40% off 52-week high → CRITICAL."""
    snap = _snap(last_price=60.0, highest_52w=120.0)
    check = _check_price_performance(snap)
    assert check is not None
    assert check.severity == "CRITICAL"
    assert check.metric == "price_performance_52w"


def test_price_performance_warning_drop():
    """>25% off 52-week high → WARNING."""
    snap = _snap(last_price=85.0, highest_52w=120.0)
    check = _check_price_performance(snap)
    assert check is not None
    assert check.severity == "WARNING"


def test_price_performance_normal():
    """<25% off 52-week high → no check."""
    snap = _snap(last_price=100.0, highest_52w=120.0)
    check = _check_price_performance(snap)
    assert check is None


def test_price_performance_missing_high():
    """Missing 52-week high → None."""
    snap = _snap()
    snap.highest_52w = None
    check = _check_price_performance(snap)
    assert check is None


# ── _check_volatility_regime ───────────────────────────────────────

def test_volatility_regime_elevated():
    """HV > 100% → WARNING."""
    snap = _snap(hv_30d=120.0)
    check = _check_volatility_regime(snap)
    assert check is not None
    assert check.severity == "WARNING"
    assert check.metric == "volatility_regime"


def test_volatility_regime_normal():
    """HV = 30% → no check."""
    snap = _snap(hv_30d=30.0)
    check = _check_volatility_regime(snap)
    assert check is None


def test_volatility_regime_missing():
    """Missing HV → None."""
    snap = _snap()
    snap.hv_30d = None
    check = _check_volatility_regime(snap)
    assert check is None


# ── _generate_overall_assessment ───────────────────────────────────

def test_overall_assessment_broken():
    checks = [
        ThesisCheck("earnings", -25, -20, "CRITICAL", "FAILED", "Earnings declining"),
    ]
    result = _generate_overall_assessment(ThesisStatus.BROKEN, checks)
    assert "THESIS BROKEN" in result
    assert "Earnings declining" in result


def test_overall_assessment_damaged():
    checks = [
        ThesisCheck("technical", -18, -15, "WARNING", "CONCERNING", "Below 200 SMA"),
    ]
    result = _generate_overall_assessment(ThesisStatus.DAMAGED, checks)
    assert "THESIS DAMAGED" in result


def test_overall_assessment_intact():
    result = _generate_overall_assessment(ThesisStatus.INTACT, [])
    assert "THESIS INTACT" in result


# ── _generate_recommended_action ───────────────────────────────────

def test_recommended_action_broken():
    checks = [
        ThesisCheck("fundamentals_pe", -456, 0, "CRITICAL", "FAILED", "Negative P/E"),
    ]
    action = _generate_recommended_action("BE", ThesisStatus.BROKEN, checks)
    assert "THESIS BROKEN" in action
    assert "Exit Wheel on BE" in action
    assert "Do-Not-Wheel" in action


def test_recommended_action_damaged():
    checks = [
        ThesisCheck("technical", -18, -15, "WARNING", "CONCERNING", "Below 200 SMA"),
    ]
    action = _generate_recommended_action("AAPL", ThesisStatus.DAMAGED, checks)
    assert "THESIS DAMAGED" in action
    assert "Monitor" in action


def test_recommended_action_intact():
    action = _generate_recommended_action("V", ThesisStatus.INTACT, [])
    assert "THESIS INTACT" in action
    assert "Continue Wheel" in action


# ── quick_thesis_check ─────────────────────────────────────────────

def test_quick_thesis_broken_negative_pe():
    """P/E < 0 → thesis broken."""
    snap = _snap(pe_ratio=-456.0, last_price=100.0, highest_52w=120.0)
    result = quick_thesis_check("BE", snap)
    assert result['broken'] is True


def test_quick_thesis_broken_extreme_pe():
    """P/E > 100 → thesis broken."""
    snap = _snap(pe_ratio=150.0, last_price=100.0, highest_52w=120.0)
    result = quick_thesis_check("SPEC", snap)
    assert result['broken'] is True


def test_quick_thesis_broken_40pct_drop():
    """>40% off 52-week high → thesis broken."""
    snap = _snap(pe_ratio=15.0, last_price=60.0, highest_52w=120.0)
    result = quick_thesis_check("CRASH", snap)
    assert result['broken'] is True


def test_quick_thesis_damaged_25pct_drop():
    """>25% off 52-week high → thesis damaged."""
    snap = _snap(pe_ratio=15.0, last_price=85.0, highest_52w=120.0)
    result = quick_thesis_check("DIP", snap)
    assert result['damaged'] is True
    assert result['broken'] is False


def test_quick_thesis_intact():
    """Normal stock → thesis intact."""
    snap = _snap(pe_ratio=20.0, last_price=100.0, highest_52w=120.0)
    result = quick_thesis_check("V", snap)
    assert result['broken'] is False
    assert result['damaged'] is False


def test_quick_thesis_missing_data():
    """Missing P/E and 52-week high → intact (graceful degradation)."""
    snap = _snap(last_price=100.0)
    snap.pe_ratio = None
    snap.highest_52w = None
    result = quick_thesis_check("UNKNOWN", snap)
    assert result['broken'] is False
    assert result['damaged'] is False


# ── GuardrailViolation dataclass ───────────────────────────────────

def test_guardrail_violation_creation():
    v = GuardrailViolation(
        guardrail_type="csp_deployment",
        current_value=0.66,
        limit_value=0.25,
        severity="CRITICAL",
        message="CSP deployment 66% > 25% limit",
        required_action="Close CSPs immediately",
        stage="EMERGENCY",
    )
    assert v.guardrail_type == "csp_deployment"
    assert v.severity == "CRITICAL"
    assert v.stage == "EMERGENCY"


# ── PositionLimits ─────────────────────────────────────────────────

def test_position_limits_constants():
    assert PositionLimits.MAX_POSITION_PCT_EMERGENCY == 0.15
    assert PositionLimits.MAX_CSP_LIABILITY_CRITICAL == 0.15
    assert PositionLimits.MAX_TOTAL_POSITIONS_TARGET == 10
    assert PositionLimits.MAX_MONTHLY_ORDERS_TARGET == 30


# ── GuardrailChecker ───────────────────────────────────────────────

def _checker(cash=21460, net_liq=238000, csp_liability=155000,
             open_positions=10, monthly_orders=40):
    return GuardrailChecker(
        net_liquidation=net_liq,
        cash=cash,
        buying_power=120000,
        open_positions=open_positions,
        monthly_orders=monthly_orders,
        csp_liability=csp_liability,
    )


def test_guardrail_stage_emergency_low_cash():
    """9% cash buffer → EMERGENCY stage."""
    checker = _checker(cash=21460, net_liq=238000)  # 9.0% cash
    assert checker.get_current_stage() == "EMERGENCY"


def test_guardrail_stage_target_medium_cash():
    """15% cash buffer → TARGET stage."""
    checker = _checker(cash=35700, net_liq=238000)  # 15% cash
    assert checker.get_current_stage() == "TARGET"


def test_guardrail_stage_comfort_high_cash():
    """25% cash buffer → COMFORT stage."""
    checker = _checker(cash=59500, net_liq=238000)  # 25% cash
    assert checker.get_current_stage() == "COMFORT"


def test_guardrail_cash_buffer_violation():
    """Low cash buffer should trigger CRITICAL violation."""
    checker = _checker(cash=21460, net_liq=238000)  # 9% cash
    violations = checker.check_all_guardrails()
    cash_violations = [v for v in violations if v.guardrail_type == "cash_buffer"]
    assert len(cash_violations) > 0
    assert cash_violations[0].severity == "CRITICAL"


def test_guardrail_csp_deployment_violation():
    """66% CSP deployment should trigger violation."""
    checker = _checker(csp_liability=155000, net_liq=238000)  # 65% deployed
    violations = checker.check_all_guardrails()
    csp_violations = [v for v in violations if v.guardrail_type == "csp_deployment"]
    assert len(csp_violations) > 0


def test_guardrail_position_concentration():
    """V at 77% should trigger concentration violation."""
    checker = _checker()
    positions = {
        'V': {'market_value': 185000, 'sector': 'Financials'},
    }
    violations = checker.check_all_guardrails(positions)
    conc_violations = [v for v in violations if v.guardrail_type == "position_concentration"]
    assert len(conc_violations) > 0


def test_guardrail_sector_concentration():
    """Financials at high concentration should trigger sector violation."""
    checker = _checker()
    positions = {
        'V': {'market_value': 185000, 'sector': 'Financials'},
        'JPM': {'market_value': 20000, 'sector': 'Financials'},
    }
    violations = checker.check_all_guardrails(positions)
    sector_violations = [v for v in violations if v.guardrail_type == "sector_concentration"]
    assert len(sector_violations) > 0


def test_guardrail_monthly_orders():
    """40 orders > 20 limit should trigger BLOCK."""
    checker = _checker(monthly_orders=40)
    violations = checker.check_all_guardrails()
    order_violations = [v for v in violations if v.guardrail_type == "monthly_orders"]
    assert len(order_violations) > 0
    assert order_violations[0].severity == "BLOCK"


def test_guardrail_healthy_portfolio():
    """Well-capitalized, low-deployment portfolio should have few violations."""
    checker = _checker(
        cash=71400, net_liq=238000,     # 30% cash
        csp_liability=30000,            # 12.6% deployed
        open_positions=5,
        monthly_orders=5,
    )
    violations = checker.check_all_guardrails()
    # Should only have cash_buffer at most (30% > 20% target → no cash violation)
    critical_or_block = [v for v in violations if v.severity in ("CRITICAL", "BLOCK")]
    assert len(critical_or_block) == 0


def test_guardrail_check_new_trade_blocks_over_limit_csp():
    """New CSP that exceeds deployment limit should be blocked."""
    checker = _checker(csp_liability=155000, net_liq=238000)
    allowed, violations = checker.check_new_trade(
        ticker="AAPL", strategy="CSP", notional=50000,
        sector="Technology",
    )
    assert allowed is False
    assert any(v.guardrail_type == "csp_deployment" for v in violations)


def test_guardrail_check_new_trade_allows_cc():
    """CC doesn't increase CSP liability — should be allowed on deployment check."""
    checker = _checker(csp_liability=30000, net_liq=238000, monthly_orders=2)
    positions = {
        'AAPL': {'market_value': 1200, 'sector': 'Technology'},
    }
    allowed, violations = checker.check_new_trade(
        ticker="AAPL", strategy="CC", notional=15000,
        sector="Technology", current_positions=positions,
    )
    # CC shouldn't trigger CSP deployment violation
    csp_violations = [v for v in violations if v.guardrail_type == "csp_deployment"]
    assert len(csp_violations) == 0


def test_guardrail_get_summary():
    """get_summary() should return all expected keys."""
    checker = _checker()
    summary = checker.get_summary()
    assert "stage" in summary
    assert "cash_buffer_pct" in summary
    assert "csp_deployment_pct" in summary
    assert "limits" in summary
    assert "current_values" in summary


# ── generate_option_decision_message ───────────────────────────────
# These tests mock check_thesis_quick to avoid live MoomooClient calls.

from unittest.mock import patch


_THESIS_INTACT = {'broken': False, 'damaged': False}
_THESIS_DAMAGED = {'broken': False, 'damaged': True}
_THESIS_BROKEN = {'broken': True, 'damaged': False}


def test_decision_expiring_soon():
    """DTE ≤ 3 → Expiring Soon message (thesis intact path)."""
    pos = {
        'days_to_expiry': 2, 'delta': 0.20, 'unrealized_pnl': 100,
        'option_type': 'PUT', 'ticker': 'AAPL',
    }
    with patch('src.portfolio.summary.check_thesis_quick', return_value=_THESIS_INTACT):
        msg = generate_option_decision_message(pos)
    assert "Expiring Soon" in msg


def test_decision_expiring_broken_thesis():
    """DTE ≤ 3 + thesis broken → auto-close."""
    pos = {
        'days_to_expiry': 2, 'delta': 0.20, 'unrealized_pnl': 100,
        'option_type': 'PUT', 'ticker': 'BE',
    }
    with patch('src.portfolio.summary.check_thesis_quick', return_value=_THESIS_BROKEN):
        msg = generate_option_decision_message(pos)
    assert "THESIS BROKEN" in msg


def test_decision_high_assignment_cc():
    """CC with |delta| ≥ 0.50 → Expected CC outcome."""
    pos = {
        'days_to_expiry': 30, 'delta': 0.55, 'unrealized_pnl': 200,
        'option_type': 'CALL', 'ticker': 'AAPL',
    }
    with patch('src.portfolio.summary.check_thesis_quick', return_value=_THESIS_INTACT):
        msg = generate_option_decision_message(pos)
    assert "Expected CC outcome" in msg


def test_decision_high_assignment_csp():
    """CSP with |delta| ≥ 0.50 → Expected CSP outcome."""
    pos = {
        'days_to_expiry': 30, 'delta': -0.60, 'unrealized_pnl': 200,
        'option_type': 'PUT', 'ticker': 'AAPL',
    }
    with patch('src.portfolio.summary.check_thesis_quick', return_value=_THESIS_INTACT):
        msg = generate_option_decision_message(pos)
    assert "Expected CSP outcome" in msg


def test_decision_underwater_thesis_intact():
    """Underwater but thesis intact → hold position."""
    pos = {
        'days_to_expiry': 30, 'delta': 0.25, 'unrealized_pnl': -500,
        'option_type': 'PUT', 'ticker': 'V',
    }
    with patch('src.portfolio.summary.check_thesis_quick', return_value=_THESIS_INTACT):
        msg = generate_option_decision_message(pos)
    assert "Thesis intact" in msg


def test_decision_underwater_thesis_broken():
    """Underwater + thesis broken → exit position."""
    pos = {
        'days_to_expiry': 30, 'delta': 0.25, 'unrealized_pnl': -500,
        'option_type': 'PUT', 'ticker': 'BE',
    }
    with patch('src.portfolio.summary.check_thesis_quick', return_value=_THESIS_BROKEN):
        msg = generate_option_decision_message(pos)
    assert "THESIS BROKEN" in msg
    assert "Exit Wheel" in msg


def test_decision_underwater_thesis_damaged():
    """Underwater + thesis damaged → monitor."""
    pos = {
        'days_to_expiry': 30, 'delta': 0.25, 'unrealized_pnl': -500,
        'option_type': 'PUT', 'ticker': 'AAPL',
    }
    with patch('src.portfolio.summary.check_thesis_quick', return_value=_THESIS_DAMAGED):
        msg = generate_option_decision_message(pos)
    assert "THESIS DAMAGED" in msg


def test_decision_on_track():
    """7-21 DTE, not underwater → on track."""
    pos = {
        'days_to_expiry': 14, 'delta': 0.20, 'unrealized_pnl': 100,
        'option_type': 'PUT', 'ticker': 'AAPL',
    }
    with patch('src.portfolio.summary.check_thesis_quick', return_value=_THESIS_INTACT):
        msg = generate_option_decision_message(pos)
    assert "On Track" in msg or "Weekly monitoring" in msg


def test_decision_long_term():
    """>21 DTE, not underwater → long-term monitoring."""
    pos = {
        'days_to_expiry': 45, 'delta': 0.20, 'unrealized_pnl': 200,
        'option_type': 'PUT', 'ticker': 'AAPL',
    }
    with patch('src.portfolio.summary.check_thesis_quick', return_value=_THESIS_INTACT):
        msg = generate_option_decision_message(pos)
    assert "Long-Term" in msg or "Monthly review" in msg


def test_decision_default():
    """Default case → no action required."""
    pos = {
        'days_to_expiry': 5, 'delta': 0.10, 'unrealized_pnl': 50,
        'option_type': 'PUT', 'ticker': 'AAPL',
    }
    with patch('src.portfolio.summary.check_thesis_quick', return_value=_THESIS_INTACT):
        msg = generate_option_decision_message(pos)
    assert "No action required" in msg


def test_decision_missing_ticker():
    """Missing ticker → should still work (no thesis check)."""
    pos = {
        'days_to_expiry': 30, 'delta': 0.20, 'unrealized_pnl': 100,
        'option_type': 'PUT', 'ticker': '',
    }
    msg = generate_option_decision_message(pos)
    assert isinstance(msg, str)
    assert len(msg) > 0
