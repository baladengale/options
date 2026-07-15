#!/usr/bin/env python3
"""PUT/CALL liability overlap analysis + exit logic by DTE."""
import sys, os, re
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import date
from collections import defaultdict
from moomoo import OpenSecTradeContext, OpenQuoteContext, TrdEnv, RET_OK

trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)
quote = OpenQuoteContext(host='127.0.0.1', port=11111, ai_type=1)
today = date.today()

ret, acc_list = trd.get_acc_list()
for _, acc in acc_list.iterrows():
    if str(acc.get('trd_env','')) == 'SIMULATE':
        continue
    acc_id = acc['acc_id']
    ret2, pos_data = trd.position_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
    if ret2 != RET_OK:
        continue

    stocks = {}
    options = []
    codes = []
    for _, p in pos_data.iterrows():
        code = p['code']
        qty = p['qty']
        if qty == 0:
            continue
        if re.search(r'\d{6}[CP]\d+', str(code)):
            parts = re.match(r'US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)', code)
            if not parts:
                continue
            ticker = parts.group(1)
            yr, mo, dy = parts.group(2), parts.group(3), parts.group(4)
            opt_type = 'CALL' if parts.group(5) == 'C' else 'PUT'
            strike = float(parts.group(6)) / 1000
            expiry = f'20{yr}-{mo}-{dy}'
            cost = p.get('cost_price', 0) or 0
            pl = p.get('pl_val', 0) or 0
            options.append({
                'code': code, 'ticker': ticker, 'type': opt_type,
                'strike': strike, 'expiry': expiry,
                'qty': int(qty), 'cost': cost, 'pl': pl,
            })
            codes.append(code)
        elif str(code).startswith('US.') and '..' not in str(code):
            ticker = str(code).replace('US.', '')
            price = p.get('nominal_price', 0) or 0
            stocks[ticker] = {'qty': qty, 'mv': qty * price, 'price': price}

    # Batch fetch current prices
    snapshots = {}
    for i in range(0, len(codes), 400):
        batch = codes[i:i+400]
        ret3, data = quote.get_market_snapshot(batch)
        if ret3 == RET_OK and data is not None:
            for _, row in data.iterrows():
                snapshots[row['code']] = row

    # ═══════════════════════════════════════════════════
    # PART 1: EXIT LOGIC BY DTE + PROFIT CAPTURED
    # ═══════════════════════════════════════════════════
    W = 120
    print('=' * W)
    print('  EXIT LOGIC — Per DTE & Profit Captured Rules')
    print('=' * W)
    hdr = (f"  {'Ticker':<6s} {'Type':<4s} {'Strike':>7s} {'Expiry':>10s} "
           f"{'DTE':>4s} {'Delta':>6s} {'Bid$':>7s} {'Cost$':>7s} "
           f"{'Capt%':>7s} {'P&L$':>8s}  {'Decision':<45s}")
    print(hdr)
    print(f"  {'-'*6} {'-'*4} {'-'*7} {'-'*10} {'-'*4} {'-'*6} {'-'*7} {'-'*7} {'-'*7} {'-'*8}  {'-'*45}")

    decisions = defaultdict(list)
    for o in sorted(options, key=lambda x: x['expiry']):
        row = snapshots.get(o['code'])
        cur_bid = float(row.get('bid_price', 0) or 0) if row is not None else 0
        delta = float(row.get('option_delta', 0) or 0) if row is not None else 0
        cost = o['cost']
        pl = o['pl']
        try:
            exp = date.fromisoformat(o['expiry'])
            dte = (exp - today).days
        except:
            dte = 0

        profit_captured = ((cost - cur_bid) / cost * 100) if cost > 0 else 0
        abs_delta = abs(delta)
        itm = abs_delta > 0.50
        underwater = profit_captured < 0
        large_loss = pl < -500

        # Decision — highest priority first
        if profit_captured >= 70:
            decision = 'CLOSE (70%+ profit captured — exit now)'
            cat = 'CLOSE_NOW'
        elif profit_captured >= 50:
            decision = 'CLOSE (50%+ profit — standard TastyTrade exit)'
            cat = 'CLOSE'
        elif profit_captured >= 30:
            decision = 'HOLD — 30%+ captured, theta working'
            cat = 'HOLD'
        elif abs_delta >= 0.60:
            decision = 'STOP LOSS — delta >= 0.60, assignment imminent'
            cat = 'STOP'
        elif dte <= 3:
            decision = 'EXPIRING — gamma explosion risk, close or roll NOW'
            cat = 'URGENT'
        elif itm and dte <= 21:
            decision = 'ITM + near expiry — roll for credit or accept assignment'
            cat = 'URGENT'
        elif underwater and dte <= 21 and large_loss:
            decision = 'UNDERWATER + near — consider rolling to 45 DTE'
            cat = 'ROLL'
        elif large_loss:
            decision = 'UNDERWATER (>$500) — evaluate: roll, close, or hold?'
            cat = 'WATCH'
        elif underwater and dte <= 7:
            decision = 'UNDERWATER + near expiry — act soon'
            cat = 'WATCH'
        elif underwater:
            decision = 'MONITOR — slightly underwater, theta should help'
            cat = 'MONITOR'
        elif dte <= 7:
            decision = 'NEAR EXPIRY — monitor daily, close at 50%+'
            cat = 'MONITOR'
        else:
            decision = 'HOLD — theta decaying, on track'
            cat = 'HOLD'

        decisions[cat].append(f"{o['ticker']} {o['type']}${o['strike']:,.0f}")

        capt_str = f"{profit_captured:+.1f}%"
        print(f"  {o['ticker']:<6s} {o['type']:<4s} ${o['strike']:>6,.0f} "
              f"{o['expiry']:>10s} {dte:>4d} {delta:>+6.3f} "
              f"${cur_bid:>6.2f} ${cost:>6.2f} {capt_str:>7s} ${pl:>+7,.0f}  {decision:<45s}")

    # Summary
    print(f"\n  PRIORITY QUEUE:")
    for cat, label in [
        ('STOP',     '🛑 STOP LOSS'),
        ('URGENT',   '⚠️  URGENT (act this week)'),
        ('ROLL',     '🔄 ROLL CANDIDATES'),
        ('WATCH',    '🔴 WATCH CLOSELY'),
        ('MONITOR',  '🟡 MONITOR'),
        ('CLOSE_NOW','✅ CLOSE NOW'),
        ('CLOSE',    '✅ CLOSE'),
        ('HOLD',     '👍 HOLD'),
    ]:
        if cat in decisions:
            print(f"  {label}: {', '.join(decisions[cat])}")

    # ═══════════════════════════════════════════════════
    # PART 2: PUT/CALL OVERLAP
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * W}")
    print(f"  PUT/CALL LIABILITY OVERLAP — Per Ticker")
    print(f"{'=' * W}")

    by_ticker = defaultdict(lambda: {'calls': [], 'puts': []})
    for o in options:
        by_ticker[o['ticker']][o['type'].lower() + 's'].append(o)

    for ticker, pos in sorted(by_ticker.items()):
        calls = pos['calls']
        puts = pos['puts']
        if not calls or not puts:
            continue  # no overlap — single-sided

        shares = int(stocks.get(ticker, {}).get('qty', 0))
        share_price = stocks.get(ticker, {}).get('price', 0)

        call_contracts = sum(abs(c['qty']) for c in calls)
        put_contracts = sum(abs(p['qty']) for p in puts)
        call_shares = call_contracts * 100
        put_shares = put_contracts * 100
        total_call_prem = sum(c['cost'] * abs(c['qty']) * 100 for c in calls)
        total_put_prem = sum(p['cost'] * abs(p['qty']) * 100 for p in puts)

        print(f"\n  {'─' * 80}")
        print(f"  🔀 {ticker} — {shares} shares owned @ ~${share_price:,.2f} "
              f"(MktVal ${shares * share_price:,.0f})")
        print(f"  {'─' * 80}")

        # Calls
        print(f"\n  📤 SHORT CALLS ({call_contracts} contracts — owe {call_shares} shares):")
        for c in sorted(calls, key=lambda x: x['expiry']):
            try: cdte = (date.fromisoformat(c['expiry']) - today).days
            except: cdte = 0
            row = snapshots.get(c['code'])
            delta = float(row.get('option_delta', 0) or 0) if row is not None else 0
            itm_flag = '🔥 ITM' if abs(delta) > 0.50 else ('⚠️  near' if abs(delta) > 0.35 else '   OTM')
            print(f"     {c['type']} ${c['strike']:>6,.0f}  exp {c['expiry']}  "
                  f"DTE={cdte:>3d}  delta={delta:+.3f}  {itm_flag}  "
                  f"×{abs(c['qty'])}  → deliver {abs(c['qty'])*100} shares @ ${c['strike']:,.0f}")

        # Puts
        print(f"\n  📥 SHORT PUTS ({put_contracts} contracts — may buy {put_shares} shares):")
        total_put_assign = 0
        for p in sorted(puts, key=lambda x: x['expiry']):
            try: pdte = (date.fromisoformat(p['expiry']) - today).days
            except: pdte = 0
            row = snapshots.get(p['code'])
            delta = float(row.get('option_delta', 0) or 0) if row is not None else 0
            itm_flag = '🔥 ITM' if abs(delta) > 0.50 else ('⚠️  near' if abs(delta) > 0.35 else '   OTM')
            assign = abs(p['qty']) * p['strike'] * 100
            total_put_assign += assign
            print(f"     {p['type']}  ${p['strike']:>6,.0f}  exp {p['expiry']}  "
                  f"DTE={pdte:>3d}  delta={delta:+.3f}  {itm_flag}  "
                  f"×{abs(p['qty'])}  → buy {abs(p['qty'])*100} shares   needs ${assign:>,.0f} cash")

        # Same-expiry same-strike straddles
        print(f"\n  ⚡ STRADDLE/OVERLAP DETECTION:")
        found = False
        for c in calls:
            for p in puts:
                if c['strike'] == p['strike'] and c['expiry'] == p['expiry']:
                    found = True
                    try: cdte = (date.fromisoformat(c['expiry']) - today).days
                    except: cdte = 0
                    prem = (c['cost'] + p['cost']) * 100
                    be_low = c['strike'] - c['cost'] - p['cost']
                    be_high = c['strike'] + c['cost'] + p['cost']
                    print(f"     SHORT STRADDLE: CALL+PUT @ ${c['strike']:,.0f}  exp {c['expiry']}  (DTE {cdte})")
                    print(f"     ├─ Premium collected: ${prem:,.2f}  ({c['cost']:.2f} call + {p['cost']:.2f} put)")
                    print(f"     ├─ Max profit: ${prem:,.2f}  (if {ticker} closes EXACTLY at ${c['strike']:,.0f})")
                    print(f"     ├─ Breakevens: ${be_low:,.2f} – ${be_high:,.2f}")
                    print(f"     └─ At expiry: one side WILL be ITM unless exactly at strike")
        if not found:
            # Check for strangles (different strikes, same expiry)
            expiry_pairs = defaultdict(lambda: {'calls': [], 'puts': []})
            for c in calls:
                try: e = c['expiry']
                except: e = ''
                expiry_pairs[e]['calls'].append(c)
            for p in puts:
                try: e = p['expiry']
                except: e = ''
                expiry_pairs[e]['puts'].append(p)
            for exp, pair in expiry_pairs.items():
                if pair['calls'] and pair['puts']:
                    found = True
                    call_strikes = ', '.join(f"${c['strike']:,.0f}" for c in pair['calls'])
                    put_strikes = ', '.join(f"${p['strike']:,.0f}" for p in pair['puts'])
                    print(f"     STRANGLE @ {exp}: CALLS at {call_strikes} + PUTS at {put_strikes}")
                    print(f"     └─ Both sides expire same day — one will be ITM if stock moves")
        if not found:
            print(f"     No same-expiry straddle/strangle found")

        # Net scenarios
        print(f"\n  📐 NET SCENARIOS for {ticker} (shares: {shares}):")
        print(f"     IF ALL CALLS EXERCISE: deliver {call_shares} shares → "
              f"{shares - call_shares} remaining ({shares - call_shares} shares)")
        print(f"     IF ALL PUTS ASSIGN:   buy {put_shares} shares → "
              f"{shares + put_shares} total (need ${total_put_assign:,.0f} cash)")
        print(f"     IF ALL EXERCISE:      deliver {call_shares} + buy {put_shares} → "
              f"net {shares - call_shares + put_shares} shares")

        # V-specific stacked call risk
        if ticker == 'V' and len(calls) >= 2:
            print(f"\n  ⚠️  STACKED CALL RISK — {ticker} has {call_contracts} calls layered:")
            sorted_calls = sorted(calls, key=lambda x: x['expiry'])
            cumulative = 0
            for c in sorted_calls:
                cumulative += abs(c['qty']) * 100
                try: d = (date.fromisoformat(c['expiry']) - today).days
                except: d = 0
                remaining = shares - cumulative
                print(f"     After {c['expiry']} (DTE {d}): "
                      f"{abs(c['qty'])*100} shares called @ ${c['strike']:,.0f} → "
                      f"{remaining} shares left")

    # ═══════════════════════════════════════════════════
    # PART 3: TICKERS WITH EARNINGS BLACKOUT CONFLICTS
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * W}")
    print(f"  EARNINGS BLACKOUT — Positions crossing earnings dates")
    print(f"{'=' * W}")

    # Known earnings dates
    earnings = {
        'GOOGL': '2026-07-23', 'V': '2026-07-29', 'MSFT': '2026-07-30',
        'META': '2026-07-30', 'AAPL': '2026-07-31', 'AMZN': '2026-07-31',
        'AMD': '2026-08-05', 'NVDA': '2026-08-27', 'CRM': '2026-09-03',
        'AVGO': '2026-09-04', 'ADBE': '2026-09-11',
    }

    for o in sorted(options, key=lambda x: x['expiry']):
        ticker = o['ticker']
        if ticker not in earnings:
            continue
        er_date_str = earnings[ticker]
        try:
            er_date = date.fromisoformat(er_date_str)
            exp_date = date.fromisoformat(o['expiry'])
            days_after_er = (exp_date - er_date).days
            days_to_er = (er_date - today).days
        except:
            continue

        if days_to_er < 0:
            continue  # earnings already passed

        if days_to_er <= 14:
            warning = '🔴 BLACKOUT — should close before earnings' if days_to_er <= 14 else ''
            row = snapshots.get(o['code'])
            cur_bid = float(row.get('bid_price', 0) or 0) if row is not None else 0
            cost = o['cost']
            profit_captured = ((cost - cur_bid) / cost * 100) if cost > 0 else 0
            print(f"  {o['ticker']:<6s} {o['type']}${o['strike']:>6,.0f}  "
                  f"exp {o['expiry']}  earnings {er_date_str} ({days_to_er}d away)  "
                  f"expires {days_after_er}d after ER  "
                  f"captured {profit_captured:+.1f}%  {warning}")

quote.close()
trd.close()
