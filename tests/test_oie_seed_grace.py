"""
Seed grace-window tests for the OIE engine.

The post-seed grace window skips exit decisions on the first cycle after
seeding, so real-world-seeded positions aren't immediately rolled/closed.

Regression guard for the bug where the grace window was anchored to each
position's ``created_at`` instead of ``seeded_at``: seed rows are all written
within milliseconds of ``seeded_at``, so the old ``abs(created_at - seeded_at)
< 300`` comparison was true forever and permanently suppressed exit decisions
for every seed-time position (e.g. a CSP left at 75% premium captured for days).
"""

import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.oie_engine import within_seed_grace, SEED_GRACE_SECONDS


SEED = datetime(2026, 8, 8, 12, 38, 37, 601795)
SEED_STR = SEED.isoformat()


def _at(seconds_after_seed: float) -> datetime:
    return SEED + timedelta(seconds=seconds_after_seed)


def test_no_seed_string_means_no_grace():
    """Empty/missing seeded_at → no grace window."""
    assert within_seed_grace('') is False
    assert within_seed_grace(None) is False


def test_unparseable_seed_string_means_no_grace():
    """A malformed seeded_at must not trip the grace window (data-blind)."""
    assert within_seed_grace('not-a-timestamp') is False


def test_just_seeded_is_within_grace():
    """First cycle after seeding (well inside 300s) → grace active."""
    assert within_seed_grace(SEED_STR, now=_at(1.0)) is True
    assert within_seed_grace(SEED_STR, now=_at(299.0)) is True


def test_grace_expires_after_window():
    """Past the 300s window → grace inactive; exits are evaluated normally."""
    assert within_seed_grace(SEED_STR, now=_at(300.0)) is False
    assert within_seed_grace(SEED_STR, now=_at(400.0)) is False


def test_old_seed_is_not_in_grace():
    """Regression: a seed from days ago must NOT still be in grace.

    This is the exact scenario that broke AMD/GOOG/AVGO/QQQ — seeded 2026-08-08,
    still "in grace" every cycle under the old created_at-anchored comparison.
    """
    assert within_seed_grace(SEED_STR) is False  # default now() is well past SEED


def test_boundary_is_exclusive():
    """The window is [0, 300): exactly 300s is outside the grace period."""
    assert within_seed_grace(SEED_STR, now=_at(SEED_GRACE_SECONDS)) is False


def test_seed_marker_constant_is_300s():
    """The engine's grace window is the documented 5 minutes."""
    assert SEED_GRACE_SECONDS == 300
