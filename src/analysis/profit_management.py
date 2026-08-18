"""
Trend-Modulated Profit & Loss Management — the single decision core for option
profit booking, shared by the portfolio view, the screener, and the OIE paper
engine.

Spec: specs/profit-loss-management-spec.md

The asymmetry insight (the core of the spec):
  - CSP (short put) in an uptrend: the stock runs AWAY from the strike, decay
    accelerates, delta → 0. Trend HELPS the short put. → extend the 50% target.
  - CC (short call) in an uptrend: the stock runs INTO the strike, delta+gamma
    climb against you, upside is capped. Trend HURTS the short call. → never
    extend; instead roll up-and-out to keep shares and recapture upside.

Everything below is a pure function over inputs that the engine already computes
(TREND_COMPOSITE, SENTIMENT_SCORE, IV_RANK, DTE, delta, profit_captured, capital
scarcity). No I/O, no moomoo calls — callers assemble the inputs.

Authority: Tastytrade 50% rule (base), made conditional on regime per the spec's
evidence synthesis (playbook §3, §6).
"""

from dataclasses import dataclass, field
from typing import Optional

from src.config import get_config


# ── Inputs / outputs ──────────────────────────────────────────────

@dataclass
class TrendContext:
    """The signal stack the exit layer previously ignored.

    All fields optional — callers populate what they have. None → that gate
    is treated as "no signal" (does not block, does not extend).
    """
    trend_composite: Optional[float] = None    # 0-100
    sentiment_score: Optional[float] = None    # 0-100
    sentiment_direction: Optional[str] = None  # BULLISH/NEUTRAL/CAUTIOUS/BEARISH
    iv_rank: Optional[float] = None            # 0-100


@dataclass
class ProfitDecision:
    """The single source of truth for an option position's profit-side action."""
    strategy: str                       # 'CSP' | 'CC'
    target_pct: float                   # 50 / 70 / 85 (the level to act at)
    action: str                         # see ACTION_* constants
    reason: str                         # human-readable, names the gates that fired
    extended_by_trend: bool = False     # True if trend raised target above base 50%
    trend_context: Optional[TrendContext] = None
    capital_scarcity: Optional[str] = None  # SCARCE / NORMAL / ABUNDANT

    # Action vocabulary (stable strings callers switch on)
    ACTION_CLOSE = 'CLOSE'                  # book profit now, redeploy capital
    ACTION_HOLD = 'HOLD'                    # below target; keep collecting theta
    ACTION_ROLL_DOWN_OUT = 'ROLL_DOWN_OUT'  # CSP winner: roll to lower strike, +DTE, credit
    ACTION_ROLL_UP_OUT = 'ROLL_UP_OUT'      # CC winner: roll to higher strike, +DTE, credit
    ACTION_MANAGE_DTE = 'MANAGE_DTE'        # ≤ 21 DTE gamma floor: close/roll/assign


# ── Core decision ─────────────────────────────────────────────────

def decide_profit_target(
    strategy: str,
    profit_captured: float,
    dte: int,
    delta: float,
    trend_ctx: Optional[TrendContext] = None,
    capital_scarcity: Optional[str] = None,
    csp_paused: bool = False,
    emergency: bool = False,
) -> ProfitDecision:
    """Decide the profit-side action for one option position.

    Implements the spec §4.2 decision matrix in priority order — hard gates
    first, then strategy-direction-specific trend extension, then the base.

    Args:
        strategy: 'CSP' or 'CC'.
        profit_captured: % of max premium captured (positive = profit,
            negative = loss). -150 means a 1.5× loss.
        dte: days to expiry.
        delta: absolute delta of the position (sign-insensitive).
        trend_ctx: the trend/sentiment/IV stack (None → base 50% behavior).
        capital_scarcity: SCARCE / NORMAL / ABUNDANT (None → treated as NORMAL).
        csp_paused: when True, signals CSP redeployment is currently blocked
            (deployment % over limit). Combined with the
            ``bypass_scarce_when_csp_paused`` config flag, this skips GATE 2
            so trend extension can apply — the capital-velocity argument for
            booking at 50% collapses when freed capital has no CSP slot to
            redeploy into. Default False (no bypass).
        emergency: True when the guardrail recovery stage is EMERGENCY (cash
            buffer below critical or CSP deployment at emergency levels).
            EMERGENCY re-enables GATE 2 even when the deployment-aware bypass
            is on — booking profit to repair the balance sheet beats letting
            a winner ride on an unvalidated bypass. Default False.

    Returns:
        ProfitDecision naming the target level, action, and reason.

    Note: loss-side hard stops (premium multiples, critical delta) are NOT
    decided here — they live in holding_score._score_option and roll_first.
    This function owns the *profit* booking target; the loss-aware 2× trend
    overlay is exposed via ``loss_alert_should_hard_stop`` below.
    """
    cfg = get_config()
    strategy = 'CC' if strategy.upper() == 'CC' else 'CSP'
    trend_ctx = trend_ctx or TrendContext()
    scarcity = (capital_scarcity or 'NORMAL').upper()
    dte_floor = cfg.profit_take('dte_floor', 21)
    scarcity_override = str(cfg.profit_take('capital_scarcity_override', 'SCARCE')).upper()

    # ── GATE 1: DTE gamma floor (hard — overrides everything) ──
    if dte <= dte_floor:
        return ProfitDecision(
            strategy=strategy, target_pct=0.0, action=ProfitDecision.ACTION_MANAGE_DTE,
            reason=f"DTE {dte} ≤ {dte_floor} floor — manage today (gamma risk): "
                   f"close / roll (credit-only) / assign.",
            trend_context=trend_ctx, capital_scarcity=scarcity,
        )

    base = float(cfg.profit_take_csp('base_pct', 50) if strategy == 'CSP'
                 else cfg.profit_take_cc('base_pct', 50))

    # ── GATE 2: capital scarcity (overrides trend extension) ──
    # Deployment-aware bypass: when CSP redeployment is blocked (csp_paused=True)
    # AND the feature flag is enabled, skip this gate. The capital-velocity
    # argument for forcing 50% assumes the freed capital has somewhere better
    # to go — when no CSP slot is available, that assumption fails, so trend
    # extension should apply to qualifying CSPs.
    # EMERGENCY override: the bypass is UNVALIDATED (playbook §4-§6, backtest
    # pending). In an EMERGENCY recovery stage it never applies — repairing
    # the balance sheet (booking profit, freeing liability) outranks riding
    # an unvalidated extension. Config flag stays on for paper validation.
    bypass_enabled = bool(cfg.profit_take('bypass_scarce_when_csp_paused', False))
    bypass_active = bypass_enabled and bool(csp_paused) and not emergency
    if scarcity == scarcity_override and not bypass_active:
        note = " — EMERGENCY stage overrides SCARCE bypass" if (emergency and bypass_enabled and csp_paused) else ""
        return _close_at_base(strategy, base, profit_captured, trend_ctx, scarcity,
                              reason=f"Capital {scarcity} — book at base {base:.0f}% "
                                     f"(capital gate beats trend extension{note}).")

    # ── CSP: trend extension allowed (stock runs away from short put) ──
    if strategy == 'CSP':
        target, reason, extended = _csp_target(trend_ctx, cfg)
        return _resolve_csp(strategy, target, profit_captured, trend_ctx, scarcity,
                            extended, reason)

    # ── CC: trend extension NOT allowed (uptrend is the danger side) ──
    # Base 50% always, but a winner in an uptrend rolls up-and-out to keep shares.
    roll_up = bool(cfg.profit_take_cc('roll_up_out_on_trend', True))
    trend_min = float(cfg.profit_take_trend('trend_composite_min', 50))
    uptrend = (trend_ctx.trend_composite is not None
               and trend_ctx.trend_composite >= trend_min)
    if profit_captured >= base:
        if roll_up and uptrend:
            return ProfitDecision(
                strategy=strategy, target_pct=base,
                action=ProfitDecision.ACTION_ROLL_UP_OUT,
                reason=f"CC {profit_captured:.0f}% ≥ {base:.0f}% in uptrend "
                       f"(trend {trend_ctx.trend_composite:.0f}) — roll up-and-out "
                       f"for credit to keep shares + recapture upside; "
                       f"if no credit roll, close and hold shares unencumbered.",
                trend_context=trend_ctx, capital_scarcity=scarcity,
            )
        return _close_at_base(strategy, base, profit_captured, trend_ctx, scarcity,
                              reason=f"CC {profit_captured:.0f}% ≥ {base:.0f}% — close, "
                                     f"redeploy (no trend extension for short calls).")
    return ProfitDecision(
        strategy=strategy, target_pct=base, action=ProfitDecision.ACTION_HOLD,
        reason=f"CC {profit_captured:.0f}% < {base:.0f}% target — hold, collect theta.",
        trend_context=trend_ctx, capital_scarcity=scarcity,
    )


# ── CSP target resolution ─────────────────────────────────────────

def _csp_target(trend_ctx: TrendContext, cfg) -> tuple[float, str, bool]:
    """Pick the CSP profit target from the trend stack. Returns (target, reason, extended)."""
    if not bool(cfg.profit_take_csp('trend_extension_enabled', True)):
        base = float(cfg.profit_take_csp('base_pct', 50))
        return base, f"CSP trend extension disabled — base {base:.0f}%.", False

    tc = trend_ctx.trend_composite
    sent_ok = _sentiment_allowed(trend_ctx, cfg)
    ivr = trend_ctx.iv_rank
    ivr_ok = ivr is None or ivr >= float(cfg.profit_take_trend('iv_rank_min', 30))

    strong_min = float(cfg.profit_take_trend('strong_trend_composite_min', 70))
    trend_min = float(cfg.profit_take_trend('trend_composite_min', 50))
    strong_tgt = float(cfg.profit_take_csp('strong_trend_target_pct', 85))
    trend_tgt = float(cfg.profit_take_csp('trend_target_pct', 70))
    base = float(cfg.profit_take_csp('base_pct', 50))

    if tc is not None and tc >= strong_min and sent_ok and ivr_ok:
        return (strong_tgt,
                f"Strong trend (composite {tc:.0f} ≥ {strong_min}, sentiment ok, "
                f"IVR {ivr if ivr is not None else 'n/a'} ≥ {cfg.profit_take_trend('iv_rank_min', 30)}) "
                f"→ extend to {strong_tgt:.0f}%.", True)
    if tc is not None and tc >= trend_min and sent_ok:
        return (trend_tgt,
                f"Confirmed trend (composite {tc:.0f} ≥ {trend_min}, sentiment ok) "
                f"→ extend to {trend_tgt:.0f}%.", True)
    return (base,
            f"No trend extension (composite {tc if tc is not None else 'n/a'}, "
            f"sentiment {trend_ctx.sentiment_direction or 'n/a'}) → base {base:.0f}%.", False)


def _resolve_csp(strategy, target, profit_captured, trend_ctx, scarcity, extended, reason):
    """Turn a CSP target into an action given the current profit captured."""
    base = float(get_config().profit_take_csp('base_pct', 50))
    if profit_captured >= target:
        # Winner at target. In an extended trend, the higher-EV move is to roll
        # down-and-out for credit (bank the win, stay in the thesis) rather than
        # flat-close — but only if trend actually extended the target.
        if extended and profit_captured >= base:
            return ProfitDecision(
                strategy=strategy, target_pct=target,
                action=ProfitDecision.ACTION_ROLL_DOWN_OUT,
                reason=f"CSP {profit_captured:.0f}% ≥ {target:.0f}% trend target — "
                       f"roll down-and-out for credit (bank win, stay in trend); "
                       f"if no credit roll, close. {reason}",
                extended_by_trend=True, trend_context=trend_ctx, capital_scarcity=scarcity,
            )
        return ProfitDecision(
            strategy=strategy, target_pct=target, action=ProfitDecision.ACTION_CLOSE,
            reason=f"CSP {profit_captured:.0f}% ≥ {target:.0f}% target — close, redeploy. {reason}",
            extended_by_trend=extended, trend_context=trend_ctx, capital_scarcity=scarcity,
        )
    return ProfitDecision(
        strategy=strategy, target_pct=target, action=ProfitDecision.ACTION_HOLD,
        reason=f"CSP {profit_captured:.0f}% < {target:.0f}% target — hold, collect theta. {reason}",
        extended_by_trend=extended, trend_context=trend_ctx, capital_scarcity=scarcity,
    )


# ── Helpers ───────────────────────────────────────────────────────

def _close_at_base(strategy, base, profit_captured, trend_ctx, scarcity, reason):
    if profit_captured >= base:
        return ProfitDecision(
            strategy=strategy, target_pct=base, action=ProfitDecision.ACTION_CLOSE,
            reason=f"{strategy} {profit_captured:.0f}% ≥ {base:.0f}% — close, redeploy. {reason}",
            trend_context=trend_ctx, capital_scarcity=scarcity,
        )
    return ProfitDecision(
        strategy=strategy, target_pct=base, action=ProfitDecision.ACTION_HOLD,
        reason=f"{strategy} {profit_captured:.0f}% < {base:.0f}% target — hold. {reason}",
        trend_context=trend_ctx, capital_scarcity=scarcity,
    )


def _sentiment_allowed(trend_ctx: TrendContext, cfg) -> bool:
    """True if the sentiment direction is in the allow-list (or unknown)."""
    allow = cfg.profit_take_trend('sentiment_direction_allow', ['BULLISH', 'NEUTRAL'])
    allow = {str(a).upper() for a in (allow or [])}
    sd = (trend_ctx.sentiment_direction or '').upper()
    if not sd:
        return True  # no signal → don't block
    return sd in allow


# ── Loss-side: 2× premium trend overlay (spec §5.1) ───────────────

def loss_alert_should_hard_stop(
    trend_ctx: Optional[TrendContext],
    rolls_so_far: int,
) -> tuple[bool, str]:
    """At the 2× premium-loss alert, does trend tip it into a hard stop?

    Per spec §5.1: trend < hard_stop_min → treat as hard stop (close/roll now);
    trend ≥ hard_stop_min → allow ONE extra roll attempt, then forced decision.
    Trend NEVER overrides the 3×/critical-delta hard stop — those are decided
    upstream in holding_score and always win.

    Returns (treat_as_hard_stop, reason).
    """
    cfg = get_config()
    overlay = cfg.profit_take('loss_alert_trend_overlay', {}) or {}
    floor = float(overlay.get('trend_composite_hard_stop_min', 40))
    max_extra = int(overlay.get('max_extra_roll_attempts', 1))
    tc = (trend_ctx.trend_composite if trend_ctx else None)

    if tc is None:
        return True, "No trend data at 2× alert — treat as hard stop (defensive)."
    if tc < floor:
        return True, (f"Trend composite {tc:.0f} < {floor:.0f} at 2× premium alert — "
                      f"trend broken, treat as hard stop.")
    if rolls_so_far >= max_extra:
        return True, (f"Trend {tc:.0f} ≥ {floor:.0f} but already used {rolls_so_far} "
                      f"extra roll(s) (max {max_extra}) — forced decision.")
    return False, (f"Trend {tc:.0f} ≥ {floor:.0f} at 2× alert — one roll attempt allowed, "
                   f"then forced decision.")


# ── Convenience: build a TrendContext from an enriched snapshot ───

def trend_context_from_snapshot(snap, sentiment_score=None, sentiment_direction=None,
                                iv_rank=None) -> TrendContext:
    """Assemble a TrendContext from a StockSnapshot using the canonical composite.

    `snap` must already be enriched (have sma/rsi/macd fields). The trend
    composite comes from src.analysis.trend.trend_composite_from_snapshot —
    the SPECS §5.3 formula (0.5×alignment + 0.3×ADX + 0.2×momentum). Returns
    None on missing anchors (short/absent history) so the profit gates treat
    it as "no signal" instead of extending on fake data.
    """
    tc = None
    try:
        from src.analysis.trend import trend_composite_from_snapshot
        tc = trend_composite_from_snapshot(snap)
    except Exception:
        tc = None
    return TrendContext(
        trend_composite=tc,
        sentiment_score=sentiment_score,
        sentiment_direction=sentiment_direction,
        iv_rank=iv_rank,
    )
