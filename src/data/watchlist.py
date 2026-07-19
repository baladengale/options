"""
Watchlist — fetch live tickers from moomoo watchlist group.

Shared by screener.py and oie_engine.py. Single source of truth.
"""

import re
from typing import Optional

from src.config import get_config


# Default watchlist (fallback if moomoo watchlist fetch fails)
DEFAULT_WATCHLIST = [
    'US.V', 'US.MSFT', 'US.GOOGL', 'US.AAPL', 'US.AMZN',
    'US.NVDA', 'US.META', 'US.AVGO', 'US.ADBE', 'US.CRM', 'US.AMD',
]


def fetch_live_watchlist(moomoo_ctx, group_name: Optional[str] = None) -> list[str]:
    """Pull US stock tickers from moomoo watchlist group. Fallback to DEFAULT_WATCHLIST.

    Args:
        moomoo_ctx: moomoo OpenSecTradeContext (must be connected)
        group_name: override watchlist group name (default: from config)

    Returns:
        list of ticker codes like ['US.V', 'US.MSFT', ...]
    """
    if group_name is None:
        cfg = get_config()
        group_name = cfg.moomoo_watchlist_group

    try:
        from moomoo import RET_OK
        ret, data = moomoo_ctx.get_user_security(group_name)
        if ret == RET_OK and data is not None and len(data) > 0:
            tickers = []
            for _, row in data.iterrows():
                code = row['code']
                # US stocks only — skip option contracts, crypto, indices
                if (code.startswith('US.') and not re.search(r'\d{6}[CP]\d+', code)
                        and '..' not in code):
                    tickers.append(code)
            if tickers:
                return tickers
    except Exception:
        pass
    return DEFAULT_WATCHLIST
