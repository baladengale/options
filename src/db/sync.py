"""
Data sync engine and freshness checking per SPECS Sections 2.3 and 3.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class SyncReport:
    """Result of a full sync operation."""
    success: bool
    portfolio_synced: bool
    orders_synced: bool
    positions_synced: bool
    chains_synced: dict[str, bool] = field(default_factory=dict)
    prices_synced: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    synced_at: datetime = field(default_factory=datetime.now)
    data_source: str = 'MOOMOO'


# ============================================================
# Freshness Checking (SPECS Section 2.3)
# ============================================================

# TTLs in seconds for each data type
DEFAULT_TTL = {
    'portfolio': 300,       # 5 minutes
    'orders': 300,          # 5 minutes
    'options_chain': 300,   # 5 minutes
    'price_history': 86400, # 24 hours
    'fundamentals': 86400,  # 24 hours
    'earnings': 604800,     # 7 days
}


def check_freshness(synced_at: datetime, max_age_seconds: int) -> bool:
    """
    Check if data synced at `synced_at` is still within `max_age_seconds`.

    Returns True if fresh, False if stale.
    """
    if synced_at is None:
        return False
    now = datetime.now()
    age = (now - synced_at).total_seconds()
    return age <= max_age_seconds
