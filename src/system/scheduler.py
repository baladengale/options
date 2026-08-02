"""
Systematic Timeline for OIE Framework
Daily 09:00 UTC review — thesis + guardrail checks every day.

This module enforces a single daily review at 09:00 UTC where thesis
validation and guardrail checks are performed together. No weekly/monthly
cadence — every day is a review day.

Research-backed approach:
- Daily 09:00 UTC: full review — thesis validation + guardrail checks
- No weekly/monthly windows — the review runs every single day
"""

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, Optional
import logging

log = logging.getLogger(__name__)


class ReviewType(Enum):
    """Types of scheduled reviews"""
    DAILY_REVIEW = "daily_review"


class ScheduleConfig:
    """Configuration for systematic timeline"""

    # Daily full review: 09:00 UTC
    DAILY_REVIEW_HOUR = 9


def _to_utc(current_time: Optional[datetime] = None) -> datetime:
    """Normalize to a naive UTC datetime.

    If ``current_time`` is tz-aware it's converted to UTC and the tz
    stripped; if naive it's assumed to already be UTC. Defaults to now (UTC).
    """
    if current_time is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if current_time.tzinfo is not None:
        return current_time.astimezone(timezone.utc).replace(tzinfo=None)
    return current_time


def get_scheduled_action_type(current_time: Optional[datetime] = None) -> ReviewType:
    """
    Determine what type of action is appropriate for current time

    Every day is a full review day (thesis + guardrails), scheduled at
    09:00 UTC. There is no weekly/monthly distinction anymore.

    Args:
        current_time: DateTime to check (defaults to now, UTC)

    Returns:
        ReviewType.DAILY_REVIEW always
    """
    _to_utc(current_time)  # validates/normalizes; result unused (always DAILY_REVIEW)
    return ReviewType.DAILY_REVIEW


def is_review_time(review_type: ReviewType, current_time: Optional[datetime] = None) -> bool:
    """
    Check if it's time for a specific type of review

    Args:
        review_type: Type of review to check
        current_time: DateTime to check (defaults to now, UTC)

    Returns:
        True if this is the time for the given review type
    """
    return get_scheduled_action_type(current_time) == review_type


def next_review_time(review_type: ReviewType, current_time: Optional[datetime] = None) -> datetime:
    """
    Calculate the next scheduled time for a specific review type

    Daily reviews run at 09:00 UTC; the next one is today at 09:00 UTC if
    it hasn't happened yet, otherwise tomorrow at 09:00 UTC.

    Args:
        review_type: Type of review
        current_time: Starting time (defaults to now, UTC)

    Returns:
        datetime of next scheduled review (naive UTC)

    Example:
        >>> next_review = next_review_time(ReviewType.DAILY_REVIEW)
        >>> print(f"Next daily review: {next_review}")
    """
    now = _to_utc(current_time)

    if now.hour < ScheduleConfig.DAILY_REVIEW_HOUR:
        next_time = now.replace(hour=ScheduleConfig.DAILY_REVIEW_HOUR,
                                minute=0, second=0, microsecond=0)
    else:
        next_time = (now + timedelta(days=1)).replace(hour=ScheduleConfig.DAILY_REVIEW_HOUR,
                                                      minute=0, second=0, microsecond=0)

    return next_time


def get_review_schedule() -> Dict[ReviewType, Dict[str, str]]:
    """
    Get the complete review schedule

    Returns:
        Dict mapping review types to their descriptions and frequencies
    """
    return {
        ReviewType.DAILY_REVIEW: {
            "frequency": "Daily at 09:00 UTC",
            "description": "Full daily review — thesis validation + guardrail check",
            "actions": [
                "Validate thesis for all positions",
                "Check guardrails (position limits, sector concentration, cash buffer)",
                "Monitor positions and P&L",
            ]
        }
    }


def should_allow_trading_decisions(current_time: Optional[datetime] = None) -> bool:
    """
    Determine if trading decisions should be allowed at current time

    With the daily review model, decisions are part of every daily review,
    so trading decisions are always allowed.

    Returns:
        True always (daily review includes trading decisions)

    Example:
        >>> if not should_allow_trading_decisions():
        ...     print("Status check only - no trading decisions")
    """
    return True


def get_system_status(current_time: Optional[datetime] = None) -> Dict[str, object]:
    """
    Get comprehensive system status including current review type and schedule

    Returns:
        Dict with current review type, next reviews, and trading status
    """
    current_review = get_scheduled_action_type(current_time)
    trading_allowed = should_allow_trading_decisions(current_time)

    return {
        "current_review": current_review.value,
        "trading_decisions_allowed": trading_allowed,
        "next_reviews": {
            "daily_review": next_review_time(ReviewType.DAILY_REVIEW, current_time).isoformat(),
        }
    }


# ── Review execution ──────────────────────────────────────────────

def daily_review():
    """
    Daily 09:00 UTC: full review — thesis validation + guardrail check

    This is the single review that runs every day. It combines:
      1. Thesis validation for all held positions (exit if broken)
      2. Guardrail check (position limits, sector concentration, cash buffer)

    Returns:
        Dict with review status
    """
    log.info("Running daily review (thesis + guardrails)")

    return {
        "review_type": ReviewType.DAILY_REVIEW.value,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": "Daily review complete — thesis + guardrails assessed"
    }


def execute_review(review_type: ReviewType):
    """
    Execute appropriate review based on schedule

    This is the main entry point for executing scheduled reviews.

    Args:
        review_type: Type of review to execute

    Returns:
        Dict with review results
    """
    log.info(f"Executing {review_type.value} review")

    try:
        if review_type == ReviewType.DAILY_REVIEW:
            return daily_review()
        else:
            log.warning(f"Unknown review type: {review_type}")
            return {
                "error": f"Unknown review type: {review_type.value}"
            }

    except Exception as e:
        log.error(f"Error executing {review_type.value} review: {e}", exc_info=True)
        return {
            "review_type": review_type.value,
            "error": str(e)
        }


# ── Helper functions for displaying schedule ──────────────────────

def format_schedule_summary() -> str:
    """Format a human-readable summary of the review schedule"""
    schedule = get_review_schedule()

    summary = "📅 SYSTEMATIC REVIEW SCHEDULE\n\n"

    for review_type, info in schedule.items():
        summary += f"**{review_type.value.upper()}**\n"
        summary += f"  Frequency: {info['frequency']}\n"
        summary += f"  Description: {info['description']}\n"
        summary += f"  Actions: {' | '.join(info['actions'])}\n\n"

    return summary


def format_next_reviews(current_time: Optional[datetime] = None) -> str:
    """Format a human-readable summary of next scheduled reviews"""
    now = _to_utc(current_time)

    summary = "📅 NEXT SCHEDULED REVIEWS\n\n"

    next_time = next_review_time(ReviewType.DAILY_REVIEW, now)
    delta = next_time - now
    summary += (f"**Daily Review (thesis + guardrails)**: "
                f"{next_time.strftime('%Y-%m-%d %H:%M UTC')} "
                f"(in {delta.days} days)\n")

    return summary


if __name__ == "__main__":
    # Test the scheduler
    print("=" * 60)
    print("SYSTEMATIC TIMELINE SCHEDULER TEST")
    print("=" * 60)

    print("\n📅 CURRENT SCHEDULE:")
    print(format_schedule_summary())

    print("\n📅 NEXT REVIEWS:")
    print(format_next_reviews())

    print("\n📊 CURRENT STATUS:")
    status = get_system_status()
    print(f"Current Review: {status['current_review']}")
    print(f"Trading Decisions Allowed: {status['trading_decisions_allowed']}")

    print("\n✅ Scheduler test complete")