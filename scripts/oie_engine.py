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

# ── Import scoring functions from screener ──
from scripts.screener import (
    _compute_ticker_score, _contract_penalty, _trend_composite,
    _score_technical, _score_options_eco, _score_fundamental,
    _score_external, _score_macro, _csp_roc,
    _fetch_option_chain_resilient, _fetch_live_watchlist,
    _fetch_live_portfolio,
)

# ── Config ──
RUNNING = True
DEFAULT_INTERVAL_MIN = 10


def _signal_handler(sig, frame):
    global RUNNING
    print("\n⏸️  Shutting down after current cycle...")
    RUNNING = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ═══════════════════════════════════════════════════════════════
# ENGINE
# ═══════════════════════════════════════════════════════════════

class OIEEngine:
    """Paper trading engine — screens, executes, tracks."""

    def __init__(self, no_external: bool = False):
        self.db = OIEDB()
        self.cfg = get_config()
        self.no_external = no_external
        self.moomoo: Optional[MoomooClient] = None
        self.yf: Optional[YFinanceClient] = None

    # ═══════════════════════════════════════════════════════════
    # INIT
    # ═══════════════════════════════════════════════════════════

    def init_portfolio(self):
        """Seed paper portfolio from REAL moomoo account.
        Combines us_cash + fund into one liquid cash pool for CSP trading."""
        if self.db.is_seeded():
            print("⚠️  Paper portfolio already seeded. Use 'reset' first to re-seed.")
            return False

        print("📋 Connecting to REAL account...")
        try:
            stocks_dict, cash, bp, fund, _ = _fetch_live_portfolio()
        except Exception as e:
            print(f"❌ Failed to fetch REAL portfolio: {e}")
            return False

        if not stocks_dict:
            print("❌ No stock positions found in REAL account.")
            return False

        # Fetch actual cost basis from moomoo (single connection, all tickers)
        cost_data = self._fetch_cost_basis(stocks_dict)

        # Fetch live prices via MoomooClient for mark-to-market
        with MoomooClient() as moomoo:
            stocks = {}
            total_cost = 0
            total_market = 0
            for ticker, qty in stocks_dict.items():
                snap = moomoo.get_stock_snapshot(f'US.{ticker}')
                live_price = snap.last_price if snap and snap.last_price > 0 else 0
                cost = cost_data.get(ticker, {}).get('cost', 0)
                if cost <= 0:
                    cost = live_price  # fallback
                stocks[ticker] = {'qty': qty, 'cost': cost, 'price': live_price}
                total_cost += qty * cost
                total_market += qty * live_price

        # Combine cash + fund into one liquid pool
        # Real account has negative us_cash (margin) offset by positive fund_assets (money market)
        net_liquid = cash + fund
        if net_liquid < 0:
            net_liquid = fund  # fallback: use fund only if combined is negative

        self.db.seed_portfolio(stocks, net_liquid, 0)  # fund=0, all in cash pool
        total = total_cost + net_liquid
        print(f"\n✅ Paper portfolio seeded (all values USD):")
        print(f"   Stocks: {len(stocks)} tickers")
        print(f"     Cost basis:  ${total_cost:>12,.2f}")
        print(f"     Market value: ${total_market:>12,.2f}")
        print(f"     Unrealized:   ${total_market - total_cost:>+12,.2f}")
        for t, s in sorted(stocks.items()):
            ur = (s['price'] - s['cost']) * s['qty']
            print(f"     {t:<6s} {s['qty']:>8,.0f} sh @ ${s['cost']:>9,.2f}  now ${s['price']:>9,.2f}  "
                  f"{'△' if ur >= 0 else '▽'} ${abs(ur):,.2f}")
        print(f"   Cash pool: ${net_liquid:>12,.2f}  (real cash ${cash:,.2f} + fund ${fund:,.2f})")
        print(f"   Paper NLV: ${total_market + net_liquid:>12,.2f}")
        print(f"\n   🔒 Options: 0 (paper starts fresh — no existing option positions copied)")
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

            # ── 1. Load state ──
            cash = float(self.db.get_state('cash', '0'))
            fund = float(self.db.get_state('fund', '0'))

            # ── 2. Mark-to-market ──
            active_stocks = self.db.get_active_stocks()
            active_options = self.db.get_active_options()

            # Update stock prices
            stock_tickers = list(set(p['ticker'] for p in active_stocks))
            stock_prices = {}
            if stock_tickers:
                snaps = self.moomoo.get_stock_snapshots([f'US.{t}' for t in stock_tickers])
                for snap in snaps:
                    t = snap.ticker.replace('US.', '')
                    stock_prices[t] = snap.last_price
                    # Update current_bid in DB
                    self.db._conn.execute(
                        "UPDATE paper_positions SET current_bid=? WHERE ticker=? AND status='ACTIVE' AND pos_type='STOCK'",
                        (snap.last_price, t))

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

            # ── 3. Check exits ──
            active_options = self.db.get_active_options()  # refresh after MTM
            today = date.today()

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

                profit_captured = ((entry - current_bid) / entry * 100) if entry > 0 else 0
                close_reason = None

                # Profit targets
                if profit_captured >= 70:
                    close_reason = 'CLOSE_70PCT'
                elif profit_captured >= 50:
                    close_reason = 'CLOSE_50PCT'
                # Expiry
                elif dte <= 0:
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
                    if close_reason in ('EXPIRE',):
                        pnl = self.db.expire_position(pos_id)
                        cash += entry * qty * 100
                        events.append(f'📅 {ticker} {pos_type} ${strike:.0f} EXPIRED: +${pnl:,.2f}')
                        closed_trades += 1
                    elif close_reason in ('CC_ASSIGN',):
                        self.db.assign_position(pos_id, 'CC', stock_prices.get(ticker, strike))
                        cash += strike * qty * 100
                        events.append(f'📈 {ticker} CC ${strike:.0f} ASSIGNED: shares called away')
                        closed_trades += 1
                    elif close_reason in ('CSP_ASSIGN',):
                        self.db.assign_position(pos_id, 'CSP', stock_prices.get(ticker, strike))
                        cash -= strike * qty * 100
                        events.append(f'📉 {ticker} CSP ${strike:.0f} ASSIGNED: {qty*100} shares added')
                        closed_trades += 1
                    else:
                        # Profit target close
                        pnl = self.db.close_position(pos_id, current_bid, close_reason,
                                                     cash_impact=current_bid * qty * 100)
                        cash -= current_bid * qty * 100  # pay to close
                        events.append(f'💰 {ticker} {pos_type} ${strike:.0f} {close_reason}: '
                                    f'{profit_captured:.0f}% captured, P&L ${pnl:,.2f}')
                        closed_trades += 1

            # ── 4. Screen new opportunities ──
            self.db.set_state('cash', str(round(cash, 2)))
            self.db._conn.commit()
            candidates = self._screen_candidates(stock_prices)

            # ── 5. Apply guardrails ──
            # Re-read state after exits
            cash = float(self.db.get_state('cash', '0'))
            active_all = self.db.get_active_positions()
            open_options = self.db.get_active_options()
            daily_new = self.db.get_daily_new_count()

            total_stock_value = sum(
                (p['current_bid'] or p['cost_price'] or 0) * p['qty']
                for p in active_all if p['pos_type'] == 'STOCK'
            )
            net_liq = cash + fund + total_stock_value

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

            gc = GuardrailChecker(net_liq=net_liq, cash=cash, buying_power=cash * 2,
                                   open_positions=gc_positions,
                                   daily_order_count=daily_new)

            # ── 6. Execute ──
            executed = 0
            max_new = max(0, min(2, self.cfg.max_daily_new_positions - daily_new))
            gr = gc.check()
            if gr.blocks:
                events.append(f'🛡️ Portfolio BLOCKS: {"; ".join(gr.blocks[:2])}')

            for c in candidates:
                if executed >= max_new:
                    break

                # Per-trade concentration check: only block if THIS ticker exceeds limit
                new_notional = c.capital_required
                ticker_pct = new_notional / net_liq * 100 if net_liq > 0 else 0
                if ticker_pct > self.cfg.max_single_position_pct * 100:
                    events.append(f'🛡️ {c.ticker} {c.strategy} BLOCKED: '
                                f'{ticker_pct:.1f}% of NLV > {self.cfg.max_single_position_pct*100:.0f}% limit')
                    continue

                # Cash buffer check for CSP
                if c.strategy == 'CSP' and c.capital_required > cash * 0.8:
                    events.append(f'🛡️ {c.ticker} CSP BLOCKED: capital ${c.capital_required:,.0f} > 80% of cash ${cash:,.0f}')
                    continue

                # Full guardrail for this specific trade
                check = gc.check_new_trade(
                    c.ticker, 'CC' if c.strategy == 'CC' else 'CSP',
                    c.capital_required,
                    sector=SECTOR_MAP.get(c.ticker, 'Unknown'))
                if not check.all_clear:
                    # Only block if the block is about THIS ticker specifically
                    trade_blocks = [b for b in check.blocks
                                   if c.ticker in b or 'cash' in b.lower()]
                    if trade_blocks:
                        events.append(f'🛡️ {c.ticker} {c.strategy} BLOCKED: {"; ".join(trade_blocks[:2])}')
                        continue
                # Warn but don't block for existing portfolio issues
                if check.warnings:
                    for w in check.warnings[:1]:
                        if c.ticker not in w.lower():
                            events.append(f'⚠️ {c.ticker} {c.strategy} WARN: {w[:80]}')

                # Execute
                if c.strategy == 'CC':
                    pos_id = self.db.open_position(
                        ticker=c.ticker, pos_type='CALL', qty=-1,
                        cost_price=c.strike, strike=c.strike,
                        expiry=c.expiry, dte=c.dte,
                        entry_premium=c.bid, delta=c.delta,
                        iv=c.iv, cash_impact=c.bid * 100,
                        note=f'CC ${c.strike:.0f}x{c.expiry} Δ{c.delta:.2f} '
                             f'RoC{c.annualized_roc_pct:.1f}% Score{c.score}')
                    cash += c.bid * 100
                    events.append(f'📝 {c.ticker} CC ${c.strike:.0f} {c.expiry} '
                                f'DTE={c.dte} Δ={c.delta:.2f} '
                                f'premium=${c.bid:.2f} RoC={c.annualized_roc_pct:.1f}%')
                    new_trades += 1
                    executed += 1

                elif c.strategy == 'CSP':
                    pos_id = self.db.open_position(
                        ticker=c.ticker, pos_type='PUT', qty=-1,
                        cost_price=c.strike, strike=c.strike,
                        expiry=c.expiry, dte=c.dte,
                        entry_premium=c.bid, delta=c.delta,
                        iv=c.iv, cash_impact=c.bid * 100,
                        note=f'CSP ${c.strike:.0f}x{c.expiry} Δ{c.delta:.2f} '
                             f'RoC{c.annualized_roc_pct:.1f}% Score{c.score}')
                    cash += c.bid * 100
                    events.append(f'📝 {c.ticker} CSP ${c.strike:.0f} {c.expiry} '
                                f'DTE={c.dte} Δ={c.delta:.2f} '
                                f'premium=${c.bid:.2f} RoC={c.annualized_roc_pct:.1f}%')
                    new_trades += 1
                    executed += 1

            # ── 7. Snapshot ──
            self.db.set_state('cash', str(round(cash, 2)))
            self.db._conn.commit()

            active_all = self.db.get_active_positions()
            stock_value = sum(
                (p['current_bid'] or p['cost_price'] or 0) * p['qty']
                for p in active_all if p['pos_type'] == 'STOCK'
            )
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
            total_value = cash + fund + stock_value + unrealized

            self.db.save_snapshot(
                total_value=total_value, cash=cash,
                stock_value=stock_value, fund_value=fund,
                option_premium=option_premium,
                option_liability=option_liability,
                unrealized_pnl=unrealized, realized_pnl=realized,
                open_positions=len(active_all))

            # ── 8. Log cycle ──
            self.db.set_state('last_cycle', datetime.now().isoformat())
            cycle_num = int(self.db.get_state('cycle_count', '0')) + 1
            self.db.set_state('cycle_count', str(cycle_num))
            self.db._conn.commit()

            elapsed = (datetime.now() - cycle_start).total_seconds()
            return {
                'cycle': cycle_num,
                'elapsed': elapsed,
                'events': events,
                'new_trades': new_trades,
                'closed_trades': closed_trades,
                'total_value': total_value,
                'cash': cash,
                'stock_value': stock_value,
                'realized_pnl': realized,
                'unrealized_pnl': unrealized,
                'open_positions': len(active_all),
            }

        except Exception as e:
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

    def _screen_candidates(self, stock_prices: dict) -> list:
        """Run screener against paper portfolio. Returns ranked candidates."""
        from dataclasses import dataclass

        @dataclass
        class Candidate:
            ticker: str
            strategy: str
            score: float
            strike: float
            expiry: str
            dte: int
            delta: float
            bid: float
            iv: float
            annualized_roc_pct: float
            open_interest: int
            capital_required: float

        candidates = []
        if not self.moomoo:
            return candidates

        watchlist = _fetch_live_watchlist(self.moomoo)
        existing_options = self.db.get_open_option_tickers()
        cash = float(self.db.get_state('cash', '0'))
        fund = float(self.db.get_state('fund', '0'))

        # Macro
        regime = 'NEUTRAL'
        regime_mult = 1.0
        vix = 20.0
        if self.yf:
            try:
                macro = get_macro_context(self.yf)
                regime = macro.market_regime
                regime_mult = macro.position_mult
                vix = macro.vix or 20.0
            except Exception:
                pass

        for ticker in watchlist:
            short = ticker.replace('US.', '')

            # Skip tickers with existing option positions
            if short in existing_options:
                continue

            snap = self.moomoo.get_stock_snapshot(ticker)
            if snap is None:
                continue

            history = self.moomoo.get_price_history(ticker, 252)
            if history:
                spy_history = self.moomoo.get_price_history('US.SPY', 252)
                enrich_stock_snapshot(snap, history, spy_history)

            # Ticker score
            ticker_score = _compute_ticker_score(
                snap=snap,
                trend_composite=_trend_composite(snap),
                analyst_consensus='N/A',
                earnings_blackout=False,
                insider_sentiment='NEUTRAL',
                target_upside=None,
                news_score=50,
                regime=regime,
                regime_mult=regime_mult,
                iv_rank=50.0,
            )

            # Option chain
            contracts = _fetch_option_chain_resilient(
                self.moomoo, ticker, dte_min=7, dte_max=90)
            if not contracts:
                continue

            has_shares = self.db.get_shares(short) >= 100
            total_stock_value = sum(
                (p['current_bid'] or p['cost_price'] or 0) * p['qty']
                for p in self.db.get_active_stocks()
            )
            net_liq = cash + fund + total_stock_value

            for c in contracts:
                # Basic filters (matching screener logic)
                if c.bid <= 0 or (c.open_interest or 0) < 10 or (c.volume or 0) < 10:
                    continue
                iv_sane = c.implied_vol and 0 < c.implied_vol < 500
                if not iv_sane:
                    continue
                vrp_ok = True
                if c.implied_vol and snap.hv_30d and snap.hv_30d > 0:
                    vrp_ok = c.implied_vol > snap.hv_30d * 0.8
                if not vrp_ok:
                    continue

                abs_d = abs(c.delta or 0)

                # CSP
                if c.option_type == 'PUT' and not has_shares:  # prefer CSP on non-held tickers
                    if abs_d < 0.05 or abs_d > 0.30:
                        continue
                    roc = _csp_roc(c.bid, c.strike, c.dte)
                    if roc < self.cfg.roc_min_csp:
                        continue
                    capital = c.strike * 100
                    if capital > net_liq * 0.15:
                        continue
                    if capital > cash * 0.8:
                        continue

                    contract_score = ticker_score + _contract_penalty(c, abs_d, roc)
                    candidates.append(Candidate(
                        ticker=short, strategy='CSP',
                        score=round(contract_score, 2),
                        strike=c.strike, expiry=c.expiry, dte=c.dte,
                        delta=abs_d, bid=c.bid, iv=c.implied_vol,
                        annualized_roc_pct=round(roc, 1),
                        open_interest=c.open_interest,
                        capital_required=capital))

                # CC
                if c.option_type == 'CALL' and has_shares:
                    if c.delta < 0.15 or c.delta > 0.35:
                        continue
                    roc = (c.bid / snap.last_price) * (365.0 / c.dte) * 100 if snap.last_price and c.dte else 0
                    if roc < self.cfg.roc_min_cc:
                        continue

                    contract_score = ticker_score + _contract_penalty(c, c.delta, roc)
                    candidates.append(Candidate(
                        ticker=short, strategy='CC',
                        score=round(contract_score, 2),
                        strike=c.strike, expiry=c.expiry, dte=c.dte,
                        delta=c.delta, bid=c.bid, iv=c.implied_vol,
                        annualized_roc_pct=round(roc, 1),
                        open_interest=c.open_interest,
                        capital_required=snap.last_price * 100))

            time.sleep(0.5)  # rate limit between tickers

        # Dedup: best per ticker per strategy
        seen = set()
        deduped = []
        candidates.sort(key=lambda x: x.score)
        for c in candidates:
            key = (c.ticker, c.strategy)
            if key not in seen:
                deduped.append(c)
                seen.add(key)

        return deduped

    # ═══════════════════════════════════════════════════════════
    # STATUS
    # ═══════════════════════════════════════════════════════════

    def show_status(self):
        if not self.db.is_seeded():
            print("❌ Paper portfolio not seeded. Run 'init' first.")
            return

        cash = float(self.db.get_state('cash', '0'))
        fund = float(self.db.get_state('fund', '0'))
        seeded_at = self.db.get_state('seeded_at', 'unknown')
        last_cycle = self.db.get_state('last_cycle', 'never')
        cycle_count = int(self.db.get_state('cycle_count', '0'))

        stocks = self.db.get_active_stocks()
        options = self.db.get_active_options()

        # Stock values at mark-to-market
        stock_value = 0.0
        stock_unrealized = 0.0
        for p in stocks:
            price = p['current_bid'] or p['cost_price'] or 0
            cost = p['cost_price'] or 0
            stock_value += price * p['qty']
            stock_unrealized += (price - cost) * p['qty']

        # Option values
        option_premium = sum((p['entry_premium'] or 0) * abs(p['qty']) * 100
                            for p in options)
        option_liability = sum((p['current_bid'] or 0) * abs(p['qty']) * 100
                              for p in options)
        option_unrealized = option_premium - option_liability
        realized = self.db.get_closed_pnl()
        total_unrealized = stock_unrealized + option_unrealized
        total = cash + fund + stock_value + option_unrealized

        print("=" * 70)
        print("  📊 OIE PAPER PORTFOLIO")
        print("=" * 70)
        print(f"  Seeded:     {seeded_at[:19]}")
        print(f"  Last cycle: {last_cycle[:19]}")
        print(f"  Cycles run: {cycle_count}")
        print()
        print(f"  💰 Net Liq Value:   ${total:>12,.2f}")
        print(f"     Cash pool:       ${cash:>12,.2f}")
        print(f"     Stock value:     ${stock_value:>12,.2f}")
        print(f"     Option prem rcvd:${option_premium:>12,.2f}")
        print(f"     Option to close: ${option_liability:>12,.2f}")
        print(f"     Unrealized P&L:  ${total_unrealized:>+12,.2f}  (stock ${stock_unrealized:>+.2f} + options ${option_unrealized:>+.2f})")
        print(f"     Realized P&L:    ${realized:>+12,.2f}")
        cash_pct = (cash / total * 100) if total > 0 else 0
        print(f"     Cash buffer:     {cash_pct:>11.1f}%")
        print()

        if stocks:
            print(f"  📈 STOCKS ({len(stocks)})")
            print(f"  {'Ticker':<8s} {'Qty':>8s} {'Cost':>10s} {'Price':>10s} "
                  f"{'MktVal':>12s} {'Unreal':>10s}")
            print(f"  {'-'*8} {'-'*8} {'-'*10} {'-'*10} {'-'*12} {'-'*10}")
            for s in sorted(stocks, key=lambda s: -(s['current_bid'] or s['cost_price'] or 0) * s['qty']):
                cost = s['cost_price'] or 0
                price = s['current_bid'] or cost
                mv = price * s['qty']
                ur = (price - cost) * s['qty']
                print(f"  {s['ticker']:<8s} {s['qty']:>8,.0f} "
                      f"${cost:>9,.2f} ${price:>9,.2f} "
                      f"${mv:>11,.2f} ${ur:>+9,.2f}")

        if options:
            print(f"\n  📊 OPTIONS ({len(options)})")
            print(f"  {'Ticker':<6s} {'Type':>5s} {'Strike':>8s} {'Expiry':>12s} "
                  f"{'Qty':>5s} {'Entry':>8s} {'Bid':>8s} {'P&L':>10s} {'Cap%':>7s} {'Δ':>6s}")
            print(f"  {'-'*6} {'-'*5} {'-'*8} {'-'*12} {'-'*5} {'-'*8} {'-'*8} {'-'*10} {'-'*7} {'-'*6}")
            for o in sorted(options, key=lambda o: (o['ticker'], o['expiry'] or '')):
                entry = o['entry_premium'] or 0
                bid = o['current_bid'] or 0
                qty = abs(o['qty'])
                pnl = (entry - bid) * qty * 100
                profit_pct = ((entry - bid) / entry * 100) if entry > 0 else 0
                delta = o['current_delta'] or 0
                dte = o.get('dte_initial', '?')
                print(f"  {o['ticker']:<6s} {o['pos_type']:>5s} ${o['strike']:>7,.2f} "
                      f"{o['expiry'] or '':>12s} {qty:>4,.0f} "
                      f"${entry:>7,.2f} ${bid:>7,.2f} ${pnl:>+9,.2f} "
                      f"{profit_pct:>6.1f}% {delta:>+5.3f}")

        # Recent events
        events = self.db.get_recent_events(8)
        if events:
            print(f"\n  📋 RECENT EVENTS")
            for e in events:
                ticker_str = f"[{e['ticker']}]" if e['ticker'] else ''
                print(f"  [{e['ts'][:19]}] {e['event']:12s} {ticker_str} {e['detail'][:100]}")
            print()

        print(f"  💡 Total P&L: realized ${realized:,.2f} + unrealized ${total_unrealized:,.2f} = ${realized + total_unrealized:,.2f}")

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
              f"{'Stocks':>12s} {'Unreal':>10s} {'Realiz':>10s} {'#Pos':>5s}")
        print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*5}")
        for s in snapshots:
            print(f"  {s['ts'][:19]:<20s} "
                  f"${s['total_value']:>11,.2f} ${s['cash']:>11,.2f} "
                  f"${s['stock_value']:>11,.2f} "
                  f"${s['unrealized_pnl']:>9,.2f} ${s['realized_pnl_total']:>9,.2f} "
                  f"{s['open_positions']:>5}")


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='OIE — Options Income Engine')
    sub = parser.add_subparsers(dest='cmd', help='Command')

    sub.add_parser('init', help='Seed paper portfolio from REAL account')
    run_p = sub.add_parser('run', help='Run continuously')
    run_p.add_argument('--interval', type=int, default=DEFAULT_INTERVAL_MIN,
                       help=f'Cycle interval in minutes (default: {DEFAULT_INTERVAL_MIN})')
    run_p.add_argument('--no-external', action='store_true',
                       help='Skip yfinance (faster)')
    sub.add_parser('once', help='Run a single cycle')
    sub.add_parser('status', help='Show paper portfolio status')
    sub.add_parser('history', help='Show P&L history')
    reset_p = sub.add_parser('reset', help='Wipe paper portfolio')
    reset_p.add_argument('--force', action='store_true', help='Skip confirmation')

    args = parser.parse_args()
    engine = OIEEngine(no_external=getattr(args, 'no_external', False))

    if args.cmd == 'init':
        if engine.db.is_seeded():
            print("⚠️  Already seeded. Use 'reset --force' then 'init' to re-seed.")
        else:
            engine.init_portfolio()

    elif args.cmd == 'run':
        if not engine.db.is_seeded():
            print("❌ Not seeded. Run 'init' first.")
            return
        interval_sec = args.interval * 60
        print(f"🔄 OIE Engine running every {args.interval} min (Ctrl+C to stop)")
        print(f"   Paper portfolio: ${float(engine.db.get_state('cash','0')) + float(engine.db.get_state('fund','0')):,.2f} "
              f"cash + {len(engine.db.get_active_stocks())} stocks\n")

        global RUNNING
        while RUNNING:
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
            # Check for stop signal in small chunks
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
            print(f"   Stocks: ${result['stock_value']:>12,.2f}")
            print(f"   Realiz: ${result['realized_pnl']:>12,.2f}")
            print(f"   Unreal: ${result['unrealized_pnl']:>12,.2f}")
            print(f"   New trades:  {result['new_trades']}")
            print(f"   Closed:      {result['closed_trades']}")
            print(f"   Positions:   {result['open_positions']}")
            if result.get('events'):
                print(f"\n   Events:")
                for e in result['events']:
                    print(f"     {e}")
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

    else:
        # Default: show status
        engine.show_status()


if __name__ == '__main__':
    main()
