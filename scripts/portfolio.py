#!/usr/bin/env python3
"""Portfolio — single umbrella for REAL-account state, P&L, health, thesis, and
the systematic review timeline. Absorbs the former comprehensive_analysis.py.

Bare run is a full sweep: funds → P&L → health+guardrails → timeline →
do-not-wheel → thesis validation → recommendations.

Usage:
    python3 scripts/portfolio.py             # full sweep (all sections)
    python3 scripts/portfolio.py --fast      # funds + P&L only (no scoring/thesis)
    python3 scripts/portfolio.py --health    # decisions + overlap + guardrails only
    python3 scripts/portfolio.py --thesis    # thesis validation on all holdings
    python3 scripts/portfolio.py --schedule  # systematic timeline only
    python3 scripts/portfolio.py --dnl       # do-not-wheel list only
    python3 scripts/portfolio.py --funds     # account funds only
    python3 scripts/portfolio.py --pnl       # positions + P&L + income
    python3 scripts/portfolio.py --orders    # order history for current positions
    python3 scripts/portfolio.py --orders AMD  # order history for specific ticker
    python3 scripts/portfolio.py --no-external   # skip yfinance (skips thesis deep-checks)
"""
import argparse
import os
import sys
import warnings
from datetime import date

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from moomoo import RET_OK

from src.logging_setup import get_logger
log = get_logger('portfolio')

from src.data.portfolio_loader import fetch_portfolio_and_orders
from src.data.moomoo_client import MoomooClient
from src.data.yfinance_client import YFinanceClient
from src.data.compute import enrich_stock_snapshot
from src.data.guardrails import GuardrailChecker, SECTOR_MAP
from src.guardrails.limits import GuardrailChecker as StagedGuardrails
from src.config import get_config
from src.risk.holdings_exit import evaluate_holding_exit, sma_slope, months_to_recover
from src.risk.overlap import analyze_overlap
from src.analysis.thesis import evaluate_thesis, fetch_thesis_inputs
from src.analysis.thesis_validator import validate_investment_thesis, ThesisStatus
from src.data.do_not_wheel_list import DoNotWheelList
from src.system.scheduler import (
    get_scheduled_action_type, get_system_status, should_allow_trading_decisions,
)
from src.scoring.holding_score import (
    _score_holding, _find_best_cc, _score_option, _parse_snapshot_row,
)
from src.portfolio.summary import (
    compute_income, compute_sector_breakdown,
    unrealized_stock_pl, unrealized_option_pl, stock_market_value,
)


def _resolve_sections(args) -> set:
    """Decide which sections to print.

    --fast           → funds + pnl only.
    any selector set → only those sections.
    bare run         → full sweep (orders stays opt-in via --orders).
    """
    if args.fast:
        return {'funds', 'pnl'}
    explicit = set()
    if args.funds:    explicit.add('funds')
    if args.pnl:      explicit.add('pnl')
    if args.health:   explicit.add('health')
    if args.orders:   explicit.add('orders')
    if args.thesis:   explicit.add('thesis')
    if args.schedule: explicit.add('timeline')
    if args.dnl:      explicit.add('dnl')
    if explicit:
        return explicit
    return {'funds', 'pnl', 'health', 'timeline', 'dnl', 'thesis', 'recommendations'}


def main():
    parser = argparse.ArgumentParser(description='Portfolio — state, P&L, funds, health')
    parser.add_argument('--fast', action='store_true', help='Funds + P&L only (no scoring)')
    parser.add_argument('--health', action='store_true', help='Decisions + overlap + guardrails')
    parser.add_argument('--funds', action='store_true', help='Account funds only')
    parser.add_argument('--pnl', action='store_true', help='Positions + P&L + income')
    parser.add_argument('--orders', nargs='?', const='ALL', help='Order history (optional: filter by ticker)')
    parser.add_argument('--thesis', action='store_true', help='Thesis validation on all holdings')
    parser.add_argument('--schedule', action='store_true', help='Systematic timeline / review schedule')
    parser.add_argument('--dnl', action='store_true', help='Do-Not-Wheel exclusion list')
    parser.add_argument('--no-external', action='store_true', help='Skip yfinance (offline)')
    args = parser.parse_args()

    sections = _resolve_sections(args)
    today = date.today()

    print("📋 Loading portfolio from moomoo...", end=' ', flush=True)
    pf, orders = fetch_portfolio_and_orders()
    if not pf.stocks and not pf.options and pf.funds.cash == 0 and pf.funds.fund == 0:
        print("\n❌ Cannot connect to moomoo OpenD (127.0.0.1:11111) or no REAL account. Aborting.")
        return
    print(f"{len(pf.stocks)} stocks, {len(pf.options)} options, "
          f"${pf.funds.liquid:,.0f} liquid")

    # ── Macro regime (only needed for health/scoring) ──
    regime, regime_mult = 'NEUTRAL', 1.0
    if 'health' in sections and not args.no_external:
        try:
            from src.analysis.sentiment import get_macro_context
            yf_macro = YFinanceClient()
            macro = get_macro_context(yf_macro)
            regime = macro.market_regime
            regime_mult = macro.position_mult
            print(f"🌍 VIX {macro.vix or 'N/A'} | {regime} | Size: {regime_mult:.0%}")
        except Exception:
            pass
    print()

    nlv = pf.net_liquidation

    # Single yfinance client shared by health scoring + thesis validation.
    yf_client = None
    if not args.no_external:
        try:
            yf_client = YFinanceClient()
        except Exception:
            yf_client = None

    # Recommendations run on the full sweep, or whenever both health + thesis run.
    run_recommendations = ('recommendations' in sections) or ({'health', 'thesis'} <= sections)

    # ── FUNDS ──
    if 'funds' in sections:
        _print_funds(pf)

    # ── P&L (positions + income + sectors) ──
    if 'pnl' in sections:
        _print_pnl(pf, orders, nlv, today)

    # ── ORDERS (order history) ──
    if 'orders' in sections:
        _print_orders(args.orders, pf)

    # ── HEALTH (decisions + overlap + guardrails) ──
    violations = []
    if 'health' in sections:
        violations = _print_health(pf, orders, yf_client, regime, regime_mult, today, nlv)

    # ── TIMELINE (systematic review schedule) ──
    if 'timeline' in sections:
        _print_timeline(pf, nlv)

    # ── DO-NOT-WHEEL ──
    if 'dnl' in sections:
        _print_do_not_wheel()

    # ── THESIS VALIDATION ──
    thesis_results = {}
    if 'thesis' in sections:
        thesis_results = _print_thesis(pf, yf_client)

    # ── RECOMMENDATIONS ──
    if run_recommendations:
        stage = _determine_recovery_stage(pf, nlv)
        trading_allowed = should_allow_trading_decisions()
        _print_recommendations(pf, orders, nlv, thesis_results, violations,
                               stage, trading_allowed)

    log.info(f"PORTFOLIO|regime={regime}|stocks={len(pf.stocks)}|options={len(pf.options)}|"
             f"nlv=${nlv:,.0f}|liquid=${pf.funds.liquid:,.0f}|sections={','.join(sorted(sections))}")


# ════════════════════════════════════════════════════════════════
# FUNDS
# ════════════════════════════════════════════════════════════════

def _print_funds(pf):
    f = pf.funds
    print(f"{'='*90}")
    print(f"  💰 ACCOUNT FUNDS")
    print(f"{'='*90}")
    print(f"  US Cash:            ${f.cash:>14,.2f}")
    print(f"  Fund Assets:        ${f.fund:>14,.2f}")
    print(f"  Liquid (cash+fund): ${f.liquid:>14,.2f}")
    print(f"  Buying Power:       ${f.buying_power:>14,.2f}")
    print(f"  Total Assets:       ${f.total_assets:>14,.2f}")
    print(f"  Total Liabilities:  ${f.total_liabilities:>14,.2f}")
    print(f"  Net Assets:         ${f.net_assets:>14,.2f}")
    print(f"  Margin Used:        {f.margin_used_pct:>13.1f}%")
    print(f"  Net Liquidation:    ${pf.net_liquidation:>14,.2f}")
    print(f"  CSP Liability:      ${pf.csp_liability:>14,.0f}  (cash needed if all puts assign)")
    print()


# ════════════════════════════════════════════════════════════════
# P&L  (positions + all-time income + sectors)
# ════════════════════════════════════════════════════════════════

def _print_pnl(pf, orders, nlv, today):
    # ── Stock positions ──
    if pf.stocks:
        print(f"{'='*90}")
        print(f"  📈 STOCK POSITIONS ({len(pf.stocks)})")
        print(f"{'='*90}")
        print(f"  {'Ticker':<8s} {'Qty':>7s} {'Price':>10s} {'Cost':>10s} "
              f"{'MktVal':>13s} {'P&L':>11s} {'P&L%':>8s}")
        print(f"  {'-'*8} {'-'*7} {'-'*10} {'-'*10} {'-'*13} {'-'*11} {'-'*8}")
        for ticker in sorted(pf.stocks):
            s = pf.stocks[ticker]
            pl_pct = (s['pl'] / (s['cost'] * s['qty']) * 100) if s['cost'] and s['qty'] else 0
            print(f"  {ticker:<8s} {s['qty']:>7,.0f} ${s['price']:>9,.2f} ${s['cost']:>9,.2f} "
                  f"${s['mv']:>12,.2f} ${s['pl']:>10,.0f} {pl_pct:>+7.1f}%")
        print(f"  {'-'*8} {'-'*7} {'-'*10} {'-'*10} {stock_market_value(pf.stocks):>13,.2f} "
              f"${unrealized_stock_pl(pf.stocks):>10,.0f}")
        print()

    # ── Option positions ──
    if pf.options:
        print(f"{'='*90}")
        print(f"  📊 OPTION POSITIONS ({len(pf.options)})")
        print(f"{'='*90}")
        print(f"  {'Code':<26s} {'Qty':>5s} {'DTE':>4s} {'Strike':>8s} "
              f"{'Cost':>8s} {'P&L':>10s} {'P&L%':>8s} {'Assign$':>10s}")
        print(f"  {'-'*26} {'-'*5} {'-'*4} {'-'*8} {'-'*8} {'-'*10} {'-'*8} {'-'*10}")
        for code in sorted(pf.options):
            o = pf.options[code]
            try:
                dte = (date.fromisoformat(o['expiry']) - today).days
            except Exception:
                dte = 0
            assign = o['strike'] * abs(o['qty']) * 100 if o['type'] == 'PUT' else 0
            print(f"  {code:<26s} {o['qty']:>5,.0f} {dte:>4d} ${o['strike']:>7,.0f} "
                  f"${o['cost']:>7,.2f} ${o['pl']:>9,.0f} {o['pl_pct']:>+7.1f}% ${assign:>9,.0f}")
        print(f"  {'-'*26} {'-'*5} {'-'*4} {'-'*8} {'-'*8} ${unrealized_option_pl(pf.options):>9,.0f}")
        print()

    # ── All-time income + monthly ──
    income = compute_income(orders)
    print(f"{'='*90}")
    print(f"  💵 ALL-TIME OPTION INCOME")
    print(f"{'='*90}")
    print(f"  Premium Collected: ${income.premium_collected:>12,.0f}")
    print(f"  Premium Paid:      ${income.premium_paid:>12,.0f}")
    print(f"  NET OPTION INCOME: ${income.net_option_income:>12,.0f}")
    print(f"  Stock Bought:      ${income.stock_bought:>12,.0f}")
    print(f"  Stock Sold:        ${income.stock_sold:>12,.0f}")
    print(f"  Filled Orders:     {income.filled_order_count:>12d}")
    print(f"  Unrealized Stock P&L:  ${unrealized_stock_pl(pf.stocks):>12,.0f}")
    print(f"  Unrealized Option P&L: ${unrealized_option_pl(pf.options):>12,.0f}")

    if income.monthly:
        print(f"\n  📅 MONTHLY OPTION INCOME")
        print(f"  {'-'*55}")
        print(f"  {'Month':<10s} {'Collected':>14s} {'Buybacks':>12s} {'Net':>12s}")
        print(f"  {'-'*55}")
        for m in sorted(income.monthly):
            b = income.monthly[m]
            print(f"  {m:<10s} ${b['collected']:>13,.0f} ${b['buyback']:>11,.0f} "
                  f"${b['collected'] - b['buyback']:>11,.0f}")
    print()

    # ── Sector concentration ──
    sectors = compute_sector_breakdown(pf.stocks)
    if sectors:
        print(f"{'='*90}")
        print(f"  🏛️  SECTOR BREAKDOWN")
        print(f"{'='*90}")
        for sec in sorted(sectors, key=sectors.get, reverse=True):
            pct = (sectors[sec] / nlv * 100) if nlv > 0 else 0
            bar = '█' * int(pct / 2)
            print(f"  {sec:<20s} ${sectors[sec]:>13,.0f}  {pct:>5.1f}%  {bar}")
    print()


# ════════════════════════════════════════════════════════════════
# ORDERS  (order history)
# ════════════════════════════════════════════════════════════════

def _print_orders(ticker_filter, pf):
    """Fetch and display order history from moomoo."""
    from moomoo import OpenSecTradeContext, TrdEnv
    from datetime import datetime, timedelta
    import re

    def parse_option_code(code):
        """Parse option code into readable format."""
        parts = re.match(r"US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)", code)
        if parts:
            ticker, yr, mo, dy, opt_type, strike_val = parts.groups()
            expiry = f"20{yr}-{mo}-{dy}"
            strike = float(strike_val) / 1000
            return ticker, expiry, opt_type, strike
        return None, None, None, None

    print(f"{'='*90}")
    print(f"  📋 ORDER HISTORY")
    print(f"{'='*90}")

    if ticker_filter != 'ALL':
        print(f"  🔍 Filtering by ticker: {ticker_filter}")
        print()

    try:
        trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)

        # Get account list
        ret, acc_list = trd.get_acc_list()

        if ret != RET_OK:
            print("  ❌ Failed to connect to moomoo")
            trd.close()
            return

        for _, acc in acc_list.iterrows():
            if str(acc.get('trd_env', '')) == 'SIMULATE':
                continue

            acc_id = acc['acc_id']

            # Calculate date range for last 90 days
            end_date = datetime.now()
            start_date = end_date - timedelta(days=90)

            ret1, hist = trd.history_order_list_query(
                trd_env=TrdEnv.REAL, acc_id=acc_id,
                start=start_date.strftime('%Y-%m-%d'),
                end=end_date.strftime('%Y-%m-%d')
            )

            if ret1 == RET_OK and hist is not None:
                # Filter orders based on ticker_filter
                if ticker_filter == 'ALL':
                    # Show all option orders
                    filtered_orders = hist[hist['code'].str.contains(r'\d{6}[CP]\d+', na=False)]
                    print(f"  Showing all option orders (last 90 days, filled only)")
                else:
                    # Filter by specific ticker
                    filtered_orders = hist[hist['code'].str.contains(ticker_filter, na=False)]
                    print(f"  Showing {ticker_filter} option orders (last 90 days, filled only)")

                # Skip cancelled orders
                filtered_orders = filtered_orders[~filtered_orders['order_status'].str.contains('CANCELLED', na=False)]

                if len(filtered_orders) == 0:
                    print(f"  ⚠️  No filled orders found for '{ticker_filter}'")
                else:
                    print(f"  Found {len(filtered_orders)} filled orders:")
                    print()

                    # Sort by create time (newest first)
                    filtered_orders = filtered_orders.sort_values('create_time', ascending=False)

                    print(f"  {'Time':<20s} {'Action':<15s} {'Details':<35s} {'Qty':>6s} {'Price':>10s} {'Total $':>12s}")
                    print(f"  {'-'*20} {'-'*15} {'-'*35} {'-'*6} {'-'*10} {'-'*12}")

                    total_premium_received = 0
                    total_premium_paid = 0
                    total_trades = 0

                    for idx, order in filtered_orders.iterrows():
                        code = str(order.get('code', ''))
                        create_time = str(order.get('create_time', ''))[:19]
                        side = str(order.get('trd_side', ''))
                        qty = float(order.get('qty', 0) or 0)
                        price = float(order.get('dealt_avg_price', 0) or order.get('price', 0) or 0)

                        # Parse option code
                        ticker, expiry, opt_type, strike = parse_option_code(code)

                        # Calculate total transaction amount
                        total_amount = abs(qty) * price * 100  # Options are 100 shares per contract

                        # Format action description
                        if side in ('SELL', 'SELL_SHORT'):
                            action = "💰 SOLD"
                            total_premium_received += total_amount
                            if opt_type == 'P':
                                details = f"{ticker} PUT ${strike:.0f} {expiry}"
                            else:
                                details = f"{ticker} CALL ${strike:.0f} {expiry}"
                        elif side in ('BUY', 'BUY_BACK'):
                            action = "🔴 BOUGHT"
                            total_premium_paid += total_amount
                            if opt_type == 'P':
                                details = f"{ticker} PUT ${strike:.0f} {expiry}"
                            else:
                                details = f"{ticker} CALL ${strike:.0f} {expiry}"
                        else:
                            action = f"❓ {side}"
                            details = code

                        total_trades += 1
                        print(f"  {create_time:<20s} {action:<15s} {details:<35s} {abs(qty):>6.0f} ${price:>9.2f} ${total_amount:>11,.2f}")

                    print()
                    print(f"  💰 TOTALS (Last 90 Days):")
                    print(f"     Premium Received:  ${total_premium_received:>12,.2f}")
                    print(f"     Premium Paid:      ${total_premium_paid:>12,.2f}")
                    print(f"     Net Income:        ${total_premium_received - total_premium_paid:>12,.2f}")
                    print(f"     Total Trades:      {total_trades:>12}")

            break  # First REAL account only

        trd.close()

        print()
        print("  💡 Usage: --orders AMD (filter by ticker) | --orders (show all)")

    except Exception as e:
        print(f"  ❌ Error fetching order history: {e}")
        import traceback
        traceback.print_exc()

    print()


# ════════════════════════════════════════════════════════════════
# HEALTH  (decisions + overlap + guardrails)
# ════════════════════════════════════════════════════════════════

def _print_health(pf, orders, yf_client, regime, regime_mult, today, nlv):
    cfg = get_config()

    print(f"  💰 Liquid: ${pf.funds.liquid:,.0f} (cash ${pf.funds.cash:,.0f} + "
          f"fund ${pf.funds.fund:,.0f}) | BP: ${pf.funds.buying_power:,.0f} | "
          f"{len(pf.stocks)} stocks, {len(pf.options)} options\n")

    with MoomooClient() as moomoo:
        # ── Stock holdings: score + CC hunt + exit framework ──
        if pf.stocks:
            _score_holdings(pf, moomoo, yf_client, cfg, regime, regime_mult, today)

        # ── Batch option snapshots (used by decisions + overlap) ──
        snap_map = _fetch_option_snapshots(moomoo, list(pf.options.keys()))

        # ── Option decisions ──
        if pf.options:
            _score_options(pf, snap_map, yf_client, today)

        # ── Put/call overlap ──
        reports = analyze_overlap(pf.options, pf.stocks, snapshots=snap_map, today=today)
        if reports:
            _print_overlap(reports, today)

    # ── Guardrails ──
    return _print_guardrails(pf, orders, nlv)


def _score_holdings(pf, moomoo, yf_client, cfg, regime, regime_mult, today):
    rows = []
    for ticker, pos in pf.stocks.items():
        qty, cost = pos['qty'], pos['cost']
        snap = moomoo.get_stock_snapshot(f'US.{ticker}')
        if snap is None or snap.last_price <= 0:
            rows.append((ticker, qty, cost, 0, 0, 'N/A', 'N/A', '⚠️  NO DATA', False))
            continue
        price, mv = snap.last_price, qty * snap.last_price
        pl_pct = ((price - cost) / cost * 100) if cost > 0 else 0
        history = moomoo.get_price_history(f'US.{ticker}', 252)
        if history:
            enrich_stock_snapshot(snap, history)
        score = _score_holding(snap, ticker, yf_client, regime, regime_mult)

        exit_rep = evaluate_holding_exit(
            ticker, price, cost, snap.sma_200,
            sma_slope([b['close'] for b in history]) if history else None, None,
            dead_zone_pct=cfg.holdings_exit('dead_zone_drop_pct', 0.15),
            conditional_pct=cfg.holdings_exit('backstop_conditional_pct', 0.30),
            hard_pct=cfg.holdings_exit('backstop_hard_pct', 0.40),
            months_to_recover_flag=cfg.holdings_exit('months_to_recover_flag', 12),
        )

        if exit_rep.decision in ('CIRCUIT_BREAKER', 'BACKSTOP_EXIT'):
            rows.append((ticker, qty, cost, price, mv, pl_pct, 10.0,
                         f"🛑 EXIT — {exit_rep.reasons[0]}", True))
        elif exit_rep.decision == 'DEAD_ZONE':
            thesis = evaluate_thesis(ticker, fetch_thesis_inputs(ticker),
                stall_quarters=cfg.thesis_gate('growth_stall_quarters', 2),
                decel_quarters=cfg.thesis_gate('decel_quarters', 3),
                gross_margin_drop_bps=cfg.thesis_gate('gross_margin_drop_bps', 100),
                debt_ebitda_max=cfg.thesis_gate('debt_ebitda_max', 4.5),
                fcf_yield_min_pct=cfg.thesis_gate('fcf_yield_min_pct', 2.0),
                broken_min_gates=cfg.thesis_gate('broken_min_gates', 2),
            ) if yf_client else None
            if thesis and thesis.broken:
                rows.append((ticker, qty, cost, price, mv, pl_pct, 9.0,
                             f"🛑 EXIT — thesis broken ({', '.join(thesis.reasons)})", True))
            else:
                rows.append((ticker, qty, cost, price, mv, pl_pct, score,
                             f"⚖️  DEAD ZONE {exit_rep.drop_pct:.0%}↓ — hold (thesis check)", False))
        else:
            best_cc = _find_best_cc(moomoo, ticker, snap, qty, cost, yf_client, regime, regime_mult)
            if best_cc:
                rows.append((ticker, qty, cost, price, mv, pl_pct, score,
                             f"SELL CC ${best_cc['strike']:.0f} {best_cc['expiry']} @ {best_cc['roc']:.1f}%",
                             True))
            elif qty < 100:
                rows.append((ticker, qty, cost, price, mv, pl_pct, score, "HOLD (<100 shares)", False))
            else:
                rows.append((ticker, qty, cost, price, mv, pl_pct, score, "HOLD (no suitable CC)", False))

    rows.sort(key=lambda r: (not r[8], r[0]))
    print(f"  📋 STOCK HOLDINGS:")
    print(f"  {'Ticker':<8s} {'Qty':>7s} {'Price':>10s} {'Cost':>10s} {'MktVal':>13s} "
          f"{'P&L%':>8s} {'Score':>6s} {'Decision'}")
    print(f"  {'-'*8} {'-'*7} {'-'*10} {'-'*10} {'-'*13} {'-'*8} {'-'*6} {'-'*40}")
    for ticker, qty, cost, price, mv, pl_pct, score, dec, _ in rows:
        sscore = f'{score:.1f}' if isinstance(score, float) else score
        print(f"  {ticker:<8s} {qty:>7,.0f} ${price:>9,.2f} ${cost:>9,.2f} "
              f"${mv:>12,.2f} {pl_pct:>+7.1f}% {sscore:>6s} {dec}")
    print()


def _fetch_option_snapshots(moomoo, codes):
    """Batch-fetch raw snapshot rows for held option codes → {code: row}."""
    snap_map = {}
    for i in range(0, len(codes), 400):
        batch = codes[i:i + 400]
        ret, data = moomoo.ctx.get_market_snapshot(batch)
        if ret == RET_OK and data is not None:
            for _, row in data.iterrows():
                code = row.get('code', '')
                if code:
                    snap_map[code] = row
    return snap_map


def _score_options(pf, snap_map, yf_client, today):
    print(f"  📊 OPTION DECISIONS:")
    print(f"  {'Code':<26s} {'Qty':>5s} {'DTE':>4s} {'Δ':>7s} {'Bid':>7s} "
          f"{'Capt%':>7s} {'Score':>6s} {'Decision'}")
    print(f"  {'-'*26} {'-'*5} {'-'*4} {'-'*7} {'-'*7} {'-'*7} {'-'*6} {'-'*30}")
    for code, pos in sorted(pf.options.items()):
        row = snap_map.get(code)
        current = _parse_snapshot_row(row) if row is not None else None
        try:
            dte = (date.fromisoformat(pos['expiry']) - today).days
        except Exception:
            dte = 0
        if current is None:
            print(f"  {code:<26s} {pos['qty']:>5,.0f} {dte:>4d} {'':>7s} {'':>7s} "
                  f"{'':>7s} {'N/A':>6s} ⚠️  NO CHAIN DATA")
            continue
        bid = current.bid or 0
        profit_captured = ((pos['cost'] - bid) / pos['cost'] * 100) if pos['cost'] > 0 else 0
        score, dec = _score_option(pos, current, profit_captured, pos.get('pl', 0), today, yf_client)
        print(f"  {code:<26s} {pos['qty']:>5,.0f} {dte:>4d} {current.delta:>+6.3f} "
              f"${bid:>6,.2f} {profit_captured:>+6.1f}% {score:>5.1f}  {dec}")
    print()


def _print_overlap(reports, today):
    print(f"  🔀 PUT/CALL OVERLAP — {len(reports)} ticker(s) with both sides")
    for r in reports:
        print(f"\n     {r.ticker} — {r.shares} shares, "
              f"{len(r.calls)} short call(s) [owe {r.call_shares}], "
              f"{len(r.puts)} short put(s) [may buy {r.put_shares}, need ${r.total_put_assign:,.0f}]")
        for s in r.straddles:
            print(f"        ⚡ STRADDLE @ ${s.strike:.0f} {s.expiry} (DTE {s.dte}) "
                  f"prem ${s.premium:,.0f}, breakevens ${s.breakeven_low:.0f}–${s.breakeven_high:.0f}")
        for g in r.strangles:
            cs = ', '.join(f"${x:.0f}" for x in g.call_strikes)
            ps = ', '.join(f"${x:.0f}" for x in g.put_strikes)
            print(f"        🔗 STRANGLE @ {g.expiry}: calls {cs} + puts {ps}")
        print(f"        📐 IF ALL CALLS: {r.net_if_calls} shares left | "
              f"IF ALL PUTS: {r.net_if_puts} shares (+${r.total_put_assign:,.0f}) | "
              f"IF ALL: {r.net_if_all} shares")
        for step in r.stacked_calls:
            print(f"        ⚠️  {step.expiry} (DTE {step.dte}): +{step.shares_called} called → "
                  f"{step.shares_remaining} left (cumulative {step.cumulative_called})")
    print()


def _compute_staged_guardrails(pf, orders, nlv):
    """Staged (recovery) guardrail view via src/guardrails/limits.py.
    Shared by _print_guardrails and _print_recommendations so stage/violations
    are computed once. Returns (stage, violations, summary_dict)."""
    positions_dict = {
        ticker: {'market_value': pos.get('mv', 0), 'sector': SECTOR_MAP.get(ticker, 'Other')}
        for ticker, pos in pf.stocks.items()
    }
    checker = StagedGuardrails(
        net_liquidation=nlv,
        cash=pf.funds.liquid,
        buying_power=pf.funds.buying_power,
        open_positions=len(pf.options),
        monthly_orders=_filled_orders_this_month(orders),
        csp_liability=pf.csp_liability,
    )
    violations = checker.check_all_guardrails(positions_dict)
    return checker.get_current_stage(), violations, checker.get_summary()


def _determine_recovery_stage(pf, nlv) -> str:
    """Coarse recovery stage for recommendations (EMERGENCY/TARGET/COMFORT)."""
    cash_buffer_pct = pf.funds.liquid / nlv if nlv > 0 else 0
    csp_deployment_pct = pf.csp_liability / nlv if nlv > 0 else 0
    if cash_buffer_pct < 0.10 or csp_deployment_pct > 0.50:
        return "EMERGENCY"
    if cash_buffer_pct < 0.15 or csp_deployment_pct > 0.35:
        return "TARGET"
    return "COMFORT"


def _filled_orders_this_month(orders) -> int:
    """Filled orders in the current calendar month.

    The order history spans years; feeding the all-time count into the monthly
    guardrail produces a false BLOCK. This buckets by the order's fill date.
    """
    ym = date.today().strftime('%Y-%m')
    return sum(1 for o in orders
               if o.get('status') in ('FILLED_ALL', 'FILLED_PART')
               and str(o.get('date', '') or '').startswith(ym))


def _print_guardrails(pf, orders, nlv):
    # Build position list: only stocks with active options for wheel strategy
    tickers_with_options = {o['ticker'] for o in pf.options.values()}
    gc_positions = []
    for ticker, pos in pf.stocks.items():
        csp_liability = sum(
            abs(o['strike']) * abs(o['qty']) * 100
            for o in pf.options.values()
            if o['ticker'] == ticker and o['type'] == 'PUT'
        )
        # Only include positions with active options for wheel management
        if ticker in tickers_with_options:
            gc_positions.append({
                'ticker': ticker, 'notional': pos.get('mv', 0),
                'sector': SECTOR_MAP.get(ticker, 'Unknown'), 'csp_liability': csp_liability,
            })
    gc = GuardrailChecker(net_liq=nlv, cash=pf.funds.liquid, buying_power=pf.funds.buying_power,
                          open_positions=gc_positions)
    gr = gc.check()
    # Override position count to use option contracts (not tickers)
    gr.open_positions = len(pf.options)

    print(f"{'='*90}")
    print(f"  🛡️  GUARDRAILS")
    print(f"{'='*90}")
    print(f"  Net Liq: ${nlv:,.0f} | Liquid: ${pf.funds.liquid:,.0f} ({gr.cash_buffer_pct:.0f}%) | "
          f"Option Positions: {gr.open_positions} (max {GuardrailChecker.MAX_OPEN_POSITIONS()})")
    print(f"  Max single: {gr.max_single_position_pct:.0f}% (limit 15%) | "
          f"Max sector: {gr.max_sector_pct:.0f}% (limit 25%)")
    if gr.worst_case_shortfall > 0:
        print(f"  ⚠️  CSP liability ${gr.worst_case_assignment:,.0f} — shortfall ${gr.worst_case_shortfall:,.0f}")
    else:
        print(f"  ✅ All CSPs covered — ${gr.worst_case_assignment:,.0f} liability")
    for b in gr.blocks:
        print(f"  🔴 BLOCK: {b}")
    for w in gr.warnings:
        print(f"  🟡 WARN: {w}")
    if gr.all_clear and not gr.warnings:
        print("  ✅ All within limits")

    # ── Staged recovery view (src/guardrails/limits.py) ──
    stage, violations, summary = _compute_staged_guardrails(pf, orders, nlv)
    print(f"\n  📐 STAGED RECOVERY — {stage}")
    print(f"     Cash buffer: {summary['cash_buffer_pct']:.1%} | "
          f"CSP deployment: {summary['csp_deployment_pct']:.1%} "
          f"(limit {summary['limits']['csp']:.0%})")
    print(f"     Position cap: {summary['limits']['position']:.0%} | "
          f"Sector cap: {summary['limits']['sector']:.0%} | "
          f"Positions: {summary['limits']['position_count']} | "
          f"Monthly orders: {summary['limits']['monthly_orders']}")
    for v in violations:
        emoji = "🚨" if v.severity == "CRITICAL" else "🔴" if v.severity == "BLOCK" else "🟡"
        print(f"     {emoji} {v.message}")
    print()
    return violations


# ════════════════════════════════════════════════════════════════
# TIMELINE  (systematic review schedule — pure, no network)
# ════════════════════════════════════════════════════════════════

def _print_timeline(pf, nlv):
    current_review = get_scheduled_action_type()
    trading_allowed = should_allow_trading_decisions()
    stage = _determine_recovery_stage(pf, nlv)
    status = get_system_status()
    print(f"{'='*90}")
    print(f"  📅 SYSTEMATIC TIMELINE")
    print(f"{'='*90}")
    print(f"  Current Review:    {current_review.value}")
    print(f"  Trading Decisions: {'✅ ALLOWED' if trading_allowed else '❌ READ-ONLY'}")
    print(f"  Recovery Stage:    {stage}")
    print(f"  Next scheduled reviews:")
    for name, when in status['next_reviews'].items():
        print(f"    {name:<20s} {when[:19]}")
    print()


# ════════════════════════════════════════════════════════════════
# DO-NOT-WHEEL  (persistent exclusion list — file I/O only)
# ════════════════════════════════════════════════════════════════

def _print_do_not_wheel():
    dnl = DoNotWheelList()
    exclusions = dnl.get_all_exclusions()
    print(f"{'='*90}")
    print(f"  🚫 DO-NOT-WHEEL LIST ({len(exclusions)} active)")
    print(f"{'='*90}")
    if not exclusions:
        print("  (none)")
    for e in exclusions:
        print(f"  ❌ {e.ticker:<6s} until {e.expiration_date} ({e.months} months) — {e.reason}")
    print()


# ════════════════════════════════════════════════════════════════
# THESIS VALIDATION  (deep checks need yfinance; skipped under --no-external)
# ════════════════════════════════════════════════════════════════

def _print_thesis(pf, yf_client) -> dict:
    """Validate the investment thesis for every holding. Returns {ticker: report}.
    Auto-adds BROKEN tickers to the Do-Not-Wheel list (6 months)."""
    print(f"{'='*90}")
    print(f"  🔍 THESIS VALIDATION ({len(pf.stocks)} holdings)")
    print(f"{'='*90}")
    if yf_client is None:
        print("  ⚠️  Skipped (--no-external): deep thesis checks require yfinance.\n")
        return {}
    if not pf.stocks:
        print("  (no stock holdings)\n")
        return {}

    results = {}
    dnl = DoNotWheelList()
    with MoomooClient() as moomoo:
        for ticker in sorted(pf.stocks):
            try:
                snap = moomoo.get_stock_snapshot(f'US.{ticker}')
                report = validate_investment_thesis(
                    ticker=ticker, entry_date=None, entry_thesis={},
                    current_snapshot=snap, yf_client=yf_client)
                results[ticker] = report
                status = report.status
                emoji = ("✅" if status == ThesisStatus.INTACT
                         else "⚠️" if status == ThesisStatus.DAMAGED else "🚨")
                print(f"  {emoji} {ticker:<6s} {status.value}")
                for chk in report.checks:
                    if chk.severity == "CRITICAL":
                        print(f"      🚨 {chk.message}")
                    elif chk.severity == "WARNING":
                        print(f"      ⚠️  {chk.message}")
                if status in (ThesisStatus.BROKEN, ThesisStatus.DAMAGED):
                    print(f"      → {report.recommended_action}")
                # Auto-add broken-thesis tickers to Do-Not-Wheel
                if status == ThesisStatus.BROKEN:
                    reason = ("; ".join(c.message for c in report.checks
                                        if c.severity == "CRITICAL") or "Thesis broken")
                    dnl.add(ticker, months=6, reason=reason)
                    print(f"      🔧 Added {ticker} to Do-Not-Wheel list (6 months)")
            except Exception as e:
                log.warning(f"Thesis validation error for {ticker}: {e}")
                print(f"  ⚠️  {ticker}: thesis validation error — {e}")

    intact = sum(1 for r in results.values() if r.status == ThesisStatus.INTACT)
    damaged = sum(1 for r in results.values() if r.status == ThesisStatus.DAMAGED)
    broken = sum(1 for r in results.values() if r.status == ThesisStatus.BROKEN)
    print(f"\n  📊 SUMMARY: ✅ {intact} intact   ⚠️ {damaged} damaged   🚨 {broken} broken\n")
    return results


# ════════════════════════════════════════════════════════════════
# RECOMMENDATIONS  (derived from live pf state — no hardcoded values)
# ════════════════════════════════════════════════════════════════

def _print_recommendations(pf, orders, nlv, thesis_results, violations, stage, trading_allowed):
    print(f"{'='*90}")
    print(f"  📋 RECOMMENDATIONS")
    print(f"{'='*90}")
    recs = []

    cash = pf.funds.liquid
    csp_liab = pf.csp_liability
    if stage == "EMERGENCY" or csp_liab > cash:
        shortfall = csp_liab - cash
        recs.append(("CRITICAL", "Reduce CSP liability below available cash",
                     f"CSP liability ${csp_liab:,.0f} vs cash ${cash:,.0f} "
                     f"(shortfall ${shortfall:,.0f}). Let puts expire / close the "
                     f"deepest-ITM puts; open no new CSPs until covered."))

    cash_pct = cash / nlv if nlv > 0 else 0
    if cash_pct < 0.15:
        recs.append(("HIGH", f"Build cash buffer to 15%+ (now {cash_pct:.0%})",
                     "Add via CC income on owned shares; pause new CSPs."))

    broken = [t for t, r in thesis_results.items() if r.status == ThesisStatus.BROKEN]
    if broken:
        recs.append(("CRITICAL", f"Exit broken-thesis positions: {', '.join(broken)}",
                     "Close, then add to Do-Not-Wheel list for 6 months."))
    damaged = [t for t, r in thesis_results.items() if r.status == ThesisStatus.DAMAGED]
    if damaged:
        recs.append(("HIGH", f"Monitor damaged-thesis positions: {', '.join(damaged)}",
                     "Weekly review; re-evaluate in 7 days."))

    v_pos = pf.stocks.get('V')
    if v_pos:
        v_pct = (v_pos.get('mv', 0) / nlv) if nlv > 0 else 0
        if v_pct > 0.30:
            recs.append(("MEDIUM", f"Reduce V concentration ({v_pct:.0%}) via CC assignment",
                         "Sell CCs on V; let assignment convert shares to cash for redeployment."))

    monthly = _filled_orders_this_month(orders)
    if monthly > 10:
        recs.append(("HIGH", "Reduce trading frequency to a systematic Wheel",
                     f"{monthly} filled orders this month — target 8–10/month. "
                     f"Use weekly reviews only; let positions expire naturally."))

    if not trading_allowed:
        recs.append(("LOW", "Current window is monitoring-only",
                     "Decisions resume at the next weekly thesis review (Mon 9AM)."))

    if not recs:
        print("  ✅ No actions — portfolio within limits and theses intact.")
    for i, (prio, title, desc) in enumerate(recs, 1):
        emoji = "🚨" if prio == "CRITICAL" else "⚠️" if prio == "HIGH" else "💡"
        print(f"  {i}. {emoji} [{prio}] {title}")
        print(f"     {desc}")
    print()


if __name__ == '__main__':
    main()
