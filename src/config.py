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
    def stop_delta_cc_critical(self) -> float:
        return self._data['stop_loss']['delta']['cc_critical']

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
