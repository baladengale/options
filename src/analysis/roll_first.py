"""
Roll-First Discipline — Attempt Roll Before Hard Stop Buyback

This module implements Refinement B from MASTER_EXIT_FRAMEWORK.md:
Attempt defensive rolls before realizing losses via hard stop buyback.

Base Logic:
- Before hitting premium-multiple or delta-gate hard stops
- Attempt to roll according to existing rolling discipline rules
- Only buy back if roll fails (debit required or max rolls exceeded)
- Aligns with Tastytrade's "defend before exit" philosophy

Authority: Tastytrade's defensive roll methodology
Research: https://www.reddit.com/r/thetagang/comments/ptbsxa/the_50_rule_i_always_frown_at_it/
"""

from dataclasses import dataclass
from typing import Optional, Tuple
from enum import Enum
import logging

log = logging.getLogger(__name__)


class RollDecision(Enum):
    """Roll decision outcome"""
    ROLL_RECOMMENDED = "ROLL_RECOMMENDED"      # Roll is viable (net credit, within limits)
    HARD_STOP = "HARD_STOP"                    # Must buy back (roll failed or exhausted)
    HOLD = "HOLD"                              # No action needed yet


@dataclass
class RollAnalysis:
    """Result of roll-first analysis"""
    decision: RollDecision
    action: str                                # "ROLL", "BUY_BACK", "HOLD"
    reason: str                                # Explanation
    target_strike: Optional[float]             # Suggested roll strike (if applicable)
    target_expiry: Optional[str]               # Suggested roll expiry (if applicable)
    net_credit: Optional[float]                # Expected net credit/debit (if applicable)


class RollFirstAnalyzer:
    """
    Analyze whether to attempt roll before hard stop

    Implements Tastytrade-aligned roll-first discipline:
    1. Check if position is approaching hard stop
    2. If yes, attempt to find viable roll
    3. Only buy back if roll fails (debit or max rolls exceeded)
    """

    def __init__(
        self,
        max_rolls_per_campaign: int = 2,
        min_extension_days: int = 30,
        net_credit_only: bool = True,
        max_dte_for_roll: int = 90
    ):
        """
        Initialize with rolling constraints

        Args:
            max_rolls_per_campaign: Maximum rolls before forced exit
            min_extension_days: Minimum DTE extension to consider roll
            net_credit_only: Only roll if net credit received
            max_dte_for_roll: Maximum DTE to roll to (very long rolls discouraged)
        """
        self.max_rolls = max_rolls_per_campaign
        self.min_days = min_extension_days
        self.credit_only = net_credit_only
        self.max_dte = max_dte_for_roll

    def analyze_roll_before_exit(
        self,
        current_premium_loss_multiple: float,
        current_delta: float,
        days_to_expiry: int,
        rolls_so_far: int,
        current_premium_bid: float,
        original_premium: float,
        option_type: str = "CSP"  # "CSP" or "CC"
    ) -> RollAnalysis:
        """
        Analyze if roll should be attempted before hard stop exit

        Args:
            current_premium_loss_multiple: Current loss as multiple of original premium
            current_delta: Current option delta
            days_to_expiry: Days until expiry
            rolls_so_far: Number of rolls already done on this position
            current_premium_bid: Current bid price (to buy back)
            original_premium: Original premium received
            option_type: "CSP" or "CC"

        Returns:
            RollAnalysis with decision and recommended action
        """
        # === Step 1: Check if Hard Stop is Imminent ===

        hard_stop_imminent = self._is_hard_stop_imminent(
            premium_loss_multiple=current_premium_loss_multiple,
            delta=current_delta,
            dte=days_to_expiry,
            option_type=option_type
        )

        if not hard_stop_imminent:
            return RollAnalysis(
                decision=RollDecision.HOLD,
                action="HOLD",
                reason=(
                    f"Hard stop not imminent. Loss: {current_premium_loss_multiple:.1f}× premium, "
                    f"Delta: {current_delta:.2f}, DTE: {days_to_expiry}. "
                    f"No action needed yet."
                ),
                target_strike=None,
                target_expiry=None,
                net_credit=None
            )

        # === Step 2: Check if Roll is Still Possible ===

        # Check roll count limit
        if rolls_so_far >= self.max_rolls:
            return RollAnalysis(
                decision=RollDecision.HARD_STOP,
                action="BUY_BACK",
                reason=(
                    f"Hard stop imminent BUT max rolls exhausted ({rolls_so_far}/{self.max_rolls}). "
                    f"Must buy back to stop losses. Current loss: {current_premium_loss_multiple:.1f}× premium."
                ),
                target_strike=None,
                target_expiry=None,
                net_credit=-current_premium_bid  # Debit to buy back
            )

        # Check DTE (too late to roll if very close to expiry)
        if days_to_expiry < 7:
            return RollAnalysis(
                decision=RollDecision.HARD_STOP,
                action="BUY_BACK",
                reason=(
                    f"Hard stop imminent BUT too close to expiry ({days_to_expiry} DTE < 7). "
                    f"Cannot roll effectively. Must buy back or accept assignment."
                ),
                target_strike=None,
                target_expiry=None,
                net_credit=-current_premium_bid
            )

        # === Step 3: Calculate Roll Viability ===

        # Estimate roll parameters (simplified - would use options chain in production)
        roll_target_dte = min(days_to_expiry + self.min_days, self.max_dte)
        estimated_roll_premium = self._estimate_roll_premium(
            original_premium=original_premium,
            current_loss_multiple=current_premium_loss_multiple,
            extension_days=roll_target_dte - days_to_expiry,
            option_type=option_type
        )

        # Check if roll would be net credit
        net_credit_debit = estimated_roll_premium - current_premium_bid

        if self.credit_only and net_credit_debit < 0:
            return RollAnalysis(
                decision=RollDecision.HARD_STOP,
                action="BUY_BACK",
                reason=(
                    f"Hard stop imminent BUT roll requires debit (${abs(net_credit_debit):.2f}). "
                    f"Net-credit-only rule: must buy back instead. "
                    f"Current loss: {current_premium_loss_multiple:.1f}× premium."
                ),
                target_strike=None,
                target_expiry=None,
                net_credit=-current_premium_bid
            )

        # === Step 4: Roll is Viable ===

        return RollAnalysis(
            decision=RollDecision.ROLL_RECOMMENDED,
            action="ROLL",
            reason=(
                f"Hard stop imminent. Roll available: extend to {roll_target_dte} DTE, "
                f"net {'credit' if net_credit_debit >= 0 else 'debit'} ${abs(net_credit_debit):.2f}. "
                f"This would be roll #{rolls_so_far + 1}/{self.max_rolls}."
            ),
            target_strike=self._suggest_roll_strike(option_type, current_delta),
            target_expiry=f"+{roll_target_dte} DTE",
            net_credit=net_credit_debit
        )

    def _is_hard_stop_imminent(
        self,
        premium_loss_multiple: float,
        delta: float,
        dte: int,
        option_type: str
    ) -> bool:
        """
        Check if hard stop is about to be triggered

        Uses config rules.yaml thresholds:
        - Premium multiple: 2-3× depending on DTE
        - Delta gates: 0.50-0.60 depending on option type
        """
        # Premium multiple hard stops
        if dte >= 21:
            # Far DTE: close at 3×
            if premium_loss_multiple >= 3.0:
                return True
        elif dte >= 14:
            # Mid DTE: close at 2×
            if premium_loss_multiple >= 2.0:
                return True
        else:
            # Near DTE: close at 1.5×
            if premium_loss_multiple >= 1.5:
                return True

        # Delta gates
        if option_type == "CSP":
            if delta >= 0.60:  # CSP critical
                return True
        elif option_type == "CC":
            if delta >= 0.50:  # CC critical
                return True

        return False

    def _estimate_roll_premium(
        self,
        original_premium: float,
        current_loss_multiple: float,
        extension_days: int,
        option_type: str
    ) -> float:
        """
        Estimate roll premium (simplified heuristic)

        In production, this would query the actual options chain for the target expiry/strike.
        Here we use a heuristic: roll premium ≈ original premium adjusted for time extension.

        This is a rough approximation for logic flow only.
        """
        # Time value decays roughly with sqrt(time)
        # This is a very rough heuristic
        time_ratio = (extension_days / 30.0) ** 0.5
        estimated_premium = original_premium * (0.7 + 0.3 * time_ratio)
        return estimated_premium

    def _suggest_roll_strike(self, option_type: str, current_delta: float) -> float:
        """
        Suggest roll strike based on current delta

        In production, this would analyze the options chain for optimal roll strike.
        Here we return a placeholder indicating the logic.
        """
        # Placeholder - in production, would use options chain data
        # to find strike that restores delta to ~0.30 range
        return 0.0  # Placeholder


def analyze_roll_first(
    premium_loss_multiple: float,
    current_delta: float,
    days_to_expiry: int,
    rolls_so_far: int,
    current_premium_bid: float,
    original_premium: float,
    option_type: str = "CSP"
) -> RollAnalysis:
    """
    Convenience function for roll-first analysis

    Args:
        premium_loss_multiple: Current loss as multiple of original premium
        current_delta: Current option delta
        days_to_expiry: Days until expiry
        rolls_so_far: Number of rolls already done
        current_premium_bid: Current bid price
        original_premium: Original premium received
        option_type: "CSP" or "CC"

    Returns:
        RollAnalysis with decision and action
    """
    analyzer = RollFirstAnalyzer()
    return analyzer.analyze_roll_before_exit(
        current_premium_loss_multiple=premium_loss_multiple,
        current_delta=current_delta,
        days_to_expiry=days_to_expiry,
        rolls_so_far=rolls_so_far,
        current_premium_bid=current_premium_bid,
        original_premium=original_premium,
        option_type=option_type
    )


if __name__ == "__main__":
    # Test the roll-first analyzer
    print("=" * 70)
    print("ROLL-FIRST DISCIPLINE TEST")
    print("=" * 70)

    analyzer = RollFirstAnalyzer()

    # Test scenarios
    scenarios = [
        # (loss_multiple, delta, dte, rolls, bid, original, type, description)
        (1.5, -0.35, 35, 0, 2.50, 3.00, "CSP", "Early loss - roll available"),
        (2.5, -0.55, 25, 1, 4.00, 3.00, "CSP", "Delta gate hit - roll if possible"),
        (3.2, -0.65, 20, 2, 5.50, 3.00, "CSP", "Hard stop + max rolls exhausted"),
        (0.8, -0.25, 10, 0, 1.50, 2.00, "CSP", "Near expiry - cannot roll"),
        (1.8, 0.52, 28, 0, 3.20, 2.50, "CC", "CC delta gate - roll available"),
    ]

    for loss_mult, delta, dte, rolls, bid, orig, opt_type, desc in scenarios:
        print(f"\n📊 SCENARIO: {desc}")
        print(f"   Loss: {loss_mult:.1f}× premium | Delta: {delta:.2f} | DTE: {dte}")
        print(f"   Rolls so far: {rolls} | Bid: ${bid:.2f} | Original: ${orig:.2f}")

        analysis = analyzer.analyze_roll_before_exit(
            current_premium_loss_multiple=loss_mult,
            current_delta=delta,
            days_to_expiry=dte,
            rolls_so_far=rolls,
            current_premium_bid=bid,
            original_premium=orig,
            option_type=opt_type
        )

        print(f"\n   → DECISION: {analysis.decision.value}")
        print(f"      Action: {analysis.action}")
        print(f"      Reason: {analysis.reason}")
        if analysis.net_credit is not None:
            print(f"      Net {'Credit' if analysis.net_credit >= 0 else 'Debit'}: ${abs(analysis.net_credit):.2f}")

    print("\n" + "=" * 70)
    print("✅ Roll-first discipline test complete")
