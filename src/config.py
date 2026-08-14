"""
Configuration loader — reads config/rules.yaml, provides typed access.

Usage:
    from src.config import get_config
    cfg = get_config()
    delta_range = cfg.delta_range('csp', 'CAUTIOUS')  # → [0.15, 0.25]

All scripts read from here. Edit config/rules.yaml to change parameters.
No hardcoded values — everything flows from the config file.
"""

import os
import yaml
from typing import Optional

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config', 'rules.yaml')
_cache: Optional['Config'] = None


def get_config() -> 'Config':
    """Get or load config. Cached after first load."""
    global _cache
    if _cache is None:
        _cache = Config(_CONFIG_PATH)
    return _cache


def reload_config():
    """Force reload from file. Use after editing rules.yaml."""
    global _cache
    _cache = None
    return get_config()


class Config:
    """Typed access to all rules from config/rules.yaml."""

    def __init__(self, path: str):
        with open(path, 'r') as f:
            self._data = yaml.safe_load(f)

    # ═══════════════════════════════════════════════════════════
    # REGIME
    # ═══════════════════════════════════════════════════════════

    @property
    def vix_low(self) -> float:
        return self._data['regime']['vix']['complacent']

    @property
    def vix_normal(self) -> float:
        return self._data['regime']['vix']['normal']

    @property
    def vix_elevated(self) -> float:
        return self._data['regime']['vix']['elevated']

    @property
    def vix_high(self) -> float:
        return self._data['regime']['vix']['high']

    def position_mult(self, regime: str) -> float:
        return self._data['regime']['position_mult'].get(regime, 0.5)

    @property
    def credit_stress_position_mult_cap(self):
        """Hard cap on position_mult when credit_regime == STRESSED.

        Returns None when the gate is disabled. The regime vote tally only
        counts stressed credit as one -1 vote; this cap prevents full-size
        positioning while credit is stressed (rules.yaml
        regime.credit_stress_position_mult_cap).
        """
        cap = self._data.get('regime', {}).get('credit_stress_position_mult_cap')
        return float(cap) if cap is not None else None

    def cash_reserve_pct(self, regime: str) -> float:
        return self._data['regime']['cash_reserve_pct'].get(regime, 0.25)

    def csp_cc_ratio(self, regime: str) -> list:
        return self._data['regime']['csp_cc_ratio'].get(regime, [50, 50])

    def regime_from_vix(self, vix: float) -> str:
        if vix < self.vix_low:        return 'BULLISH'
        elif vix < self.vix_normal:   return 'NEUTRAL'
        elif vix < self.vix_elevated: return 'CAUTIOUS'
        elif vix < self.vix_high:     return 'VOLATILE'
        else:                          return 'BEARISH'

    # ═══════════════════════════════════════════════════════════
    # OPTION SELECTION
    # ═══════════════════════════════════════════════════════════

    def delta_range(self, strategy: str, regime: str) -> list:
        """Return [min, max] delta for strategy+regime. strategy='csp' or 'cc'."""
        return self._data['options']['delta'][strategy].get(regime, [0.20, 0.30])

    @property
    def dte_screen_min(self) -> int:
        return self._data['options']['dte']['screen_min']

    @property
    def dte_screen_max(self) -> int:
        return self._data['options']['dte']['screen_max']

    @property
    def dte_optimal_min(self) -> int:
        return self._data['options']['dte']['optimal_min']

    @property
    def dte_optimal_max(self) -> int:
        return self._data['options']['dte']['optimal_max']

    @property
    def dte_penalty_start(self) -> int:
        return self._data['options']['dte']['penalty_start']

    @property
    def dte_weekly_max(self) -> int:
        return self._data['options']['dte'].get('weekly_max', 14)

    @property
    def dte_long_start(self) -> int:
        return self._data['options']['dte'].get('long_start', 60)

    @property
    def dte_hard_block(self) -> int:
        return self._data['options']['dte']['hard_block']

    @property
    def roc_min_csp(self) -> float:
        return self._data['options']['roc_min']['csp']

    @property
    def roc_min_cc(self) -> float:
        return self._data['options']['roc_min']['cc']

    @property
    def iv_rank_min(self) -> float:
        return self._data['options']['iv_rank_min']

    @property
    def oi_min(self) -> int:
        return self._data['options']['liquidity']['open_interest_min']

    @property
    def volume_min(self) -> int:
        return self._data['options']['liquidity']['volume_min']

    @property
    def spread_max_pct(self) -> float:
        return self._data['options']['liquidity']['bid_ask_spread_max_pct']

    @property
    def earnings_blackout_days(self) -> int:
        return self._data['options']['earnings']['blackout_days']

    # ═══════════════════════════════════════════════════════════
    # SCORING WEIGHTS
    # ═══════════════════════════════════════════════════════════

    @property
    def scoring_weights(self) -> dict:
        return self._data['scoring']['weights']

    def contract_penalty(self, key: str) -> float:
        return self._data['scoring']['contract_penalty'].get(key, 0.0)

    # ═══════════════════════════════════════════════════════════
    # POSITION LIMITS
    # ═══════════════════════════════════════════════════════════

    @property
    def max_single_position_pct(self) -> float:
        return self._data['position_limits']['max_single_position_pct']

    @property
    def max_sector_pct(self) -> float:
        return self._data['position_limits']['max_sector_pct']

    @property
    def max_csp_deployed_pct(self) -> float:
        return self._data['position_limits']['max_csp_deployed_pct']

    @property
    def max_csp_deployed_volatile_pct(self) -> float:
        return self._data['position_limits']['max_csp_deployed_volatile_pct']

    @property
    def max_open_positions(self) -> int:
        return self._data['position_limits']['max_open_positions']

    @property
    def max_daily_new_positions(self) -> int:
        return self._data['position_limits']['max_daily_new_positions']

    @property
    def max_margin_pct(self) -> float:
        return self._data['position_limits']['max_margin_pct']

    @property
    def csp_capital_coverage(self) -> float:
        return self._data['position_limits'].get('csp_capital_coverage', 1.0)

    @property
    def bp_margin_buffer(self) -> float:
        return self._data['position_limits'].get('bp_margin_buffer', 0.50)

    @property
    def cc_assignment_buffer(self) -> float:
        """Haircut on CC assignment notional in worst-case coverage (0.0-1.0).
        
        CC proceeds are not guaranteed — the stock may be below all strikes at
        expiry. This parameter controls how much of the CC notional is counted
        as available funds in the worst-case CSP-assignment stress test.
        0.50 = count 50% (default conservative), 0.0 = ignore CCs entirely.
        """
        return self._data['position_limits'].get('cc_assignment_buffer', 0.50)

    @property
    def cash_buffer_warn(self) -> float:
        return self._data['position_limits'].get('cash_buffer_warn', 0.25)

    @property
    def cash_buffer_critical(self) -> float:
        return self._data['position_limits'].get('cash_buffer_critical', 0.10)

    # ═══════════════════════════════════════════════════════════
    # CSP PAUSE
    # ═══════════════════════════════════════════════════════════

    @property
    def csp_pause_vix(self) -> float:
        return self._data['csp_pause']['vix_above']

    @property
    def csp_pause_spy_sma(self) -> int:
        return self._data['csp_pause']['spy_below_sma']

    @property
    def csp_pause_regime_score(self) -> int:
        return self._data['csp_pause']['regime_min_score']

    @property
    def csp_pause_cash_reserve_pct(self) -> float:
        return self._data['csp_pause']['cash_reserve_below_pct']

    @property
    def csp_pause_stock_drop_pct(self) -> float:
        return self._data['csp_pause']['stock_drop_from_basis_pct']

    # ═══════════════════════════════════════════════════════════
    # STOP LOSS
    # ═══════════════════════════════════════════════════════════

    def stop_loss(self, key: str, default=None):
        """Get stop-loss parameter. e.g., stop_loss('far_close') → 3.0"""
        val = self._data.get('stop_loss', {}).get('premium_stop', {}).get(key)
        if val is not None:
            return val
        val = self._data.get('stop_loss', {}).get('delta', {}).get(key)
        return val if val is not None else default

    @property
    def stop_delta_csp_critical(self) -> float:
        return self._data['stop_loss']['delta']['csp_critical']

    @property
    def stop_delta_csp_itm(self) -> float:
        return self._data['stop_loss']['delta'].get('csp_itm', 0.50)

    @property
    def stop_delta_csp_decision(self) -> float:
        return self._data['stop_loss']['delta'].get('csp_decision', 0.40)

    @property
    def stop_delta_cc_critical(self) -> float:
        return self._data['stop_loss']['delta']['cc_critical']

    @property
    def stop_delta_cc_warn(self) -> float:
        return self._data['stop_loss']['delta'].get('cc_warn', 0.40)

    @property
    def stop_delta_cc_close(self) -> float:
        """CC |Δ| at which the engine rolls up-and-out (the autonomy threshold)."""
        return self._data['stop_loss']['delta'].get('cc_close', 0.60)

    @property
    def stop_delta_cc_decision(self) -> float:
        """CC |Δ| at which the engine starts monitoring (warn only, no action)."""
        return self._data['stop_loss']['delta'].get('cc_decision', 0.50)

    @property
    def stop_delta_cc_assign_dte(self) -> int:
        """CC Δ ≥ cc_close AND DTE ≤ this → HOLD for assignment (let the wheel turn).
        Near-expiry ITM calls have negligible time value — rolling just churns
        commissions. Assignment is the expected wheel outcome."""
        return int(self._data['stop_loss']['delta'].get('cc_assign_dte', 14))

    @property
    def stop_heavy_loss_abs(self) -> float:
        """Unconditional close at this absolute $ loss (catch-all).

        Legacy scalar — still honored as a single-band fallback when
        ``heavy_loss_bands`` is absent. New config should use the band table
        via ``stop_heavy_loss_for_premium``.
        """
        return self._data['stop_loss']['delta'].get('heavy_loss_abs', 1000)

    @property
    def stop_heavy_loss_bands(self) -> list[tuple[float, float]]:
        """Tiered absolute-loss floor keyed on total premium collected.

        Returns a list of ``(premium_max, max_loss)`` tuples sorted ascending
        by ``premium_max``. ``premium_max`` is the upper edge of each band;
        ``.inf`` is the catch-everything top band. Falls back to a single
        ``(inf, heavy_loss_abs)`` band when only the legacy scalar is present,
        so old configs keep working unchanged.
        """
        import math
        delta_cfg = self._data.get('stop_loss', {}).get('delta', {})
        raw_bands = delta_cfg.get('heavy_loss_bands')
        if not raw_bands:
            return [(math.inf, float(delta_cfg.get('heavy_loss_abs', 1000)))]
        out = []
        for b in raw_bands:
            pm = b.get('premium_max', math.inf)
            pm = math.inf if pm in ('.inf', 'inf', float('inf')) else float(pm)
            out.append((pm, float(b.get('max_loss', 1000))))
        out.sort(key=lambda t: t[0])
        return out

    def stop_heavy_loss_for_premium(self, premium_collected: float) -> float:
        """Return the absolute $ loss floor for a trade of this premium size.

        Picks the first band whose ``premium_max`` ≥ ``premium_collected``.
        A ``premium_collected`` of 0 (unknown) returns the smallest band —
        matching the old flat-floor behavior for un-instrumented call sites.
        """
        bands = self.stop_heavy_loss_bands
        pc = float(premium_collected or 0)
        for premium_max, max_loss in bands:
            if pc <= premium_max:
                return max_loss
        return bands[-1][1]   # top band (defensive — its premium_max is inf)

    # ═══════════════════════════════════════════════════════════
    # ROLLING DISCIPLINE
    # ═══════════════════════════════════════════════════════════

    def rolling(self, key: str, default=None):
        """Get rolling-discipline parameter. e.g., rolling('max_rolls_per_campaign') → 2"""
        return self._data.get('rolling', {}).get(key, default)

    # ═══════════════════════════════════════════════════════════
    # HOLDINGS EXIT FRAMEWORK
    # ═══════════════════════════════════════════════════════════

    def holdings_exit(self, key: str, default=None):
        """Get holdings-exit parameter. e.g., holdings_exit('backstop_hard_pct') → 0.40"""
        return self._data.get('holdings_exit', {}).get(key, default)

    def thesis_gate(self, key: str, default=None):
        """Get thesis-gate threshold. e.g., thesis_gate('debt_ebitda_max') → 4.5"""
        return self._data.get('holdings_exit', {}).get('thesis', {}).get(key, default)

    # ═══════════════════════════════════════════════════════════
    # THESIS VALIDATION (weekly checks: P/E, SMA, earnings, price perf)
    # ═══════════════════════════════════════════════════════════

    def thesis_validation(self, key: str, default=None):
        """Get a thesis-validation parameter from config/rules.yaml.

        e.g., thesis_validation('pe_ratio_critical') → 100
        """
        return self._data.get('thesis_validation', {}).get(key, default)

    @property
    def trusted_tickers(self) -> set:
        """Tickers that skip the P/E thesis check (user accepts their valuation).

        Read from thesis_validation.trusted_tickers; upper-cased and US.-stripped.
        """
        raw = self._data.get('thesis_validation', {}).get('trusted_tickers', []) or []
        return {str(t).upper().replace('US.', '') for t in raw}

    # ═══════════════════════════════════════════════════════════
    # TREND-MODULATED PROFIT BOOKING (specs/profit-loss-management-spec.md)
    # ═══════════════════════════════════════════════════════════

    def profit_take(self, key: str, default=None):
        """Top-level profit_take param. e.g., profit_take('dte_floor') → 21"""
        return self._data.get('profit_take', {}).get(key, default)

    def profit_take_csp(self, key: str, default=None):
        """CSP profit-booking param. e.g., profit_take_csp('strong_trend_target_pct') → 85"""
        return self._data.get('profit_take', {}).get('csp', {}).get(key, default)

    def profit_take_cc(self, key: str, default=None):
        """CC profit-booking param. e.g., profit_take_cc('roll_up_out_on_trend') → true"""
        return self._data.get('profit_take', {}).get('cc', {}).get(key, default)

    def profit_take_trend(self, key: str, default=None):
        """Trend-input threshold. e.g., profit_take_trend('strong_trend_composite_min') → 70"""
        return self._data.get('profit_take', {}).get('trend_inputs', {}).get(key, default)

    # ═══════════════════════════════════════════════════════════
    # CC MANAGEMENT
    # ═══════════════════════════════════════════════════════════

    @property
    def cc_close_profit_pct(self) -> float:
        return self._data['cc_management']['close_at_profit_pct']

    @property
    def cc_roll_dte(self) -> int:
        return self._data['cc_management']['roll_dte_threshold']

    @property
    def cc_pause_drop_pct(self) -> float:
        return self._data['cc_management']['pause_cc_if_drop_pct']

    # ═══════════════════════════════════════════════════════════
    # PUT CREDIT SPREAD (defined-risk income supplement)
    # ═══════════════════════════════════════════════════════════

    def _cs(self, key: str, default=None):
        """Raw credit_spread config value."""
        return self._data.get('credit_spread', {}).get(key, default)

    @property
    def credit_spread_enabled(self) -> bool:
        return bool(self._cs('enabled', True))

    @property
    def credit_spread_strategy_code(self) -> str:
        return str(self._cs('strategy_code', 'PS'))

    @property
    def credit_spread_widths(self) -> list:
        """Preferred strike widths to match greedily."""
        return list(self._cs('width', {}).get('allowed', [2.5, 5.0, 7.5, 10.0]))

    @property
    def credit_spread_width_min(self) -> float:
        return float(self._cs('width', {}).get('min', 1.0))

    @property
    def credit_spread_width_max(self) -> float:
        return float(self._cs('width', {}).get('max', 10.0))

    @property
    def credit_spread_credit_ratio_min(self) -> float:
        return float(self._cs('credit_ratio_min', 0.333))

    @property
    def credit_spread_roc_min(self) -> float:
        return float(self._cs('roc_min', 8.0))

    @property
    def credit_spread_long_leg_oi(self) -> int:
        return int(self._cs('long_leg', {}).get('open_interest_min', 100))

    @property
    def credit_spread_long_leg_volume(self) -> int:
        return int(self._cs('long_leg', {}).get('volume_min', 10))

    @property
    def credit_spread_max_per_ticker(self) -> int:
        return int(self._cs('max_per_ticker', 1))

    @property
    def credit_spread_cash_backed(self) -> bool:
        return bool(self._cs('cash_backed', True))

    # ═══════════════════════════════════════════════════════════
    # WATCHLIST
    # ═══════════════════════════════════════════════════════════

    @property
    def moomoo_watchlist_group(self) -> str:
        """Moomoo watchlist group name to pull live tickers from."""
        return self._data['watchlist'].get('moomoo_group', 'Options')

    @property
    def default_watchlist(self) -> list:
        return self._data['watchlist']['default']

    @property
    def diversify_tickers(self) -> dict:
        return self._data['watchlist']['diversify']

    # ═══════════════════════════════════════════════════════════
    # GUARDRAIL LIMITS
    # ═══════════════════════════════════════════════════════════

    def guardrail_limits(self, key: str, default=None):
        """Get guardrail operational limit. e.g., guardrail_limits('max_monthly_orders_emergency') → 15"""
        return self._data.get('guardrail_limits', {}).get(key, default)

    # ═══════════════════════════════════════════════════════════
    # CONVENIENCE
    # ═══════════════════════════════════════════════════════════

    def should_pause_csp(self, vix: float, regime_score: int,
                         cash_reserve_pct: float) -> tuple[bool, list[str]]:
        """Check CSP pause triggers. Returns (should_pause, reasons)."""
        reasons = []
        if vix > self.csp_pause_vix:
            reasons.append(f'VIX {vix:.1f} > {self.csp_pause_vix}')
        if regime_score <= self.csp_pause_regime_score:
            reasons.append(f'Regime score {regime_score} ≤ {self.csp_pause_regime_score}')
        if cash_reserve_pct < self.csp_pause_cash_reserve_pct:
            reasons.append(f'Cash reserve {cash_reserve_pct:.0%} < {self.csp_pause_cash_reserve_pct:.0%}')
        return len(reasons) > 0, reasons

    def get_regime_params(self, regime: str) -> dict:
        """Get all parameters for a given regime. Single call for convenience."""
        return {
            'regime': regime,
            'position_mult': self.position_mult(regime),
            'cash_reserve_pct': self.cash_reserve_pct(regime),
            'csp_cc_ratio': self.csp_cc_ratio(regime),
            'csp_delta': self.delta_range('csp', regime),
            'cc_delta': self.delta_range('cc', regime),
        }
