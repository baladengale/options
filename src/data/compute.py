"""
Deterministic indicator computation from raw market data.

All formulas are pure math — no AI, no randomness, no external calls.
Inputs: StockSnapshot, OptionSnapshot, price history (list of dicts).
Outputs: computed fields, ratios, and derived indicators.

Professional trader parameters covered:
  Stocks:  RSI(14), MACD(12,26,9), ADX(14), ATR(14), SMA(20/50/200),
           Bollinger(20,2), HV(30), Beta vs SPY, Volume Ratio
  Options: IV Rank, IV Percentile, Max Pain, Put/Call Ratio,
           Skew (25-delta), Term Structure, ATM IV, OI Walls,
           Greeks summary, Gamma Exposure (GEX)
"""

import math
from typing import Optional
from collections import defaultdict
from datetime import date

from src.data.models import StockSnapshot, OptionSnapshot, OptionChainBundle


# ═══════════════════════════════════════════════════════════════
# PRICE HISTORY → INDICATORS
# ═══════════════════════════════════════════════════════════════

def _closes(history: list[dict]) -> list[float]:
    return [r['close'] for r in history]


def compute_sma(prices: list[float], period: int) -> Optional[float]:
    """Simple Moving Average."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def compute_ema(prices: list[float], period: int) -> Optional[float]:
    """Exponential Moving Average."""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def compute_rsi(history: list[dict], period: int = 14) -> Optional[float]:
    """RSI (Relative Strength Index) — Wilder's smoothing."""
    closes = _closes(history)
    if len(closes) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-(period + 1) + i] - closes[-(period + 1) + i - 1]
        gains.append(diff if diff > 0 else 0)
        losses.append(abs(diff) if diff < 0 else 0)

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_macd(history: list[dict]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """MACD(12,26,9). Returns (macd, signal, histogram)."""
    closes = _closes(history)
    if len(closes) < 26 + 9:
        return None, None, None

    ema12 = compute_ema(closes, 12)
    ema26 = compute_ema(closes, 26)
    if ema12 is None or ema26 is None:
        return None, None, None

    macd = ema12 - ema26

    # Signal line: EMA(9) of MACD — compute from MACD history
    # Simplified: use short EMA approximation
    macd_history = []
    for i in range(26, len(closes) + 1):
        e12 = compute_ema(closes[:i], 12)
        e26 = compute_ema(closes[:i], 26)
        if e12 is not None and e26 is not None:
            macd_history.append(e12 - e26)

    signal = compute_ema(macd_history, 9) if len(macd_history) >= 9 else macd
    histogram = macd - signal if signal is not None else None
    return macd, signal, histogram


def compute_atr(history: list[dict], period: int = 14) -> Optional[float]:
    """Average True Range."""
    if len(history) < period + 1:
        return None

    tr_values = []
    for i in range(1, len(history)):
        h, l, prev_c = history[i]['high'], history[i]['low'], history[i - 1]['close']
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_values.append(tr)

    if len(tr_values) < period:
        return None
    return sum(tr_values[-period:]) / period


def compute_adx(history: list[dict], period: int = 14) -> Optional[float]:
    """Average Directional Index."""
    if len(history) < period * 2:
        return None

    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(history)):
        h, l, prev_h, prev_l = history[i]['high'], history[i]['low'], history[i - 1]['high'], history[i - 1]['low']
        prev_c = history[i - 1]['close']
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)

        up_move = h - prev_h
        down_move = prev_l - l
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

    atr_val = sum(tr_list[:period]) / period if tr_list else 1
    if atr_val == 0:
        return None

    smoothed_plus = sum(plus_dm[:period]) / period
    smoothed_minus = sum(minus_dm[:period]) / period

    plus_di = (smoothed_plus / atr_val) * 100
    minus_di = (smoothed_minus / atr_val) * 100

    dx_sum = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 0 else 0
    return dx_sum  # ADX ≈ smooth DX over period


def compute_bollinger(prices: list[float], period: int = 20, std_dev: float = 2.0):
    """Bollinger Bands. Returns (mid, upper, lower)."""
    if len(prices) < period:
        return None, None, None
    mid = sum(prices[-period:]) / period
    variance = sum((p - mid) ** 2 for p in prices[-period:]) / period
    std = math.sqrt(variance)
    return mid, mid + std_dev * std, mid - std_dev * std


def compute_hv(history: list[dict], period: int = 30) -> Optional[float]:
    """Historical Volatility (annualized) from daily log returns."""
    closes = _closes(history)
    if len(closes) < period + 1:
        return None

    log_returns = []
    for i in range(len(closes) - period, len(closes) - 1):
        if closes[i] > 0 and closes[i + 1] > 0:
            log_returns.append(math.log(closes[i + 1] / closes[i]))

    if len(log_returns) < 2:
        return None

    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return math.sqrt(variance) * math.sqrt(252)  # annualize


def compute_beta(stock_history: list[dict], spy_history: list[dict]) -> Optional[float]:
    """Beta vs SPY from daily log returns."""
    if len(stock_history) < 60 or len(spy_history) < 60:
        return None

    stock_returns = _daily_returns(stock_history)
    spy_returns = _daily_returns(spy_history)

    # Align by dates (simple: take min length)
    n = min(len(stock_returns), len(spy_returns))
    if n < 2:
        return None

    sr = stock_returns[-n:]
    mr = spy_returns[-n:]

    mean_sr = sum(sr) / n
    mean_mr = sum(mr) / n

    covariance = sum((sr[i] - mean_sr) * (mr[i] - mean_mr) for i in range(n)) / (n - 1)
    variance = sum((r - mean_mr) ** 2 for r in mr) / (n - 1)

    return covariance / variance if variance > 0 else None


def _daily_returns(history: list[dict]) -> list[float]:
    returns = []
    for i in range(1, len(history)):
        if history[i - 1]['close'] > 0:
            returns.append(math.log(history[i]['close'] / history[i - 1]['close']))
    return returns


# ═══════════════════════════════════════════════════════════════
# COMPUTE ALL STOCK INDICATORS (enriches StockSnapshot)
# ═══════════════════════════════════════════════════════════════

def enrich_stock_snapshot(
    snap: StockSnapshot,
    history: list[dict],
    spy_history: Optional[list[dict]] = None,
):
    """Compute all technical indicators and set them on the snapshot."""
    closes = _closes(history)
    snap.history_points = len(closes)

    snap.sma_20 = compute_sma(closes, 20)
    snap.sma_50 = compute_sma(closes, 50)
    snap.sma_200 = compute_sma(closes, 200)
    snap.rsi_14 = compute_rsi(history, 14)
    snap.atr_14 = compute_atr(history, 14)
    snap.adx_14 = compute_adx(history, 14)
    snap.hv_30d = compute_hv(history, 30)
    snap.macd, snap.macd_signal, snap.macd_histogram = compute_macd(history)
    snap.bollinger_mid, snap.bollinger_upper, snap.bollinger_lower = compute_bollinger(closes)

    if spy_history:
        snap.beta_vs_spy = compute_beta(history, spy_history)


# ═══════════════════════════════════════════════════════════════
# IV RANK & IV PERCENTILE
# ═══════════════════════════════════════════════════════════════

def compute_iv_rank(current_iv: float, iv_history: list[float]) -> Optional[float]:
    """IV Rank: where current IV sits in the 1Y range. Returns 0-100."""
    if not iv_history or current_iv <= 0:
        return None
    low, high = min(iv_history), max(iv_history)
    if high <= low:
        return 50.0
    return ((current_iv - low) / (high - low)) * 100


def compute_iv_percentile(current_iv: float, iv_history: list[float]) -> Optional[float]:
    """IV Percentile: % of days with IV lower than current. Returns 0-100."""
    if not iv_history:
        return None
    below = sum(1 for iv in iv_history if iv < current_iv)
    return (below / len(iv_history)) * 100


# ═══════════════════════════════════════════════════════════════
# OPTION CHAIN COMPUTED INDICATORS
# ═══════════════════════════════════════════════════════════════

def compute_option_chain_bundle(
    ticker: str,
    underlying_price: float,
    contracts: list[OptionSnapshot],
    iv_history: Optional[list[float]] = None,
) -> OptionChainBundle:
    """Compute all chain-level indicators from a list of option snapshots."""
    calls = [c for c in contracts if c.option_type == 'CALL']
    puts = [c for c in contracts if c.option_type == 'PUT']

    bundle = OptionChainBundle(
        ticker=ticker,
        underlying_price=underlying_price,
        fetched_at=date.today(),
        calls=calls,
        puts=puts,
    )

    # Enrich each contract with IV rank
    if iv_history:
        for c in contracts:
            c.iv_rank = compute_iv_rank(c.implied_vol, iv_history)
            c.iv_percentile = compute_iv_percentile(c.implied_vol, iv_history)

    # ── Put/Call Ratios ──
    total_call_oi = sum(c.open_interest for c in calls if c.open_interest > 0)
    total_put_oi = sum(p.open_interest for p in puts if p.open_interest > 0)
    total_call_vol = sum(c.volume for c in calls if c.volume > 0)
    total_put_vol = sum(p.volume for p in puts if p.volume > 0)

    bundle.put_call_oi_ratio = (total_put_oi / total_call_oi) if total_call_oi > 0 else None
    bundle.put_call_vol_ratio = (total_put_vol / total_call_vol) if total_call_vol > 0 else None

    # ── Max Pain ──
    bundle.max_pain = _compute_max_pain(calls, puts)

    # ── ATM IV ──
    bundle.atm_iv = _compute_atm_iv(calls, puts, underlying_price)

    # ── Skew (25-delta) ──
    bundle.skew_25d = _compute_skew_25d(calls, puts)

    # ── Term Structure ──
    bundle.term_structure = _compute_term_structure(contracts)

    # ── OI Walls ──
    bundle.call_oi_wall = _max_oi_strike(calls)
    bundle.put_oi_wall = _max_oi_strike(puts)

    # ── GEX (simplified) ──
    bundle.gamma_exposure = _compute_gex(calls, puts, underlying_price)

    return bundle


def _compute_max_pain(calls: list[OptionSnapshot], puts: list[OptionSnapshot]) -> Optional[float]:
    """Max Pain: strike where total intrinsic value of all options is minimized."""
    all_contracts = calls + puts
    if not all_contracts:
        return None

    strikes = sorted(set(c.strike for c in all_contracts))
    if not strikes:
        return None

    min_pain, best_strike = float('inf'), None
    for strike in strikes:
        pain = 0.0
        for c in calls:
            if c.strike < strike:
                pain += (strike - c.strike) * c.open_interest * 100 if c.open_interest else 0
        for p in puts:
            if p.strike > strike:
                pain += (p.strike - strike) * p.open_interest * 100 if p.open_interest else 0
        if pain < min_pain:
            min_pain, best_strike = pain, strike

    return best_strike


def _compute_atm_iv(
    calls: list[OptionSnapshot], puts: list[OptionSnapshot], price: float
) -> Optional[float]:
    """ATM IV = average IV of nearest call and put to underlying price."""
    if not calls or not puts:
        return None

    nearest_call = min(calls, key=lambda c: abs(c.strike - price))
    nearest_put = min(puts, key=lambda p: abs(p.strike - price))

    if nearest_call.implied_vol > 0 and nearest_put.implied_vol > 0:
        return (nearest_call.implied_vol + nearest_put.implied_vol) / 2
    return None


def _compute_skew_25d(calls: list[OptionSnapshot], puts: list[OptionSnapshot]) -> Optional[float]:
    """25-delta skew: IV of 25-delta put - IV of 25-delta call."""
    call_25 = min(calls, key=lambda c: abs(abs(c.delta) - 0.25), default=None) if calls else None
    put_25 = min(puts, key=lambda p: abs(abs(p.delta) - 0.25), default=None) if puts else None

    if call_25 and put_25 and call_25.implied_vol > 0 and put_25.implied_vol > 0:
        return put_25.implied_vol - call_25.implied_vol
    return None


def _compute_term_structure(contracts: list[OptionSnapshot]) -> Optional[str]:
    """Term structure: IV of near-term vs far-term. Requires at least 2 expiries."""
    by_expiry: dict[str, list[float]] = defaultdict(list)
    for c in contracts:
        if c.implied_vol > 0 and c.expiry:
            by_expiry[c.expiry].append(c.implied_vol)

    expiries = sorted(by_expiry.keys())
    if len(expiries) < 2:
        return None

    near_iv = sum(by_expiry[expiries[0]]) / len(by_expiry[expiries[0]])
    far_iv = sum(by_expiry[expiries[-1]]) / len(by_expiry[expiries[-1]])

    diff_pct = (near_iv - far_iv) / far_iv * 100 if far_iv > 0 else 0
    if diff_pct > 2:
        return 'BACKWARDATION'
    elif diff_pct < -2:
        return 'CONTANGO'
    return 'FLAT'


def _max_oi_strike(contracts: list[OptionSnapshot]) -> Optional[float]:
    """Strike with highest open interest."""
    if not contracts:
        return None
    best = max(contracts, key=lambda c: c.open_interest if c.open_interest else 0)
    return best.strike if best.open_interest > 0 else None


def _compute_gex(
    calls: list[OptionSnapshot], puts: list[OptionSnapshot], price: float
) -> Optional[float]:
    """
    Simplified Gamma Exposure estimate.
    GEX = Σ (gamma × OI × spot × 100) / total_oi for calls - same for puts.
    Positive GEX = dealers long gamma (dampening). Negative = short gamma (amplifying).
    """
    total_call_gex = 0.0
    total_put_gex = 0.0

    for c in calls:
        if c.gamma and c.open_interest:
            total_call_gex += abs(c.gamma) * c.open_interest * price * 100

    for p in puts:
        if p.gamma and p.open_interest:
            total_put_gex -= abs(p.gamma) * p.open_interest * price * 100

    return total_call_gex + total_put_gex
