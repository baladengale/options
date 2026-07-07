"""
Correlation analysis per SPECS Section 7.2.5.

Computes correlation penalty vs existing holdings.
"""


def compute_correlation_score(
    correlations: dict[str, float],
    max_threshold: float = 0.8,
) -> float:
    """
    Compute correlation penalty score.

    If any correlation > max_threshold → 0 (HARD FAIL)
    Otherwise: 100 × (1 - max_correlation)
    """
    if not correlations:
        return 100.0

    max_corr = max(correlations.values())

    if max_corr > max_threshold:
        return 0.0  # HARD FAIL

    return round(100.0 * (1.0 - max_corr), 2)
