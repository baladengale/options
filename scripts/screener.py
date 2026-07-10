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
import sys
import warnings
from dataclasses import dataclass, field
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
from src.data.models import StockSnapshot, OptionSnapshot
from src.analysis.sentiment import (
    get_macro_context, get_ticker_sentiment, get_watchlist_sentiment,
    score_analyst_consensus, score_news_sentiment, score_earnings_blackout,
)

_iv_tracker: Optional['IVHistoryTracker'] = None


def _get_iv_tracker() -> Optional['IVHistoryTracker']:
    global _iv_tracker
    if _iv_tracker is None:
        try:
            _iv_tracker = IVHistoryTracker()
        except Exception:
            pass
    return _iv_tracker

# Watchlist from CLAUDE.md
# Default watchlist (used if moomoo watchlist fetch fails)
_DEFAULT_WATCHLIST = [
    'US.V', 'US.MSFT', 'US.GOOGL', 'US.AAPL', 'US.AMZN',
    'US.NVDA', 'US.META', 'US.AVGO', 'US.ADBE', 'US.CRM', 'US.AMD',
]

MAX_MARGIN_PCT = 30.0


def _fetch_live_watchlist(moomoo) -> list[str]:
    """Pull US stock tickers from moomoo 'Options' watchlist group. Fallback to default."""
    import re
    try:
        ret, data = moomoo.ctx.get_user_security('Options')
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


def _fetch_live_portfolio() -> tuple[dict[str, float], float, float, float]:
    """Pull live portfolio positions, cash, buying power, fund_assets. Fallback to defaults."""
    fund_usd = 0.0
    try:
        trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)
        ret, acc_list = trd.get_acc_list()
        if ret != RET_OK:
            trd.close()
            return {}, 817.0, 48638.89, 48500.0

        for _, acc in acc_list.iterrows():
            # trd_env is returned as string 'REAL' or 'SIMULATE'
            trd_env_raw = acc.get('trd_env', 'SIMULATE')
            if trd_env_raw == 'SIMULATE' or trd_env_raw == TrdEnv.SIMULATE:
                continue

            acc_id = acc['acc_id']
            # Use TrdEnv.REAL for live account
            trd_env = TrdEnv.REAL

            # Funds
            ret2, funds = trd.accinfo_query(trd_env=trd_env, acc_id=acc_id, refresh_cache=True)
            if ret2 != RET_OK:
                continue
            f = funds.iloc[0]
            usd_cash = (f.get('us_cash', 0) or 0)
            usd_bp = (f.get('usd_net_cash_power', 0) or 0)
            fund_raw = (f.get('fund_assets', 0) or 0)
            currency = str(f.get('currency', ''))
            fund_usd = (fund_raw / 7.8) if (currency == 'HKD' and fund_raw) else fund_raw

            # Positions
            ret3, pos = trd.position_list_query(trd_env=trd_env, acc_id=acc_id, refresh_cache=True)
            holdings = {}
            if ret3 == RET_OK and pos is not None and len(pos) > 0:
                import re
                for _, p in pos.iterrows():
                    code = p['code']
                    if (code.startswith('US.') and not re.search(r'\d{6}[CP]\d+', code)
                            and '..' not in code):
                        short = code.replace('US.', '')
                        holdings[short] = p['qty']
            trd.close()
            trd.close()
            return holdings, usd_cash, usd_bp, fund_usd
        trd.close()
    except Exception:
        pass
    return {}, 817.0, 48638.89, 48500.0


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
    parser.add_argument('--log', action='store_true', help='Log top picks to trade log')
    args = parser.parse_args()

    candidates: list[TradeCandidate] = []

    # ── FETCH PORTFOLIO FIRST (separate connection, close before MoomooClient) ──
    PORTFOLIO, CASH, BUYING_POWER, FUND = _fetch_live_portfolio()

    with MoomooClient() as moomoo:
        yf_client = YFinanceClient() if not args.no_external else None

        # ── LIVE WATCHLIST ──
        print("📋 Loading watchlist + portfolio...", end=' ')
        WATCHLIST = _fetch_live_watchlist(moomoo)
        print(f"{len(WATCHLIST)} tickers, {len(PORTFOLIO)} positions, ${CASH + FUND:,.0f} liquid")
        print()

        # ── MACRO CONTEXT ──
        vix = 20.0
        regime = 'NEUTRAL'
        regime_mult = 1.0
        if yf_client:
            macro = get_macro_context(yf_client)
            vix = macro.vix or 20.0
            regime = macro.market_regime
            regime_mult = macro.position_mult
            print(f"🌍 VIX {vix:.1f} | {regime} | Size: {regime_mult:.0%}"
                  + (f" | 10Y {macro.treasury_10y:.1f}%" if macro.treasury_10y else ""))

        # ── SCAN EACH TICKER ──
        for ticker in WATCHLIST:
            short = ticker.replace('US.', '')

            # Stock snapshot + technicals
            snap = moomoo.get_stock_snapshot(ticker)
            if snap is None:
                continue
            history = moomoo.get_price_history(ticker, 252)
            if history:
                spy_history = moomoo.get_price_history('US.SPY', 252)
                enrich_stock_snapshot(snap, history, spy_history)

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

            # IV rank from history (prefer history over snapshot-only)
            iv_rank = 50.0
            iv_tracker = _get_iv_tracker()
            if iv_tracker:
                iv_data = iv_tracker.get_iv_rank(short)
                if iv_data:
                    iv_rank = iv_data['iv_rank']

            # ── TICKER-LEVEL SCORE (1-10) ──
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

            # ── OPTION CHAIN ──
            contracts = moomoo.get_option_snapshots(ticker, dte_min=7, dte_max=90)
            if not contracts:
                continue

            has_shares = short in PORTFOLIO and PORTFOLIO[short] > 0

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
                    if c.bid <= 0 or (c.open_interest or 0) < 10 or (c.volume or 0) < 10:
                        continue
                    # CSP: Δ 0.05-0.30, rejects deep ITM (Δ>0.70 = synthetic stock)
                    abs_d = abs(c.delta or 0)
                    if abs_d < 0.05 or abs_d > 0.30:
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
                    if roc < 12.0:
                        continue
                    capital = c.strike * 100

                    # ── CONCENTRATION GATE: ≤15% per name (pro standard) ──
                    total_nlv = sum(PORTFOLIO.get(t, 0) * snap.last_price
                                    for t, snap2 in [(short, snap)]
                                    if snap2.last_price) + CASH + FUND
                    if capital > total_nlv * 0.15:
                        continue

                    # ── CASH BUFFER GATE: include fund assets, block if < 10% ──
                    liquid = CASH + FUND
                    cash_pct = liquid / total_nlv if total_nlv > 0 else 0
                    if cash_pct < 0.10:
                        continue

                    if capital > BUYING_POWER * 0.8:
                        continue

                    adj_roc = roc * regime_mult
                    contract_score = ticker_score + _contract_penalty(c, abs_d, adj_roc)
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
                    if c.bid <= 0 or (c.open_interest or 0) < 10 or (c.volume or 0) < 10:
                        continue
                    if c.delta < 0.15 or c.delta > 0.35:  # CC: delta 0.20-0.30
                        continue
                    if not iv_sane:
                        continue  # IV sanity
                    if not vrp_ok:
                        continue  # VRP gate
                    cost_basis = snap.last_price
                    roc = (c.bid / cost_basis) * (365.0 / c.dte) * 100 if cost_basis > 0 else 0
                    if roc < 8.0:
                        continue
                    contract_score = ticker_score + _contract_penalty(c, c.delta, roc)
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

    # ── RANK & OUTPUT ──
    # Dedup: best contract per ticker per strategy (max 1 CSP + 1 CC per ticker)
    seen = set()
    deduped = []
    candidates.sort(key=lambda x: x.score)
    for c in candidates:
        key = (c.ticker, c.strategy)
        if key not in seen:
            deduped.append(c)
            seen.add(key)
    top = deduped[:args.top]

    print(f"\n{'='*90}")
    print(f"  🎯 TOP {len(top)} TRADES  (1 = best, 10 = worst)")
    print(f"{'='*90}")

    if not top:
        print("  No candidates found. Relax DTE/delta/ROC filters or check data.")
        return

    print(f"  {'#':>2s} {'Ticker':<6s} {'Strat':>5s} {'Score':>5s} "
          f"{'Strike':>10s} {'Expiry':>12s} {'DTE':>3s} {'Δ':>6s} "
          f"{'Bid':>8s} {'RoC':>7s} {'IV':>7s} {'OI':>6s} {'Capital':>10s}  Reason")
    print(f"  {'─'*2} {'─'*6} {'─'*5} {'─'*5} "
          f"{'─'*10} {'─'*12} {'─'*3} {'─'*6} "
          f"{'─'*8} {'─'*7} {'─'*7} {'─'*6} {'─'*10}  {'─'*40}")

    for i, c in enumerate(top, 1):
        score_star = _score_stars(c.score)
        print(f"  {i:>2d} {c.ticker:<6s} {c.strategy:>5s} {score_star:<5s} "
              f"${c.strike:>9,.2f} {c.expiry:>12s} {c.dte:>3d} {c.delta:>5.3f} "
              f"${c.bid:>7,.2f} {c.annualized_roc_pct:>6.1f}% {c.iv:>6.1f}% {c.open_interest:>6d} "
              f"${c.capital_required:>9,.0f}  {c.reason[:45]}")

    # ── Summary insight ──
    print(f"\n  💡 Best CSP: {_best_of(candidates, 'CSP')}")
    print(f"  💡 Best CC:  {_best_of(candidates, 'CC')}")

    if regime in ('VOLATILE', 'BEARISH'):
        print(f"  ⚠️  {regime} regime — favor CC over CSP, reduce position size by 25-50%")
    if regime == 'BULLISH':
        print(f"  ✅ BULLISH regime — CSP premium is favorable, assignment risk lower")

    # ── Log top picks for backtesting / paper tracking ──
    if args.log and candidates:
        from src.data.trade_log import TradeLog
        with TradeLog() as log:
            for c in candidates[:10]:
                log.log_recommendation(
                    ticker=c.ticker, strategy=c.strategy,
                    strike=c.strike, expiry=c.expiry,
                    delta=c.delta, premium=c.bid,
                    contracts=1, roc_pct=c.annualized_roc_pct,
                    dte=c.dte, score=c.score,
                    capital_req=c.capital_required,
                    mode='PAPER',
                )
        print(f"  📝 Logged top 10 picks to {TradeLog.__module__}")


# ═══════════════════════════════════════════════════════════════
# SCORING ENGINE (1-10, lower = better)
# ═══════════════════════════════════════════════════════════════

def _compute_ticker_score(
    snap: StockSnapshot,
    trend_composite: float,
    analyst_consensus: str,
    earnings_blackout: bool,
    insider_sentiment: str,
    target_upside: Optional[float],
    news_score: float = 50.0,
    regime: str = 'NEUTRAL',
    regime_mult: float = 1.0,
    iv_rank: float = 50.0,
) -> float:
    """
    Ticker-level score (1-10). Lower = better.
    Every sub-score is 1 (best) to 10 (worst).
    Weighted: Technical 25% + Options Eco 25% + Fundamental 15% + External 20% + Macro 15%
    """
    scores = {}

    # 1. TECHNICAL (25%) — trend quality for premium selling
    tech = _score_technical(snap, trend_composite)
    scores['tech'] = tech * 0.25

    # 2. OPTIONS ECOSYSTEM (25%) — spread + IV rank
    opt_eco = _score_options_eco(snap, iv_rank)
    scores['opt_eco'] = opt_eco * 0.25

    # 3. FUNDAMENTAL (15%) — valuation health
    fund = _score_fundamental(snap)
    scores['fund'] = fund * 0.15

    # 4. EXTERNAL SENTIMENT (20%) — analyst + earnings + insider + news
    ext = _score_external(analyst_consensus, earnings_blackout, insider_sentiment, target_upside, news_score)
    scores['ext'] = ext * 0.20

    # 5. MACRO/RISK (15%) — VIX regime + VRP adjustment
    macro = _score_macro(regime, regime_mult, earnings_blackout)
    scores['macro'] = macro * 0.15

    return round(sum(scores.values()), 2)


def _score_technical(snap: StockSnapshot, trend_comp: float) -> float:
    """Score trend quality. 1 = ideal for premium selling, 10 = avoid."""
    # RSI: 45-55 = ideal (1), extremes = bad (10)
    rsi = snap.rsi_14 or 50
    if 45 <= rsi <= 55:  rsi_score = 1.0
    elif 40 <= rsi <= 60: rsi_score = 3.0
    elif 35 <= rsi <= 65: rsi_score = 5.0
    elif 30 <= rsi <= 70: rsi_score = 7.0
    else:                  rsi_score = 9.0

    # Trend alignment: price > SMA50 > SMA200 = good
    trend_score = 3.0
    if snap.sma_50 and snap.sma_200:
        if snap.last_price > snap.sma_50 > snap.sma_200:
            trend_score = 1.0
        elif snap.last_price > snap.sma_200:
            trend_score = 3.0
        elif snap.last_price > snap.sma_50:
            trend_score = 5.0
        elif snap.last_price > snap.sma_200:
            trend_score = 7.0
        else:
            trend_score = 9.0

    # ADX: >25 trending (good for directional)
    adx = snap.adx_14 or 20
    if adx >= 40:     adx_score = 1.0
    elif adx >= 25:   adx_score = 3.0
    elif adx >= 20:   adx_score = 5.0
    else:             adx_score = 8.0

    # Volume: good volume = better execution
    vol_score = 3.0
    if snap.volume_ratio and snap.volume_ratio > 1.0:
        vol_score = 1.0
    elif snap.volume_ratio and snap.volume_ratio > 0.7:
        vol_score = 4.0
    else:
        vol_score = 7.0

    return (rsi_score * 0.35 + trend_score * 0.30 + adx_score * 0.20 + vol_score * 0.15)


def _score_options_eco(snap: StockSnapshot, iv_rank: float = 50.0) -> float:
    """Score options ecosystem quality. 1 = great, 10 = poor."""
    # Spread
    if snap.bid_ask_spread_pct < 0.5:    spread = 1.0
    elif snap.bid_ask_spread_pct < 1.0:  spread = 3.0
    elif snap.bid_ask_spread_pct < 3.0:  spread = 5.0
    elif snap.bid_ask_spread_pct < 5.0:  spread = 7.0
    else:                                 spread = 9.0

    # IV Rank: 30-70 = ideal for premium selling
    if 30 <= iv_rank <= 70:     iv_score = 1.0
    elif 20 <= iv_rank <= 80:   iv_score = 3.0
    elif iv_rank > 80:          iv_score = 5.0
    else:                       iv_score = 7.0

    # Market cap proxy: large cap = liquid options
    if snap.market_cap and snap.market_cap > 500e9: cap = 1.0
    elif snap.market_cap and snap.market_cap > 100e9: cap = 3.0
    elif snap.market_cap and snap.market_cap > 10e9: cap = 5.0
    else: cap = 8.0

    # Beta: too high beta = risky premium selling
    if snap.beta_vs_spy and snap.beta_vs_spy < 1.0: beta = 1.0
    elif snap.beta_vs_spy and snap.beta_vs_spy < 1.5: beta = 3.0
    elif snap.beta_vs_spy and snap.beta_vs_spy < 2.0: beta = 6.0
    else: beta = 9.0

    return spread * 0.25 + iv_score * 0.25 + cap * 0.25 + beta * 0.25


def _score_fundamental(snap: StockSnapshot) -> float:
    """Score fundamental health. 1 = great, 10 = poor."""
    # P/E: reasonable P/E = better
    pe = snap.pe_ttm or snap.pe_ratio or 25
    if pe and 10 <= pe <= 25:  pe_score = 1.0
    elif pe and 25 < pe <= 40: pe_score = 3.0
    elif pe and 40 < pe <= 60: pe_score = 5.0
    elif pe and pe > 60:       pe_score = 8.0
    else:                       pe_score = 5.0

    # Dividend: dividend stocks work well for wheel
    div = snap.dividend_yield_ttm or 0
    if div and div > 2.0:     div_score = 1.0
    elif div and div > 1.0:   div_score = 3.0
    elif div and div > 0:     div_score = 5.0
    else:                      div_score = 6.0

    # Earnings: consistent EPS
    if snap.eps_ttm and snap.eps_ttm > 0: eps_score = 1.0
    else:                                   eps_score = 7.0

    return pe_score * 0.40 + div_score * 0.30 + eps_score * 0.30


def _score_external(consensus: str, blackout: bool, insider: str,
                    upside: Optional[float], news_score: float = 50.0) -> float:
    """Score external sentiment. 1 = bullish, 10 = bearish. Includes news sentiment."""
    base = 4.0

    if consensus == 'STRONG_BUY':  base -= 1.5
    elif consensus == 'BUY':       base -= 0.8
    elif consensus == 'HOLD':      base += 0.5
    elif consensus == 'SELL':      base += 3.0
    elif consensus == 'STRONG_SELL': base += 5.0

    if upside and upside > 15:      base -= 1.0
    elif upside and upside > 5:     base -= 0.5
    elif upside and upside < -10:   base += 2.0

    if blackout:                    base += 2.0

    if insider == 'BUYING':         base -= 1.0
    elif insider == 'SELLING':      base += 1.5

    # News sentiment score (1-100 → penalty/bonus to 1-10 scale)
    if news_score >= 70:            base -= 1.0   # bullish news
    elif news_score <= 30:          base += 2.0   # bearish news
    elif news_score <= 40:          base += 1.0   # cautious news

    return max(1.0, min(10.0, base))


def _score_macro(regime: str, regime_mult: float, blackout: bool) -> float:
    """Score macro/risk context. 1 = favorable, 10 = unfavorable."""
    if regime == 'BULLISH':    base = 2.0
    elif regime == 'NEUTRAL':  base = 3.0
    elif regime == 'VOLATILE': base = 6.0
    elif regime == 'BEARISH':  base = 8.0
    else:                      base = 5.0

    if blackout:
        base += 2.0

    return max(1.0, min(10.0, base))


def _trend_composite(snap: StockSnapshot) -> float:
    """0-100 trend composite (simplified from existing scoring)."""
    trend = 50.0
    if snap.sma_50 and snap.sma_200:
        if snap.last_price > snap.sma_50 > snap.sma_200:
            trend = 75.0
        elif snap.last_price > snap.sma_200:
            trend = 60.0
        elif snap.last_price > snap.sma_50:
            trend = 40.0
        else:
            trend = 25.0
    rsi = snap.rsi_14 or 50
    rsi_factor = 0.5 if 40 <= rsi <= 60 else 0.3
    return trend * (0.7 + rsi_factor)


def _contract_penalty(c: OptionSnapshot, delta: float, roc: float) -> float:
    """Per-contract score adjustment (added to ticker score). Lower = better."""
    penalty = 0.0

    # ═══ DTE WINDOW (most important factor after fundamentals) ═══
    if c.dte < 7:                   penalty += 99.0   # HARD BLOCK — gamma explosion
    elif c.dte < 14:                penalty += 3.0    # extreme gamma, need active mgmt
    elif c.dte < 21:                penalty += 1.5    # weekly — higher gamma
    elif c.dte < 30:                penalty += 0.5    # acceptable but not ideal
    elif 30 <= c.dte <= 45:         penalty -= 0.5    # SWEET SPOT — best theta/gamma
    elif c.dte <= 60:               penalty += 0.0    # neutral — slower theta
    else:                           penalty += 1.0    # too slow, capital tied up

    # Low OI → risky
    if c.open_interest < 100:       penalty += 1.5
    elif c.open_interest < 500:     penalty += 0.5

    # Wide spread → costly
    if c.bid_ask_spread_pct > 5:    penalty += 2.0
    elif c.bid_ask_spread_pct > 2:  penalty += 1.0

    # Delta extreme
    if delta < 0.15:                penalty += 0.5

    # Reward high RoC
    if roc > 24:                    penalty -= 1.5
    elif roc > 18:                  penalty -= 0.8
    elif roc > 15:                  penalty -= 0.3

    # Reward high IV
    if c.implied_vol > 35:          penalty -= 0.5

    # Low volume → illiquid
    if c.volume < 50:               penalty += 1.0
    elif c.volume < 10:             penalty += 2.0

    return penalty


def _compute_chain_gex(contracts: list, underlying_price: float) -> float:
    """Approximate Gamma Exposure from option contracts. Negative = dealer short gamma."""
    total = 0.0
    for c in contracts:
        if c.gamma and c.open_interest and underlying_price > 0:
            total += abs(c.gamma) * c.open_interest * underlying_price * 100
    return total


def _csp_roc(bid: float, strike: float, dte: int) -> float:
    if strike <= 0 or dte <= 0:
        return 0.0
    return (bid / strike) * (365.0 / dte) * 100


def _regime_multiplier(regime: str) -> float:
    return {'BULLISH': 0.85, 'NEUTRAL': 1.0, 'VOLATILE': 1.2, 'BEARISH': 1.5}.get(regime, 1.0)


def _reason(ticker_score: float, contract_score: float, strat: str) -> str:
    if contract_score <= 2.0:
        return f"{'🔥' if strat=='CSP' else '💎'} Excellent setup"
    elif contract_score <= 3.5:
        return f"Strong {strat} candidate"
    elif contract_score <= 5.0:
        return "Good, moderate risk"
    elif contract_score <= 7.0:
        return "Decent, higher risk"
    else:
        return "Marginal, caution"


def _score_stars(score: float) -> str:
    if score <= 2.0: return '⭐1'
    elif score <= 3.0: return '⭐2'
    elif score <= 4.0: return '⭐3'
    elif score <= 5.0: return ' 4 '
    elif score <= 6.0: return ' 5 '
    elif score <= 7.0: return ' 6 '
    elif score <= 8.0: return ' 7 '
    return ' 8+'


def _best_of(candidates: list[TradeCandidate], strategy: str) -> str:
    filtered = [c for c in candidates if c.strategy == strategy]
    if not filtered:
        return f"No {strategy} candidates"
    best = filtered[0]
    return f"{best.ticker} ${best.strike:,.0f} {best.expiry} Δ{best.delta:.2f} RoC {best.annualized_roc_pct:.1f}% Score {best.score}"


if __name__ == '__main__':
    main()
