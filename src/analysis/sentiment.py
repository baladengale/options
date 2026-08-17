"""
Market Sentiment — reusable functions for macro context, analyst ratings,
earnings data, news sentiment, and ticker-level external data.

Used by: screener.py, portfolio_check.py, daily_run.py, market_sentiment.py

All functions are deterministic and cache-free. Callers handle rate limiting.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from src.data.yfinance_client import YFinanceClient, MacroData, AnalystRatings, EarningsData


@dataclass
class TickerSentiment:
    """Aggregated external sentiment for one ticker."""
    ticker: str
    analyst_consensus: str = 'N/A'
    analyst_upside_pct: Optional[float] = None
    num_analysts: int = 0
    next_earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None
    in_earnings_blackout: bool = False
    earnings_growth_pct: Optional[float] = None
    last_surprise_pct: Optional[float] = None
    news_score: Optional[int] = None
    news_direction: str = 'NEUTRAL'
    news_headlines: int = 0
    fetched_at: str = ''


@dataclass
class WatchlistSentiment:
    """Sentiment for an entire watchlist + macro context."""
    macro: Optional[MacroData] = None
    tickers: dict[str, TickerSentiment] = field(default_factory=dict)
    fetched_at: str = ''


def get_macro_context(client: YFinanceClient) -> MacroData:
    """Fetch full macro context: VIX, yields, regime, Fear & Greed."""
    return client.get_macro_data()


def get_ticker_sentiment(client: YFinanceClient, ticker: str) -> TickerSentiment:
    """
    Fetch all external sentiment data for one ticker.
    ticker can be 'AAPL' (yfinance format) or 'US.AAPL'.
    """
    yf_ticker = ticker.replace('US.', '')
    result = TickerSentiment(ticker=ticker, fetched_at=datetime.now().isoformat())

    # Analyst ratings
    try:
        ratings = client.get_analyst_ratings(yf_ticker)
        if ratings:
            result.analyst_consensus = ratings.consensus
            result.analyst_upside_pct = ratings.target_upside_pct
            result.num_analysts = ratings.num_analysts
    except Exception:
        pass

    # Earnings calendar
    try:
        earnings = client.get_earnings(yf_ticker)
        if earnings:
            result.next_earnings_date = earnings.next_earnings_date
            result.days_to_earnings = earnings.days_to_earnings
            result.in_earnings_blackout = earnings.in_blackout
            result.earnings_growth_pct = earnings.earnings_growth_pct
            result.last_surprise_pct = earnings.last_eps_surprise_pct
    except Exception:
        pass

    # News sentiment
    try:
        news = client.get_news_sentiment_score(yf_ticker)
        if news:
            result.news_score = news.get('score')
            result.news_direction = news.get('direction', 'NEUTRAL')
            result.news_headlines = news.get('headlines_scored', 0)
    except Exception:
        pass

    return result


def get_watchlist_sentiment(
    client: YFinanceClient, tickers: list[str], skip_macro: bool = False
) -> WatchlistSentiment:
    """
    Fetch macro context + sentiment for all tickers.
    Returns WatchlistSentiment with macro + per-ticker data.
    """
    result = WatchlistSentiment(fetched_at=datetime.now().isoformat())

    if not skip_macro:
        try:
            result.macro = client.get_macro_data()
        except Exception:
            pass

    for ticker in tickers:
        try:
            result.tickers[ticker] = get_ticker_sentiment(client, ticker)
        except Exception:
            pass

    return result


# ═══════════════════════════════════════════════════════════════
# Scoring helpers — used by screener.py
# ═══════════════════════════════════════════════════════════════

def score_analyst_consensus(consensus: str) -> float:
    """Convert analyst consensus to 0-1 score (1 = most bullish)."""
    mapping = {
        'STRONG_BUY': 1.0,
        'BUY': 0.75,
        'HOLD': 0.5,
        'SELL': 0.25,
        'STRONG_SELL': 0.0,
        'N/A': 0.5,
    }
    return mapping.get(consensus, 0.5)


def score_news_sentiment(score: Optional[int]) -> float:
    """Convert news sentiment score (1-100) to 0-1 scale."""
    if score is None:
        return 0.5
    return score / 100.0


def score_earnings_blackout(days_to_earnings: Optional[int]) -> tuple[bool, float]:
    """
    Check earnings blackout. Returns (in_blackout, penalty).
    Penalty: 0 = no penalty, 0.5 = approaching, 1.0 = in blackout.
    Blackout window from config (options.earnings.blackout_days); the
    approaching window extends 7 days past it.
    """
    from src.config import get_config
    blackout_days = int(get_config().earnings_blackout_days)
    if days_to_earnings is None:
        return False, 0.0
    if 0 <= days_to_earnings <= blackout_days:
        return True, 1.0
    if blackout_days + 1 <= days_to_earnings <= blackout_days + 7:
        return False, 0.3
    return False, 0.0
