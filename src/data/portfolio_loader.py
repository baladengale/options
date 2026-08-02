"""
Portfolio Loader — single source of truth for the REAL moomoo account state.

Wraps OpenSecTradeContext (the trade/account context) to pull funds + positions
in one connection. `src/data/moomoo_client.py` is read-only *quote* data only;
this module is its account-state counterpart.

This replaces the `_fetch_positions()` / `_fetch_live_portfolio()` logic that was
copy-pasted across scripts/portfolio_summary.py, portfolio_check.py,
options_table.py, and screener.py.

Usage:
    from src.data.portfolio_loader import fetch_portfolio, parse_option_code
    pf = fetch_portfolio()          # one connection, full state
    pf.funds.cash, pf.stocks, pf.options
    pf.options[code]['strike']

Read-only — never submits orders.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from moomoo import OpenSecTradeContext, TrdEnv, RET_OK

# moomoo encodes the option contract code as: US.<TICKER><YYMMDD><C|P><STRIKE×1000>
# e.g. US.V260918C360000 → V, 2026-09-18, CALL, $360.00
# Non-greedy ticker + 6-digit date anchor lets it backtracks to the digit boundary,
# so multi-char tickers (GOOGL, AVGO) parse correctly.
_OPTION_RE = re.compile(r'US\.(\w+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)')
_OPTION_DETECT_RE = re.compile(r'\d{6}[CP]\d+')

# Fallback USD→HKD rate when the live FX quote is unavailable (offline/API
# failure). The moomoo app converts HKD→USD using the live exchange rate.
HKD_TO_USD = 7.8

_live_fx_cache: Optional[float] = None


def _live_hkd_to_usd() -> Optional[float]:
    """Best-effort live USD/HKD rate (cached once per process). None on failure."""
    global _live_fx_cache
    if _live_fx_cache is not None:
        return _live_fx_cache
    try:
        import yfinance as yf
        hist = yf.Ticker('HKD=X').history(period='1d', interval='1d')
        if hist is not None and not hist.empty:
            rate = float(hist['Close'].iloc[-1])
            if 7.5 < rate < 8.2:          # sanity band for USD/HKD
                _live_fx_cache = rate
                return rate
    except Exception:
        pass
    return None


def _hkd_to_usd(raw: float) -> float:
    """Convert HKD→USD using the live FX rate when available, else 7.8."""
    rate = _live_hkd_to_usd() or HKD_TO_USD
    return raw / rate if raw else 0.0


@dataclass
class Funds:
    """Account funds (USD-normalized)."""
    cash: float = 0.0                 # us_cash
    buying_power: float = 0.0         # usd_net_cash_power — cash-only BP (matches moomoo display)
    margin_power: float = 0.0         # moomoo `power` — margin-inclusive BP (cash + securities)
    fund: float = 0.0                 # fund_assets (USD)
    total_assets: float = 0.0
    total_liabilities: float = 0.0
    net_assets: float = 0.0
    margin_used_pct: float = 0.0
    currency: str = ''

    @property
    def liquid(self) -> float:
        """Cash + fund assets — the deployable pool."""
        return self.cash + self.fund


@dataclass
class Portfolio:
    """Full REAL-account state: funds + stock positions + option positions.

    stocks:  ticker -> {qty, cost, price, mv, pl, pl_pct}
    options: code   -> {ticker, type, strike, expiry, qty, cost, pl, pl_pct, delta, dte}
    """
    funds: Funds = field(default_factory=Funds)
    stocks: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)

    @property
    def stock_value(self) -> float:
        return sum(s.get('mv', 0) for s in self.stocks.values())

    @property
    def net_liquidation(self) -> float:
        return self.funds.liquid + self.stock_value

    @property
    def option_tickers(self) -> set:
        """Set of underlying tickers with open option positions."""
        return {o['ticker'] for o in self.options.values() if o.get('ticker')}

    @property
    def csp_liability(self) -> float:
        """Cash required if ALL short puts assign at strike."""
        return sum(
            abs(o['strike']) * abs(o['qty']) * 100
            for o in self.options.values()
            if o.get('type') == 'PUT'
        )


def parse_option_code(code: str) -> Optional[tuple]:
    """Parse a moomoo option code into (ticker, expiry_iso, type, strike).

    Returns None for non-option codes or malformed input.
    type is 'CALL' or 'PUT'; strike is the dollar value (encoded strike ÷ 1000).
    """
    if not code or not _OPTION_DETECT_RE.search(str(code)):
        return None
    m = _OPTION_RE.match(str(code))
    if not m:
        return None
    ticker, yr, mo, dy, cp, strike_enc = m.groups()
    return (
        ticker,
        f'20{yr}-{mo}-{dy}',
        'CALL' if cp == 'C' else 'PUT',
        float(strike_enc) / 1000.0,
    )


def is_option_code(code: str) -> bool:
    """True if code looks like an option contract."""
    return bool(code and _OPTION_DETECT_RE.search(str(code)))


def _normalize_fund(raw: float, currency: str) -> float:
    """fund_assets is reported in HKD for some accounts — convert to USD."""
    if currency == 'HKD' and raw:
        return _hkd_to_usd(raw)
    return raw or 0.0


def _finite(v) -> float:
    """Coerce a moomoo accinfo scalar to float; None/NaN → 0.0."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(x) else x


def _to_usd(raw: float, currency: str) -> float:
    """Normalize a moomoo accinfo money field to USD.

    Moomoo reports several accinfo fields (total_assets, total_liabilities,
    net_assets, power) in the *account currency* — which is HKD for many accounts.
    Reading them raw inflates USD figures ~7.8×. This applies the HKD→USD
    conversion when the account currency is HKD.
    """
    if currency == 'HKD' and raw:
        return _hkd_to_usd(raw)
    return raw or 0.0


def _buying_power(f, currency: str) -> float:
    """Cash buying power (USD) — what moomoo displays as 'Buying Power'.

    This is ``usd_net_cash_power`` (cash-only purchasing power, no margin).
    The ``usd_`` prefix is literal — moomoo reports this field ALREADY in USD
    regardless of the account's reporting currency (HKD for many accounts), so
    no HKD→USD conversion is applied. The margin-inclusive ``power`` is exposed
    separately via ``_margin_power`` for accounts that want to see total
    purchasing power against securities.
    """
    return _finite(f.get('usd_net_cash_power'))


def _margin_power(f, currency: str) -> float:
    """Margin-inclusive purchasing power (USD) — ``power`` if moomoo returns it.

    Purchasing power against cash + securities. 0 when unavailable (the cash-only
    buying_power should be used instead). Normalized to USD.
    """
    return _to_usd(_finite(f.get('power')), currency)


def fetch_funds(trd) -> Funds:
    """Read funds for the first REAL account found. Pass an open OpenSecTradeContext."""
    ret, acc_list = trd.get_acc_list()
    if ret != RET_OK:
        return Funds()
    for _, acc in acc_list.iterrows():
        if str(acc.get('trd_env', '')) == 'SIMULATE':
            continue
        acc_id = acc['acc_id']
        ret2, funds = trd.accinfo_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
        if ret2 != RET_OK or funds is None or len(funds) == 0:
            continue
        f = funds.iloc[0]
        currency = str(f.get('currency', ''))
        return Funds(
            cash=_finite(f.get('us_cash')),
            buying_power=_buying_power(f, currency),
            margin_power=_margin_power(f, currency),
            fund=_normalize_fund(float(f.get('fund_assets', 0) or 0), currency),
            total_assets=_to_usd(_finite(f.get('total_assets')), currency),
            total_liabilities=_to_usd(_finite(f.get('total_liabilities')), currency),
            net_assets=_to_usd(_finite(f.get('net_assets')), currency),
            margin_used_pct=float(f.get('margin_used_pct', 0) or 0),
            currency=currency,
        )
    return Funds()


def fetch_positions(trd) -> tuple[dict, dict]:
    """Read stock + option positions for the first REAL account.

    Returns (stocks, options) dicts. Pass an open OpenSecTradeContext.
    """
    ret, acc_list = trd.get_acc_list()
    stocks: dict = {}
    options: dict = {}
    if ret != RET_OK:
        return stocks, options

    for _, acc in acc_list.iterrows():
        if str(acc.get('trd_env', '')) == 'SIMULATE':
            continue
        acc_id = acc['acc_id']
        ret2, pos_data = trd.position_list_query(
            trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
        if ret2 != RET_OK or pos_data is None:
            continue

        for _, p in pos_data.iterrows():
            code = p['code']
            qty = p['qty']
            if qty == 0:
                continue
            if '..' in str(code):
                continue

            if is_option_code(code):
                parsed = parse_option_code(code)
                if parsed is None:
                    continue
                ticker, expiry, opt_type, strike = parsed
                options[code] = {
                    'code': code,
                    'ticker': ticker,
                    'type': opt_type,
                    'strike': strike,
                    'expiry': expiry,
                    'qty': qty,
                    'cost': float(p.get('cost_price', 0) or 0),
                    'pl': float(p.get('pl_val', 0) or 0),
                    'pl_pct': float(p.get('pl_ratio', 0) or 0),
                    'delta': 0.0,   # filled later from quote snapshot
                    'dte': 0,
                }
            elif str(code).startswith('US.'):
                ticker = str(code).replace('US.', '')
                price = float(p.get('nominal_price', 0) or 0)
                cost = float(p.get('cost_price', 0) or 0)
                stocks[ticker] = {
                    'qty': qty,
                    'cost': cost,
                    'price': price,
                    'mv': qty * price,
                    'pl': float(p.get('pl_val', 0) or 0),
                    'pl_pct': float(p.get('pl_ratio', 0) or 0),
                }
        # First REAL account only
        break
    return stocks, options


def fetch_orders(trd, start: str = '2024-01-01', end: Optional[str] = None) -> list[dict]:
    """Read filled + working orders for the first REAL account, deduped by order_id.

    Returns a list of normalized dicts: {order_id, date, code, side, qty, price, status}.
    `date` is the first 10 chars of updated_time (YYYY-MM-DD). Used by the portfolio
    summary to compute realized option income + monthly breakdown.
    """
    end = end or date.today().isoformat()
    ret, acc_list = trd.get_acc_list()
    if ret != RET_OK:
        return []

    for _, acc in acc_list.iterrows():
        if str(acc.get('trd_env', '')) == 'SIMULATE':
            continue
        acc_id = acc['acc_id']
        raw_rows = []

        ret1, hist = trd.history_order_list_query(
            trd_env=TrdEnv.REAL, acc_id=acc_id, start=start, end=end)
        if ret1 == RET_OK and hist is not None:
            raw_rows.extend(row for _, row in hist.iterrows())

        ret2, live = trd.order_list_query(
            trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
        if ret2 == RET_OK and live is not None:
            raw_rows.extend(row for _, row in live.iterrows())

        seen: set = set()
        out: list[dict] = []
        for r in raw_rows:
            oid = str(r.get('order_id', ''))
            if not oid or oid in seen:
                continue
            seen.add(oid)
            out.append({
                'order_id': oid,
                'date': str(r.get('updated_time', ''))[:10],
                'code': str(r.get('code', '')),
                'side': str(r.get('trd_side', '')),
                'qty': float(r.get('qty', 0) or 0),
                'price': float(r.get('dealt_avg_price', 0) or r.get('price', 0) or 0),
                'status': str(r.get('order_status', '')),
            })
        return out
    return []


def fetch_portfolio(host: str = '127.0.0.1', port: int = 11111) -> Portfolio:
    """Open one trade context, pull funds + positions, close. Returns empty Portfolio on failure."""
    try:
        trd = OpenSecTradeContext(host=host, port=port, ai_type=1)
        try:
            funds = fetch_funds(trd)
            stocks, options = fetch_positions(trd)
            return Portfolio(funds=funds, stocks=stocks, options=options)
        finally:
            trd.close()
    except Exception:
        return Portfolio()


def fetch_live_portfolio(host: str = '127.0.0.1', port: int = 11111):
    """Convenience: fetch portfolio as tuple for screener/OIE engine compatibility.
    Returns (holdings_dict, cash, buying_power, fund, option_tickers).
    Falls back to safe defaults if OpenD is unreachable."""
    pf = fetch_portfolio(host, port)
    if not pf.stocks and pf.funds.cash == 0 and pf.funds.fund == 0:
        return {}, 817.0, 48638.89, 48500.0, set()
    holdings = {t: pos['qty'] for t, pos in pf.stocks.items()}
    return holdings, pf.funds.cash, pf.funds.buying_power, pf.funds.fund, pf.option_tickers


def fetch_portfolio_and_orders(
    host: str = '127.0.0.1', port: int = 11111, start: str = '2024-01-01',
) -> tuple[Portfolio, list[dict]]:
    """One connection: funds + positions + order history. For the portfolio summary.

    Returns (Portfolio, orders). On failure returns (empty Portfolio, []).
    """
    try:
        trd = OpenSecTradeContext(host=host, port=port, ai_type=1)
        try:
            funds = fetch_funds(trd)
            stocks, options = fetch_positions(trd)
            orders = fetch_orders(trd, start=start)
            return Portfolio(funds=funds, stocks=stocks, options=options), orders
        finally:
            trd.close()
    except Exception:
        return Portfolio(), []
