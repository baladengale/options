#!/usr/bin/env python3
"""
OIE — Options Income Engine

Paper trading engine that applies screener decisions to a local simulated portfolio.
Runs autonomously, tracks P&L, enforces guardrails, logs every action.

Usage:
    python3 scripts/oie_engine.py init       # Seed paper portfolio from REAL account
    python3 scripts/oie_engine.py run        # Run continuously (configurable interval)
    python3 scripts/oie_engine.py once       # Run a single cycle
    python3 scripts/oie_engine.py status     # Show paper portfolio + open positions
    python3 scripts/oie_engine.py history    # Show P&L snapshots over time
    python3 scripts/oie_engine.py reset      # Wipe paper portfolio and re-init
"""

import argparse
import os
import re
import signal
import sys
import time
import warnings
from datetime import date, datetime
from typing import Optional

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from moomoo import OpenSecTradeContext, TrdEnv, RET_OK
from src.data.moomoo_client import MoomooClient
from src.data.oie_db import OIEDB
from src.data.compute import enrich_stock_snapshot
from src.data.guardrails import GuardrailChecker, SECTOR_MAP
from src.data.models import StockSnapshot, OptionSnapshot
from src.analysis.sentiment import get_macro_context
from src.data.yfinance_client import YFinanceClient
from src.config import get_config

# ── Import scoring from src/ (NOT from scripts.screener — scripts never import scripts) ──
from src.scoring.screener_score import (
    _compute_ticker_score, _contract_penalty, _trend_composite,
    _csp_roc,
)
from src.filters.contract_filters import passes_all_gates, cc_roc
from src.data.watchlist import fetch_live_watchlist
from src.data.portfolio_loader import fetch_live_portfolio
from src.data.models import TradeCandidate
from src.analysis.profit_management import (
    TrendContext, trend_context_from_snapshot,
)
from src.analysis.exit_management import decide_exit_action

# ── Logging ──
from src.logging_setup import get_logger
log = get_logger('oie')

# ── Config ──
RUNNING = True
DEFAULT_INTERVAL_MIN = 30

# US market hours (Eastern). Summer EDT = UTC-4, winter EST = UTC-5
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MIN = 0


def is_market_open() -> tuple[bool, str]:
    """
    Check if US stock market is currently open.
    Returns (is_open, reason_string).
    DST-aware: converts to US Eastern via zoneinfo (America/New_York) —
    no month-approximation. Holidays/half-days are NOT handled (known gap,
    specs/oie-paper-engine-spec.md §7).
    """
    from src.data.market_time import eastern_now

    eastern_now = eastern_now()

    # Weekend check
    if eastern_now.weekday() >= 5:
        return False, f"Market closed — {eastern_now.strftime('%A')}"

    eastern_hour = eastern_now.hour
    eastern_minute = eastern_now.minute

    # Market hours: 9:30 AM - 4:00 PM ET
    market_open_minutes = MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN
    market_close_minutes = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    current_minutes = eastern_hour * 60 + eastern_minute

    if current_minutes < market_open_minutes:
        return False, f"Market closed — pre-open ({eastern_hour:02d}:{eastern_minute:02d} ET)"
    if current_minutes >= market_close_minutes:
        return False, f"Market closed — after hours ({eastern_hour:02d}:{eastern_minute:02d} ET)"

    return True, f"Market open ({eastern_hour:02d}:{eastern_minute:02d} ET)"


def _signal_handler(sig, frame):
    global RUNNING
    print("\n⏸️  Shutting down after current cycle...", flush=True)
    RUNNING = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ═══════════════════════════════════════════════════════════════
# ENGINE
# ═══════════════════════════════════════════════════════════════

class OIEEngine:
    """Paper trading engine — screens, executes, tracks."""

    def __init__(self, no_external: bool = False, dry_run: bool = False,
                 force: bool = False):
        self.db = OIEDB()
        self.cfg = get_config()
        self.no_external = no_external
        self.dry_run = dry_run
        self.force = force
        self.moomoo: Optional[MoomooClient] = None
        self.yf: Optional[YFinanceClient] = None

    # ═══════════════════════════════════════════════════════════
    # INIT
    # ═══════════════════════════════════════════════════════════

    def init_portfolio(self):
        """Seed paper portfolio from REAL moomoo account — OPTIONS ONLY + cash.
        Stocks are tracked outside this engine — we only care about option positions."""
        if self.db.is_seeded():
            print("⚠️  Paper portfolio already seeded. Use 'reset' first to re-seed.")
            return False

        print("📋 Connecting to REAL account...")
        try:
            (stocks_dict, cash, cash_bp, fund, existing_opts,
             _opts_dict, margin_bp, _csp_liab) = fetch_live_portfolio()
            bp = margin_bp if margin_bp > 0 else cash_bp
        except Exception as e:
            print(f"❌ Failed to fetch REAL portfolio: {e}")
            return False

        # Combine cash + fund into one liquid pool
        net_liquid = cash + fund
        if net_liquid < 0:
            net_liquid = fund

        # Fetch cost basis for all stock holdings
        stocks_with_cost = self._fetch_cost_basis(stocks_dict)

        # Fetch and seed existing option positions via MoomooClient
        with MoomooClient() as moomoo:
            option_positions = self._fetch_option_positions()
            options_seeded = 0
            options_total_premium = 0.0

            if option_positions:
                # Batch-fetch live snapshots for all option codes
                opt_codes = [o['code'] for o in option_positions]
                snapshot_map = {}
                for i in range(0, len(opt_codes), 400):
                    batch = opt_codes[i:i+400]
                    ret, data = moomoo.ctx.get_market_snapshot(batch)
                    if ret == RET_OK and data is not None:
                        for _, row in data.iterrows():
                            code = row.get('code', '')
                            if code:
                                snapshot_map[code] = row

                for opt in option_positions:
                    code = opt['code']
                    snap_row = snapshot_map.get(code)
                    current_bid = float(snap_row.get('bid_price', 0) or 0) if snap_row is not None else 0
                    current_delta = float(snap_row.get('option_delta', 0) or 0) if snap_row is not None else 0
                    current_iv = float(snap_row.get('option_implied_volatility', 0) or 0) if snap_row is not None else 0

                    self.db.open_position(
                        ticker=opt['ticker'], pos_type=opt['type'], qty=opt['qty'],
                        cost_price=opt['strike'], strike=opt['strike'],
                        expiry=opt['expiry'], dte=opt['dte'],
                        entry_premium=opt['cost'],
                        delta=current_delta, iv=current_iv,
                        current_bid=current_bid,
                        cash_impact=0,
                        note=f'SEED: {opt["type"]} ${opt["strike"]:.0f} {opt["expiry"]} '
                             f'(from REAL portfolio, cost ${opt["cost"]:.2f})')
                    options_seeded += 1
                    options_total_premium += opt['cost'] * abs(opt['qty']) * 100
                    # Premium already reflected in REAL cash — do NOT add to net_liquid

        # Seed portfolio with stocks (reference only — not screened) + cash
        self.db.seed_portfolio(stocks_with_cost, net_liquid, 0)

        stock_value = sum(info.get('qty', 0) * info.get('cost', 0) for info in stocks_with_cost.values())
        total_portfolio = stock_value + net_liquid

        print(f"\n✅ Paper portfolio seeded (all values USD):")
        print(f"   Stocks:     ${stock_value:>12,.2f}  ({len(stocks_with_cost)} tickers, reference only)")
        print(f"   Cash pool:  ${net_liquid:>12,.2f}  (real cash ${cash:,.2f} + fund ${fund:,.2f})")
        print(f"   Total:      ${total_portfolio:>12,.2f}")
        if options_seeded > 0:
            print(f"\n   📊 {options_seeded} options seeded from REAL portfolio "
                  f"(premium received: ${options_total_premium:,.2f})")
            for opt in sorted(option_positions, key=lambda o: (o['ticker'], o['expiry'])):
                print(f"     {opt['ticker']:<6s} {opt['type']:>4s} ${opt['strike']:>8,.2f} "
                      f"{opt['expiry']}  cost=${opt['cost']:.2f}  qty={opt['qty']:.0f}")
        else:
            print(f"\n   🔒 No option positions in REAL account")
        print(f"\n   📈 Stocks: managed outside this engine (check portfolio_check.py for stock positions)")
        return True

    def reconcile(self):
        """Non-destructively sync the paper STOCK rows + cash from the REAL
        account WITHOUT wiping options or history.

        The paper book is the source of truth for CC eligibility (the wheel
        rotates in paper). After manual real-account trades (buy/sell shares,
        deposit/withdraw cash), run this to correct drift so the paper book
        matches reality again. Options and P&L history are preserved.

        Behaviour:
          - For each ticker present in the REAL account: upsert the paper
            STOCK qty/cost to match (replace existing ACTIVE STOCK rows).
          - For tickers in paper but no longer in REAL: leave them (the wheel
            may have added them via CSP assignment) — surfaced in the report.
          - Cash + fund are reset to the REAL liquid pool.
        """
        if not self.db.is_seeded():
            print("❌ Not seeded. Run 'init' first.")
            return False

        print("📋 Connecting to REAL account...")
        try:
            (stocks_dict, cash, cash_bp, fund, _existing_opts,
             _opts_dict, margin_bp, _csp_liab) = fetch_live_portfolio()
        except Exception as e:
            print(f"❌ Failed to fetch REAL portfolio: {e}")
            return False

        net_liquid = cash + fund
        if net_liquid < 0:
            net_liquid = fund

        stocks_with_cost = self._fetch_cost_basis(stocks_dict)
        added, updated, unchanged = self.db.reconcile_stocks(stocks_with_cost)

        # Rebase cash to the REAL liquid pool via an AUDIT-TRAIL row (not a raw
        # set_state) so derived cash (seeded_cash + Σ cash_change) always equals
        # stored cash. Option premiums already banked stay in the running balance
        # — the adjustment row carries only the rebaselining delta.
        derived_cash = float(self.db.get_state('seeded_cash', '0')) + (
            self.db._conn.execute(
                "SELECT COALESCE(SUM(cash_change), 0) as t FROM paper_trades"
            ).fetchone()['t'] or 0)
        adjustment = round(net_liquid - derived_cash, 2)
        self.db._log_trade(datetime.now().isoformat(), 'RECONCILE', None, None,
                           f'Cash rebaselined to REAL liquid pool '
                           f'${derived_cash:,.2f} → ${net_liquid:,.2f}',
                           cash_change=adjustment)
        old_cash = derived_cash
        # Fund pool folds into cash (net_liquid = cash + fund combined)
        self.db.set_state('fund', str(round(0.0, 2)))
        self.db._conn.commit()

        print(f"\n✅ Paper book reconciled to REAL account:")
        print(f"   Stocks:   {added} added, {updated} updated, {unchanged} unchanged")
        print(f"   Cash:     ${old_cash:,.2f} → ${net_liquid:,.2f}")
        # Surface paper-only holdings (e.g. shares added by CSP assignment)
        paper_tickers = {p['ticker'] for p in self.db.get_active_stocks()}
        real_tickers = set(stocks_with_cost.keys())
        paper_only = paper_tickers - real_tickers
        if paper_only:
            print(f"   ℹ️  {len(paper_only)} ticker(s) in paper only (likely CSP-assigned): "
                  f"{', '.join(sorted(paper_only))}")
        print(f"\n   Options and P&L history preserved. Wheel rotation intact.")
        return True



    def _fetch_cost_basis(self, stocks_dict: dict) -> dict:
        """Fetch actual cost basis + live price for all tickers in ONE connection."""
        result = {}
        try:
            trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)
            ret, acc_list = trd.get_acc_list()
            if ret != RET_OK:
                trd.close()
                return result

            for _, acc in acc_list.iterrows():
                if str(acc.get('trd_env', '')) == 'SIMULATE':
                    continue
                ret3, pos = trd.position_list_query(
                    trd_env=TrdEnv.REAL, acc_id=acc['acc_id'], refresh_cache=True)
                if ret3 == RET_OK and pos is not None:
                    for _, p in pos.iterrows():
                        code = p['code']
                        if (code.startswith('US.') and not re.search(r'\d{6}[CP]\d+', code)
                                and '..' not in code):
                            ticker = code.replace('US.', '')
                            if ticker in stocks_dict:
                                cost = float(p.get('cost_price', 0) or 0)
                                price = float(p.get('nominal_price', 0) or 0)
                                if cost > 0:
                                    result[ticker] = {
                                        'qty': stocks_dict[ticker],
                                        'cost': cost,
                                        'price': price,
                                    }
                trd.close()
                break
            trd.close()
        except Exception:
            pass

        # Fill missing from live snapshot fallback
        for t, q in stocks_dict.items():
            if t not in result:
                result[t] = {'qty': q, 'cost': 0, 'price': 0}
        return result

    # ═══════════════════════════════════════════════════════════

    def _fetch_option_positions(self) -> list[dict]:
        """Fetch active option positions from REAL moomoo account."""
        options = []
        try:
            trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)
            ret, acc_list = trd.get_acc_list()
            if ret != RET_OK:
                trd.close()
                return options
            for _, acc in acc_list.iterrows():
                if str(acc.get('trd_env', '')) == 'SIMULATE':
                    continue
                ret3, pos = trd.position_list_query(
                    trd_env=TrdEnv.REAL, acc_id=acc['acc_id'], refresh_cache=True)
                if ret3 == RET_OK and pos is not None:
                    for _, p in pos.iterrows():
                        code = p['code']
                        qty = p['qty']
                        if qty == 0:
                            continue
                        parts = re.match(r"US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)", code)
                        if parts:
                            ticker = parts.group(1)
                            yr, mo, dy = parts.group(2), parts.group(3), parts.group(4)
                            opt_type = 'CALL' if parts.group(5) == 'C' else 'PUT'
                            strike_val = float(parts.group(6)) / 1000
                            expiry_str = f'20{yr}-{mo}-{dy}'
                            cost = float(p.get('cost_price', 0) or 0)
                            dte = max(0, (date.fromisoformat(expiry_str) - date.today()).days)
                            options.append({
                                'code': code, 'ticker': ticker, 'type': opt_type,
                                'strike': strike_val, 'expiry': expiry_str,
                                'qty': qty, 'cost': cost, 'dte': dte,
                            })
                trd.close()
                break
            trd.close()
        except Exception:
            pass
        return options

    # CYCLE
    # ═══════════════════════════════════════════════════════════

    def run_cycle(self) -> dict:
        """Execute one full cycle. Returns summary dict."""
        if not self.db.is_seeded():
            return {'error': 'Not seeded. Run init first.'}

        cycle_start = datetime.now()
        events = []
        new_trades = 0
        closed_trades = 0

        try:
            self.moomoo = MoomooClient()
            self.yf = YFinanceClient() if not self.no_external else None

            log.info(f"Cycle starting — dry_run={self.dry_run}")
            if self.dry_run:
                print("🔍 DRY RUN — no changes will be written to DB\n")

            # ── 1. Load state ──
            cash = float(self.db.get_state('cash', '0'))
            fund = float(self.db.get_state('fund', '0'))

            # ── 2. Load real portfolio (for CC share check) + MTM options ──
            try:
                self._real_portfolio, _, _, _, _, _, _, _ = fetch_live_portfolio()
            except Exception:
                self._real_portfolio = {}
            active_options = self.db.get_active_options()

            # Fetch live stock prices for real portfolio + option underlyings.
            # NOTE: `stock_prices` (used by the expiry ITM/OTM resolution below)
            # aliases self._stock_prices — both must see the same live marks.
            real_tickers = list(self._real_portfolio.keys())
            opt_tickers = list(set(o['ticker'] for o in active_options))
            all_stock_tickers = list(set(real_tickers + opt_tickers))
            self._stock_prices = {}
            if all_stock_tickers:
                try:
                    stock_snaps = self.moomoo.get_stock_snapshots(
                        [f'US.{t}' for t in all_stock_tickers])
                    for s in stock_snaps:
                        short = s.ticker.replace('US.', '')
                        self._stock_prices[short] = s.last_price
                except Exception:
                    pass
            stock_prices = self._stock_prices

            # Update option prices
            option_codes = [p['id'] for p in active_options]  # we need codes not IDs
            # Actually fetch by underlying ticker
            opt_underlyings = set(p['ticker'] for p in active_options)
            for ul in opt_underlyings:
                try:
                    contracts = self.moomoo.get_option_snapshots(
                        f'US.{ul}', dte_min=0, dte_max=120)
                    for c in contracts:
                        # Match to active position by strike + expiry + type
                        for pos in active_options:
                            if (pos['ticker'] == ul and pos['pos_type'] == c.option_type
                                    and abs((pos['strike'] or 0) - c.strike) < 0.01
                                    and pos['expiry'] == c.expiry):
                                self.db._conn.execute(
                                    """UPDATE paper_positions
                                       SET current_bid=?, current_delta=?, current_iv=?
                                       WHERE id=?""",
                                    (c.bid, c.delta, c.implied_vol, pos['id']))
                except Exception:
                    pass

            self.db._conn.commit()

            # ── 3. Screen watchlist (find opportunities first) ──
            active_options = self.db.get_active_options()  # refresh after MTM
            today = date.today()

            # ── Seed grace period: skip exit decisions for just-seeded positions.
            # Positions seeded from the real portfolio reflect real-world entry
            # prices/deltas — they should NOT be immediately rolled/closed on the
            # first cycle. A 300s window after seeded_at covers the first cycle.
            seeded_at_str = self.db.get_state('seeded_at', '')
            seeded_at = None
            if seeded_at_str:
                try:
                    seeded_at = datetime.fromisoformat(seeded_at_str)
                except (ValueError, TypeError):
                    pass

            for pos in active_options:
                entry = pos['entry_premium'] or 0
                current_bid = pos['current_bid'] or 0
                strike = pos['strike'] or 0
                expiry_str = pos['expiry'] or ''
                pos_type = pos['pos_type']
                ticker = pos['ticker']
                pos_id = pos['id']
                qty = abs(pos['qty'])

                # Compute DTE
                dte = 999
                if expiry_str:
                    try:
                        dte = (date.fromisoformat(expiry_str) - today).days
                    except Exception:
                        pass

                # Skip if no pricing data
                if current_bid <= 0 and dte > 0:
                    continue

                # Skip exit decisions for positions created during the seed —
                # they haven't had a full cycle yet. (Grace window: ±5 min of
                # seeded_at, covering the seed + first cycle window.)
                if seeded_at and pos.get('created_at'):
                    try:
                        pos_created = datetime.fromisoformat(pos['created_at'])
                        if abs((pos_created - seeded_at).total_seconds()) < 300:
                            continue  # grace period — skip exit decisions
                    except (ValueError, TypeError):
                        pass

                profit_captured = ((entry - current_bid) / entry * 100) if entry > 0 else 0
                delta = abs(pos.get('current_delta', 0) or 0)
                strategy = 'CC' if pos_type == 'CALL' else 'CSP'
                pnl_dollars = (entry - current_bid) * qty * 100  # negative = loss
                premium_collected = entry * qty * 100   # total credit banked

                # ── Single exit decision core (src/analysis/exit_management.py) ──
                # Composes the trend-modulated profit side (decide_profit_target)
                # with the loss-side hard stops (delta gates + premium tiers +
                # absolute catch-all) into one structured decision. All thresholds
                # come from config/rules.yaml — nothing hardcoded. This replaces
                # the prior inline block whose CC delta-stop only warned.
                tctx = self._trend_ctx(ticker)
                edec = decide_exit_action(
                    strategy, profit_captured, dte, delta, pnl_dollars,
                    tctx, capital_scarcity=self._capital_scarcity(),
                    csp_paused=self._csp_paused(),
                    premium_collected=premium_collected, cfg=self.cfg,
                    emergency=self._emergency_stage())
                close_reason = edec.close_reason
                roll_decision = edec.roll_decision
                if edec.warn:
                    events.append(f'⚠️  {ticker} {strategy} ${strike:.0f} — {edec.warn}')

                # Expiry — caller resolves ITM/OTM to ASSIGN/EXPIRE (needs stock
                # price). Only fires when the decision core took no action and
                # the contract is at/past expiry.
                if not close_reason and not roll_decision and dte <= 0:
                    if pos_type == 'CALL':
                        stock_price = stock_prices.get(ticker, 0)
                        if stock_price > strike:
                            close_reason = 'CC_ASSIGN'
                        else:
                            close_reason = 'EXPIRE'
                    else:  # PUT
                        stock_price = stock_prices.get(ticker, 0)
                        if stock_price < strike:
                            close_reason = 'CSP_ASSIGN'
                        else:
                            close_reason = 'EXPIRE'

                if close_reason:
                    # NOTE: cash is maintained ONLY by the DB audit trail
                    # (oie_db._log_trade applies every cash_impact to state).
                    # The engine never mutates cash locally — state and the
                    # derived value (seeded_cash + Σ cash_change) always agree.
                    if close_reason in ('EXPIRE',):
                        pnl = self.db.expire_position(pos_id)
                        log.info(f"EXPIRE {ticker} {pos_type} ${strike:.0f}: +${pnl:,.2f}")
                        events.append(f'📅 {ticker} {pos_type} ${strike:.0f} EXPIRED: +${pnl:,.2f}')
                        closed_trades += 1
                    elif close_reason in ('CC_ASSIGN',):
                        self.db.assign_position(pos_id, 'CC', stock_prices.get(ticker, strike))
                        events.append(f'📈 {ticker} CC ${strike:.0f} ASSIGNED: shares called away')
                        closed_trades += 1
                    elif close_reason in ('CSP_ASSIGN',):
                        self.db.assign_position(pos_id, 'CSP', stock_prices.get(ticker, strike))
                        events.append(f'📉 {ticker} CSP ${strike:.0f} ASSIGNED: {qty*100} shares added')
                        closed_trades += 1
                    elif close_reason in ('STOP_DELTA', 'STOP_LOSS'):
                        # Stop-loss close (pay to close → negative cash_impact)
                        pnl = self.db.close_position(pos_id, current_bid, close_reason,
                                                     cash_impact=-current_bid * qty * 100)
                        entry_total = entry * qty * 100
                        loss_str = (f'{abs(pnl_dollars) / entry_total:.1f}x premium'
                                    if entry_total > 0 else '')
                        log.info(f"STOP {ticker} {pos_type} ${strike:.0f} {close_reason}: "
                                f"Δ={delta:.2f} loss={loss_str} P&L=${pnl:,.2f}")
                        events.append(f'🛑 {ticker} {pos_type} ${strike:.0f} {close_reason}: '
                                    f'{profit_captured:.0f}% captured, P&L ${pnl:,.2f}')
                        closed_trades += 1
                    else:
                        # Profit target close — per-ticker churn cap
                        # (guardrail_limits.max_closes_per_ticker_per_month):
                        # discretionary profit-taking is capped; hard exits
                        # (stops/assignments/expiry) are never blocked.
                        churn_cap = int(self.cfg.guardrail_limits(
                            'max_closes_per_ticker_per_month', 2))
                        if close_reason in ('CLOSE_50PCT', 'CLOSE_TREND') and \
                                self.db.get_monthly_profit_closes(ticker) >= churn_cap:
                            events.append(f'⏸️ {ticker} {pos_type} ${strike:.0f} {close_reason} '
                                        f'HELD: monthly profit-close cap ({churn_cap}/month) reached')
                            log.info(f"CHURN_HOLD {ticker}: profit-close cap {churn_cap}/month reached")
                        else:
                            pnl = self.db.close_position(pos_id, current_bid, close_reason,
                                                         cash_impact=-current_bid * qty * 100)
                            log.info(f"CLOSE {ticker} {pos_type} ${strike:.0f} {close_reason}: {profit_captured:.0f}%, P&L=${pnl:,.2f}")
                            events.append(f'💰 {ticker} {pos_type} ${strike:.0f} {close_reason}: '
                                        f'{profit_captured:.0f}% captured, P&L ${pnl:,.2f}')
                            closed_trades += 1

                # ── Roll winner (trend-modulated): bank profit, redeploy for credit ──
                # Paper semantics: close the winner now (booking the profit), and let
                # PHASE 4 screening open a fresh contract on the same ticker/strategy
                # (net-credit-only + ≤2 rolls gate via the rolling config). This mirrors
                # how a practitioner rolls — close the tested leg, sell a new one.
                # Cash: entry credit was booked at open (open_position), the buyback is
                # booked here — both via the audit trail.
                if roll_decision and not close_reason:
                    pnl = self.db.close_position(pos_id, current_bid, roll_decision,
                                                 cash_impact=-current_bid * qty * 100)
                    direction = 'down-and-out' if roll_decision == 'ROLL_DOWN_OUT' else 'up-and-out'
                    log.info(f"ROLL {ticker} {pos_type} ${strike:.0f} {roll_decision}: "
                             f"{profit_captured:.0f}% captured, P&L=${pnl:,.2f}, {direction}")
                    events.append(f'🔄 {ticker} {pos_type} ${strike:.0f} ROLL ({direction}): '
                                f'{profit_captured:.0f}% captured, P&L=${pnl:,.2f} — '
                                f'redeploying for credit in screen phase')
                    closed_trades += 1

            # ── 4. Screen new opportunities ──
            # Cash state is maintained by the DB audit trail — re-read it.
            self.db._conn.commit()
            candidates = self._screen_candidates(stock_prices)

            # ── 5. Apply guardrails (skip if --ignore-guardrails) ──
            if self.force:
                log.info("Guardrails DISABLED — executing all candidates")
            # Re-read state after exits — guardrails count OPTIONS ONLY
            cash = float(self.db.get_state('cash', '0'))
            open_options = self.db.get_active_options()
            active_all = open_options  # guardrails only check options
            daily_new = self.db.get_daily_new_count()

            # NLV = cash + real stock market value (for accurate concentration %)
            stock_mv = sum(
                qty * (self._stock_prices.get(t, 0) or 0)
                for t, qty in self._real_portfolio.items())
            net_liq = cash + fund + stock_mv

            gc_positions = []
            for p in active_all:
                if p['pos_type'] == 'STOCK':
                    notional = (p['current_bid'] or p['cost_price'] or 0) * p['qty']
                else:
                    notional = (p['strike'] or 0) * abs(p['qty']) * 100
                csp_liability = sum(
                    abs(op['strike'] or 0) * abs(op['qty']) * 100
                    for op in open_options
                    if op['ticker'] == p['ticker'] and op['pos_type'] == 'PUT'
                )
                gc_positions.append({
                    'ticker': p['ticker'], 'notional': notional,
                    'sector': SECTOR_MAP.get(p['ticker'], 'Unknown'),
                    'csp_liability': csp_liability,
                    'strategy': 'CSP' if p['pos_type'] == 'PUT' else (
                        'CC' if p['pos_type'] == 'CALL' else 'STOCK'),
                })

            # Buying power = liquid only (cash + fund). Margin BP NEVER counts
            # toward CSP coverage (GOAL #4 — 100% cash-secured). The regime is
            # threaded so the CSP-deployment block tightens in VOLATILE/BEARISH.
            regime_label = getattr(self, '_regime', 'NEUTRAL')
            gc = GuardrailChecker(net_liq=net_liq, cash=cash,
                                   buying_power=cash + fund,
                                   open_positions=gc_positions,
                                   daily_order_count=daily_new,
                                   regime=regime_label)

            # ── 6. Execute ──
            executed = 0
            max_new = max(0, min(self.cfg.max_new_positions_per_cycle,
                                 self.cfg.max_daily_new_positions - daily_new))
            gr = gc.check()
            if gr.blocks:
                log.warning(f"Portfolio BLOCKS: {gr.blocks}")
                events.append(f'🛡️ Portfolio BLOCKS: {"; ".join(gr.blocks[:2])}')

            for c in candidates:
                if executed >= max_new:
                    break

                # Put credit spreads are suggestion-only in this version — the
                # OIE engine does not (yet) execute or persist them. Explicitly
                # skip so a stray 'PS' candidate is never silently dropped by
                # the CC/CSP branches below. See src/strategies/credit_spread.py.
                if getattr(c, 'strategy', '') == 'PS':
                    log.debug(f"SKIP_PS {c.ticker} — spreads are suggestion-only")
                    continue

                # Per-trade guardrail checks (skipped if --ignore-guardrails)
                if not self.force:
                    new_notional = c.capital_required
                    ticker_pct = new_notional / net_liq * 100 if net_liq > 0 else 0
                    if ticker_pct > self.cfg.max_single_position_pct * 100:
                        msg = f'{c.ticker} {c.strategy} BLOCKED: {ticker_pct:.1f}% > {self.cfg.max_single_position_pct*100:.0f}% limit'
                        log.warning(msg)
                        events.append(f'🛡️ {msg}')
                        continue

                    # CSP pause — the 5 GOAL.md §5 triggers (config-driven)
                    if c.strategy == 'CSP':
                        paused, reasons = self._csp_pause_reasons(c.ticker)
                        if paused:
                            msg = f'{c.ticker} CSP BLOCKED: pause trigger — {"; ".join(reasons)}'
                            log.warning(msg)
                            events.append(f'🛡️ {msg}')
                            continue

                    # Same-strike reopen cooldown (guardrail_limits)
                    cooldown_days = int(self.cfg.guardrail_limits(
                        'same_strike_reopen_cooldown_days', 14))
                    if cooldown_days > 0 and self.db.get_last_exit_within_days(
                            c.ticker, 'PUT' if c.strategy == 'CSP' else 'CALL',
                            c.strike, cooldown_days):
                        msg = f'{c.ticker} {c.strategy} ${c.strike:.0f} BLOCKED: ' \
                              f'same-strike cooldown ({cooldown_days}d)'
                        log.info(msg)
                        events.append(f'🛡️ {msg}')
                        continue

                    # Cash buffer check for CSP — liquid cash only, fraction from
                    # config (position_limits.csp_single_cash_fraction).
                    if c.strategy == 'CSP' and c.capital_required > cash * self.cfg.csp_single_cash_fraction:
                        msg = (f'{c.ticker} CSP BLOCKED: capital ${c.capital_required:,.0f} > '
                               f'{self.cfg.csp_single_cash_fraction:.0%} of cash ${cash:,.0f}')
                        log.warning(msg)
                        events.append(f'🛡️ {msg}')
                        continue

                    # Full guardrail for this specific trade
                    is_cc = c.strategy == 'CC'
                    check = gc.check_new_trade(
                        c.ticker, 'CC' if is_cc else 'CSP',
                        c.capital_required,
                        sector=SECTOR_MAP.get(c.ticker, 'Unknown'))
                    if not check.all_clear:
                        # For CCs: skip cash-buffer and CSP-deployment blocks —
                        # a covered call is share-secured, not cash-secured.
                        # Only ticker-specific blocks (concentration, sector) apply.
                        if is_cc:
                            trade_blocks = [b for b in check.blocks
                                           if c.ticker in b]
                        else:
                            trade_blocks = [b for b in check.blocks
                                           if c.ticker in b or 'cash' in b.lower()
                                           or 'CSP' in b]
                        if trade_blocks:
                            events.append(f'🛡️ {c.ticker} {c.strategy} BLOCKED: {"; ".join(trade_blocks[:2])}')
                            continue
                    if check.warnings:
                        for w in check.warnings[:1]:
                            if c.ticker not in w.lower():
                                events.append(f'⚠️ {c.ticker} {c.strategy} WARN: {w[:80]}')

                # Execute (or simulate in dry-run). Cash updates flow through
                # open_position's cash_impact → the DB audit trail only.
                prefix = '🔍 [DRY RUN] Would open' if self.dry_run else '📝'
                if c.strategy == 'CC':
                    if not self.dry_run:
                        self.db.open_position(
                            ticker=c.ticker, pos_type='CALL', qty=-1,
                            cost_price=c.strike, strike=c.strike,
                            expiry=c.expiry, dte=c.dte,
                            entry_premium=c.bid, delta=c.delta,
                            iv=c.iv, current_bid=c.bid,
                            cash_impact=c.bid * 100,
                            note=f'CC ${c.strike:.0f}x{c.expiry} Δ{c.delta:.2f} '
                                 f'RoC{c.annualized_roc_pct:.1f}% Score{c.score}')
                    log.info(f"OPEN_CC {c.ticker} ${c.strike:.0f} {c.expiry} DTE={c.dte} Δ={c.delta:.2f} prem=${c.bid:.2f} RoC={c.annualized_roc_pct:.1f}%")
                    events.append(f'{prefix} {c.ticker} CC ${c.strike:.0f} {c.expiry} '
                                f'DTE={c.dte} Δ={c.delta:.2f} '
                                f'premium=${c.bid:.2f} RoC={c.annualized_roc_pct:.1f}%')
                    new_trades += 1
                    executed += 1

                elif c.strategy == 'CSP':
                    if not self.dry_run:
                        self.db.open_position(
                            ticker=c.ticker, pos_type='PUT', qty=-1,
                            cost_price=c.strike, strike=c.strike,
                            expiry=c.expiry, dte=c.dte,
                            entry_premium=c.bid, delta=c.delta,
                            iv=c.iv, current_bid=c.bid,
                            cash_impact=c.bid * 100,
                            note=f'CSP ${c.strike:.0f}x{c.expiry} Δ{c.delta:.2f} '
                                 f'RoC{c.annualized_roc_pct:.1f}% Score{c.score}')
                    log.info(f"OPEN_CSP {c.ticker} ${c.strike:.0f} {c.expiry} DTE={c.dte} Δ={c.delta:.2f} prem=${c.bid:.2f} RoC={c.annualized_roc_pct:.1f}%")
                    events.append(f'{prefix} {c.ticker} CSP ${c.strike:.0f} {c.expiry} '
                                f'DTE={c.dte} Δ={c.delta:.2f} '
                                f'premium=${c.bid:.2f} RoC={c.annualized_roc_pct:.1f}%')
                    new_trades += 1
                    executed += 1

            # ── 7. Snapshot ──
            # Cash re-read from the audit-maintained state (single writer).
            self.db._conn.commit()
            cash = float(self.db.get_state('cash', '0'))

            active_all = self.db.get_active_positions()
            option_premium = sum(
                (p['entry_premium'] or 0) * abs(p['qty']) * 100
                for p in active_all if p['pos_type'] in ('CALL', 'PUT')
            )
            option_liability = sum(
                (p['current_bid'] or 0) * abs(p['qty']) * 100
                for p in active_all if p['pos_type'] in ('CALL', 'PUT')
            )
            realized = self.db.get_closed_pnl()
            unrealized = option_premium - option_liability
            total_value = cash + fund + unrealized  # options-only NLV

            if not self.dry_run:
                option_count = sum(1 for p in active_all if p['pos_type'] in ('CALL', 'PUT'))
                self.db.save_snapshot(
                    total_value=total_value, cash=cash,
                    stock_value=0, fund_value=fund,
                    option_premium=option_premium,
                    option_liability=option_liability,
                    unrealized_pnl=unrealized, realized_pnl=realized,
                    open_positions=option_count)

                # ── 8. Log cycle ──
                self.db.set_state('last_cycle', datetime.now().isoformat())
                cycle_num = int(self.db.get_state('cycle_count', '0')) + 1
                self.db.set_state('cycle_count', str(cycle_num))
                self.db._conn.commit()
            else:
                cycle_num = int(self.db.get_state('cycle_count', '0')) + 1
                print(f"\n  🔍 Dry run complete. Would have been cycle #{cycle_num}.")

            elapsed = (datetime.now() - cycle_start).total_seconds()
            log.info(f"Cycle complete: {elapsed:.1f}s, value=${total_value:,.2f}, new={new_trades}, closed={closed_trades}, positions={len(active_all)}")
            return {
                'cycle': cycle_num,
                'elapsed': elapsed,
                'events': events,
                'new_trades': new_trades,
                'closed_trades': closed_trades,
                'total_value': total_value,
                'cash': cash,
                'stock_value': 0,
                'realized_pnl': realized,
                'unrealized_pnl': unrealized,
                'open_positions': len(active_all),
            }

        except Exception as e:
            log.error(f"Cycle failed: {e}", exc_info=True)
            self.db._log_trade(datetime.now().isoformat(), 'ERROR', None, None,
                             f'Cycle failed: {e}')
            self.db._conn.commit()
            return {'error': str(e)}

        finally:
            if self.moomoo:
                self.moomoo.close()
                self.moomoo = None

    # ═══════════════════════════════════════════════════════════
    # SCREENING
    # ═══════════════════════════════════════════════════════════

    def _trend_ctx(self, ticker: str) -> TrendContext:
        """Build a TrendContext for one underlying (best-effort, cached per cycle).

        Reuses the shared _trend_composite so the paper engine's exit decisions
        see the same 0-100 number the entry screen scored on.
        """
        cache = getattr(self, '_trend_cache', None)
        if cache is None:
            cache = self._trend_cache = {}
        if ticker in cache:
            return cache[ticker]
        ctx = TrendContext()
        try:
            if self.moomoo:
                snap = self.moomoo.get_stock_snapshot(f'US.{ticker}')
                if snap and snap.last_price > 0:
                    history = self.moomoo.get_price_history(f'US.{ticker}', 252)
                    if history:
                        from src.data.compute import enrich_stock_snapshot
                        enrich_stock_snapshot(snap, history)
                    ctx = trend_context_from_snapshot(snap)
        except Exception:
            pass
        cache[ticker] = ctx
        return ctx

    def _capital_scarcity(self) -> str:
        """Coarse capital-scarcity label from paper state (SCARCE/NORMAL/ABUNDANT)."""
        try:
            cash = float(self.db.get_state('cash') or 0)
            positions = self.db.get_active_options()
            slot_util = len(positions) / max(1, get_config().max_open_positions)
            # NLV approximation from paper state
            nlv = cash + sum(float(p.get('entry_premium', 0) or 0) * abs(p.get('qty', 1)) * 100
                             for p in positions)
            cash_pct = cash / nlv if nlv > 0 else 0
            if slot_util < 0.25 and cash_pct >= 0.30:
                return 'ABUNDANT'
            if slot_util < 0.50 and cash_pct >= 0.20:
                return 'NORMAL'
            return 'SCARCE'
        except Exception:
            return 'NORMAL'

    def _csp_paused(self) -> bool:
        """Whether CSP redeployment is currently blocked (paper state).

        Mirrors the live portfolio.py check: deployment % over the limit means
        freed capital has no CSP slot to redeploy into, which enables the
        deployment-aware SCARCE bypass in decide_profit_target. CSP liability
        = Σ(strike × |qty| × 100) over active PUT positions; NLV mirrors
        _capital_scarcity's approximation. The limit tightens to
        max_csp_deployed_volatile_pct in VOLATILE/BEARISH regimes.
        """
        try:
            positions = self.db.get_active_options()
            csp_liability = sum(
                float(p.get('strike', 0) or 0) * abs(p.get('qty', 1)) * 100
                for p in positions if p.get('pos_type') == 'PUT'
            )
            if csp_liability <= 0:
                return False
            cash = float(self.db.get_state('cash') or 0)
            nlv = cash + sum(float(p.get('entry_premium', 0) or 0) * abs(p.get('qty', 1)) * 100
                             for p in positions)
            if nlv <= 0:
                return False
            limit = get_config().max_csp_deployed_pct
            if getattr(self, '_regime', '') in ('VOLATILE', 'BEARISH'):
                limit = get_config().max_csp_deployed_volatile_pct
            return (csp_liability / nlv) > limit
        except Exception:
            return False

    def _csp_pause_reasons(self, ticker: Optional[str] = None) -> tuple[bool, list[str]]:
        """ALL five GOAL.md §5 CSP pause triggers, config-driven
        (Config.should_pause_csp + per-ticker basis drop).

        1. VIX > csp_pause.vix_above            (macro, when yf available)
        2. SPY < csp_pause.spy_below_sma-day SMA (moomoo history)
        3. Regime score ≤ csp_pause.regime_min_score
        4. Cash reserve < csp_pause.cash_reserve_below_pct
        5. Ticker drop > csp_pause.stock_drop_from_basis_pct from paper basis
           (per-ticker — only when ticker is given)

        Data-blind triggers (no macro/SPY data) do not fire — the engine never
        blocks on unknowns it cannot measure.
        """
        reasons: list[str] = []
        try:
            macro = getattr(self, '_macro', None)
            vix = getattr(macro, 'vix', None) if macro else None
            regime_score = int(getattr(macro, 'regime_score', 0)) if macro else None

            # Cash reserve vs NLV (cash + real stock market value)
            cash = float(self.db.get_state('cash') or 0)
            fund = float(self.db.get_state('fund') or 0)
            stock_mv = sum(
                qty * (self._stock_prices.get(t, 0) or 0)
                for t, qty in getattr(self, '_real_portfolio', {}).items())
            nlv = cash + fund + stock_mv
            cash_reserve_pct = (cash + fund) / nlv if nlv > 0 else 1.0

            # SPY vs its N-day SMA (moomoo, best-effort)
            spy_price = spy_sma = None
            try:
                if self.moomoo:
                    spy_snap = self.moomoo.get_stock_snapshot('US.SPY')
                    if spy_snap and spy_snap.last_price > 0:
                        spy_price = spy_snap.last_price
                        window = self.cfg.csp_pause_spy_sma
                        hist = self.moomoo.get_price_history('US.SPY', window)
                        if hist and len(hist) >= window // 2:
                            spy_sma = sum(hist[-window:]) / len(hist[-window:])
            except Exception:
                pass

            paused, r = self.cfg.should_pause_csp(
                vix=vix if vix is not None else -1.0,   # -1 → trigger can't fire
                regime_score=regime_score if regime_score is not None else +1,
                cash_reserve_pct=cash_reserve_pct,
                spy_price=spy_price, spy_sma=spy_sma)
            reasons.extend(r)

            # Per-ticker trigger: drop from paper cost basis
            if ticker:
                drop_pct = self.cfg.csp_pause_stock_drop_pct
                basis = self._paper_stock_basis(ticker)
                price = getattr(self, '_stock_prices', {}).get(ticker, 0)
                if basis > 0 and price > 0 and price < basis * (1 - drop_pct):
                    reasons.append(
                        f'{ticker} {-(1 - price / basis):.0%} > {drop_pct:.0%} below basis')

            if reasons:
                log.info(f"CSP_PAUSE|ticker={ticker or 'GLOBAL'}|reasons={'; '.join(reasons)}")
        except Exception:
            return False, []
        return bool(reasons), reasons

    def _paper_stock_basis(self, ticker: str) -> float:
        """Quantity-weighted cost basis of ACTIVE paper STOCK rows (0 if unknown)."""
        try:
            rows = self.db.get_active_stocks()
            qty = basis = 0.0
            for r in rows:
                if r['ticker'] == ticker:
                    q = r['qty'] or 0
                    qty += q
                    basis += q * (r['cost_price'] or 0)
            return basis / qty if qty > 0 else 0.0
        except Exception:
            return 0.0

    def _emergency_stage(self) -> bool:
        """Whether the paper account is in EMERGENCY recovery stage.

        Mirrors live portfolio.py _determine_recovery_stage thresholds:
        cash buffer < 10% critical OR CSP deployment > 50% → EMERGENCY.
        EMERGENCY disables the deployment-aware SCARCE bypass in
        decide_profit_target (booking profit to repair the balance sheet
        outranks the unvalidated trend extension).
        """
        try:
            positions = self.db.get_active_options()
            cash = float(self.db.get_state('cash') or 0)
            nlv = cash + sum(float(p.get('entry_premium', 0) or 0) * abs(p.get('qty', 1)) * 100
                             for p in positions)
            if nlv <= 0:
                return False
            cash_pct = cash / nlv
            csp_liability = sum(
                float(p.get('strike', 0) or 0) * abs(p.get('qty', 1)) * 100
                for p in positions if p.get('pos_type') == 'PUT'
            )
            csp_dep = csp_liability / nlv
            return cash_pct < 0.10 or csp_dep > 0.50
        except Exception:
            return False

    def _screen_candidates(self, stock_prices: dict) -> list:
        """Run screener against paper portfolio. Returns ranked TradeCandidate list.
        Optimized: batch snapshots, cached history, tiered DTE, pre-filter."""
        candidates = []
        if not self.moomoo:
            return candidates

        watchlist = fetch_live_watchlist(self.moomoo.ctx)
        cash = float(self.db.get_state('cash', '0'))
        fund = float(self.db.get_state('fund', '0'))

        # CC share availability — PAPER BOOK IS THE SOURCE OF TRUTH.
        # free_shares = paper shares held − shares already committed to open CCs.
        # This makes the wheel rotate in paper: a CSP assignment adds shares
        # (assign_position('CSP')), a CC assignment removes them. The real
        # account is only read at seed / reconcile, so the engine is
        # self-contained and can run the full CC↔CSP cycle on paper.
        cc_committed: dict[str, float] = {}
        for o in self.db.get_active_options():
            if o.get('pos_type') == 'CALL':
                t = o['ticker']
                cc_committed[t] = cc_committed.get(t, 0) + abs(o.get('qty', 0)) * 100


        # Macro — stored on self for the CSP-pause triggers (_csp_pause_reasons)
        # and the regime-aware CSP-deployment limit (GuardrailChecker regime=).
        regime = 'NEUTRAL'
        regime_mult = 1.0
        self._macro = None
        if self.yf:
            try:
                macro = get_macro_context(self.yf)
                self._macro = macro
                regime = macro.market_regime
                regime_mult = macro.position_mult
            except Exception:
                pass
        self._regime = regime

        log.info(f"OIE_SCAN|tickers={len(watchlist)}|regime={regime}|"
                 f"cash=${cash:,.0f}|force={self.force}|dry={self.dry_run}")

        # ── OPTIMIZATION 1: Batch all stock snapshots (watchlist + portfolio) ──
        all_tickers = list(set(watchlist + [f'US.{t}' for t in self._real_portfolio.keys()]))
        all_snaps = self.moomoo.get_stock_snapshots(all_tickers)
        snap_map = {}
        for s in all_snaps:
            snap_map[s.ticker] = s

        # ── OPTIMIZATION 2: SPY history cached (first fetch only) ──
        spy_history = self.moomoo.get_price_history('US.SPY', 252)

        # NLV = cash + fund + real stock market value (for accurate concentration %)
        stock_mv = 0.0
        for t, qty in self._real_portfolio.items():
            snap = snap_map.get(f'US.{t}')
            if snap and snap.last_price > 0:
                stock_mv += qty * snap.last_price
        net_liq = cash + fund + stock_mv

        # ── OPTIMIZATION 4: Pre-filter tickers ──
        viable = []
        for ticker in watchlist:
            short = ticker.replace('US.', '')
            # Don't skip ticker entirely — only skip exact same option later
            snap = snap_map.get(ticker)
            if snap is None or snap.last_price <= 0:
                continue
            # Skip illiquid tickers (wide spread = poor option liquidity) —
            # threshold from config (options.liquidity.bid_ask_spread_max_pct)
            if snap.bid_ask_spread_pct and snap.bid_ask_spread_pct > self.cfg.spread_max_pct:
                continue
            viable.append((ticker, short, snap))

        # Global CSP pause (GOAL.md §5, all 5 triggers) — skip the CSP branch
        # for the whole scan when paused. CCs (share-secured) are unaffected.
        csp_paused, pause_reasons = self._csp_pause_reasons()
        if csp_paused:
            log.info(f"CSP PAUSED this cycle: {'; '.join(pause_reasons)}")

        # ── OPTIMIZATION 3: Single DTE range per ticker ──
        for ticker, short, snap in viable:
            # CC eligibility: free paper shares (held − committed to open CCs).
            free_shares = self.db.get_shares(short) - cc_committed.get(short, 0)
            has_shares = free_shares >= 100

            # Fetch history + enrich (cached after first cycle)
            history = self.moomoo.get_price_history(ticker, 252)
            if history:
                enrich_stock_snapshot(snap, history, spy_history)

            # Earnings blackout — best-effort via yfinance; hard gate when KNOWN
            # (options.earnings.blackout_days). Unknown data does not block.
            earnings_blackout = False
            days_to_earnings = None
            if self.yf:
                try:
                    from src.analysis.sentiment import get_ticker_sentiment
                    ts = get_ticker_sentiment(self.yf, ticker)
                    earnings_blackout = bool(ts.in_earnings_blackout)
                    days_to_earnings = ts.days_to_earnings
                except Exception:
                    pass

            # Ticker score (earnings blackout feeds the +2.0 penalty; IV rank
            # neutral when unknown — the per-contract IVR gate lives in
            # passes_all_gates and only fires on known data)
            ticker_score = _compute_ticker_score(
                snap=snap,
                trend_composite=_trend_composite(snap),
                analyst_consensus='N/A',
                earnings_blackout=earnings_blackout,
                insider_sentiment='NEUTRAL',
                target_upside=None,
                news_score=50,
                regime=regime,
                regime_mult=regime_mult,
                iv_rank=50.0,
            )

            contracts = self.moomoo.get_option_snapshots_resilient(
                ticker, dte_min=self.cfg.dte_screen_min,
                dte_max=self.cfg.dte_screen_max)
            if not contracts:
                continue

            # CC cost-basis gates (GOAL.md §6, cc_management config):
            #  - never sell below cost basis (locks in a loss)
            #  - pause CCs when the stock is deep below basis
            basis = self._paper_stock_basis(short)
            price = snap.last_price or 0
            cc_below_basis_block = (self.cfg.cc_never_sell_below_basis
                                    and basis > 0)
            cc_pause_drop = (basis > 0 and price > 0
                             and price < basis * (1 - self.cfg.cc_pause_drop_pct))
            if has_shares and cc_pause_drop:
                log.info(f"CC PAUSED {short}: price ${price:.2f} > "
                         f"{self.cfg.cc_pause_drop_pct:.0%} below basis ${basis:.2f}")

            for c in contracts:
                # Basic filters (matching screener logic)
                abs_d = abs(c.delta or 0)

                # CSP
                if c.option_type == 'PUT' and not has_shares and not csp_paused:
                    # Earnings blackout hard gate (known data only)
                    if earnings_blackout:
                        continue
                    ok, reason = passes_all_gates(
                        c, 'CSP', regime, snap, cfg=self.cfg,
                        skip_concentration=self.force,
                        skip_cash_buffer=self.force,
                        net_liq=net_liq, cash=cash,
                        buying_power=cash + fund)
                    if not ok:
                        continue
                    roc = _csp_roc(c.bid, c.strike, c.dte)
                    capital = c.strike * 100

                    contract_score = ticker_score + _contract_penalty(c, abs_d, roc)
                    if contract_score <= 5:
                        log.info(f"CSP|{short}|${c.strike:.0f}|{c.expiry}|DTE={c.dte}|Δ={abs_d:.3f}|"
                                 f"bid={c.bid:.2f}|IV={c.implied_vol:.0f}%|OI={c.open_interest}|"
                                 f"RoC={roc:.1f}%|score={contract_score:.1f}")
                    candidates.append(TradeCandidate(
                        ticker=short, strategy='CSP',
                        score=round(contract_score, 2),
                        strike=c.strike, expiry=c.expiry, dte=c.dte,
                        delta=abs_d, bid=c.bid, iv=c.implied_vol,
                        annualized_roc_pct=round(roc, 1),
                        open_interest=c.open_interest,
                        capital_required=capital))

                # CC
                if c.option_type == 'CALL' and has_shares:
                    # Never sell below cost basis (GOAL.md §6) — a CC strike
                    # under basis locks in a loss on assignment.
                    if cc_below_basis_block and c.strike < basis:
                        continue
                    # Earnings blackout hard gate (GOAL.md §8 pre-trade
                    # checklist applies to ANY new trade; known data only)
                    if earnings_blackout:
                        continue
                    ok, reason = passes_all_gates(
                        c, 'CC', regime, snap, cfg=self.cfg,
                        skip_concentration=True,
                        skip_cash_buffer=True,
                        net_liq=net_liq, cash=cash,
                        buying_power=cash + fund)
                    if not ok:
                        continue
                    roc = cc_roc(c.bid, snap.last_price, c.dte)
                    capital = snap.last_price * 100

                    contract_score = ticker_score + _contract_penalty(c, c.delta, roc)
                    if contract_score <= 5:
                        log.info(f"CC|{short}|${c.strike:.0f}|{c.expiry}|DTE={c.dte}|Δ={c.delta:.3f}|"
                                 f"bid={c.bid:.2f}|IV={c.implied_vol:.0f}%|OI={c.open_interest}|"
                                 f"RoC={roc:.1f}%|score={contract_score:.1f}")
                    candidates.append(TradeCandidate(
                        ticker=short, strategy='CC',
                        score=round(contract_score, 2),
                        strike=c.strike, expiry=c.expiry, dte=c.dte,
                        delta=c.delta, bid=c.bid, iv=c.implied_vol,
                        annualized_roc_pct=round(roc, 1),
                        open_interest=c.open_interest,
                        capital_required=capital))

            time.sleep(0.1)  # light rate limit between tickers

        # Build existing option signatures for dedup
        active_opts = self.db.get_active_options()
        existing_sigs = set()
        for ao in active_opts:
            existing_sigs.add((
                ao['ticker'],
                'CC' if ao['pos_type'] == 'CALL' else 'CSP',
                round(ao['strike'] or 0, 2),
                ao['expiry'] or ''
            ))

        # Dedup: best per ticker (one recommendation per ticker), skip existing
        seen = set()
        deduped = []
        candidates.sort(key=lambda x: x.score)
        for c in candidates:
            # Skip if this exact option is already in portfolio
            sig = (c.ticker, c.strategy, round(c.strike, 2), c.expiry)
            if sig in existing_sigs:
                continue
            # One best per ticker
            if c.ticker not in seen:
                deduped.append(c)
                seen.add(c.ticker)

        return deduped

    # ═══════════════════════════════════════════════════════════
    # TEST
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def run_tests() -> bool:
        """Self-check — validates DB, config, and scoring imports. No OpenD needed."""
        import traceback
        all_ok = True

        def check(name: str, fn) -> bool:
            try:
                fn()
                print(f"  ✅ {name}")
                return True
            except Exception as e:
                print(f"  ❌ {name}: {e}")
                return False

        print("🔍 OIE Self-Test\n")

        # 1. DB schema
        print("─ DB Schema ─")
        ok = True
        try:
            db = OIEDB()
            tables = db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            names = {r['name'] for r in tables}
            for t in ['paper_state', 'paper_positions', 'paper_trades', 'paper_snapshots']:
                if t in names:
                    print(f"  ✅ {t}")
                else:
                    print(f"  ❌ {t} MISSING")
                    ok = False
            db.close()
        except Exception as e:
            print(f"  ❌ DB connection failed: {e}")
            ok = False
        all_ok &= ok

        # 2. Config
        print("\n─ Config ─")
        def _check_config():
            from src.config import get_config
            cfg = get_config()
            assert cfg.roc_min_csp > 0, "roc_min_csp must be positive"
            assert cfg.roc_min_cc > 0, "roc_min_cc must be positive"
            assert cfg.max_open_positions > 0, "max_open_positions must be positive"
            assert 0 < cfg.max_single_position_pct < 1, "max_single_position_pct must be 0-1"
            assert len(cfg.default_watchlist) > 0, "default_watchlist must not be empty"
        all_ok &= check("config/rules.yaml loads + all required keys present", _check_config)

        # 3. Scoring imports
        print("\n─ Scoring Functions ─")
        def _check_scoring():
            from scripts.screener import (
                _compute_ticker_score, _contract_penalty, _trend_composite,
                _csp_roc, _score_technical, _score_macro, _score_stars, _reason)
            # Quick smoke test
            roc = _csp_roc(5.0, 100.0, 42)
            assert roc > 0, f"_csp_roc should be positive, got {roc}"
            assert _score_stars(1.5) == '⭐1'
            assert 'Excellent' in _reason(5.0, 1.5, 'CSP')
            assert 'Marginal' in _reason(5.0, 8.0, 'CC')
        all_ok &= check("screener scoring functions import + smoke test", _check_scoring)

        # 4. Guardrails import
        print("\n─ Guardrails ─")
        def _check_guardrails():
            from src.data.guardrails import GuardrailChecker, SECTOR_MAP
            gc = GuardrailChecker(net_liq=100000, cash=30000, buying_power=60000,
                                   open_positions=[{'ticker': 'TEST', 'notional': 10000,
                                                    'sector': 'Technology', 'csp_liability': 0}])
            report = gc.check()
            assert report.all_clear, "Should pass with safe portfolio"
            assert len(report.blocks) == 0, f"Unexpected blocks: {report.blocks}"
        all_ok &= check("GuardrailChecker import + basic check", _check_guardrails)

        # 5. Market hours
        print("\n─ Market Hours ─")
        is_open, reason = is_market_open()
        print(f"  ℹ️  {reason}")

        print(f"\n{'='*50}")
        if all_ok:
            print("✅ ALL CHECKS PASSED")
        else:
            print("❌ SOME CHECKS FAILED — review output above")
        return all_ok

    # ═══════════════════════════════════════════════════════════
    # STATUS
    # ═══════════════════════════════════════════════════════════

    def show_status(self):
        if not self.db.is_seeded():
            print("❌ Paper portfolio not seeded. Run 'init' first.")
            return

        # Cash is DERIVED from trade history (never stale):
        #   seeded_cash + sum of all cash_change in paper_trades
        seeded_cash = float(self.db.get_state('seeded_cash', '0'))
        trade_flows = self.db._conn.execute(
            "SELECT COALESCE(SUM(cash_change), 0) as total FROM paper_trades WHERE cash_change != 0"
        ).fetchone()
        cash = seeded_cash + (trade_flows['total'] or 0)
        seeded_at = self.db.get_state('seeded_at', 'unknown')
        last_cycle = self.db.get_state('last_cycle', 'never')
        cycle_count = int(self.db.get_state('cycle_count', '0'))
        options = self.db.get_active_options()
        stocks = self.db.get_active_stocks()

        option_premium = sum((p['entry_premium'] or 0) * abs(p['qty']) * 100 for p in options)
        option_liability = sum((p['current_bid'] or 0) * abs(p['qty']) * 100 for p in options)
        option_unrealized = option_premium - option_liability
        realized = self.db.get_closed_pnl()
        stock_total = sum(p['cost_price'] * abs(p['qty']) for p in stocks) if stocks else 0.0
        # Cash buffer: cash as % of cash+stocks (option liability is contingent)
        total_assets = cash + stock_total
        cash_pct = (cash / total_assets * 100) if total_assets > 0 else 0

        print("=" * 70)
        print("  📊 OIE — OPTIONS PORTFOLIO")
        print("=" * 70)
        print(f"  Seeded:     {seeded_at[:19]}")
        print(f"  Last cycle: {last_cycle[:19]}")
        print(f"  Cycles run: {cycle_count}")
        print()
        print(f"  💰 Cash pool:        ${cash:>12,.2f}")
        print(f"     Premium received: ${option_premium:>12,.2f}")
        print(f"     Cost to close:    ${option_liability:>12,.2f}")
        print(f"     Unrealized P&L:   ${option_unrealized:>+12,.2f}")
        print(f"     Realized P&L:     ${realized:>+12,.2f}")
        print(f"     Total P&L:        ${realized + option_unrealized:>+12,.2f}")
        # Cash buffer: cash as % of cash+stocks (option liability is contingent)
        print(f"     Cash buffer:      {cash_pct:>11.1f}%")
        print()

        # Stocks (reference only — for CC tracking, not screened)
        if stocks:
            print(f"\n  📈 STOCKS ({len(stocks)} tickers, reference — NOT screened)")
            print(f"  {'Ticker':<8s} {'Qty':>6s} {'Cost':>10s} {'Value':>12s}")
            print(f"  {'-'*8} {'-'*6} {'-'*10} {'-'*12}")
            for s in sorted(stocks, key=lambda x: x['ticker']):
                print(f"  {s['ticker']:<8s} {s['qty']:>6,.0f} ${s['cost_price']:>9,.2f} ${s['cost_price']*s['qty']:>11,.2f}")
            print(f"  {'─':─>8} {'─':─>6} {'─':─>10} {'─':─>12}")
            print(f"  {'Total':<8s} {'':>6s} {'':>10s} ${stock_total:>11,.2f}")

        # Net Liquidation Value = cash + stocks(at cost) - option liability
        # Cash already includes premiums received. Option liability = cost to close.
        paper_nlv = cash + stock_total - option_liability
        print(f"\n  💰 PAPER NET LIQ VALUE")
        print(f"     Cash (incl. premiums):  ${cash:>12,.2f}")
        print(f"     Stocks (at cost):       ${stock_total:>12,.2f}")
        print(f"     Option liability:       ${-option_liability:>12,.2f}")
        print(f"     {'─'*30}")
        print(f"     Paper NLV:              ${paper_nlv:>12,.2f}")

        # ── Options table (right above events) ──
        if options:
            # Capt% = percentage of premium captured. Positive = profit locked in
            # (can close for less than received), negative = underwater.
            print(f"\n  📊 OPTIONS ({len(options)})")
            print(f"  {'Ticker':<6s}  {'Type':<4s}  {'Strike':>7s}  {'Expiry':>10s}  "
                  f"{'Qty':>3s}  {'Entry':>7s}  {'Bid':>7s}  "
                  f"{'P&L':>10s}  {'Capt%':>6s}  {'Δ':>6s}  {'DTE':>3s}")
            print(f"  {'─'*6}  {'─'*4}  {'─'*7}  {'─'*10}  "
                  f"{'─'*3}  {'─'*7}  {'─'*7}  "
                  f"{'─'*10}  {'─'*6}  {'─'*6}  {'─'*3}")
            for o in sorted(options, key=lambda o: (o['ticker'], o['expiry'] or '')):
                entry = o['entry_premium'] or 0
                bid = o['current_bid'] or 0
                qty = abs(o['qty'])
                pnl = (entry - bid) * qty * 100
                profit_pct = ((entry - bid) / entry * 100) if entry > 0 else 0
                delta = o['current_delta'] or 0
                dte = o.get('dte_initial', '?')
                print(f"  {o['ticker']:<6s}  {o['pos_type']:<4s}  "
                      f"${o['strike']:>6,.0f}  {str(o['expiry'] or ''):>10s}  "
                      f"{qty:>3,.0f}  ${entry:>6,.2f}  ${bid:>6,.2f}  "
                      f"${pnl:>+9,.2f}  {profit_pct:>5.1f}%  "
                      f"{delta:>+6.3f}  {str(dte):>3s}")

        # ── Recent events table — option trades shown individually,
        #     stock seeds collapsed into one line ──
        all_trades = self.db.get_recent_events(50)
        if all_trades:
            print(f"\n  📋 RECENT EVENTS")
            print(f"  {'Date Time':<19s} {'Event':<28s} {'Ticker':<6s} {'Strike':>8s} "
                  f"{'Expiry':>12s} {'P&L':>9s}  Detail")
            print(f"  {'─'*19} {'─'*28} {'─'*6} {'─'*8} {'─'*12} {'─'*9}  {'─'*6}")

            # Separate option trades from stock/portfolio seeds
            option_trades = []
            stock_seeds = []
            portfolio_seed = None

            for e in all_trades:
                if e['event'] in ('OPEN_CALL', 'OPEN_PUT', 'CLOSE', 'EXPIRE', 'ASSIGN'):
                    option_trades.append(e)
                elif e['event'] == 'SEED':
                    if e['ticker']:
                        stock_seeds.append(e)
                    else:
                        portfolio_seed = e

            # Collapse stock seeds into one line
            if stock_seeds:
                seed_count = len(stock_seeds)
                seed_ts = (stock_seeds[0]['ts'][0:10] + ' ' + stock_seeds[0]['ts'][11:19]) if len(stock_seeds[0]['ts']) >= 19 else ''
                print(f"  {seed_ts:<19s} {'📦 SEED (stocks)':<28s} "
                      f"{'':<6s} {'—':>8s} {'—':>12s} {'—':>9s}  "
                      f"{seed_count} tickers seeded")

            if portfolio_seed:
                pts = (portfolio_seed['ts'][0:10] + ' ' + portfolio_seed['ts'][11:19]) if len(portfolio_seed['ts']) >= 19 else ''
                detail = portfolio_seed['detail'] or ''
                total_str = detail.split('=')[-1].strip() if '=' in detail else detail
                print(f"  {pts:<19s} {'📦 SEED (portfolio)':<28s} "
                      f"{'':<6s} {'—':>8s} {'—':>12s} {'—':>9s}  {total_str}")

            # Show option trades individually
            for e in option_trades:
                ts = (e['ts'][0:10] + ' ' + e['ts'][11:19]) if len(e['ts']) >= 19 else e['ts'][:8]
                event_raw = e['event']
                ticker = e['ticker'] or ''
                detail = e['detail'] or ''
                pos_id = e['pos_id']

                pos = self.db.get_position(pos_id) if pos_id else None
                pos_type = pos['pos_type'] if pos else None
                strike = pos.get('strike') if pos else None
                expiry = pos.get('expiry') if pos else None
                entry_premium = pos.get('entry_premium') if pos else None
                realized_pnl = pos.get('realized_pnl') if pos else None

                # ── Format event label ──
                if event_raw == 'OPEN_CALL':
                    event_label = '🔵 OPEN CC'
                elif event_raw == 'OPEN_PUT':
                    event_label = '🔴 OPEN CSP'
                elif event_raw == 'CLOSE':
                    reason = detail.split(':')[0] if ':' in detail else 'CLOSE'
                    if reason == 'ROLL_UP_OUT':
                        event_label = '🔄 ROLL UP & OUT (CC)'
                    elif reason == 'ROLL_DOWN_OUT':
                        event_label = '🔄 ROLL DOWN & OUT (CSP)'
                    elif reason in ('CLOSE_50PCT', 'CLOSE_TREND'):
                        event_label = '💰 CLOSE (profit)'
                    elif reason == 'STOP_DELTA':
                        event_label = '🛑 STOP (delta)'
                    elif reason == 'STOP_LOSS':
                        event_label = '🛑 STOP (loss)'
                    else:
                        event_label = f'📕 {reason}'
                elif event_raw == 'EXPIRE':
                    event_label = '📅 EXPIRE'
                elif event_raw == 'ASSIGN':
                    event_label = '📈 CC ASSIGN' if pos_type == 'CALL' else '📉 CSP ASSIGN'
                else:
                    event_label = event_raw

                strike_str = f'${strike:,.0f}' if strike else '—'
                expiry_str = str(expiry) if expiry else '—'

                if event_raw == 'CLOSE' and realized_pnl is not None:
                    pnl_str = f'${realized_pnl:+,.0f}'
                else:
                    pnl_str = '—'

                if event_raw in ('OPEN_CALL', 'OPEN_PUT'):
                    detail_str = f'premium ${entry_premium:,.2f}' if entry_premium else detail[:50]
                elif event_raw == 'CLOSE':
                    pnl_part = detail.split(': ')[-1].split(',')[0] if ': ' in detail else ''
                    try:
                        pnl_val = float(pnl_part)
                        entry_val = entry_premium or 0
                        pct = (pnl_val / (entry_val * 100)) * 100 if entry_val > 0 else 0
                        detail_str = f'{pnl_part} ({pct:+.0f}% captured)'
                    except (ValueError, ZeroDivisionError):
                        detail_str = pnl_part
                else:
                    detail_str = detail[:60]

                print(f"  {ts:<19s} {event_label:<28s} {ticker:<6s} {strike_str:>8s} "
                      f"{expiry_str:>12s} {pnl_str:>9s}  {detail_str}")

        print(f"  💡 Options P&L: ${realized + option_unrealized:,.2f}")

    def show_history(self):
        if not self.db.is_seeded():
            print("❌ Not seeded.")
            return

        snapshots = self.db.get_snapshots(50)
        if not snapshots:
            print("No snapshots yet. Run a cycle first.")
            return

        print(f"📈 OIE HISTORY ({len(snapshots)} snapshots)")
        print(f"  {'Time':<20s} {'Total':>12s} {'Cash':>12s} "
              f"{'Unreal':>10s} {'Realiz':>10s} {'#Opts':>6s}")
        print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*6}")
        for s in snapshots:
            print(f"  {s['ts'][:19]:<20s} "
                  f"${s['total_value']:>11,.2f} ${s['cash']:>11,.2f} "
                  f"${s['unrealized_pnl']:>9,.2f} ${s['realized_pnl_total']:>9,.2f} "
                  f"{s['open_positions']:>6}")


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='OIE — Options Income Engine')
    sub = parser.add_subparsers(dest='cmd', help='Command')

    sub.add_parser('test', help='Self-check — validates DB, config, scoring (no OpenD needed)')
    sub.add_parser('init', help='Seed paper portfolio from REAL account')

    run_p = sub.add_parser('run', help='Run continuously')
    run_p.add_argument('--interval', type=int, default=DEFAULT_INTERVAL_MIN,
                       help=f'Cycle interval in minutes (default: {DEFAULT_INTERVAL_MIN})')
    run_p.add_argument('--no-external', action='store_true', help='Skip yfinance')
    run_p.add_argument('--skip-closed', action='store_true', default=True,
                       help='Skip cycles when market is closed (default)')
    run_p.add_argument('--force', action='store_true',
                       help='Run even if market closed AND skip guardrails')

    once_p = sub.add_parser('once', help='Run a single cycle')
    once_p.add_argument('--no-external', action='store_true', help='Skip yfinance')
    once_p.add_argument('--dry-run', action='store_true',
                        help='Screen + guardrails without modifying DB')
    once_p.add_argument('--skip-closed', action='store_true', default=False,
                        help='Skip if market is closed')
    once_p.add_argument('--force', action='store_true',
                        help='Run even if market closed AND skip guardrails')

    sub.add_parser('status', help='Show paper portfolio status')
    sub.add_parser('history', help='Show P&L history')
    sub.add_parser('reconcile',
                   help='Sync paper STOCK rows + cash from REAL account '
                        '(non-destructive; preserves options & history)')
    reset_p = sub.add_parser('reset', help='Wipe paper portfolio')
    reset_p.add_argument('--force', action='store_true', help='Skip confirmation')

    # ── sim: manual paper trade simulation ──
    sim_p = sub.add_parser('sim', help='Simulate manual paper trades (no OpenD needed)')
    sim_sub = sim_p.add_subparsers(dest='sim_cmd', help='Action')

    sim_open = sim_sub.add_parser('open', help='Open a paper position')
    sim_open.add_argument('strategy', choices=['CSP', 'CC'], help='Strategy')
    sim_open.add_argument('ticker', help='Ticker symbol')
    sim_open.add_argument('strike', type=float, help='Strike price')
    sim_open.add_argument('expiry', help='Expiry date (YYYY-MM-DD)')
    sim_open.add_argument('--premium', type=float, required=True, help='Premium per contract')
    sim_open.add_argument('--contracts', type=int, default=1, help='Number of contracts')
    sim_open.add_argument('--delta', type=float, default=0.25, help='Option delta')
    sim_open.add_argument('--iv', type=float, default=30, help='Implied volatility %%')

    sim_close = sim_sub.add_parser('close', help='Close a paper position')
    sim_close.add_argument('pos_id', type=int, help='Position ID to close')
    sim_close.add_argument('--price', type=float, required=True, help='Exit price (buyback cost)')

    sim_expire = sim_sub.add_parser('expire', help='Expire a paper position OTM')
    sim_expire.add_argument('pos_id', type=int, help='Position ID to expire')

    sim_sub.add_parser('list', help='List all paper positions with IDs')

    args = parser.parse_args()

    # ── test command ──
    if args.cmd == 'test':
        ok = OIEEngine.run_tests()
        sys.exit(0 if ok else 1)

    engine = OIEEngine(
        no_external=getattr(args, 'no_external', False),
        dry_run=getattr(args, 'dry_run', False),
        force=getattr(args, 'force', False))

    # ── Market hours check ──
    # Single-cycle commands (`once`, etc.) exit immediately if closed.
    # `run` mode loops internally (sleeps and re-checks), so it must NOT
    # early-return here — otherwise the engine would exit on weekends/nights
    # and the whole point of a continuous daemon would be defeated.
    skip_closed = getattr(args, 'skip_closed', False)
    force = getattr(args, 'force', False)
    if skip_closed and not force and args.cmd != 'run':
        market_open, reason = is_market_open()
        if not market_open:
            print(f"⏸️  {reason}")
            print("   Use --force to override.")
            return

    if args.cmd == 'init':
        if engine.db.is_seeded():
            print("⚠️  Already seeded. Use 'reset --force' then 'init' to re-seed.")
        else:
            engine.init_portfolio()

    elif args.cmd == 'reconcile':
        engine.reconcile()

    elif args.cmd == 'run':
        if not engine.db.is_seeded():
            print("❌ Not seeded. Run 'init' first.")
            return
        interval_sec = args.interval * 60
        print(f"🔄 OIE Engine running every {args.interval} min (Ctrl+C to stop)")
        print(f"   Paper portfolio: ${float(engine.db.get_state('cash','0')) + float(engine.db.get_state('fund','0')):,.2f} "
              f"cash + {len(engine.db.get_active_options())} options")
        if skip_closed:
            print(f"   🕐 Market hours filter: ON (skips when closed)")

        global RUNNING
        while RUNNING:
            # Market hours check before each cycle
            if skip_closed and not force:
                market_open, reason = is_market_open()
                if not market_open:
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] ⏸️  {reason} — waiting...")
                    for _ in range(60):
                        if not RUNNING:
                            break
                        time.sleep(1)
                    continue

            cycle_start = datetime.now()
            result = engine.run_cycle()
            if 'error' in result:
                print(f"  ❌ Cycle failed: {result['error']}")
            else:
                ts = cycle_start.strftime('%H:%M:%S')
                print(f"  [{ts}] Cycle #{result['cycle']} | "
                      f"Value=${result['total_value']:,.2f} | "
                      f"New={result['new_trades']} Closed={result['closed_trades']} | "
                      f"Positions={result['open_positions']} | "
                      f"({result['elapsed']:.1f}s)")
                for event in result.get('events', [])[:5]:
                    print(f"    {event}")

            # Sleep remaining time
            elapsed = (datetime.now() - cycle_start).total_seconds()
            sleep_time = max(1, interval_sec - elapsed)
            for _ in range(int(sleep_time)):
                if not RUNNING:
                    break
                time.sleep(1)

        print(f"\n✅ Engine stopped. {engine.db.get_snapshot_count()} snapshots saved.")

    elif args.cmd == 'once':
        if not engine.db.is_seeded():
            print("❌ Not seeded. Run 'init' first.")
            return
        print("🔄 Running single cycle...")
        result = engine.run_cycle()
        if 'error' in result:
            print(f"❌ Failed: {result['error']}")
        else:
            print(f"\n✅ Cycle #{result['cycle']} complete "
                  f"(${result['elapsed']:.1f}s)")
            print(f"   Total:  ${result['total_value']:>12,.2f}")
            print(f"   Cash:   ${result['cash']:>12,.2f}")
            # Stocks tracked outside engine
            print(f"   Realiz: ${result['realized_pnl']:>12,.2f}")
            print(f"   Unreal: ${result['unrealized_pnl']:>12,.2f}")
            print(f"   New trades:  {result['new_trades']}")
            print(f"   Closed:      {result['closed_trades']}")
            print(f"   Positions:   {result['open_positions']}")
            if result.get('events'):
                print(f"\n   Events:")
                for e in result['events']:
                    print(f"     {e}")
        if not engine.dry_run:
            engine.show_status()

    elif args.cmd == 'status':
        engine.show_status()

    elif args.cmd == 'history':
        engine.show_history()

    elif args.cmd == 'reset':
        if not args.force:
            confirm = input("⚠️  This will DELETE all paper portfolio data. Continue? [y/N] ")
            if confirm.lower() != 'y':
                print("Aborted.")
                return
        engine.db.reset_all()
        print("✅ Paper portfolio reset. Run 'init' to re-seed.")

    elif args.cmd == 'sim':
        if not engine.db.is_seeded():
            print("❌ Not seeded. Run 'init' first (or use 'sim' without a portfolio — positions only).")
            # Auto-seed with just cash for sim mode
            engine.db.seed_portfolio({}, 50000, 0)
            print("   Auto-seeded with $50,000 cash for simulation.\n")

        if args.sim_cmd == 'open':
            pos_type = 'PUT' if args.strategy == 'CSP' else 'CALL'
            qty = -args.contracts
            dte = max(0, (date.fromisoformat(args.expiry) - date.today()).days)
            premium_total = args.premium * args.contracts * 100
            pos_id = engine.db.open_position(
                ticker=args.ticker.upper(), pos_type=pos_type, qty=qty,
                cost_price=args.strike, strike=args.strike,
                expiry=args.expiry, dte=dte,
                entry_premium=args.premium, delta=args.delta, iv=args.iv,
                cash_impact=premium_total,
                note=f'SIM: {args.strategy} ${args.strike:.0f} {args.expiry} '
                     f'prem=${args.premium:.2f}×{args.contracts}')
            # Cash already applied by the audit trail (open_position → _log_trade)
            print(f"✅ Opened {args.strategy} {args.ticker} ${args.strike:.0f} {args.expiry}")
            print(f"   Position ID: {pos_id} | Premium: ${premium_total:,.2f} | DTE: {dte}")
            engine.show_status()

        elif args.sim_cmd == 'close':
            pos = engine.db.get_position(args.pos_id)
            if not pos:
                print(f"❌ Position {args.pos_id} not found")
                return
            buyback_cost = args.price * abs(pos['qty']) * 100
            pnl = engine.db.close_position(args.pos_id, args.price, 'SIM_CLOSE',
                                           cash_impact=-buyback_cost)
            # Cash already applied by the audit trail (close_position → _log_trade)
            print(f"✅ Closed position {args.pos_id} @ ${args.price:.2f}")
            print(f"   P&L: ${pnl:,.2f} | Reason: SIM_CLOSE")
            engine.show_status()

        elif args.sim_cmd == 'expire':
            pos = engine.db.get_position(args.pos_id)
            if not pos:
                print(f"❌ Position {args.pos_id} not found")
                return
            pnl = engine.db.expire_position(args.pos_id)
            # No cash change — premium already received, no buyback needed
            print(f"✅ Expired position {args.pos_id} — full premium kept")
            print(f"   P&L: ${pnl:,.2f}")
            engine.show_status()

        elif args.sim_cmd == 'list':
            engine.show_status()

    else:
        engine.show_status()


if __name__ == '__main__':
    main()
