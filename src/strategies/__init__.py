"""Strategy-specific scoring modules.

Each module scores one strategy family using the shared ticker score from
src/scoring/ plus its own contract-level penalty. Today: put credit spreads
(src/strategies/credit_spread.py). CSP/CC scoring stays in src/scoring/.
"""
