"""
IV History persistence — local SQLite DB for IV Rank/Percentile tracking.

Stores daily IV snapshots per ticker. After 252+ days of data, IV Rank
becomes reliable. Before that, marks tickers as "new" for cautious scoring.

Fills SPECS P2 gap (schema + repository) and the IV Rank Calculator spec.

Usage:
    from src.data.iv_history import IVHistoryTracker
    tracker = IVHistoryTracker()
    tracker.record_iv('AAPL', 25.5)          # store today's IV
    iv_rank = tracker.get_iv_rank('AAPL')    # compute IV rank
    tracker.close()
"""

import sqlite3
import os
from datetime import date, timedelta
from typing import Optional


DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'db', 'options.db')


class IVHistoryTracker:
    """SQLite-based IV history for computing accurate IV Rank/Percentile."""

    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS iv_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                iv REAL NOT NULL,
                atm_iv REAL,
                put_iv REAL,
                call_iv REAL,
                UNIQUE(ticker, date)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_iv_ticker_date ON iv_history(ticker, date)
        """)
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ═══════════════════════════════════════════════════════════
    # WRITE
    # ═══════════════════════════════════════════════════════════

    def record_iv(self, ticker: str, iv: float,
                  atm_iv: Optional[float] = None,
                  put_iv: Optional[float] = None,
                  call_iv: Optional[float] = None):
        """Insert or update today's IV for a ticker."""
        today = date.today().isoformat()
        self._conn.execute("""
            INSERT OR REPLACE INTO iv_history (ticker, date, iv, atm_iv, put_iv, call_iv)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (ticker, today, iv, atm_iv, put_iv, call_iv))
        self._conn.commit()

    def record_batch(self, records: list[dict]):
        """Batch insert: [{'ticker': 'AAPL', 'iv': 25.5}, ...]"""
        today = date.today().isoformat()
        for r in records:
            self._conn.execute("""
                INSERT OR REPLACE INTO iv_history (ticker, date, iv, atm_iv, put_iv, call_iv)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (r['ticker'], today, r['iv'],
                  r.get('atm_iv'), r.get('put_iv'), r.get('call_iv')))
        self._conn.commit()

    # ═══════════════════════════════════════════════════════════
    # READ
    # ═══════════════════════════════════════════════════════════

    def get_iv_history(self, ticker: str, days: int = 252) -> list[float]:
        """Get historical IV values for a ticker. Most recent first."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            "SELECT iv FROM iv_history WHERE ticker = ? AND date >= ? ORDER BY date DESC",
            (ticker, cutoff)
        ).fetchall()
        return [r[0] for r in rows]

    def get_all_iv_history(self, ticker: str) -> list[float]:
        """Get ALL stored IV values for a ticker."""
        rows = self._conn.execute(
            "SELECT iv FROM iv_history WHERE ticker = ? ORDER BY date DESC",
            (ticker,)
        ).fetchall()
        return [r[0] for r in rows]

    def get_iv_rank(self, ticker: str, current_iv: Optional[float] = None,
                    lookback: int = 252) -> Optional[dict]:
        """
        Compute IV Rank and IV Percentile for a ticker.

        IV Rank = (current - min) / (max - min) * 100
        IV Percentile = pct of days with IV < current

        Returns None if insufficient data.
        """
        iv_list = self.get_iv_history(ticker, lookback)
        if not iv_list:
            return None

        current = current_iv if current_iv is not None else iv_list[0]
        if current <= 0:
            return None

        low, high = min(iv_list), max(iv_list)
        rank = ((current - low) / (high - low) * 100) if high > low else 50.0

        below = sum(1 for iv in iv_list if iv < current)
        percentile = (below / len(iv_list)) * 100

        is_new = len(iv_list) < 30
        quality = 'LOW' if is_new else ('EXCELLENT' if len(iv_list) >= 252 else 'ADEQUATE')

        return {
            'ticker': ticker,
            'current_iv': current,
            'iv_rank': round(rank, 1),
            'iv_percentile': round(percentile, 1),
            'days_collected': len(iv_list),
            'is_new_tracker': is_new,
            'data_quality': quality,
            'iv_min_52w': low if not is_new else None,
            'iv_max_52w': high if not is_new else None,
        }

    def get_stats(self, ticker: str) -> dict:
        """Basic stats for a ticker's IV history."""
        iv_list = self.get_all_iv_history(ticker)
        if not iv_list:
            return {'ticker': ticker, 'days': 0}

        return {
            'ticker': ticker,
            'days': len(iv_list),
            'current_iv': iv_list[0],
            'mean_iv': sum(iv_list) / len(iv_list),
            'min_iv': min(iv_list),
            'max_iv': max(iv_list),
            'is_new': len(iv_list) < 30,
        }

    def has_today(self, ticker: str) -> bool:
        """Check if we already recorded IV for this ticker today."""
        today = date.today().isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM iv_history WHERE ticker = ? AND date = ?",
            (ticker, today)
        ).fetchone()
        return row is not None

    def get_all_tickers(self) -> list[str]:
        """List all tickers with IV history."""
        rows = self._conn.execute(
            "SELECT DISTINCT ticker FROM iv_history ORDER BY ticker"
        ).fetchall()
        return [r[0] for r in rows]

    def prune_old(self, keep_days: int = 365):
        """Remove records older than keep_days."""
        cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
        self._conn.execute("DELETE FROM iv_history WHERE date < ?", (cutoff,))
        self._conn.commit()


# ═══════════════════════════════════════════════════════════════
# CLI: daily IV harvest (run via cron)
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("IV History Tracker — daily harvest")
    import sys
    sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..')))

    from src.data.moomoo_client import MoomooClient

    watchlist = ['US.V', 'US.MSFT', 'US.GOOGL', 'US.AAPL', 'US.AMZN',
                 'US.NVDA', 'US.META', 'US.AVGO', 'US.ADBE', 'US.CRM', 'US.AMD']

    with IVHistoryTracker() as tracker, MoomooClient() as moomoo:
        records = []
        for ticker in watchlist:
            contracts = moomoo.get_option_snapshots(ticker, dte_min=30, dte_max=45)
            if not contracts:
                continue
            # Use average IV of ATM-ish options as proxy
            calls = [c for c in contracts if c.option_type == 'CALL' and abs(c.delta) > 0.40 and abs(c.delta) < 0.60]
            puts = [c for c in contracts if c.option_type == 'PUT' and abs(c.delta) > 0.40 and abs(c.delta) < 0.60]
            atm_ivs = [c.implied_vol for c in calls + puts if c.implied_vol > 0]
            avg_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else 0

            if avg_iv > 0:
                short = ticker.replace('US.', '')
                tracker.record_iv(short, avg_iv)
                stats = tracker.get_stats(short)
                print(f"  {short:6s}  IV: {avg_iv:6.1f}%  Days: {stats['days']:>4d}  "
                      f"{'⚠️ NEW' if stats['is_new'] else '✅'}")

    print(f"\nDone. {len(tracker.get_all_tickers())} tickers tracked.")
