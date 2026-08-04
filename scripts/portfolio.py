#!/usr/bin/env python3
"""Portfolio — single umbrella for REAL-account state, P&L, health, thesis, and
the systematic review timeline. Absorbs the former comprehensive_analysis.py.

Bare run is a full sweep: funds → P&L → health+guardrails → timeline →
thesis validation (with inline do-not-wheel flags) → recommendations.

Usage:
    python3 scripts/portfolio.py             # full sweep (all sections)
    python3 scripts/portfolio.py --fast      # funds + P&L only (no scoring/thesis)
    python3 scripts/portfolio.py --health    # decisions + overlap + guardrails only
    python3 scripts/portfolio.py --thesis    # thesis validation on all holdings
    python3 scripts/portfolio.py --schedule  # systematic timeline only
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

from src.data.portfolio_loader import fetch_portfolio_and_orders, is_option_code
from src.data.moomoo_client import MoomooClient
from src.data.yfinance_client import YFinanceClient
from src.data.compute import enrich_stock_snapshot
from src.data.guardrails import GuardrailChecker, SECTOR_MAP
from src.guardrails.limits import GuardrailChecker as StagedGuardrails
from src.config import get_config
from src.risk.holdings_exit import evaluate_holding_exit, sma_slope, months_to_recover
from src.risk.overlap import analyze_overlap
from src.analysis.profit_management import TrendContext, trend_context_from_snapshot
from src.analysis.thesis import evaluate_thesis, fetch_thesis_inputs
from src.analysis.thesis_validator import validate_investment_thesis, ThesisStatus
from src.data.do_not_wheel_list import DoNotWheelList, is_wheel_eligible
from src.system.scheduler import (
    get_scheduled_action_type, get_system_status, should_allow_trading_decisions,
)
from src.scoring.holding_score import (
    _score_holding, _find_best_cc, _score_option, _parse_snapshot_row,
)
from src.portfolio.summary import (
    compute_income, compute_sector_breakdown,
    unrealized_stock_pl, unrealized_option_pl, stock_market_value,
    order_income_breakdown,
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
    if explicit:
        return explicit
    return {'funds', 'pnl', 'health', 'timeline', 'thesis', 'recommendations'}


def main():
    parser = argparse.ArgumentParser(description='Portfolio — state, P&L, funds, health')
    parser.add_argument('--fast', action='store_true', help='Funds + P&L only (no scoring)')
    parser.add_argument('--health', action='store_true', help='Decisions + overlap + guardrails')
    parser.add_argument('--funds', action='store_true', help='Account funds only')
    parser.add_argument('--pnl', action='store_true', help='Positions + P&L + income')
    parser.add_argument('--orders', nargs='?', const='ALL', help='Order history (optional: filter by ticker)')
    parser.add_argument('--thesis', action='store_true', help='Thesis validation on all holdings')
    parser.add_argument('--schedule', action='store_true', help='Systematic timeline / review schedule')
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
        _print_orders(args.orders, orders)

    # ── HEALTH (decisions + overlap + guardrails) ──
    violations = []
    roll_recs = []
    if 'health' in sections:
        violations, roll_recs = _print_health(pf, orders, yf_client, regime, regime_mult, today, nlv)

    # ── TIMELINE (systematic review schedule) ──
    if 'timeline' in sections:
        _print_timeline(pf, nlv)

    # ── THESIS VALIDATION ──
    thesis_results = {}
    if 'thesis' in sections:
        thesis_results = _print_thesis(pf, yf_client)

    # ── RECOMMENDATIONS ──
    if run_recommendations:
        stage = _determine_recovery_stage(pf, nlv)
        trading_allowed = should_allow_trading_decisions()
        _print_recommendations(pf, orders, nlv, thesis_results, violations,
                               stage, trading_allowed, roll_recs)

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
    print(f"  Buying Power:       ${f.margin_power:>14,.2f}  (margin — matches moomoo app)")
    print(f"  Cash Buying Power:  ${f.buying_power:>14,.2f}  (cash-only, usd_net_cash_power)")
    print(f"  Total Assets:       ${f.total_assets:>14,.2f}")
    print(f"  Margin Used:        {f.margin_used_pct:>13.1f}%")
    print(f"  Net Liquidation:    ${pf.net_liquidation:>14,.2f}")
    print(f"  CSP Liability:      ${pf.csp_liability:>14,.0f}  (cash needed if all puts assign)")
    if f.currency:
        print(f"  Account Currency:   USD  (moomoo reports {f.currency}; all figures USD-converted)")
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
              f"{'Cost':>8s} {'P&L':>10s} {'P&L%':>8s} {'Assign$ (CC+/CSP-)':>18s}")
        print(f"  {'-'*26} {'-'*5} {'-'*4} {'-'*8} {'-'*8} {'-'*10} {'-'*8} {'-'*18}")
        # Assignment cash impact: CSP assign → pay strike (cash OUT, negative);
        # CC assign → receive strike (cash IN, positive). Net shows the balance.
        net_assign = 0.0
        net_csp = 0.0
        net_cc = 0.0
        for code in sorted(pf.options):
            o = pf.options[code]
            try:
                dte = (date.fromisoformat(o['expiry']) - today).days
            except Exception:
                dte = 0
            notional = o['strike'] * abs(o['qty']) * 100
            # CSP: cash spent to buy shares at strike (−); CC: cash received from shares called (+)
            assign = -notional if o['type'] == 'PUT' else notional
            if o['type'] == 'PUT':
                net_csp += assign
            else:
                net_cc += assign
            net_assign += assign
            sign = '+' if assign >= 0 else '-'
            print(f"  {code:<26s} {o['qty']:>5,.0f} {dte:>4d} ${o['strike']:>7,.0f} "
                  f"${o['cost']:>7,.2f} ${o['pl']:>9,.0f} {o['pl_pct']:>+7.1f}% "
                  f"{sign}${abs(assign):>15,.0f}")
        print(f"  {'-'*26} {'-'*5} {'-'*4} {'-'*8} {'-'*8} {'-'*10} {'-'*8} {'-'*18}")
        print(f"  {'':>26s} {'':>5s} {'':>4s} {'':>8s} {'':>8s} ${unrealized_option_pl(pf.options):>9,.0f} "
              f"{'':>8s} {'(unrealized)':>18s}")
        print(f"  {'':>26s} {'':>5s} {'':>4s} {'':>8s} {'':>8s} {'':>10s} "
              f"{'':>8s} {'CSP subtotal':<12s} -${abs(net_csp):>15,.0f}")
        print(f"  {'':>26s} {'':>5s} {'':>4s} {'':>8s} {'':>8s} {'':>10s} "
              f"{'':>8s} {'CC subtotal':<12s} +${net_cc:>15,.0f}")
        net_sign = '+' if net_assign >= 0 else '-'
        print(f"  {'':>26s} {'':>5s} {'':>4s} {'':>8s} {'':>8s} {'':>10s} "
              f"{'':>8s} {'NET assign':<12s} {net_sign}${abs(net_assign):>15,.0f}")
        print(f"  {'':>26s} {'':>5s} {'':>4s} {'':>8s} {'':>8s} {'':>10s} "
              f"{'':>8s} {'':<12s} {'+ net = surplus cash | − net = cash gap':>17s}")
        print()

    # ── All-time income + monthly ──
    income = compute_income(orders)
    option_orders = sum(1 for o in orders
                        if o.get('status') in ('FILLED_ALL', 'FILLED_PART')
                        and is_option_code(o.get('code', '')))
    print(f"{'='*90}")
    print(f"  💵 ALL-TIME OPTION INCOME")
    print(f"{'='*90}")
    print(f"  Premium Collected: ${income.premium_collected:>12,.0f}")
    print(f"  Premium Paid:      ${income.premium_paid:>12,.0f}")
    print(f"  NET OPTION INCOME: ${income.net_option_income:>12,.0f}")
    print(f"  Stock Bought:      ${income.stock_bought:>12,.0f}")
    print(f"  Stock Sold:        ${income.stock_sold:>12,.0f}")
    print(f"  Filled Orders:     {income.filled_order_count:>12d}  "
          f"({option_orders} option · {income.filled_order_count - option_orders} stock, all-time)")
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

def _print_orders(ticker_filter, orders):
    """Display filled order history for the last 90 days.

    Reuses the shared order list already fetched by main() (fetch_orders) and
    the order_income_breakdown helper, so these totals ALWAYS agree with the
    --pnl view (same data, same classification). No separate moomoo connection.
    """
    print(f"{'='*90}")
    print(f"  📋 ORDER HISTORY")
    print(f"{'='*90}")

    flt = ticker_filter if ticker_filter != 'ALL' else None
    if flt:
        print(f"  🔍 Filtering by ticker: {flt}")
        print()

    rows, summary = order_income_breakdown(orders, ticker_filter=flt, days=90)

    # Option orders only in this view (matches the original behaviour).
    opt_rows = [r for r in rows if r.is_option]
    label = f"{flt} option orders" if flt else "all option orders"
    print(f"  Showing {label} (last 90 days, filled only)")

    if not opt_rows:
        print(f"  ⚠️  No filled option orders found{f' for {flt}' if flt else ''}")
        print()
        print("  💡 Usage: --orders AMD (filter by ticker) | --orders (show all)")
        print()
        return

    print(f"  Found {len(opt_rows)} filled orders:")
    print()
    print(f"  {'Date':<12s} {'Action':<15s} {'Details':<35s} {'Qty':>6s} {'Price':>10s} {'Total $':>12s}")
    print(f"  {'-'*12} {'-'*15} {'-'*35} {'-'*6} {'-'*10} {'-'*12}")

    for r in opt_rows:
        action = "💰 SOLD" if r.action == 'SOLD' else "🔴 BOUGHT" if r.action == 'BOUGHT' else f"❓ {r.side}"
        kind = "PUT" if r.opt_type == 'P' else "CALL"
        details = f"{r.ticker} {kind} ${r.strike:.0f} {r.expiry}"
        print(f"  {r.date:<12s} {action:<15s} {details:<35s} {r.qty:>6.0f} ${r.price:>9.2f} ${abs(r.amount):>11,.2f}")

    print()
    print(f"  💰 TOTALS (Last 90 Days):")
    print(f"     Premium Received:  ${summary.premium_collected:>12,.2f}")
    print(f"     Premium Paid:      ${summary.premium_paid:>12,.2f}")
    print(f"     Net Income:        ${summary.net_option_income:>12,.2f}")
    print(f"     Total Trades:      {summary.filled_order_count:>12}")
    print()
    print("  💡 Usage: --orders AMD (filter by ticker) | --orders (show all)")
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

        # ── Trend context per option underlying (for trend-modulated profit booking) ──
        trend_map = _build_trend_map(pf, moomoo, yf_client)

        # ── Option decisions ──
        roll_recs = []
        if pf.options:
            roll_recs = _score_options(pf, snap_map, yf_client, today, trend_map, nlv, pf, orders=orders)

        # ── Put/call overlap ──
        reports = analyze_overlap(pf.options, pf.stocks, snapshots=snap_map, today=today)
        if reports:
            _print_overlap(reports, today)

    # ── Guardrails ──
    violations = _print_guardrails(pf, orders, nlv)
    return violations, roll_recs


def _capital_scarcity(pf, nlv) -> str:
    """Coarse capital-scarity label for the profit-booking gate (SCARCE/NORMAL/ABUNDANT)."""
    if nlv <= 0:
        return 'SCARCE'
    cash_pct = pf.funds.liquid / nlv
    slot_util = len(pf.options) / max(1, get_config().max_open_positions)
    if slot_util < 0.25 and cash_pct >= 0.30:
        return 'ABUNDANT'
    if slot_util < 0.50 and cash_pct >= 0.20:
        return 'NORMAL'
    return 'SCARCE'


def _build_trend_map(pf, moomoo, yf_client) -> dict:
    """{ticker: TrendContext} for every option underlying, enriched once.

    Reuses the shared _trend_composite so the exit layer sees the same 0-100
    number the entry layer scored on. Best-effort: failures → no entry (caller
    falls back to base-50% behavior for that ticker).
    """
    out: dict = {}
    for ticker in pf.option_tickers:
        try:
            snap = moomoo.get_stock_snapshot(f'US.{ticker}')
            if snap is None or snap.last_price <= 0:
                continue
            history = moomoo.get_price_history(f'US.{ticker}', 252)
            if history:
                enrich_stock_snapshot(snap, history)
            sent_score = sent_dir = None
            if yf_client:
                try:
                    ts = yf_client.get_ticker_sentiment(ticker)
                    sent_score = getattr(ts, 'score', None)
                    sent_dir = getattr(ts, 'direction', None)
                except Exception:
                    pass
            out[ticker] = trend_context_from_snapshot(snap, sent_score, sent_dir)
        except Exception as e:
            log.debug(f"trend context build failed for {ticker}: {e}")
    return out


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


def _score_options(pf, snap_map, yf_client, today, trend_map=None, nlv=None, portfolio=None,
                    orders=None):
    trend_map = trend_map or {}
    scarcity = _capital_scarcity(portfolio, nlv) if portfolio and nlv else None
    # CSP redeployment status — feeds the deployment-aware SCARCE bypass in
    # decide_profit_target. When deployment exceeds the limit, freed capital
    # has no CSP slot to redeploy into, so forcing 50% booking is suboptimal.
    # Same csp_liability/nlv ratio computed in _print_guardrails / _compute_staged_guardrails.
    cfg = get_config()
    csp_dep = (pf.csp_liability / nlv) if (nlv and nlv > 0 and pf.csp_liability) else 0.0
    csp_paused = csp_dep > cfg.max_csp_deployed_pct
    roll_recs = []
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
        tctx = trend_map.get(pos.get('ticker'))
        score, dec, _pd = _score_option(pos, current, profit_captured, pos.get('pl', 0), today,
                                        yf_client, trend_ctx=tctx, capital_scarcity=scarcity,
                                        orders=orders, csp_paused=csp_paused)
        print(f"  {code:<26s} {pos['qty']:>5,.0f} {dte:>4d} {current.delta:>+6.3f} "
              f"${bid:>6,.2f} {profit_captured:>+6.1f}% {score:>5.1f}  {dec}")
        # Collect roll-winner recommendations (recommend-only; net-credit + ≤2 rolls gate).
        if 'ROLL' in dec:
            roll_recs.append((pos.get('ticker', ''), dec))
    print()
    return roll_recs


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
    print(f"  Next scheduled review:")
    for name, when in status['next_reviews'].items():
        print(f"    {name:<20s} {when[:19]}")
    print()


# ════════════════════════════════════════════════════════════════
# THESIS VALIDATION  (deep checks need yfinance; skipped under --no-external)
# ════════════════════════════════════════════════════════════════

def _thesis_targets(pf, moomoo=None) -> dict:
    """Build {ticker: source_label} for thesis validation.

    Unifies stocks + option underlyings + the LIVE moomoo watchlist group, so
    every name the engine cares about is thesis-checked. The watchlist is the
    master list — not the static config fallback (which can carry stale names
    like BE/CRM that are no longer watched). Each ticker validated once;
    precedence: stock > option > watchlist.
    """
    targets: dict[str, str] = {}
    for t in pf.option_tickers:
        targets[t] = 'option'
    for t in pf.stocks:
        targets[t] = 'stock'        # stock takes precedence over option
    try:
        if moomoo is not None:
            from src.data.watchlist import fetch_live_watchlist
            live = fetch_live_watchlist(moomoo.ctx)
        else:
            live = get_config().default_watchlist
        for t in live:
            t = str(t).upper().replace('US.', '')
            if t:
                targets.setdefault(t, 'watchlist')
    except Exception:
        pass
    return targets


def _print_thesis_explanation(report, status, ticker):
    """Render a thesis report's reason + action as right-aligned labeled lines.

    Replaces the embedded-newline layout in report.recommended_action (which
    wrapped messily) with a clean two-column format.
    """
    label_w = 10  # width of the right-aligned label column
    if status == ThesisStatus.BROKEN:
        crit = [c.message for c in report.checks if c.severity == "CRITICAL"]
        reason = "; ".join(crit) if crit else "Thesis broken"
        print(f"      {'Reason:':>{label_w}s} {reason}")
        print(f"      {'Action:':>{label_w}s} Close position, add {ticker} to Do-Not-Wheel (6 months)")
    elif status == ThesisStatus.DAMAGED:
        warns = [c.message for c in report.checks if c.severity == "WARNING"]
        concerns = "; ".join(warns) if warns else "Technical damage"
        print(f"      {'Concerns:':>{label_w}s} {concerns}")
        print(f"      {'Action:':>{label_w}s} Weekly monitoring, re-evaluate in 7 days")


def _print_thesis(pf, yf_client) -> dict:
    """Validate the investment thesis for stocks + option underlyings + watchlist.

    Returns {ticker: report}. Auto-adds BROKEN tickers to the Do-Not-Wheel list
    (6 months) and auto-REMOVES a listed ticker when its thesis recovers to the
    configured recovery status (default INTACT), so the list no longer waits out
    the full 6-month expiry for a stock that has healed.
    """
    if yf_client is None:
        print("  ⚠️  Skipped (--no-external): deep thesis checks require yfinance.\n")
        return {}

    results = {}
    dnl = DoNotWheelList()
    with MoomooClient() as moomoo:
        targets = _thesis_targets(pf, moomoo)
        print(f"{'='*90}")
        print(f"  🔍 THESIS VALIDATION ({len(targets)} tickers: "
              f"{sum(1 for s in targets.values() if s == 'stock')} stock, "
              f"{sum(1 for s in targets.values() if s == 'option')} option, "
              f"{sum(1 for s in targets.values() if s == 'watchlist')} watch)")
        print(f"{'='*90}")
        if not targets:
            print("  (no tickers to validate)\n")
            return {}
        for ticker in sorted(targets):
            source = targets[ticker]
            try:
                snap = moomoo.get_stock_snapshot(f'US.{ticker}')
                report = validate_investment_thesis(
                    ticker=ticker, entry_date=None, entry_thesis={},
                    current_snapshot=snap, yf_client=yf_client)
                results[ticker] = report
                status = report.status
                emoji = ("✅" if status == ThesisStatus.INTACT
                         else "⚠️" if status == ThesisStatus.DAMAGED else "🚨")
                # Read-only eligibility tag (moomoo snapshot) — shows whether the
                # name is wheelable today. The watchlist is the master list; the
                # engine no longer auto-mutates a blacklist file.
                eligible, inelig_reason = is_wheel_eligible(snap, ticker)
                elig_tag = "eligible" if eligible else "NOT eligible"
                # Inline Do-Not-Wheel flag (manual override from do_not_wheel.yaml).
                # The persistent list is no longer a standalone section — it's
                # marked here next to the ticker itself with the exclusion reason.
                dnw_tag = ""
                dnw_exp = ""
                if dnl.is_excluded(ticker):
                    dnw_exp = dnl.get_expiration(ticker) or ""
                    dnw_reason = dnl.get_reason(ticker) or ""
                    dnw_tag = f"  🚫 DO-NOT-WHEEL until {dnw_exp}"
                    print(f"  {emoji} {ticker:<6s} [{source:<7s}] {status.value}  [{elig_tag}]{dnw_tag}")
                    if dnw_reason:
                        print(f"      🚫 {dnw_reason}")
                else:
                    print(f"  {emoji} {ticker:<6s} [{source:<7s}] {status.value}  [{elig_tag}]")
                if not eligible:
                    print(f"      ⛔ {inelig_reason}")
                for chk in report.checks:
                    if chk.severity == "CRITICAL":
                        print(f"      🚨 {chk.message}")
                    elif chk.severity == "WARNING":
                        print(f"      ⚠️  {chk.message}")
                # Structured explanation with right-aligned labels (replaces the
                # multi-line \n in report.recommended_action with clean columns).
                if status in (ThesisStatus.BROKEN, ThesisStatus.DAMAGED):
                    _print_thesis_explanation(report, status, ticker)
            except Exception as e:
                log.warning(f"Thesis validation error for {ticker}: {e}")
                print(f"  ⚠️  {ticker:<6s} [{source:<7s}] thesis validation error — {e}")

    intact = sum(1 for r in results.values() if r.status == ThesisStatus.INTACT)
    damaged = sum(1 for r in results.values() if r.status == ThesisStatus.DAMAGED)
    broken = sum(1 for r in results.values() if r.status == ThesisStatus.BROKEN)
    print(f"\n  📊 SUMMARY: ✅ {intact} intact   ⚠️ {damaged} damaged   🚨 {broken} broken\n")
    return results


# ════════════════════════════════════════════════════════════════
# RECOMMENDATIONS  (derived from live pf state — no hardcoded values)
# ════════════════════════════════════════════════════════════════

def _print_recommendations(pf, orders, nlv, thesis_results, violations, stage,
                           trading_allowed, roll_recs=None):
    print(f"{'='*90}")
    print(f"  📋 RECOMMENDATIONS")
    print(f"{'='*90}")
    recs = []
    roll_recs = roll_recs or []
    cfg = get_config()

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

    # ── Put credit spread pointer (defined-risk income substitute) ──
    # When CSP is effectively paused (csp deployment over limit OR cash-tight),
    # a put credit spread gives income at a fraction of the capital (max_loss,
    # not the full strike) with bounded risk. Suggestion-only — the screener
    # does the actual scoring. Honors "never prefer margin": max_loss must be
    # 100% cash-backed (enforced in src/strategies/credit_spread.py).
    if cfg.credit_spread_enabled:
        csp_dep = (csp_liab / nlv) if (nlv and nlv > 0 and csp_liab) else 0.0
        csp_paused_here = csp_dep > cfg.max_csp_deployed_pct or cash_pct < 0.15
        if csp_paused_here:
            recs.append(("MEDIUM",
                         "CSP paused / capital-tight — defined-risk put credit spreads available",
                         f"CSP deployment {csp_dep:.0%} (limit {cfg.max_csp_deployed_pct:.0%}), "
                         f"cash {cash_pct:.0%}. Put credit spreads cap downside at max loss "
                         f"(cash-backed) instead of the full strike. "
                         f"Run: python3 scripts/screener.py --ps-only"))

    broken = [t for t, r in thesis_results.items() if r.status == ThesisStatus.BROKEN]
    if broken:
        recs.append(("CRITICAL", f"Exit broken-thesis positions: {', '.join(broken)}",
                     "Close, then add to Do-Not-Wheel list for 6 months."))
    damaged = [t for t, r in thesis_results.items() if r.status == ThesisStatus.DAMAGED]
    if damaged:
        recs.append(("HIGH", f"Monitor damaged-thesis positions: {', '.join(damaged)}",
                     "Daily review covers thesis + guardrails at 09:00 UTC."))

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
                     f"Use the daily review at 09:00 UTC; let positions expire naturally."))

    # Roll winners (trend-modulated): bank the accrued profit AND stay in the
    # thesis instead of flat-closing. Recommend-only for the real portfolio;
    # net-credit-only + ≤2 rolls per campaign gate the recommendation.
    if roll_recs:
        tickers = ', '.join(sorted({t for t, _ in roll_recs}))
        recs.append(("MEDIUM", f"Roll winners in trend: {tickers}",
                     "Trend supports more upside — roll for a net credit (down-and-out "
                     "for CSPs, up-and-out for CCs) to bank profit + keep the position. "
                     "Net-credit only; max 2 rolls/campaign; skip if no credit roll exists."))

    if not trading_allowed:
        recs.append(("LOW", "Current window is monitoring-only",
                     "Daily review runs every day at 09:00 UTC — theses + guardrails."))

    if not recs:
        print("  ✅ No actions — portfolio within limits and theses intact.")
    for i, (prio, title, desc) in enumerate(recs, 1):
        emoji = "🚨" if prio == "CRITICAL" else "⚠️" if prio == "HIGH" else "💡"
        print(f"  {i}. {emoji} [{prio}] {title}")
        print(f"     {desc}")
    print()


if __name__ == '__main__':
    main()
