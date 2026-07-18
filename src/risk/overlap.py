"""
Put/Call liability overlap analysis.

Extracted (and de-printed) from the former scripts/liability_overlap.py Part 2 —
the only piece of that script not already covered by the holding-decision engine.
For every ticker that has BOTH short calls and short puts, detect straddles,
strangles, and stacked-call risk, and compute the net share scenarios if all
calls exercise and/or all puts assign.

Pure data in, structured data out — no printing, no network. Deterministic.

Usage:
    from src.risk.overlap import analyze_overlap
    reports = analyze_overlap(pf.options, pf.stocks, snapshots=snap_map)
    for r in reports:
        ...  # r.straddles, r.net_if_all, r.stacked_calls, ...
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class OverlapLeg:
    code: str
    type: str               # 'CALL' | 'PUT'
    strike: float
    expiry: str
    qty: float
    dte: int = 0
    delta: float = 0.0
    cost: float = 0.0


@dataclass
class Straddle:
    strike: float
    expiry: str
    dte: int
    premium: float          # (call cost + put cost) × 100
    breakeven_low: float
    breakeven_high: float


@dataclass
class Strangle:
    expiry: str
    call_strikes: list
    put_strikes: list


@dataclass
class StackedCallStep:
    expiry: str
    dte: int
    shares_called: int      # this leg
    cumulative_called: int
    shares_remaining: int


@dataclass
class OverlapReport:
    ticker: str
    shares: int                 # shares currently owned
    share_price: float
    calls: list[OverlapLeg] = field(default_factory=list)
    puts: list[OverlapLeg] = field(default_factory=list)

    straddles: list[Straddle] = field(default_factory=list)
    strangles: list[Strangle] = field(default_factory=list)
    stacked_calls: list[StackedCallStep] = field(default_factory=list)

    # Net scenarios (share counts)
    call_shares: int = 0        # shares owed if all calls exercise
    put_shares: int = 0         # shares bought if all puts assign
    total_put_assign: float = 0.0   # cash needed if all puts assign

    @property
    def net_if_calls(self) -> int:
        return self.shares - self.call_shares

    @property
    def net_if_puts(self) -> int:
        return self.shares + self.put_shares

    @property
    def net_if_all(self) -> int:
        return self.shares - self.call_shares + self.put_shares


def _dte(expiry: str, today: date) -> int:
    try:
        return (date.fromisoformat(expiry) - today).days
    except Exception:
        return 0


def _delta_from_snapshot(snapshots: Optional[dict], code: str) -> float:
    if not snapshots:
        return 0.0
    row = snapshots.get(code)
    if row is None:
        return 0.0
    try:
        return float(row.get('option_delta', 0) or 0)
    except Exception:
        return 0.0


def analyze_overlap(
    options: dict,
    stocks: dict,
    snapshots: Optional[dict] = None,
    today: Optional[date] = None,
) -> list[OverlapReport]:
    """Analyze put/call overlap per ticker.

    options:   code -> position dict (from PortfolioLoader) with keys
               ticker, type, strike, expiry, qty, cost.
    stocks:    ticker -> {qty, price, ...} (from PortfolioLoader).
    snapshots: optional code -> raw moomoo snapshot row (for delta). Omit → delta 0.
    today:     reference date for DTE (default date.today()).

    Returns one OverlapReport per ticker that has BOTH calls and puts, sorted by ticker.
    """
    today = today or date.today()

    by_ticker: dict = defaultdict(lambda: {'calls': [], 'puts': []})
    for code, o in options.items():
        side = o.get('type', '').lower()
        bucket = 'calls' if side == 'call' else 'puts' if side == 'put' else None
        if bucket is None:
            continue
        leg = OverlapLeg(
            code=code,
            type=o.get('type', ''),
            strike=float(o.get('strike', 0) or 0),
            expiry=o.get('expiry', ''),
            qty=float(o.get('qty', 0) or 0),
            dte=_dte(o.get('expiry', ''), today),
            delta=_delta_from_snapshot(snapshots, code),
            cost=float(o.get('cost', 0) or 0),
        )
        by_ticker[o.get('ticker', '')][bucket].append(leg)

    reports: list[OverlapReport] = []
    for ticker in sorted(by_ticker):
        calls = by_ticker[ticker]['calls']
        puts = by_ticker[ticker]['puts']
        if not calls or not puts:
            continue  # no overlap — single-sided

        shares = int(stocks.get(ticker, {}).get('qty', 0) or 0)
        share_price = float(stocks.get(ticker, {}).get('price', 0) or 0)

        rep = OverlapReport(ticker=ticker, shares=shares, share_price=share_price,
                            calls=calls, puts=puts)

        rep.call_shares = sum(abs(c.qty) for c in calls) * 100
        rep.put_shares = sum(abs(p.qty) for p in puts) * 100
        rep.total_put_assign = sum(abs(p.qty) * p.strike * 100 for p in puts)

        # Same-strike, same-expiry straddles
        for c in calls:
            for p in puts:
                if c.strike == p.strike and c.expiry == p.expiry:
                    premium = (c.cost + p.cost) * 100
                    rep.straddles.append(Straddle(
                        strike=c.strike, expiry=c.expiry, dte=c.dte,
                        premium=premium,
                        breakeven_low=c.strike - c.cost - p.cost,
                        breakeven_high=c.strike + c.cost + p.cost,
                    ))

        # Same-expiry strangles (only meaningful classification when no straddle)
        if not rep.straddles:
            by_expiry = defaultdict(lambda: {'calls': [], 'puts': []})
            for c in calls:
                by_expiry[c.expiry]['calls'].append(c.strike)
            for p in puts:
                by_expiry[p.expiry]['puts'].append(p.strike)
            for exp, pair in by_expiry.items():
                if pair['calls'] and pair['puts']:
                    rep.strangles.append(Strangle(
                        expiry=exp, call_strikes=pair['calls'], put_strikes=pair['puts']))

        # Stacked-call risk: cumulative shares called away, by expiry (>=2 calls)
        if len(calls) >= 2:
            cumulative = 0
            for c in sorted(calls, key=lambda x: x.expiry):
                cumulative += abs(c.qty) * 100
                rep.stacked_calls.append(StackedCallStep(
                    expiry=c.expiry, dte=c.dte,
                    shares_called=abs(c.qty) * 100,
                    cumulative_called=cumulative,
                    shares_remaining=shares - cumulative,
                ))

        reports.append(rep)

    return reports
