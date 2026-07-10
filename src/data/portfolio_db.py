"""
Local portfolio database — schema + CRUD.

Tables:
    portfolio_snapshots  — fund snapshots polled from moomoo REAL account
    positions            — stock + option positions polled from REAL account
    local_trades         — trade journal (our buy/sell decisions, tracked offline)

DB path: db/options.db
"""

import sqlite3
import os
from datetime import date, datetime
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'db', 'options.db')


class PortfolioDB:
    """Local portfolio mirror — read from moomoo, write to SQLite."""

    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ═══════════════════════════════════════════════════════════
    # SCHEMA
    # ═══════════════════════════════════════════════════════════

    def _init_schema(self):
        self._conn.executescript("""
            -- Fund snapshots from moomoo accinfo_query
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                synced_at TEXT NOT NULL,
                acc_id INTEGER,
                total_assets REAL,
                cash REAL,
                buying_power REAL,
                stock_value REAL,
                currency TEXT
            );

            -- Position snapshots from moomoo position_list_query
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                synced_at TEXT NOT NULL,
                code TEXT NOT NULL,
                pos_type TEXT NOT NULL,
                qty REAL,
                cost_price REAL,
                current_price REAL,
                market_value REAL,
                pl_val REAL,
                pl_pct REAL,
                strike REAL,
                expiry TEXT
            );

            -- Local trade journal — our decisions, not submitted to moomoo
            CREATE TABLE IF NOT EXISTS local_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                strategy TEXT,
                strike REAL,
                expiry TEXT,
                dte INTEGER,
                contracts INTEGER DEFAULT 1,
                entry_price REAL,
                entry_date TEXT,
                exit_price REAL,
                exit_date TEXT,
                pnl REAL,
                status TEXT DEFAULT 'OPEN',
                notes TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_date
                ON portfolio_snapshots(synced_at);
            CREATE INDEX IF NOT EXISTS idx_positions_date
                ON positions(synced_at);
            CREATE INDEX IF NOT EXISTS idx_positions_code
                ON positions(code);
            CREATE INDEX IF NOT EXISTS idx_trades_status
                ON local_trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_ticker
                ON local_trades(ticker);
        """)
        self._conn.commit()

    # ═══════════════════════════════════════════════════════════
    # FUND SNAPSHOTS
    # ═══════════════════════════════════════════════════════════

    def save_funds(self, acc_id: int, funds: dict):
        """Insert a fund snapshot."""
        now = datetime.now().isoformat()
        self._conn.execute("""
            INSERT INTO portfolio_snapshots
            (synced_at, acc_id, total_assets, cash, buying_power,
             stock_value, currency)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (now, acc_id,
              funds.get('total_assets'), funds.get('cash'),
              funds.get('buying_power'), funds.get('stock_value'),
              funds.get('currency', 'USD')))
        self._conn.commit()

    def get_latest_funds(self) -> Optional[dict]:
        """Most recent fund snapshot."""
        row = self._conn.execute("""
            SELECT * FROM portfolio_snapshots
            ORDER BY synced_at DESC LIMIT 1
        """).fetchone()
        return dict(row) if row else None

    def get_funds_history(self, days: int = 30) -> list[dict]:
        """Fund snapshots over time."""
        rows = self._conn.execute("""
            SELECT synced_at, total_assets, cash, stock_value
            FROM portfolio_snapshots
            WHERE synced_at >= DATE('now', ?)
            ORDER BY synced_at
        """, (f'-{days} days',)).fetchall()
        return [dict(r) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # POSITIONS
    # ═══════════════════════════════════════════════════════════

    def save_positions(self, positions: list[dict]):
        """Replace current positions with new snapshot."""
        now = datetime.now().isoformat()
        # Delete previous snapshot
        self._conn.execute("DELETE FROM positions")
        for p in positions:
            self._conn.execute("""
                INSERT INTO positions
                (synced_at, code, pos_type, qty, cost_price, current_price,
                 market_value, pl_val, pl_pct, strike, expiry)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (now, p['code'], p['pos_type'], p['qty'],
                  p.get('cost_price'), p.get('current_price'),
                  p.get('market_value'), p.get('pl_val'), p.get('pl_pct'),
                  p.get('strike'), p.get('expiry')))
        self._conn.commit()

    def get_positions(self) -> list[dict]:
        """Current positions (latest snapshot)."""
        rows = self._conn.execute("""
            SELECT * FROM positions ORDER BY pos_type, code
        """).fetchall()
        return [dict(r) for r in rows]

    def get_stocks(self) -> list[dict]:
        """Stock positions only."""
        rows = self._conn.execute("""
            SELECT * FROM positions WHERE pos_type = 'STOCK'
        """).fetchall()
        return [dict(r) for r in rows]

    def get_options(self) -> list[dict]:
        """Option positions only."""
        rows = self._conn.execute("""
            SELECT * FROM positions WHERE pos_type IN ('CALL', 'PUT')
        """).fetchall()
        return [dict(r) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # LOCAL TRADE JOURNAL
    # ═══════════════════════════════════════════════════════════

    def log_trade(self, ticker: str, action: str, strategy: str = None,
                  strike: float = None, expiry: str = None, dte: int = None,
                  contracts: int = 1, entry_price: float = 0,
                  notes: str = '') -> int:
        """Log a local trade decision. Returns row id."""
        now = datetime.now().isoformat()
        cur = self._conn.execute("""
            INSERT INTO local_trades
            (ticker, action, strategy, strike, expiry, dte, contracts,
             entry_price, entry_date, status, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
        """, (ticker, action, strategy, strike, expiry, dte, contracts,
              entry_price, now, notes, now))
        self._conn.commit()
        return cur.lastrowid

    def close_trade(self, trade_id: int, exit_price: float = 0,
                    pnl: float = None):
        """Close a local trade — sets exit price and auto-computes P&L if needed."""
        now = datetime.now().isoformat()
        trade = self._conn.execute(
            "SELECT entry_price, contracts, strategy FROM local_trades WHERE id=?",
            (trade_id,)
        ).fetchone()
        if not trade:
            return

        entry_px, contracts, _ = trade
        if pnl is None and entry_px:
            # Short option: P&L = (entry - exit) * contracts * 100
            pnl = (entry_px - exit_price) * (contracts or 1) * 100

        self._conn.execute("""
            UPDATE local_trades
            SET status='CLOSED', exit_price=?, exit_date=?, pnl=?
            WHERE id=?
        """, (exit_price, now, round(pnl, 2) if pnl else 0, trade_id))
        self._conn.commit()

    def expire_trade(self, trade_id: int):
        """Mark a trade as expired (keeps full premium)."""
        trade = self._conn.execute(
            "SELECT entry_price, contracts FROM local_trades WHERE id=?",
            (trade_id,)
        ).fetchone()
        if trade:
            pnl = trade['entry_price'] * trade['contracts'] * 100
            self._conn.execute("""
                UPDATE local_trades
                SET status='EXPIRED', exit_price=0, exit_date=?, pnl=?
                WHERE id=?
            """, (datetime.now().isoformat(), round(pnl, 2), trade_id))
            self._conn.commit()

    def get_open_trades(self) -> list[dict]:
        """All currently open local trades."""
        rows = self._conn.execute("""
            SELECT * FROM local_trades WHERE status='OPEN'
            ORDER BY created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def get_trade_summary(self) -> dict:
        """P&L summary for local trades."""
        total = self._conn.execute("""
            SELECT COUNT(*), SUM(pnl), AVG(pnl),
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)
            FROM local_trades WHERE status IN ('CLOSED', 'EXPIRED')
        """).fetchone()

        open_count = self._conn.execute(
            "SELECT COUNT(*) FROM local_trades WHERE status='OPEN'"
        ).fetchone()[0]

        return {
            'total_trades': total[0] or 0,
            'total_pnl': round(total[1] or 0, 2),
            'avg_pnl': round(total[2] or 0, 2),
            'win_count': total[3] or 0,
            'win_rate': round((total[3] or 0) / max(1, total[0] or 1) * 100, 1),
            'open_trades': open_count,
        }

    def get_portfolio_summary(self) -> dict:
        """Combined view: latest funds + positions + open trades."""
        funds = self.get_latest_funds()
        positions = self.get_positions()
        trades = self.get_open_trades()
        trade_summary = self.get_trade_summary()

        stock_value = sum(p['market_value'] or 0 for p in positions
                         if p['pos_type'] == 'STOCK')
        option_value = sum(abs(p['market_value'] or 0) for p in positions
                          if p['pos_type'] in ('CALL', 'PUT'))
        total_pl = sum(p['pl_val'] or 0 for p in positions)

        return {
            'funds': funds,
            'positions': {
                'stocks': len([p for p in positions if p['pos_type'] == 'STOCK']),
                'options': len([p for p in positions if p['pos_type'] in ('CALL', 'PUT')]),
                'stock_value': round(stock_value, 2),
                'option_value': round(option_value, 2),
                'unrealized_pl': round(total_pl, 2),
            },
            'trades': {
                'open': len(trades),
                **trade_summary,
            },
            'synced_at': funds['synced_at'] if funds else None,
        }
