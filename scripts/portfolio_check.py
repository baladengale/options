#!/usr/bin/env python3
"""Portfolio Position Checker — score every open position, give buy/sell/hold decisions."""
import argparse
import os
import re
import sys
import warnings
from datetime import date, datetime
from typing import Optional

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from moomoo import OpenSecTradeContext, TrdEnv, RET_OK
from src.logging_setup import get_logger
log = get_logger('portfolio')

from src.data.moomoo_client import MoomooClient
from src.data.yfinance_client import YFinanceClient
from src.data.compute import enrich_stock_snapshot
from src.data.guardrails import GuardrailChecker, SECTOR_MAP


def main():
    parser = argparse.ArgumentParser(description='Portfolio Position Checker')
    parser.add_argument('--no-external', action='store_true', help='Skip yfinance (offline)')
    args = parser.parse_args()

    today = date.today()

    # ── FETCH MACRO ──
    regime = 'NEUTRAL'
    regime_mult = 1.0
    if not args.no_external:
        try:
            from src.analysis.sentiment import get_macro_context
            yf = YFinanceClient()
            macro = get_macro_context(yf)
            regime = macro.market_regime
            regime_mult = macro.position_mult
            print(f"🌍 VIX {macro.vix or 'N/A'} | {regime} | Size: {regime_mult:.0%}")
        except Exception:
            pass
    print()

    # ── FETCH PORTFOLIO ──
    stocks, options, cash, bp, fund = _fetch_positions()
    liquid = cash + fund
    log.info(f"PORTFOLIO_CHECK|regime={regime}|"
             f"stocks={len(stocks)}|options={len(options)}|"
             f"liquid=${liquid:,.0f}|bp=${bp:,.0f}")
    if not stocks and not options:
        print("No positions found.")
        return

    liquid = cash + fund
    print(f"💰 Liquid: ${liquid:,.0f} (cash ${cash:,.0f} + fund ${fund:,.0f}) | "
          f"BP: ${bp:,.0f} | {len(stocks)} stocks, {len(options)} options\n")

    with MoomooClient() as moomoo:
        yf_client = YFinanceClient() if not args.no_external else None

        # ── SCORE STOCKS ──
        if stocks:
            rows = []
            for ticker, pos in stocks.items():
                qty, cost = pos['qty'], pos['cost']
                full_ticker = f'US.{ticker}'
                snap = moomoo.get_stock_snapshot(full_ticker)
                if snap is None or snap.last_price <= 0:
                    rows.append((ticker, qty, cost, 0, 0, 'N/A', '5.0', 'NO DATA', True))
                    continue
                price, mv = snap.last_price, qty * snap.last_price
                pl_pct = ((price - cost) / cost * 100) if cost > 0 else 0
                history = moomoo.get_price_history(full_ticker, 252)
                if history:
                    enrich_stock_snapshot(snap, history)
                score = _score_holding(snap, ticker, yf_client, regime, regime_mult)
                best_cc = _find_best_cc(moomoo, ticker, snap, qty, cost, yf_client, regime, regime_mult)
                if best_cc:
                    dec = f"SELL CC ${best_cc['strike']:.0f} {best_cc['expiry']} @ {best_cc['roc']:.1f}%"
                    actionable = True
                    log.info(f"STOCK|{ticker}|qty={qty:.0f}|price=${price:.2f}|cost=${cost:.2f}|"
                             f"P&L={pl_pct:+.1f}%|score={score:.1f}|CC ${best_cc['strike']:.0f}|{best_cc['expiry']}|RoC={best_cc['roc']:.1f}%")
                elif qty < 100:
                    dec = "HOLD (<100 shares, can't sell CC)"
                    actionable = False
                else:
                    dec = "HOLD (no calls with Δ 0.15-0.35 + OI≥10 + Vol≥10)"
                    actionable = False
                rows.append((ticker, qty, cost, price, mv, pl_pct, f"{score:.1f}", dec, actionable))

            rows.sort(key=lambda r: (not r[8], r[0]))
            hdr = (f"  {'Ticker':<8s} {'Qty':>6s} {'Price':>10s} {'Cost':>10s} "
                   f"{'MktVal':>12s} {'P&L%':>8s} {'Score':>6s} {'Decision':>45s}")
            sep = f"  {'-'*8} {'-'*6} {'-'*10} {'-'*10} {'-'*12} {'-'*8} {'-'*6} {'-'*45}"

            for r in rows:
                if r[8] and r == [x for x in rows if x[8]][0]:
                    print(f"\n  🎯 COVERED CALL CANDIDATES:")
                    print(hdr); print(sep)
                elif not r[8] and r == [x for x in rows if not x[8]][0]:
                    print(f"\n  📋 HOLD:")
                    print(hdr); print(sep)
                _print_stock_row(r[0], r[1], r[2], r[3], r[4], r[5], r[6], '', r[7])

        # ── PRE-FETCH ALL OPTION SNAPSHOTS IN ONE BATCH ──
        option_codes = list(options.keys())
        snapshot_map = {}
        batch_size = 400
        for i in range(0, len(option_codes), batch_size):
            batch = option_codes[i:i + batch_size]
            ret, data = moomoo.ctx.get_market_snapshot(batch)
            if ret == RET_OK and data is not None:
                for _, row in data.iterrows():
                    code = row.get('code', '')
                    if code:
                        snapshot_map[code] = row

        # ── SCORE OPTIONS ──
        if options:
            print(f"\n{'='*100}")
            print(f"  📊 OPTION POSITIONS ({len(options)})")
            print(f"{'='*100}")
            print(f"  {'Code':<24s} {'Qty':>5s} {'DTE':>4s} {'Δ':>7s} "
                  f"{'Cost':>8s} {'Bid':>8s} {'P&L':>10s} {'P&L%':>7s} "
                  f"{'Profit%':>8s} {'Score':>6s} {'Decision':>24s}")
            print(f"  {'-'*24} {'-'*5} {'-'*4} {'-'*7} {'-'*8} {'-'*8} {'-'*10} {'-'*7} {'-'*8} {'-'*6} {'-'*24}")

            for code, pos in sorted(options.items()):
                qty = pos['qty']
                cost = pos['cost']
                ticker = pos['ticker']
                option_type = pos['type']
                strike = pos['strike']
                expiry = pos['expiry']

                # Lookup in batch snapshot
                row = snapshot_map.get(code)
                current = None
                if row is not None:
                    # Build a mini OptionSnapshot from the raw row
                    current = _parse_snapshot_row(row)

                # Compute DTE from expiry date even if chain data missing
                computed_dte = 0
                if expiry:
                    try:
                        exp_date = date.fromisoformat(expiry)
                        computed_dte = (exp_date - today).days
                    except Exception:
                        pass

                if current is None:
                    if computed_dte < 0:
                        decision = '✅ EXPIRED — premium kept'
                        score = 1.0
                    elif computed_dte == 0:
                        decision = '⚠️  EXPIRING TODAY'
                        score = 3.0
                    else:
                        decision = '⚠️  NO CHAIN DATA'
                        score = 'N/A'
                    _print_opt_row(code, qty, computed_dte, 0, cost, 0,
                                   pos.get('pl', 0), pos.get('pl_pct', 0), 0,
                                   str(score), decision)
                    continue

                bid = current.bid or 0
                # Use moomoo's P&L directly (already correct for short options)
                pl = pos.get('pl', 0) or 0
                basis = abs(qty) * cost * 100
                pl_pct = (pl / basis * 100) if basis > 0 else 0

                # % of max premium captured (for short options: cost→0 = 100% captured)
                profit_captured = ((cost - bid) / cost * 100) if cost > 0 else 0

                # Score and decision
                dte = current.dte or 0
                delta = abs(current.delta or 0)
                score, decision = _score_option(pos, current, profit_captured, pl, today, yf_client)

                log.info(f"OPTION|{ticker}|{option_type}|${strike:.0f}|{expiry}|"
                         f"DTE={dte}|Δ={delta:+.3f}|cost=${cost:.2f}|bid=${bid:.2f}|"
                         f"P&L={pl:+.0f}|captured={profit_captured:.0f}%|score={score:.1f}|{decision}")

                _print_opt_row(code, qty, dte, current.delta, cost, bid,
                               pl, pl_pct, profit_captured, f"{score:.1f}", decision)

    # ═══════════════════════════════════════════════════════════
    # GUARDRAILS
    # ═══════════════════════════════════════════════════════════
    total_stock_val = sum(
        pos.get('mv', 0) if isinstance(pos, dict) else 0
        for pos in stocks.values()
    )
    net_liq = liquid + total_stock_val

    gc_positions = []
    for ticker, pos in stocks.items():
        notional = pos.get('mv', 0) if isinstance(pos, dict) else (pos * 100)
        csp_liability = sum(
            abs(opt['strike']) * abs(opt['qty']) * 100
            for code, opt in options.items()
            if opt['ticker'] == ticker and opt['type'] == 'PUT'
        )
        gc_positions.append({
            'ticker': ticker, 'notional': notional,
            'sector': SECTOR_MAP.get(ticker, 'Unknown'),
            'csp_liability': csp_liability,
        })

    gc = GuardrailChecker(net_liq=net_liq, cash=liquid, buying_power=bp,
                           open_positions=gc_positions)
    gr = gc.check()

    print(f"\n{'='*90}")
    print(f"  🛡️  POSITION SIZING GUARDRAILS")
    print(f"{'='*90}")
    print(f"  Net Liq: ${net_liq:>12,.0f} | Liquid: ${liquid:>10,.0f} ({gr.cash_buffer_pct:.0f}%) | "
          f"Positions: {gr.open_positions} (max {GuardrailChecker.MAX_OPEN_POSITIONS()})")
    print(f"  Max single: {gr.max_single_position_pct:.0f}% (limit 15%) | "
          f"Max sector: {gr.max_sector_pct:.0f}% (limit 25%)")
    if gr.worst_case_shortfall > 0:
        print(f"  ⚠️  CSP liability: ${gr.worst_case_assignment:,.0f} — shortfall ${gr.worst_case_shortfall:,.0f}")
    else:
        print(f"  ✅ All CSPs covered — ${gr.worst_case_assignment:,.0f} liability")

    log.info(f"GUARDRAILS|NLV=${net_liq:,.0f}|cash_buf={gr.cash_buffer_pct:.0f}%|"
             f"positions={gr.open_positions}|max_single={gr.max_single_position_pct:.0f}%|"
             f"blocks={len(gr.blocks)}|warns={len(gr.warnings)}")
    for b in gr.blocks:
        print(f"  🔴 BLOCK: {b}")
        log.warning(f"BLOCK: {b}")
    for w in gr.warnings:
        print(f"  🟡 WARN: {w}")
        log.warning(f"WARN: {w}")
    if gr.all_clear and not gr.warnings:
        print(f"  ✅ All within limits")


def _fetch_positions() -> tuple[dict, dict, float, float, float]:
    """Fetch live stock + option positions from moomoo. Returns (stocks, options, cash, bp, fund)."""
    stocks, options = {}, {}
    cash, bp, fund = 0.0, 0.0, 0.0

    try:
        trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)
        ret, acc_list = trd.get_acc_list()
        if ret != RET_OK:
            trd.close()
            return stocks, options, cash, bp, fund

        for _, acc in acc_list.iterrows():
            if str(acc.get('trd_env', '')) == 'SIMULATE':
                continue

            acc_id = acc['acc_id']
            # Funds
            ret2, funds = trd.accinfo_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
            if ret2 == RET_OK:
                f = funds.iloc[0]
                cash = (f.get('us_cash', 0) or 0)
                bp = (f.get('usd_net_cash_power', 0) or 0)
                fund = (f.get('fund_assets', 0) or 0)
                # Convert fund_assets from HKD to USD if needed
                currency = str(f.get('currency', ''))
                if currency == 'HKD' and fund:
                    fund = fund / 7.8

            # Positions
            ret3, pos_data = trd.position_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
            if ret3 != RET_OK or pos_data is None:
                continue

            for _, p in pos_data.iterrows():
                code = p['code']
                qty = p['qty']

                # Skip zero-qty positions (closed/expired but not yet settled)
                if qty == 0:
                    continue

                if re.search(r'\d{6}[CP]\d+', code):
                    # Parse: US.V260918C360000 → ticker=V, expiry=2026-09-18, type=CALL, strike=360.00
                    parts = re.match(r'US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)', code)
                    if parts:
                        ticker = parts.group(1)
                        yr, mo, dy = parts.group(2), parts.group(3), parts.group(4)
                        opt_type = 'CALL' if parts.group(5) == 'C' else 'PUT'
                        strike_val = float(parts.group(6)) / 1000  # moomoo encodes strike × 1000
                        expiry_str = f'20{yr}-{mo}-{dy}'
                    else:
                        ticker, strike_val, opt_type, expiry_str = '', 0, '', ''

                    cost = p.get('cost_price', 0) or 0
                    options[code] = {
                        'ticker': ticker,
                        'type': opt_type,
                        'strike': strike_val,
                        'expiry': expiry_str,
                        'qty': qty,
                        'cost': cost,
                        'delta': 0,
                        'dte': 0,
                        'pl': p.get('pl_val', 0) or 0,
                        'pl_pct': (p.get('pl_ratio', 0) or 0) * 100,
                    }
                elif code.startswith('US.') and '..' not in code:
                    # Stock position
                    ticker = code.replace('US.', '')
                    cost = p.get('cost_price', 0) or 0
                    price = p.get('nominal_price', 0) or 0
                    stocks[ticker] = {
                        'qty': qty,
                        'cost': cost,
                        'price': price,
                        'mv': qty * price,
                        'pl': p.get('pl_val', 0) or 0,
                        'pl_pct': (p.get('pl_ratio', 0) or 0) * 100,
                    }
        trd.close()
        return stocks, options, cash, bp, fund
    except Exception:
        pass

    return stocks, options, cash, bp, 0.0


def _score_holding(snap, ticker: str, yf_client, regime: str, regime_mult: float) -> float:
    """Score a stock holding 1-10 (1=best, borrows from screener logic)."""
    score = 5.0  # neutral base

    # RSI
    rsi = snap.rsi_14 or 50
    if 45 <= rsi <= 55:    score -= 1.0
    elif 30 <= rsi <= 70:  score += 0.0
    else:                  score += 2.0

    # Trend
    if snap.sma_50 and snap.sma_200:
        if snap.last_price > snap.sma_50 > snap.sma_200:
            score -= 1.0
        elif snap.last_price < snap.sma_200:
            score += 1.5

    # Volume ratio
    if snap.volume_ratio:
        if snap.volume_ratio > 1.5:   score -= 0.5
        elif snap.volume_ratio < 0.5: score += 0.5

    # External sentiment
    if yf_client:
        ratings = yf_client.get_analyst_ratings(ticker)
        if ratings:
            if ratings.consensus == 'STRONG_BUY':  score -= 1.0
            elif ratings.consensus == 'BUY':       score -= 0.5
            elif ratings.consensus == 'SELL':      score += 2.0

        earnings = yf_client.get_earnings(ticker)
        if earnings and earnings.in_blackout:
            score += 1.5

        news = yf_client.get_news_sentiment_score(ticker)
        if news:
            if news['score'] >= 70:    score -= 0.5
            elif news['score'] <= 30:  score += 1.0

    # Regime
    if regime == 'BEARISH':   score += 2.0
    elif regime == 'VOLATILE': score += 1.0

    return max(1.0, min(10.0, score))


def _find_best_cc(moomoo, ticker: str, snap, shares: float, cost_basis: float,
                  yf_client, regime: str, regime_mult: float) -> Optional[dict]:
    """Find best covered call candidate for a stock holding.
    GOAL.md rule: Never sell CC below cost basis."""
    if shares < 100:
        return None

    contracts = moomoo.get_option_snapshots(f'US.{ticker}', dte_min=7, dte_max=60)
    best = None
    best_score = 999

    for c in contracts:
        if c.option_type != 'CALL':
            continue
        # GOAL.md: Never sell CC below cost basis
        if c.strike <= cost_basis:
            continue
        if (c.bid or 0) <= 0 or (c.open_interest or 0) < 10 or (c.volume or 0) < 10:
            continue
        if (c.delta or 0) < 0.15 or (c.delta or 0) > 0.35:
            continue
        if not (c.implied_vol and 0 < c.implied_vol < 500):
            continue

        roc = (c.bid / snap.last_price) * (365.0 / c.dte) * 100 if snap.last_price and c.dte else 0
        if roc < 8.0:
            continue

        # Penalty scoring
        penalty = 0.0
        if c.dte < 7:            penalty += 99
        elif c.dte < 14:         penalty += 3.0
        elif c.dte < 21:         penalty += 1.5
        elif 30 <= c.dte <= 45:  penalty -= 0.5
        if (c.open_interest or 0) < 100:  penalty += 1.5
        elif (c.open_interest or 0) < 500: penalty += 0.5
        if roc > 24:             penalty -= 1.5
        elif roc > 18:           penalty -= 0.8

        if penalty < best_score:
            best_score = penalty
            best = {
                'strike': c.strike,
                'expiry': c.expiry,
                'dte': c.dte,
                'delta': c.delta,
                'bid': c.bid,
                'roc': roc,
                'strike_str': f'${c.strike:.0f} {c.expiry}',
            }

    return best


def _score_option(pos: dict, current, profit_captured: float, pl: float,
                  today: date, yf_client) -> tuple[float, str]:
    """Score an option position 1-10. Returns (score, decision)."""
    score = 5.0
    decision = 'HOLD'
    dte = current.dte or 0
    delta = abs(current.delta or 0)
    strategy = 'CC' if pos['type'] == 'CALL' else 'CSP'

    # Profit captured — close at 50%+
    if profit_captured >= 70:
        score -= 2.0
        decision = '✅ CLOSE (70%+ profit)'
    elif profit_captured >= 50:
        score -= 1.5
        decision = '✅ CLOSE (50%+ profit)'
    elif profit_captured >= 30:
        score -= 0.5
        decision = '👍 HOLD (30%+ captured)'

    # DTE check
    if dte <= 3:
        score -= 1.5
        if 'CLOSE' not in decision:
            decision = '⚠️  EXPIRING — close or roll'
    elif dte <= 7:
        score += 1.0
        if profit_captured < 30:
            decision = '⚠️  NEAR EXPIRY — monitor closely'
    elif dte <= 14:
        if profit_captured < 0:  # underwater
            score += 1.0
            decision = '🔄 ROLL at 21 DTE' if dte > 21 else '⚠️  UNDERWATER — consider rolling'
    elif dte <= 21:
        if profit_captured < 0:
            decision = '🔄 CONSIDER ROLLING'

    # Layer 2: Delta gates (from config)
    if strategy == 'CSP' and delta >= 0.60:
        score += 2.0
        decision = '🛑 DELTA STOP — |Δ|≥0.60, cut position'
    elif strategy == 'CC' and delta >= 0.50:
        score += 1.5
        decision = '⚠️  DELTA WARN — Δ≥0.50, assignment risk'
    elif strategy == 'CSP' and delta >= 0.50:
        score += 1.0
        if 'CLOSE' not in decision and 'STOP' not in decision:
            decision = '⚠️  ITM — assignment risk'

    # Layer 1: Premium multiple stop-loss (DTE-adjusted)
    # profit_captured is a percentage: positive = profit, negative = loss
    if profit_captured < 0:  # position is underwater
        loss_multiple = abs(profit_captured) / 100  # -150% → 1.5× loss

        if dte > 30:
            if loss_multiple >= 3.0:
                score += 2.0; decision = '🛑 STOP LOSS — 3× premium lost'
            elif loss_multiple >= 2.0:
                score += 1.0
                if 'STOP' not in decision: decision = '⚠️  STOP ALERT — 2× premium lost'
        elif dte > 21:
            if loss_multiple >= 2.0:
                score += 2.0; decision = '🛑 STOP LOSS — 2× premium, consider rolling'
            elif loss_multiple >= 1.0:
                score += 1.0
                if 'STOP' not in decision: decision = '⚠️  STOP ALERT — 1× premium lost'
        else:  # dte <= 21
            if loss_multiple >= 1.5:
                score += 2.5; decision = '🛑 STOP LOSS — 1.5× premium, gamma risk'
            elif loss_multiple >= 0.5:
                score += 1.0
                if 'STOP' not in decision: decision = '⚠️  NEAR STOP — monitor closely'

    # Earnings blackout
    if yf_client and pos['ticker']:
        earnings = yf_client.get_earnings(pos['ticker'])
        if earnings and earnings.in_blackout and dte > earnings.days_to_earnings:
            score += 1.5
            decision = '⚠️  EARNINGS IN DTE — close before'

    # Heavy loss catch-all
    if pl < -1000 and 'STOP' not in decision:
        score += 1.5
        if 'CLOSE' not in decision:
            decision = '🔴 UNDERWATER — evaluate exit'

    return max(1.0, min(10.0, score)), decision


class _OptionCurrent:
    """Lightweight option snapshot from batch API."""
    def __init__(self, row):
        self.bid = float(row.get('bid_price', 0) or 0)
        self.ask = float(row.get('ask_price', 0) or 0)
        self.last_price = float(row.get('last_price', 0) or 0)
        self.delta = float(row.get('option_delta', 0) or 0)
        self.gamma = float(row.get('option_gamma', 0) or 0)
        self.implied_vol = float(row.get('option_implied_volatility', 0) or 0)
        self.open_interest = int(row.get('option_open_interest', 0) or 0)
        self.volume = int(row.get('volume', 0) or 0)
        self.dte = int(row.get('option_expiry_date_distance', 0) or 0)
        self.strike = float(row.get('option_strike_price', 0) or 0)
        self.option_type = str(row.get('option_type', ''))


def _parse_snapshot_row(row) -> Optional[_OptionCurrent]:
    """Parse snapshot row into lightweight option data."""
    try:
        return _OptionCurrent(row)
    except Exception:
        return None


def _print_stock_row(ticker, qty, cost, price, mv, pl_pct, score, cc, decision):
    print(f"  {ticker:<8s} {qty:>6,.0f} ${price:>9,.2f} ${cost:>9,.2f} "
          f"${mv:>11,.2f} {pl_pct:>+7.1f}% {score:>6s} {decision:<36s}")


def _print_opt_row(code, qty, dte, delta, cost, bid, pl, pl_pct, profit, score, decision):
    d_str = f'{delta:+.3f}' if isinstance(delta, (int, float)) else str(delta)[:7]
    dte_str = f'{dte:>4d}' if isinstance(dte, (int, float)) else str(dte)[:4]
    pl_str = f'${pl:>9,.2f}' if isinstance(pl, (int, float)) else str(pl)[:10]
    pct_str = f'{pl_pct:>+6.1f}%' if isinstance(pl_pct, (int, float)) else str(pl_pct)[:7]
    prof_str = f'{profit:>7.1f}%' if isinstance(profit, (int, float)) else str(profit)[:8]
    print(f"  {code:<24s} {qty:>5,.0f} {dte_str:>4s} {d_str:>7s} "
          f"${cost:>7,.2f} ${bid:>7,.2f} {pl_str:>10s} {pct_str:>7s} "
          f"{prof_str:>8s} {score:>6s} {decision:<24s}")


if __name__ == '__main__':
    main()
