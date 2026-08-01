"""
Thesis Validation Module
Checks if original investment thesis remains intact

This module implements systematic thesis validation to prevent trading drift
and provide clear, automated exit signals when investment theses break.

Research-backed approach based on:
- Buffett's Rule #1: Sell when fundamentals deteriorate
- Lynch's "story changes" principle
- QuantWheel's thesis-based exit criteria
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple, Optional
import logging
from datetime import datetime, timedelta

# Import from existing modules
from src.data.models import StockSnapshot
from src.data.yfinance_client import YFinanceClient
from src.config import get_config

log = logging.getLogger(__name__)


class ThesisStatus(Enum):
    """Investment thesis status"""
    INTACT = "THESIS_INTACT"
    BROKEN = "THESIS_BROKEN"
    DAMAGED = "TECHNICAL_DAMAGE"


@dataclass
class ThesisCheck:
    """Result of a single thesis validation check"""
    metric: str
    current_value: float
    threshold: float
    severity: str  # INFO, WARNING, CRITICAL
    status: str
    message: str


@dataclass
class ThesisReport:
    """Complete thesis validation report"""
    ticker: str
    status: ThesisStatus
    checks: List[ThesisCheck]
    timestamp: datetime
    overall_assessment: str
    recommended_action: str


def validate_investment_thesis(
    ticker: str,
    entry_date: str,
    entry_thesis: dict,
    current_snapshot: Optional[StockSnapshot] = None,
    yf_client: Optional[YFinanceClient] = None
) -> ThesisReport:
    """
    Validate if original investment thesis still valid

    This function runs a comprehensive thesis validation check across multiple
    dimensions: earnings trend, fundamentals, technical health, and volatility regime.

    Args:
        ticker: Stock ticker symbol
        entry_date: Original position entry date (YYYY-MM-DD)
        entry_thesis: Dict containing original thesis (revenue_growth, eps_quality, etc.)
        current_snapshot: Current stock snapshot (optional, will fetch if None)
        yf_client: YFinance client for fetching data (optional)

    Returns:
        ThesisReport with status, all checks, and recommended action

    Example:
        >>> report = validate_investment_thesis('BE', '2026-07-01', {'pe_ratio': 456})
        >>> if report.status == ThesisStatus.BROKEN:
        ...     print(f"Exit position: {report.recommended_action}")
    """
    checks = []

    try:
        # Fetch current snapshot if not provided
        if current_snapshot is None:
            from src.data.moomoo_client import MoomooClient
            moomoo = MoomooClient()
            current_snapshot = moomoo.get_stock_snapshot(ticker)

        if current_snapshot is None:
            log.warning(f"Could not fetch snapshot for {ticker}")
            return ThesisReport(
                ticker=ticker,
                status=ThesisStatus.INTACT,
                checks=[],
                timestamp=datetime.now(),
                overall_assessment="Unable to validate - data unavailable",
                recommended_action="HOLD position pending data availability"
            )

        # === CHECK 1: Earnings Trend ===
        earnings_check = _check_earnings_trend(ticker, yf_client)
        if earnings_check:
            checks.append(earnings_check)

        # === CHECK 2: Fundamental Health ===
        fundamental_check = _check_fundamental_health(ticker, current_snapshot)
        if fundamental_check:
            checks.append(fundamental_check)

        # === CHECK 3: Technical Damage ===
        technical_check = _check_technical_damage(ticker, current_snapshot, yf_client)
        if technical_check:
            checks.append(technical_check)

        # === CHECK 4: Volatility Regime ===
        volatility_check = _check_volatility_regime(current_snapshot)
        if volatility_check:
            checks.append(volatility_check)

        # === CHECK 5: Price Performance ===
        price_check = _check_price_performance(current_snapshot)
        if price_check:
            checks.append(price_check)

        # Determine overall status
        critical_checks = [c for c in checks if c.severity == "CRITICAL"]
        warning_checks = [c for c in checks if c.severity == "WARNING"]

        if critical_checks:
            status = ThesisStatus.BROKEN
        elif warning_checks:
            status = ThesisStatus.DAMAGED
        else:
            status = ThesisStatus.INTACT

        # Generate overall assessment and action
        overall_assessment = _generate_overall_assessment(status, checks)
        recommended_action = _generate_recommended_action(ticker, status, checks)

        return ThesisReport(
            ticker=ticker,
            status=status,
            checks=checks,
            timestamp=datetime.now(),
            overall_assessment=overall_assessment,
            recommended_action=recommended_action
        )

    except Exception as e:
        log.error(f"Error validating thesis for {ticker}: {e}", exc_info=True)
        return ThesisReport(
            ticker=ticker,
            status=ThesisStatus.INTACT,
            checks=[],
            timestamp=datetime.now(),
            overall_assessment=f"Validation error: {str(e)}",
            recommended_action="HOLD position - unable to validate"
        )


def _check_earnings_trend(
    ticker: str,
    yf_client: Optional[YFinanceClient]
) -> Optional[ThesisCheck]:
    """
    Check earnings trend for analyst downgrades

    CRITICAL if: Earnings estimate cuts >20%
    WARNING if: Earnings estimate cuts >10%
    """
    if yf_client is None:
        return None

    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)

        # Get analyst estimates (if available)
        try:
            # Try to get earnings estimates
            info = stock.info

            # Check for negative earnings revisions
            # This is a simplified check - real implementation would use analyst data
            if 'earningsQuarterlyGrowth' in info:
                earnings_growth = info.get('earningsQuarterlyGrowth', 0)

                if earnings_growth < -20:
                    return ThesisCheck(
                        metric="earnings_trend",
                        current_value=earnings_growth,
                        threshold=-20,
                        severity="CRITICAL",
                        status="FAILED",
                        message=f"Analysts downgrading earnings estimates >20% (current: {earnings_growth:.1f}%)"
                    )
                elif earnings_growth < -10:
                    return ThesisCheck(
                        metric="earnings_trend",
                        current_value=earnings_growth,
                        threshold=-10,
                        severity="WARNING",
                        status="CONCERNING",
                        message=f"Earnings growth declining (current: {earnings_growth:.1f}%)"
                    )
        except Exception as e:
            log.debug(f"Could not fetch earnings trend for {ticker}: {e}")

    except Exception as e:
        log.debug(f"Error checking earnings trend: {e}")

    return None


def _check_fundamental_health(ticker: str, snapshot: StockSnapshot) -> Optional[ThesisCheck]:
    """
    Check fundamental health using P/E ratio and other metrics.

    Thresholds come from config/rules.yaml → thesis_validation so they can be
    tuned without code changes. Trusted tickers (thesis_validation.trusted_tickers)
    skip the P/E valuation check entirely — the user accepts their valuation.
    A negative P/E still flags unless pe_negative_critical is false.

    CRITICAL if: P/E negative (configurable) or above pe_ratio_critical
    WARNING if: P/E above pe_ratio_warning
    """
    pe_ratio = snapshot.pe_ratio if hasattr(snapshot, 'pe_ratio') else None

    if pe_ratio is None or pe_ratio == 0:
        return None

    cfg = get_config()
    pe_warning = cfg.thesis_validation('pe_ratio_warning', 50)
    pe_critical = cfg.thesis_validation('pe_ratio_critical', 100)
    pe_negative_critical = cfg.thesis_validation('pe_negative_critical', True)
    trusted = cfg.trusted_tickers

    # Negative P/E = company losing money — flag unless explicitly disabled.
    # Trusted tickers skip only the *valuation* (high-P/E) check, not solvency.
    if pe_ratio < 0 and pe_negative_critical:
        return ThesisCheck(
            metric="fundamentals_pe",
            current_value=pe_ratio,
            threshold=0,
            severity="CRITICAL",
            status="FAILED",
            message=f"Negative P/E ratio ({pe_ratio:.1f}) - company losing money"
        )

    # Trusted tickers skip the high-P/E valuation check.
    if ticker and ticker.upper().replace('US.', '') in trusted:
        log.debug(f"{ticker}: trusted — skipping P/E valuation check (P/E {pe_ratio:.1f})")
        return None

    if pe_ratio > pe_critical:
        return ThesisCheck(
            metric="fundamentals_pe",
            current_value=pe_ratio,
            threshold=pe_critical,
            severity="CRITICAL",
            status="FAILED",
            message=f"P/E ratio extremely high ({pe_ratio:.1f}) - speculative valuation"
        )
    elif pe_ratio > pe_warning:
        return ThesisCheck(
            metric="fundamentals_pe",
            current_value=pe_ratio,
            threshold=pe_warning,
            severity="WARNING",
            status="CONCERNING",
            message=f"P/E ratio elevated ({pe_ratio:.1f}) - monitor fundamentals"
        )

    return None


def _check_technical_damage(
    ticker: str,
    snapshot: StockSnapshot,
    yf_client: Optional[YFinanceClient]
) -> Optional[ThesisCheck]:
    """
    Check for technical damage using price vs 200-day SMA

    CRITICAL if: Price below 200 SMA by >25%
    WARNING if: Price below 200 SMA by >15%
    """
    if yf_client is None:
        return None

    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)

        # Get historical data for 200-day SMA
        hist = stock.history(period="1y")

        if len(hist) < 200:
            log.debug(f"Insufficient data for 200 SMA on {ticker}")
            return None

        current_price = snapshot.last_price
        sma_200 = hist['Close'].tail(200).mean()

        if sma_200 == 0:
            return None

        pct_below_sma = ((current_price - sma_200) / sma_200) * 100

        if pct_below_sma < -25:
            return ThesisCheck(
                metric="technical_damage_200sma",
                current_value=pct_below_sma,
                threshold=-25,
                severity="CRITICAL",
                status="FAILED",
                message=f"Price {abs(pct_below_sma):.1f}% below 200-day SMA - major breakdown"
            )
        elif pct_below_sma < -15:
            return ThesisCheck(
                metric="technical_damage_200sma",
                current_value=pct_below_sma,
                threshold=-15,
                severity="WARNING",
                status="CONCERNING",
                message=f"Price {abs(pct_below_sma):.1f}% below 200-day SMA - technical damage"
            )

    except Exception as e:
        log.debug(f"Error checking technical damage for {ticker}: {e}")

    return None


def _check_volatility_regime(snapshot: StockSnapshot) -> Optional[ThesisCheck]:
    """
    Check for elevated volatility regime

    WARNING if: Historical volatility >100% (structural volatility change)
    """
    hv = snapshot.hv_30d if hasattr(snapshot, 'hv_30d') else None

    if hv is None:
        return None

    if hv > 100:
        return ThesisCheck(
            metric="volatility_regime",
            current_value=hv,
            threshold=100,
            severity="WARNING",
            status="CONCERNING",
            message=f"Historical volatility elevated ({hv:.1f}%) - regime change"
        )

    return None


def _check_price_performance(snapshot: StockSnapshot) -> Optional[ThesisCheck]:
    """
    Check price performance from highs

    CRITICAL if: Stock down >40% from 52-week highs
    WARNING if: Stock down >25% from 52-week highs
    """
    week_52_high = snapshot.highest_52w if hasattr(snapshot, 'highest_52w') else None
    current_price = snapshot.last_price

    if week_52_high is None or week_52_high == 0:
        return None

    pct_off_high = ((current_price - week_52_high) / week_52_high) * 100

    if pct_off_high < -40:
        return ThesisCheck(
            metric="price_performance_52w",
            current_value=pct_off_high,
            threshold=-40,
            severity="CRITICAL",
            status="FAILED",
            message=f"Stock down {abs(pct_off_high):.1f}% from 52-week highs - major decline"
        )
    elif pct_off_high < -25:
        return ThesisCheck(
            metric="price_performance_52w",
            current_value=pct_off_high,
            threshold=-25,
            severity="WARNING",
            status="CONCERNING",
            message=f"Stock down {abs(pct_off_high):.1f}% from 52-week highs - concerning"
        )

    return None


def _generate_overall_assessment(status: ThesisStatus, checks: List[ThesisCheck]) -> str:
    """Generate overall assessment of thesis health"""
    if status == ThesisStatus.BROKEN:
        failed_checks = [c for c in checks if c.severity == "CRITICAL"]
        reasons = "; ".join([c.message for c in failed_checks])
        return f"THESIS BROKEN: {reasons}"

    elif status == ThesisStatus.DAMAGED:
        warning_checks = [c for c in checks if c.severity == "WARNING"]
        reasons = "; ".join([c.message for c in warning_checks])
        return f"THESIS DAMAGED: {reasons}"

    else:
        return "THESIS INTACT: All checks passed, investment thesis remains valid"


def _generate_recommended_action(ticker: str, status: ThesisStatus, checks: List[ThesisCheck]) -> str:
    """Generate recommended action based on thesis validation"""
    if status == ThesisStatus.BROKEN:
        critical_checks = [c for c in checks if c.severity == "CRITICAL"]
        reasons = "; ".join([c.message for c in critical_checks])
        return (
            f"🚨 THESIS BROKEN — Exit Wheel on {ticker}\n"
            f"Reason: {reasons}\n"
            f"Action: Close position, add to Do-Not-Wheel list for 6 months"
        )

    elif status == ThesisStatus.DAMAGED:
        warning_checks = [c for c in checks if c.severity == "WARNING"]
        reasons = "; ".join([c.message for c in warning_checks])
        return (
            f"⚠️  THESIS DAMAGED — Monitor {ticker}\n"
            f"Concerns: {reasons}\n"
            f"Action: Weekly monitoring, re-evaluate in 7 days"
        )

    else:
        return (
            f"✅ THESIS INTACT — Continue Wheel on {ticker}\n"
            f"Status: All checks passed\n"
            f"Action: Hold position, let assignment occur naturally"
        )


def quick_thesis_check(ticker: str, snapshot: StockSnapshot) -> dict:
    """
    Quick thesis check for decision messages (non-blocking)

    This is a faster, simpler version used in decision messages where
    full validation isn't necessary. Returns a simple dict with flags.

    Returns:
        dict with 'broken' and 'damaged' boolean flags

    Example:
        >>> check = quick_thesis_check('BE', snapshot)
        >>> if check['broken']:
        ...     print("Thesis broken - exit position")
    """
    try:
        # Quick fundamental check (P/E) — honors the same config + trust list
        # as the full validator so decision messages stay consistent with it.
        pe_ratio = snapshot.pe_ratio if hasattr(snapshot, 'pe_ratio') else None

        # Quick price performance check
        week_52_high = snapshot.highest_52w if hasattr(snapshot, 'highest_52w') else None
        current_price = snapshot.last_price

        broken = False
        damaged = False

        cfg = get_config()
        pe_critical = cfg.thesis_validation('pe_ratio_critical', 100)
        pe_negative_critical = cfg.thesis_validation('pe_negative_critical', True)
        trusted = cfg.trusted_tickers
        is_trusted = ticker and ticker.upper().replace('US.', '') in trusted

        # P/E drives BROKEN: negative (solvency) or above critical (valuation).
        # Trusted tickers skip the high-P/E valuation flag, not the negative one.
        if pe_ratio:
            if pe_ratio < 0 and pe_negative_critical:
                broken = True
            elif pe_ratio > pe_critical and not is_trusted:
                broken = True

        if not broken and week_52_high and week_52_high > 0:
            pct_off_high = ((current_price - week_52_high) / week_52_high) * 100
            if pct_off_high < -40:
                broken = True
            elif pct_off_high < -25:
                damaged = True

        return {
            'broken': broken,
            'damaged': damaged,
            'pe_ratio': pe_ratio,
            'pct_off_high': ((current_price - week_52_high) / week_52_high * 100) if week_52_high else None
        }

    except Exception as e:
        log.debug(f"Error in quick thesis check for {ticker}: {e}")
        # Default to intact if data unavailable
        return {'broken': False, 'damaged': False}
