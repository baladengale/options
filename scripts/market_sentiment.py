#!/usr/bin/env python3
"""Fetch market sentiment & external data for a ticker or watchlist.

Covers:
  - Macro: VIX, Treasury yields, yield curve, market regime
  - Analyst: ratings, consensus, price targets, upside %
  - Earnings: next date, estimates, surprise history, blackout warning
  - Institutional: top holders, insider trades, net insider sentiment
  - News: recent headlines with keyword-based sentiment/type classification

Usage:
    python3 scripts/market_sentiment.py                    # macro only
    python3 scripts/market_sentiment.py TICKER             # macro + single ticker
    python3 scripts/market_sentiment.py TICKER --news      # + recent news
    python3 scripts/market_sentiment.py --watchlist         # all watchlist tickers

Data sources:
    - Yahoo Finance (yfinance) — analyst ratings, earnings, institutions, news
    - FRED API — interest rates (future: add FRED key)
    - Moomoo — VIX snapshot (fallback if yfinance VIX fails)
"""

import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")
from datetime import date, datetime

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.yfinance_client import (
    YFinanceClient, AnalystRatings, EarningsData, InstitutionData, NewsItem, MacroData,
)
from src.data.moomoo_client import MoomooClient
from src.analysis.sentiment import get_macro_context, get_ticker_sentiment

WATCHLIST = ['US.V', 'US.MSFT', 'US.GOOGL', 'US.AAPL', 'US.AMZN',
             'US.NVDA', 'US.META', 'US.AVGO', 'US.ADBE', 'US.CRM', 'US.AMD']


def main():
    parser = argparse.ArgumentParser(description='Market sentiment & external data')
    parser.add_argument('ticker', nargs='?', help='Ticker (e.g. V, US.AAPL)')
    parser.add_argument('--news', '-n', action='store_true', help='Include recent news')
    parser.add_argument('--watchlist', '-w', action='store_true', help='Fetch for entire watchlist')
    args = parser.parse_args()

    yf_client = YFinanceClient()

    # ── MACRO ──
    _print_macro(yf_client)

    # Determine tickers to fetch
    tickers: list[str] = []
    if args.watchlist:
        tickers = WATCHLIST
    elif args.ticker:
        ticker = args.ticker if '.' in args.ticker else f'US.{args.ticker}'
        tickers = [ticker]
    else:
        return  # macro only

    # ── PER-TICKER ──
    for ticker in tickers:
        _print_ticker_header(ticker)
        yf_ticker = ticker.replace('US.', '')  # yfinance expects plain ticker
        _print_analyst(yf_client, yf_ticker)
        _print_earnings(yf_client, yf_ticker)
        _print_institutional(yf_client, yf_ticker)
        if args.news:
            _print_news(yf_client, yf_ticker)


def _print_macro(client: YFinanceClient):
    m = get_macro_context(client)
    print(f"\n{'='*70}")
    print(f"  🌍 MACRO — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*70}")
    print(f"  VIX:        {m.vix:>8,.2f}" if m.vix else "  VIX:           N/A")
    print(f"  Regime:     {m.market_regime:>12}  "
          f"(score: {m.regime_score:+d}, size: {m.position_mult:.0%})")
    print(f"  10Y Yield:  {m.treasury_10y:>8,.2f}%" if m.treasury_10y else "  10Y Yield:     N/A")
    print(f"  2Y Yield:   {m.treasury_2y:>8,.2f}%" if m.treasury_2y else "")
    print(f"  30Y Yield:  {m.treasury_30y:>8,.2f}%" if m.treasury_30y else "")
    if m.yield_spread_10y2y is not None:
        inv_flag = " ⚠️ INVERTED" if m.yield_spread_10y2y < 0 else ""
        print(f"  10Y-2Y:     {m.yield_spread_10y2y:>+8,.2f}%{inv_flag}")
    if m.dxy:
        print(f"  DXY:        {m.dxy:>8,.2f}" + (" ⚠️ strong" if m.dxy > 106 else ""))
    if m.vvix:
        print(f"  VVIX:      {m.vvix:>9,.2f}" + (" ⚠️ vol of vol elevated" if m.vvix > 120 else ""))
    if m.hyg_ief_spread is not None:
        credit_emoji = {'HEALTHY': '✅', 'CONCERNING': '⚠️', 'STRESSED': '🔴'}.get(m.credit_regime, '')
        print(f"  Credit:     {m.hyg_ief_spread:>+8,.2f}%  ({m.credit_regime}) {credit_emoji}")

    # Fear & Greed
    fg = client.get_fear_greed()
    if fg:
        emoji = {'Extreme Fear': '🔴🔴', 'Fear': '🔴', 'Neutral': '⚪',
                 'Greed': '🟢', 'Extreme Greed': '🟢🟢'}.get(fg['classification'], '')
        print(f"  Fear&Greed: {fg['value']:>4d}  ({fg['classification']}) {emoji}")

    # Position sizing guidance
    print(f"\n  💡 POSITION SIZING: {m.position_mult:.0%} of normal size")
    if m.regime_score <= -3:
        print(f"     ⛔ DEFENSIVE — no new positions recommended")
    elif m.regime_score <= -1:
        print(f"     ⚠️  REDUCED — smaller size, tighter stops")


def _print_ticker_header(ticker: str):
    short = ticker.replace('US.', '')
    print(f"\n{'─'*70}")
    print(f"  📋 {ticker} ({short})")
    print(f"{'─'*70}")


def _print_analyst(client: YFinanceClient, ticker: str):
    a = client.get_analyst_ratings(ticker)
    if a is None:
        return
    print(f"\n  🎯 ANALYST RATINGS  ({a.num_analysts} analysts)")
    if a.consensus != 'N/A':
        print(f"    Consensus:     {a.consensus}")
    total = a.strong_buy + a.buy + a.hold + a.sell + a.strong_sell
    if total > 0:
        sb_pct = a.strong_buy / total * 100
        b_pct = a.buy / total * 100
        print(f"    Breakdown:     Strong Buy {sb_pct:.0f}% | Buy {b_pct:.0f}% | Hold/Other {100-sb_pct-b_pct:.0f}%")
    if a.mean_target and a.current_price:
        upside = a.target_upside_pct
        arrow = "📈" if (upside or 0) > 0 else "📉"
        print(f"    Price Target:  Mean ${a.mean_target:,.2f}  (${a.low_target:,.2f} - ${a.high_target:,.2f})")
        print(f"    Current:       ${a.current_price:,.2f}  →  Upside: {upside:+.1f}% {arrow}" if upside else "")
    if a.last_rating_change:
        print(f"    Last Change:   {a.last_rating_change}")


def _print_earnings(client: YFinanceClient, ticker: str):
    e = client.get_earnings(ticker)
    if e is None:
        return
    print(f"\n  📅 EARNINGS")
    if e.next_earnings_date:
        days = e.days_to_earnings
        flag = " ⚠️ BLACKOUT" if e.in_blackout else ""
        print(f"    Next:         {e.next_earnings_date}  ({days}d away){flag}")
    if e.earnings_avg_estimate:
        print(f"    EPS Estimate:  ${e.earnings_avg_estimate:,.2f}")
        if e.year_ago_eps:
            print(f"    Year Ago EPS:  ${e.year_ago_eps:,.2f}")
        if e.earnings_growth_pct:
            print(f"    Growth Est:    {e.earnings_growth_pct:+.1f}%")
    if e.revenue_avg_estimate:
        print(f"    Revenue Est:   ${e.revenue_avg_estimate:,.0f}")
    if e.last_eps_surprise is not None:
        print(f"    Last Surprise: {e.last_eps_surprise:+.2f} ({e.last_eps_surprise_pct:+.1f}%)")
    if e.last_reported_date:
        print(f"    Last Reported: {e.last_reported_date}  EPS: ${e.last_reported_eps:,.2f}" if e.last_reported_eps else f"    Last Reported: {e.last_reported_date}")


def _print_institutional(client: YFinanceClient, ticker: str):
    inst = client.get_institution_data(ticker)
    if inst is None:
        return
    print(f"\n  🏦 INSTITUTIONAL")
    if inst.institutional_ownership_pct:
        print(f"    Inst Ownership:  {inst.institutional_ownership_pct:.1f}%")
    if inst.insider_ownership_pct:
        print(f"    Insider Own:     {inst.insider_ownership_pct:.1f}%")
    if inst.net_insider_sentiment != 'NEUTRAL':
        flag = "🟢" if inst.net_insider_sentiment == 'BUYING' else "🔴"
        print(f"    Insider Sentiment: {inst.net_insider_sentiment} {flag}")
    if inst.top_institutions:
        print(f"    Top Holders:")
        for h in inst.top_institutions[:3]:
            print(f"      {h['name'][:30]:30s} {h['shares']:>12,} shares  ({h['pct']:.2f}%)" if h['pct'] else f"      {h['name'][:30]:30s} {h['shares']:>12,} shares")


def _print_news(client: YFinanceClient, ticker: str):
    news = client.get_news(ticker, max_items=5)
    if not news:
        return
    print(f"\n  📰 RECENT NEWS ({len(news)} items)")
    for n in news:
        sent_flag = {'POSITIVE': '🟢', 'NEUTRAL': '⚪', 'NEGATIVE': '🔴'}.get(n.sentiment_hint, '⚪')
        type_tag = f"[{n.news_type}]" if n.news_type != 'GENERAL' else ""
        print(f"    {sent_flag} {type_tag} {n.title[:80]}  ({n.publisher})")


if __name__ == '__main__':
    main()
