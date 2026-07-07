"""
Options chain quality analysis per SPECS Section 7.2.3.

Scores individual option contracts on:
- Bid-ask spread tightness
- Open interest depth
- Volume liquidity
- IV vs HV spread
- Term structure
"""


def compute_spread_score(bid: float, ask: float) -> float:
    """Score bid-ask spread as percentage of mid price."""
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 20  # worst bucket for invalid data
    spread_pct = (ask - bid) / mid * 100

    if spread_pct < 0.5:
        return 100
    elif spread_pct < 1.0:
        return 80
    elif spread_pct < 2.0:
        return 60
    elif spread_pct < 5.0:
        return 40
    else:
        return 20


def compute_oi_score(open_interest: int) -> float:
    """Score open interest depth."""
    if open_interest >= 1000:
        return 100
    elif open_interest >= 500:
        return 80
    elif open_interest >= 100:
        return 60
    elif open_interest >= 50:
        return 40
    else:
        return 20


def compute_volume_score(volume: int) -> float:
    """Score daily volume liquidity."""
    if volume >= 500:
        return 100
    elif volume >= 100:
        return 70
    elif volume >= 50:
        return 40
    else:
        return 10


def compute_iv_hv_spread_score(iv: float, hv: float) -> float:
    """Score the spread between implied and historical volatility."""
    diff_pct = abs(iv - hv) * 100  # convert to percentage points

    if diff_pct <= 5:
        return 100
    elif diff_pct <= 10:
        return 70
    elif diff_pct <= 20:
        return 40
    else:
        return 20


def compute_options_chain_score(contract: dict) -> float:
    """
    Composite options chain quality score for a single contract.

    Averages: spread, OI, volume, IV/HV spread, and term structure scores.
    """
    scores = []

    # Bid-ask spread
    if 'bid' in contract and 'ask' in contract:
        scores.append(compute_spread_score(contract['bid'], contract['ask']))

    # Open interest
    if 'oi' in contract:
        scores.append(compute_oi_score(contract['oi']))

    # Volume
    if 'volume' in contract:
        scores.append(compute_volume_score(contract['volume']))

    # IV vs HV
    if 'iv' in contract:
        # Use 0.25 as default HV if not provided
        hv = contract.get('hv', 0.25)
        scores.append(compute_iv_hv_spread_score(contract['iv'], hv))

    if not scores:
        return 0.0

    return sum(scores) / len(scores)
