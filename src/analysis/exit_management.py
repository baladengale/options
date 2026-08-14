"""
Exit Management — the single decision core for option EXITS (close / roll /
stop / assign) in the OIE paper engine.

This composes the profit-side decision (``profit_management.decide_profit_target``)
with the loss-side hard stops (delta gates + premium-multiple tiers + absolute
catch-all) into one structured ``ExitDecision``. Callers (``oie_engine.run_cycle``)
switch on the returned labels to dispatch DB operations.

Spec: specs/profit-loss-management-spec.md

Design — the two sides and why they're separate-but-composed:
  - PROFIT side (Tastytrade 50% rule, trend-modulated): owned by
    ``profit_management.decide_profit_target``. CSP can extend to 70/85% in an
    uptrend (stock runs away from strike); CC never extends but rolls
    up-and-out to keep shares. This module delegates to it unchanged.
  - LOSS side (hard stops): owned HERE. The previous engine inlined these
    tiers with hardcoded thresholds and a CC delta-stop that only warned — so
    a deep-ITM short call could bleed indefinitely. This module makes both
    sides config-driven, symmetric, and unit-testable.

CC delta gate — DTE-aware (the wheel-turn fix, 2026-08-08):
  A deep-ITM covered call near expiry is NOT a risk — it's the wheel doing
  exactly what it was designed to do. Previously Δ ≥ 0.60 unconditionally
  returned ROLL_UP_OUT, which churned near-expiry ITM CCs unnecessarily.
  Now the CC delta gate is DTE-aware:
    - DTE ≤ ``stop_delta_cc_assign_dte`` (14): HOLD + warn — let the wheel
      turn. Shares get called at a strike you chose. Assignment is profit.
    - DTE > 14: ROLL_UP_OUT (net-credit-only) — enough time value remains
      for a credit roll to make sense.
  CSP is unchanged: Δ ≥ 0.60 → ``STOP_DELTA`` (cut it — no offsetting asset).

CC premium-multiple exemption:
  Premium-multiple stop-loss does NOT apply to CCs. The option "loss" on a
  covered call is offset by share gains — closing the option leg in
  isolation destroys value. CSP premium stops are unchanged (no offsetting
  asset). The absolute catch-all (>$1,000 loss) still applies to both.

Authority: config is the single source of truth — no thresholds are hardcoded
here. Spec §4.2 (decision matrix), §5.1 (loss alerts), §7.2 (gamma-zone).
"""

from dataclasses import dataclass
from typing import Optional

from src.config import get_config
from src.analysis.profit_management import (
    TrendContext, ProfitDecision, decide_profit_target,
)


# ── Close / roll vocabulary (stable strings the engine switches on) ──
# Close reasons ---------------------------------------------------------------
CLOSE_50PCT = 'CLOSE_50PCT'      # profit-side close at base 50% target
CLOSE_TREND = 'CLOSE_TREND'      # profit-side close at trend-extended 70/85%
STOP_DELTA = 'STOP_DELTA'        # loss-side delta-gate close (CSP cut / CC roll)
STOP_LOSS = 'STOP_LOSS'          # loss-side premium-multiple close
EXPIRE = 'EXPIRE'                # OTM at expiry (full premium kept)
CC_ASSIGN = 'CC_ASSIGN'          # ITM call at expiry → shares called away
CSP_ASSIGN = 'CSP_ASSIGN'        # ITM put at expiry → shares assigned
# Roll reasons ----------------------------------------------------------------
ROLL_UP_OUT = 'ROLL_UP_OUT'      # CC: close, reopen higher strike / more DTE
ROLL_DOWN_OUT = 'ROLL_DOWN_OUT'  # CSP: close, reopen lower strike / more DTE
# Warn-only (no action, but surfaced for the operator) ------------------------
WARN_CC_DELTA = 'WARN_CC_DELTA'


@dataclass
class ExitDecision:
    """The single source of truth for an option position's exit action.

    Exactly one of ``close_reason`` / ``roll_decision`` is set per cycle
    (the engine dispatches on whichever is non-None). ``warn`` carries an
    advisory string (e.g. the 0.50–0.60 CC band) that the engine logs but
    does NOT act on — so a position can be both warned-at and held.
    """
    close_reason: Optional[str] = None    # one of CLOSE_* / STOP_* / EXPIRE / *_ASSIGN
    roll_decision: Optional[str] = None   # one of ROLL_*
    warn: Optional[str] = None            # advisory only; never triggers a trade
    reason: str = ''                      # human-readable explanation

    @property
    def acts(self) -> bool:
        """True if this decision will move the position this cycle."""
        return self.close_reason is not None or self.roll_decision is not None


def decide_exit_action(
    strategy: str,
    profit_captured: float,
    dte: int,
    delta: float,
    pnl_dollars: float,
    trend_ctx: Optional[TrendContext] = None,
    capital_scarcity: Optional[str] = None,
    csp_paused: bool = False,
    premium_collected: float = 0.0,
    cfg=None,
    emergency: bool = False,
) -> ExitDecision:
    """Decide the exit action for one option position.

    Pure function over inputs the engine already computes. No I/O. Priority:

      1. Expiry (DTE ≤ 0) — caller resolves ITM/OTM to ASSIGN/EXPIRE via the
         stock price; here we only flag DTE ≤ 0 so the caller's expiry branch
         owns it. (Returns close_reason=None, roll=None — caller handles.)
      2. PROFIT side — delegate to ``decide_profit_target``. CLOSE → book it;
         ROLL_* → roll it; MANAGE_DTE (≤ 21 DTE floor) falls through to the
         loss tiers below (the dead-end bug: a winner inside 21 DTE used to
         rot because MANAGE_DTE mapped to no action — now the loss/profit
         tiers re-evaluate it).
      3. LOSS side — delta gates (CSP cut / CC roll), then premium-multiple
         tiers (DTE-adjusted), then the absolute catch-all.

    Args:
        strategy: 'CSP' or 'CC' (CALL→CC, PUT→CSP).
        profit_captured: % of max premium captured. Positive = profit,
            negative = loss (e.g. -150 = 1.5× premium lost).
        dte: days to expiry.
        delta: absolute delta (sign-insensitive).
        pnl_dollars: mark-to-market $ P&L ((entry - current_bid) × qty × 100).
            Negative = loss. Used by the absolute catch-all.
        trend_ctx: the trend/sentiment/IV stack (None → base behavior).
        capital_scarcity: SCARCE / NORMAL / ABUNDANT.
        csp_paused: True when CSP redeployment is over its deployment cap.
        premium_collected: total credit banked on this trade
            (entry × qty × 100). Selects the absolute-loss band so a large
            premium isn't cut on noise — the catch-all floor scales with the
            premium. 0 (default) selects the smallest band, matching the old
            flat-floor behavior.
        cfg: Config instance (default: module singleton).
        emergency: True in EMERGENCY recovery stage — disables the
            deployment-aware SCARCE bypass so profit books at base (see
            profit_management.decide_profit_target).

    Returns:
        ExitDecision naming the close_reason and/or roll_decision.
    """
    cfg = cfg or get_config()
    strategy = 'CC' if strategy.upper() == 'CC' else 'CSP'

    # ── 1. Expiry: caller owns ITM/OTM resolution (needs stock price) ──
    if dte <= 0:
        return ExitDecision(reason=f"DTE {dte} ≤ 0 — caller resolves to "
                                   f"ASSIGN/EXPIRE via stock price.")

    # ── 2. Profit side (delegated) ──
    pdec = decide_profit_target(
        strategy, profit_captured, dte, abs(delta),
        trend_ctx, capital_scarcity=capital_scarcity, csp_paused=csp_paused,
        emergency=emergency)
    profit_side = _from_profit_decision(pdec)
    if profit_side.acts:
        return profit_side

    # ── 3. Loss side ──
    # 3a. Delta gates — config-driven, symmetric. CSP cuts; CC rolls.
    delta_action = _delta_gate(strategy, delta, dte, cfg)
    if delta_action.acts:
        return delta_action

    # A CC in the warn band (0.50–0.60) surfaces an advisory but does not act.
    warn = delta_action.warn

    # 3b. Premium-multiple tiers (DTE-adjusted). CSP only — CC is exempt
    #     (the option "loss" is offset by share gains in a covered position).
    premium_action = _premium_tier_stop(strategy, profit_captured, dte, cfg)
    if premium_action.acts:
        return premium_action

    # 3c. Absolute catch-all — tiered by premium collected so a large credit
    #     isn't exited on normal noise (the old flat $1k fired at 0.17× of a
    #     $6k CSP). The DTE premium tiers (3×/2×/1.5×) above stay the binding
    #     constraint for ordinary losses; this floor catches a genuine rout.
    heavy = float(cfg.stop_heavy_loss_for_premium(premium_collected))
    if pnl_dollars < -heavy:
        return ExitDecision(
            close_reason=STOP_LOSS,
            reason=f"Heavy loss ${pnl_dollars:,.0f} < -${heavy:,.0f} "
                   f"catch-all (premium ${premium_collected:,.0f} band) — close.",
            warn=warn)

    # No action this cycle. Carry the advisory (if any) so the engine can log.
    if warn:
        return ExitDecision(warn=warn,
                            reason=f"Hold — {warn}.")
    return ExitDecision(reason=f"Hold — below all exit thresholds "
                               f"(Δ={delta:.2f}, captured={profit_captured:.0f}%).")


# ── Profit-side mapping ────────────────────────────────────────────

def _from_profit_decision(pdec: ProfitDecision) -> ExitDecision:
    """Translate a profit-side ProfitDecision into an ExitDecision."""
    a = pdec.action
    if a == ProfitDecision.ACTION_CLOSE:
        reason = CLOSE_50PCT if pdec.target_pct <= 50 else CLOSE_TREND
        return ExitDecision(close_reason=reason, reason=pdec.reason)
    if a == ProfitDecision.ACTION_ROLL_UP_OUT:
        return ExitDecision(roll_decision=ROLL_UP_OUT, reason=pdec.reason)
    if a == ProfitDecision.ACTION_ROLL_DOWN_OUT:
        return ExitDecision(roll_decision=ROLL_DOWN_OUT, reason=pdec.reason)
    # ACTION_HOLD and ACTION_MANAGE_DTE do not act on the profit side here.
    # MANAGE_DTE (DTE ≤ 21 floor) deliberately falls through to the loss tiers
    # — a winner inside 21 DTE is re-evaluated by the premium/delta stops so
    # it books profit rather than rotting to expiry (the prior dead-end bug).
    return ExitDecision(reason=pdec.reason)


# ── Loss side: delta gates ─────────────────────────────────────────

def _delta_gate(strategy: str, delta: float, dte: int, cfg) -> ExitDecision:
    """Layer 2 — delta gates. CSP cuts at critical. CC is DTE-aware:
    near-expiry ITM → HOLD (wheel turn); more DTE → ROLL_UP_OUT."""
    delta = abs(delta)

    if strategy == 'CSP':
        csp_critical = float(cfg.stop_delta_csp_critical)
        if delta >= csp_critical:
            return ExitDecision(
                close_reason=STOP_DELTA,
                reason=f"CSP |Δ|={delta:.2f} ≥ {csp_critical:.2f} critical — "
                       f"too directional, cut it.")
        # CSP decision band is advisory only (handled by portfolio.py display).
        return ExitDecision()

    # CC: DTE-aware delta response.
    # Near-expiry ITM calls → HOLD for assignment. The wheel strategy
    # EXPECTS covered calls to go ITM — assignment means selling shares
    # at a strike you chose, booking profit. Churning near-expiry rolls
    # just pays commissions to delay the inevitable.
    cc_close = float(cfg.stop_delta_cc_close)
    cc_decision = float(cfg.stop_delta_cc_decision)
    cc_assign_dte = int(cfg.stop_delta_cc_assign_dte)

    if delta >= cc_close:
        if dte <= cc_assign_dte:
            return ExitDecision(
                warn=f"CC Δ={delta:.2f} ≥ {cc_close:.2f} with {dte} DTE ≤ "
                     f"{cc_assign_dte} — assignment imminent; letting the wheel "
                     f"turn (shares called at strike = profit booked).")
        return ExitDecision(
            roll_decision=ROLL_UP_OUT,
            reason=f"CC Δ={delta:.2f} ≥ {cc_close:.2f} with {dte} DTE > "
                   f"{cc_assign_dte} — roll up-and-out for credit "
                   f"(net-credit-only per rolling discipline); "
                   f"if no credit roll, accept assignment.")
    if delta >= cc_decision:
        return ExitDecision(
            warn=f"CC Δ={delta:.2f} in [{cc_decision:.2f}, {cc_close:.2f}) — "
                 f"monitor closely (no auto-action)")
    return ExitDecision()


# ── Loss side: premium-multiple tiers (DTE-adjusted) ───────────────

def _premium_tier_stop(strategy: str, profit_captured: float, dte: int,
                       cfg) -> ExitDecision:
    """Layer 1 — premium-multiple stops. close_multiple depends on DTE band.

    profit_captured < 0 here (a loss). loss_multiple = |captured| / 100,
    i.e. how many × the original premium has been lost.

    CC is EXEMPT: the option "loss" on a covered call is offset by share
    gains — closing the option leg in isolation destroys value. The
    absolute catch-all (heavy_loss_abs) still applies to both strategies.
    """
    if strategy == 'CC':
        return ExitDecision()

    if profit_captured >= 0:
        return ExitDecision()

    loss_multiple = abs(profit_captured) / 100.0
    far_close = float(cfg.stop_loss('far_close', 3.0))
    mid_close = float(cfg.stop_loss('mid_close', 2.0))
    near_close = float(cfg.stop_loss('near_close', 1.5))

    band, threshold = None, None
    if dte > int(cfg.stop_loss('far_dte', 30)):
        band, threshold = f"> {int(cfg.stop_loss('far_dte', 30))} DTE", far_close
    elif dte > int(cfg.stop_loss('mid_dte', 21)):
        band, threshold = "21–30 DTE", mid_close
    else:
        band, threshold = "≤ 21 DTE", near_close

    if loss_multiple >= threshold:
        return ExitDecision(
            close_reason=STOP_LOSS,
            reason=f"{loss_multiple:.1f}× premium lost ≥ {threshold:.1f}× "
                   f"({band}) — close.")
    return ExitDecision()
