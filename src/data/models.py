"""
Deterministic data models for stock and option market data.

All fields are direct from moomoo snapshot unless noted as *computed*.
No AI, no randomness, no external calls in these models.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# STOCK SNAPSHOT
# ═══════════════════════════════════════════════════════════════

@dataclass
class StockSnapshot:
    """All deterministic fields for one stock at a point in time."""
    ticker: str
    name: str

    # Price
    last_price: float = 0.0
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    prev_close: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_vol: float = 0.0
    ask_vol: float = 0.0

    # Volume & liquidity
    volume: int = 0
    turnover: float = 0.0
    turnover_rate: float = 0.0
    volume_ratio: float = 0.0          # today_volume / avg_volume
    bid_ask_spread_pct: float = 0.0    # *computed*: (ask-bid)/mid × 100

    # Range
    amplitude: float = 0.0             # daily range %
    highest_52w: float = 0.0
    lowest_52w: float = 0.0

    # Fundamentals (from snapshot)
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    pe_ttm: Optional[float] = None
    earnings_yield: Optional[float] = None
    market_cap: Optional[float] = None
    circulating_market_cap: Optional[float] = None
    eps_ttm: Optional[float] = None
    net_profit: Optional[float] = None
    net_asset_per_share: Optional[float] = None
    dividend_ttm: Optional[float] = None
    dividend_yield_ttm: Optional[float] = None
    dividend_lfy: Optional[float] = None
    issued_shares: Optional[float] = None

    # Short selling
    short_sell_rate: Optional[float] = None
    short_available: Optional[float] = None

    # Status
    suspension: bool = False
    lot_size: int = 100
    update_time: Optional[datetime] = None

    # *Computed* — filled later by compute module
    change_pct: float = 0.0            # (last - prev_close) / prev_close × 100
    rsi_14: Optional[float] = None
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    adx_14: Optional[float] = None
    atr_14: Optional[float] = None
    hv_30d: Optional[float] = None     # historical volatility, 30d annualized
    beta_vs_spy: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    bollinger_upper: Optional[float] = None
    bollinger_lower: Optional[float] = None
    bollinger_mid: Optional[float] = None


# ═══════════════════════════════════════════════════════════════
# OPTION CONTRACT
# ═══════════════════════════════════════════════════════════════

@dataclass
class OptionSnapshot:
    """All deterministic fields for one option contract at a point in time."""
    code: str
    name: str
    underlying: str

    # Identity
    option_type: str                    # 'CALL' or 'PUT'
    strike: float
    expiry: str                         # ISO date
    dte: int                            # days to expiration
    area_type: str                      # 'AMERICAN' or 'EUROPEAN'

    # Price & liquidity
    last_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_vol: float = 0.0
    ask_vol: float = 0.0
    volume: int = 0
    bid_ask_spread_pct: float = 0.0    # *computed*
    prev_close: float = 0.0

    # Greeks
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0

    # Volatility
    implied_vol: float = 0.0           # IV as decimal (0.20 = 20%)
    iv_rank: Optional[float] = None     # *computed*: IV percentile vs 1Y range
    iv_percentile: Optional[float] = None  # *computed*: % of days IV was lower

    # Open interest
    open_interest: int = 0
    net_open_interest: Optional[int] = None

    # Contract details
    contract_size: float = 100.0
    contract_multiplier: float = 100.0
    premium: float = 0.0               # option premium
    contract_nominal_value: Optional[float] = None

    # Status
    suspension: bool = False
    lot_size: int = 100
    update_time: Optional[datetime] = None


# ═══════════════════════════════════════════════════════════════
# OPTION CHAIN BUNDLE (per ticker, per expiry or DTE range)
# ═══════════════════════════════════════════════════════════════

@dataclass
class OptionChainBundle:
    """Aggregated option chain data + *computed* market indicators."""
    ticker: str
    underlying_price: float
    fetched_at: datetime

    # All contracts in the chain
    calls: list[OptionSnapshot] = field(default_factory=list)
    puts: list[OptionSnapshot] = field(default_factory=list)

    # *Computed* chain-wide indicators
    put_call_oi_ratio: Optional[float] = None       # total put OI / total call OI
    put_call_vol_ratio: Optional[float] = None      # total put vol / total call vol
    max_pain: Optional[float] = None                # strike where total value minimized
    skew_25d: Optional[float] = None                # 25-delta put IV - 25-delta call IV
    term_structure: Optional[str] = None            # 'CONTANGO' | 'BACKWARDATION' | 'FLAT'
    atm_iv: Optional[float] = None                  # IV of nearest ATM straddle
    call_oi_wall: Optional[float] = None            # strike with highest call OI
    put_oi_wall: Optional[float] = None             # strike with highest put OI
    gamma_exposure: Optional[float] = None          # *computed* dealer GEX estimate


# ═══════════════════════════════════════════════════════════════
# MARKET REGIME (computed from macro data)
# ═══════════════════════════════════════════════════════════════

@dataclass
class MarketRegime:
    """Macro market context from VIX + SPY."""
    vix: float = 0.0
    spy_price: float = 0.0
    spy_sma_50: Optional[float] = None
    spy_sma_200: Optional[float] = None
    regime: str = 'UNKNOWN'  # BULLISH | NEUTRAL | VOLATILE | BEARISH


# ═══════════════════════════════════════════════════════════════
# TRADE CANDIDATE (screened option recommendation)
# ═══════════════════════════════════════════════════════════════

@dataclass
class TradeCandidate:
    """A screened option trade candidate — shared by screener and OIE engine."""
    ticker: str
    strategy: str              # CSP or CC
    score: float               # 1-10, lower = better
    strike: float
    expiry: str
    dte: int
    delta: float
    bid: float
    ask: float = 0.0
    premium: float = 0.0       # premium per contract
    annualized_roc_pct: float = 0.0
    iv: float = 0.0
    iv_rank: float = 50.0
    open_interest: int = 0
    capital_required: float = 0.0
    reason: str = ''
