"""
Moomoo Market Data Client — all data fetching from OpenD.

Single entry point for all moomoo data. Returns pure dataclass objects.
Used by: analysis modules, scoring engine, CLI scripts, notebooks.

Usage:
    from src.data.moomoo_client import MoomooClient
    client = MoomooClient()
    snap = client.get_stock_snapshot('US.AAPL')

    chain = client.get_option_snapshots('US.AAPL', dte_min=30, dte_max=45)
    client.close()
"""

import time
from datetime import date, datetime
from typing import Optional

from moomoo import (
    OpenQuoteContext, RET_OK, SubType,
)

# Suppress SDK connect/disconnect log spam on stderr
import logging
logging.getLogger('FTConsoleLog').setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════
# SAFETY: This client is READ-ONLY. It wraps OpenQuoteContext
# (market data) — no trade submission capability. For paper
# trading execution, use scripts/paper_trading.py (SIMULATE only).
# REAL account is NEVER used for order submission in this project.
# ═══════════════════════════════════════════════════════════════

from src.data.models import StockSnapshot, OptionSnapshot


class MoomooClient:
    """Thin wrapper over moomoo OpenQuoteContext. Returns typed dataclasses."""

    def __init__(self, host: str = '127.0.0.1', port: int = 11111, rate_limit: float = 0.2):
        self._ctx: Optional[OpenQuoteContext] = None
        self._host = host
        self._port = port
        self._rate_limit = rate_limit  # seconds between API calls to avoid moomoo throttling
        self._last_call = 0.0
        # Cache: daily OHLCV data only changes once per day
        self._history_cache: dict[tuple[str, int], list[dict]] = {}
        # Cache: option chain codes per (ticker, dte_min, dte_max) per session
        self._chain_cache: dict[tuple[str, int, int], list[str]] = {}

    @property
    def ctx(self) -> OpenQuoteContext:
        if self._ctx is None:
            self._ctx = OpenQuoteContext(host=self._host, port=self._port, ai_type=1)
        return self._ctx

    def _throttle(self):
        """Sleep if needed to respect moomoo API rate limits (~3 calls/sec)."""
        now = time.time()
        gap = now - self._last_call
        if gap < self._rate_limit:
            time.sleep(self._rate_limit - gap)
        self._last_call = time.time()

    def close(self):
        if self._ctx is not None:
            self._ctx.close()
            self._ctx = None
        self._history_cache.clear()
        self._chain_cache.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ═══════════════════════════════════════════════════════════
    # STOCK SNAPSHOT
    # ═══════════════════════════════════════════════════════════

    def get_stock_snapshot(self, ticker: str) -> Optional[StockSnapshot]:
        """Get full snapshot for a single stock. Returns None if no data."""
        snapshots = self.get_stock_snapshots([ticker])
        return snapshots[0] if snapshots else None

    def get_stock_snapshots(self, tickers: list[str]) -> list[StockSnapshot]:
        """Batch snapshot for multiple stocks (max 400 per moomoo limit)."""
        results: list[StockSnapshot] = []
        batch_size = 400

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i + batch_size]
            self._throttle()
            ret, data = self.ctx.get_market_snapshot(batch)
            if ret != RET_OK or data is None or len(data) == 0:
                continue

            for _, row in data.iterrows():
                code = self._s(row, 'code', '')
                if not code:
                    continue
                snap = StockSnapshot(
                    ticker=code,
                    name=self._s(row, 'name', ''),
                    last_price=self._f(row, 'last_price'),
                    open_price=self._f(row, 'open_price'),
                    high_price=self._f(row, 'high_price'),
                    low_price=self._f(row, 'low_price'),
                    prev_close=self._f(row, 'prev_close_price'),
                    bid=self._f(row, 'bid_price'),
                    ask=self._f(row, 'ask_price'),
                    bid_vol=self._f(row, 'bid_vol'),
                    ask_vol=self._f(row, 'ask_vol'),
                    volume=self._i(row, 'volume'),
                    turnover=self._f(row, 'turnover'),
                    turnover_rate=self._f(row, 'turnover_rate'),
                    volume_ratio=self._f(row, 'volume_ratio'),
                    amplitude=self._f(row, 'amplitude'),
                    highest_52w=self._f(row, 'highest52weeks_price'),
                    lowest_52w=self._f(row, 'lowest52weeks_price'),
                    pe_ratio=self._fn(row, 'pe_ratio'),
                    pb_ratio=self._fn(row, 'pb_ratio'),
                    pe_ttm=self._fn(row, 'pe_ttm_ratio'),
                    earnings_yield=self._fn(row, 'ey_ratio'),
                    market_cap=self._fn(row, 'total_market_val'),
                    circulating_market_cap=self._fn(row, 'circular_market_val'),
                    eps_ttm=self._fn(row, 'earning_per_share'),
                    net_profit=self._fn(row, 'net_profit'),
                    net_asset_per_share=self._fn(row, 'net_asset_per_share'),
                    dividend_ttm=self._fn(row, 'dividend_ttm'),
                    dividend_yield_ttm=self._fn(row, 'dividend_ratio_ttm'),
                    dividend_lfy=self._fn(row, 'dividend_lfy'),
                    issued_shares=self._fn(row, 'issued_shares'),
                    short_sell_rate=self._fn(row, 'short_sell_rate'),
                    short_available=self._fn(row, 'short_available_volume'),
                    suspension=self._s(row, 'suspension', '') == 'True' or self._s(row, 'suspension', '') is True,
                    lot_size=self._i(row, 'lot_size', 100),
                    update_time=self._dt(row, 'update_time'),
                )
                # Compute derived fields
                self._compute_derived_stock(snap)
                results.append(snap)

        return results

    @staticmethod
    def _compute_derived_stock(s: StockSnapshot):
        mid = (s.bid + s.ask) / 2
        if mid > 0 and s.ask > s.bid:
            s.bid_ask_spread_pct = (s.ask - s.bid) / mid * 100
        if s.prev_close > 0:
            s.change_pct = (s.last_price - s.prev_close) / s.prev_close * 100

    # ═══════════════════════════════════════════════════════════
    # OPTION CHAIN + SNAPSHOTS (with Greeks)
    # ═══════════════════════════════════════════════════════════

    def get_option_chain_codes(
        self, ticker: str, dte_min: int = 30, dte_max: int = 45
    ) -> list[str]:
        """Get option codes for a ticker within DTE range. Chunks 30-day API limit.
        Cached per session — option chain listings don't change intraday.
        Includes rate limiting and retry to avoid moomoo throttling (ret=-1)."""
        cache_key = (ticker, dte_min, dte_max)
        if cache_key in self._chain_cache:
            return self._chain_cache[cache_key]

        today = date.today()
        from datetime import timedelta
        codes = []
        seen = set()

        # Moomoo API requires start >= today+1
        chunk_start = max(1, dte_min)
        while chunk_start < dte_max:
            chunk_end = min(chunk_start + 29, dte_max)
            start = (today + timedelta(days=chunk_start)).isoformat()
            end = (today + timedelta(days=chunk_end)).isoformat()

            # Rate limit: throttle before each get_option_chain call
            self._throttle()

            # Retry up to 2 times on rate-limit failure (ret=-1)
            for attempt in range(3):
                if attempt > 0:
                    self._throttle()  # re-throttle before retry
                ret, data = self.ctx.get_option_chain(ticker, start=start, end=end)
                if ret == RET_OK:
                    break
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))  # backoff: 0.5s, 1s

            if ret == RET_OK and data is not None and len(data) > 0:
                for code in data['code']:
                    if code not in seen:
                        codes.append(code)
                        seen.add(code)
            chunk_start = chunk_end + 1

        if codes:  # only cache successful fetches
            self._chain_cache[cache_key] = codes
        return codes

    def get_option_snapshots(
        self, ticker: str, dte_min: int = 30, dte_max: int = 45
    ) -> list[OptionSnapshot]:
        """
        Get full option snapshots (with Greeks, IV, OI) for all contracts
        within the DTE range. Uses get_option_chain for codes, then snapshot.
        """
        codes = self.get_option_chain_codes(ticker, dte_min, dte_max)
        if not codes:
            return []

        results: list[OptionSnapshot] = []
        batch_size = 400

        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            self._throttle()
            ret, data = self.ctx.get_market_snapshot(batch)
            if ret != RET_OK or data is None or len(data) == 0:
                continue

            for _, row in data.iterrows():
                # Only option rows have option_valid=True
                if not self._b(row, 'option_valid'):
                    continue

                snap = OptionSnapshot(
                    code=self._s(row, 'code', ''),
                    name=self._s(row, 'name', ''),
                    underlying=self._s(row, 'stock_owner', ticker),
                    option_type=self._s(row, 'option_type', ''),
                    strike=self._f(row, 'option_strike_price'),
                    expiry=self._s(row, 'strike_time', ''),
                    dte=self._i(row, 'option_expiry_date_distance'),
                    area_type=self._s(row, 'option_area_type', 'AMERICAN'),
                    last_price=self._f(row, 'last_price'),
                    bid=self._f(row, 'bid_price'),
                    ask=self._f(row, 'ask_price'),
                    bid_vol=self._f(row, 'bid_vol'),
                    ask_vol=self._f(row, 'ask_vol'),
                    volume=self._i(row, 'volume'),
                    prev_close=self._f(row, 'prev_close_price'),
                    delta=self._f(row, 'option_delta'),
                    gamma=self._f(row, 'option_gamma'),
                    theta=self._f(row, 'option_theta'),
                    vega=self._f(row, 'option_vega'),
                    rho=self._f(row, 'option_rho'),
                    implied_vol=self._f(row, 'option_implied_volatility'),
                    open_interest=self._i(row, 'option_open_interest'),
                    net_open_interest=self._fn(row, 'option_net_open_interest'),
                    contract_size=self._f(row, 'option_contract_size', 100.0),
                    contract_multiplier=self._f(row, 'option_contract_multiplier', 100.0),
                    premium=self._f(row, 'option_premium'),
                    contract_nominal_value=self._fn(row, 'option_contract_nominal_value'),
                    suspension=self._b(row, 'suspension'),
                    lot_size=self._i(row, 'lot_size', 100),
                    update_time=self._dt(row, 'update_time'),
                )
                # Derived
                mid = (snap.bid + snap.ask) / 2
                if mid > 0 and snap.ask > snap.bid:
                    snap.bid_ask_spread_pct = (snap.ask - snap.bid) / mid * 100
                results.append(snap)

        return results

    def get_all_option_snapshots(
        self, ticker: str, dte_min: int = 15, dte_max: int = 60
    ) -> list[OptionSnapshot]:
        """
        Get ALL option contracts across a wide DTE range.
        Useful for computing IV rank, max pain, PCR, skew, etc.
        Makes multiple chain calls if needed (30-day API limit per call).
        """
        all_codes: list[str] = []
        today = date.today()
        from datetime import timedelta

        # Window size: 29 days to stay under 30-day API limit
        window = 29
        current_start = today + timedelta(days=max(1, dte_min))
        end_date = today + timedelta(days=dte_max)

        while current_start < end_date:
            window_end = min(current_start + timedelta(days=window), end_date)
            ret, data = self.ctx.get_option_chain(
                ticker,
                start=current_start.isoformat(),
                end=window_end.isoformat(),
            )
            if ret == RET_OK and data is not None and len(data) > 0:
                all_codes.extend(list(data['code']))
            current_start = window_end + timedelta(days=1)

        if not all_codes:
            return []

        # Batch snapshot all codes
        results: list[OptionSnapshot] = []
        for i in range(0, len(all_codes), 400):
            batch = all_codes[i:i + 400]
            self._throttle()
            ret, data = self.ctx.get_market_snapshot(batch)
            if ret != RET_OK or data is None or len(data) == 0:
                continue
            for _, row in data.iterrows():
                if not self._b(row, 'option_valid'):
                    continue
                snap = OptionSnapshot(
                    code=self._s(row, 'code', ''),
                    name=self._s(row, 'name', ''),
                    underlying=self._s(row, 'stock_owner', ticker),
                    option_type=self._s(row, 'option_type', ''),
                    strike=self._f(row, 'option_strike_price'),
                    expiry=self._s(row, 'strike_time', ''),
                    dte=self._i(row, 'option_expiry_date_distance'),
                    area_type=self._s(row, 'option_area_type', 'AMERICAN'),
                    last_price=self._f(row, 'last_price'),
                    bid=self._f(row, 'bid_price'),
                    ask=self._f(row, 'ask_price'),
                    bid_vol=self._f(row, 'bid_vol'),
                    ask_vol=self._f(row, 'ask_vol'),
                    volume=self._i(row, 'volume'),
                    prev_close=self._f(row, 'prev_close_price'),
                    delta=self._f(row, 'option_delta'),
                    gamma=self._f(row, 'option_gamma'),
                    theta=self._f(row, 'option_theta'),
                    vega=self._f(row, 'option_vega'),
                    rho=self._f(row, 'option_rho'),
                    implied_vol=self._f(row, 'option_implied_volatility'),
                    open_interest=self._i(row, 'option_open_interest'),
                    net_open_interest=self._fn(row, 'option_net_open_interest'),
                    contract_size=self._f(row, 'option_contract_size', 100.0),
                    contract_multiplier=self._f(row, 'option_contract_multiplier', 100.0),
                    premium=self._f(row, 'option_premium'),
                    contract_nominal_value=self._fn(row, 'option_contract_nominal_value'),
                    suspension=self._b(row, 'suspension'),
                    lot_size=self._i(row, 'lot_size', 100),
                    update_time=self._dt(row, 'update_time'),
                )
                mid = (snap.bid + snap.ask) / 2
                if mid > 0 and snap.ask > snap.bid:
                    snap.bid_ask_spread_pct = (snap.ask - snap.bid) / mid * 100
                results.append(snap)

        return results

    # ═══════════════════════════════════════════════════════════
    # PRICE HISTORY (KLINE)
    # ═══════════════════════════════════════════════════════════

    def get_price_history(
        self, ticker: str, days: int = 252
    ) -> list[dict]:
        """Get daily OHLCV history. Returns list of dicts with keys:
        date, open, high, low, close, volume.
        Cached per session — daily data only changes once per day."""
        cache_key = (ticker, days)
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]

        from moomoo import KLType, AuType
        self._throttle()
        ret, data, _ = self.ctx.request_history_kline(
            ticker, max_count=days, ktype=KLType.K_DAY, autype=AuType.QFQ
        )
        if ret != RET_OK or data is None or len(data) == 0:
            return []  # don't cache failures — retry next time

        records = []
        for _, row in data.iterrows():
            records.append({
                'date': str(row.get('time_key', '')),
                'open': float(row.get('open', 0) or 0),
                'high': float(row.get('high', 0) or 0),
                'low': float(row.get('low', 0) or 0),
                'close': float(row.get('close', 0) or 0),
                'volume': int(row.get('volume', 0) or 0),
            })
        self._history_cache[cache_key] = records
        return records

    # ═══════════════════════════════════════════════════════════
    # CAPITAL FLOW
    # ═══════════════════════════════════════════════════════════

    def get_capital_flow(self, ticker: str) -> Optional[dict]:
        """Get today's capital flow (inflow by order size)."""
        ret, data = self.ctx.get_capital_flow(ticker)
        if ret != RET_OK or data is None or len(data) == 0:
            return None
        row = data.iloc[0]
        return {
            'in_flow': self._fn(row, 'in_flow'),
            'super_in_flow': self._fn(row, 'super_in_flow'),
            'big_in_flow': self._fn(row, 'big_in_flow'),
            'mid_in_flow': self._fn(row, 'mid_in_flow'),
            'sml_in_flow': self._fn(row, 'sml_in_flow'),
            'main_in_flow': self._fn(row, 'main_in_flow'),
        }

    def get_capital_distribution(self, ticker: str) -> Optional[dict]:
        """Get capital distribution (inflow/outflow by order size)."""
        ret, data = self.ctx.get_capital_distribution(ticker)
        if ret != RET_OK or data is None or len(data) == 0:
            return None
        row = data.iloc[0]
        return {
            'in_super': self._fn(row, 'capital_in_super'),
            'out_super': self._fn(row, 'capital_out_super'),
            'in_big': self._fn(row, 'capital_in_big'),
            'out_big': self._fn(row, 'capital_out_big'),
            'in_mid': self._fn(row, 'capital_in_mid'),
            'out_mid': self._fn(row, 'capital_out_mid'),
            'in_small': self._fn(row, 'capital_in_small'),
            'out_small': self._fn(row, 'capital_out_small'),
        }

    # ═══════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _f(row, key, default=0.0) -> float:
        val = row.get(key)
        return float(val) if val is not None and str(val).lower() not in ('nan', 'n/a', '') else default

    @staticmethod
    def _fn(row, key, default=None) -> Optional[float]:
        val = row.get(key)
        if val is None:
            return default
        s = str(val).lower()
        if s in ('nan', 'n/a', ''):
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _i(row, key, default=0) -> int:
        val = row.get(key)
        try:
            return int(float(val)) if val is not None and str(val).lower() not in ('nan', 'n/a', '') else default
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _s(row, key, default='') -> str:
        val = row.get(key)
        return str(val) if val is not None and str(val).lower() not in ('nan', 'n/a') else default

    @staticmethod
    def _b(row, key, default=False) -> bool:
        val = row.get(key)
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        return str(val).lower() in ('true', '1')

    @staticmethod
    def _dt(row, key) -> Optional[datetime]:
        val = row.get(key)
        if val is None or str(val).lower() in ('nan', 'n/a', ''):
            return None
        try:
            return datetime.strptime(str(val)[:19], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            return None
