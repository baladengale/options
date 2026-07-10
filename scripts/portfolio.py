#!/usr/bin/env python3
"""
Portfolio Manager — sync, view, track.

Pulls REAL account data (funds, positions, executed orders) into local DB.
NEVER submits orders. All read-only from moomoo.

Usage:
    python3 scripts/portfolio.py sync          # Full sync: funds + positions + orders
    python3 scripts/portfolio.py status        # Full portfolio view from DB
    python3 scripts/portfolio.py summary       # Quick numbers
    python3 scripts/portfolio.py history 30    # Fund history over N days
"""

import sys
import os
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.portfolio_sync import PortfolioSync
from src.data.portfolio_db import PortfolioDB


def cmd_sync():
    with PortfolioSync() as ps:
        ps.sync_all()


def cmd_status():
    ps = PortfolioSync()
    ps._ctx = None  # no OpenD needed
    try:
        ps.show_status()
    finally:
        ps._db.close()


def cmd_summary():
    db = PortfolioDB()
    try:
        s = db.get_portfolio_summary()
        f = s['funds'] or {}
        print(f"Cash=${f.get('cash', 0):,.0f}  "
              f"Stocks={s['positions']['stocks']}(${s['positions']['stock_value']:,.0f})  "
              f"Options={s['positions']['options']}  "
              f"Unrealized=${s['positions']['unrealized_pl']:,.0f}  "
              f"OpenTrades={s['trades']['open']}  "
              f"ClosedP&L=${s['trades']['total_pnl']:,.0f}")
    finally:
        db.close()


def cmd_history(days: int = 30):
    db = PortfolioDB()
    try:
        rows = db.get_funds_history(days)
        if not rows:
            print("No history yet. Run a sync first.")
            return
        print(f"📈 FUND HISTORY ({len(rows)} days)")
        print(f"  {'Date':<12s} {'Total':>14s} {'Cash':>14s} {'Stocks':>14s}")
        print(f"  {'-'*12} {'-'*14} {'-'*14} {'-'*14}")
        for r in rows:
            print(f"  {r['synced_at'][:10]:<12s} "
                  f"${r['total_assets'] or 0:>13,.2f} "
                  f"${r['cash'] or 0:>13,.2f} "
                  f"${r['stock_value'] or 0:>13,.2f}")
    finally:
        db.close()


def main():
    import argparse
    p = argparse.ArgumentParser(description='Portfolio Manager')
    sub = p.add_subparsers(dest='cmd', help='Action')

    sub.add_parser('sync', help='Sync funds + positions + orders from REAL account')
    sub.add_parser('status', help='Full portfolio view from local DB')
    sub.add_parser('summary', help='Quick numbers')
    hist_p = sub.add_parser('history', help='Fund history')
    hist_p.add_argument('days', type=int, nargs='?', default=30, help='Days of history')

    args = p.parse_args()

    if args.cmd == 'sync':
        cmd_sync()
    elif args.cmd == 'status':
        cmd_status()
    elif args.cmd == 'summary':
        cmd_summary()
    elif args.cmd == 'history':
        cmd_history(args.days)
    else:
        # Default: sync
        cmd_sync()


if __name__ == '__main__':
    main()
