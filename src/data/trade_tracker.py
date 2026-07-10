"""
Local Trade Tracker — record our buy/sell DECISIONS without submitting to moomoo.

All execution is MANUAL by the user. Library class used by scripts/track_trades.py
"""

from datetime import datetime
from src.data.portfolio_db import PortfolioDB


class TradeTracker:
    """Local trade journal — decisions tracked, execution manual."""

    def __init__(self):
        self._db = PortfolioDB()

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def open_trade(self, ticker: str, action: str, strike: float = None,
                   expiry: str = None, entry_price: float = 0,
                   contracts: int = 1, dte: int = None,
                   strategy: str = None, notes: str = '') -> int:
        """Record a trade decision. Returns trade ID."""
        # Auto-detect strategy
        if strategy is None:
            if action == 'SELL_CSP':
                strategy = 'CSP'
            elif action == 'SELL_CC':
                strategy = 'CC'
            elif action == 'BUY_STOCK':
                strategy = 'STOCK'

        # Auto-compute DTE from expiry
        if dte is None and expiry:
            try:
                from datetime import date
                exp_date = date.fromisoformat(expiry)
                dte = (exp_date - date.today()).days
            except Exception:
                pass

        tid = self._db.log_trade(
            ticker=ticker, action=action, strategy=strategy,
            strike=strike, expiry=expiry, dte=dte,
            contracts=contracts, entry_price=entry_price,
            notes=notes,
        )
        print(f"✅ Trade #{tid} opened: {action} {ticker} "
              + (f"${strike:,.2f} {expiry}" if strike else "")
              + (f" @ ${entry_price:,.2f}" if entry_price else "")
              + (f" × {contracts} contracts" if contracts > 1 else ""))
        return tid

    def close_trade(self, trade_id: int, exit_price: float = 0):
        """Close a trade at given exit price."""
        self._db.close_trade(trade_id, exit_price=exit_price)
        print(f"✅ Trade #{trade_id} closed @ ${exit_price:,.2f}")

    def expire_trade(self, trade_id: int):
        """Mark a trade as expired (full premium kept)."""
        self._db.expire_trade(trade_id)
        print(f"✅ Trade #{trade_id} expired — full premium kept")

    def list_trades(self, status: str = 'OPEN'):
        """Show trades."""
        if status == 'OPEN':
            trades = self._db.get_open_trades()
        else:
            trades = []  # could add get_all_trades()

        if not trades:
            print(f"No {status.lower()} trades.")
            return

        print(f"\n📋 {status} TRADES ({len(trades)})")
        print(f"  {'ID':<5s} {'Ticker':<8s} {'Action':<12s} {'Strategy':<8s} "
              f"{'Strike':>8s} {'Expiry':>12s} {'DTE':>4s} "
              f"{'Entry$':>8s} {'Exit$':>8s} {'P&L':>10s}")
        print(f"  {'-'*5} {'-'*8} {'-'*12} {'-'*8} {'-'*8} {'-'*12} {'-'*4} "
              f"{'-'*8} {'-'*8} {'-'*10}")
        for t in trades:
            pnl_str = f"${t['pnl']:>9,.2f}" if t['pnl'] else '—'
            print(f"  {t['id']:<5} {t['ticker']:<8s} {t['action']:<12s} "
                  f"{t['strategy'] or '':<8s} "
                  f"${t['strike'] or 0:>7,.2f} {t['expiry'] or '':>12s} "
                  f"{t['dte'] or '':>4} "
                  f"${t['entry_price'] or 0:>7,.2f} ${t['exit_price'] or 0:>7,.2f} "
                  f"{pnl_str:>10s}")

    def show_summary(self):
        """P&L summary."""
        s = self._db.get_trade_summary()
        pos = self._db.get_positions()
        funds = self._db.get_latest_funds()

        print("\n" + "=" * 50)
        print("  PORTFOLIO + TRADE SUMMARY")
        print("=" * 50)

        if funds:
            print(f"\n💰 FUNDS (from moomoo)")
            print(f"  Cash:        ${funds.get('cash', 0):>12,.2f}")
            print(f"  Stock Value: ${funds.get('stock_value', 0):>12,.2f}")
            print(f"  Total:       ${funds.get('total_assets', 0):>12,.2f}")

        if pos:
            print(f"\n📈 POSITIONS ({len(pos)})")
            for p in pos:
                print(f"  {p['code']:<24s} {p['pos_type']:>6s} {p['qty']:>8,.0f} "
                      f"P&L=${p['pl_val'] or 0:>10,.2f}")

        print(f"\n📊 TRADE P&L")
        print(f"  Total Closed:  {s['total_trades']}")
        print(f"  Win Rate:      {s['win_rate']:.1f}%")
        print(f"  Total P&L:     ${s['total_pnl']:>10,.2f}")
        print(f"  Avg P&L/Trade: ${s['avg_pnl']:>10,.2f}")
        print(f"  Open Trades:   {s['open_trades']}")


