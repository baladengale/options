"""
Holding-decision scoring — score existing stock + option positions and emit
buy/sell/hold decisions.

Moved verbatim from scripts/portfolio_check.py so the portfolio wrapper is thin.
These functions are deterministic; yfinance (yf_client) and moomoo (quote data)
are passed in by the caller.

Public:
    _score_holding(snap, ticker, yf_client, regime, regime_mult) -> float
    _find_best_cc(moomoo, ticker, snap, shares, cost_basis, yf_client,
                  regime, regime_mult, allow_below_basis=False) -> Optional[dict]
    _score_option(pos, current, profit_captured, pl, today, yf_client, ...) -> (score, decision, profit_decision)
    _ticker_frequency_ok(pos, today, orders) -> (ok, note)
    _OptionCurrent / _parse_snapshot_row — lightweight option view from a raw API row
"""

from datetime import date
from typing import Optional

from src.config import get_config
from src.data.models import StockSnapshot
from src.filters.contract_filters import passes_liquidity, passes_delta, iv_sane, passes_roc, cc_roc
from src.scoring.screener_score import _contract_penalty


def _score_holding(snap: StockSnapshot, ticker: str, yf_client, regime: str, regime_mult: float) -> float:
    """Score a stock holding 1-10 (1=best, borrows from screener logic)."""
    score = 5.0  # neutral base

    # RSI
    rsi = snap.rsi_14 or 50
    if 45 <= rsi <= 55:    score -= 1.0
    elif 30 <= rsi <= 70:  score += 0.0
    else:                  score += 2.0

    # Trend
    if snap.sma_50 and snap.sma_200:
        if snap.last_price > snap.sma_50 > snap.sma_200:
            score -= 1.0
        elif snap.last_price < snap.sma_200:
            score += 1.5

    # Volume ratio
    if snap.volume_ratio:
        if snap.volume_ratio > 1.5:   score -= 0.5
        elif snap.volume_ratio < 0.5: score += 0.5

    # External sentiment
    if yf_client:
        ratings = yf_client.get_analyst_ratings(ticker)
        if ratings:
            if ratings.consensus == 'STRONG_BUY':  score -= 1.0
            elif ratings.consensus == 'BUY':       score -= 0.5
            elif ratings.consensus == 'SELL':      score += 2.0

        earnings = yf_client.get_earnings(ticker)
        if earnings and earnings.in_blackout:
            score += 1.5

        news = yf_client.get_news_sentiment_score(ticker)
        if news:
            if news['score'] >= 70:    score -= 0.5
            elif news['score'] <= 30:  score += 1.0

    # Regime
    if regime == 'BEARISH':   score += 2.0
    elif regime == 'VOLATILE': score += 1.0

    return max(1.0, min(10.0, score))


def _find_best_cc(moomoo, ticker: str, snap, shares: float, cost_basis: float,
                  yf_client, regime: str, regime_mult: float,
                  allow_below_basis: Optional[bool] = None,
                  open_cc_contracts: int = 0) -> Optional[dict]:
    """Find best covered call candidate for a stock holding.
    GOAL.md rule: Never sell CC below cost basis — unless allow_below_basis, the
    Decision #10 dead-zone path, where a flagged below-basis candidate is
    surfaced (with months-to-recover) for the operator to consciously choose.

    Collar gate (SPECS §12.1): open_cc_contracts are short calls already sold
    against these shares. A CC is only covered if ≥100 shares remain FREE —
    without this netting, a fully encumbered holding (e.g. 500 shares with 5
    open CCs) gets a 6th "covered" call recommendation that is actually a
    naked call (hard-constraint #1 violation). Mirrors the screener's
    CC_SHARES_COMMITTED gate (scripts/screener.py).
    """
    if shares < 100:
        return None
    if shares - open_cc_contracts * 100 < 100:
        return None

    cfg = get_config()
    if allow_below_basis is None:
        allow_below_basis = not cfg._data.get('cc_management', {}).get('never_sell_below_cost_basis', True)

    contracts = moomoo.get_option_snapshots(f'US.{ticker}', dte_min=7, dte_max=60)
    best = None
    best_score = 999

    for c in contracts:
        if c.option_type != 'CALL':
            continue
        # GOAL.md: Never sell CC below cost basis (bypassed only on the flagged path)
        if not allow_below_basis and c.strike <= cost_basis:
            continue
        if not passes_liquidity(c, cfg):
            continue
        ok, _ = passes_delta(c, 'CC', regime, cfg)
        if not ok:
            continue
        if not iv_sane(c):
            continue

        roc = cc_roc(c.bid, snap.last_price, c.dte)
        if not passes_roc(roc, 'CC', cfg):
            continue

        # Use shared contract penalty (all dimensions: DTE, OI, spread, delta, RoC, IV, volume)
        penalty = _contract_penalty(c, c.delta or 0, roc)

        if penalty < best_score:
            best_score = penalty
            best = {
                'strike': c.strike,
                'expiry': c.expiry,
                'dte': c.dte,
                'delta': c.delta,
                'bid': c.bid,
                'roc': roc,
                'strike_str': f'${c.strike:.0f} {c.expiry}',
            }

    return best


def _ticker_frequency_ok(pos: dict, today: date, orders: list) -> tuple[bool, str]:
    """Check per-ticker close frequency cap (spec §9).

    Returns (ok, note). If ok=False, the caller should suppress a profit-taking
    CLOSE but still allow defensive closes (delta gate, premium stop, MANAGE_DTE).
    """
    cfg = get_config()
    max_closes = cfg.guardrail_limits('max_closes_per_ticker_per_month', 2)
    ticker = pos.get('ticker', '')
    ym = today.strftime('%Y-%m')

    closes_this_month = sum(
        1 for o in (orders or [])
        if o.get('status') in ('FILLED_ALL', 'FILLED_PART')
        and str(o.get('date', '') or '').startswith(ym)
        and o.get('code', '').upper().startswith(ticker.upper())
        and o.get('side', '').upper() in ('BUY', 'BUY_BACK')
    )

    if closes_this_month >= max_closes:
        return False, f'{closes_this_month}/{max_closes} {ticker} closes this month'
    return True, ''


def _trend_label(strategy: str, tc) -> str:
    """Compact strategy-aware trend tag for decision strings.

    ▲ uptrend / → flat / ▼ downtrend. The ⚠️ marks the direction that HURTS
    the position: a downtrend for short puts (stock falls into the strike),
    an uptrend for short calls (stock runs into the strike).
    """
    if tc is None:
        return 'trend —'
    if tc >= 60:
        tag = f'trend {tc:.0f}▲'
        danger = strategy == 'CC'
    elif tc < 40:
        tag = f'trend {tc:.0f}▼'
        danger = strategy == 'CSP'
    else:
        tag = f'trend {tc:.0f}→'
        danger = False
    return tag + ('⚠️' if danger else '')


def _score_option(pos: dict, current, profit_captured: float, pl: float,
                  today: date, yf_client, trend_ctx=None, capital_scarcity=None,
                  orders=None, csp_paused: bool = False,
                  emergency: bool = False) -> tuple[float, str, object]:
    """Score an option position 1-10. Returns (score, decision, profit_decision).

    Profit booking is delegated to src.analysis.profit_management.decide_profit_target
    (trend-modulated per specs/profit-loss-management-spec.md). With trend_ctx=None
    it falls back to the flat 50%/70% close behavior (backward compatible).
    Loss-side logic (stop tiers, delta gates, thesis catch-all) runs unchanged.

    The OTM-only close gate (spec §6) overrides a profit-taking CLOSE when the
    position is far OTM (|Δ| < close_if_delta_above) with ample DTE (> close_if_dte_below).
    The third return element is the ProfitDecision from decide_profit_target, threaded
    out so downstream consumers can switch on its ACTION_* constants.

    csp_paused is forwarded to decide_profit_target to enable the deployment-aware
    SCARCE bypass when CSP redeployment is blocked. Default False (no bypass).
    emergency (EMERGENCY recovery stage) is forwarded to disable that bypass —
    booking profit to repair the balance sheet outranks the unvalidated
    trend extension. Default False.
    """
    score = 5.0
    decision = 'HOLD'
    dte = current.dte or 0
    delta = abs(current.delta or 0)
    strategy = 'CC' if pos['type'] == 'CALL' else 'CSP'

    # Profit captured — trend-modulated target (spec §4.2)
    from src.analysis.profit_management import decide_profit_target, ProfitDecision
    pd = decide_profit_target(strategy, profit_captured, dte, delta, trend_ctx,
                              capital_scarcity, csp_paused=csp_paused,
                              emergency=emergency)

    # ── OTM-only close gate (spec §6) ──
    # Do not auto-close a profitable position when it is far OTM with ample DTE.
    # Let theta keep working. This only overrides ACTION_CLOSE; MANAGE_DTE, ROLLs,
    # and loss-side stops fire regardless.
    base_target = float(get_config().profit_take_csp('base_pct', 50) if strategy == 'CSP'
                         else get_config().profit_take_cc('base_pct', 50))
    otm_gate_delta = get_config().profit_take('close_if_delta_above', 0.30)
    otm_gate_dte = get_config().profit_take('close_if_dte_below', 21)

    otm_override = (
        pd.action == ProfitDecision.ACTION_CLOSE
        and profit_captured >= base_target
        and delta < otm_gate_delta
        and dte > otm_gate_dte
    )

    if pd.action == ProfitDecision.ACTION_CLOSE and not otm_override:
        # Per-ticker frequency cap (spec §9) — suppress profit-taking CLOSE if
        # this ticker already hit the monthly close limit. Defensive closes
        # (delta gate, premium stop, MANAGE_DTE) are applied by later layers and
        # are NOT suppressed by this check.
        freq_ok, freq_note = _ticker_frequency_ok(pos, today, orders or [])
        if not freq_ok:
            decision = (f'👍 HOLD FREQ-CAPPED ({profit_captured:.0f}% ≥ {pd.target_pct:.0f}% target, '
                        f'{freq_note})')
        else:
            # Target-aware scoring: deeper past the engine's target = more "decided"
            depth = profit_captured - pd.target_pct
            score -= 2.0 if depth >= 20 else (1.5 if depth >= 0 else 1.0)
            decision = f'✅ CLOSE ({profit_captured:.0f}% ≥ {pd.target_pct:.0f}% target)'
    elif otm_override:
        # Engine said CLOSE but OTM gate overrides to HOLD — theta still working.
        decision = (f'👍 HOLD OTM GATE ({profit_captured:.0f}% ≥ {pd.target_pct:.0f}% target, '
                    f'|Δ|={delta:.2f} < {otm_gate_delta}, DTE={dte} > {otm_gate_dte})')
    elif pd.action == ProfitDecision.ACTION_MANAGE_DTE:
        score -= 1.5
        decision = '📅 MANAGE DTE — close/roll/assign (21-DTE floor)'
    elif pd.action == ProfitDecision.ACTION_ROLL_DOWN_OUT:
        score -= 1.0  # winner: don't flag as urgently as a flat close
        decision = f'🔄 ROLL DOWN-OUT ({profit_captured:.0f}% ≥ {pd.target_pct:.0f}% trend target — credit only)'
    elif pd.action == ProfitDecision.ACTION_ROLL_UP_OUT:
        score -= 1.0
        decision = f'🔄 ROLL UP-OUT (CC winner, uptrend — keep shares, credit only)'
    elif profit_captured >= 30:
        score -= 0.5
        decision = (f'👍 HOLD ({profit_captured:.0f}% < {pd.target_pct:.0f}% target'
                    + (f', trend-extended' if pd.extended_by_trend else '') + ')')
    elif profit_captured < 0:
        # Underwater — comparing a loss to a profit target reads as nonsense
        # ("-58% < 85% target, trend-extended"). The trend-extended tag describes
        # the target, not the position, and is noise on a loser. Show loss
        # posture instead: the ×-multiple of premium lost. The stop tiers /
        # delta gates later in this function overwrite this string when they
        # fire, so "below stop tiers" holds by construction.
        loss_multiple = abs(profit_captured) / 100
        decision = (f'HOLD (underwater {profit_captured:.0f}% — {loss_multiple:.2f}× premium, '
                    f'below stop tiers)')
    else:
        decision = (f'HOLD ({profit_captured:.0f}% < {pd.target_pct:.0f}% target'
                    + (f', trend-extended' if pd.extended_by_trend else '') + ')')

    # DTE check — 21 DTE is a universal management point, not just for losers
    # (gamma roughly doubles 21→7 DTE; playbook §3)
    if dte <= 3:
        score -= 1.5
        if 'CLOSE' not in decision:
            decision = '📅 Expiring Soon — Let expire/assign naturally'
    elif dte <= 7:
        score += 1.0
        if profit_captured < 30:
            decision = '📅 Near Expiry — Monitor only, no action needed'
    elif dte <= 14:
        if profit_captured < 0:  # underwater
            score += 1.0
            decision = '📊 Position Underwater — Monitor thesis, not price'
        elif 'CLOSE' not in decision:
            decision = '📋 Near Management Point — Review, no action needed'
    elif dte <= 21:
        if profit_captured < 0:
            decision = '📋 Review Position — Evaluate rolling at weekly check'
        elif 'CLOSE' not in decision:
            score -= 0.5
            decision = '📋 At Management Point — Weekly review, no action needed'

    # Layer 2: Delta gates (from config, DTE-interaction per §7.2)
    # Inside the gamma zone (DTE ≤ 21) the decision delta relaxes to 0.40
    # because gamma is ramping and there's less time to recover.
    cfg = get_config()
    _GAMMA_ZONE_DECISION_DELTA = 0.40  # fallback inside DTE ≤ 21
    eff_csp_decision = _GAMMA_ZONE_DECISION_DELTA if dte <= 21 else cfg.stop_delta_csp_decision
    eff_cc_warn = _GAMMA_ZONE_DECISION_DELTA if dte <= 21 else cfg.stop_delta_cc_warn

    if strategy == 'CSP' and delta >= cfg.stop_delta_csp_critical:
        score += 2.0
        decision = '🛑 |Δ|≥0.60 — roll / take assignment / exit'
    elif strategy == 'CC' and delta >= cfg.stop_delta_cc_critical:
        score += 1.5
        decision = '📈 High Assignment Probability — Expected CC outcome'
    elif strategy == 'CSP' and delta >= cfg.stop_delta_csp_itm:
        score += 1.0
        if 'CLOSE' not in decision and 'STOP' not in decision:
            decision = '📈 ITM — Assignment probable, expected outcome'
    elif strategy == 'CSP' and delta >= eff_csp_decision:
        score += 0.5
        if decision == 'HOLD' or 'Management Point' in decision or 'OTM GATE' in decision:
            decision = f'🔶 Δ≥{eff_csp_decision:.2f} decision time — plan roll/assign/exit'
    elif strategy == 'CC' and delta >= eff_cc_warn:
        score += 0.5
        if decision == 'HOLD' or 'OTM GATE' in decision:
            decision = f'⚠️  Δ≥{eff_cc_warn:.2f} — monitor closely'

    # Layer 1: Premium multiple stop-loss (DTE-adjusted)
    # profit_captured is a percentage: positive = profit, negative = loss
    # Stop tiers route to a decision tree, not a blanket close (playbook §3):
    # CSP: roll for credit → else take assignment (thesis+basis+size OK) → else exit.
    if profit_captured < 0:  # position is underwater
        loss_multiple = abs(profit_captured) / 100  # -150% → 1.5× loss
        tree = 'roll / take assignment / exit' if strategy == 'CSP' else 'close or roll for credit'
        far_close = cfg.stop_loss('far_close', 3.0)
        far_alert = cfg.stop_loss('far_alert', 2.0)
        mid_close = cfg.stop_loss('mid_close', 2.0)
        mid_alert = cfg.stop_loss('mid_alert', 1.0)
        near_close = cfg.stop_loss('near_close', 1.5)
        near_alert = cfg.stop_loss('near_alert', 0.5)

        if dte > 30:
            if loss_multiple >= far_close:
                score += 2.0; decision = f'🛑 {far_close}× STOP TIER — {tree}'
            elif loss_multiple >= far_alert:
                score += 1.0
                if 'STOP' not in decision: decision = f'⚠️  STOP ALERT — {far_alert}× premium lost'
        elif dte > 21:
            if loss_multiple >= mid_close:
                score += 2.0; decision = f'🛑 {mid_close}× STOP TIER — {tree}'
            elif loss_multiple >= mid_alert:
                score += 1.0
                if 'STOP' not in decision: decision = f'⚠️  STOP ALERT — {mid_alert}× premium lost'
        else:  # dte <= 21
            if loss_multiple >= near_close:
                score += 2.5; decision = f'🛑 {near_close}× STOP TIER (gamma) — {tree}'
            elif loss_multiple >= near_alert:
                score += 1.0
                if 'STOP' not in decision: decision = '⚠️  NEAR STOP — monitor closely'

    # Earnings blackout
    if yf_client and pos['ticker']:
        earnings = yf_client.get_earnings(pos['ticker'])
        if earnings and earnings.in_blackout and dte > earnings.days_to_earnings:
            score += 1.5
            decision = '⚠️  EARNINGS IN DTE — close before'

    # Heavy loss catch-all - NOW THESIS-AWARE, premium-tiered
    # The flat −$1k was punitive for large premiums (fired at 0.17× of a $6k
    # CSP). Now scales with the trade's total credit via config bands.
    premium_collected = abs(pos.get('cost', 0) or 0) * abs(pos.get('qty', 0) or 0) * 100
    heavy_floor = cfg.stop_heavy_loss_for_premium(premium_collected)
    if pl < -heavy_floor and 'STOP' not in decision:
        # Use thesis validation instead of generic "evaluate exit"
        try:
            from src.analysis.thesis_validator import quick_thesis_check
            from src.data.moomoo_client import MoomooClient

            # Get snapshot for thesis check
            moomoo = MoomooClient()
            snapshot = moomoo.get_stock_snapshot(f"US.{pos['ticker']}")

            if snapshot:
                thesis_check = quick_thesis_check(pos['ticker'], snapshot)

                if thesis_check['broken']:
                    score += 0.5
                    decision = '🚨 THESIS BROKEN — Exit Wheel position'
                elif thesis_check['damaged']:
                    score += 1.0
                    decision = '⚠️  THESIS DAMAGED — Monitor, no action needed'
                else:
                    score += 1.5
                    decision = '📊 Position Down — Thesis intact, hold position'
        except Exception:
            # Fallback to original message if thesis check fails
            score += 1.5
            if 'CLOSE' not in decision:
                decision = '🔴 UNDERWATER — Monitor thesis, not price'

    # Trend context tag — every non-action decision carries the same
    # strategy-aware trend signal (uptrend helps CSP, hurts CC). Action
    # decisions (CLOSE/ROLL/STOP tiers) skip it; 'trend —' flags missing data.
    if 'trend' not in decision:
        dl = decision.lower()
        if any(m in dl for m in ('hold', 'monitor', 'review', 'management', 'expiring')):
            tc = pd.trend_context.trend_composite if pd.trend_context else None
            decision += f', {_trend_label(strategy, tc)}'

    return max(1.0, min(10.0, score)), decision, pd


class _OptionCurrent:
    """Lightweight option snapshot from batch API."""
    def __init__(self, row):
        self.bid = float(row.get('bid_price', 0) or 0)
        self.ask = float(row.get('ask_price', 0) or 0)
        self.last_price = float(row.get('last_price', 0) or 0)
        self.delta = float(row.get('option_delta', 0) or 0)
        self.gamma = float(row.get('option_gamma', 0) or 0)
        self.implied_vol = float(row.get('option_implied_volatility', 0) or 0)
        self.open_interest = int(row.get('option_open_interest', 0) or 0)
        self.volume = int(row.get('volume', 0) or 0)
        self.dte = int(row.get('option_expiry_date_distance', 0) or 0)
        self.strike = float(row.get('option_strike_price', 0) or 0)
        self.option_type = str(row.get('option_type', ''))


def _parse_snapshot_row(row) -> Optional[_OptionCurrent]:
    """Parse snapshot row into lightweight option data."""
    try:
        return _OptionCurrent(row)
    except Exception:
        return None
