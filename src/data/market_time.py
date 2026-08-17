"""US Eastern time helpers — shared by the OIE engine and paper DB.

DST is handled by the IANA zone database (America/New_York), NOT by the old
month-approximation (which was wrong by an hour for up to ~7 days a year).
The "trading day" for daily limits is the EASTERN date, so the daily
new-position budget resets at midnight ET — not at local-machine midnight.

Note: this does NOT include a US holiday/half-day calendar — market-hours
checks still treat federal holidays as open (documented as a known gap in
specs/oie-paper-engine-spec.md §7).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')


def eastern_now() -> datetime:
    """Current US Eastern time (DST-aware via zoneinfo)."""
    return datetime.now(ET)


def et_today_str() -> str:
    """Current US Eastern date as YYYY-MM-DD (the US trading day)."""
    return eastern_now().strftime('%Y-%m-%d')
