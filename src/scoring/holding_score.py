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
    _score_option(pos, current, profit_captured, pl, today, yf_client) -> (score, decision)
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
                  allow_below_basis: Optional[bool] = None) -> Optional[dict]:
    """Find best covered call candidate for a stock holding.
    GOAL.md rule: Never sell CC below cost basis — unless allow_below_basis, the
    Decision #10 dead-zone path, where a flagged below-basis candidate is
    surfaced (with months-to-recover) for the operator to consciously choose."""
    if shares < 100:
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


def _score_option(pos: dict, current, profit_captured: float, pl: float,
                  today: date, yf_client) -> tuple[float, str]:
    """Score an option position 1-10. Returns (score, decision)."""
    score = 5.0
    decision = 'HOLD'
    dte = current.dte or 0
    delta = abs(current.delta or 0)
    strategy = 'CC' if pos['type'] == 'CALL' else 'CSP'

    # Profit captured — close at 50%+
    if profit_captured >= 70:
        score -= 2.0
        decision = '✅ CLOSE (70%+ profit)'
    elif profit_captured >= 50:
        score -= 1.5
        decision = '✅ CLOSE (50%+ profit)'
    elif profit_captured >= 30:
        score -= 0.5
        decision = '👍 HOLD (30%+ captured)'

    # DTE check — 21 DTE is a universal management point, not just for losers
    # (gamma roughly doubles 21→7 DTE; playbook §3)
    if dte <= 3:
        score -= 1.5
        if 'CLOSE' not in decision:
            decision = '⚠️  EXPIRING — close or roll'
    elif dte <= 7:
        score += 1.0
        if profit_captured < 30:
            decision = '⚠️  NEAR EXPIRY — monitor closely'
    elif dte <= 14:
        if profit_captured < 0:  # underwater
            score += 1.0
            decision = '⚠️  UNDERWATER — consider rolling'
        elif 'CLOSE' not in decision:
            decision = '⏰ <21 DTE — close or roll (mgmt point)'
    elif dte <= 21:
        if profit_captured < 0:
            decision = '🔄 CONSIDER ROLLING'
        elif 'CLOSE' not in decision:
            score -= 0.5
            decision = '⏰ 21 DTE — close or roll (mgmt point)'

    # Layer 2: Delta gates (from config)
    cfg = get_config()
    if strategy == 'CSP' and delta >= cfg.stop_delta_csp_critical:
        score += 2.0
        decision = '🛑 |Δ|≥0.60 — roll / take assignment / exit'
    elif strategy == 'CC' and delta >= cfg.stop_delta_cc_critical:
        score += 1.5
        decision = '⚠️  DELTA WARN — Δ≥0.50, assignment risk'
    elif strategy == 'CSP' and delta >= cfg.stop_delta_csp_itm:
        score += 1.0
        if 'CLOSE' not in decision and 'STOP' not in decision:
            decision = '⚠️  ITM — assignment risk'
    elif strategy == 'CSP' and delta >= cfg.stop_delta_csp_decision:
        score += 0.5
        if decision == 'HOLD' or '21 DTE' in decision:
            decision = '🔶 Δ≥0.40 decision time — plan roll/assign/exit'
    elif strategy == 'CC' and delta >= cfg.stop_delta_cc_warn:
        score += 0.5
        if decision == 'HOLD':
            decision = '⚠️  Δ≥0.40 — monitor closely'

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

    # Heavy loss catch-all
    if pl < -1000 and 'STOP' not in decision:
        score += 1.5
        if 'CLOSE' not in decision:
            decision = '🔴 UNDERWATER — evaluate exit'

    return max(1.0, min(10.0, score)), decision


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
