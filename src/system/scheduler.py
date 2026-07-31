"""
Systematic Timeline for OIE Framework
Enforces weekly/monthly reviews, eliminates daily decisions

This module implements the systematic timeline framework to prevent trading drift
by enforcing scheduled reviews and eliminating daily decision-making.

Research-backed approach:
- Weekly thesis validation (Monday 9AM)
- Monthly guardrail checks (1st of month 9AM)
- Expiry processing only (Friday 4PM)
- Daily status checks only (no decisions)
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional
import logging

log = logging.getLogger(__name__)


class ReviewType(Enum):
    """Types of scheduled reviews"""
    DAILY_STATUS = "daily_status_check"
    WEEKLY_THESIS = "weekly_thesis_validation"
    MONTHLY_GUARDRAIL = "monthly_guardrail_check"
    EXPIRY_PROCESSING = "expiry_processing_only"


class ScheduleConfig:
    """Configuration for systematic timeline"""

    # Weekly thesis validation: Monday 9AM
    WEEKLY_THESIS_DAY = 0  # 0 = Monday
    WEEKLY_THESIS_HOUR = 9

    # Expiry processing: Friday 4PM (16:00)
    EXPIRY_PROCESSING_DAY = 4  # 4 = Friday
    EXPIRY_PROCESSING_HOUR = 16

    # Monthly guardrail check: 1st of month 9AM
    MONTHLY_GUARDRAIL_DAY = 1
    MONTHLY_GUARDRAIL_HOUR = 9

    # Daily status check: 9AM
    DAILY_STATUS_HOUR = 9


def get_scheduled_action_type(current_time: Optional[datetime] = None) -> ReviewType:
    """
    Determine what type of action is appropriate for current time

    Args:
        current_time: DateTime to check (defaults to now)

    Returns:
        ReviewType indicating what type of review should be performed

    Example:
        >>> review_type = get_scheduled_action_type()
        >>> if review_type == ReviewType.WEEKLY_THESIS:
        ...     print("Time for weekly thesis validation")
    """
    if current_time is None:
        current_time = datetime.now()

    day_of_week = current_time.weekday()  # 0=Monday, 6=Sunday
    hour = current_time.hour
    day_of_month = current_time.day

    # Monday 9AM: Weekly Thesis Validation
    if day_of_week == ScheduleConfig.WEEKLY_THESIS_DAY and hour == ScheduleConfig.WEEKLY_THESIS_HOUR:
        return ReviewType.WEEKLY_THESIS

    # Friday 4PM: Expiry Processing
    if day_of_week == ScheduleConfig.EXPIRY_PROCESSING_DAY and hour == ScheduleConfig.EXPIRY_PROCESSING_HOUR:
        return ReviewType.EXPIRY_PROCESSING

    # First day of month 9AM: Monthly Guardrail Check
    if day_of_month == ScheduleConfig.MONTHLY_GUARDRAIL_DAY and hour == ScheduleConfig.MONTHLY_GUARDRAIL_HOUR:
        return ReviewType.MONTHLY_GUARDRAIL

    # Default: Daily status only (no decisions)
    return ReviewType.DAILY_STATUS


def is_review_time(review_type: ReviewType, current_time: Optional[datetime] = None) -> bool:
    """
    Check if it's time for a specific type of review

    Args:
        review_type: Type of review to check
        current_time: DateTime to check (defaults to now)

    Returns:
        True if it's time for this review type
    """
    return get_scheduled_action_type(current_time) == review_type


def next_review_time(review_type: ReviewType, current_time: Optional[datetime] = None) -> datetime:
    """
    Calculate the next scheduled time for a specific review type

    Args:
        review_type: Type of review
        current_time: Starting time (defaults to now)

    Returns:
        datetime of next scheduled review

    Example:
        >>> next_thesis = next_review_time(ReviewType.WEEKLY_THESIS)
        >>> print(f"Next thesis review: {next_thesis}")
    """
    if current_time is None:
        current_time = datetime.now()

    if review_type == ReviewType.WEEKLY_THESIS:
        # Next Monday at 9AM
        days_until_monday = (ScheduleConfig.WEEKLY_THESIS_DAY - current_time.weekday()) % 7
        if days_until_monday == 0 and current_time.hour >= ScheduleConfig.WEEKLY_THESIS_HOUR:
            days_until_monday = 7  # Next Monday if already passed today

        next_time = current_time + timedelta(days=days_until_monday)
        next_time = next_time.replace(hour=ScheduleConfig.WEEKLY_THESIS_HOUR, minute=0, second=0, microsecond=0)

    elif review_type == ReviewType.EXPIRY_PROCESSING:
        # Next Friday at 4PM
        days_until_friday = (ScheduleConfig.EXPIRY_PROCESSING_DAY - current_time.weekday()) % 7
        if days_until_friday == 0 and current_time.hour >= ScheduleConfig.EXPIRY_PROCESSING_HOUR:
            days_until_friday = 7  # Next Friday if already passed today

        next_time = current_time + timedelta(days=days_until_friday)
        next_time = next_time.replace(hour=ScheduleConfig.EXPIRY_PROCESSING_HOUR, minute=0, second=0, microsecond=0)

    elif review_type == ReviewType.MONTHLY_GUARDRAIL:
        # 1st of next month at 9AM
        if current_time.day == 1 and current_time.hour < ScheduleConfig.MONTHLY_GUARDRAIL_HOUR:
            # Still this month, just later today
            next_time = current_time.replace(hour=ScheduleConfig.MONTHLY_GUARDRAIL_HOUR, minute=0, second=0, microsecond=0)
        else:
            # First of next month
            if current_time.month == 12:
                next_time = current_time.replace(year=current_time.year + 1, month=1, day=1,
                                                 hour=ScheduleConfig.MONTHLY_GUARDRAIL_HOUR, minute=0, second=0, microsecond=0)
            else:
                next_time = current_time.replace(month=current_time.month + 1, day=1,
                                                 hour=ScheduleConfig.MONTHLY_GUARDRAIL_HOUR, minute=0, second=0, microsecond=0)

    else:  # DAILY_STATUS
        # Tomorrow at 9AM (or today if before 9AM)
        if current_time.hour < ScheduleConfig.DAILY_STATUS_HOUR:
            next_time = current_time.replace(hour=ScheduleConfig.DAILY_STATUS_HOUR, minute=0, second=0, microsecond=0)
        else:
            next_time = (current_time + timedelta(days=1)).replace(hour=ScheduleConfig.DAILY_STATUS_HOUR, minute=0, second=0, microsecond=0)

    return next_time


def get_review_schedule() -> Dict[ReviewType, Dict[str, str]]:
    """
    Get the complete review schedule

    Returns:
        Dict mapping review types to their descriptions and frequencies
    """
    return {
        ReviewType.DAILY_STATUS: {
            "frequency": "Daily at 9AM",
            "description": "Position status check only (no decisions)",
            "actions": ["Check position status", "Update P&L", "No trading decisions"]
        },
        ReviewType.WEEKLY_THESIS: {
            "frequency": "Every Monday at 9AM",
            "description": "Weekly thesis validation (automatic if broken)",
            "actions": ["Validate thesis for all positions", "Auto-exit if thesis broken", "Monitor if thesis damaged"]
        },
        ReviewType.EXPIRY_PROCESSING: {
            "frequency": "Every Friday at 4PM",
            "description": "Expiry processing only",
            "actions": ["Process expiring positions", "Handle assignments", "No early closes"]
        },
        ReviewType.MONTHLY_GUARDRAIL: {
            "frequency": "1st of every month at 9AM",
            "description": "Guardrail review",
            "actions": ["Check position limits", "Check sector concentration", "Check CSP deployment", "Verify cash buffer"]
        }
    }


def should_allow_trading_decisions(current_time: Optional[datetime] = None) -> bool:
    """
    Determine if trading decisions should be allowed at current time

    Returns:
        True if decisions allowed, False if status-check-only time

    Example:
        >>> if not should_allow_trading_decisions():
        ...     print("Status check only - no trading decisions")
    """
    review_type = get_scheduled_action_type(current_time)

    # Only weekly thesis validation allows decisions (and only for exits)
    # Daily status is read-only
    # Monthly guardrail is monitoring only
    # Expiry processing is automatic

    return review_type == ReviewType.WEEKLY_THESIS


def get_system_status(current_time: Optional[datetime] = None) -> Dict[str, any]:
    """
    Get comprehensive system status including current review type and schedule

    Returns:
        Dict with current review type, next reviews, and trading status
    """
    if current_time is None:
        current_time = datetime.now()

    current_review = get_scheduled_action_type(current_time)
    trading_allowed = should_allow_trading_decisions(current_time)

    return {
        "current_time": current_time,
        "current_review": current_review.value,
        "trading_decisions_allowed": trading_allowed,
        "next_reviews": {
            "daily_status": next_review_time(ReviewType.DAILY_STATUS, current_time).isoformat(),
            "weekly_thesis": next_review_time(ReviewType.WEEKLY_THESIS, current_time).isoformat(),
            "expiry_processing": next_review_time(ReviewType.EXPIRY_PROCESSING, current_time).isoformat(),
            "monthly_guardrail": next_review_time(ReviewType.MONTHLY_GUARDRAIL, current_time).isoformat()
        }
    }


# Review execution functions (to be implemented)

def daily_status_check():
    """
    Daily 9AM: Position status only, no decisions

    This is a status check function that should NOT trigger any trading decisions.
    It's for monitoring only.
    """
    log.info("Running daily status check (read-only)")

    # This will be implemented to check position status only
    # No trading decisions allowed

    return {
        "review_type": ReviewType.DAILY_STATUS.value,
        "timestamp": datetime.now().isoformat(),
        "message": "Daily status check complete - no actions required"
    }


def weekly_thesis_validation():
    """
    Weekly Monday 9AM: Validate thesis for all positions

    This is the ONLY time automated exit decisions should be made based on
    thesis validation. All other times are for monitoring only.
    """
    log.info("Running weekly thesis validation")

    # This will be implemented to:
    # 1. Get all open positions
    # 2. Validate thesis for each position
    # 3. Auto-exit if thesis broken (no user choice required)
    # 4. Monitor if thesis damaged (re-check in 1 week)
    # 5. Hold if thesis intact (continue Wheel)

    return {
        "review_type": ReviewType.WEEKLY_THESIS.value,
        "timestamp": datetime.now().isoformat(),
        "message": "Weekly thesis validation complete"
    }


def monthly_guardrail_check():
    """
    Monthly: Check all guardrails and concentrations

    This is a monitoring function to ensure portfolio stays within limits.
    Should NOT trigger automatic exits, only warnings and blocks on new positions.
    """
    log.info("Running monthly guardrail check")

    # This will be implemented to:
    # 1. Check position concentration limits
    # 2. Check sector concentration limits
    # 3. Check CSP deployment limits
    # 4. Check cash buffer levels
    # 5. Generate warnings or blocks as needed

    return {
        "review_type": ReviewType.MONTHLY_GUARDRAIL.value,
        "timestamp": datetime.now().isoformat(),
        "message": "Monthly guardrail check complete"
    }


def process_expiring_positions():
    """
    Friday 4PM: Process expirations only

    This function handles options that are expiring. It should NOT close
    positions early. Only process actual expirations and assignments.
    """
    log.info("Processing expiring positions")

    # This will be implemented to:
    # 1. Find positions expiring today (DTE = 0)
    # 2. Determine if ITM or OTM
    # 3. Process OTM expirations (remove position, keep premium)
    # 4. Process ITM assignments (buy/sell shares)
    # 5. NO early closes - let positions play out

    return {
        "review_type": ReviewType.EXPIRY_PROCESSING.value,
        "timestamp": datetime.now().isoformat(),
        "message": "Expiry processing complete"
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
        if review_type == ReviewType.DAILY_STATUS:
            return daily_status_check()
        elif review_type == ReviewType.WEEKLY_THESIS:
            return weekly_thesis_validation()
        elif review_type == ReviewType.MONTHLY_GUARDRAIL:
            return monthly_guardrail_check()
        elif review_type == ReviewType.EXPIRY_PROCESSING:
            return process_expiring_positions()
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


# Helper functions for displaying schedule

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
    if current_time is None:
        current_time = datetime.now()

    summary = "📅 NEXT SCHEDULED REVIEWS\n\n"

    next_reviews = {
        ReviewType.WEEKLY_THESIS: "Weekly Thesis Validation",
        ReviewType.EXPIRY_PROCESSING: "Expiry Processing",
        ReviewType.MONTHLY_GUARDRAIL: "Monthly Guardrail Check",
        ReviewType.DAILY_STATUS: "Daily Status Check"
    }

    for review_type, name in next_reviews.items():
        next_time = next_review_time(review_type, current_time)
        delta = next_time - current_time
        summary += f"**{name}**: {next_time.strftime('%Y-%m-%d %I:%M %p')} (in {delta.days} days)\n"

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
