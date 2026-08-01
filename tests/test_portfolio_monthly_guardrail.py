"""Tests for the monthly-order guardrail fix in scripts/portfolio.py.

Background: `_compute_staged_guardrails` previously fed the all-time filled-order
count into the monthly guardrail, so any mature account (>10–20 lifetime fills)
was permanently BLOCKed. `_filled_orders_this_month` buckets by the order's fill
date so only the current calendar month counts.

`scripts/` is not an importable package (it runs code at import via `__file__`),
so we load just the helper with importlib rather than importing the module.
"""

import importlib.util
from datetime import date
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "portfolio.py"


def _load_filled_orders_this_month():
    """Load scripts/portfolio.py as an isolated module and return the helper.

    Module-level side effects in portfolio.py are harmless setup (warnings
    filters + sys.path insert); main() only runs under __main__, which the
    importlib spec name avoids.
    """
    spec = importlib.util.spec_from_file_location("_portfolio_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._filled_orders_this_month


_filled_orders_this_month = _load_filled_orders_this_month()


def _ord(status, d):
    """Build a normalized order dict like portfolio_loader.fetch_orders emits."""
    return {"status": status, "date": d, "order_id": d + status}


TODAY = date.today()
YM = TODAY.strftime("%Y-%m")


# ── bucketing by current month ────────────────────────────────────

def test_counts_only_current_month_fills():
    orders = [
        _ord("FILLED_ALL", f"{YM}-01"),
        _ord("FILLED_PART", f"{YM}-15"),
        _ord("FILLED_ALL", "2024-03-10"),   # different year → ignored
        _ord("FILLED_ALL", "2025-12-31"),   # different month → ignored
    ]
    assert _filled_orders_this_month(orders) == 2


def test_all_time_history_does_not_false_block():
    """The regression: a year of fills must not count toward this month."""
    orders = [_ord("FILLED_ALL", f"2025-{m:02d}-15") for m in range(1, 13)]  # 12 historical
    orders += [_ord("FILLED_ALL", f"{YM}-03")]                                # 1 this month
    # Historical orders are excluded, so the monthly count stays well under the
    # guardrail limit (10 target / 20 emergency).
    assert _filled_orders_this_month(orders) == 1


def test_empty_orders_is_zero():
    assert _filled_orders_this_month([]) == 0


def test_no_filled_status_is_zero():
    # Working / cancelled orders never count.
    orders = [
        _ord("WORKING", f"{YM}-01"),
        _ord("CANCELLED", f"{YM}-02"),
        _ord("SUBMITTED", f"{YM}-03"),
    ]
    assert _filled_orders_this_month(orders) == 0


# ── robustness ────────────────────────────────────────────────────

def test_missing_or_blank_date_ignored():
    orders = [
        {"status": "FILLED_ALL"},                 # no date key
        {"status": "FILLED_ALL", "date": None},   # null date
        {"status": "FILLED_ALL", "date": ""},     # blank date
        {"status": "FILLED_ALL", "date": f"{YM}-04"},
    ]
    assert _filled_orders_this_month(orders) == 1


def test_both_filled_statuses_count():
    orders = [
        _ord("FILLED_ALL", f"{YM}-01"),
        _ord("FILLED_PART", f"{YM}-02"),
    ]
    assert _filled_orders_this_month(orders) == 2
