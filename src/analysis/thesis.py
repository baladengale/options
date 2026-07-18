"""
Thesis-break gates — codable fundamental deterioration checks on held positions.

Implements loss-management-playbook.md §5: objective criteria that decide when a
quality-stock thesis is BROKEN (sell despite the loss) vs. when a drawdown is
just volatility (hold). Complements src/risk/holdings_exit.py price backstops.

Gate convention: each gate function returns Optional[bool] —
    True  = gate FAILED (thesis damage)
    False = gate passed
    None  = insufficient data (reported as NO_DATA, never counted as a failure)

All series are ordered oldest → newest. Thresholds default from the playbook;
callers should pass values from config/rules.yaml `holdings_exit.thesis`.

Deterministic only — no AI, no judgment calls. UNVALIDATED — backtest pending.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ThesisInputs:
    """Fundamental series for one ticker (oldest → newest). None = unavailable."""
    revenue_quarterly: Optional[list] = None        # quarterly revenue (needs ≥6 for 2 YoY points)
    eps_quarterly: Optional[list] = None            # quarterly diluted EPS
    gross_margin_quarterly: Optional[list] = None   # quarterly gross margin, percent points
    total_debt: Optional[float] = None              # latest total debt
    ebitda_ttm: Optional[float] = None              # trailing-12-month EBITDA
    fcf_yield_pct: Optional[float] = None           # FCF / market cap, percent
    net_capital_raising: Optional[float] = None     # TTM debt+equity issuance − repayment


@dataclass
class ThesisReport:
    """Result of running all gates for one ticker."""
    ticker: str
    gates: dict = field(default_factory=dict)   # gate name → 'PASS' | 'FAIL' | 'NO_DATA'
    broken: bool = False
    reasons: list = field(default_factory=list)

    @property
    def failed_count(self) -> int:
        return sum(1 for v in self.gates.values() if v == 'FAIL')

    @property
    def evaluated_count(self) -> int:
        return sum(1 for v in self.gates.values() if v != 'NO_DATA')


def yoy_growth_series(quarterly: list) -> list:
    """YoY growth points from a quarterly series (q vs q-4). Oldest → newest."""
    out = []
    for i in range(4, len(quarterly)):
        prev = quarterly[i - 4]
        if prev and prev != 0:
            out.append((quarterly[i] - prev) / abs(prev))
    return out


def gate_growth_stall(revenue_quarterly: Optional[list],
                      stall_quarters: int = 2) -> Optional[bool]:
    """
    FAIL if the last `stall_quarters` YoY revenue growth values are all negative.
    93% of stalled companies never regain 2% growth (playbook §5).
    """
    if not revenue_quarterly:
        return None
    growth = yoy_growth_series(revenue_quarterly)
    if len(growth) < stall_quarters:
        return None
    return all(g < 0 for g in growth[-stall_quarters:])


def gate_dual_deceleration(revenue_quarterly: Optional[list],
                           eps_quarterly: Optional[list],
                           decel_quarters: int = 3) -> Optional[bool]:
    """
    FAIL if BOTH revenue and EPS YoY growth decelerated for `decel_quarters`
    consecutive quarters (each growth point lower than the one before).
    """
    if not revenue_quarterly or not eps_quarterly:
        return None
    rev_g = yoy_growth_series(revenue_quarterly)
    eps_g = yoy_growth_series(eps_quarterly)
    # Need decel_quarters transitions → decel_quarters + 1 growth points
    if len(rev_g) < decel_quarters + 1 or len(eps_g) < decel_quarters + 1:
        return None

    def _decelerating(series: list) -> bool:
        tail = series[-(decel_quarters + 1):]
        return all(tail[i + 1] < tail[i] for i in range(len(tail) - 1))

    return _decelerating(rev_g) and _decelerating(eps_g)


def gate_margin_erosion(gross_margin_quarterly: Optional[list],
                        drop_bps: float = 100,
                        declining_quarters: int = 3) -> Optional[bool]:
    """
    FAIL if gross margin fell ≥ drop_bps over 2 years (8 quarters) AND declined
    YoY (q vs q-4) for `declining_quarters` consecutive quarters. Margins in
    percent points (100 bps = 1.0 point).
    """
    m = gross_margin_quarterly
    if not m or len(m) < 4 + declining_quarters or len(m) < 9:
        return None
    two_year_drop = m[-9] - m[-1]                    # 8 quarters ago vs now
    yoy_declines = [m[-(i + 1)] < m[-(i + 5)] for i in range(declining_quarters)]
    return two_year_drop >= (drop_bps / 100.0) and all(yoy_declines)


def gate_balance_sheet(total_debt: Optional[float],
                       ebitda_ttm: Optional[float],
                       max_ratio: float = 4.5) -> Optional[bool]:
    """FAIL if Debt/EBITDA > max_ratio, or if there's debt against non-positive EBITDA."""
    if total_debt is None or ebitda_ttm is None:
        return None
    if total_debt <= 0:
        return False
    if ebitda_ttm <= 0:
        return True
    return (total_debt / ebitda_ttm) > max_ratio


def gate_cash_flow(fcf_yield_pct: Optional[float],
                   net_capital_raising: Optional[float],
                   min_yield_pct: float = 2.0) -> Optional[bool]:
    """FAIL if FCF yield < min_yield_pct AND the company is net-raising capital."""
    if fcf_yield_pct is None or net_capital_raising is None:
        return None
    return fcf_yield_pct < min_yield_pct and net_capital_raising > 0


def evaluate_thesis(ticker: str,
                    inputs: ThesisInputs,
                    stall_quarters: int = 2,
                    decel_quarters: int = 3,
                    gross_margin_drop_bps: float = 100,
                    debt_ebitda_max: float = 4.5,
                    fcf_yield_min_pct: float = 2.0,
                    broken_min_gates: int = 2) -> ThesisReport:
    """
    Run all gates. Thesis is BROKEN when ≥ broken_min_gates gates FAIL, or when
    growth_stall alone fails (it is catastrophic on its own — playbook §5).
    NO_DATA gates never count toward broken.
    """
    checks = {
        'growth_stall': gate_growth_stall(inputs.revenue_quarterly, stall_quarters),
        'dual_deceleration': gate_dual_deceleration(
            inputs.revenue_quarterly, inputs.eps_quarterly, decel_quarters),
        'margin_erosion': gate_margin_erosion(
            inputs.gross_margin_quarterly, gross_margin_drop_bps),
        'balance_sheet': gate_balance_sheet(
            inputs.total_debt, inputs.ebitda_ttm, debt_ebitda_max),
        'cash_flow': gate_cash_flow(
            inputs.fcf_yield_pct, inputs.net_capital_raising, fcf_yield_min_pct),
    }

    report = ThesisReport(ticker=ticker)
    for name, failed in checks.items():
        if failed is None:
            report.gates[name] = 'NO_DATA'
        elif failed:
            report.gates[name] = 'FAIL'
            report.reasons.append(name)
        else:
            report.gates[name] = 'PASS'

    report.broken = (report.gates.get('growth_stall') == 'FAIL'
                     or report.failed_count >= broken_min_gates)
    return report


def fetch_thesis_inputs(ticker: str) -> ThesisInputs:
    """
    Best-effort yfinance adapter. Missing data → None fields → gates report
    NO_DATA (never fabricated). Kept separate so the gates stay pure/testable.
    """
    inputs = ThesisInputs()
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        try:
            stmt = t.quarterly_income_stmt  # columns newest → oldest
            if stmt is not None and not stmt.empty:
                cols = list(stmt.columns)[::-1]  # reorder oldest → newest
                def _series(row_name):
                    if row_name in stmt.index:
                        vals = [stmt.at[row_name, c] for c in cols]
                        vals = [float(v) for v in vals if v == v and v is not None]
                        return vals if vals else None
                    return None
                inputs.revenue_quarterly = _series('Total Revenue')
                inputs.eps_quarterly = _series('Diluted EPS')
                gross = _series('Gross Profit')
                if gross and inputs.revenue_quarterly and len(gross) == len(inputs.revenue_quarterly):
                    inputs.gross_margin_quarterly = [
                        (g / r * 100.0) for g, r in zip(gross, inputs.revenue_quarterly) if r]
        except Exception:
            pass

        try:
            info = t.info or {}
            inputs.total_debt = info.get('totalDebt')
            inputs.ebitda_ttm = info.get('ebitda')
            fcf = info.get('freeCashflow')
            mcap = info.get('marketCap')
            if fcf is not None and mcap:
                inputs.fcf_yield_pct = fcf / mcap * 100.0
        except Exception:
            pass

        try:
            cf = t.quarterly_cashflow
            if cf is not None and not cf.empty:
                def _ttm(row_name):
                    if row_name in cf.index:
                        vals = [float(v) for v in list(cf.loc[row_name])[:4] if v == v]
                        return sum(vals) if vals else None
                    return None
                issued = (_ttm('Issuance Of Debt') or 0) + (_ttm('Issuance Of Capital Stock') or 0)
                repaid = abs(_ttm('Repayment Of Debt') or 0) + abs(_ttm('Repurchase Of Capital Stock') or 0)
                inputs.net_capital_raising = issued - repaid
        except Exception:
            pass
    except Exception:
        pass
    return inputs
