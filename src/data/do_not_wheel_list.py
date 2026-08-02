"""
Do-Not-Wheel List — Persistent exclusion list for broken thesis stocks

This module maintains a persistent list of stocks that should not be wheeled
for a specified time period (typically 6 months) after thesis break.

The list is stored in config/do_not_wheel.yaml and loaded by:
- Screener: to filter out excluded tickers
- Comprehensive analysis: to show active exclusions
- Portfolio decisions: to prevent new positions on excluded stocks

Usage:
    from src.data.do_not_wheel_list import DoNotWheelList

    dnl = DoNotWheelList()
    dnl.add('AMD', months=6, reason='P/E 183.2 - speculative valuation')
    dnl.add('PLTR', months=6, reason='P/E 194.1, -20% SMA - thesis broken')
    dnl.add('TSLA', months=6, reason='P/E 286.0, -25% SMA - thesis broken')

    if dnl.is_excluded('AMD'):
        print(f"AMD excluded until {dnl.get_expiration('AMD')}")
"""

import os
import yaml
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from typing import Dict, List, Optional
from dataclasses import dataclass
import logging

log = logging.getLogger(__name__)


@dataclass
class ExclusionEntry:
    """A single Do-Not-Wheel exclusion entry"""
    ticker: str
    added_date: str
    expiration_date: str
    reason: str
    months: int


class DoNotWheelList:
    """
    Do-Not-Wheel List Manager

    Manages persistent exclusion list for stocks with broken theses.
    Prevents re-opening positions on fundamentally broken stocks.
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize Do-Not-Wheel list

        Args:
            config_path: Path to YAML file (defaults to config/do_not_wheel.yaml)
        """
        if config_path is None:
            # Default to config/do_not_wheel.yaml in project root
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            config_path = os.path.join(project_root, 'config', 'do_not_wheel.yaml')

        self.config_path = config_path
        self.exclusions: Dict[str, ExclusionEntry] = {}
        self._load()

    def _load(self):
        """Load exclusions from YAML file"""
        if not os.path.exists(self.config_path):
            log.debug(f"No Do-Not-Wheel file found at {self.config_path}, starting fresh")
            self.exclusions = {}
            return

        try:
            with open(self.config_path, 'r') as f:
                data = yaml.safe_load(f) or {}

            for ticker, entry_data in data.items():
                self.exclusions[ticker] = ExclusionEntry(
                    ticker=ticker,
                    added_date=entry_data.get('added_date', ''),
                    expiration_date=entry_data.get('expiration_date', ''),
                    reason=entry_data.get('reason', ''),
                    months=entry_data.get('months', 6)
                )

            log.info(f"Loaded {len(self.exclusions)} Do-Not-Wheel exclusions from {self.config_path}")

        except Exception as e:
            log.error(f"Error loading Do-Not-Wheel list: {e}")
            self.exclusions = {}

    def _save(self):
        """Save exclusions to YAML file"""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)

            data = {}
            for ticker, entry in self.exclusions.items():
                data[ticker] = {
                    'added_date': entry.added_date,
                    'expiration_date': entry.expiration_date,
                    'reason': entry.reason,
                    'months': entry.months
                }

            with open(self.config_path, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=True)

            log.info(f"Saved {len(self.exclusions)} Do-Not-Wheel exclusions to {self.config_path}")

        except Exception as e:
            log.error(f"Error saving Do-Not-Wheel list: {e}")

    def add(self, ticker: str, months: int = 6, reason: str = "Thesis broken"):
        """
        Add a ticker to the Do-Not-Wheel list

        Args:
            ticker: Stock ticker symbol
            months: Months to exclude (default 6)
            reason: Reason for exclusion
        """
        ticker = ticker.upper().replace('US.', '')

        # Calculate expiration date
        added_date = datetime.now().strftime('%Y-%m-%d')
        expiration_date = (datetime.now() + relativedelta(months=months)).strftime('%Y-%m-%d')

        self.exclusions[ticker] = ExclusionEntry(
            ticker=ticker,
            added_date=added_date,
            expiration_date=expiration_date,
            reason=reason,
            months=months
        )

        self._save()
        log.info(f"Added {ticker} to Do-Not-Wheel list until {expiration_date}: {reason}")

    def remove(self, ticker: str):
        """
        Remove a ticker from the Do-Not-Wheel list

        Args:
            ticker: Stock ticker symbol
        """
        ticker = ticker.upper().replace('US.', '')

        if ticker in self.exclusions:
            del self.exclusions[ticker]
            self._save()
            log.info(f"Removed {ticker} from Do-Not-Wheel list")
        else:
            log.debug(f"{ticker} not in Do-Not-Wheel list")

    def is_excluded(self, ticker: str) -> bool:
        """
        Check if a ticker is currently excluded

        Args:
            ticker: Stock ticker symbol

        Returns:
            True if ticker is excluded (and not expired)
        """
        ticker = ticker.upper().replace('US.', '')

        if ticker not in self.exclusions:
            return False

        entry = self.exclusions[ticker]

        # Check if expired
        if datetime.now() > datetime.strptime(entry.expiration_date, '%Y-%m-%d'):
            log.debug(f"{ticker} exclusion expired on {entry.expiration_date}, removing")
            self.remove(ticker)
            return False

        return True

    def get_expiration(self, ticker: str) -> Optional[str]:
        """
        Get expiration date for an excluded ticker

        Args:
            ticker: Stock ticker symbol

        Returns:
            Expiration date string or None if not excluded
        """
        ticker = ticker.upper().replace('US.', '')

        if ticker not in self.exclusions:
            return None

        return self.exclusions[ticker].expiration_date

    def get_reason(self, ticker: str) -> Optional[str]:
        """
        Get exclusion reason for a ticker

        Args:
            ticker: Stock ticker symbol

        Returns:
            Reason string or None if not excluded
        """
        ticker = ticker.upper().replace('US.', '')

        if ticker not in self.exclusions:
            return None

        return self.exclusions[ticker].reason

    def get_all_exclusions(self) -> List[ExclusionEntry]:
        """
        Get all current (non-expired) exclusions

        Returns:
            List of ExclusionEntry objects
        """
        # Clean up expired entries first
        current_time = datetime.now()
        active_exclusions = []

        for ticker, entry in self.exclusions.items():
            expiration = datetime.strptime(entry.expiration_date, '%Y-%m-%d')
            if current_time <= expiration:
                active_exclusions.append(entry)
            else:
                # Remove expired entry
                self.remove(ticker)

        return active_exclusions

    def get_excluded_tickers(self) -> List[str]:
        """
        Get list of currently excluded tickers

        Returns:
            List of ticker symbols
        """
        return [entry.ticker for entry in self.get_all_exclusions()]

    def clear_expired(self):
        """Remove all expired exclusions"""
        current_time = datetime.now()
        expired = []

        for ticker, entry in list(self.exclusions.items()):
            expiration = datetime.strptime(entry.expiration_date, '%Y-%m-%d')
            if current_time > expiration:
                expired.append(ticker)
                self.remove(ticker)

        if expired:
            log.info(f"Cleared {len(expired)} expired exclusions: {', '.join(expired)}")

    def clear_all(self):
        """Clear all exclusions (use with caution)"""
        count = len(self.exclusions)
        self.exclusions = {}
        self._save()
        log.warning(f"Cleared all {count} Do-Not-Wheel exclusions")


def is_excluded_from_wheel(ticker: str) -> bool:
    """
    Convenience function to check if ticker is excluded from wheel

    Args:
        ticker: Stock ticker symbol

    Returns:
        True if excluded (and not expired)
    """
    dnl = DoNotWheelList()
    return dnl.is_excluded(ticker)


def add_to_do_not_wheel_list(ticker: str, months: int = 6, reason: str = "Thesis broken"):
    """
    Convenience function to add ticker to exclusion list

    Args:
        ticker: Stock ticker symbol
        months: Months to exclude (default 6)
        reason: Reason for exclusion
    """
    dnl = DoNotWheelList()
    dnl.add(ticker, months=months, reason=reason)


def is_wheel_eligible(snapshot, ticker: str = '') -> tuple:
    """Can we run the Wheel on this stock today? Read-only, daily check.

    The watchlist is the master list — this filter only auto-skips *clear,
    objective* loss-makers so they never get screened. It uses moomoo snapshot
    data only (``net_profit`` + ``eps_ttm`` + ``pe_ratio``), so it is fast and
    has no yfinance dependency. Curate the watchlist directly for anything
    subjective ("consistently bad"); use ``do_not_wheel.yaml`` only as a manual
    override.

    Blocks when (each toggle-able via ``thesis_validation`` in rules.yaml):
      - ``unprofitable_block``: ``net_profit < 0`` AND ``eps_ttm < 0`` (loss-maker)
      - ``pe_negative_critical``: ``pe_ratio < 0`` (negative P/E = losing money)

    Args:
        snapshot: a StockSnapshot (moomoo) with ``net_profit``, ``eps_ttm``,
            ``pe_ratio`` fields. Duck-typed — only the fields it reads.
        ticker: optional ticker for the reason string.

    Returns:
        (eligible: bool, reason: str). ``eligible=True`` → proceed to screen.
    """
    def _val(name):
        v = getattr(snapshot, name, None) if snapshot is not None else None
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    try:
        from src.config import get_config
        cfg = get_config()
        block_unprofitable = cfg.thesis_validation('unprofitable_block', True)
        block_neg_pe = cfg.thesis_validation('pe_negative_critical', True)
    except Exception:
        block_unprofitable, block_neg_pe = True, True

    net_profit = _val('net_profit')
    eps_ttm = _val('eps_ttm')
    pe_ratio = _val('pe_ratio')
    label = ticker or getattr(snapshot, 'ticker', '') or ''

    # Unprofitable: net loss AND negative trailing EPS (a clear loss-maker).
    # One negative quarter (net_profit<0 but eps still positive, or vice versa)
    # is NOT enough — that's often a one-off charge, not a broken business.
    if block_unprofitable and net_profit is not None and eps_ttm is not None:
        if net_profit < 0 and eps_ttm < 0:
            return False, (f"{label}: not eligible — unprofitable "
                           f"(net profit ${net_profit:,.0f}, EPS {eps_ttm:.2f})")

    # Negative P/E is the same loss-maker signal expressed via valuation.
    if block_neg_pe and pe_ratio is not None and pe_ratio < 0:
        return False, (f"{label}: not eligible — negative P/E ({pe_ratio:.1f}, "
                       f"company losing money)")

    return True, ""


if __name__ == "__main__":
    # Test the Do-Not-Wheel list
    print("=" * 60)
    print("DO-NOT-WHEEL LIST TEST")
    print("=" * 60)

    dnl = DoNotWheelList()

    # Add test exclusions
    print("\n📝 Adding test exclusions...")
    dnl.add('AMD', months=6, reason='P/E 183.2 - speculative valuation')
    dnl.add('PLTR', months=6, reason='P/E 194.1, -20% SMA - thesis broken')
    dnl.add('TSLA', months=6, reason='P/E 286.0, -25% SMA - thesis broken')

    # Check exclusions
    print("\n🔍 Checking exclusions...")
    for ticker in ['AMD', 'PLTR', 'TSLA', 'V', 'AAPL']:
        if dnl.is_excluded(ticker):
            print(f"  ❌ {ticker}: EXCLUDED until {dnl.get_expiration(ticker)}")
            print(f"     Reason: {dnl.get_reason(ticker)}")
        else:
            print(f"  ✅ {ticker}: Not excluded")

    # Get all exclusions
    print("\n📋 All active exclusions:")
    exclusions = dnl.get_all_exclusions()
    for entry in exclusions:
        print(f"  {entry.ticker}: until {entry.expiration_date} ({entry.months} months)")
        print(f"    Reason: {entry.reason}")

    print("\n✅ Do-Not-Wheel list test complete")
