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
    python3 scripts/screener.py --ps-only         # put credit spreads only
    python3 scripts/screener.py --top 5           # top 5 results
    python3 scripts/screener.py --no-external     # skip yfinance (faster, offline)
"""

import argparse
import os
import sys
import time
import warnings
from datetime import date, datetime
from typing import Optional

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from src.logging_setup import get_logger
log = get_logger('screener')

from src.data.moomoo_client import MoomooClient
from src.data.portfolio_loader import fetch_live_portfolio
from src.data.yfinance_client import YFinanceClient
from src.data.compute import enrich_stock_snapshot
from src.data.models import StockSnapshot, OptionSnapshot, TradeCandidate
from src.data.watchlist import fetch_live_watchlist
from src.data.do_not_wheel_list import DoNotWheelList, is_wheel_eligible
from src.data.guardrails import GuardrailChecker, SECTOR_MAP
from src.filters.contract_filters import passes_all_gates, cc_roc
from src.analysis.sentiment import (
    get_macro_context, get_ticker_sentiment, get_watchlist_sentiment,
    score_analyst_consensus, score_news_sentiment, score_earnings_blackout,
)
from src.config import get_config
from src.strategies.credit_spread import score_put_credit_spreads

# Scoring engine — re-exported for backward compatibility (oie_engine tests, test_screener_scoring)
from src.scoring.screener_score import (
    _cfg_val,
    _compute_ticker_score, _contract_penalty, _trend_composite,
    _score_technical, _score_options_eco, _score_fundamental,
    _score_external, _score_macro, _csp_roc,
    _score_stars, _reason, _compute_chain_gex,
)


def main():
    parser = argparse.ArgumentParser(description='Options Wheel Screener')
    parser.add_argument('--cc-only', action='store_true', help='Covered calls only')
    parser.add_argument('--csp-only', action='store_true', help='Cash-secured puts only')
    parser.add_argument('--ps-only', action='store_true',
                        help='Put credit spreads only (defined-risk; suggestion-only)')
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
    (PORTFOLIO, CASH, CASH_BP, FUND, EXISTING_OPTIONS,
     OPTIONS_DICT, MARGIN_BP, CSP_LIABILITY) = fetch_live_portfolio()
    # Use margin-inclusive BP for capital checks (reflects true Reg-T capacity)
    BUYING_POWER = MARGIN_BP if MARGIN_BP > 0 else CASH_BP
    # Compute CC shares already committed (existing short calls × 100 per contract)
    # Each CC contract = -1 qty = 100 shares owed if assigned
    CC_SHARES_COMMITTED: dict[str, int] = {}
    for code, opt in OPTIONS_DICT.items():
        if opt.get('type') == 'CALL':
            t = opt.get('ticker', '')
            CC_SHARES_COMMITTED[t] = CC_SHARES_COMMITTED.get(t, 0) + abs(opt.get('qty', 0)) * 100
    # Use the same worst-case formula as guardrails for CSP cash eligibility:
    #   available = liquid + (margin-BP × bp_margin_buffer) + (CC notional × cc_assignment_buffer)
    cc_notional = sum(
        abs(o['strike']) * abs(o['qty']) * 100
        for o in OPTIONS_DICT.values()
        if o.get('type') == 'CALL'
    )
    cfg_guard = get_config()
    LIQUID = CASH + FUND
    CSP_AVAILABLE = LIQUID + BUYING_POWER * cfg_guard.bp_margin_buffer + cc_notional * cfg_guard.cc_assignment_buffer

    with MoomooClient() as moomoo:
        yf_client = YFinanceClient() if not args.no_external else None

        # ── LIVE WATCHLIST ──
        print("📋 Loading watchlist + portfolio...", end=' ')
        WATCHLIST = fetch_live_watchlist(moomoo.ctx)
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
        # OPTIMIZATION: Batch all stock snapshots upfront (1 API call vs N).
        # Includes held tickers that aren't in the watchlist (VOO, SKHY, …)
        # so the guardrail NLV below prices the WHOLE portfolio — same
        # formula as Portfolio.net_liquidation in portfolio.py.
        all_snaps = moomoo.get_stock_snapshots(
            list(set(WATCHLIST + [f'US.{t}' for t in PORTFOLIO.keys()])))
        snap_map = {s.ticker: s for s in all_snaps}
        spy_history = moomoo.get_price_history('US.SPY', 252)  # cached after first fetch

        # ── PORTFOLIO GUARDRAILS (same GuardrailChecker as portfolio.py /
        #    the OIE engine — keeps all three surfaces in agreement) ──
        # When the portfolio-level CSP blocks fire (CSP capital deployed over
        # limit, cash buffer critical), new CSP candidates are suppressed.
        # CC candidates are unaffected (share-secured, not cash-secured).
        # --force skips this like every other gate.
        csp_blocked_reasons: list[str] = []
        if not args.force:
            snap_px = {s.ticker: s.last_price for s in all_snaps if s.last_price}
            stock_mv = sum(
                qty * snap_px.get(f'US.{t}', 0) for t, qty in PORTFOLIO.items())
            nlv = (CASH + FUND) + stock_mv  # same formula as Portfolio.net_liquidation
            opt_tickers = {o.get('ticker') for o in OPTIONS_DICT.values()}
            gc_positions = [{
                'ticker': t,
                'notional': qty * snap_px.get(f'US.{t}', 0),
                'sector': SECTOR_MAP.get(t, 'Unknown'),
                'csp_liability': sum(
                    abs(o['strike']) * abs(o['qty']) * 100
                    for o in OPTIONS_DICT.values()
                    if o.get('ticker') == t and o.get('type') == 'PUT'),
            } for t, qty in PORTFOLIO.items() if t in opt_tickers]
            cc_assignment = sum(
                abs(o['strike']) * abs(o['qty']) * 100
                for o in OPTIONS_DICT.values() if o.get('type') == 'CALL')
            gc = GuardrailChecker(net_liq=nlv, cash=CASH + FUND,
                                  buying_power=BUYING_POWER,
                                  open_positions=gc_positions,
                                  cc_assignment_notional=cc_assignment,
                                  regime=regime)
            gr = gc.check()
            csp_blocked_reasons = [b for b in gr.blocks
                                   if 'CSP' in b or 'cash' in b.lower()]
            if csp_blocked_reasons:
                print(f"🛡️  CSP candidates suppressed — portfolio guardrail BLOCK active:")
                for b in csp_blocked_reasons[:2]:
                    print(f"     🔴 {b}")
                print()
                log.info(f"CSP_SUPPRESSED|blocks={'|'.join(csp_blocked_reasons)}")

        for ticker in WATCHLIST:
            short = ticker.replace('US.', '')

            # Stock snapshot (from batch)
            snap = snap_map.get(ticker)
            if snap is None or snap.last_price <= 0:
                continue
            # Pre-filter: skip illiquid tickers (config: liquidity.bid_ask_spread_max_pct)
            if snap.bid_ask_spread_pct and snap.bid_ask_spread_pct > cfg_guard.spread_max_pct:
                log.debug(f"  {short}: SKIP — spread {snap.bid_ask_spread_pct:.1f}% "
                          f"> {cfg_guard.spread_max_pct:.0f}%")
                continue

            # Pre-filter: Wheel eligibility (read-only daily check).
            # Uses moomoo snapshot data (net_profit + eps_ttm) already fetched —
            # skips clear loss-makers automatically. The watchlist is the master
            # list; do_not_wheel.yaml remains a manual override below.
            eligible, inelig_reason = is_wheel_eligible(snap, short)
            if not eligible:
                log.debug(f"  {short}: SKIP — {inelig_reason}")
                continue

            # Manual override: honor a hand-edited do_not_wheel.yaml (still supported).
            if DoNotWheelList().is_excluded(short):
                expiration = DoNotWheelList().get_expiration(short)
                reason = DoNotWheelList().get_reason(short)
                log.debug(f"  {short}: SKIP — manual override until {expiration}: {reason}")
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
            contracts = moomoo.get_option_snapshots_resilient(ticker, dte_min=dte_min, dte_max=dte_max)
            if not contracts:
                continue
            ticker_candidate_count = 0  # track how many pass for this ticker
            time.sleep(0.25)  # light rate limit (throttle in moomoo_client handles the rest)

            has_shares = short in PORTFOLIO and PORTFOLIO[short] >= 100

            # ── GEX GATE: negative GEX = dealer amplifying → pause CSP/PCS ──
            # Computed from chain: total gamma × OI × price
            gex_negative = _compute_chain_gex(contracts, snap.last_price) < -500000

            # Net liquidation for this ticker context (held shares + cash + fund).
            # Hoisted out of the contract loop — same value each iteration.
            total_nlv = (PORTFOLIO.get(short, 0) * snap.last_price
                         if snap.last_price else 0.0) + CASH + FUND

            for c in contracts:
                # ── CSP candidates ──
                if not args.cc_only and not args.ps_only and c.option_type == 'PUT':
                    # Portfolio-level CSP block (deployed % over limit / cash
                    # critical) — same rule the OIE engine enforces at execute
                    # time. Suppressed here so the screener never surfaces a
                    # CSP the guardrails would refuse. --force bypasses.
                    if csp_blocked_reasons:
                        continue
                    abs_d = abs(c.delta or 0)
                    # CSP headroom: how much ADDITIONAL CSP capital fits within the
                    # worst-case coverage formula. Uses the same formula as guardrails:
                    #   available = liquid + (margin-BP × buffer) + (CC notional × buffer)
                    # Headroom = available − existing CSP liability
                    csp_headroom = max(0.0, CSP_AVAILABLE - CSP_LIABILITY)
                    ok, reason = passes_all_gates(
                        c, 'CSP', regime, snap,
                        skip_concentration=args.force,
                        skip_cash_buffer=args.force,
                        net_liq=total_nlv, cash=CASH + FUND,
                        buying_power=BUYING_POWER,
                        csp_headroom=csp_headroom)
                    if not ok:
                        continue
                    # GEX gate (screener-specific)
                    if gex_negative:
                        continue
                    roc = _csp_roc(c.bid, c.strike, c.dte)
                    capital = c.strike * 100

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
                # Gate 1: must hold ≥100 free shares (total − committed CCs)
                cc_committed = CC_SHARES_COMMITTED.get(short, 0)
                shares_held = PORTFOLIO.get(short, 0)
                free_shares = shares_held - cc_committed
                cc_eligible = has_shares and (free_shares >= 100)
                if not args.force and not cc_eligible:
                    if has_shares and free_shares < 100:
                        log.debug(f"  {short} CC SKIP: {shares_held} shares − {cc_committed} committed = {free_shares} free (< 100)")
                    continue
                if not args.csp_only and not args.ps_only and c.option_type == 'CALL' and (has_shares or args.force):
                    ok, reason = passes_all_gates(
                        c, 'CC', regime, snap,
                        skip_concentration=True,
                        skip_cash_buffer=True)
                    if not ok:
                        continue
                    # When --force is off, enforce available-shares gate
                    if not args.force and not cc_eligible:
                        continue
                    roc = cc_roc(c.bid, snap.last_price, c.dte)
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

            # ── PUT CREDIT SPREAD (PCS) candidates ──
            # Defined-risk income supplement. Reuses the PUT contracts already
            # fetched (no extra API call). Scores on max_loss capital, not the
            # full strike — honors "never prefer margin" (GOAL.md #4).
            # Suggestion-only: never auto-executed; surfaced for manual review.
            if not args.cc_only and not args.csp_only:
                puts = [c for c in contracts if c.option_type == 'PUT']
                ps_cands = score_put_credit_spreads(
                    puts, snap, ticker=short, ticker_score=ticker_score,
                    regime=regime, net_liq=total_nlv,
                    cash=(CASH + FUND) if not args.force else 0,
                    gex_negative=gex_negative,
                )
                for ps in ps_cands:
                    ticker_candidate_count += 1
                    if ps.score <= 5:
                        log.info(
                            f"PCS|{short}|${ps.strike:.0f}/${ps.long_strike:.0f}|"
                            f"{ps.expiry}|DTE={ps.dte}|Δ={ps.delta:.3f}|"
                            f"credit=${ps.net_credit:.2f}|width=${ps.spread_width:.0f}|"
                            f"maxLoss=${ps.max_loss:.2f}|RoC={ps.annualized_roc_pct:.1f}%|"
                            f"score={ps.score:.1f}")
                    candidates.append(ps)

            if ticker_candidate_count > 0:
                log.info(f"  {short}: {ticker_candidate_count} candidates passed")

    # ── RANK & OUTPUT ──
    # Dedup: one best PER STRATEGY per ticker. A ticker may show a CSP and a
    # PCS side-by-side so the user can compare "own the shares" vs "defined
    # risk". CC/CSP/PS are mutually distinct keys.
    seen = set()
    deduped = []
    candidates.sort(key=lambda x: x.score)
    for c in candidates:
        key = (c.ticker, c.strategy)
        if key not in seen:
            deduped.append(c)
            seen.add(key)
    top = deduped[:args.top]

    top_str = ", ".join(f"{c.ticker}:{c.strategy}${c.strike:.0f}s={c.score:.1f}" for c in top[:5])
    log.info(f"SCAN_DONE|raw={len(candidates)}|top={len(top)}|{top_str}")

    # Find best of each strategy for 💡 marker
    best_csp = None
    best_cc = None
    best_ps = None
    for c in top:
        if c.strategy == 'CSP' and best_csp is None:
            best_csp = c
        if c.strategy == 'CC' and best_cc is None:
            best_cc = c
        if c.strategy == 'PS' and best_ps is None:
            best_ps = c

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
        # 💡 marker for best CSP / best CC / best PS
        if c is best_csp or c is best_cc or c is best_ps:
            bulb = "💡 "
        else:
            bulb = "   "
        # Spread rows render short/long strikes, net credit as the "bid", and
        # max_loss as the capital (not the full short strike). Single-leg rows
        # render as before.
        if c.strategy == 'PS' and c.long_strike is not None:
            strike_str = f"{'$'+f'{c.strike:g}'+'/'+f'${c.long_strike:g}':>10}"
            bid_str = f"{'$'+f'{c.net_credit:,.2f}':>8}c"
            capital_str = f"{'$'+f'{c.capital_required:,.0f}':>10}"
        else:
            strike_str = f"{'$'+f'{c.strike:,.2f}':>10}"
            bid_str = f"{'$'+f'{c.bid:,.2f}':>8}"
            capital_str = f"{'$'+f'{c.capital_required:,.0f}':>10}"
        print(f"  {bulb}{i:>2d} {c.ticker:<6s} {c.strategy:>5s} "
              f"{strike_str} {c.expiry:>12s} {c.dte:>3d} {c.delta:>5.3f} "
              f"{bid_str} {c.annualized_roc_pct:>6.1f}% {c.iv:>6.1f}% {c.open_interest:>6d} "
              f"{capital_str} {score_star:<6s} {c.reason[:45]}")

    # PCS legend (only if any spread is shown)
    if best_ps is not None:
        print(f"\n  📐 PS = put credit spread (defined risk). Strike col = short/long. "
              f"'Bid..c' = net credit. Capital = max loss (cash-backed). "
              f"RoC on max loss.")

    # ── Regime warning ──
    if regime in ('VOLATILE', 'BEARISH'):
        print(f"\n  ⚠️  {regime} regime — favor CC over CSP, reduce position size by 25-50%")
    if regime == 'BULLISH':
        print(f"\n  ✅ BULLISH regime — CSP premium is favorable, assignment risk lower")

    # ── Exit-management hint for tickers already held (trend-modulated rolls) ──
    # If a top candidate's ticker is one you already have an open option on, a
    # winning position there may be worth rolling (down-and-out CSP / up-and-out
    # CC) instead of flat-closing — see specs/profit-loss-management-spec.md §4.3.
    held_in_top = [c for c in top if c.ticker in EXISTING_OPTIONS]
    if held_in_top:
        tickers = ', '.join(sorted({c.ticker for c in held_in_top}))
        print(f"\n  🔄 HELD WINNERS in trend: {tickers}")
        print(f"     If any open position here is ≥50% captured, consider a net-credit roll")
        print(f"     (down-and-out CSP / up-and-out CC) to bank profit + stay in the thesis.")

    # ── Log top picks for backtesting / paper tracking ──


# ═══════════════════════════════════════════════════════════════
# SCORING ENGINE — moved to src/scoring/screener_score.py.
# Re-exported above (import block) for backwards compatibility.
# ═══════════════════════════════════════════════════════════════



if __name__ == '__main__':
    main()
