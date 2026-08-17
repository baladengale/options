"""Config-driven gate plumbing — the 2026-08-16 drift-fix as a regression test.

Before this fix, several GOAL.md checks were hardcoded or dead:
  - `buying_power = cash * 2` in the OIE engine (margin extended CSP coverage,
    violating GOAL #4 "100% cash-secured")
  - engine per-cycle cap hardcoded `min(2, ...)`
  - CSP pause helper implemented 3 of 5 triggers and had zero callers
  - deep-ITM delta 0.70 / VRP 0.8 / cash gate 0.8 / earnings 14 / spread 5.0
    literals scattered in code
  - churn caps + same-strike cooldown + volatile CSP cap + CC-below-basis were
    dead config keys

These tests pin the wiring: every gate reads from config/rules.yaml.
"""

from dataclasses import dataclass

import pytest

from src.config import get_config


@dataclass
class FakeContract:
    bid: float = 5.0
    delta: float = -0.25
    implied_vol: float = 40.0
    open_interest: int = 1000
    volume: int = 100
    dte: int = 40
    strike: float = 100.0
    option_type: str = 'PUT'
    iv_rank: float = None


@dataclass
class FakeSnap:
    last_price: float = 100.0
    hv_30d: float = None


@pytest.fixture(scope='module')
def cfg():
    return get_config()


# ── Config keys exist and are consumed ────────────────────────────

def test_new_config_keys_present(cfg):
    assert cfg.max_new_positions_per_cycle == 2
    assert 0 < cfg.csp_single_cash_fraction <= 1.0
    assert 0 < cfg.delta_deep_itm_max <= 1.0
    assert 0 < cfg.vrp_hv_factor_min <= 1.0
    assert cfg.iv_rank_min == 30
    assert cfg.cc_never_sell_below_basis is True


def test_config_fallbacks_match_yaml(cfg):
    """src/config.py fallback defaults must equal the rules.yaml values —
    the fallback is the safety net when a YAML key is missing."""
    assert cfg.cash_buffer_warn == 0.15
    assert cfg.stop_delta_csp_decision == 0.50
    assert cfg.stop_delta_cc_warn == 0.50


# ── CSP pause: all five triggers ──────────────────────────────────

def test_csp_pause_vix_trigger(cfg):
    paused, reasons = cfg.should_pause_csp(vix=26.0, regime_score=1,
                                           cash_reserve_pct=0.5)
    assert paused and any('VIX' in r for r in reasons)


def test_csp_pause_spy_below_sma_trigger(cfg):
    paused, reasons = cfg.should_pause_csp(vix=10.0, regime_score=1,
                                           cash_reserve_pct=0.5,
                                           spy_price=500.0, spy_sma=550.0)
    assert paused and any('SPY' in r for r in reasons)


def test_csp_pause_spy_above_sma_does_not_fire(cfg):
    paused, _ = cfg.should_pause_csp(vix=10.0, regime_score=1,
                                     cash_reserve_pct=0.5,
                                     spy_price=560.0, spy_sma=550.0)
    assert not paused


def test_csp_pause_regime_score_trigger(cfg):
    paused, reasons = cfg.should_pause_csp(vix=10.0, regime_score=-2,
                                           cash_reserve_pct=0.5)
    assert paused and any('Regime' in r for r in reasons)


def test_csp_pause_cash_reserve_trigger(cfg):
    paused, reasons = cfg.should_pause_csp(vix=10.0, regime_score=1,
                                           cash_reserve_pct=0.15)
    assert paused and any('Cash reserve' in r for r in reasons)


def test_csp_pause_no_trigger_when_healthy(cfg):
    paused, reasons = cfg.should_pause_csp(vix=15.0, regime_score=1,
                                           cash_reserve_pct=0.5,
                                           spy_price=600.0, spy_sma=550.0)
    assert not paused and reasons == []


def test_csp_pause_unknown_data_does_not_fire(cfg):
    """Data-blind triggers must NOT block (vix=None passed as -1, macro absent)."""
    paused, _ = cfg.should_pause_csp(vix=-1.0, regime_score=+1,
                                     cash_reserve_pct=0.5)
    assert not paused


# ── Contract filters: thresholds from config ──────────────────────

def test_deep_itm_delta_cap_from_config(cfg):
    from src.filters.contract_filters import passes_delta

    @dataclass
    class WideCfg:
        """Regime range wide open so ONLY the deep-ITM cap can fail."""
        delta_deep_itm_max: float = cfg.delta_deep_itm_max

        def delta_range(self, strategy, regime):
            return [0.0, 0.99]

    c = FakeContract(delta=-0.75)
    ok, reason = passes_delta(c, 'CSP', 'NEUTRAL', cfg=WideCfg())
    assert not ok and 'deep ITM' in reason
    # just under the cap passes with the wide range
    ok, reason = passes_delta(FakeContract(delta=-0.65), 'CSP', 'NEUTRAL',
                              cfg=WideCfg())
    assert ok


def test_vrp_factor_from_config():
    from src.filters.contract_filters import passes_vrp
    c = FakeContract(implied_vol=39.0)
    # IV 39 vs HV 50 × 0.8 = 40 → fails VRP with default factor
    assert not passes_vrp(c, hv_30d=50.0)
    assert passes_vrp(c, hv_30d=45.0)


def test_cash_gate_fraction_from_config():
    from src.filters.contract_filters import passes_cash_buffer
    # capital 90 vs BP 100 × 0.80 = 80 → blocked with default fraction
    assert not passes_cash_buffer(capital=90, cash=50, net_liq=500,
                                  buying_power=100)
    assert passes_cash_buffer(capital=70, cash=50, net_liq=500,
                              buying_power=100)


def test_iv_rank_gate_known_low_ivr_rejects():
    from src.filters.contract_filters import passes_all_gates
    c = FakeContract(iv_rank=20.0)  # below options.iv_rank_min (30)
    ok, reason = passes_all_gates(c, 'CSP', 'NEUTRAL', FakeSnap(),
                                  net_liq=0, skip_concentration=True,
                                  skip_cash_buffer=True)
    assert not ok and 'IVR' in reason


def test_iv_rank_gate_unknown_ivr_passes_by_default():
    from src.filters.contract_filters import passes_all_gates
    c = FakeContract(iv_rank=None)
    ok, _ = passes_all_gates(c, 'CSP', 'NEUTRAL', FakeSnap(),
                             net_liq=0, skip_concentration=True,
                             skip_cash_buffer=True)
    assert ok


# ── Guardrails: regime-aware volatile CSP cap ─────────────────────

def _positions(csp_liability=20_000):
    return [{'ticker': 'TEST', 'notional': 50_000, 'sector': 'Technology',
             'csp_liability': csp_liability, 'strategy': 'CSP'}]


def test_volatile_regime_tightens_csp_cap():
    from src.data.guardrails import GuardrailChecker
    # 20% deployment: fine at the 25% normal cap, BLOCKED at the 10% volatile cap
    normal = GuardrailChecker(net_liq=100_000, cash=50_000, buying_power=50_000,
                              open_positions=_positions())
    assert not any('CSP capital deployed' in b for b in normal.check().blocks)

    volatile = GuardrailChecker(net_liq=100_000, cash=50_000, buying_power=50_000,
                                open_positions=_positions(), regime='VOLATILE')
    blocks = volatile.check().blocks
    assert any('CSP capital deployed' in b and '10%' in b for b in blocks)


# ── Engine: no more hardcoded buying power / cycle cap ────────────

def test_engine_source_has_no_cash_times_two():
    src = open('scripts/oie_engine.py').read()
    assert 'cash * 2' not in src, "buying_power = cash * 2 must not reappear"
    assert 'buying_power=cash + fund' in src


def test_engine_cycle_cap_reads_config():
    src = open('scripts/oie_engine.py').read()
    assert 'min(self.cfg.max_new_positions_per_cycle,' in src
    assert 'min(2,' not in src


def test_engine_uses_config_dte_and_spread():
    src = open('scripts/oie_engine.py').read()
    assert 'dte_min=self.cfg.dte_screen_min' in src
    assert 'self.cfg.spread_max_pct' in src
    assert 'self.cfg.csp_single_cash_fraction' in src


# ── Paper DB: churn + cooldown queries ────────────────────────────

@pytest.fixture()
def db(tmp_path):
    from src.data.oie_db import OIEDB
    d = OIEDB(str(tmp_path / 'test.db'))
    d.seed_portfolio({}, 50_000, 0)
    yield d
    d.close()


def test_monthly_profit_closes_counts_only_profit_reasons(db):
    pid = db.open_position('TEST', 'PUT', qty=-1, cost_price=100, strike=100,
                           expiry='2030-01-01', dte=30, entry_premium=5.0,
                           cash_impact=500)
    db.close_position(pid, 2.5, 'CLOSE_50PCT', cash_impact=-250)
    assert db.get_monthly_profit_closes('TEST') == 1
    assert db.get_monthly_profit_closes('OTHER') == 0


def test_same_strike_cooldown(db):
    pid = db.open_position('TEST', 'PUT', qty=-1, cost_price=100, strike=100,
                           expiry='2030-01-01', dte=30, entry_premium=5.0,
                           cash_impact=500)
    db.close_position(pid, 2.5, 'CLOSE_50PCT', cash_impact=-250)
    assert db.get_last_exit_within_days('TEST', 'PUT', 100.0, days=14)
    assert not db.get_last_exit_within_days('TEST', 'PUT', 105.0, days=14)
    assert not db.get_last_exit_within_days('OTHER', 'PUT', 100.0, days=14)


def test_cash_single_writer_invariant_after_cycle_actions(db):
    """seeded_cash + Σ cash_change == stored cash after a full open/close."""
    seeded = float(db.get_state('seeded_cash'))
    pid = db.open_position('TEST', 'PUT', qty=-1, cost_price=100, strike=100,
                           expiry='2030-01-01', dte=30, entry_premium=5.0,
                           cash_impact=500)
    db.close_position(pid, 2.5, 'CLOSE_50PCT', cash_impact=-250)
    flows = db._conn.execute(
        "SELECT COALESCE(SUM(cash_change),0) t FROM paper_trades").fetchone()['t']
    assert float(db.get_state('cash')) == seeded + flows == 50_000 + 500 - 250


def test_daily_new_count_counts_engine_opens_today(db):
    """A trade opened now (local) must count for the ET trading day."""
    db.open_position('TEST', 'PUT', qty=-1, cost_price=100, strike=100,
                     expiry='2030-01-01', dte=30, entry_premium=5.0,
                     cash_impact=500)
    assert db.get_daily_new_count() == 1
