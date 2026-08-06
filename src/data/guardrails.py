"""
Portfolio Guardrails — position sizing, capital allocation, risk limits.

All limits based on pro trader research (2025 consensus):
- 15% max per position (standard: 20-25%, conservative: 15%)
- 25% cash buffer (standard: 25%, aggressive: 10%)
- Max 8 open wheel positions (management bandwidth limit)
- Max 2 new trades per day (prevents overtrading)
- 30% max margin utilization
- Worst-case assignment: model ALL CSPs going ITM simultaneously

Usage:
    from src.data.guardrails import GuardrailChecker
    gc = GuardrailChecker(net_liq=221000, cash=800, buying_power=48000,
                           margin_used=0, open_positions=[...])
    report = gc.check()
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GuardrailReport:
    """Result of all guardrail checks."""
    all_clear: bool = True
    warnings: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)

    # Metrics
    net_liq: float = 0.0
    cash_available: float = 0.0
    buying_power: float = 0.0
    deployed_pct: float = 0.0                     # % of net liq deployed
    cash_buffer_pct: float = 0.0                   # % unallocated
    margin_used_pct: float = 0.0
    open_positions: int = 0
    max_single_position_pct: float = 0.0
    max_sector_pct: float = 0.0
    worst_case_assignment: float = 0.0              # total CSP liability
    worst_case_shortfall: float = 0.0               # shortfall if all assigned
    daily_order_count: int = 0
    recommended_max_new: int = 0


class GuardrailChecker:
    """
    Portfolio risk guardrails. Check before every trade.

    Hard limits (BLOCK new trades):
        - Single position > 15% of net liq
        - All CSPs assigned simultaneously > available funds
        - Cash buffer < 10% (critical)
        - Daily order count > 2

    Soft limits (WARN only):
        - Open wheel positions > 8
        - Cash buffer < 25%
        - Margin > 30%
        - Single sector > 25%
    """

    # ── Limits (loaded from config/rules.yaml) ──
    @classmethod
    def _cfg(cls):
        from src.config import get_config
        return get_config()

    @classmethod
    def MAX_POSITION_PCT(cls):
        return cls._cfg().max_single_position_pct

    @classmethod
    def MAX_SECTOR_PCT(cls):
        return cls._cfg().max_sector_pct

    @classmethod
    def MIN_CASH_BUFFER_WARN(cls):
        return cls._cfg().cash_buffer_warn

    @classmethod
    def MIN_CASH_BUFFER_CRITICAL(cls):
        return cls._cfg().cash_buffer_critical

    @classmethod
    def MAX_OPEN_POSITIONS(cls):
        return cls._cfg().max_open_positions

    @classmethod
    def MAX_DAILY_ORDERS(cls):
        return cls._cfg().max_daily_new_positions

    @classmethod
    def MAX_MARGIN_PCT(cls):
        return cls._cfg().max_margin_pct

    @classmethod
    def CSP_CAPITAL_COVERAGE(cls):
        return cls._cfg().csp_capital_coverage

    def __init__(self, net_liq: float, cash: float, buying_power: float,
                 margin_used: float = 0.0,
                 open_positions: Optional[list[dict]] = None,
                 daily_order_count: int = 0,
                 cc_assignment_notional: float = 0.0):
        self._net_liq = net_liq
        self._cash = cash
        self._bp = buying_power
        self._margin = margin_used
        self._positions = open_positions or []
        self._daily_orders = daily_order_count
        self._cc_notional = cc_assignment_notional

    def check(self) -> GuardrailReport:
        """Run all guardrails. Returns report with warnings + blocks."""
        r = GuardrailReport()
        r.net_liq = self._net_liq
        r.cash_available = self._cash
        r.buying_power = self._bp
        r.daily_order_count = self._daily_orders

        # ── Deployed capital ──
        r.deployed_pct = 1.0 - (self._cash / self._net_liq) if self._net_liq > 0 else 1.0
        r.cash_buffer_pct = self._cash / self._net_liq * 100 if self._net_liq > 0 else 0
        r.margin_used_pct = self._margin / self._net_liq * 100 if self._net_liq > 0 else 0

        # ── Cash buffer ──
        if r.cash_buffer_pct < self.MIN_CASH_BUFFER_CRITICAL() * 100:
            r.blocks.append(
                f"Cash buffer {r.cash_buffer_pct:.1f}% < {self.MIN_CASH_BUFFER_CRITICAL()*100:.0f}% critical. "
                f"Keep ≥{self.MIN_CASH_BUFFER_WARN()*100:.0f}% cash. Close positions or add funds.")
        elif r.cash_buffer_pct < self.MIN_CASH_BUFFER_WARN() * 100:
            r.warnings.append(
                f"Cash buffer {r.cash_buffer_pct:.1f}% < {self.MIN_CASH_BUFFER_WARN()*100:.0f}% recommended. "
                f"Consider reducing position sizes.")

        # ── Margin ──
        if r.margin_used_pct > self.MAX_MARGIN_PCT() * 100:
            r.warnings.append(f"Margin {r.margin_used_pct:.1f}% > {self.MAX_MARGIN_PCT()*100:.0f}% limit.")

        # ── Position count ──
        r.open_positions = len(self._positions)
        if r.open_positions > self.MAX_OPEN_POSITIONS():
            r.warnings.append(f"{r.open_positions} open positions > {self.MAX_OPEN_POSITIONS()} max. "
                              f"Management bandwidth exceeded. Close before opening new.")

        # ── Daily orders ──
        r.recommended_max_new = max(0, self.MAX_DAILY_ORDERS() - self._daily_orders)
        if self._daily_orders >= self.MAX_DAILY_ORDERS():
            r.warnings.append(f"Daily order limit ({self.MAX_DAILY_ORDERS()}) reached. Wait until tomorrow.")

        # ── Single position concentration ──
        for pos in self._positions:
            notional = pos.get('notional', 0)
            pct = (notional / self._net_liq * 100) if self._net_liq > 0 else 0
            if pct > r.max_single_position_pct:
                r.max_single_position_pct = pct
            if pct > self.MAX_POSITION_PCT() * 100:
                r.warnings.append(
                    f"{pos.get('ticker', '??')} at {pct:.1f}% of portfolio "
                    f"> {self.MAX_POSITION_PCT()*100:.0f}% limit. Reduce via CC only; no new CSP/stock buys.")

        # ── Sector concentration ──
        sectors = {}
        for pos in self._positions:
            sec = pos.get('sector', 'Unknown')
            sectors[sec] = sectors.get(sec, 0) + pos.get('notional', 0)
        for sec, val in sectors.items():
            pct = val / self._net_liq * 100 if self._net_liq > 0 else 0
            if pct > r.max_sector_pct:
                r.max_sector_pct = pct
            if pct > self.MAX_SECTOR_PCT() * 100:
                r.warnings.append(f"{sec} sector at {pct:.1f}% > {self.MAX_SECTOR_PCT()*100:.0f}% limit.")

        # ── Worst-case assignment stress test ──
        # Available resources if ALL CSPs assign simultaneously:
        #   liquid cash (100%) + margin BP × bp_margin_buffer + CC notional × cc_assignment_buffer
        # CC proceeds are haircut because the stock may be below all strikes at expiry,
        # making assignment proceeds zero. The buffer parameter controls how much credit
        # we give CCs in the worst case (0.50 = 50% assumed assignable).
        csp_total = sum(p.get('csp_liability', 0) for p in self._positions)
        r.worst_case_assignment = csp_total
        cc_available = self._cc_notional * self._cfg().cc_assignment_buffer
        available = self._cash + self._bp * self._cfg().bp_margin_buffer + cc_available
        r.worst_case_shortfall = csp_total - available
        if r.worst_case_shortfall > 0:
            r.warnings.append(
                f"⚠️  Worst-case: all CSPs assigned = ${csp_total:,.0f}. "
                f"Available: ${available:,.0f} (liquid ${self._cash:,.0f} + BP/{1/self._cfg().bp_margin_buffer:.0f} ${self._bp * self._cfg().bp_margin_buffer:,.0f}"
                f"{' + CC/' + str(int(1/self._cfg().cc_assignment_buffer)) + ' ' + '${:,.0f}'.format(cc_available) if cc_available > 0 else ''}). "
                f"Shortfall: ${r.worst_case_shortfall:,.0f}. "
                f"Reduce CSP count or increase cash buffer.")
        else:
            r.all_clear = r.all_clear and len(r.blocks) == 0  # only clear if no blocks either

        # ── CSP capital deployed concentration ──
        csp_deployed_pct = (csp_total / self._net_liq) if self._net_liq > 0 else 0
        csp_limit = self._cfg().max_csp_deployed_pct
        if csp_deployed_pct > csp_limit:
            r.blocks.append(
                f"CSP capital deployed {csp_deployed_pct:.1%} > {csp_limit:.0%} limit. "
                f"Close or let expire before opening new CSPs.")

        r.all_clear = len(r.blocks) == 0
        return r

    def check_new_trade(self, ticker: str, strategy: str, notional: float,
                        sector: str = 'Unknown') -> GuardrailReport:
        """Check if a new trade would pass all guardrails."""
        r = GuardrailReport()

        # ── Check over-concentrated position rules ──
        current_pos_pct = 0.0
        for pos in self._positions:
            if pos.get('ticker') == ticker:
                current_pos_pct = (pos.get('notional', 0) / self._net_liq * 100) if self._net_liq > 0 else 0
                break

        # If position already >15%, only allow CC to reduce exposure
        if current_pos_pct > self.MAX_POSITION_PCT() * 100:
            is_cc = strategy in ('CC', 'COVERED_CALL')
            is_csp = strategy in ('CSP', 'CASH_SECURED_PUT')
            is_buy = strategy in ('STOCK_BUY', 'BUY')

            if is_csp or is_buy:
                r.blocks.append(
                    f"{ticker} at {current_pos_pct:.1f}% > {self.MAX_POSITION_PCT()*100:.0f}% limit. "
                    f"Only CC allowed to reduce concentration. No new CSP/stock buys.")
                return r
            elif not is_cc:
                r.warnings.append(
                    f"{ticker} at {current_pos_pct:.1f}% > {self.MAX_POSITION_PCT()*100:.0f}% limit. "
                    f"Consider CC to reduce position gradually.")

        # ── Run full guardrail check with simulated new position ──
        new_positions = self._positions + [{
            'ticker': ticker, 'strategy': strategy,
            'notional': notional, 'sector': sector,
            'csp_liability': notional if strategy in ('CSP', 'CASH_SECURED_PUT') else 0,
        }]
        gc = GuardrailChecker(
            net_liq=self._net_liq, cash=self._cash,
            buying_power=self._bp, margin_used=self._margin,
            open_positions=new_positions,
            daily_order_count=self._daily_orders + 1,
        )
        r = gc.check()
        return r


# ═══════════════════════════════════════════════════════════════
# Pre-computed sector map for watchlist tickers
# ═══════════════════════════════════════════════════════════════

SECTOR_MAP = {
    'V': 'Financial', 'MSFT': 'Technology', 'GOOGL': 'Technology',
    'GOOG': 'Technology', 'AAPL': 'Technology', 'AMZN': 'Consumer',
    'NVDA': 'Technology', 'META': 'Technology', 'AVGO': 'Technology',
    'ADBE': 'Technology', 'CRM': 'Technology', 'AMD': 'Technology',
    'TSLA': 'Consumer', 'PLTR': 'Technology', 'SOFI': 'Financial',
    'IREN': 'Technology', 'COST': 'Consumer', 'MRVL': 'Technology',
    'ASTS': 'Technology', 'MU': 'Technology', 'SPCX': 'Aerospace',
    'NBIS': 'Technology', 'BE': 'Energy', 'VOO': 'ETF',
    'SPY': 'ETF', 'SPMO': 'ETF', 'QQQ': 'ETF', 'IBIT': 'Crypto',
    'IWM': 'ETF', 'VUG': 'ETF', 'VGT': 'ETF', 'CRWV': 'Technology',
    'LITE': 'Technology',
}
