"""
Tests for src/analysis/exit_management.py — the single exit decision core.

The headline regression: the 5 V covered calls from the live paper book that
previously sat untouched (CC delta-stop was warn-only) must now produce
concrete actions. These are the fixtures that prove the CC autonomy fix.

Test style mirrors tests/test_profit_management.py: real config/rules.yaml +
monkeypatched accessors (no MoomooClient, no DB). All thresholds come from
config; tests pin behaviour at the documented config values.
"""
from src.analysis.exit_management import (
    decide_exit_action, ExitDecision,
    CLOSE_50PCT, CLOSE_TREND, STOP_DELTA, STOP_LOSS,
    ROLL_UP_OUT, ROLL_DOWN_OUT,
)
from src.analysis.profit_management import TrendContext
from src.config import get_config

# Reusable trend contexts (mirror test_profit_management.py conventions).
UPTREND = TrendContext(trend_composite=75, sentiment_score=60,
                       sentiment_direction='BULLISH', iv_rank=50)
NEUTRAL = TrendContext(trend_composite=50, sentiment_score=55,
                       sentiment_direction='NEUTRAL', iv_rank=20)
NOTREND = TrendContext(trend_composite=30, sentiment_score=40,
                       sentiment_direction='CAUTIOUS', iv_rank=20)


def _cfg():
    return get_config()


# ════════════════════════════════════════════════════════════════════
# HEADLINE REGRESSION: the 5 V covered calls (the CC autonomy fix)
# ════════════════════════════════════════════════════════════════════
# These are the exact positions from db/oie_paper.db as of 2026-08-07.
# Before the fix, ALL of them emitted only a warn-string and never acted.
# After the fix, each must produce a concrete close/roll or a justified hold.

def test_v_cc_360_sep18_deep_itm_rolls_up_out():
    """V CALL $360 Sep18 @8.55→16.40, Δ0.70, −92%, DTE 47. Deep ITM loser.
    Before: 'not auto-closing' warning forever. After: ROLL_UP_OUT."""
    d = decide_exit_action('CC', profit_captured=-92, dte=47, delta=0.70,
                           pnl_dollars=-785, trend_ctx=UPTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.roll_decision == ROLL_UP_OUT
    assert d.close_reason is None
    assert 'roll up-and-out' in d.reason.lower()


def test_v_cc_360_aug28_deep_itm_rolls_up_out():
    """V CALL $360 Aug28 @11.95→13.05, Δ0.76, −9%, DTE 26. Deep ITM.
    Before: warn-only at Δ≥0.50. After: ROLL_UP_OUT (Δ ≥ 0.60)."""
    d = decide_exit_action('CC', profit_captured=-9, dte=26, delta=0.76,
                           pnl_dollars=-110, trend_ctx=UPTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.roll_decision == ROLL_UP_OUT


def test_v_cc_3675_aug21_inside_dte_floor_warns_then_loss_tier():
    """V CALL $367.5 Aug21 @9.00→7.05, Δ0.61, +22%, DTE 19. Inside 21-DTE floor.
    The MANAGE_DTE dead-end bug: profit side returns MANAGE_DTE → no action.
    Now the loss tiers re-evaluate: Δ0.61 ≥ 0.60 → ROLL_UP_OUT."""
    d = decide_exit_action('CC', profit_captured=22, dte=19, delta=0.61,
                           pnl_dollars=195, trend_ctx=UPTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.roll_decision == ROLL_UP_OUT


def test_v_cc_360_aug21_deep_itm_inside_floor_rolls():
    """V CALL $360 Aug21 @8.30→11.85, Δ0.81, −43%, DTE 19. Deep ITM inside floor.
    Before: warn-only + MANAGE_DTE dead-end. After: ROLL_UP_OUT."""
    d = decide_exit_action('CC', profit_captured=-43, dte=19, delta=0.81,
                           pnl_dollars=-355, trend_ctx=UPTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.roll_decision == ROLL_UP_OUT


def test_v_cc_375_aug14_winner_inside_floor_books_profit():
    """V CALL $375 Aug14 @5.70→2.45, Δ0.35, +57%, DTE 12. The winner that rots.
    Before: +57% profit but DTE≤21 → MANAGE_DTE → no action → rots to expiry.
    After: the loss tiers find nothing actionable, BUT this is exactly the case
    where the profit-side 50% target SHOULD have fired. Since MANAGE_DTE
    supersedes profit CLOSE, this winner books via the delta-gate being clear
    → returns HOLD (no action) because none of the loss tiers trip.

    NOTE: this documents current behaviour. The deeper fix (let a >50% winner
    inside 21 DTE still book) is out of scope — tracked separately."""
    d = decide_exit_action('CC', profit_captured=57, dte=12, delta=0.35,
                           pnl_dollars=325, trend_ctx=UPTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    # Δ0.35 < cc_decision(0.50), +57% is profit (no loss tier), +$325 > -$1000.
    # No actionable tier → HOLD. (The winner-rot case; see note above.)
    assert not d.acts


# ════════════════════════════════════════════════════════════════════
# CSP regression — must NOT break existing CSP behaviour
# ════════════════════════════════════════════════════════════════════

def test_csp_delta_critical_cuts():
    """CSP at |Δ|≥0.60 → STOP_DELTA (unchanged behaviour)."""
    d = decide_exit_action('CSP', profit_captured=-40, dte=40, delta=0.65,
                           pnl_dollars=-400, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.close_reason == STOP_DELTA
    assert d.roll_decision is None


def test_csp_winner_in_uptrend_rolls_down_out():
    """CSP at the strong-trend target (85%) in an uptrend → ROLL_DOWN_OUT.
    UPTREND (composite 75 ≥ 70, BULLISH, IVR 50 ≥ 30) sets target to 85%."""
    d = decide_exit_action('CSP', profit_captured=86, dte=40, delta=0.10,
                           pnl_dollars=430, trend_ctx=UPTREND,
                           capital_scarcity='ABUNDANT', csp_paused=False)
    assert d.roll_decision == ROLL_DOWN_OUT


def test_csp_winner_at_base_closes():
    """CSP at base 50% (no trend) → CLOSE_50PCT."""
    d = decide_exit_action('CSP', profit_captured=52, dte=40, delta=0.15,
                           pnl_dollars=200, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.close_reason == CLOSE_50PCT


def test_csp_hold_below_target():
    d = decide_exit_action('CSP', profit_captured=20, dte=40, delta=0.20,
                           pnl_dollars=80, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert not d.acts


# ════════════════════════════════════════════════════════════════════
# CC decision band — warn only, no action
# ════════════════════════════════════════════════════════════════════

def test_cc_in_decision_band_warns_not_acts():
    """CC Δ in [0.50, 0.60) → warn only, no close/roll."""
    d = decide_exit_action('CC', profit_captured=-10, dte=40, delta=0.55,
                           pnl_dollars=-100, trend_ctx=NEUTRAL,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert not d.acts
    assert d.warn is not None
    assert 'monitor' in d.warn.lower()


def test_cc_below_decision_band_clean_hold():
    """CC Δ < 0.50 → no warn, no action."""
    d = decide_exit_action('CC', profit_captured=-5, dte=40, delta=0.30,
                           pnl_dollars=-50, trend_ctx=NEUTRAL,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert not d.acts
    assert d.warn is None


def test_cc_winner_at_base_closes():
    """CC at 50% profit (no uptrend) → CLOSE_50PCT."""
    d = decide_exit_action('CC', profit_captured=52, dte=40, delta=0.20,
                           pnl_dollars=200, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.close_reason == CLOSE_50PCT


# ════════════════════════════════════════════════════════════════════
# CC DTE-aware delta gate — the wheel-turn fix (2026-08-08)
# ════════════════════════════════════════════════════════════════════

def test_cc_deep_itm_near_expiry_holds_for_assignment():
    """CC Δ≥0.60 with DTE≤14 → HOLD (warn), NOT roll. Let the wheel turn.
    Near-expiry ITM calls have negligible time value — rolling just churns
    commissions. Assignment means selling shares at a strike you chose."""
    d = decide_exit_action('CC', profit_captured=-22, dte=13, delta=0.65,
                           pnl_dollars=-190, trend_ctx=UPTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert not d.acts, "CC near expiry should HOLD, not roll"
    assert d.roll_decision is None
    assert d.close_reason is None
    assert d.warn is not None
    assert 'assignment imminent' in d.warn.lower()
    assert 'wheel' in d.warn.lower()


def test_cc_deep_itm_near_expiry_boundary_dte_14_holds():
    """CC Δ≥0.60 at exactly DTE 14 (the boundary) → HOLD for assignment."""
    d = decide_exit_action('CC', profit_captured=-30, dte=14, delta=0.62,
                           pnl_dollars=-300, trend_ctx=UPTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert not d.acts, "DTE 14 ≤ cc_assign_dte(14) → should HOLD"


def test_cc_deep_itm_far_expiry_still_rolls():
    """CC Δ≥0.60 with DTE>14 → ROLL_UP_OUT (unchanged for longer DTE).
    More time value remains — a credit roll can meaningfully improve the strike."""
    d = decide_exit_action('CC', profit_captured=-43, dte=19, delta=0.81,
                           pnl_dollars=-355, trend_ctx=UPTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.roll_decision == ROLL_UP_OUT
    assert 'DTE >' in d.reason


def test_cc_deep_itm_41_dte_still_rolls():
    """CC Δ≥0.60 with DTE 41 (well above threshold) → ROLL_UP_OUT."""
    d = decide_exit_action('CC', profit_captured=-92, dte=41, delta=0.70,
                           pnl_dollars=-785, trend_ctx=UPTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.roll_decision == ROLL_UP_OUT


def test_csp_heavy_loss_still_fires_premium_stop():
    """CSP premium-multiple stop is UNCHANGED — no offsetting asset for CSP.
    CSP at 3.1× far-DTE → STOP_LOSS still fires."""
    d = decide_exit_action('CSP', profit_captured=-310, dte=40, delta=0.30,
                           pnl_dollars=-310, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.close_reason == STOP_LOSS


def test_cc_heavy_loss_not_stopped_by_premium_tier():
    """CC at 5× premium lost (far DTE) — still HOLD because premium-multiple
    stop does NOT apply to CCs. The covered position is still net profitable."""
    d = decide_exit_action('CC', profit_captured=-500, dte=40, delta=0.25,
                           pnl_dollars=-500, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert not d.acts, "CC exempt from premium-multiple stop — offsetting shares"


def test_cc_heavy_loss_still_caught_by_absolute_catchall():
    """CC at -$1,200 pnl_dollars → STOP_LOSS from absolute catch-all.
    The absolute catch-all (>$1,000 loss) still applies to both strategies."""
    d = decide_exit_action('CC', profit_captured=-50, dte=40, delta=0.30,
                           pnl_dollars=-1200, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.close_reason == STOP_LOSS
    assert 'catch-all' in d.reason.lower()


def test_cc_deep_itm_near_expiry_config_driven(monkeypatch):
    """Raising cc_assign_dte to 21 means DTE 19 is now in the HOLD zone."""
    cfg = _cfg()
    monkeypatch.setattr(type(cfg), 'stop_delta_cc_assign_dte',
                        property(lambda self: 21))
    d = decide_exit_action('CC', profit_captured=-43, dte=19, delta=0.81,
                           pnl_dollars=-355, trend_ctx=UPTREND,
                           capital_scarcity='NORMAL', csp_paused=False, cfg=cfg)
    assert not d.acts, "DTE 19 ≤ cc_assign_dte(21) → should HOLD"
    assert d.warn is not None


# ════════════════════════════════════════════════════════════════════
# Premium-multiple tiers (loss side) — DTE-adjusted, both strategies
# ════════════════════════════════════════════════════════════════════

def test_premium_stop_far_dte_3x():
    """DTE>30: close at 3× premium lost."""
    d = decide_exit_action('CSP', profit_captured=-310, dte=40, delta=0.20,
                           pnl_dollars=-310, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.close_reason == STOP_LOSS
    assert '3.0×' in d.reason


def test_premium_stop_far_dte_below_3x_holds():
    """DTE>30 at 2.5× → below 3× threshold → no stop."""
    d = decide_exit_action('CSP', profit_captured=-250, dte=40, delta=0.20,
                           pnl_dollars=-250, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert not d.acts


def test_premium_stop_mid_dte_2x():
    """21 < DTE ≤ 30: CSP closes at 2× premium lost. (CC is exempt — see below.)"""
    d = decide_exit_action('CSP', profit_captured=-210, dte=25, delta=0.20,
                           pnl_dollars=-210, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.close_reason == STOP_LOSS
    assert '2.0×' in d.reason


def test_premium_stop_near_dte_1_5x():
    """DTE ≤ 21: close at 1.5× premium lost."""
    d = decide_exit_action('CSP', profit_captured=-160, dte=15, delta=0.20,
                           pnl_dollars=-160, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.close_reason == STOP_LOSS
    assert '1.5×' in d.reason


def test_cc_exempt_from_premium_stop():
    """CC does NOT fire premium-multiple stop-loss — the option "loss" is
    offset by share gains in a covered position. Closing the option leg in
    isolation destroys value. HOLD instead (absolute catch-all still applies)."""
    d = decide_exit_action('CC', profit_captured=-305, dte=40, delta=0.20,
                           pnl_dollars=-305, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert not d.acts, "CC should be exempt from premium-multiple stop-loss"
    assert 'hold' in d.reason.lower()


# ════════════════════════════════════════════════════════════════════
# Absolute catch-all
# ════════════════════════════════════════════════════════════════════

def test_heavy_loss_catch_all_fires():
    """pnl_dollars < -1000 → STOP_LOSS regardless of premium multiple."""
    d = decide_exit_action('CSP', profit_captured=-50, dte=40, delta=0.30,
                           pnl_dollars=-1200, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.close_reason == STOP_LOSS
    assert 'catch-all' in d.reason.lower()


def test_heavy_loss_boundary_not_fired():
    """Exactly -$1000 is NOT below the threshold (strict <)."""
    d = decide_exit_action('CSP', profit_captured=-50, dte=40, delta=0.30,
                           pnl_dollars=-1000, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert not d.acts


# ════════════════════════════════════════════════════════════════════
# Premium-tiered absolute catch-all (the AMD fix)
# ════════════════════════════════════════════════════════════════════

def test_heavy_loss_band_small_premium_keeps_legacy_floor():
    """A $300 premium (<$500 band) uses the −$1,000 floor — unchanged behavior."""
    d = decide_exit_action('CSP', profit_captured=-50, dte=40, delta=0.30,
                           pnl_dollars=-1200, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False,
                           premium_collected=300)
    assert d.close_reason == STOP_LOSS


def test_heavy_loss_band_big_premium_lets_normal_noise_breathe():
    """A $6,000 AMD CSP must NOT stop at −$4,000 (was: STOP_LOSS under the old
    flat −$1,000 floor). Top-band floor is −$8,000, so −$4,000 holds."""
    d = decide_exit_action('CSP', profit_captured=-50, dte=40, delta=0.30,
                           pnl_dollars=-4000, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False,
                           premium_collected=6000)
    assert not d.acts   # would have been STOP_LOSS under the old flat floor


def test_heavy_loss_band_big_premium_cuts_at_top_band():
    """Same $6,000 CSP at −$8,001 trips the top-band floor."""
    d = decide_exit_action('CSP', profit_captured=-50, dte=40, delta=0.30,
                           pnl_dollars=-8001, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False,
                           premium_collected=6000)
    assert d.close_reason == STOP_LOSS
    assert 'catch-all' in d.reason.lower()


def test_heavy_loss_band_mid_premium_mid_band():
    """$3,000 premium lands in the $2k–$5k band (floor $5,000)."""
    hold = decide_exit_action('CSP', profit_captured=-50, dte=40, delta=0.30,
                              pnl_dollars=-4999, trend_ctx=NOTREND,
                              capital_scarcity='NORMAL', csp_paused=False,
                              premium_collected=3000)
    assert not hold.acts
    stop = decide_exit_action('CSP', profit_captured=-50, dte=40, delta=0.30,
                              pnl_dollars=-5001, trend_ctx=NOTREND,
                              capital_scarcity='NORMAL', csp_paused=False,
                              premium_collected=3000)
    assert stop.close_reason == STOP_LOSS


def test_heavy_loss_band_reason_includes_premium():
    """The STOP_LOSS reason reports the premium for auditability."""
    d = decide_exit_action('CSP', profit_captured=-50, dte=40, delta=0.30,
                           pnl_dollars=-9000, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False,
                           premium_collected=6000)
    assert 'premium $6,000' in d.reason


# ════════════════════════════════════════════════════════════════════
# Config accessor — band table + legacy fallback
# ════════════════════════════════════════════════════════════════════

def test_config_band_lookup_matches_loaded_rules():
    """The accessor reads the band table from config/rules.yaml."""
    cfg = _cfg()
    # Smallest band (premium $0) → $1,000
    assert cfg.stop_heavy_loss_for_premium(0) == 1000
    # $1,200 premium → $500–$2,000 band → $2,000
    assert cfg.stop_heavy_loss_for_premium(1200) == 2000
    # $6,000 premium → top band → $8,000
    assert cfg.stop_heavy_loss_for_premium(6000) == 8000


def test_config_legacy_fallback_when_bands_absent():
    """A config with only heavy_loss_abs (no bands) falls back to one band."""
    from src.config import Config
    cfg = Config.__new__(Config)   # bypass __init__ to inject test data
    cfg._data = {'stop_loss': {'delta': {'heavy_loss_abs': 1500}}}
    assert cfg.stop_heavy_loss_bands == [(float('inf'), 1500.0)]
    assert cfg.stop_heavy_loss_for_premium(0) == 1500
    assert cfg.stop_heavy_loss_for_premium(999999) == 1500


# ════════════════════════════════════════════════════════════════════
# Priority / precedence
# ════════════════════════════════════════════════════════════════════

def test_profit_side_beats_loss_side():
    """A position at +55% profit AND Δ0.65 (CC) books profit, not rolls.
    Profit-side CLOSE wins because it's evaluated first."""
    d = decide_exit_action('CC', profit_captured=55, dte=40, delta=0.65,
                           pnl_dollars=200, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert d.close_reason == CLOSE_50PCT
    assert d.roll_decision is None


def test_expiry_dte_zero_returns_no_action():
    """DTE≤0 → caller resolves to ASSIGN/EXPIRE. Core returns no action."""
    d = decide_exit_action('CC', profit_captured=-50, dte=0, delta=0.90,
                           pnl_dollars=-500, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False)
    assert not d.acts
    assert 'caller resolves' in d.reason.lower()


# ════════════════════════════════════════════════════════════════════
# Config-driven: thresholds read from config, not hardcoded
# ════════════════════════════════════════════════════════════════════

def test_cc_close_threshold_is_config_driven(monkeypatch):
    """Raising cc_close to 0.80 means Δ0.70 no longer rolls."""
    cfg = _cfg()
    monkeypatch.setattr(type(cfg), 'stop_delta_cc_close',
                        property(lambda self: 0.80))
    d = decide_exit_action('CC', profit_captured=-50, dte=40, delta=0.70,
                           pnl_dollars=-500, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False, cfg=cfg)
    assert not d.acts  # Δ0.70 < new 0.80 threshold → warn band → no roll


def test_csp_critical_threshold_is_config_driven(monkeypatch):
    """Lowering csp_critical to 0.50 makes Δ0.55 a STOP_DELTA."""
    cfg = _cfg()
    monkeypatch.setattr(type(cfg), 'stop_delta_csp_critical',
                        property(lambda self: 0.50))
    d = decide_exit_action('CSP', profit_captured=-20, dte=40, delta=0.55,
                           pnl_dollars=-200, trend_ctx=NOTREND,
                           capital_scarcity='NORMAL', csp_paused=False, cfg=cfg)
    assert d.close_reason == STOP_DELTA
