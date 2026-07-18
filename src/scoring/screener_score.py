"""
Screener scoring engine — the shared ticker + contract scoring used by both
scripts/screener.py and scripts/oie_engine.py.

Moved verbatim from scripts/screener.py so the two consumers share ONE source of
truth instead of oie_engine importing scoring out of the screener script.

Scoring: 1-10 per ticker (1 = best, lower is better).
Dimensions: Technical (25%) | Options Quality (25%) | Fundamental (15%) |
            External Sentiment (20%) | Macro/Risk (15%)   [weights from config]
Final contract score = ticker_score + _contract_penalty(...).

These functions are deterministic — no AI, no network. Config via src.config.
"""

from typing import Optional

from src.config import get_config
from src.data.models import StockSnapshot, OptionSnapshot


def _cfg_val(getter, default=None):
    """Config accessor — delegates to the cached get_config() singleton."""
    cfg = get_config()
    return getter(cfg) if default is None else getter(cfg)


# ═══════════════════════════════════════════════════════════════
# TICKER-LEVEL SCORE (1-10, lower = better)
# ═══════════════════════════════════════════════════════════════

def _compute_ticker_score(
    snap: StockSnapshot,
    trend_composite: float,
    analyst_consensus: str,
    earnings_blackout: bool,
    insider_sentiment: str,
    target_upside: Optional[float],
    news_score: float = 50.0,
    regime: str = 'NEUTRAL',
    regime_mult: float = 1.0,
    iv_rank: float = 50.0,
) -> float:
    """
    Ticker-level score (1-10). Lower = better.
    Every sub-score is 1 (best) to 10 (worst).
    Weighted: Technical 25% + Options Eco 25% + Fundamental 15% + External 20% + Macro 15%
    """
    scores = {}

    w = _cfg_val(lambda c: c.scoring_weights)

    # 1. TECHNICAL — trend quality for premium selling
    tech = _score_technical(snap, trend_composite)
    scores['tech'] = tech * w['technical']

    # 2. OPTIONS ECOSYSTEM — spread + IV rank
    opt_eco = _score_options_eco(snap, iv_rank)
    scores['opt_eco'] = opt_eco * w['options_quality']

    # 3. FUNDAMENTAL — valuation health
    fund = _score_fundamental(snap)
    scores['fund'] = fund * w['fundamental']

    # 4. EXTERNAL SENTIMENT — analyst + earnings + insider + news
    ext = _score_external(analyst_consensus, earnings_blackout, insider_sentiment, target_upside, news_score)
    scores['ext'] = ext * w['external_sentiment']

    # 5. MACRO/RISK — VIX regime + VRP adjustment
    macro = _score_macro(regime, regime_mult, earnings_blackout)
    scores['macro'] = macro * w['macro_risk']

    return round(sum(scores.values()), 2)


def _score_technical(snap: StockSnapshot, trend_comp: float) -> float:
    """Score trend quality. 1 = ideal for premium selling, 10 = avoid."""
    # RSI: 45-55 = ideal (1), extremes = bad (10)
    rsi = snap.rsi_14 or 50
    if 45 <= rsi <= 55:  rsi_score = 1.0
    elif 40 <= rsi <= 60: rsi_score = 3.0
    elif 35 <= rsi <= 65: rsi_score = 5.0
    elif 30 <= rsi <= 70: rsi_score = 7.0
    else:                  rsi_score = 9.0

    # Trend alignment: price > SMA50 > SMA200 = good
    trend_score = 3.0
    if snap.sma_50 and snap.sma_200:
        if snap.last_price > snap.sma_50 > snap.sma_200:
            trend_score = 1.0
        elif snap.last_price > snap.sma_200:
            trend_score = 3.0
        elif snap.last_price > snap.sma_50:
            trend_score = 5.0
        elif snap.last_price > snap.sma_200:
            trend_score = 7.0
        else:
            trend_score = 9.0

    # ADX: >25 trending (good for directional)
    adx = snap.adx_14 or 20
    if adx >= 40:     adx_score = 1.0
    elif adx >= 25:   adx_score = 3.0
    elif adx >= 20:   adx_score = 5.0
    else:             adx_score = 8.0

    # Volume: good volume = better execution
    vol_score = 3.0
    if snap.volume_ratio and snap.volume_ratio > 1.0:
        vol_score = 1.0
    elif snap.volume_ratio and snap.volume_ratio > 0.7:
        vol_score = 4.0
    else:
        vol_score = 7.0

    return (rsi_score * 0.35 + trend_score * 0.30 + adx_score * 0.20 + vol_score * 0.15)


def _score_options_eco(snap: StockSnapshot, iv_rank: float = 50.0) -> float:
    """Score options ecosystem quality. 1 = great, 10 = poor."""
    # Spread
    if snap.bid_ask_spread_pct < 0.5:    spread = 1.0
    elif snap.bid_ask_spread_pct < 1.0:  spread = 3.0
    elif snap.bid_ask_spread_pct < 3.0:  spread = 5.0
    elif snap.bid_ask_spread_pct < 5.0:  spread = 7.0
    else:                                 spread = 9.0

    # IV Rank: 30-70 = ideal for premium selling
    if 30 <= iv_rank <= 70:     iv_score = 1.0
    elif 20 <= iv_rank <= 80:   iv_score = 3.0
    elif iv_rank > 80:          iv_score = 5.0
    else:                       iv_score = 7.0

    # Market cap proxy: large cap = liquid options
    if snap.market_cap and snap.market_cap > 500e9: cap = 1.0
    elif snap.market_cap and snap.market_cap > 100e9: cap = 3.0
    elif snap.market_cap and snap.market_cap > 10e9: cap = 5.0
    else: cap = 8.0

    # Beta: too high beta = risky premium selling
    if snap.beta_vs_spy and snap.beta_vs_spy < 1.0: beta = 1.0
    elif snap.beta_vs_spy and snap.beta_vs_spy < 1.5: beta = 3.0
    elif snap.beta_vs_spy and snap.beta_vs_spy < 2.0: beta = 6.0
    else: beta = 9.0

    return spread * 0.25 + iv_score * 0.25 + cap * 0.25 + beta * 0.25


def _score_fundamental(snap: StockSnapshot) -> float:
    """Score fundamental health. 1 = great, 10 = poor."""
    # P/E: reasonable P/E = better
    pe = snap.pe_ttm or snap.pe_ratio or 25
    if pe and 10 <= pe <= 25:  pe_score = 1.0
    elif pe and 25 < pe <= 40: pe_score = 3.0
    elif pe and 40 < pe <= 60: pe_score = 5.0
    elif pe and pe > 60:       pe_score = 8.0
    else:                       pe_score = 5.0

    # Dividend: dividend stocks work well for wheel
    div = snap.dividend_yield_ttm or 0
    if div and div > 2.0:     div_score = 1.0
    elif div and div > 1.0:   div_score = 3.0
    elif div and div > 0:     div_score = 5.0
    else:                      div_score = 6.0

    # Earnings: consistent EPS
    if snap.eps_ttm and snap.eps_ttm > 0: eps_score = 1.0
    else:                                   eps_score = 7.0

    return pe_score * 0.40 + div_score * 0.30 + eps_score * 0.30


def _score_external(consensus: str, blackout: bool, insider: str,
                    upside: Optional[float], news_score: float = 50.0) -> float:
    """Score external sentiment. 1 = bullish, 10 = bearish. Includes news sentiment."""
    base = 4.0

    if consensus == 'STRONG_BUY':  base -= 1.5
    elif consensus == 'BUY':       base -= 0.8
    elif consensus == 'HOLD':      base += 0.5
    elif consensus == 'SELL':      base += 3.0
    elif consensus == 'STRONG_SELL': base += 5.0

    if upside and upside > 15:      base -= 1.0
    elif upside and upside > 5:     base -= 0.5
    elif upside and upside < -10:   base += 2.0

    if blackout:                    base += 2.0

    if insider == 'BUYING':         base -= 1.0
    elif insider == 'SELLING':      base += 1.5

    # News sentiment score (1-100 → penalty/bonus to 1-10 scale)
    if news_score >= 70:            base -= 1.0   # bullish news
    elif news_score <= 30:          base += 2.0   # bearish news
    elif news_score <= 40:          base += 1.0   # cautious news

    return max(1.0, min(10.0, base))


def _score_macro(regime: str, regime_mult: float, blackout: bool) -> float:
    """Score macro/risk context. 1 = favorable, 10 = unfavorable."""
    if regime == 'BULLISH':    base = 2.0
    elif regime == 'NEUTRAL':  base = 3.0
    elif regime == 'CAUTIOUS': base = 4.0
    elif regime == 'VOLATILE': base = 6.0
    elif regime == 'BEARISH':  base = 8.0
    else:                      base = 5.0

    if blackout:
        base += 2.0

    return max(1.0, min(10.0, base))


def _trend_composite(snap: StockSnapshot) -> float:
    """0-100 trend composite (simplified from existing scoring)."""
    trend = 50.0
    if snap.sma_50 and snap.sma_200:
        if snap.last_price > snap.sma_50 > snap.sma_200:
            trend = 75.0
        elif snap.last_price > snap.sma_200:
            trend = 60.0
        elif snap.last_price > snap.sma_50:
            trend = 40.0
        else:
            trend = 25.0
    rsi = snap.rsi_14 or 50
    rsi_factor = 0.5 if 40 <= rsi <= 60 else 0.3
    return trend * (0.7 + rsi_factor)


# ═══════════════════════════════════════════════════════════════
# CONTRACT PENALTY + HELPERS
# ═══════════════════════════════════════════════════════════════

def _contract_penalty(c: OptionSnapshot, delta: float, roc: float) -> float:
    """Per-contract score adjustment (added to ticker score). Lower = better.
    All thresholds from config/rules.yaml."""
    penalty = 0.0
    cp = lambda key: _cfg_val(lambda cfg: cfg.contract_penalty(key))

    # ═══ DTE WINDOW ═══
    if c.dte < _cfg_val(lambda cfg: cfg.dte_hard_block):
        penalty += cp('dte_hard_block')
    elif c.dte < 14:
        penalty += cp('dte_weekly_penalty')
    elif c.dte < _cfg_val(lambda cfg: cfg.dte_penalty_start):
        penalty += cp('dte_short_penalty')
    elif c.dte < _cfg_val(lambda cfg: cfg.dte_optimal_min):
        penalty += 0.5
    elif c.dte <= _cfg_val(lambda cfg: cfg.dte_optimal_max):
        penalty += cp('dte_optimal_bonus')
    elif c.dte <= 60:
        penalty += 0.0
    else:
        penalty += cp('dte_long_penalty')

    # Low OI
    if c.open_interest < 100:
        penalty += cp('low_oi_penalty')
    elif c.open_interest < _cfg_val(lambda cfg: cfg.oi_min):
        penalty += cp('medium_oi_penalty')

    # Wide spread
    if c.bid_ask_spread_pct > 5:
        penalty += cp('wide_spread_penalty')
    elif c.bid_ask_spread_pct > 2:
        penalty += cp('medium_spread_penalty')

    # Delta extreme
    if delta < 0.15:
        penalty += cp('low_delta_penalty')

    # Reward high RoC
    if roc > 24:
        penalty += cp('high_roc_bonus')
    elif roc > 18:
        penalty += cp('medium_roc_bonus')
    elif roc > 15:
        penalty -= 0.3

    # Reward high IV
    if c.implied_vol > 35:
        penalty += cp('high_iv_bonus')

    # Low volume
    if c.volume < 50:
        penalty += cp('low_volume_penalty')
    elif c.volume < _cfg_val(lambda cfg: cfg.volume_min):
        penalty += 2.0

    return penalty


def _compute_chain_gex(contracts: list, underlying_price: float) -> float:
    """Approximate Gamma Exposure from option contracts. Negative = dealer short gamma."""
    total = 0.0
    for c in contracts:
        if c.gamma and c.open_interest and underlying_price > 0:
            total += abs(c.gamma) * c.open_interest * underlying_price * 100
    return total


def _csp_roc(bid: float, strike: float, dte: int) -> float:
    if strike <= 0 or dte <= 0:
        return 0.0
    return (bid / strike) * (365.0 / dte) * 100


def _regime_multiplier(regime: str) -> float:
    return {'BULLISH': 0.85, 'NEUTRAL': 1.0, 'VOLATILE': 1.2, 'BEARISH': 1.5}.get(regime, 1.0)


def _reason(ticker_score: float, contract_score: float, strat: str) -> str:
    if contract_score <= 2.0:
        return f"{'🔥' if strat=='CSP' else '💎'} Excellent setup"
    elif contract_score <= 3.5:
        return f"Strong {strat} candidate"
    elif contract_score <= 5.0:
        return "Good, moderate risk"
    elif contract_score <= 7.0:
        return "Decent, higher risk"
    else:
        return "Marginal, caution"


def _score_stars(score: float) -> str:
    if score <= 2.0: return '⭐1'
    elif score <= 3.0: return '⭐2'
    elif score <= 4.0: return '⭐3'
    elif score <= 5.0: return ' 4 '
    elif score <= 6.0: return ' 5 '
    elif score <= 7.0: return ' 6 '
    elif score <= 8.0: return ' 7 '
    return ' 8+'
