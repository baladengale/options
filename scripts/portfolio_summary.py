#!/usr/bin/env python3
"""Comprehensive Portfolio Summary — positions, orders, overall P&L."""
import sys, os
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import date, datetime
from collections import defaultdict
from moomoo import OpenSecTradeContext, TrdEnv, RET_OK

def main():
    today = date.today()
    trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)

    # ── 1. Account List ──
    ret, acc_list = trd.get_acc_list()
    if ret != RET_OK:
        print("❌ Cannot connect to moomoo OpenD. Is it running on 127.0.0.1:11111?")
        trd.close()
        return

    for _, acc in acc_list.iterrows():
        if str(acc.get('trd_env', '')) == 'SIMULATE':
            continue
        acc_id = acc['acc_id']
        print("=" * 100)
        print(f"  📋 ACCOUNT: {acc_id}")
        print("=" * 100)

        # ── 2. Account Funds ──
        ret2, funds = trd.accinfo_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
        cash = bp = fund = total_assets = total_liability = net_assets = 0.0
        margin_used = 0.0
        if ret2 == RET_OK and funds is not None:
            f = funds.iloc[0]
            cash = f.get('us_cash', 0) or 0
            bp = f.get('usd_net_cash_power', 0) or 0
            fund = f.get('fund_assets', 0) or 0
            total_assets = f.get('total_assets', 0) or 0
            total_liability = f.get('total_liabilities', 0) or 0
            net_assets = f.get('net_assets', 0) or 0
            margin_used = f.get('margin_used_pct', 0) or 0

            # Convert fund_assets from HKD if needed
            currency = str(f.get('currency', ''))
            if currency == 'HKD' and fund:
                fund = fund / 7.8

        liquid = cash + fund
        print(f"\n  💰 ACCOUNT SUMMARY")
        print(f"  {'─' * 60}")
        print(f"  US Cash:          ${cash:>12,.2f}")
        print(f"  Fund Assets:      ${fund:>12,.2f}")
        print(f"  Liquid (cash+fund):${liquid:>11,.2f}")
        print(f"  Buying Power:     ${bp:>12,.2f}")
        print(f"  Total Assets:     ${total_assets:>12,.2f}")
        print(f"  Total Liabilities:${total_liability:>12,.2f}")
        print(f"  Net Assets:       ${net_assets:>12,.2f}")
        print(f"  Margin Used:      {margin_used:>11.1f}%")

        # ── 3. Positions ──
        ret3, pos_data = trd.position_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
        stocks = {}
        options = {}
        total_stock_mv = 0.0
        total_stock_pl = 0.0
        total_opt_pl = 0.0
        total_csp_liability = 0.0

        if ret3 == RET_OK and pos_data is not None:
            for _, p in pos_data.iterrows():
                code = p['code']
                qty = p['qty']
                if qty == 0:
                    continue

                if '..' in code:  # skip weird codes
                    continue

                import re
                if re.search(r'\d{6}[CP]\d+', code):
                    # Option position
                    parts = re.match(r'US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)', code)
                    if parts:
                        ticker = parts.group(1)
                        yr, mo, dy = parts.group(2), parts.group(3), parts.group(4)
                        opt_type = 'CALL' if parts.group(5) == 'C' else 'PUT'
                        strike_val = float(parts.group(6)) / 1000
                        expiry_str = f'20{yr}-{mo}-{dy}'
                    else:
                        ticker, strike_val, opt_type, expiry_str = '', 0, '', ''

                    cost = p.get('cost_price', 0) or 0
                    pl = p.get('pl_val', 0) or 0
                    options[code] = {
                        'ticker': ticker, 'type': opt_type, 'strike': strike_val,
                        'expiry': expiry_str, 'qty': qty, 'cost': cost, 'pl': pl,
                    }
                    total_opt_pl += pl
                    if opt_type == 'PUT':
                        total_csp_liability += strike_val * abs(qty) * 100

                elif code.startswith('US.'):
                    # Stock position
                    ticker = code.replace('US.', '')
                    cost = p.get('cost_price', 0) or 0
                    price = p.get('nominal_price', 0) or 0
                    mv = qty * price
                    pl = p.get('pl_val', 0) or 0
                    stocks[ticker] = {
                        'qty': qty, 'cost': cost, 'price': price,
                        'mv': mv, 'pl': pl,
                    }
                    total_stock_mv += mv
                    total_stock_pl += pl

        # ── 4. Print Positions ──
        nlv = liquid + total_stock_mv

        print(f"\n  📈 STOCK POSITIONS ({len(stocks)} holdings)")
        print(f"  {'─' * 85}")
        print(f"  {'Ticker':<8s} {'Qty':>6s} {'Price':>10s} {'Cost Basis':>12s} "
              f"{'Mkt Value':>12s} {'P&L':>10s} {'P&L%':>8s}")
        print(f"  {'─' * 85}")
        for ticker in sorted(stocks.keys()):
            s = stocks[ticker]
            pl_pct = (s['pl'] / (s['cost'] * s['qty']) * 100) if s['cost'] and s['qty'] else 0
            print(f"  {ticker:<8s} {int(s['qty']):>6d} ${s['price']:>9,.2f} "
                  f"${s['cost']:>11,.2f} ${s['mv']:>11,.2f} "
                  f"${s['pl']:>9,.0f} {pl_pct:>7.1f}%")
        print(f"  {'─' * 85}")
        print(f"  TOTAL: {int(sum(s['qty'] for s in stocks.values())):>5d} shares                  "
              f"${total_stock_mv:>11,.2f} ${total_stock_pl:>9,.0f}")

        if options:
            print(f"\n  📊 OPTION POSITIONS ({len(options)} contracts)")
            print(f"  {'─' * 95}")
            print(f"  {'Code':<26s} {'Qty':>5s} {'DTE':>4s} {'Strike':>8s} "
                  f"{'Cost':>8s} {'P&L':>10s}")
            print(f"  {'─' * 95}")
            for code in sorted(options.keys()):
                o = options[code]
                try:
                    exp = date.fromisoformat(o['expiry'])
                    dte = (exp - today).days
                except:
                    dte = 0
                print(f"  {code:<26s} {int(o['qty']):>5d} {dte:>4d} "
                      f"${o['strike']:>7,.2f} ${o['cost']:>7.2f} "
                      f"${o['pl']:>9,.0f}")
            print(f"  {'─' * 95}")
            print(f"  TOTAL OPTIONS P&L: ${total_opt_pl:>9,.0f}")

        # ── 5. Order History (ALL TIME) ──
        from datetime import timedelta
        start_date = '2024-01-01'  # Full history
        end_date = today.strftime('%Y-%m-%d')

        # Use history_order_list_query — this is where completed/filled orders live
        ret5, hist_data = trd.history_order_list_query(
            trd_env=TrdEnv.REAL, acc_id=acc_id,
            start=start_date, end=end_date,
        )

        all_orders = []
        total_premium_collected = 0.0   # SELL_SHORT (option sold = cash in)
        total_premium_paid = 0.0        # BUY_BACK (option bought back = cash out)
        total_stock_bought = 0.0        # BUY (stock purchased)
        total_stock_sold = 0.0          # SELL (stock sold)
        total_dividends = 0.0

        # Also process current/working orders
        ret4, order_data = trd.order_list_query(
            trd_env=TrdEnv.REAL, acc_id=acc_id,
            refresh_cache=True
        )
        all_rows = []
        if ret5 == RET_OK and hist_data is not None and len(hist_data) > 0:
            all_rows.extend([(row, 'HIST') for _, row in hist_data.iterrows()])
        if ret4 == RET_OK and order_data is not None and len(order_data) > 0:
            all_rows.extend([(row, 'LIVE') for _, row in order_data.iterrows()])

        # Deduplicate by order_id
        seen_ids = set()
        unique_rows = []
        for row, src in all_rows:
            oid = str(row.get('order_id', ''))
            if oid and oid not in seen_ids:
                seen_ids.add(oid)
                unique_rows.append((row, src))

        print(f"\n  📜 ORDER HISTORY ({len(unique_rows)} orders)")
        print(f"  {'─' * 110}")
        print(f"  {'Date':<12s} {'Ticker':<12s} {'Side':<12s} {'Qty':>5s} "
              f"{'Price':>9s} {'Cash Flow':>12s} {'Status':<14s}")
        print(f"  {'─' * 110}")

        for row, src in unique_rows:
            code = row.get('code', '')
            qty = float(row.get('qty', 0) or 0)
            price = float(row.get('dealt_avg_price', 0) or row.get('price', 0) or 0)
            side = str(row.get('trd_side', ''))
            status = str(row.get('order_status', ''))
            ts_raw = row.get('updated_time', '')
            ts_str = str(ts_raw)[:10] if ts_raw else ''

            is_option = bool(re.search(r'\d{6}[CP]\d+', str(code))) if code else False
            mult = 100 if is_option else 1
            gross = abs(qty) * price * mult

            # Cash flow sign: positive = cash IN, negative = cash OUT
            if side in ('SELL_SHORT', 'SELL'):
                cash_flow = +gross  # money received
            elif side in ('BUY_BACK', 'BUY'):
                cash_flow = -gross  # money spent
            else:
                cash_flow = 0

            # Tally
            if status in ('FILLED_ALL', 'FILLED_PART'):
                if side == 'SELL_SHORT':
                    total_premium_collected += gross
                elif side == 'BUY_BACK':
                    total_premium_paid += gross
                elif side == 'BUY':
                    total_stock_bought += gross
                elif side == 'SELL':
                    total_stock_sold += gross

                all_orders.append({
                    'date': ts_str, 'side': side, 'qty': int(qty),
                    'price': price, 'cash_flow': cash_flow,
                    'is_option': is_option, 'status': status,
                })

            # Ticker display
            ticker_display = str(code)
            if code and str(code).startswith('US.') and not is_option:
                ticker_display = str(code).replace('US.', '')
            elif is_option and code:
                parts = re.match(r'US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)', str(code))
                if parts:
                    ticker_display = f"{parts.group(1)} ${int(parts.group(6))/1000:.0f}{parts.group(5)}"

            cf_str = f"${cash_flow:>+,.0f}" if cash_flow else '—'
            print(f"  {ts_str:<12s} {ticker_display:<12s} {side:<12s} {int(qty):>5d} "
                  f"${price:>8,.2f} {cf_str:>12s} {status:<14s}")

        # Monthly breakdown
        monthly_premium = defaultdict(float)
        monthly_buyback = defaultdict(float)
        for o in all_orders:
            if o['date'] and len(o['date']) >= 7:
                month_key = o['date'][:7]  # YYYY-MM
                if o['side'] == 'SELL_SHORT' and o['is_option']:
                    monthly_premium[month_key] += o['price'] * abs(o['qty']) * 100
                elif o['side'] == 'BUY_BACK' and o['is_option']:
                    monthly_buyback[month_key] += o['price'] * abs(o['qty']) * 100

        net_option_income = total_premium_collected - total_premium_paid

        # ── 6. OVERALL SUMMARY ──
        print(f"\n\n{'='*100}")
        print(f"  📊 OVERALL POSITION SUMMARY")
        print(f"{'='*100}")
        print(f"  Net Liquidation Value:    ${nlv:>12,.2f}")
        print(f"  ── Stock Value:            ${total_stock_mv:>12,.2f}")
        print(f"  ── Liquid (cash+fund):     ${liquid:>12,.2f}")
        print(f"  ── Option Positions:       {len(options):>12d}")
        print(f"  CSP Liability (all puts):  ${total_csp_liability:>12,.0f}")
        print(f"")
        print(f"  Unrealized P&L:")
        print(f"  ── Stock P&L:              ${total_stock_pl:>12,.0f}")
        print(f"  ── Option P&L:             ${total_opt_pl:>12,.0f}")
        print(f"  ── Total Unrealized:       ${total_stock_pl + total_opt_pl:>12,.0f}")
        print(f"")
        print(f"  ALL-TIME OPTION INCOME:")
        print(f"  ── Premium Collected (sold):  ${total_premium_collected:>12,.0f}")
        print(f"  ── Premium Paid (buybacks):   ${total_premium_paid:>12,.0f}")
        print(f"  ── NET OPTION INCOME:         ${net_option_income:>12,.0f}")
        print(f"")
        print(f"  Stock Trading:")
        print(f"  ── Stock Buys:                ${total_stock_bought:>12,.0f}")
        print(f"  ── Stock Sells:               ${total_stock_sold:>12,.0f}")
        print(f"  ── Total Filled Orders:       {len(all_orders):>12d}")

        # Monthly breakdown
        if monthly_premium:
            print(f"\n  📅 MONTHLY OPTION INCOME BREAKDOWN")
            print(f"  {'─' * 65}")
            print(f"  {'Month':<10s} {'Prem Collected':>16s} {'Buybacks':>12s} {'Net Income':>14s}")
            print(f"  {'─' * 65}")
            all_months = sorted(set(list(monthly_premium.keys()) + list(monthly_buyback.keys())))
            for m in all_months:
                col = monthly_premium.get(m, 0)
                buy = monthly_buyback.get(m, 0)
                net = col - buy
                print(f"  {m:<10s} ${col:>15,.0f} ${buy:>11,.0f} ${net:>13,.0f}")
            print(f"  {'─' * 65}")
            total_col = sum(monthly_premium.values())
            total_buy = sum(monthly_buyback.values())
            print(f"  {'TOTAL':<10s} ${total_col:>15,.0f} ${total_buy:>11,.0f} ${total_col - total_buy:>13,.0f}")

        # Sector concentration
        print(f"\n  🏛️  SECTOR BREAKDOWN")
        print(f"  {'─' * 60}")
        sector_map = {
            'V': 'Financial', 'AAPL': 'Technology', 'AMD': 'Technology',
            'AVGO': 'Technology', 'GOOG': 'Technology', 'MSFT': 'Technology',
            'NVDA': 'Technology', 'IBIT': 'Crypto', 'TSLA': 'Consumer Cyclical',
            'VOO': 'ETF', 'SPY': 'ETF', 'SPMO': 'ETF', 'SKHY': 'ETF',
            'PLTR': 'Technology', 'ASTS': 'Technology', 'BE': 'Energy',
            'CRWV': 'Technology', 'IREN': 'Technology', 'MRVL': 'Technology',
            'SOFI': 'Financial',
        }
        sector_vals = defaultdict(float)
        for ticker, s in stocks.items():
            sec = sector_map.get(ticker, 'Other')
            sector_vals[sec] += s['mv']
        for sec in sorted(sector_vals.keys(), key=lambda x: sector_vals[x], reverse=True):
            pct = (sector_vals[sec] / nlv * 100) if nlv > 0 else 0
            bar = '█' * int(pct / 2)
            print(f"  {sec:<20s} ${sector_vals[sec]:>12,.0f}  {pct:>5.1f}%  {bar}")

    trd.close()


if __name__ == '__main__':
    main()
