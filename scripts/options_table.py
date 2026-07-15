#!/usr/bin/env python3
"""Options position table — buy price, current price, premium, P&L, assignment cost."""
import sys, os, re
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import date
from moomoo import OpenSecTradeContext, OpenQuoteContext, TrdEnv, RET_OK

trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)
quote = OpenQuoteContext(host='127.0.0.1', port=11111, ai_type=1)
ret, acc_list = trd.get_acc_list()
today = date.today()

for _, acc in acc_list.iterrows():
    if str(acc.get('trd_env','')) == 'SIMULATE':
        continue
    acc_id = acc['acc_id']
    ret2, pos_data = trd.position_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
    if ret2 != RET_OK:
        continue

    options = []
    option_codes = []
    for _, p in pos_data.iterrows():
        code = p['code']
        qty = p['qty']
        if qty == 0:
            continue
        if not re.search(r'\d{6}[CP]\d+', str(code)):
            continue
        parts = re.match(r'US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)', code)
        if not parts:
            continue
        ticker = parts.group(1)
        yr, mo, dy = parts.group(2), parts.group(3), parts.group(4)
        opt_type = 'CALL' if parts.group(5) == 'C' else 'PUT'
        strike = float(parts.group(6)) / 1000
        expiry_str = f'20{yr}-{mo}-{dy}'
        cost = p.get('cost_price', 0) or 0
        pl = p.get('pl_val', 0) or 0
        options.append({
            'code': code, 'ticker': ticker, 'type': opt_type,
            'strike': strike, 'expiry': expiry_str,
            'qty': int(qty), 'cost': cost, 'pl': pl,
        })
        option_codes.append(code)

    # Batch fetch current prices
    snapshots = {}
    for i in range(0, len(option_codes), 400):
        batch = option_codes[i:i+400]
        ret3, data = quote.get_market_snapshot(batch)
        if ret3 == RET_OK and data is not None:
            for _, row in data.iterrows():
                snapshots[row['code']] = row

    # Print table
    hdr = (f"{'Ticker':<8s} {'Type':<5s} {'Strike':>7s} {'Expiry':>12s} {'DTE':>4s} "
           f"{'Qty':>4s} {'Buy$':>8s} {'Cur$':>8s} {'Prem$':>8s} "
           f"{'P&L$':>9s} {'Loss%':>7s} {'Assign$':>10s}")
    sep = '-' * len(hdr)
    print(hdr)
    print(sep)

    total_prem = 0
    total_pl = 0
    total_assign = 0

    for o in sorted(options, key=lambda x: x['expiry']):
        code = o['code']
        row = snapshots.get(code)
        cur_bid = float(row.get('bid_price', 0) or 0) if row is not None else 0
        cur_ask = float(row.get('ask_price', 0) or 0) if row is not None else 0
        cur_mid = (cur_bid + cur_ask) / 2 if cur_bid and cur_ask else cur_bid or cur_ask

        try:
            exp_date = date.fromisoformat(o['expiry'])
            dte = (exp_date - today).days
        except:
            dte = 0

        qty = abs(o['qty'])
        buy_price = o['cost']
        cur_price = cur_mid
        premium_collected = buy_price * qty * 100
        pl = o['pl']
        loss_pct = ((cur_price - buy_price) / buy_price * 100) if buy_price > 0 else 0

        if o['type'] == 'PUT':
            assign_cost = o['strike'] * qty * 100
        else:
            assign_cost = 0

        total_prem += premium_collected
        total_pl += pl
        total_assign += assign_cost

        sign = '🔴' if loss_pct > 20 else ('🟡' if loss_pct > 0 else '🟢')
        pl_str = f"${pl:+,.0f}"
        print(f"{o['ticker']:<8s} {o['type']:<5s} ${o['strike']:>6,.0f} "
              f"{o['expiry']:>12s} {dte:>4d} {o['qty']:>4d} "
              f"${buy_price:>7.2f} ${cur_price:>7.2f} ${premium_collected:>7,.0f} "
              f"{pl_str:>9s} {loss_pct:>+6.1f}% {sign} ${assign_cost:>9,.0f}")

    print(sep)
    print(f"{'TOTAL':<8s} {'':5s} {'':>7s} {'':>12s} {'':>4s} {'':>4s} "
          f"{'':>8s} {'':>8s} ${total_prem:>7,.0f} "
          f"${total_pl:>+9,.0f} {'':>7s} ${total_assign:>9,.0f}")
    print(f"\n  Loss% = (current - buy) / buy — positive means underwater (buyback costs more than you sold for)")
    print(f"  Prem$ = premium collected at open (buy_price x contracts x 100)")
    print(f"  Assign$ = cash needed if ALL puts assigned at strike (CALL = $0, shares called away)")

quote.close()
trd.close()
