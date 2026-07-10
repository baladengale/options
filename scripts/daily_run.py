#!/usr/bin/env python3
"""
Daily Run — unified workflow: screen watchlist + check portfolio + log everything.

Single command that orchestrates screener, portfolio_check, and trade logging.
Stores all data in db/options.db for audit trail and future backtesting.

Usage:
    python3 scripts/daily_run.py                    # full run
    python3 scripts/daily_run.py --no-external      # offline mode
    python3 scripts/daily_run.py --top 5             # top 5 screener picks
"""

import argparse
import os
import re
import sys
import time
import warnings
from datetime import date, datetime
from typing import Optional

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from moomoo import OpenSecTradeContext, TrdEnv, RET_OK
from src.data.moomoo_client import MoomooClient
from src.data.yfinance_client import YFinanceClient
from src.data.compute import enrich_stock_snapshot
from src.data.iv_history import IVHistoryTracker
from src.data.trade_log import TradeLog, DailyRunDB
from src.data.guardrails import GuardrailChecker, SECTOR_MAP

# ── Import scoring logic from screener + portfolio_check ──
# We reuse their functions directly — no subprocess, no CLI parsing
from scripts.screener import (
    _fetch_live_watchlist,
    _score_technical as screener_score_technical,
    _score_options_eco as screener_score_options,
    _score_external as screener_score_external,
    _score_macro as screener_score_macro,
    _contract_penalty as screener_contract_penalty,
    _csp_roc, _compute_chain_gex,
    TradeCandidate,
)
from scripts.portfolio_check import (
    _score_holding, _find_best_cc, _score_option,
)

# Watchlist default
_DEFAULT_WATCHLIST = [
    'US.V', 'US.MSFT', 'US.GOOGL', 'US.AAPL', 'US.AMZN',
    'US.NVDA', 'US.META', 'US.AVGO', 'US.ADBE', 'US.CRM', 'US.AMD',
]


def main():
    parser = argparse.ArgumentParser(description='Daily Run — screen + check + log')
    parser.add_argument('--no-external', action='store_true', help='Skip yfinance')
    parser.add_argument('--top', type=int, default=10, help='Top N screener picks')
    parser.add_argument('--archive-chains', action='store_true',
                        help='Archive full option chains to DB (adds ~60s, builds historical DB)')
    args = parser.parse_args()

    run_id = date.today().strftime('%Y-%m-%d')  # one run per day, upsert on re-run
    today = date.today()

    # ── Auto-sync portfolio from REAL account ──
    try:
        from src.data.portfolio_sync import PortfolioSync
        with PortfolioSync() as ps:
            ps.sync_all()
    except Exception as e:
        print(f"⚠️  Portfolio sync skipped: {e}")

    db = DailyRunDB()
    trade_log = TradeLog()
    iv_tracker = IVHistoryTracker()

    # ── AUTO-PRUNE: keep last 7 days of signals/positions, 30 days of chains ──
    db.prune_old_runs(keep_days=7)
    db.prune_old_chains(keep_days=30)

    # ═══════════════════════════════════════════════════════════
    # 0. MACRO CONTEXT
    # ═══════════════════════════════════════════════════════════
    print("🌍 Loading macro...", end=' ')
    macro = None
    regime, regime_mult, vix = 'NEUTRAL', 1.0, 20.0
    fg_value = None

    if not args.no_external:
        try:
            from src.analysis.sentiment import get_macro_context
            yf = YFinanceClient()
            macro = get_macro_context(yf)
            regime = macro.market_regime
            regime_mult = macro.position_mult
            vix = macro.vix or 20.0
            fg = yf.get_fear_greed()
            fg_value = fg['value'] if fg else None
        except Exception:
            pass

    macro_data = {
        'vix': vix,
        'vvix': macro.vvix if macro else None,
        'dxy': macro.dxy if macro else None,
        'treasury_10y': macro.treasury_10y if macro else None,
        'yield_spread_10y2y': macro.yield_spread_10y2y if macro else None,
        'credit_spread': macro.hyg_ief_spread if macro else None,
        'regime': regime,
        'regime_score': macro.regime_score if macro else 0,
        'position_mult': regime_mult,
        'fear_greed': fg_value,
    }
    print(f"VIX {vix:.1f} | {regime} | Size: {regime_mult:.0%}")

    # ═══════════════════════════════════════════════════════════
    # 1. PORTFOLIO + WATCHLIST
    # ═══════════════════════════════════════════════════════════
    print("📋 Loading portfolio + watchlist...", end=' ')
    PORTFOLIO, CASH, BP, OPTION_POSITIONS = _fetch_live_portfolio_full()

    with MoomooClient() as moomoo:
        WATCHLIST = _fetch_live_watchlist(moomoo)
        yf_client = YFinanceClient() if not args.no_external else None
        print(f"{len(WATCHLIST)} tickers, {len(PORTFOLIO)} positions, ${CASH:,.0f} cash\n")

        screener_candidates: list[TradeCandidate] = []
        position_decisions: list[dict] = []
        chain_snapshots: dict[str, list[dict]] = {}
        all_tickers_to_chain = set(t.replace('US.', '') for t in WATCHLIST) | set(PORTFOLIO.keys())

        # ═══════════════════════════════════════════════════════
        # 2. SCREEN WATCHLIST (reuse screener logic)
        # ═══════════════════════════════════════════════════════
        print(f"{'='*90}")
        print(f"  🔍 SCREENING {len(WATCHLIST)} TICKERS")
        print(f"{'='*90}")

        for ticker in WATCHLIST:
            short = ticker.replace('US.', '')
            snap = moomoo.get_stock_snapshot(ticker)
            if snap is None or snap.last_price <= 0:
                continue

            history = moomoo.get_price_history(ticker, 252)
            if history:
                enrich_stock_snapshot(snap, history)

            # Reuse screener scoring
            trend_comp = _trend_composite(snap)
            analyst, earnings_blk, insider, target_up, news_sc = _get_external(yf_client, short)

            iv_rank = 50.0
            iv_data = iv_tracker.get_iv_rank(short) if iv_tracker else None
            if iv_data:
                iv_rank = iv_data['iv_rank']

            ticker_score = _compute_ticker_score(
                snap, trend_comp, analyst, earnings_blk, insider, target_up,
                news_sc, regime, regime_mult, iv_rank,
            )

            # Option chain + GEX
            contracts = moomoo.get_option_snapshots(ticker, dte_min=30, dte_max=45)
            if not contracts:
                continue
            gex_neg = _compute_chain_gex(contracts, snap.last_price) < -500000
            has_shares = short in PORTFOLIO and PORTFOLIO[short] > 0

            # Log chain snapshot
            if args.archive_chains and contracts:
                chain_snapshots[short] = [
                    {'strike': c.strike, 'expiry': c.expiry, 'dte': c.dte,
                     'type': c.option_type, 'bid': c.bid, 'ask': c.ask,
                     'delta': c.delta, 'gamma': c.gamma, 'theta': c.theta,
                     'vega': c.vega, 'iv': c.implied_vol,
                     'oi': c.open_interest, 'volume': c.volume}
                    for c in contracts[:500]  # cap at 500 per ticker
                ]

            for c in contracts:
                iv_sane = c.implied_vol and 0 < c.implied_vol < 500
                vrp_ok = True
                if c.implied_vol and snap.hv_30d and snap.hv_30d > 0:
                    vrp_ok = c.implied_vol > snap.hv_30d * 0.8

                # CSP
                if c.option_type == 'PUT':
                    if not (c.bid > 0 and (c.open_interest or 0) >= 10 and (c.volume or 0) >= 10):
                        continue
                    abs_d = abs(c.delta or 0)
                    if abs_d < 0.05 or abs_d > 0.30 or abs_d > 0.70:
                        continue
                    if not iv_sane or not vrp_ok or gex_neg:
                        continue
                    roc = _csp_roc(c.bid, c.strike, c.dte)
                    if roc < 12.0:
                        continue
                    capital = c.strike * 100
                    total_nlv = sum(PORTFOLIO.get(t, 0) * snap.last_price for t in [short] if snap.last_price) + CASH + 48500
                    if capital > total_nlv * 0.08 or capital > BP * 0.8:
                        continue
                    adj_roc = roc * regime_mult
                    sc = ticker_score + screener_contract_penalty(c, abs_d, adj_roc)
                    screener_candidates.append(TradeCandidate(
                        ticker=short, strategy='CSP', score=round(sc, 2),
                        strike=c.strike, expiry=c.expiry, dte=c.dte,
                        delta=abs_d, bid=c.bid, ask=c.ask,
                        premium=c.bid * 100, annualized_roc_pct=round(roc, 1),
                        iv=c.implied_vol, iv_rank=iv_rank,
                        open_interest=c.open_interest or 0,
                        capital_required=capital,
                        reason=_reason(ticker_score, sc, 'CSP'),
                    ))

                # CC
                if c.option_type == 'CALL' and has_shares:
                    if not (c.bid > 0 and (c.open_interest or 0) >= 10 and (c.volume or 0) >= 10):
                        continue
                    if (c.delta or 0) < 0.15 or (c.delta or 0) > 0.35:
                        continue
                    if not iv_sane or not vrp_ok:
                        continue
                    roc = (c.bid / snap.last_price) * (365.0 / c.dte) * 100 if snap.last_price and c.dte else 0
                    if roc < 8.0:
                        continue
                    sc = ticker_score + screener_contract_penalty(c, c.delta, roc)
                    screener_candidates.append(TradeCandidate(
                        ticker=short, strategy='CC', score=round(sc, 2),
                        strike=c.strike, expiry=c.expiry, dte=c.dte,
                        delta=c.delta, bid=c.bid, ask=c.ask,
                        premium=c.bid * 100, annualized_roc_pct=round(roc, 1),
                        iv=c.implied_vol, iv_rank=iv_rank,
                        open_interest=c.open_interest or 0,
                        capital_required=PORTFOLIO.get(short, 0) * 100,
                        reason=_reason(ticker_score, sc, 'CC'),
                    ))

        # Sort and print top picks
        screener_candidates.sort(key=lambda x: x.score)
        top = screener_candidates[:args.top]

        print(f"\n  🎯 TOP {len(top)} PICKS:")
        print(f"  {'#':>2s} {'Ticker':<8s} {'Strat':>4s} {'Score':>6s} {'Strike':>9s} "
              f"{'Expiry':>12s} {'DTE':>4s} {'Δ':>6s} {'Bid':>7s} {'RoC':>6s} {'IV':>6s} {'OI':>6s}")
        print(f"  {'-'*2} {'-'*8} {'-'*4} {'-'*6} {'-'*9} {'-'*12} {'-'*4} {'-'*6} {'-'*7} {'-'*6} {'-'*6} {'-'*6}")
        for i, c in enumerate(top):
            print(f"  {i+1:>2d} {c.ticker:<8s} {c.strategy:>4s} {c.score:>5.1f} "
                  f"${c.strike:>8,.2f} {c.expiry:>12s} {c.dte:>4d} "
                  f"{c.delta:>5.3f} ${c.bid:>6,.2f} {c.annualized_roc_pct:>5.1f}% "
                  f"{c.iv:>5.1f}% {c.open_interest:>6,d}")

        # ═══════════════════════════════════════════════════════
        # 3. CHECK POSITIONS (reuse portfolio_check logic)
        # ═══════════════════════════════════════════════════════
        # Stocks
        stock_decisions = []
        for ticker, pos in PORTFOLIO.items():
            qty, cost = pos, 0
            full_ticker = f'US.{ticker}'
            snap = moomoo.get_stock_snapshot(full_ticker)
            if snap is None or snap.last_price <= 0:
                continue
            price = snap.last_price
            history = moomoo.get_price_history(full_ticker, 252)
            if history:
                enrich_stock_snapshot(snap, history)
            score = _score_holding(snap, ticker, yf_client, regime, regime_mult)
            best_cc = _find_best_cc(moomoo, ticker, snap, qty, yf_client, regime, regime_mult)
            dec = {
                'ticker': ticker, 'type': 'STOCK', 'qty': qty, 'price': price,
                'score': score, 'cc_strike': best_cc['strike'] if best_cc else None,
                'cc_expiry': best_cc['expiry'] if best_cc else None,
                'cc_roc': best_cc['roc'] if best_cc else None,
                'action': 'SELL_CC' if best_cc else 'HOLD',
            }
            stock_decisions.append(dec)

        # Options — batch snapshot
        option_codes = list(OPTION_POSITIONS.keys())
        snapshot_map = {}
        if option_codes:
            for i in range(0, len(option_codes), 400):
                batch = option_codes[i:i + 400]
                ret, data = moomoo.ctx.get_market_snapshot(batch)
                if ret == RET_OK and data is not None:
                    for _, row in data.iterrows():
                        snapshot_map[row.get('code', '')] = row

        # Score options from live portfolio
        opt_decisions = []
        for code, pos in OPTION_POSITIONS.items():
            row = snapshot_map.get(code)
            if row is None:
                continue
            bid = float(row.get('bid_price', 0) or 0)
            delta = float(row.get('option_delta', 0) or 0)
            iv = float(row.get('iv', 0) or 0)
            dte = int(row.get('option_expiry_date_distance', 0) or 0)
            cost = pos['cost']
            qty = pos['qty']
            profit_captured = ((cost - bid) / cost * 100) if cost > 0 else 0
            pl = pos.get('pl', 0) or 0

            sc_raw, decision = _score_option(pos, type('_Opt', (), {
                'bid': bid, 'dte': dte, 'delta': delta, 'implied_vol': iv,
                'strike': pos['strike'], 'option_type': pos['type'],
            })(), profit_captured, pl, today, yf_client)

            opt_decisions.append({
                'code': code, 'ticker': pos['ticker'], 'type': 'OPTION',
                'strategy': 'CC' if pos['type'] == 'CALL' else 'CSP',
                'strike': pos['strike'], 'expiry': pos['expiry'],
                'qty': qty, 'cost': cost, 'bid': bid, 'dte': dte,
                'delta': delta, 'iv': iv, 'pl': pl,
                'profit_captured': profit_captured,
                'score': float(sc_raw) if isinstance(sc_raw, (int, float)) else 5.0,
                'action': decision,
            })

        # ═══════════════════════════════════════════════════════
        # 3.5 GUARDRAILS — capital allocation check
        # ═══════════════════════════════════════════════════════
        gc_positions = []
        for ticker in PORTFOLIO:
            snap = moomoo.get_stock_snapshot(f'US.{ticker}')
            notional = (snap.last_price * PORTFOLIO[ticker]) if snap else 0
            csp_liab = sum(
                pos['strike'] * abs(pos['qty']) * 100
                for code, pos in OPTION_POSITIONS.items()
                if pos['ticker'] == ticker and pos['type'] == 'PUT'
            )
            gc_positions.append({'ticker': ticker, 'notional': notional,
                                 'sector': SECTOR_MAP.get(ticker, 'Unknown'),
                                 'csp_liability': csp_liab})
        total_liq = CASH + sum(p['notional'] for p in gc_positions) + 48500
        gc = GuardrailChecker(net_liq=total_liq, cash=CASH, buying_power=BP,
                               open_positions=gc_positions)
        gr = gc.check()

        # ═══════════════════════════════════════════════════════
        # 4. EXECUTION SUMMARY
        # ═══════════════════════════════════════════════════════
        print(f"\n{'='*90}")
        print(f"  📋 EXECUTION SUMMARY — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*90}")

        # Trades to OPEN
        opens = [c for c in top if c.score <= 5.0]
        if opens:
            print(f"\n  🟢 OPEN ({len(opens)} candidates):")
            for c in opens[:5]:
                print(f"    {c.strategy} {c.ticker} \${c.strike:.0f} {c.expiry} "
                      f"Δ{c.delta:.2f} RoC {c.annualized_roc_pct:.1f}% Score {c.score:.1f}")

        # Trades to CLOSE
        closes = [d for d in opt_decisions if 'CLOSE' in d['action']]
        if closes:
            print(f"\n  🔴 CLOSE ({len(closes)} positions):")
            for d in closes:
                print(f"    {d['code'][:20]} {d['action']}")

        # Trades to ROLL
        rolls = [d for d in opt_decisions if 'ROLL' in d['action'].upper()]
        if rolls:
            print(f"\n  🟡 ROLL ({len(rolls)} positions):")
            for d in rolls:
                print(f"    {d['code'][:20]} {d['action']}")

        # CC opportunities
        cc_ops = [d for d in stock_decisions if d['action'] == 'SELL_CC']
        if cc_ops:
            print(f"\n  📈 SELL CC ({len(cc_ops)} positions):")
            for d in cc_ops:
                print(f"    {d['ticker']} \${d['cc_strike']:.0f} {d['cc_expiry']} "
                      f"RoC {d['cc_roc']:.1f}%")

        # ═══════════════════════════════════════════════════════
        # 5. LOG EVERYTHING
        # ═══════════════════════════════════════════════════════
        print(f"\n  📝 Logging to db/options.db...", end=' ')

        db.log_daily_run(run_id, macro_data)

        for c in top[:args.top]:
            db.log_run_signal(run_id, c.ticker, c.strategy, c.strike, c.expiry,
                              c.dte, c.delta, c.bid, c.annualized_roc_pct,
                              c.iv, c.open_interest, c.score, c.reason)

        for d in stock_decisions:
            db.log_run_position(run_id, d['ticker'], 'STOCK', d.get('cc_strike'),
                                d.get('cc_expiry'), d['qty'], d['price'],
                                d['score'], d['action'], d.get('cc_roc'))

        for d in opt_decisions:
            db.log_run_position(run_id, d['code'], 'OPTION', d['strike'],
                                d['expiry'], d['qty'], d['bid'], d['score'],
                                d['action'], profit_captured=d.get('profit_captured'))

        if args.archive_chains:
            for ticker, chain in chain_snapshots.items():
                db.log_run_chain(run_id, ticker, chain)

        # Log screener top picks to trade log for tracking
        for c in top[:5]:
            trade_log.log_recommendation(
                ticker=c.ticker, strategy=c.strategy,
                strike=c.strike, expiry=c.expiry,
                delta=c.delta, premium=c.bid,
                contracts=1, roc_pct=c.annualized_roc_pct,
                dte=c.dte, score=c.score,
                capital_req=c.capital_required,
                mode='PAPER', notes=f'run={run_id}',
            )

        # ── GUARDRAILS REPORT ──
        print(f"\n{'='*90}")
        print(f"  🛡️  GUARDRAILS")
        print(f"{'='*90}")
        print(f"  Net Liq: ${total_liq:>12,.0f}  |  Cash: ${CASH:>10,.0f} ({gr.cash_buffer_pct:.0f}%)  |  "
              f"BP: ${BP:>10,.0f}  |  Margin: {gr.margin_used_pct:.0f}%")
        print(f"  Positions: {gr.open_positions} (max {GuardrailChecker.MAX_OPEN_POSITIONS})  |  "
              f"Max single: {gr.max_single_position_pct:.0f}%  |  "
              f"CSP liability: ${gr.worst_case_assignment:,.0f}")
        if gr.worst_case_shortfall > 0:
            print(f"  ⚠️  STRESS TEST: If all CSPs assigned, shortfall = ${gr.worst_case_shortfall:,.0f}")
        else:
            print(f"  ✅ STRESS TEST: All CSPs covered even if assigned simultaneously")

        if gr.blocks:
            print(f"\n  🔴 BLOCKED ({len(gr.blocks)}):")
            for b in gr.blocks:
                print(f"    {b}")
        if gr.warnings:
            print(f"  🟡 WARNINGS ({len(gr.warnings)}):")
            for w in gr.warnings:
                print(f"    {w}")
        if gr.all_clear and not gr.warnings:
            print(f"  ✅ All clear — within limits")

        # ── MAP EXECUTED ORDERS → TRADE LOG ──
        paper_fills = db.map_orders_to_recommendations(mode='PAPER')
        live_fills = db.map_orders_to_recommendations(mode='LIVE')
        if paper_fills or live_fills:
            parts = []
            if paper_fills: parts.append(f"{paper_fills} paper fills")
            if live_fills: parts.append(f"{live_fills} live fills")
            print(f"  📊 Mapped {', '.join(parts)} to trade log")

        print(f"✅ Done")

    trade_log.close()
    db.close()
    if iv_tracker:
        iv_tracker.close()


# ═══════════════════════════════════════════════════════════════
# Helpers (reused from screener / portfolio_check)
# ═══════════════════════════════════════════════════════════════

def _fetch_live_portfolio_full() -> tuple:
    """Fetch stocks + options + cash + BP in one trade context."""
    try:
        trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)
        ret, acc_list = trd.get_acc_list()
        if ret != RET_OK:
            trd.close()
            return {}, 0, 0, {}

        stocks, options = {}, {}
        cash, bp = 0.0, 0.0

        for _, acc in acc_list.iterrows():
            if str(acc.get('trd_env', '')) == 'SIMULATE':
                continue
            acc_id = acc['acc_id']
            ret2, funds = trd.accinfo_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
            if ret2 == RET_OK:
                f = funds.iloc[0]
                cash = (f.get('us_cash', 0) or 0)
                bp = (f.get('usd_net_cash_power', 0) or 0)
            ret3, pos_data = trd.position_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
            if ret3 != RET_OK or pos_data is None:
                continue
            for _, p in pos_data.iterrows():
                code = p['code']
                qty = p['qty']
                if re.search(r'\d{6}[CP]\d+', code):
                    parts = re.match(r'US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)', code)
                    ticker, opt_type, strike_val, expiry_str = '', '', 0.0, ''
                    if parts:
                        ticker = parts.group(1)
                        yr, mo, dy = parts.group(2), parts.group(3), parts.group(4)
                        opt_type = 'CALL' if parts.group(5) == 'C' else 'PUT'
                        strike_val = float(parts.group(6)) / 1000
                        expiry_str = f'20{yr}-{mo}-{dy}'
                    options[code] = {
                        'ticker': ticker, 'type': opt_type,
                        'strike': strike_val, 'expiry': expiry_str,
                        'qty': qty, 'cost': p.get('cost_price', 0) or 0,
                        'pl': p.get('pl_val', 0) or 0,
                    }
                elif code.startswith('US.') and '..' not in code:
                    short = code.replace('US.', '')
                    stocks[short] = p['qty']
        trd.close()
        return stocks, cash, bp, options
    except Exception:
        return {}, 0, 0, {}


def _trend_composite(snap) -> float:
    """Simplified trend composite from snapshot."""
    score = 50.0
    rsi = snap.rsi_14 or 50
    if 45 <= rsi <= 55:           score += 15
    elif 40 <= rsi <= 60:         score += 5
    elif 30 <= rsi <= 70:         score -= 5
    else:                         score -= 15
    if snap.sma_50 and snap.sma_200:
        if snap.last_price > snap.sma_50 > snap.sma_200:
            score += 20
        elif snap.last_price > snap.sma_200:
            score += 5
        else:
            score -= 10
    return max(0, min(100, score))


def _get_external(yf_client, ticker: str):
    """Fetch external sentiment data."""
    analyst, earnings_blk, insider, target_up, news_sc = 'N/A', False, 'NEUTRAL', None, 50.0
    if yf_client:
        ratings = yf_client.get_analyst_ratings(ticker)
        if ratings:
            analyst = ratings.consensus
            target_up = ratings.target_upside_pct
        earnings = yf_client.get_earnings(ticker)
        if earnings:
            earnings_blk = earnings.in_blackout
        inst = yf_client.get_institution_data(ticker)
        if inst:
            insider = inst.net_insider_sentiment
        news = yf_client.get_news_sentiment_score(ticker)
        if news:
            news_sc = news['score']
    return analyst, earnings_blk, insider, target_up, news_sc


def _compute_ticker_score(snap, trend_comp, analyst, earnings_blk, insider,
                          target_up, news_sc, regime, regime_mult, iv_rank):
    """Composite ticker score 1-10."""
    scores = {}
    scores['tech'] = screener_score_technical(snap, trend_comp) * 0.25
    scores['opt'] = screener_score_options(snap, iv_rank) * 0.25
    # Fundamental — simplified
    fund = 4.0
    if snap.pe_ratio and snap.pe_ratio < 20:  fund -= 0.5
    if snap.pe_ratio and snap.pe_ratio > 50:  fund += 1.5
    scores['fund'] = fund * 0.15
    scores['ext'] = screener_score_external(analyst, earnings_blk, insider, target_up, news_sc) * 0.20
    scores['macro'] = screener_score_macro(regime, regime_mult, earnings_blk) * 0.15
    return round(sum(scores.values()), 2)


def _reason(ticker_score: float, contract_score: float, strategy: str) -> str:
    """Human-readable reason for a trade recommendation."""
    if contract_score <= 2.0:
        return '💎 Excellent setup'
    elif contract_score <= 3.0:
        return 'Strong CC candidate' if strategy == 'CC' else 'Strong CSP candidate'
    elif contract_score <= 4.5:
        return 'Good, moderate risk'
    elif contract_score <= 6.0:
        return 'Decent, higher risk'
    else:
        return 'Marginal, review carefully'


if __name__ == '__main__':
    main()
