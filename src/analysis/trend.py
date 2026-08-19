"""
Trend, momentum, and signal formulas per SPECS Section 5.

All computations are deterministic — SMA, ADX, RSI, MACD,
trend alignment, and composite trend score.
"""

from typing import Optional


# ============================================================
# SMA Calculation
# ============================================================

def compute_sma(prices: list[float], period: int) -> float:
    """Simple Moving Average over `period` data points."""
    if not prices:
        return 0.0
    window = prices[-period:] if len(prices) >= period else prices
    return sum(window) / len(window)


def compute_sma_from_history(price_history: list[dict], period: int) -> float:
    """Compute SMA from list of dicts with 'close' key."""
    closes = [row['close'] for row in price_history]
    return compute_sma(closes, period)


# ============================================================
# Trend Alignment (SPECS Section 5.1)
# ============================================================

def compute_trend_alignment(
    price: float,
    sma20: float,
    sma50: float,
    sma200: float,
    strategy: str,
) -> float:
    """
    Score SMA alignment for trend direction.

    For CSP: bullish alignment scores points, bearish scores 0.
    For CC: same scoring but floored at 30 (you already own shares).
    """
    if strategy not in ('COVERED_CALL', 'CASH_SECURED_PUT'):
        raise ValueError(f"Unknown strategy: {strategy}")

    bullish_pairs = 0
    total_pairs = 3

    # Pair 1: price vs SMA20
    if price > sma20:
        bullish_pairs += 1
    elif price < sma20:
        pass  # bearish

    # Pair 2: SMA20 vs SMA50
    if sma20 > sma50:
        bullish_pairs += 1
    elif sma20 < sma50:
        pass  # bearish

    # Pair 3: SMA50 vs SMA200
    if sma50 > sma200:
        bullish_pairs += 1
    elif sma50 < sma200:
        pass  # bearish

    score = (bullish_pairs / total_pairs) * 100

    # CC floor at 30
    if strategy == 'COVERED_CALL':
        score = max(score, 30.0)

    return round(score, 2)


# ============================================================
# ADX Trend Strength (SPECS Section 5.1)
# ============================================================

def compute_adx_strength_score(adx: float) -> float:
    """Score ADX into trend strength buckets."""
    if adx >= 40:
        return 100
    elif adx >= 25:
        return 75
    elif adx >= 20:
        return 50
    else:
        return 25


# ============================================================
# RSI Score (SPECS Section 5.2)
# ============================================================

def compute_rsi_score(rsi: float, strategy: str) -> float:
    """Score RSI(14) for the given strategy."""
    if strategy not in ('COVERED_CALL', 'CASH_SECURED_PUT'):
        raise ValueError(f"Unknown strategy: {strategy}")

    if strategy == 'CASH_SECURED_PUT':
        if 45 <= rsi <= 55:
            return 100
        elif 40 <= rsi < 45:
            return 85
        elif 55 < rsi <= 60:
            return 70
        elif 35 <= rsi < 40:
            return 60
        elif 60 < rsi <= 65:
            return 50
        elif 30 <= rsi < 35:
            return 35
        elif 65 < rsi <= 75:
            return 25
        elif rsi < 30:
            return 15
        else:  # rsi > 75
            return 10
    else:  # COVERED_CALL
        if 45 <= rsi <= 55:
            return 100
        elif 55 < rsi <= 65:
            return 85
        elif 40 <= rsi < 45:
            return 70
        elif 65 < rsi <= 75:
            return 60
        elif 35 <= rsi < 40:
            return 50
        elif rsi > 75:
            return 40
        elif rsi < 35:
            return 20
        else:
            return 50  # fallback


# ============================================================
# MACD Score (SPECS Section 5.2)
# ============================================================

def compute_macd_score(
    macd: float,
    signal: float,
    histogram: float,
    prev_histogram: Optional[float],
    strategy: str,
) -> float:
    """Score MACD configuration for the given strategy."""
    if strategy not in ('COVERED_CALL', 'CASH_SECURED_PUT'):
        raise ValueError(f"Unknown strategy: {strategy}")

    if macd > signal and histogram > 0:
        if prev_histogram is not None and histogram > prev_histogram:
            return 100  # bullish accelerating
        else:
            return 80   # bullish decelerating or unknown prev
    elif macd > signal and histogram <= 0:
        return 60       # just crossed bullish
    elif macd < signal and histogram < 0:
        if prev_histogram is not None and histogram >= prev_histogram:
            return 40   # bearish decelerating
        else:
            return 30   # bearish accelerating
    elif macd < signal and histogram >= 0:
        return 50       # just crossed bearish
    else:
        return 50


# ============================================================
# Composite Trend Score (SPECS Section 5.3)
# ============================================================

def compute_trend_composite(
    trend_alignment: float,
    adx_strength: float,
    momentum: float,
) -> float:
    """
    TREND_COMPOSITE = 0.5 × TREND_ALIGNMENT + 0.3 × ADX_STRENGTH + 0.2 × MOMENTUM
    """
    score = 0.5 * trend_alignment + 0.3 * adx_strength + 0.2 * momentum
    return max(0.0, min(100.0, score))


# ============================================================
# Canonical snapshot → composite (SPECS Section 5.3, wired 2026-08)
# ============================================================

def trend_composite_from_snapshot(snap, strategy: str = 'CASH_SECURED_PUT') -> Optional[float]:
    """TREND_COMPOSITE for one enriched StockSnapshot — the single definition
    shared by the entry layer and the trend-modulated exit layer.

        TREND_COMPOSITE = 0.5×ALIGNMENT + 0.3×ADX_STRENGTH + 0.2×MOMENTUM
        MOMENTUM        = 0.5×RSI_SCORE + 0.5×MACD_SCORE      (spec §5.2 → §5.3)

    Returns None when the alignment anchors are not trustworthy — any of
    sma_20/sma_50/sma_200 missing, or fewer than 200 sessions of history
    (a "SMA200" averaged over a short window is a mislabeled short average).
    None means "no signal": profit-management gates must NOT extend on it
    (they treat None as non-confirmation). Missing RSI/MACD degrade to
    neutral momentum (50) — never to a bullish signal.
    """
    if getattr(snap, 'history_points', 0) < 200:
        return None
    if not (snap.sma_20 and snap.sma_50 and snap.sma_200):
        return None
    if not snap.last_price or snap.last_price <= 0:
        return None

    alignment = compute_trend_alignment(
        snap.last_price, snap.sma_20, snap.sma_50, snap.sma_200, strategy)

    # ADX missing → neutral strength (50), not "strong trend"
    adx_strength = compute_adx_strength_score(snap.adx_14 or 20.0)

    # Momentum: blend RSI + MACD scores; each missing half degrades to 50.
    if snap.rsi_14 is not None:
        rsi_score = compute_rsi_score(snap.rsi_14, strategy)
    else:
        rsi_score = 50.0
    if snap.macd is not None and snap.macd_signal is not None and snap.macd_histogram is not None:
        macd_score = compute_macd_score(
            snap.macd, snap.macd_signal, snap.macd_histogram, None, strategy)
    else:
        macd_score = 50.0
    momentum = 0.5 * rsi_score + 0.5 * macd_score

    return compute_trend_composite(alignment, adx_strength, momentum)
