"""
OIE Paper Portfolio Database — separate from real portfolio DB.

Tables:
    paper_state     — key-value store (cash, fund, engine meta)
    paper_positions — stock + option positions with lifecycle status
    paper_trades    — full audit log of every action
    paper_snapshots — periodic net-liq snapshots for history

DB path: db/oie_paper.db  (never committed, .gitignored)
"""

import sqlite3
import os
from datetime import datetime
from typing import Optional

from src.paths import OIE_DB_PATH as DB_PATH


class OIEDB:
    """Paper portfolio — tracks simulated positions, P&L, and audit trail."""

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
            CREATE TABLE IF NOT EXISTS paper_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                pos_type TEXT NOT NULL,       -- STOCK, CALL, PUT
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                qty REAL NOT NULL,            -- shares (>0) or contracts (<0 for short)
                cost_price REAL,              -- entry price (per share or per contract)
                strike REAL,                  -- option strike, NULL for stocks
                expiry TEXT,                  -- ISO date, NULL for stocks
                dte_initial INTEGER,          -- DTE at entry
                entry_date TEXT NOT NULL,
                entry_premium REAL,           -- premium collected (per contract) or 0 for stock
                current_bid REAL,             -- mark-to-market
                current_delta REAL,
                current_iv REAL,
                exit_price REAL,
                exit_date TEXT,
                exit_reason TEXT,             -- CLOSE_50PCT, EXPIRE, ASSIGN, BUY_BACK, SOLD
                realized_pnl REAL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                event TEXT NOT NULL,          -- SEED, OPEN_CC, OPEN_CSP, CLOSE, EXPIRE,
                                             -- ASSIGN_CSP, ASSIGN_CC, SNAPSHOT, ERROR, CYCLE
                ticker TEXT,
                pos_id INTEGER,              -- FK to paper_positions
                detail TEXT,                  -- JSON-like description
                cash_change REAL DEFAULT 0,  -- impact on cash balance
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                total_value REAL,            -- cash + stock_mv + fund - option_liability
                cash REAL,
                stock_value REAL,
                fund_value REAL,
                option_premium_received REAL,
                option_liability REAL,       -- cost to close all open options
                unrealized_pnl REAL,
                realized_pnl_total REAL,
                open_positions INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_pp_status ON paper_positions(status);
            CREATE INDEX IF NOT EXISTS idx_pp_ticker ON paper_positions(ticker);
            CREATE INDEX IF NOT EXISTS idx_pt_event ON paper_trades(event);
            CREATE INDEX IF NOT EXISTS idx_ps_ts ON paper_snapshots(ts);
        """)
        self._conn.commit()

    # ═══════════════════════════════════════════════════════════
    # STATE (key-value for engine resume)
    # ═══════════════════════════════════════════════════════════

    def get_state(self, key: str, default: str = '') -> str:
        row = self._conn.execute(
            "SELECT value FROM paper_state WHERE key=?", (key,)
        ).fetchone()
        return row['value'] if row else default

    def set_state(self, key: str, value: str):
        self._conn.execute(
            "INSERT OR REPLACE INTO paper_state (key, value) VALUES (?, ?)",
            (key, value)
        )
        self._conn.commit()

    # ═══════════════════════════════════════════════════════════
    # SEED — initial portfolio from REAL account
    # ═══════════════════════════════════════════════════════════

    def seed_portfolio(self, stocks: dict[str, dict], cash: float, fund: float):
        """Seed paper portfolio with stock holdings + cash. No options."""
        now = datetime.now().isoformat()
        today = now[:10]

        # Save cash/fund state
        self.set_state('cash', str(round(cash, 2)))
        self.set_state('fund', str(round(fund, 2)))
        self.set_state('seeded_at', now)
        self.set_state('seeded_cash', str(round(cash, 2)))
        self.set_state('seeded_fund', str(round(fund, 2)))

        # Insert stock positions
        for ticker, info in stocks.items():
            qty = info.get('qty', 0)
            cost = info.get('cost', 0)
            mv = qty * cost if qty and cost else 0
            self._conn.execute("""
                INSERT INTO paper_positions
                (ticker, pos_type, status, qty, cost_price,
                 entry_date, entry_premium, current_bid, realized_pnl, created_at)
                VALUES (?, 'STOCK', 'ACTIVE', ?, ?, ?, 0, ?, 0, ?)
            """, (ticker, qty, cost, today, cost, now))
            self._log_trade(now, 'SEED', ticker, None,
                          f'Seed {qty:.0f} shares @ ${cost:.2f} = ${mv:,.2f}', cash_change=0)

        seed_total = sum(
            info.get('qty', 0) * info.get('cost', 0)
            for info in stocks.values()
        ) + cash + fund

        self._log_trade(now, 'SEED', None, None,
                      f'Portfolio seeded: stocks=${sum(info.get("qty",0)*info.get("cost",0) for info in stocks.values()):,.2f} '
                      f'+ cash=${cash:,.2f} + fund=${fund:,.2f} = ${seed_total:,.2f}',
                      cash_change=0)
        self._conn.commit()

    def is_seeded(self) -> bool:
        return bool(self.get_state('seeded_at'))

    def reconcile_stocks(self, stocks: dict[str, dict]) -> tuple[int, int, int]:
        """Non-destructively sync paper STOCK rows to the REAL account.

        For each ticker in ``stocks``:
          - If paper holds an ACTIVE STOCK row for it → update qty/cost/price.
          - Else → insert a fresh ACTIVE STOCK row.
        Tickers in paper but absent from ``stocks`` are left untouched (the
        wheel may have added them via CSP assignment). Options and history
        are never touched.

        Returns (added, updated, unchanged) counts.
        """
        now = datetime.now().isoformat()
        today = now[:10]
        added = updated = unchanged = 0
        for ticker, info in stocks.items():
            qty = info.get('qty', 0)
            cost = info.get('cost', 0)
            mv = qty * cost if qty and cost else 0
            row = self._conn.execute(
                "SELECT id, qty, cost_price FROM paper_positions "
                "WHERE ticker=? AND status='ACTIVE' AND pos_type='STOCK' "
                "ORDER BY id LIMIT 1", (ticker,)
            ).fetchone()
            if row is None:
                self._conn.execute("""
                    INSERT INTO paper_positions
                    (ticker, pos_type, status, qty, cost_price,
                     entry_date, entry_premium, current_bid, realized_pnl, created_at)
                    VALUES (?, 'STOCK', 'ACTIVE', ?, ?, ?, 0, ?, 0, ?)
                """, (ticker, qty, cost, today, cost, now))
                self._log_trade(now, 'RECONCILE', ticker, None,
                              f'Reconcile ADD {qty:.0f} shares @ ${cost:.2f} = ${mv:,.2f}',
                              cash_change=0)
                added += 1
            elif abs((row['qty'] or 0) - qty) > 1e-6 or abs((row['cost_price'] or 0) - cost) > 1e-6:
                self._conn.execute(
                    "UPDATE paper_positions SET qty=?, cost_price=?, current_bid=? WHERE id=?",
                    (qty, cost, cost, row['id']))
                self._log_trade(now, 'RECONCILE', ticker, row['id'],
                              f'Reconcile UPDATE → {qty:.0f} shares @ ${cost:.2f} = ${mv:,.2f}',
                              cash_change=0)
                updated += 1
            else:
                unchanged += 1
        self._conn.commit()
        return added, updated, unchanged

    # ═══════════════════════════════════════════════════════════
    # POSITIONS — open, close, expire, assign
    # ═══════════════════════════════════════════════════════════

    def open_position(self, ticker: str, pos_type: str, qty: float,
                      cost_price: float, strike: float = None,
                      expiry: str = None, dte: int = None,
                      entry_premium: float = 0, delta: float = None,
                      iv: float = None, cash_impact: float = 0,
                      note: str = '') -> int:
        """Open a new paper position. Returns position ID."""
        now = datetime.now().isoformat()
        today = now[:10]

        cur = self._conn.execute("""
            INSERT INTO paper_positions
            (ticker, pos_type, status, qty, cost_price, strike, expiry,
             dte_initial, entry_date, entry_premium,
             current_bid, current_delta, current_iv, created_at)
            VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, pos_type, qty, cost_price, strike, expiry,
              dte, today, entry_premium,
              entry_premium, delta, iv, now))
        pos_id = cur.lastrowid

        event = f'OPEN_{pos_type}'
        self._log_trade(now, event, ticker, pos_id, note, cash_change=cash_impact)
        self._conn.commit()
        return pos_id

    def close_position(self, pos_id: int, exit_price: float, reason: str,
                       cash_impact: float = 0):
        """Close a paper position — buy back option or sell stock."""
        now = datetime.now().isoformat()
        pos = self._conn.execute(
            "SELECT * FROM paper_positions WHERE id=?", (pos_id,)
        ).fetchone()
        if not pos:
            return
        if pos['status'] != 'ACTIVE':
            return  # already closed/expired/assigned

        entry = pos['entry_premium'] or 0
        qty = abs(pos['qty'])
        if pos['pos_type'] in ('CALL', 'PUT'):
            # Short option: P&L = (entry_premium - exit_price) * qty * 100
            pnl = round((entry - exit_price) * qty * 100, 2)
        else:
            # Stock: P&L = (exit_price - cost_price) * qty
            pnl = round((exit_price - pos['cost_price']) * qty, 2)

        self._conn.execute("""
            UPDATE paper_positions
            SET status='CLOSED', exit_price=?, exit_date=?, exit_reason=?,
                realized_pnl=?
            WHERE id=?
        """, (exit_price, now, reason, pnl, pos_id))

        self._log_trade(now, 'CLOSE', pos['ticker'], pos_id,
                      f'{reason}: {"+" if pnl>=0 else ""}{pnl:,.2f}, '
                      f'exit @ ${exit_price:.2f}',
                      cash_change=cash_impact)
        self._conn.commit()
        return pnl

    def expire_position(self, pos_id: int) -> float:
        """Option expired OTM — keep full premium."""
        now = datetime.now().isoformat()
        pos = self._conn.execute(
            "SELECT * FROM paper_positions WHERE id=?", (pos_id,)
        ).fetchone()
        if not pos:
            return 0

        entry = pos['entry_premium'] or 0
        qty = abs(pos['qty'])
        pnl = round(entry * qty * 100, 2)  # full premium kept

        self._conn.execute("""
            UPDATE paper_positions
            SET status='EXPIRED', exit_price=0, exit_date=?, exit_reason='EXPIRED',
                realized_pnl=?
            WHERE id=?
        """, (now, pnl, pos_id))

        # Premium already received at open — no cash change on expiry
        self._log_trade(now, 'EXPIRE', pos['ticker'], pos_id,
                      f'Expired OTM: +${pnl:,.2f} premium kept',
                      cash_change=0)
        self._conn.commit()
        return pnl

    def assign_position(self, pos_id: int, action: str,
                        stock_price: float) -> int:
        """
        Handle assignment.
        action='CSP' → add 100 shares at strike, return new stock pos_id
        action='CC'  → remove 100 shares at strike, return 0
        """
        now = datetime.now().isoformat()
        pos = self._conn.execute(
            "SELECT * FROM paper_positions WHERE id=?", (pos_id,)
        ).fetchone()
        if not pos:
            return 0

        strike = pos['strike']
        entry = pos['entry_premium'] or 0
        qty = abs(pos['qty'])
        cost_basis = strike - entry  # effective cost basis per share

        if action == 'CC':
            # Stock called away: sell 100 shares at strike. Remove from stock holdings.
            pnl = round((strike - pos['cost_price']) * qty * 100 + entry * qty * 100, 2)
            self._conn.execute("""
                UPDATE paper_positions
                SET status='ASSIGNED', exit_price=strike, exit_date=?,
                    exit_reason='CC_ASSIGN', realized_pnl=?
                WHERE id=?
            """, (now, pnl, pos_id))
            # Deduct shares from stock position (earliest ID first). Guard
            # against over-deduction: only deduct what the paper book actually
            # holds. If shares are short (drift between paper and real), deduct
            # what's available and log the shortfall so an operator notices.
            remaining = qty * 100
            stock_rows = self._conn.execute(
                "SELECT id, qty FROM paper_positions "
                "WHERE ticker=? AND status='ACTIVE' AND pos_type='STOCK' ORDER BY id",
                (pos['ticker'],)
            ).fetchall()
            held = sum(sr['qty'] for sr in stock_rows)
            for sr in stock_rows:
                if remaining <= 0:
                    break
                deduct = min(sr['qty'], remaining)
                remaining -= deduct
                if sr['qty'] <= deduct:
                    self._conn.execute(
                        "UPDATE paper_positions SET status='CLOSED', qty=0 WHERE id=?",
                        (sr['id'],))
                else:
                    self._conn.execute(
                        "UPDATE paper_positions SET qty=qty-? WHERE id=?",
                        (deduct, sr['id']))
            short_share = max(0, qty * 100 - held)
            detail = (f'CC assigned: sold {qty*100} shares @ ${strike:.2f}, '
                      f'+${entry*qty*100:,.2f} premium, P&L ${pnl:,.2f}')
            if short_share:
                detail += (f' ⚠️ {short_share:.0f} shares not held in paper book '
                           f'(drift from real account) — reconcile to correct.')
            self._log_trade(now, 'ASSIGN_CC', pos['ticker'], pos_id, detail,
                          cash_change=strike * qty * 100)
            self._conn.commit()
            return 0

        else:  # CSP assigned
            # Buy 100 shares at strike (minus premium)
            pnl = round(entry * qty * 100, 2)
            self._conn.execute("""
                UPDATE paper_positions
                SET status='ASSIGNED', exit_price=strike, exit_date=?,
                    exit_reason='CSP_ASSIGN', realized_pnl=?
                WHERE id=?
            """, (now, pnl, pos_id))

            # Add stock position
            cur = self._conn.execute("""
                INSERT INTO paper_positions
                (ticker, pos_type, status, qty, cost_price,
                 entry_date, entry_premium, current_bid, realized_pnl, created_at)
                VALUES (?, 'STOCK', 'ACTIVE', ?, ?, ?, 0, ?, 0, ?)
            """, (pos['ticker'], qty * 100, cost_basis, now[:10], stock_price, now))
            new_id = cur.lastrowid

            self._log_trade(now, 'ASSIGN_CSP', pos['ticker'], pos_id,
                          f'CSP assigned: bought {qty*100} shares @ ${strike:.2f} '
                          f'(effective ${cost_basis:.2f} after ${entry:.2f} premium)',
                          cash_change=-(strike * qty * 100))
            self._conn.commit()
            return new_id

    # ═══════════════════════════════════════════════════════════
    # QUERIES
    # ═══════════════════════════════════════════════════════════

    def get_active_positions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM paper_positions WHERE status='ACTIVE' ORDER BY pos_type, ticker"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_options(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM paper_positions WHERE status='ACTIVE' AND pos_type IN ('CALL','PUT')"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_stocks(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM paper_positions WHERE status='ACTIVE' AND pos_type='STOCK'"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_pnl(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) as total FROM paper_positions "
            "WHERE status IN ('CLOSED','EXPIRED','ASSIGNED')"
        ).fetchone()
        return row['total'] if row else 0

    def get_position(self, pos_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM paper_positions WHERE id=?", (pos_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_shares(self, ticker: str) -> float:
        """Total ACTIVE shares of a ticker."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(qty), 0) as total FROM paper_positions "
            "WHERE ticker=? AND status='ACTIVE' AND pos_type='STOCK'",
            (ticker,)
        ).fetchone()
        return row['total'] if row else 0

    def get_open_option_tickers(self) -> set[str]:
        """Set of tickers with ACTIVE option positions."""
        rows = self._conn.execute(
            "SELECT DISTINCT ticker FROM paper_positions "
            "WHERE status='ACTIVE' AND pos_type IN ('CALL','PUT')"
        ).fetchall()
        return {r['ticker'] for r in rows}

    def get_daily_new_count(self) -> int:
        """New positions opened by the engine today (excludes SEED)."""
        today = datetime.now().isoformat()[:10]
        seeded_at = self.get_state('seeded_at', '')
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_trades "
            "WHERE ts LIKE ? AND event LIKE 'OPEN_%' AND ts > ?",
            (f'{today}%', seeded_at)
        ).fetchone()
        return row['cnt'] if row else 0

    # ═══════════════════════════════════════════════════════════
    # SNAPSHOTS
    # ═══════════════════════════════════════════════════════════

    def save_snapshot(self, total_value: float, cash: float, stock_value: float,
                      fund_value: float, option_premium: float,
                      option_liability: float, unrealized_pnl: float,
                      realized_pnl: float, open_positions: int):
        now = datetime.now().isoformat()
        self._conn.execute("""
            INSERT INTO paper_snapshots
            (ts, total_value, cash, stock_value, fund_value,
             option_premium_received, option_liability,
             unrealized_pnl, realized_pnl_total, open_positions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (now, round(total_value, 2), round(cash, 2), round(stock_value, 2),
              round(fund_value, 2), round(option_premium, 2),
              round(option_liability, 2), round(unrealized_pnl, 2),
              round(realized_pnl, 2), open_positions))
        self._conn.commit()

    def get_snapshots(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM paper_snapshots ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def get_snapshot_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_snapshots"
        ).fetchone()
        return row['cnt'] if row else 0

    # ═══════════════════════════════════════════════════════════
    # AUDIT
    # ═══════════════════════════════════════════════════════════

    def _log_trade(self, ts: str, event: str, ticker: str, pos_id: int,
                   detail: str, cash_change: float = 0):
        self._conn.execute("""
            INSERT INTO paper_trades (ts, event, ticker, pos_id, detail, cash_change, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ts, event, ticker, pos_id, detail, cash_change, datetime.now().isoformat()))
        # Update cash state on every trade
        if cash_change != 0:
            current = float(self.get_state('cash', '0'))
            new_cash = round(current + cash_change, 2)
            self.set_state('cash', str(new_cash))

    def get_recent_events(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    # ═══════════════════════════════════════════════════════════
    # RESET
    # ═══════════════════════════════════════════════════════════

    def reset_all(self):
        """Wipe all paper data."""
        self._conn.executescript("""
            DELETE FROM paper_positions;
            DELETE FROM paper_trades;
            DELETE FROM paper_snapshots;
            DELETE FROM paper_state;
        """)
        self._conn.commit()
