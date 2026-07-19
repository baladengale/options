#!/usr/bin/env python3
"""Options Wheel Screener — rank all watchlist tickers, output best CC/CSP trades.

Scoring: 1-10 (1 = best) per ticker. Lower is better.
Dimensions: Technical (25%) | Options Quality (25%) | Fundamental (15%) |
            External Sentiment (20%) | Macro/Risk (15%)

Output: top ranked trades with specific strike, expiry, delta, RoC.

Usage:
    python3 scripts/screener.py                  # screen entire watchlist
    python3 scripts/screener.py --cc-only         # covered calls only
    python3 scripts/screener.py --csp-only        # cash-secured puts only
    python3 scripts/screener.py --top 5           # top 5 results
    python3 scripts/screener.py --no-external     # skip yfinance (faster, offline)
"""

import argparse
import os
import re
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from moomoo import OpenSecTradeContext, TrdEnv, RET_OK

from src.logging_setup import get_logger
log = get_logger('screener')

from src.data.moomoo_client import MoomooClient
from src.data.portfolio_loader import fetch_portfolio
from src.data.yfinance_client import YFinanceClient
from src.data.compute import enrich_stock_snapshot
from src.data.models import StockSnapshot, OptionSnapshot
from src.analysis.sentiment import (
    get_macro_context, get_ticker_sentiment, get_watchlist_sentiment,
    score_analyst_consensus, score_news_sentiment, score_earnings_blackout,
)
from src.config import get_config

# Scoring engine now lives in src/ — re-exported here so scripts/oie_engine.py and
# tests/test_screener_scoring.py can keep importing these names from scripts.screener
# (single source of truth, no behavior change).
from src.scoring.screener_score import (
    _cfg_val,
    _compute_ticker_score, _contract_penalty, _trend_composite,
    _score_technical, _score_options_eco, _score_fundamental,
    _score_external, _score_macro, _csp_roc,
    _score_stars, _reason, _compute_chain_gex,
)

# Watchlist from CLAUDE.md
# Default watchlist (used if moomoo watchlist fetch fails)
_DEFAULT_WATCHLIST = [
    'US.V', 'US.MSFT', 'US.GOOGL', 'US.AAPL', 'US.AMZN',
    'US.NVDA', 'US.META', 'US.AVGO', 'US.ADBE', 'US.CRM', 'US.AMD',
]


def _fetch_option_chain_resilient(moomoo, ticker: str, dte_min: int = 7, dte_max: int = 90) -> list:
    """
    Fetch option chain with minimal retry on rate limits.
    Single get_option_snapshots call with one 1s retry if empty.
    No yfinance fallback — moomoo is the source of truth.
    """
    for attempt in range(2):
        if attempt > 0:
            time.sleep(1)
        contracts = moomoo.get_option_snapshots(ticker, dte_min=dte_min, dte_max=dte_max)
        if contracts:
            return contracts
    return []


def _fetch_live_watchlist(moomoo) -> list[str]:
    """Pull US stock tickers from moomoo watchlist group (name from config). Fallback to default."""
    import re
    group_name = _cfg_val(lambda c: c.moomoo_watchlist_group, 'Options')
    try:
        ret, data = moomoo.ctx.get_user_security(group_name)
        if ret == RET_OK and data is not None and len(data) > 0:
            tickers = []
            for _, row in data.iterrows():
                code = row['code']
                # US stocks only — skip option contracts, crypto, indices
                if (code.startswith('US.') and not re.search(r'\d{6}[CP]\d+', code)
                        and '..' not in code):
                    tickers.append(code)
            if tickers:
                return tickers
    except Exception:
        pass
    return _DEFAULT_WATCHLIST


def _fetch_live_portfolio() -> tuple[dict[str, float], float, float, float, set[str]]:
    """Pull live portfolio via the shared loader (src.data.portfolio_loader).
    Kept here because scripts/oie_engine.py imports it. Returns
    (stock_holdings, cash, buying_power, fund, existing_option_tickers).
    Falls back to last-known cash if OpenD is unreachable."""
    pf = fetch_portfolio()
    if not pf.stocks and pf.funds.cash == 0 and pf.funds.fund == 0:
        # OpenD unavailable — preserve historical fallback for graceful degradation
        return {}, 817.0, 48638.89, 48500.0, set()
    holdings = {t: pos['qty'] for t, pos in pf.stocks.items()}
    return holdings, pf.funds.cash, pf.funds.buying_power, pf.funds.fund, pf.option_tickers


@dataclass
class TradeCandidate:
    ticker: str
    strategy: str              # COVERED_CALL | CASH_SECURED_PUT
    score: float               # 1-10, lower = better
    strike: float
    expiry: str
    dte: int
    delta: float
    bid: float
    ask: float
    premium: float             # premium per contract
    annualized_roc_pct: float
    iv: float
    iv_rank: float
    open_interest: int
    capital_required: float
    reason: str


def main():
    parser = argparse.ArgumentParser(description='Options Wheel Screener')
    parser.add_argument('--cc-only', action='store_true', help='Covered calls only')
    parser.add_argument('--csp-only', action='store_true', help='Cash-secured puts only')
    parser.add_argument('--top', type=int, default=10, help='Show top N results')
    parser.add_argument('--no-external', action='store_true', help='Skip yfinance (offline)')
    parser.add_argument('--force', action='store_true',
                        help='Skip guardrails + market hours checks — show all candidates')
    parser.add_argument('--validate', type=str, nargs='?', const='__first__',
                        help='Validate single ticker only (e.g. --validate AAPL). '
                             'Without arg, uses first ticker from watchlist.')
    args = parser.parse_args()

    candidates: list[TradeCandidate] = []

    # ── FETCH PORTFOLIO FIRST (separate connection, close before MoomooClient) ──
    PORTFOLIO, CASH, BUYING_POWER, FUND, EXISTING_OPTIONS = _fetch_live_portfolio()

    with MoomooClient() as moomoo:
        yf_client = YFinanceClient() if not args.no_external else None

        # ── LIVE WATCHLIST ──
        print("📋 Loading watchlist + portfolio...", end=' ')
        WATCHLIST = _fetch_live_watchlist(moomoo)
        print(f"{len(WATCHLIST)} tickers, {len(PORTFOLIO)} positions, ${CASH + FUND:,.0f} liquid")
        print()

        # ── MACRO CONTEXT ──
        vix = None
        regime = 'NEUTRAL'
        regime_mult = 1.0
        if yf_client:
            try:
                macro = get_macro_context(yf_client)
                vix = macro.vix
                regime = macro.market_regime
                regime_mult = macro.position_mult
            except Exception:
                pass
        if vix is None:
            vix = 20.0  # fallback when yfinance unavailable
        print(f"🌍 VIX {vix:.1f} | {regime} | Size: {regime_mult:.0%}"
              + (f" | 10Y {macro.treasury_10y:.1f}%" if yf_client and macro and macro.treasury_10y else ""))

        log.info(f"SCAN_START|tickers={len(WATCHLIST)}|regime={regime}|vix={vix:.1f}|"
                 f"size_mult={regime_mult:.0%}|positions={len(PORTFOLIO)}|"
                 f"liquid=${CASH+FUND:,.0f}|cash=${CASH:,.0f}|fund=${FUND:,.0f}|"
                 f"bp=${BUYING_POWER:,.0f}|dte={_cfg_val(lambda c: c.dte_screen_min)}-{_cfg_val(lambda c: c.dte_screen_max)}|"
                 f"csp_delta={_cfg_val(lambda c: c.delta_range('csp', regime))}|"
                 f"cc_delta={_cfg_val(lambda c: c.delta_range('cc', regime))}|"
                 f"roc_min_csp={_cfg_val(lambda c: c.roc_min_csp)}%|roc_min_cc={_cfg_val(lambda c: c.roc_min_cc)}%")
        # ── VALIDATE: single ticker mode ──
        if args.validate:
            target = args.validate
            if target == '__first__':
                target = WATCHLIST[0].replace('US.', '') if WATCHLIST else 'V'
            # Find matching ticker in watchlist
            match = [t for t in WATCHLIST if target.upper() in t.upper()]
            if match:
                WATCHLIST = [match[0]]
                log.info(f"VALIDATE mode: only scanning {WATCHLIST[0]}")
                print(f"🔍 Validate mode — only scanning: {WATCHLIST[0].replace('US.', '')}")
            else:
                # Allow tickers not in watchlist — add them temporarily
                ticker = f'US.{target.upper()}'
                WATCHLIST = [ticker]
                log.info(f"VALIDATE mode: added {ticker} (not in watchlist)")
                print(f"🔍 Validate mode — scanning: {target.upper()} (adhoc, not in watchlist)")
            print()
        # ── SCAN EACH TICKER (dedup at end — don't skip entire ticker) ──
        # OPTIMIZATION: Batch all stock snapshots upfront (1 API call vs N)
        all_snaps = moomoo.get_stock_snapshots(WATCHLIST)
        snap_map = {s.ticker: s for s in all_snaps}
        spy_history = moomoo.get_price_history('US.SPY', 252)  # cached after first fetch

        for ticker in WATCHLIST:
            short = ticker.replace('US.', '')

            # Stock snapshot (from batch)
            snap = snap_map.get(ticker)
            if snap is None or snap.last_price <= 0:
                continue
            # Pre-filter: skip illiquid tickers
            if snap.bid_ask_spread_pct and snap.bid_ask_spread_pct > 5.0:
                log.debug(f"  {short}: SKIP — spread {snap.bid_ask_spread_pct:.1f}% > 5%")
                continue

            history = moomoo.get_price_history(ticker, 252)
            if history:
                enrich_stock_snapshot(snap, history, spy_history)
            log.debug(f"  {short}: ${snap.last_price:.2f} rsi={snap.rsi_14 or 0:.0f} hv30={snap.hv_30d or 0:.1%} vol_ratio={snap.volume_ratio or 0:.1f}")

            # External sentiment — use shared module
            if yf_client:
                ts = get_ticker_sentiment(yf_client, ticker)
                analyst_consensus = ts.analyst_consensus
                earnings_blackout = ts.in_earnings_blackout
                days_to_earnings = ts.days_to_earnings
                target_upside = ts.analyst_upside_pct
                news_score = ts.news_score or 50
            else:
                analyst_consensus = 'N/A'
                earnings_blackout = False
                days_to_earnings = None
                target_upside = None
                news_score = 50
            insider_sentiment = 'NEUTRAL'  # not in TickerSentiment yet

            # ── TICKER-LEVEL SCORE (1-10) ──
            iv_rank = 50.0  # default (iv_history removed — set neutral)
            ticker_score = _compute_ticker_score(
                snap=snap,
                trend_composite=_trend_composite(snap),
                analyst_consensus=analyst_consensus,
                earnings_blackout=earnings_blackout,
                insider_sentiment=insider_sentiment,
                target_upside=target_upside,
                news_score=news_score,
                regime=regime,
                regime_mult=regime_mult,
                iv_rank=iv_rank,
            )

            # ── OPTION CHAIN (with retry + yfinance fallback) ──
            dte_min = _cfg_val(lambda c: c.dte_screen_min)
            dte_max = _cfg_val(lambda c: c.dte_screen_max)
            contracts = _fetch_option_chain_resilient(moomoo, ticker, dte_min=dte_min, dte_max=dte_max)
            if not contracts:
                continue
            ticker_candidate_count = 0  # track how many pass for this ticker
            time.sleep(0.25)  # light rate limit (throttle in moomoo_client handles the rest)

            has_shares = short in PORTFOLIO and PORTFOLIO[short] >= 100

            # ── GEX GATE: negative GEX = dealer amplifying → pause CSP ──
            # Computed from chain: total gamma × OI × price
            gex_negative = _compute_chain_gex(contracts, snap.last_price) < -500000

            for c in contracts:
                # ═══ VRP GATE ═══
                vrp_ok = True
                if c.implied_vol and snap.hv_30d and snap.hv_30d > 0:
                    vrp_ok = c.implied_vol > snap.hv_30d * 0.8
                # IV sanity: moomoo returns IV as % (e.g. 41.2 = 41.2%), reject >500%
                iv_sane = c.implied_vol and 0 < c.implied_vol < 500

                # ── CSP candidates ──
                if not args.cc_only and c.option_type == 'PUT':
                    if c.bid <= 0 or (c.open_interest or 0) < _cfg_val(lambda c: c.oi_min) or (c.volume or 0) < 10:
                        continue
                    # CSP delta check from config (regime-adjusted)
                    abs_d = abs(c.delta or 0)
                    delta_range = _cfg_val(lambda c: c.delta_range('csp', regime))
                    if abs_d < delta_range[0] or abs_d > delta_range[1]:
                        continue
                    if abs_d > 0.70:
                        continue  # deep ITM, not premium selling
                        continue
                    if not iv_sane:
                        continue  # IV sanity: skip absurd IV values
                    if not vrp_ok:
                        continue  # VRP gate: skip if IV too cheap
                    if gex_negative:
                        continue  # GEX gate: skip CSP in negative GEX regime
                    roc = _csp_roc(c.bid, c.strike, c.dte)
                    if roc < _cfg_val(lambda c: c.roc_min_csp):
                        continue
                    capital = c.strike * 100

                    # ── CONCENTRATION GATE ──
                    if not args.force:
                        total_nlv = sum(PORTFOLIO.get(t, 0) * snap.last_price
                                        for t, snap2 in [(short, snap)]
                                        if snap2.last_price) + CASH + FUND
                        if capital > total_nlv * _cfg_val(lambda c: c.max_single_position_pct):
                            continue

                        # ── CASH BUFFER GATE ──
                        liquid = CASH + FUND
                        cash_pct = liquid / total_nlv if total_nlv > 0 else 0
                        if cash_pct < 0.10:
                            continue

                        if capital > BUYING_POWER * 0.8:
                            continue

                    adj_roc = roc * regime_mult
                    contract_score = ticker_score + _contract_penalty(c, abs_d, adj_roc)
                    ticker_candidate_count += 1
                    if contract_score <= 5:  # only log good candidates at INFO
                        log.info(f"CSP|{short}|${c.strike:.0f}|{c.expiry}|DTE={c.dte}|Δ={abs_d:.3f}|"
                                 f"bid={c.bid:.2f}|IV={c.implied_vol:.0f}%|OI={c.open_interest}|"
                                 f"RoC={roc:.1f}%|score={contract_score:.1f}")
                    candidates.append(TradeCandidate(
                        ticker=short, strategy='CSP', score=round(contract_score, 2),
                        strike=c.strike, expiry=c.expiry, dte=c.dte,
                        delta=abs_d, bid=c.bid, ask=c.ask,
                        premium=c.bid * 100, annualized_roc_pct=round(roc, 1),
                        iv=c.implied_vol, iv_rank=c.iv_rank or 50,
                        open_interest=c.open_interest,
                        capital_required=capital,
                        reason=_reason(ticker_score, contract_score, 'CSP')
                        + (' ⚠️ GEX' if gex_negative else ''),
                    ))

                # ── CC candidates ──
                if not args.csp_only and c.option_type == 'CALL' and has_shares:
                    if c.bid <= 0 or (c.open_interest or 0) < _cfg_val(lambda c: c.oi_min) or (c.volume or 0) < 10:
                        continue
                    # CC delta check from config (regime-adjusted)
                    cc_delta_range = _cfg_val(lambda c: c.delta_range('cc', regime))
                    if c.delta < cc_delta_range[0] or c.delta > cc_delta_range[1]:
                        continue
                    if not iv_sane:
                        continue  # IV sanity
                    if not vrp_ok:
                        continue  # VRP gate
                    cost_basis = snap.last_price
                    roc = (c.bid / cost_basis) * (365.0 / c.dte) * 100 if cost_basis > 0 else 0
                    if roc < _cfg_val(lambda c: c.roc_min_cc):
                        continue
                    contract_score = ticker_score + _contract_penalty(c, c.delta, roc)
                    ticker_candidate_count += 1
                    if contract_score <= 5:
                        log.info(f"CC|{short}|${c.strike:.0f}|{c.expiry}|DTE={c.dte}|Δ={c.delta:.3f}|"
                                 f"bid={c.bid:.2f}|IV={c.implied_vol:.0f}%|OI={c.open_interest}|"
                                 f"RoC={roc:.1f}%|score={contract_score:.1f}")
                    candidates.append(TradeCandidate(
                        ticker=short, strategy='CC', score=round(contract_score, 2),
                        strike=c.strike, expiry=c.expiry, dte=c.dte,
                        delta=c.delta, bid=c.bid, ask=c.ask,
                        premium=c.bid * 100, annualized_roc_pct=round(roc, 1),
                        iv=c.implied_vol, iv_rank=c.iv_rank or 50,
                        open_interest=c.open_interest,
                        capital_required=short in PORTFOLIO and PORTFOLIO[short] * 100 or 0,
                        reason=_reason(ticker_score, contract_score, 'CC'),
                    ))

            if ticker_candidate_count > 0:
                log.info(f"  {short}: {ticker_candidate_count} candidates passed")

    # ── RANK & OUTPUT ──
    # Dedup: one best per ticker (includes tickers with existing options)
    seen = set()
    deduped = []
    candidates.sort(key=lambda x: x.score)
    for c in candidates:
        if c.ticker not in seen:
            deduped.append(c)
            seen.add(c.ticker)
    top = deduped[:args.top]

    top_str = ", ".join(f"{c.ticker}:{c.strategy}${c.strike:.0f}s={c.score:.1f}" for c in top[:5])
    log.info(f"SCAN_DONE|raw={len(candidates)}|top={len(top)}|{top_str}")

    # Find best of each strategy for 💡 marker
    best_csp = None
    best_cc = None
    for c in top:
        if c.strategy == 'CSP' and best_csp is None:
            best_csp = c
        if c.strategy == 'CC' and best_cc is None:
            best_cc = c

    print(f"\n{'='*90}")
    print(f"  🎯 TOP {len(top)} TRADES  (1 = best, 10 = worst)")
    print(f"{'='*90}")

    if not top:
        print("  No candidates found. Relax DTE/delta/ROC filters or check data.")
        return

    # Column order: # Ticker Strat Strike Expiry DTE Δ Bid RoC IV OI Capital Score Reason
    print(f"  {'':>5s} {'Ticker':<6s} {'Strat':>5s} "
          f"{'Strike':>10s} {'Expiry':>12s} {'DTE':>3s} {'Δ':>6s} "
          f"{'Bid':>8s} {'RoC':>7s} {'IV':>7s} {'OI':>6s} {'Capital':>10s} {'Score':>6s}  Reason")
    print(f"  {'─'*5} {'─'*6} {'─'*5} "
          f"{'─'*10} {'─'*12} {'─'*3} {'─'*6} "
          f"{'─'*8} {'─'*7} {'─'*7} {'─'*6} {'─'*10} {'─'*6}  {'─'*40}")

    for i, c in enumerate(top, 1):
        score_star = _score_stars(c.score)
        # 💡 marker for best CSP / best CC
        if c is best_csp or c is best_cc:
            bulb = "💡 "
        else:
            bulb = "   "
        # $ connected to amounts (no space)
        strike_str = f"{'$'+f'{c.strike:,.2f}':>10}"
        bid_str = f"{'$'+f'{c.bid:,.2f}':>8}"
        capital_str = f"{'$'+f'{c.capital_required:,.0f}':>10}"
        print(f"  {bulb}{i:>2d} {c.ticker:<6s} {c.strategy:>5s} "
              f"{strike_str} {c.expiry:>12s} {c.dte:>3d} {c.delta:>5.3f} "
              f"{bid_str} {c.annualized_roc_pct:>6.1f}% {c.iv:>6.1f}% {c.open_interest:>6d} "
              f"{capital_str} {score_star:<6s} {c.reason[:45]}")

    # ── Regime warning ──
    if regime in ('VOLATILE', 'BEARISH'):
        print(f"\n  ⚠️  {regime} regime — favor CC over CSP, reduce position size by 25-50%")
    if regime == 'BULLISH':
        print(f"\n  ✅ BULLISH regime — CSP premium is favorable, assignment risk lower")

    # ── Log top picks for backtesting / paper tracking ──


# ═══════════════════════════════════════════════════════════════
# SCORING ENGINE — moved to src/scoring/screener_score.py.
# Re-exported above (import block) for backwards compatibility.
# ═══════════════════════════════════════════════════════════════



if __name__ == '__main__':
    main()
