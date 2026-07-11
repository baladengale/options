"""
Portfolio Sync — poll REAL moomoo account → local SQLite DB.

Read-only. NEVER submits orders. Library class used by scripts/sync_portfolio.py
"""

import warnings
import logging
from datetime import datetime

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")
logging.getLogger('FTConsoleLog').setLevel(logging.WARNING)

from moomoo import OpenSecTradeContext, RET_OK, TrdEnv

from src.data.portfolio_db import PortfolioDB


class PortfolioSync:
    """Poll REAL moomoo account → local PortfolioDB."""

    def __init__(self, host: str = '127.0.0.1', port: int = 11111):
        self._ctx = None  # lazy — only connect when syncing
        self._host = host
        self._port = port
        self._db = PortfolioDB()

    @property
    def ctx(self):
        if self._ctx is None:
            self._ctx = OpenSecTradeContext(
                host=self._host, port=self._port, ai_type=1)
        return self._ctx

    def close(self):
        if self._ctx is not None:
            self._ctx.close()
            self._ctx = None
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ═══════════════════════════════════════════════════════════
    # SYNC
    # ═══════════════════════════════════════════════════════════

    def sync_all(self) -> bool:
        """Sync funds + positions + orders from REAL account to local DB."""
        acc_id, trd_env = self._find_real_account()
        if acc_id is None:
            print("❌ No REAL account found.")
            return False

        print(f"📋 REAL Account: {acc_id}")
        print(f"🔄 Syncing funds...")
        funds_ok = self._sync_funds(acc_id, trd_env)

        print(f"🔄 Syncing positions...")
        positions_ok = self._sync_positions(acc_id, trd_env)

        print(f"🔄 Syncing orders...")
        orders_ok = self._sync_orders(acc_id, trd_env)

        if funds_ok and positions_ok:
            summary = self._db.get_portfolio_summary()
            print(f"\n✅ Sync complete")
            print(f"   Cash:       ${summary['funds']['cash']:>12,.2f}" if summary['funds'] else "   No fund data")
            print(f"   Stocks:      {summary['positions']['stocks']:>4} (${summary['positions']['stock_value']:>12,.2f})" if summary['funds'] else "")
            print(f"   Options:     {summary['positions']['options']:>4}")
            print(f"   Orders:      {orders_ok} synced")
            print(f"   Open Trades: {summary['trades']['open']:>4}")
            return True
        return False

    def _find_real_account(self) -> tuple:
        """Find REAL account ID (string-safe comparison)."""
        ret, acc_list = self.ctx.get_acc_list()
        if ret != RET_OK:
            return None, None
        for _, acc in acc_list.iterrows():
            trd_env_raw = str(acc.get('trd_env', ''))
            if trd_env_raw == 'REAL':
                return acc['acc_id'], TrdEnv.REAL
        return None, None

    def _sync_funds(self, acc_id: int, trd_env) -> bool:
        """Poll accinfo_query → portfolio_snapshots. All values stored in USD."""
        try:
            ret, funds = self.ctx.accinfo_query(
                trd_env=trd_env, acc_id=acc_id, refresh_cache=True
            )
            if ret != RET_OK or funds is None or len(funds) == 0:
                print(f"  ❌ Fund query failed: {funds}")
                return False

            f = funds.iloc[0]

            def nf(key, default=0.0):
                val = f.get(key)
                try:
                    return float(val) if val is not None else default
                except (ValueError, TypeError):
                    return default

            currency = str(f.get('currency', 'USD'))
            is_hkd = (currency == 'HKD')
            HKD_TO_USD = 7.8  # approximate peg rate

            # USD-specific fields (already in USD, correct for US stocks)
            usd_assets = nf('usd_assets')        # US stock + option market value
            us_cash = nf('us_cash')               # USD cash (may be negative for margin)
            us_bp = nf('usd_net_cash_power')      # USD buying power

            # Fund assets — moomoo reports in account currency, convert if HKD
            fund_raw = nf('fund_assets')
            fund_usd = (fund_raw / HKD_TO_USD) if (is_hkd and fund_raw) else fund_raw

            # Total net liquidation in USD = stocks/options + cash + fund
            total_usd = usd_assets + us_cash + fund_usd

            fund_data = {
                'total_assets': round(total_usd, 2),
                'cash': round(us_cash, 2),
                'buying_power': round(us_bp, 2),
                'stock_value': round(usd_assets, 2),
                'currency': 'USD',  # always stored in USD
            }
            self._db.save_funds(acc_id, fund_data)
            return True
        except Exception as e:
            print(f"  ❌ Fund sync error: {e}")
            return False

    def _sync_positions(self, acc_id: int, trd_env) -> bool:
        """Poll position_list_query → positions table."""
        try:
            ret, pos = self.ctx.position_list_query(
                trd_env=trd_env, acc_id=acc_id, refresh_cache=True
            )
            if ret != RET_OK:
                print(f"  ❌ Position query failed: {pos}")
                return False

            if pos is None or len(pos) == 0:
                print("  ℹ️  No positions in REAL account.")
                self._db.save_positions([])
                return True

            import re

            positions = []
            for _, p in pos.iterrows():
                code = str(p.get('code', ''))
                qty = self._nf(p.get('qty'))
                # Skip zero-qty (closed/expired but not yet settled)
                if qty == 0:
                    continue
                cost = self._nf(p.get('cost_price'))
                price = self._nf(p.get('nominal_price'))

                # Classify: STOCK vs CALL/PUT
                opt_match = re.match(
                    r'.*?(\d{2})(\d{2})(\d{2})([CP])(\d+)', code)
                if opt_match:
                    yr, mo, dy = opt_match.group(1), opt_match.group(2), opt_match.group(3)
                    opt_type = 'CALL' if opt_match.group(4) == 'C' else 'PUT'
                    strike_val = float(opt_match.group(5)) / 1000
                    expiry_str = f'20{yr}-{mo}-{dy}'
                    pos_type = opt_type
                else:
                    strike_val = None
                    expiry_str = None
                    pos_type = 'STOCK'

                mv = qty * price if qty and price else 0
                pl = self._nf(p.get('pl_val'))
                basis = abs(qty) * cost * (100 if opt_match else 1) if qty and cost else 0
                pl_pct = (pl / basis * 100) if basis > 0 else 0

                positions.append({
                    'code': code,
                    'pos_type': pos_type,
                    'qty': qty,
                    'cost_price': cost,
                    'current_price': price,
                    'market_value': mv,
                    'pl_val': pl,
                    'pl_pct': round(pl_pct, 2),
                    'strike': strike_val,
                    'expiry': expiry_str,
                })

            self._db.save_positions(positions)
            print(f"  📊 {len(positions)} positions synced "
                  f"({sum(1 for p in positions if p['pos_type'] == 'STOCK')} stocks, "
                  f"{sum(1 for p in positions if p['pos_type'] != 'STOCK')} options)")
            return True
        except Exception as e:
            print(f"  ❌ Position sync error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════
    # ORDER SYNC
    # ═══════════════════════════════════════════════════════════

    def _sync_orders(self, acc_id: int, trd_env) -> int:
        """
        Pull today's filled orders from moomoo → local_trades.
        Skips orders already recorded. Returns count of new orders.
        """
        import re
        from datetime import date as dt_date

        try:
            ret, orders = self.ctx.order_list_query(
                trd_env=trd_env, acc_id=acc_id, refresh_cache=True
            )
            if ret != RET_OK or orders is None or len(orders) == 0:
                return 0

            new_count = 0
            today = dt_date.today().isoformat()

            for _, o in orders.iterrows():
                status = str(o.get('order_status', ''))
                if status not in ('FILLED_ALL', 'FILLED_PART'):
                    continue

                code = str(o.get('code', ''))
                trd_side = str(o.get('trd_side', ''))
                qty = abs(self._nf(o.get('qty')))
                dealt_price = self._nf(o.get('dealt_avg_price')) or self._nf(o.get('price'))
                order_date = str(o.get('updated_time', ''))[:10] or today

                if not code or qty == 0:
                    continue

                # Parse option code or stock ticker
                opt_match = re.match(r'(US\.\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)', code)
                if opt_match:
                    ticker = opt_match.group(1).replace('US.', '')
                    yr, mo, dy = opt_match.group(2), opt_match.group(3), opt_match.group(4)
                    opt_type = 'CALL' if opt_match.group(5) == 'C' else 'PUT'
                    strike = float(opt_match.group(6)) / 1000
                    expiry = f'20{yr}-{mo}-{dy}'
                    strategy = 'CC' if opt_type == 'CALL' else 'CSP'
                    is_option = True
                else:
                    ticker = code.replace('US.', '')
                    opt_type = None
                    strike = None
                    expiry = None
                    strategy = 'STOCK'
                    is_option = False

                # Determine action from side
                if is_option:
                    side_str = str(trd_side)
                    if 'SELL' in side_str.upper():
                        action = 'SELL_CC' if opt_type == 'CALL' else 'SELL_CSP'
                    else:
                        action = 'BUY_BACK'
                else:
                    side_str = str(trd_side)
                    action = 'BUY_STOCK' if 'BUY' in side_str.upper() else 'SELL_STOCK'

                # Compute DTE
                dte = None
                if expiry:
                    try:
                        exp_date = dt_date.fromisoformat(expiry)
                        dte = (exp_date - dt_date.fromisoformat(order_date)).days
                    except Exception:
                        pass

                # Check if already recorded — entry_date now stores actual trade date
                existing = self._db._conn.execute(
                    """SELECT id FROM local_trades
                       WHERE ticker=? AND action=? AND entry_date=?
                       AND entry_price=? AND ABS(COALESCE(strike, 0) - ?) < 0.01
                       LIMIT 1""",
                    (ticker, action, order_date, dealt_price, strike or 0)
                ).fetchone()

                if existing:
                    continue

                # Map to existing OPEN trade (buy back matching an open sell)
                if action == 'BUY_BACK' and strike:
                    # Find matching open trade to close
                    matched = self._db._conn.execute(
                        """SELECT id FROM local_trades
                           WHERE ticker=? AND strike=? AND expiry=?
                           AND status='OPEN' AND action IN ('SELL_CSP', 'SELL_CC')
                           ORDER BY created_at DESC LIMIT 1""",
                        (ticker, strike, expiry)
                    ).fetchone()
                    if matched:
                        self._db.close_trade(matched['id'], exit_price=dealt_price)
                        new_count += 1
                        continue

                # Record new trade with actual order date for dedup
                self._db.log_trade(
                    ticker=ticker, action=action, strategy=strategy,
                    strike=strike, expiry=expiry, dte=dte,
                    contracts=qty, entry_price=dealt_price,
                    entry_date=order_date,
                    notes=f'Auto-synced from moomoo {order_date}',
                )
                new_count += 1

            return new_count
        except Exception as e:
            print(f"  ⚠️  Order sync skipped: {e}")
            return 0

    # ═══════════════════════════════════════════════════════════
    # STATUS / SUMMARY
    # ═══════════════════════════════════════════════════════════

    def show_status(self):
        """Full portfolio status from local DB."""
        summary = self._db.get_portfolio_summary()

        print("=" * 60)
        print("  PORTFOLIO STATUS (from local DB)")
        print("=" * 60)

        f = summary['funds']
        if f:
            print(f"\n💰 FUNDS")
            print(f"  Total Assets:   ${f['total_assets']:>12,.2f}" if f.get('total_assets') else "")
            print(f"  Cash:           ${f['cash']:>12,.2f}" if f.get('cash') else "")
            print(f"  Stock Value:    ${f['stock_value']:>12,.2f}" if f.get('stock_value') else "")
            print(f"  Buying Power:   ${f['buying_power']:>12,.2f}" if f.get('buying_power') else "")
            print(f"  Synced:         {f['synced_at']}")

        positions = self._db.get_positions()
        stocks = [p for p in positions if p['pos_type'] == 'STOCK']
        options = [p for p in positions if p['pos_type'] != 'STOCK']

        if stocks:
            print(f"\n📈 STOCKS ({len(stocks)})")
            print(f"  {'Ticker':<8s} {'Qty':>6s} {'Cost':>10s} {'Price':>10s} "
                  f"{'MktVal':>12s} {'P&L':>12s} {'P&L%':>8s}")
            print(f"  {'-'*8} {'-'*6} {'-'*10} {'-'*10} {'-'*12} {'-'*12} {'-'*8}")
            for s in stocks:
                code = s['code'].replace('US.', '')
                print(f"  {code:<8s} {s['qty']:>6,.0f} "
                      f"${s['cost_price'] or 0:>9,.2f} ${s['current_price'] or 0:>9,.2f} "
                      f"${s['market_value'] or 0:>11,.2f} ${s['pl_val'] or 0:>11,.2f} "
                      f"{s['pl_pct'] or 0:>+7.1f}%")

        if options:
            print(f"\n📊 OPTIONS ({len(options)})")
            print(f"  {'Code':<24s} {'Type':>5s} {'Qty':>5s} {'Strike':>8s} "
                  f"{'Expiry':>12s} {'P&L':>10s}")
            print(f"  {'-'*24} {'-'*5} {'-'*5} {'-'*8} {'-'*12} {'-'*10}")
            for o in options:
                print(f"  {o['code']:<24s} {o['pos_type']:>5s} {o['qty']:>5,.0f} "
                      f"${o['strike'] or 0:>7,.2f} {o['expiry'] or '':>12s} "
                      f"${o['pl_val'] or 0:>9,.2f}")

        trades = self._db.get_open_trades()
        if trades:
            print(f"\n📋 LOCAL OPEN TRADES ({len(trades)})")
            print(f"  {'ID':<5s} {'Ticker':<8s} {'Action':<14s} {'Strike':>8s} "
                  f"{'Expiry':>12s} {'DTE':>4s} {'Entry$':>8s}")
            print(f"  {'-'*5} {'-'*8} {'-'*14} {'-'*8} {'-'*12} {'-'*4} {'-'*8}")
            for t in trades:
                print(f"  {t['id']:<5} {t['ticker']:<8s} {t['action']:<14s} "
                      f"${t['strike'] or 0:>7,.2f} {t['expiry'] or '':>12s} "
                      f"{t['dte'] or '':>4} ${t['entry_price'] or 0:>7,.2f}")

        ts = summary['trades']
        if ts['total_trades'] > 0:
            print(f"\n📊 TRADE P&L SUMMARY")
            print(f"  Total Closed:  {ts['total_trades']}")
            print(f"  Win Rate:      {ts['win_rate']:.1f}%")
            print(f"  Total P&L:     ${ts['total_pnl']:>10,.2f}")
            print(f"  Avg P&L:       ${ts['avg_pnl']:>10,.2f}")
            print(f"  Open:          {ts['open_trades']}")

    def show_summary(self):
        """Quick numbers only."""
        s = self._db.get_portfolio_summary()
        f = s['funds'] or {}
        print(f"Cash=${f.get('cash', 0):,.0f}  "
              f"Stocks={s['positions']['stocks']}(${s['positions']['stock_value']:,.0f})  "
              f"Options={s['positions']['options']}  "
              f"Unrealized=${s['positions']['unrealized_pl']:,.0f}  "
              f"OpenTrades={s['trades']['open']}  "
              f"TotalP&L=${s['trades']['total_pnl']:,.0f}")

    def show_history(self, days: int = 30):
        """Fund history over time."""
        rows = self._db.get_funds_history(days)
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

    @staticmethod
    def _nf(val, default=0.0) -> float:
        try:
            return float(val) if val is not None else default
        except (ValueError, TypeError):
            return default


