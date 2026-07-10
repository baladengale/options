#!/usr/bin/env python3
"""Fetch comprehensive market data for a ticker — stocks snapshot + option chain.

Usage:
    python3 scripts/market_data.py TICKER                 # stock only
    python3 scripts/market_data.py TICKER --options       # stock + options chain
    python3 scripts/market_data.py TICKER --options --all # full chain + computed indicators
    python3 scripts/market_data.py TICKER --chain 30 45   # custom DTE range

Examples:
    python3 scripts/market_data.py US.V
    python3 scripts/market_data.py US.AAPL --options
    python3 scripts/market_data.py US.NVDA --options --all
"""
import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.moomoo_client import MoomooClient
from src.data.compute import (
    enrich_stock_snapshot, compute_option_chain_bundle,
)

SPY_HISTORY_CACHE: list[dict] = []


def fmt_f(val, decimals=2) -> str:
    if val is None:
        return 'N/A'.rjust(8)
    return f'{val:>{decimals+5}.{decimals}f}'


def fmt_pct(val, decimals=1) -> str:
    if val is None:
        return 'N/A'.rjust(7)
    sign = '+' if val > 0 else ''
    return f'{sign}{val:.{decimals}f}%'


def main():
    parser = argparse.ArgumentParser(description='Fetch market data for a ticker')
    parser.add_argument('ticker', help='Ticker, e.g. US.V or V')
    parser.add_argument('--options', '-o', action='store_true', help='Include option chain')
    parser.add_argument('--all', '-a', action='store_true', help='Full chain + computed indicators')
    parser.add_argument('--chain', '-c', nargs=2, type=int, metavar=('DTE_MIN', 'DTE_MAX'),
                        default=[0, 90], help='DTE range for option chain (default: 0 90 = all)')
    args = parser.parse_args()

    ticker = args.ticker if '.' in args.ticker else f'US.{args.ticker}'

    with MoomooClient() as client:
        # ── STOCK SNAPSHOT ──
        print(f"\n{'='*72}")
        print(f"  MARKET DATA — {ticker}")
        print(f"{'='*72}")

        snap = client.get_stock_snapshot(ticker)
        if snap is None:
            print(f"  ❌ No data for {ticker}")
            return

        # Compute technical indicators from price history
        history = client.get_price_history(ticker, 252)
        if history:
            spy_history = client.get_price_history('US.SPY', 252)
            enrich_stock_snapshot(snap, history, spy_history)

        _print_stock(snap)

        # ── OPTIONS ──
        if args.options or args.all:
            dte_min, dte_max = args.chain
            print(f"\n  📊 OPTIONS CHAIN ({dte_min}-{dte_max} DTE)")

            if args.all:
                contracts = client.get_all_option_snapshots(ticker, dte_min, dte_max)
            else:
                contracts = client.get_option_snapshots(ticker, dte_min, dte_max)

            if not contracts:
                print("    (no contracts found in DTE range)")
            else:
                bundle = compute_option_chain_bundle(
                    ticker, snap.last_price, contracts
                )
                _print_options(contracts, bundle, args.all)

        print()


def _print_stock(s: 'StockSnapshot'):
    print(f"\n  💰 PRICE")
    print(f"    Last: ${s.last_price:,.2f}  ({fmt_pct(s.change_pct)})  "
          f"Bid: ${s.bid:,.2f}  Ask: ${s.ask:,.2f}  Spread: {s.bid_ask_spread_pct:.2f}%")
    print(f"    Open: ${s.open_price:,.2f}  "
          f"High: ${s.high_price:,.2f}  Low: ${s.low_price:,.2f}  "
          f"Prev: ${s.prev_close:,.2f}")
    print(f"    Range: {fmt_pct(s.amplitude)}  "
          f"52W: ${s.lowest_52w:,.2f} - ${s.highest_52w:,.2f}")

    print(f"\n  📈 TECHNICALS")
    print(f"    RSI(14): {fmt_f(s.rsi_14, 0):>6s}  "
          f"ADX(14): {fmt_f(s.adx_14, 0):>6s}  "
          f"ATR(14): ${fmt_f(s.atr_14, 2):>8s}  "
          f"HV(30d): {fmt_pct(s.hv_30d * 100 if s.hv_30d else None, 1):>7s}")
    print(f"    SMA-20:  ${fmt_f(s.sma_20):>8s}  "
          f"SMA-50:  ${fmt_f(s.sma_50):>8s}  "
          f"SMA-200: ${fmt_f(s.sma_200):>8s}")
    print(f"    MACD: {fmt_f(s.macd, 3):>8s}  "
          f"Signal: {fmt_f(s.macd_signal, 3):>8s}  "
          f"Hist: {fmt_f(s.macd_histogram, 3):>8s}")
    if s.bollinger_mid:
        print(f"    Bollinger: Upper ${s.bollinger_upper:,.2f}  "
              f"Mid ${s.bollinger_mid:,.2f}  Lower ${s.bollinger_lower:,.2f}")
    if s.beta_vs_spy:
        print(f"    Beta vs SPY: {s.beta_vs_spy:.2f}")

    print(f"\n  📊 VOLUME & LIQUIDITY")
    print(f"    Volume: {s.volume:>12,}  "
          f"Turnover: ${s.turnover:>12,.0f}  "
          f"Turnover Rate: {fmt_pct(s.turnover_rate):>7s}")
    print(f"    Volume Ratio: {fmt_f(s.volume_ratio, 1):>8s}x  "
          f"Short Rate: {fmt_pct(s.short_sell_rate):>7s}  "
          f"Short Avail: {s.short_available:>12,.0f}" if s.short_available else "")

    print(f"\n  🏛️ FUNDAMENTALS")
    print(f"    P/E:     {fmt_f(s.pe_ratio, 1):>8s}  "
          f"P/E TTM: {fmt_f(s.pe_ttm, 1):>8s}  "
          f"P/B:     {fmt_f(s.pb_ratio, 2):>8s}")
    print(f"    EPS TTM: ${fmt_f(s.eps_ttm, 2):>8s}  "
          f"Div TTM: ${fmt_f(s.dividend_ttm, 2):>8s}  "
          f"Div Yld: {fmt_pct(s.dividend_yield_ttm):>7s}")
    print(f"    Mkt Cap: ${s.market_cap:>12,.0f}" if s.market_cap else "    Mkt Cap: N/A")
    print(f"    Shares Out: {s.issued_shares:>12,.0f}" if s.issued_shares else "")


def _print_options(contracts, bundle, show_all: bool):
    calls = [c for c in contracts if c.option_type == 'CALL']
    puts = [c for c in contracts if c.option_type == 'PUT']

    # ── Chain Summary ──
    print(f"\n  CHAIN SUMMARY  ({len(calls)} calls, {len(puts)} puts)")
    if bundle.atm_iv:
        print(f"    ATM IV:       {bundle.atm_iv:>7.1f}%")
    if bundle.put_call_oi_ratio:
        print(f"    Put/Call OI:  {bundle.put_call_oi_ratio:.2f}")
    if bundle.put_call_vol_ratio:
        print(f"    Put/Call Vol: {bundle.put_call_vol_ratio:.2f}")
    if bundle.max_pain:
        print(f"    Max Pain:     ${bundle.max_pain:,.2f}")
    if bundle.skew_25d:
        skew_str = f"{bundle.skew_25d:+.1f}%"
        print(f"    25Δ Skew:     {skew_str:>7s}  ({'Puts bid ↑ fear' if bundle.skew_25d > 0 else 'Calls bid ↑ greed'})")
    if bundle.term_structure:
        print(f"    Term Struct:  {bundle.term_structure}")
    if bundle.call_oi_wall:
        print(f"    Call Wall:    ${bundle.call_oi_wall:,.2f}")
    if bundle.put_oi_wall:
        print(f"    Put Wall:     ${bundle.put_oi_wall:,.2f}")
    if bundle.gamma_exposure:
        direction = "Long γ (dampening)" if bundle.gamma_exposure > 0 else "Short γ (amplifying)"
        print(f"    GEX:          {bundle.gamma_exposure:,.0f}  ({direction})")

    # Filter dead contracts: both OI and volume must be >= 10
    calls = [c for c in calls if (c.open_interest or 0) >= 10 and (c.volume or 0) >= 10]
    puts = [p for p in puts if (p.open_interest or 0) >= 10 and (p.volume or 0) >= 10]

    # ── Top Calls ──
    print(f"\n  TOP CALLS (by OI)")
    top_calls = sorted(calls, key=lambda c: c.open_interest or 0, reverse=True)[:10]
    _print_contract_table(top_calls)

    # ── Top Puts ──
    print(f"\n  TOP PUTS (by OI)")
    top_puts = sorted(puts, key=lambda p: p.open_interest or 0, reverse=True)[:10]
    _print_contract_table(top_puts)

    # ── Full chain if -a ──
    if show_all:
        print(f"\n  ALL CALLS ({len(calls)})")
        _print_contract_table(sorted(calls, key=lambda c: c.strike))
        print(f"\n  ALL PUTS ({len(puts)})")
        _print_contract_table(sorted(puts, key=lambda p: p.strike))


def _print_contract_table(contracts):
    if not contracts:
        print("    (none)")
        return
    hdr = (f"  {'Strike':>8s} {'Expiry':>12s} {'DTE':>4s} "
           f"{'Bid':>8s} {'Ask':>8s} {'Last':>8s} {'IV':>8s} "
           f"{'Δ':>7s} {'Γ':>7s} {'Θ':>8s} {'V':>7s} "
           f"{'OI':>8s} {'Vol':>8s}")
    print(hdr)
    sep = f"  {'-'*8} {'-'*12} {'-'*4} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*7} {'-'*7} {'-'*8} {'-'*7} {'-'*8} {'-'*8}"
    print(sep)
    for c in contracts:
        print(f"  ${c.strike:>7,.2f} {c.expiry:>12s} {c.dte:>4d} "
              f"${c.bid:>7,.2f} ${c.ask:>7,.2f} ${c.last_price:>7,.2f} "
              f"{c.implied_vol:>7.1f}% "
              f"{c.delta:>+6.3f} {c.gamma:>+6.4f} {c.theta:>+7.4f} {c.vega:>+6.4f} "
              f"{c.open_interest:>8,d} {c.volume:>8,d}")


if __name__ == '__main__':
    main()
