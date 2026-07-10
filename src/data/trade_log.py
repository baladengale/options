"""
Trade Log — SQLite P&L tracker for backtesting, paper, and live trading.

Records trade recommendations and execution fills. Supports 3 modes:
  - BACKTEST: local simulation with historical data
  - PAPER: moomoo paper trading (TrdEnv.SIMULATE)
  - LIVE: recommendations only (no auto-submission), fills logged manually

Usage:
    from src.data.trade_log import TradeLog
    log = TradeLog()

    # Record a recommendation (screener/portfolio_check output)
    log.log_recommendation('AAPL', 'CSP', 300.0, '2026-08-21', 0.18,
                           2.50, roc_pct=15.2, score=2.35)

    # Record execution (paper or manual live fill)
    log.log_fill('AAPL', 'CSP', 300.0, '2026-08-21', 2.50, 2,
                 fill_price=2.45, mode='PAPER')

    # Record close/expiry
    log.log_close('AAPL', 300.0, '2026-08-21', close_price=0.00, reason='EXPIRED')

    # Query P&L
    summary = log.get_summary()
    log.close()
"""

import sqlite3
import os
import re
import logging
from datetime import date, datetime
from typing import Optional

logging.getLogger('FTConsoleLog').setLevel(logging.WARNING)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'db', 'options.db')


class TradeLog:
    """SQLite trade journal for backtesting, paper, and live trading."""

    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL CHECK(strategy IN ('CC', 'CSP', 'COVERED_CALL', 'CASH_SECURED_PUT')),
                strike REAL NOT NULL,
                expiry TEXT NOT NULL,
                dte INTEGER,
                delta_at_entry REAL,
                premium_received REAL NOT NULL,
                contracts INTEGER DEFAULT 1,
                roc_pct REAL,
                score REAL,
                mode TEXT NOT NULL DEFAULT 'PAPER' CHECK(mode IN ('BACKTEST', 'PAPER', 'LIVE')),
                status TEXT NOT NULL DEFAULT 'RECOMMENDED'
                    CHECK(status IN ('RECOMMENDED', 'FILLED', 'CLOSED', 'EXPIRED', 'ASSIGNED', 'REJECTED')),
                fill_price REAL,
                close_price REAL,
                close_reason TEXT,
                pnl REAL,
                pnl_pct REAL,
                capital_required REAL,
                logged_at TEXT NOT NULL,
                filled_at TEXT,
                closed_at TEXT,
                notes TEXT
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_ticker ON trade_log(ticker)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_status ON trade_log(status)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_date ON trade_log(logged_at)")
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ═══════════════════════════════════════════════════════════
    # RECOMMENDATION — screener/portfolio_check output
    # ═══════════════════════════════════════════════════════════

    def log_recommendation(self, ticker: str, strategy: str, strike: float,
                           expiry: str, delta: float, premium: float,
                           contracts: int = 1, roc_pct: float = 0,
                           dte: int = 0, score: float = 0,
                           capital_req: float = 0,
                           mode: str = 'PAPER', notes: str = '') -> int:
        """Log a trade recommendation. Returns row id."""
        now = datetime.now().isoformat()
        cur = self._conn.execute("""
            INSERT INTO trade_log (ticker, strategy, strike, expiry, dte,
                delta_at_entry, premium_received, contracts, roc_pct, score,
                mode, status, capital_required, logged_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RECOMMENDED', ?, ?, ?)
        """, (ticker, strategy, strike, expiry, dte, delta, premium,
              contracts, roc_pct, score, mode, capital_req, now, notes))
        self._conn.commit()
        return cur.lastrowid

    # ═══════════════════════════════════════════════════════════
    # FILL — trade executed
    # ═══════════════════════════════════════════════════════════

    def log_fill(self, ticker: str, strategy: str, strike: float,
                 expiry: str, premium: float, contracts: int = 1,
                 fill_price: Optional[float] = None,
                 mode: str = 'PAPER', recommendation_id: Optional[int] = None) -> int:
        """Log an executed trade. Returns row id."""
        now = datetime.now().isoformat()
        fill = fill_price if fill_price is not None else premium

        if recommendation_id:
            self._conn.execute("""
                UPDATE trade_log SET status='FILLED', fill_price=?, filled_at=?
                WHERE id=?
            """, (fill, now, recommendation_id))
            self._conn.commit()
            return recommendation_id

        cur = self._conn.execute("""
            INSERT INTO trade_log (ticker, strategy, strike, expiry,
                premium_received, contracts, fill_price, mode, status,
                logged_at, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'FILLED', ?, ?)
        """, (ticker, strategy, strike, expiry, premium, contracts,
              fill, mode, now, now))
        self._conn.commit()
        return cur.lastrowid

    # ═══════════════════════════════════════════════════════════
    # CLOSE — trade resolved
    # ═══════════════════════════════════════════════════════════

    def log_close(self, ticker: str, strike: float, expiry: str,
                  close_price: float = 0.0, reason: str = 'EXPIRED',
                  row_id: Optional[int] = None) -> bool:
        """
        Close a trade. Computes P&L automatically.
        - EXPIRED worthless → close_price=0, P&L = premium_received
        - CLOSED early → close_price is buyback cost, P&L = premium - close_price
        - ASSIGNED → close_price is the effective cost, P&L computed accordingly
        """
        now = datetime.now().isoformat()

        if row_id:
            rows = [(row_id,)]
        else:
            # Find matching open trade
            rows = self._conn.execute("""
                SELECT id FROM trade_log
                WHERE ticker=? AND strike=? AND expiry=? AND status IN ('FILLED', 'RECOMMENDED')
                ORDER BY logged_at DESC LIMIT 1
            """, (ticker, strike, expiry)).fetchall()

        if not rows:
            return False

        row_id = rows[0][0]
        trade = self._conn.execute(
            "SELECT premium_received, contracts, fill_price, strategy FROM trade_log WHERE id=?",
            (row_id,)
        ).fetchone()

        if not trade:
            return False

        premium, contracts, fill_px, strategy = trade
        contracts = contracts or 1
        entry_px = fill_px if fill_px else premium

        # P&L for short options: premium_received - close_price
        # Per contract × 100 shares × contracts
        pnl = (entry_px - close_price) * 100 * contracts
        capital = strike * 100 * contracts if strategy == 'CASH_SECURED_PUT' else premium * 100 * contracts
        pnl_pct = (pnl / capital * 100) if capital > 0 else 0

        self._conn.execute("""
            UPDATE trade_log SET status=?, close_price=?, close_reason=?,
                pnl=?, pnl_pct=?, closed_at=?
            WHERE id=?
        """, ('CLOSED' if reason != 'EXPIRED' else 'EXPIRED',
              close_price, reason, round(pnl, 2), round(pnl_pct, 2), now, row_id))
        self._conn.commit()
        return True

    # ═══════════════════════════════════════════════════════════
    # QUERY
    # ═══════════════════════════════════════════════════════════

    def get_open_trades(self) -> list[dict]:
        """All currently open trades (FILLED status)."""
        return self._fetch("""
            SELECT * FROM trade_log WHERE status = 'FILLED'
            ORDER BY logged_at DESC
        """)

    def get_recommendations(self, limit: int = 20) -> list[dict]:
        """Recent recommendations not yet acted on."""
        return self._fetch(f"""
            SELECT * FROM trade_log WHERE status = 'RECOMMENDED'
            ORDER BY logged_at DESC LIMIT {limit}
        """)

    def get_summary(self, mode: Optional[str] = None) -> dict:
        """Portfolio-level P&L summary."""
        where = "AND mode = ?" if mode else ""
        params = (mode,) if mode else ()

        total = self._conn.execute(f"""
            SELECT COUNT(*), SUM(pnl), AVG(pnl), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)
            FROM trade_log WHERE status IN ('CLOSED', 'EXPIRED', 'ASSIGNED') {where}
        """, params).fetchone()

        open_count = self._conn.execute(f"""
            SELECT COUNT(*) FROM trade_log WHERE status = 'FILLED' {where}
        """, params).fetchone()[0]

        return {
            'total_trades': total[0] or 0,
            'total_pnl': round(total[1] or 0, 2),
            'avg_pnl': round(total[2] or 0, 2),
            'win_count': total[3] or 0,
            'win_rate': round((total[3] or 0) / max(1, total[0] or 1) * 100, 1),
            'open_trades': open_count,
        }

    def get_pnl_by_ticker(self, mode: Optional[str] = None) -> list[dict]:
        """P&L broken down by ticker."""
        where = "AND mode = ?" if mode else ""
        params = (mode,) if mode else ()
        rows = self._conn.execute(f"""
            SELECT ticker, COUNT(*) as cnt, SUM(pnl) as total_pnl, AVG(pnl) as avg_pnl
            FROM trade_log WHERE status IN ('CLOSED', 'EXPIRED', 'ASSIGNED') {where}
            GROUP BY ticker ORDER BY total_pnl DESC
        """, params).fetchall()
        return [{'ticker': r[0], 'trades': r[1], 'total_pnl': round(r[2] or 0, 2),
                 'avg_pnl': round(r[3] or 0, 2)} for r in rows]

    def get_pnl_timeline(self, days: int = 90) -> list[dict]:
        """Daily P&L timeline for charting."""
        rows = self._conn.execute(f"""
            SELECT DATE(COALESCE(closed_at, logged_at)) as day, SUM(pnl) as daily_pnl
            FROM trade_log WHERE status IN ('CLOSED', 'EXPIRED', 'ASSIGNED')
            AND closed_at >= DATE('now', '-{days} days')
            GROUP BY day ORDER BY day
        """).fetchall()
        return [{'date': r[0], 'pnl': round(r[1] or 0, 2)} for r in rows]

    def _fetch(self, query: str, params: tuple = ()) -> list[dict]:
        rows = self._conn.execute(query, params).fetchall()
        if rows:
            cols = [d[0] for d in self._conn.execute(f"SELECT * FROM ({query}) WHERE 1=0", params).description]
        else:
            cols = []
        return [dict(zip(cols, r)) for r in rows]


# ═══════════════════════════════════════════════════════════════
# INTEGRATION: Log screener/portfolio_check output
# ═══════════════════════════════════════════════════════════════

def log_screener_picks(candidates: list, mode: str = 'PAPER') -> int:
    """Log top screener picks to trade log. Returns count logged."""
    with TradeLog() as log:
        count = 0
        for c in candidates[:10]:  # Top 10 only
            log.log_recommendation(
                ticker=c.ticker, strategy=c.strategy,
                strike=c.strike, expiry=c.expiry,
                delta=c.delta, premium=c.bid,
                contracts=1, roc_pct=c.annualized_roc_pct,
                dte=c.dte, score=c.score,
                capital_req=c.capital_required,
                mode=mode,
            )
            count += 1
        return count


class DailyRunDB:
    """Persistent storage for daily runs — signals, positions, chain snapshots."""

    def __init__(self, db_path: str = DB_PATH.replace('trade_log.db', 'options.db')):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                run_at TEXT NOT NULL,
                vix REAL, vvix REAL, dxy REAL,
                treasury_10y REAL, yield_spread_10y2y REAL,
                credit_spread REAL, regime TEXT, regime_score INTEGER,
                position_mult REAL, fear_greed INTEGER,
                notes TEXT
            );
            CREATE TABLE IF NOT EXISTS run_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES daily_runs(run_id),
                ticker TEXT NOT NULL, strategy TEXT NOT NULL,
                strike REAL, expiry TEXT, dte INTEGER,
                delta REAL, bid REAL, roc_pct REAL,
                iv REAL, open_interest INTEGER, score REAL,
                reason TEXT, logged_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES daily_runs(run_id),
                code TEXT NOT NULL, pos_type TEXT NOT NULL,
                strike REAL, expiry TEXT, qty REAL,
                price REAL, score REAL, action TEXT,
                roc_pct REAL, profit_captured REAL,
                logged_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_chains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES daily_runs(run_id),
                ticker TEXT NOT NULL,
                strike REAL, expiry TEXT, dte INTEGER,
                option_type TEXT, bid REAL, ask REAL,
                delta REAL, gamma REAL, theta REAL, vega REAL,
                iv REAL, open_interest INTEGER, volume INTEGER,
                logged_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_run ON run_signals(run_id);
            CREATE INDEX IF NOT EXISTS idx_positions_run ON run_positions(run_id);
            CREATE INDEX IF NOT EXISTS idx_chains_run ON run_chains(run_id, ticker);
        """)
        # Add detail column if not present (safe migration)
        try:
            self._conn.execute("ALTER TABLE run_signals ADD COLUMN detail TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE run_positions ADD COLUMN detail TEXT")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def close(self):
        self._conn.commit()
        self._conn.close()

    def log_daily_run(self, run_id: str, macro: dict):
        """Upsert daily run. Clears old signals/positions for this run_id on re-run."""
        now = datetime.now().isoformat()
        # Clear old entries for this run_id (re-run = fresh data)
        self._conn.execute("DELETE FROM run_signals WHERE run_id=?", (run_id,))
        self._conn.execute("DELETE FROM run_positions WHERE run_id=?", (run_id,))
        self._conn.execute("DELETE FROM run_chains WHERE run_id=?", (run_id,))
        self._conn.execute("""
            INSERT OR REPLACE INTO daily_runs
            (run_id, run_at, vix, vvix, dxy, treasury_10y, yield_spread_10y2y,
             credit_spread, regime, regime_score, position_mult, fear_greed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, now, macro.get('vix'), macro.get('vvix'), macro.get('dxy'),
              macro.get('treasury_10y'), macro.get('yield_spread_10y2y'),
              macro.get('credit_spread'), macro.get('regime'),
              macro.get('regime_score'), macro.get('position_mult'),
              macro.get('fear_greed')))
        self._conn.commit()

    def log_run_signal(self, run_id, ticker, strategy, strike, expiry, dte,
                       delta, bid, roc, iv, oi, score, reason,
                       detail: Optional[dict] = None):
        import json
        self._conn.execute("""
            INSERT INTO run_signals
            (run_id, ticker, strategy, strike, expiry, dte, delta, bid,
             roc_pct, iv, open_interest, score, reason, detail, logged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, ticker, strategy, strike, expiry, dte, delta, bid,
              roc, iv, oi, score, reason,
              json.dumps(detail) if detail else None,
              datetime.now().isoformat()))
        self._conn.commit()

    def log_run_position(self, run_id, code, pos_type, strike, expiry, qty,
                         price, score, action, roc_pct=None, profit_captured=None,
                         detail: Optional[dict] = None):
        import json
        self._conn.execute("""
            INSERT INTO run_positions
            (run_id, code, pos_type, strike, expiry, qty, price, score,
             action, roc_pct, profit_captured, detail, logged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, code, pos_type, strike, expiry, qty, price, score,
              action, roc_pct, profit_captured,
              json.dumps(detail) if detail else None,
              datetime.now().isoformat()))
        self._conn.commit()

    def log_run_chain(self, run_id: str, ticker: str, contracts: list[dict]):
        now = datetime.now().isoformat()
        rows = [(run_id, ticker, c['strike'], c['expiry'], c.get('dte'),
                 c.get('type', c.get('option_type', '')), c['bid'], c['ask'],
                 c.get('delta'), c.get('gamma'), c.get('theta'), c.get('vega'),
                 c.get('iv'), c.get('oi', c.get('open_interest')),
                 c.get('volume'), now)
                for c in contracts]
        self._conn.executemany("""
            INSERT INTO run_chains
            (run_id, ticker, strike, expiry, dte, option_type, bid, ask,
             delta, gamma, theta, vega, iv, open_interest, volume, logged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        self._conn.commit()

    def prune_old_runs(self, keep_days: int = 7):
        """Delete signals and positions older than N days. Keep daily_runs metadata."""
        cutoff = (date.today() - __import__('datetime').timedelta(days=keep_days)).isoformat()
        self._conn.execute("DELETE FROM run_signals WHERE logged_at < ?", (cutoff,))
        self._conn.execute("DELETE FROM run_positions WHERE logged_at < ?", (cutoff,))
        self._conn.commit()

    def prune_old_chains(self, keep_days: int = 30):
        cutoff = (date.today() - __import__('datetime').timedelta(days=keep_days)).isoformat()
        self._conn.execute("DELETE FROM run_chains WHERE logged_at < ?", (cutoff,))
        self._conn.commit()

    def map_orders_to_recommendations(self, mode: str = 'PAPER') -> int:
        """
        Pull today's executed orders from moomoo, match to trade_log recommendations
        by ticker + strategy + strike + expiry. Update status from RECOMMENDED to FILLED.
        Returns count of matched orders.
        """
        from moomoo import OpenSecTradeContext, TrdEnv, RET_OK as M_OK
        mapped = 0
        try:
            trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)
            ret, acc_list = trd.get_acc_list()
            if ret != M_OK:
                trd.close()
                return 0

            for _, acc in acc_list.iterrows():
                trd_env_raw = str(acc.get('trd_env', ''))
                if mode == 'PAPER' and trd_env_raw != 'SIMULATE':
                    continue
                if mode == 'LIVE' and trd_env_raw == 'SIMULATE':
                    continue

                acc_id = acc['acc_id']
                trd_env = TrdEnv.SIMULATE if mode == 'PAPER' else TrdEnv.REAL

                ret2, orders = trd.order_list_query(
                    trd_env=trd_env, acc_id=acc_id, refresh_cache=True)
                if ret2 != M_OK or orders is None or len(orders) == 0:
                    continue

                for _, o in orders.iterrows():
                    status = str(o.get('order_status', ''))
                    if status != 'FILLED_ALL':
                        continue

                    code = str(o.get('code', ''))
                    qty = o.get('qty', 0)
                    price = o.get('price', 0) or o.get('dealt_avg_price', 0)

                    # Parse option code to extract ticker, strike, expiry
                    parts = __import__('re').match(
                        r'US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)', code)
                    if not parts:
                        continue

                    ticker = parts.group(1)
                    yr, mo, dy = parts.group(2), parts.group(3), parts.group(4)
                    opt_type = 'CALL' if parts.group(5) == 'C' else 'PUT'
                    strategy = 'CC' if opt_type == 'CALL' else 'CSP'
                    strike = float(parts.group(6)) / 1000
                    expiry = f'20{yr}-{mo}-{dy}'

                    # Find matching recommendation
                    rec = self._conn.execute("""
                        SELECT id FROM trade_log
                        WHERE ticker=? AND strategy=? AND strike=? AND expiry=?
                        AND status='RECOMMENDED' AND mode=?
                        ORDER BY logged_at DESC LIMIT 1
                    """, (ticker, strategy, strike, expiry, mode)).fetchone()

                    if rec:
                        now = __import__('datetime').datetime.now().isoformat()
                        self._conn.execute("""
                            UPDATE trade_log SET status='FILLED', fill_price=?,
                            filled_at=?, contracts=?
                            WHERE id=?
                        """, (price, now, abs(int(qty)), rec[0]))
                        self._conn.commit()
                        mapped += 1

            trd.close()
        except Exception:
            pass
        return mapped

    def get_recent_runs(self, limit: int = 10) -> list[dict]:
        rows = self._conn.execute("""
            SELECT * FROM daily_runs ORDER BY run_at DESC LIMIT ?
        """, (limit,)).fetchall()
        cols = ['id', 'run_id', 'run_at', 'vix', 'vvix', 'dxy', 'treasury_10y',
                'yield_spread_10y2y', 'credit_spread', 'regime', 'regime_score',
                'position_mult', 'fear_greed', 'notes']
        return [dict(zip(cols, r)) for r in rows]

    def get_run_signals(self, run_id: str) -> list[dict]:
        rows = self._conn.execute("""
            SELECT * FROM run_signals WHERE run_id=? ORDER BY score
        """, (run_id,)).fetchall()
        cols = ['id', 'run_id', 'ticker', 'strategy', 'strike', 'expiry',
                'dte', 'delta', 'bid', 'roc_pct', 'iv', 'open_interest',
                'score', 'reason', 'logged_at']
        return [dict(zip(cols, r)) for r in rows]

    def get_run_positions(self, run_id: str) -> list[dict]:
        rows = self._conn.execute("""
            SELECT * FROM run_positions WHERE run_id=?
        """, (run_id,)).fetchall()
        cols = ['id', 'run_id', 'code', 'pos_type', 'strike', 'expiry',
                'qty', 'price', 'score', 'action', 'roc_pct',
                'profit_captured', 'logged_at']
        return [dict(zip(cols, r)) for r in rows]

    def _get_option_positions_codes(self) -> list[str]:
        """Get option position codes from moomoo trade context."""
        from moomoo import OpenSecTradeContext, TrdEnv, RET_OK as M_RET_OK
        codes = []
        try:
            trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)
            ret, acc_list = trd.get_acc_list()
            if ret == M_RET_OK:
                for _, acc in acc_list.iterrows():
                    if str(acc.get('trd_env', '')) == 'SIMULATE':
                        continue
                    ret2, pos = trd.position_list_query(
                        trd_env=TrdEnv.REAL, acc_id=acc['acc_id'], refresh_cache=True)
                    if ret2 == RET_OK and pos is not None:
                        for _, p in pos.iterrows():
                            if re.search(r'\d{6}[CP]\d+', p['code']):
                                codes.append(p['code'])
            trd.close()
        except Exception:
            pass
        return codes

    def _get_option_positions(self) -> dict:
        """Get option position details from moomoo trade context."""
        opts = {}
        try:
            trd = OpenSecTradeContext(host='127.0.0.1', port=11111, ai_type=1)
            ret, acc_list = trd.get_acc_list()
            if ret == M_RET_OK:
                for _, acc in acc_list.iterrows():
                    if str(acc.get('trd_env', '')) == 'SIMULATE':
                        continue
                    ret2, pos = trd.position_list_query(
                        trd_env=TrdEnv.REAL, acc_id=acc['acc_id'], refresh_cache=True)
                    if ret2 == RET_OK and pos is not None:
                        for _, p in pos.iterrows():
                            code = p['code']
                            if not re.search(r'\d{6}[CP]\d+', code):
                                continue
                            parts = re.match(r'US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)', code)
                            ticker, opt_type, strike_val, expiry_str = '', '', 0.0, ''
                            if parts:
                                ticker = parts.group(1)
                                yr, mo, dy = parts.group(2), parts.group(3), parts.group(4)
                                opt_type = 'CALL' if parts.group(5) == 'C' else 'PUT'
                                strike_val = float(parts.group(6)) / 1000
                                expiry_str = f'20{yr}-{mo}-{dy}'
                            opts[code] = {
                                'ticker': ticker, 'type': opt_type,
                                'strike': strike_val, 'expiry': expiry_str,
                                'qty': p['qty'],
                                'cost': p.get('cost_price', 0) or 0,
                                'pl': p.get('pl_val', 0) or 0,
                            }
            trd.close()
        except Exception:
            pass
        return opts


if __name__ == '__main__':
    import sys
    # Quick test
    if '--summary' in sys.argv:
        with TradeLog() as log:
            s = log.get_summary()
            print(f"Total trades: {s['total_trades']} | Total P&L: ${s['total_pnl']:,.2f} | "
                  f"Win rate: {s['win_rate']:.1f}% | Open: {s['open_trades']}")

            print("\nBy ticker:")
            for t in log.get_pnl_by_ticker():
                print(f"  {t['ticker']:6s}: {t['trades']} trades, P&L ${t['total_pnl']:>10,.2f}")

            open_trades = log.get_open_trades()
            if open_trades:
                print(f"\nOpen trades ({len(open_trades)}):")
                for t in open_trades:
                    print(f"  {t['ticker']:6s} {t['strategy']:18s} ${t['strike']:>8,.2f} "
                          f"{t['expiry']} DTE={t['dte']} premium=${t['premium_received']}")
    else:
        print("Trade Log ready. Use --summary to view, or import TradeLog.")
        print(f"  DB: {DB_PATH}")
