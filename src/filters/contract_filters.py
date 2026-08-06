"""
Contract gate functions — shared filters used by screener, OIE engine, and holding scorer.

Every function is pure: data in → bool or (bool, reason). All thresholds from config/rules.yaml.
Single source of truth for all contract filtering. No duplication across scripts.

Usage:
    from src.filters.contract_filters import passes_all_gates

    ok, reason = passes_all_gates(contract, 'CSP', regime, snap, cfg)
    if not ok:
        continue  # reason tells you which gate failed
"""

from typing import Optional, Tuple

from src.config import get_config, Config


# ═══════════════════════════════════════════════════════════════
# RoC FORMULAS
# ═══════════════════════════════════════════════════════════════

def csp_roc(bid: float, strike: float, dte: int) -> float:
    """Cash-Secured Put annualized return on capital.
    Formula: (premium / strike) * (365 / DTE) * 100"""
    if strike <= 0 or dte <= 0:
        return 0.0
    return (bid / strike) * (365.0 / dte) * 100


def cc_roc(bid: float, price: float, dte: int) -> float:
    """Covered Call annualized return on capital.
    Formula: (premium / stock_price) * (365 / DTE) * 100"""
    if price <= 0 or dte <= 0:
        return 0.0
    return (bid / price) * (365.0 / dte) * 100


# ═══════════════════════════════════════════════════════════════
# GATE FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def passes_liquidity(contract, cfg: Optional[Config] = None) -> bool:
    """Bid > 0, OI >= cfg.oi_min, volume >= cfg.volume_min."""
    if cfg is None:
        cfg = get_config()
    if (contract.bid or 0) <= 0:
        return False
    if (contract.open_interest or 0) < cfg.oi_min:
        return False
    if (contract.volume or 0) < cfg.volume_min:
        return False
    return True


def passes_delta(contract, strategy: str, regime: str,
                 cfg: Optional[Config] = None) -> Tuple[bool, str]:
    """Delta within config range for strategy+regime.
    CSP also checks abs_d <= 0.70 (deep ITM = not premium selling).
    Returns (passed, reason)."""
    if cfg is None:
        cfg = get_config()

    delta_range = cfg.delta_range(strategy.lower(), regime)
    if strategy.upper() == 'CSP':
        abs_d = abs(contract.delta or 0)
        if abs_d < delta_range[0]:
            return False, f'Δ {abs_d:.3f} below min {delta_range[0]}'
        if abs_d > delta_range[1]:
            return False, f'Δ {abs_d:.3f} above max {delta_range[1]}'
        if abs_d > 0.70:
            return False, f'Δ {abs_d:.3f} deep ITM — not premium selling'
    else:  # CC
        d = contract.delta or 0
        if d < delta_range[0]:
            return False, f'Δ {d:.3f} below min {delta_range[0]}'
        if d > delta_range[1]:
            return False, f'Δ {d:.3f} above max {delta_range[1]}'

    return True, ''


def iv_sane(contract) -> bool:
    """IV exists and 0 < IV < 500%. Moomoo returns IV as % (e.g. 41.2 = 41.2%)."""
    return bool(contract.implied_vol and 0 < contract.implied_vol < 500)


def passes_vrp(contract, hv_30d: Optional[float]) -> bool:
    """Volatility Risk Premium gate: IV must be > HV(30d) * 0.8.
    We only sell premium when options are priced above actual volatility."""
    if not contract.implied_vol:
        return False
    if hv_30d and hv_30d > 0:
        return contract.implied_vol > hv_30d * 0.8
    return True  # no HV data → pass (can't check)


def passes_roc(roc: float, strategy: str, cfg: Optional[Config] = None) -> bool:
    """RoC meets minimum for strategy. CSP >= 12%, CC >= 8% (configurable)."""
    if cfg is None:
        cfg = get_config()
    if strategy.upper() == 'CSP':
        return roc >= cfg.roc_min_csp
    else:
        return roc >= cfg.roc_min_cc


def passes_concentration(capital: float, net_liq: float,
                         cfg: Optional[Config] = None) -> bool:
    """Single position <= max_single_position_pct of net liquidation."""
    if cfg is None:
        cfg = get_config()
    if net_liq <= 0:
        return False
    return capital <= net_liq * cfg.max_single_position_pct


def passes_cash_buffer(capital: float, cash: float, net_liq: float,
                       buying_power: float, cfg: Optional[Config] = None,
                       csp_headroom: float = 0.0) -> bool:
    """Cash buffer check: cash >= 10% of NLV, capital <= 80% of buying power.

    When csp_headroom > 0 (CSP-available headroom from worst-case formula),
    the simple cash/% check is replaced by: capital must fit within headroom.
    Headroom = liquid + (margin-BP × bp_buffer) + (CC notional × cc_buffer) − existing CSP.
    """
    if cfg is None:
        cfg = get_config()
    if csp_headroom > 0:
        # Comprehensive coverage model — new CSP capital fits within available headroom
        return capital <= csp_headroom
    if net_liq > 0:
        cash_pct = cash / net_liq
        if cash_pct < cfg.cash_buffer_critical:
            return False
    if capital > buying_power * 0.8:
        return False
    return True


# ═══════════════════════════════════════════════════════════════
# ORCHESTRATOR — one call for all gates
# ═══════════════════════════════════════════════════════════════

def passes_all_gates(contract, strategy: str, regime: str,
                     snap, cfg: Optional[Config] = None,
                     skip_concentration: bool = False,
                     skip_cash_buffer: bool = False,
                     net_liq: float = 0, cash: float = 0,
                     buying_power: float = 0,
                     csp_headroom: float = 0.0) -> Tuple[bool, str]:
    """Run all gates for a contract. Returns (passed, skip_reason).

    Args:
        contract: OptionSnapshot with bid, delta, iv, oi, volume, dte, strike
        strategy: 'CSP' or 'CC'
        regime: market regime label (BULLISH, NEUTRAL, etc.)
        snap: StockSnapshot with hv_30d, last_price
        cfg: Config instance (auto-loaded if None)
        skip_concentration: if True, skip position sizing check
        skip_cash_buffer: if True, skip cash buffer check
        net_liq, cash, buying_power: needed for concentration/cash checks

    Returns:
        (True, '') if all gates pass
        (False, 'reason') if any gate fails
    """
    if cfg is None:
        cfg = get_config()

    # 1. Liquidity gate
    if not passes_liquidity(contract, cfg):
        return False, 'liquidity'

    # 2. Delta gate
    ok, reason = passes_delta(contract, strategy, regime, cfg)
    if not ok:
        return False, reason

    # 3. IV sanity
    if not iv_sane(contract):
        return False, 'IV sanity'

    # 4. VRP gate
    hv = getattr(snap, 'hv_30d', None)
    if not passes_vrp(contract, hv):
        return False, 'VRP'

    # 5. RoC minimum
    if strategy.upper() == 'CSP':
        roc = csp_roc(contract.bid, contract.strike, contract.dte)
    else:
        roc = cc_roc(contract.bid, snap.last_price, contract.dte)
    if not passes_roc(roc, strategy, cfg):
        return False, f'RoC {roc:.1f}% below min'

    # 6. Concentration (optional — skipped by screener when --force)
    if not skip_concentration and net_liq > 0:
        capital = contract.strike * 100 if strategy.upper() == 'CSP' else snap.last_price * 100
        if not passes_concentration(capital, net_liq, cfg):
            return False, f'concentration (capital ${capital:,.0f} > {cfg.max_single_position_pct:.0%} of NLV)'

    # 7. Cash buffer (optional)
    if not skip_cash_buffer and cash > 0:
        capital = contract.strike * 100 if strategy.upper() == 'CSP' else snap.last_price * 100
        if not passes_cash_buffer(capital, cash, net_liq, buying_power, cfg,
                                  csp_headroom=csp_headroom if strategy.upper() == 'CSP' else 0.0):
            return False, 'cash buffer'

    return True, ''
