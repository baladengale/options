"""
Yahoo Finance data client — analyst ratings, earnings, institutional data, news, macro.

Runtime-only fetch (no DB caching). All data is dynamic — fetched fresh each run.
Free, no API key required. Rate limit: be respectful, ~1 request/sec.

Usage:
    from src.data.yfinance_client import YFinanceClient
    client = YFinanceClient()
    ratings = client.get_analyst_ratings('AAPL')
    earnings = client.get_earnings_calendar('AAPL')
    macro = client.get_macro_data()
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
import time
import math

import os
import sys
import logging
import yfinance as yf

# Reusable stderr suppressor for yfinance HTTP 404 noise
_devnull = open(os.devnull, 'w')


def _shut_up():
    sys.stderr = _devnull


def _speak_up():
    sys.stderr = sys.__stderr__


# ═══════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════

@dataclass
class AnalystRatings:
    ticker: str
    strong_buy: int = 0
    buy: int = 0
    hold: int = 0
    sell: int = 0
    strong_sell: int = 0
    mean_target: Optional[float] = None
    low_target: Optional[float] = None
    high_target: Optional[float] = None
    median_target: Optional[float] = None
    current_price: Optional[float] = None
    target_upside_pct: Optional[float] = None    # (mean_target - price) / price * 100
    consensus: str = 'N/A'                        # STRONG_BUY | BUY | HOLD | SELL | STRONG_SELL
    num_analysts: int = 0
    last_rating_change: Optional[str] = None      # firm + action
    fetched_at: str = ''


@dataclass
class EarningsData:
    ticker: str
    next_earnings_date: Optional[str] = None      # next earnings date
    earnings_avg_estimate: Optional[float] = None  # EPS estimate
    earnings_low_estimate: Optional[float] = None
    earnings_high_estimate: Optional[float] = None
    year_ago_eps: Optional[float] = None
    earnings_growth_pct: Optional[float] = None    # YoY EPS growth estimate
    revenue_avg_estimate: Optional[float] = None
    revenue_growth_pct: Optional[float] = None
    last_eps_surprise: Optional[float] = None      # actual - estimate
    last_eps_surprise_pct: Optional[float] = None
    last_reported_eps: Optional[float] = None
    last_reported_date: Optional[str] = None
    days_to_earnings: Optional[int] = None         # days until next earnings
    in_blackout: bool = False                      # within 14-day window
    fetched_at: str = ''


@dataclass
class InstitutionData:
    ticker: str
    institutional_ownership_pct: Optional[float] = None
    insider_ownership_pct: Optional[float] = None
    top_institutions: list[dict] = field(default_factory=list)  # [{name, shares, pct}]
    recent_insider_trades: list[dict] = field(default_factory=list)  # [{date, name, shares, type}]
    net_insider_sentiment: str = 'NEUTRAL'  # BUYING | NEUTRAL | SELLING
    fetched_at: str = ''


@dataclass
class NewsItem:
    title: str
    publisher: str
    publish_time: str
    news_type: str = ''                # EARNINGS | DIVIDEND | M&A | REGULATORY | GENERAL
    sentiment_hint: str = 'NEUTRAL'    # POSITIVE | NEUTRAL | NEGATIVE (keyword-based)


@dataclass
class MacroData:
    vix: Optional[float] = None
    vvix: Optional[float] = None                     # vol-of-vol (VIX of VIX)
    fed_funds_rate: Optional[float] = None
    treasury_2y: Optional[float] = None
    treasury_10y: Optional[float] = None
    treasury_30y: Optional[float] = None
    yield_spread_10y2y: Optional[float] = None       # 10Y - 2Y (negative = inversion)
    yield_spread_10y3m: Optional[float] = None       # 10Y - 3M
    dxy: Optional[float] = None                       # US Dollar Index
    hyg_ief_spread: Optional[float] = None            # HYG/IEF credit spread proxy
    market_breadth_pct: Optional[float] = None        # % SPX stocks above 200 SMA
    regime_score: int = 0                             # -5 to +5 (voting tally)
    position_mult: float = 1.0                        # 0.0, 0.5, 1.0 (position sizing)
    market_regime: str = 'UNKNOWN'                    # BULLISH | NEUTRAL | VOLATILE | BEARISH
    vix_regime: str = 'UNKNOWN'                       # LOW | NORMAL | ELEVATED | HIGH | STRESS
    credit_regime: str = 'UNKNOWN'                    # HEALTHY | CONCERNING | STRESSED
    sizing_gate_note: str = ''                        # non-empty when a hard gate capped sizing
    fetched_at: str = ''


def apply_credit_stress_cap(macro: MacroData, cap) -> None:
    """Cap macro.position_mult when credit_regime == STRESSED (pure, in-place).

    The multi-condition vote tally counts stressed credit as a single -1 vote,
    so a calm-VIX/stressed-credit mix still sizes at full regime multiples.
    This hard gate (rules.yaml regime.credit_stress_position_mult_cap) clamps
    sizing whenever credit is STRESSED, recording why in sizing_gate_note.
    ``cap`` of None disables the gate.
    """
    if cap is None or macro.credit_regime != 'STRESSED':
        return
    if macro.position_mult > cap:
        macro.sizing_gate_note = (f"credit STRESSED — size capped "
                                  f"{macro.position_mult:.0%} → {cap:.0%} "
                                  f"(regime.credit_stress_position_mult_cap)")
        macro.position_mult = cap


# ═══════════════════════════════════════════════════════════════
# CLIENT
# ═══════════════════════════════════════════════════════════════

class YFinanceClient:
    """Runtime-only data fetch from Yahoo Finance. No caching, no DB."""

    def __init__(self, rate_limit_sec: float = 0.5):
        self._last_call = 0.0
        self._rate_limit = rate_limit_sec

    def _throttle(self):
        now = time.time()
        gap = now - self._last_call
        if gap < self._rate_limit:
            time.sleep(self._rate_limit - gap)
        self._last_call = time.time()

    # ═══════════════════════════════════════════════════════════
    # ANALYST RATINGS
    # ═══════════════════════════════════════════════════════════

    def get_analyst_ratings(self, ticker: str) -> Optional[AnalystRatings]:
        """Fetch analyst recommendations + price targets."""
        self._throttle()
        try:
            _shut_up()
            t = yf.Ticker(ticker)
            info = t.info or {}
            # Recommendations summary
            strong_buy = info.get('numberOfAnalystOpinions', 0) or 0
            # Try recommendation breakdown
            rec_key = info.get('recommendationKey', '').upper()
            rec_mean = info.get('recommendationMean', 0) or 0

            # Price targets
            mean_target = info.get('targetMeanPrice')
            low_target = info.get('targetLowPrice')
            high_target = info.get('targetHighPrice')
            median_target = info.get('targetMedianPrice')
            current_price = info.get('currentPrice') or info.get('regularMarketPrice')

            upside = None
            if mean_target and current_price and current_price > 0:
                upside = (mean_target - current_price) / current_price * 100

            # Consensus from recommendationMean: 1=Strong Buy, 2=Buy, 3=Hold, 4=Sell, 5=Strong Sell
            consensus = 'N/A'
            if rec_mean:
                if rec_mean <= 1.5:
                    consensus = 'STRONG_BUY'
                elif rec_mean <= 2.5:
                    consensus = 'BUY'
                elif rec_mean <= 3.5:
                    consensus = 'HOLD'
                elif rec_mean <= 4.5:
                    consensus = 'SELL'
                else:
                    consensus = 'STRONG_SELL'

            # Try to get actual breakdown
            try:
                recs = t.recommendations
                if recs is not None and len(recs) > 0:
                    latest = recs.iloc[-1]
                    strong_buy = int(latest.get('strongBuy', 0) or 0)
                    buy = int(latest.get('buy', 0) or 0)
                    hold = int(latest.get('hold', 0) or 0)
                    sell = int(latest.get('sell', 0) or 0)
                    strong_sell = int(latest.get('strongSell', 0) or 0)
                else:
                    buy = info.get('numberOfAnalystOpinions', 0) or 0
                    hold = sell = strong_sell = 0
            except Exception:
                buy = info.get('numberOfAnalystOpinions', 0) or 0
                hold = sell = strong_sell = 0

            # Last rating change
            last_change = None
            try:
                ud = t.upgrades_downgrades
                if ud is not None and len(ud) > 0:
                    latest = ud.iloc[-1]
                    firm = latest.get('firm', 'Unknown')
                    action = latest.get('action', latest.get('toGrade', ''))
                    last_change = f"{firm}: {action}"
            except Exception:
                pass

            return AnalystRatings(
                ticker=ticker,
                strong_buy=strong_buy,
                buy=buy,
                hold=hold,
                sell=sell,
                strong_sell=strong_sell,
                mean_target=mean_target,
                low_target=low_target,
                high_target=high_target,
                median_target=median_target,
                current_price=current_price,
                target_upside_pct=upside,
                consensus=consensus,
                num_analysts=strong_buy + buy + hold + sell + strong_sell,
                last_rating_change=last_change,
                fetched_at=datetime.now().isoformat(),
            )
        except Exception:
            _speak_up()
            return AnalystRatings(ticker=ticker, fetched_at=datetime.now().isoformat())

    # ═══════════════════════════════════════════════════════════
    # EARNINGS
    # ═══════════════════════════════════════════════════════════

    def get_earnings(self, ticker: str) -> Optional[EarningsData]:
        """Fetch earnings calendar, estimates, and surprise history."""
        self._throttle()
        try:
            _shut_up()
            t = yf.Ticker(ticker)
            info = t.info or {}
            cal = t.calendar or {}
            cal = cal if isinstance(cal, dict) else {}

            # Next earnings date
            next_date = None
            if 'Earnings Date' in cal:
                ed = cal['Earnings Date']
                if isinstance(ed, list) and len(ed) > 0:
                    next_date = str(ed[0])[:10]
            elif 'earningsDate' in info:
                ed = info['earningsDate']
                if isinstance(ed, list) and len(ed) > 0:
                    next_date = str(ed[0])[:10]

            # Earnings estimates
            try:
                est = t.earnings_estimate
                avg_est = float(est.iloc[0]['avg']) if est is not None and len(est) > 0 else None
                low_est = float(est.iloc[0]['low']) if est is not None and len(est) > 0 else None
                high_est = float(est.iloc[0]['high']) if est is not None and len(est) > 0 else None
                year_ago = float(est.iloc[0]['yearAgoEps']) if est is not None and len(est) > 0 else None
                growth = float(est.iloc[0]['growth']) * 100 if est is not None and len(est) > 0 and est.iloc[0].get('growth') else None
            except Exception:
                avg_est = info.get('forwardEps')
                low_est = high_est = year_ago = growth = None

            # Revenue estimates
            try:
                rev_est = t.revenue_estimate
                rev_avg = float(rev_est.iloc[0]['avg']) if rev_est is not None and len(rev_est) > 0 else None
                rev_growth = float(rev_est.iloc[0]['growth']) * 100 if rev_est is not None and len(rev_est) > 0 and rev_est.iloc[0].get('growth') else None
            except Exception:
                rev_avg = rev_growth = None

            # Last earnings surprise
            last_surprise = last_surprise_pct = last_eps = last_date = None
            try:
                edata = t.earnings_history
                if edata is not None and len(edata) > 0:
                    latest = edata.iloc[-1]
                    est_eps = float(latest.get('epsEstimate', 0) or 0)
                    act_eps = float(latest.get('epsActual', 0) or 0)
                    last_surprise = act_eps - est_eps
                    last_surprise_pct = (last_surprise / abs(est_eps) * 100) if est_eps != 0 else None
                    last_eps = act_eps
                    last_date = str(latest.get('epsDate', ''))[:10] if latest.get('epsDate') else None
            except Exception:
                last_eps = info.get('trailingEps')

            # Days to earnings + blackout
            days_to = None
            in_blackout = False
            if next_date:
                try:
                    nd = date.fromisoformat(next_date)
                    days_to = (nd - date.today()).days
                    in_blackout = 0 <= days_to <= 14
                except Exception:
                    pass

            _speak_up()
            return EarningsData(
                ticker=ticker,
                next_earnings_date=next_date,
                earnings_avg_estimate=avg_est,
                earnings_low_estimate=low_est,
                earnings_high_estimate=high_est,
                year_ago_eps=year_ago,
                earnings_growth_pct=growth,
                revenue_avg_estimate=rev_avg,
                revenue_growth_pct=rev_growth,
                last_eps_surprise=last_surprise,
                last_eps_surprise_pct=last_surprise_pct,
                last_reported_eps=last_eps,
                last_reported_date=last_date,
                days_to_earnings=days_to,
                in_blackout=in_blackout,
                fetched_at=datetime.now().isoformat(),
            )
        except Exception as e:
            _speak_up()
            return EarningsData(ticker=ticker, fetched_at=datetime.now().isoformat())

    # ═══════════════════════════════════════════════════════════
    # INSTITUTIONAL & INSIDER
    # ═══════════════════════════════════════════════════════════

    def get_institution_data(self, ticker: str) -> Optional[InstitutionData]:
        """Fetch institutional holders + insider transactions."""
        self._throttle()
        try:
            _shut_up()
            t = yf.Ticker(ticker)
            info = t.info or {}

            inst_pct = info.get('heldPercentInstitutions')
            if inst_pct:
                inst_pct = inst_pct * 100
            insider_pct = info.get('heldPercentInsiders')
            if insider_pct:
                insider_pct = insider_pct * 100

            # Top institutions
            top_inst = []
            try:
                holders = t.institutional_holders
                if holders is not None and len(holders) > 0:
                    for _, row in holders.head(5).iterrows():
                        top_inst.append({
                            'name': str(row.get('Holder', row.get('holder', ''))),
                            'shares': int(row.get('Shares', row.get('shares', 0)) or 0),
                            'pct': float(row.get('pctHeld', row.get('% Out', 0)) or 0),
                        })
            except Exception:
                pass

            # Insider trades
            insider_trades = []
            net_sentiment = 'NEUTRAL'
            buy_count = sell_count = 0
            try:
                txns = t.insider_transactions
                if txns is not None and len(txns) > 0:
                    for _, row in txns.head(10).iterrows():
                        shares = int(row.get('Shares', row.get('shares', 0)) or 0)
                        txn_type = str(row.get('Transaction', row.get('transaction', row.get('Start Date', ''))))
                        name = str(row.get('Insider', row.get('insider', '')))
                        if 'Purchase' in txn_type or 'Buy' in txn_type:
                            buy_count += 1
                        elif 'Sale' in txn_type or 'Sell' in txn_type:
                            sell_count += 1
                        insider_trades.append({
                            'name': name,
                            'shares': shares,
                            'type': txn_type,
                        })
            except Exception:
                pass

            if buy_count > sell_count:
                net_sentiment = 'BUYING'
            elif sell_count > buy_count:
                net_sentiment = 'SELLING'

            _speak_up()
            return InstitutionData(
                ticker=ticker,
                institutional_ownership_pct=inst_pct,
                insider_ownership_pct=insider_pct,
                top_institutions=top_inst,
                recent_insider_trades=insider_trades,
                net_insider_sentiment=net_sentiment,
                fetched_at=datetime.now().isoformat(),
            )
        except Exception as e:
            _speak_up()
            return InstitutionData(ticker=ticker, fetched_at=datetime.now().isoformat())

    # ═══════════════════════════════════════════════════════════
    # NEWS
    # ═══════════════════════════════════════════════════════════

    def get_news(self, ticker: str, max_items: int = 10) -> list[NewsItem]:
        """Fetch recent news with basic keyword sentiment tagging."""
        self._throttle()
        items = []
        try:
            _shut_up()
            t = yf.Ticker(ticker)
            news = t.news
            if not news:
                _speak_up()
                return items

            for n in news[:max_items]:
                content = n.get('content', {})
                title = content.get('title', '')
                publisher = content.get('provider', {}).get('displayName', content.get('pubDate', ''))
                pub_time = content.get('pubDate', '')

                # Simple keyword-based sentiment and type classification
                news_type = _classify_news_type(title)
                sentiment = _classify_news_sentiment(title)

                items.append(NewsItem(
                    title=title,
                    publisher=str(publisher),
                    publish_time=str(pub_time),
                    news_type=news_type,
                    sentiment_hint=sentiment,
                ))
        except Exception:
            pass
        _speak_up()
        return items

    def get_news_sentiment_score(self, ticker: str) -> Optional[dict]:
        """
        Rolling 7-day news sentiment score (1-100).

        Deterministic. No AI. Keyword-based per headline, then aggregated:
        - Positive keyword → +2, Negative → -2, Neutral → 0
        - Weighted by recency (today = 1.0, 7d ago = 0.3)
        - Normalized to 1-100 scale

        Returns:
            {'score': 65, 'direction': 'BULLISH', 'headlines_scored': 12, 'positive': 5, ...}
        """
        items = self.get_news(ticker, max_items=20)
        if not items:
            return None

        weighted_sum = 0.0
        max_possible = 0.0
        positive = negative = neutral = 0

        for i, item in enumerate(items):
            # Recency weight: 1.0 (newest) → ~0.3 (oldest)
            weight = max(0.3, 1.0 - (i / len(items)) * 0.7)

            sentiment = item.sentiment_hint
            if sentiment == 'POSITIVE':
                weighted_sum += 2 * weight
                positive += 1
            elif sentiment == 'NEGATIVE':
                weighted_sum -= 2 * weight
                negative += 1
            else:
                neutral += 1

            max_possible += 2 * weight

        # Normalize to 1-100
        if max_possible > 0:
            raw_pct = (weighted_sum + max_possible) / (2 * max_possible)  # 0-1
            score = round(raw_pct * 100)
        else:
            score = 50

        score = max(1, min(100, score))

        direction = 'BULLISH' if score >= 65 else ('BEARISH' if score <= 35 else 'NEUTRAL')

        return {
            'ticker': ticker,
            'score': score,
            'direction': direction,
            'headlines_scored': len(items),
            'positive': positive,
            'negative': negative,
            'neutral': neutral,
        }

    # ═══════════════════════════════════════════════════════════
    # FEAR & GREED INDEX
    # ═══════════════════════════════════════════════════════════

    def get_fear_greed(self) -> Optional[dict]:
        """Fetch CNN Fear & Greed Index from alternative.me API."""
        try:
            import requests
            resp = requests.get('https://api.alternative.me/fng/?limit=1', timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                item = data.get('data', [{}])[0]
                return {
                    'value': int(item.get('value', 50)),
                    'classification': item.get('value_classification', 'Neutral'),
                    'timestamp': item.get('timestamp', ''),
                }
        except Exception:
            pass
        return None

    # ═══════════════════════════════════════════════════════════
    # MACRO DATA (VIX, Rates, Yields)
    # ═══════════════════════════════════════════════════════════

    def get_macro_data(self) -> MacroData:
        """Fetch macro context: VIX, VVIX, DXY, credit spreads, yields, breadth."""
        macro = MacroData(fetched_at=datetime.now().isoformat())

        # VIX from Yahoo
        self._throttle()
        try:
            vix_t = yf.Ticker('^VIX')
            info = vix_t.info or {}
            macro.vix = info.get('regularMarketPrice') or info.get('previousClose')
        except Exception:
            pass

        # VVIX — vol-of-vol (regime shift detector)
        self._throttle()
        try:
            vvix_t = yf.Ticker('^VVIX')
            vvix_info = vvix_t.info or {}
            macro.vvix = vvix_info.get('regularMarketPrice') or vvix_info.get('previousClose')
        except Exception:
            pass

        # DXY — US Dollar Index
        self._throttle()
        try:
            dxy_t = yf.Ticker('DX-Y.NYB')
            dxy_info = dxy_t.info or {}
            macro.dxy = dxy_info.get('regularMarketPrice') or dxy_info.get('previousClose')
        except Exception:
            pass

        # Treasury yields
        self._throttle()
        try:
            tnx_t = yf.Ticker('^TNX')
            tnx_info = tnx_t.info or {}
            macro.treasury_10y = tnx_info.get('regularMarketPrice') or tnx_info.get('previousClose')
        except Exception:
            pass

        self._throttle()
        try:
            irx_t = yf.Ticker('^IRX')
            irx_info = irx_t.info or {}
            macro.treasury_2y = irx_info.get('regularMarketPrice') or irx_info.get('previousClose')
        except Exception:
            pass

        self._throttle()
        try:
            tyx_t = yf.Ticker('^TYX')
            tyx_info = tyx_t.info or {}
            macro.treasury_30y = tyx_info.get('regularMarketPrice') or tyx_info.get('previousClose')
        except Exception:
            pass

        # HYG/IEF credit spread proxy
        self._throttle()
        try:
            hyg_t = yf.Ticker('HYG')
            hyg_info = hyg_t.info or {}
            hyg_price = hyg_info.get('regularMarketPrice') or hyg_info.get('previousClose')
        except Exception:
            hyg_price = None

        self._throttle()
        try:
            ief_t = yf.Ticker('IEF')
            ief_info = ief_t.info or {}
            ief_price = ief_info.get('regularMarketPrice') or ief_info.get('previousClose')
        except Exception:
            ief_price = None

        if hyg_price and ief_price and ief_price > 0:
            macro.hyg_ief_spread = (hyg_price / ief_price - 1) * 100

        # Compute spreads
        if macro.treasury_10y and macro.treasury_2y:
            macro.yield_spread_10y2y = macro.treasury_10y - macro.treasury_2y

        # ── MULTI-CONDITION REGIME VOTING ──
        votes = 0  # each condition: +1 (risk-on), 0 (neutral), -1 (risk-off)

        # 1. VIX level
        if macro.vix:
            if macro.vix < 15:
                votes += 1
                macro.vix_regime = 'LOW'
            elif macro.vix < 20:
                votes += 0
                macro.vix_regime = 'NORMAL'
            elif macro.vix < 25:
                votes -= 1
                macro.vix_regime = 'ELEVATED'
            elif macro.vix < 30:
                votes -= 1
                macro.vix_regime = 'HIGH'
            else:
                votes -= 1
                macro.vix_regime = 'STRESS'

        # 2. Yield curve (10Y-2Y)
        if macro.yield_spread_10y2y is not None:
            if macro.yield_spread_10y2y > 1.0:
                votes += 1    # steep = growth
            elif macro.yield_spread_10y2y < 0:
                votes -= 1    # inverted = recession warning
            # else: flat = 0

        # 3. Credit spread (HYG/IEF proxy)
        if macro.hyg_ief_spread is not None:
            if macro.hyg_ief_spread > -2:       # HYG strong vs IEF = risk-on
                votes += 1
                macro.credit_regime = 'HEALTHY'
            elif macro.hyg_ief_spread < -5:     # HYG weak vs IEF = credit stress
                votes -= 1
                macro.credit_regime = 'STRESSED'
            else:
                macro.credit_regime = 'CONCERNING'

        # 4. VVIX — vol-of-vol: high VVIX = unstable vol = caution
        if macro.vvix:
            if macro.vvix > 120:
                votes -= 1    # volatile vol = regime shift risk

        # 5. DXY — rising dollar = risk-off (tightens financial conditions)
        # DXY > 105 = strong dollar headwind
        if macro.dxy:
            if macro.dxy > 106:
                votes -= 1
            elif macro.dxy < 100:
                votes += 1

        # Tally → regime score (-5 to +5)
        macro.regime_score = max(-5, min(5, votes))

        # Overall regime label
        if macro.regime_score >= 3:
            macro.market_regime = 'BULLISH'
        elif macro.regime_score >= 1:
            macro.market_regime = 'NEUTRAL'
        elif macro.regime_score >= -1:
            macro.market_regime = 'CAUTIOUS'
        elif macro.regime_score >= -3:
            macro.market_regime = 'VOLATILE'
        else:
            macro.market_regime = 'BEARISH'

        # Position sizing from config (rules.yaml regime.position_mult)
        from src.config import get_config
        cfg = get_config()
        macro.position_mult = cfg.position_mult(macro.market_regime)

        # Credit-stress hard gate — STRESSED credit caps sizing regardless of
        # the vote tally. See apply_credit_stress_cap docstring.
        apply_credit_stress_cap(macro, cfg.credit_stress_position_mult_cap)

        return macro


# ═══════════════════════════════════════════════════════════════
# KEYWORD-BASED NEWS CLASSIFICATION (deterministic, no AI)
# ═══════════════════════════════════════════════════════════════

_NEGATIVE_KEYWORDS = [
    'loss', 'decline', 'drop', 'plunge', 'crash', 'lawsuit', 'investigation',
    'fine', 'penalty', 'layoff', 'cut', 'downgrade', 'sell-off', 'selloff',
    'warning', 'miss', 'below', 'weak', 'bear', 'risk', 'debt', 'default',
    'recall', 'scandal', 'fraud', 'bankrupt', 'delist', 'halt',
]

_POSITIVE_KEYWORDS = [
    'beat', 'exceed', 'upgrade', 'raise', 'growth', 'profit', 'record',
    'buyback', 'dividend', 'expansion', 'partnership', 'approval', 'launch',
    'breakthrough', 'innovation', 'lead', 'surge', 'rally', 'bull', 'strong',
    'outperform', 'positive', 'gain',
]

_EARNINGS_KEYWORDS = ['earnings', 'revenue', 'eps', 'profit', 'quarter', 'q1', 'q2', 'q3', 'q4', 'fiscal', 'report', 'guidance', 'outlook']
_DIVIDEND_KEYWORDS = ['dividend', 'payout', 'yield', 'distribution']
_MA_KEYWORDS = ['acquisition', 'merger', 'acquire', 'takeover', 'buyout', 'spin-off', 'spinoff', 'divestiture']
_REGULATORY_KEYWORDS = ['sec', 'ftc', 'doj', 'regulation', 'antitrust', 'approval', 'lawsuit', 'investigation', 'fine', 'settlement']


def _classify_news_sentiment(title: str) -> str:
    t = title.lower()
    neg = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in t)
    pos = sum(1 for kw in _POSITIVE_KEYWORDS if kw in t)
    if neg > pos:
        return 'NEGATIVE'
    elif pos > neg:
        return 'POSITIVE'
    return 'NEUTRAL'


def _classify_news_type(title: str) -> str:
    t = title.lower()
    if any(kw in t for kw in _EARNINGS_KEYWORDS):
        return 'EARNINGS'
    if any(kw in t for kw in _DIVIDEND_KEYWORDS):
        return 'DIVIDEND'
    if any(kw in t for kw in _MA_KEYWORDS):
        return 'M&A'
    if any(kw in t for kw in _REGULATORY_KEYWORDS):
        return 'REGULATORY'
    return 'GENERAL'


# ═══════════════════════════════════════════════════════════════
# MOOMOO FALLBACK METHODS (called when moomoo times out or fails)
# ═══════════════════════════════════════════════════════════════

def get_stock_snapshot_fallback(ticker: str) -> Optional[StockSnapshot]:
    """Fallback stock snapshot from yfinance when moomoo fails."""
    try:
        _shut_up()
        plain_ticker = ticker.replace('US.', '')
        t = yf.Ticker(plain_ticker)
        info = t.info or {}
        hist = t.history(period='5d', interval='1d')

        if info is None or len(hist) == 0:
            _speak_up()
            return None

        latest = hist.iloc[-1]
        prev_close = info.get('previousClose') or latest.get('Close')

        snap = StockSnapshot(
            ticker=ticker,
            name=info.get('longName') or info.get('shortName', ''),
            last_price=info.get('currentPrice') or info.get('regularMarketPrice') or latest.get('Close'),
            open_price=latest.get('Open'),
            high_price=latest.get('High'),
            low_price=latest.get('Low'),
            prev_close=prev_close,
            bid=info.get('bid'),
            ask=info.get('ask'),
            bid_vol=info.get('bidSize'),
            ask_vol=info.get('askSize'),
            volume=int(latest.get('Volume') or 0),
            turnover=None,
            turnover_rate=None,
            volume_ratio=None,
            amplitude=None,
            highest_52w=info.get('fiftyTwoWeekHigh'),
            lowest_52w=info.get('fiftyTwoWeekLow'),
            pe_ratio=info.get('trailingPE'),
            pb_ratio=info.get('priceToBook'),
            pe_ttm=info.get('trailingPE'),
            earnings_yield=info.get('earningsQuarterlyGrowth'),
            market_cap=info.get('marketCap'),
            circulating_market_cap=info.get('floatShares'),
            eps_ttm=info.get('trailingEps'),
            net_profit=None,
            net_asset_per_share=info.get('bookValue'),
            dividend_ttm=info.get('dividendRate'),
            dividend_yield_ttm=info.get('dividendYield'),
            dividend_lfy=None,
            issued_shares=info.get('sharesOutstanding'),
            short_sell_rate=None,
            short_available=None,
            suspension=False,
            lot_size=100,
            update_time=datetime.now().isoformat(),
        )
        # Compute derived fields
        if snap.ask > 0 and snap.bid > 0:
            mid = (snap.ask + snap.bid) / 2
            snap.bid_ask_spread_pct = (snap.ask - snap.bid) / mid * 100
        if snap.prev_close > 0:
            snap.change_pct = (snap.last_price - snap.prev_close) / snap.prev_close * 100

        _speak_up()
        return snap
    except Exception as e:
        _speak_up()
        return None


def get_price_history_fallback(ticker: str, days: int = 252) -> list[dict]:
    """Fallback price history from yfinance when moomoo fails."""
    try:
        _shut_up()
        plain_ticker = ticker.replace('US.', '')
        t = yf.Ticker(plain_ticker)

        # Map days to yfinance period
        period = '1y' if days >= 252 else '6mo' if days >= 180 else '3mo' if days >= 90 else '1mo'
        hist = t.history(period=period, interval='1d')

        if hist is None or len(hist) == 0:
            _speak_up()
            return []

        history = []
        for date, row in hist.iterrows():
            history.append({
                'date': date.strftime('%Y-%m-%d'),
                'open': float(row.get('Open') or 0),
                'high': float(row.get('High') or 0),
                'low': float(row.get('Low') or 0),
                'close': float(row.get('Close') or 0),
                'volume': int(row.get('Volume') or 0),
            })

        _speak_up()
        return history[-days:]  # Return only the requested number of days
    except Exception as e:
        _speak_up()
        return []


# Import StockSnapshot for fallback methods
from src.data.models import StockSnapshot
